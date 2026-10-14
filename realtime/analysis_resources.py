"""Keep offline finish analysis below CycleRace and the recorder.

The analysis runs in its own process. It lowers that process's priority,
asks Windows to treat it as an efficiency (EcoQoS) process, limits OpenCV to
one worker thread and pauses while the whole computer is busy. Pausing only
delays the problem list; it never touches timing or recording.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional


logger = logging.getLogger("FinishReview.AnalysisResources")

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
_PROCESS_POWER_THROTTLING = 4
_POWER_THROTTLING_CURRENT_VERSION = 1
_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def lower_process_priority(*, idle: bool = False) -> bool:
    """Lower this process's CPU priority and enable efficiency mode if possible."""

    lowered = False
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentProcess()
            priority = IDLE_PRIORITY_CLASS if idle else BELOW_NORMAL_PRIORITY_CLASS
            lowered = bool(kernel32.SetPriorityClass(handle, priority))

            class _PowerThrottling(ctypes.Structure):
                _fields_ = [
                    ("Version", ctypes.c_ulong),
                    ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong),
                ]

            state = _PowerThrottling(
                _POWER_THROTTLING_CURRENT_VERSION,
                _POWER_THROTTLING_EXECUTION_SPEED,
                _POWER_THROTTLING_EXECUTION_SPEED,
            )
            kernel32.SetProcessInformation(
                handle, _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state),
            )
        except (AttributeError, OSError) as error:  # pragma: no cover - platform specific
            logger.warning("could not lower analysis priority: %s", error)
    else:  # pragma: no cover - non-Windows development hosts
        try:
            os.nice(10 if not idle else 19)
            lowered = True
        except OSError:
            lowered = False
    return lowered


def limit_opencv_threads(count: int = 1) -> None:
    try:
        import cv2

        cv2.setNumThreads(int(count))
    except ImportError:  # pragma: no cover - OpenCV is a declared dependency
        pass


class CpuMonitor:
    """Whole-computer CPU load from ``GetSystemTimes`` (no psutil)."""

    def __init__(self) -> None:
        self._previous = self._times()

    @staticmethod
    def _times() -> Optional[tuple[int, int]]:
        if sys.platform != "win32":
            try:
                with open("/proc/stat", encoding="ascii") as handle:
                    values = [int(value) for value in handle.readline().split()[1:]]
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                return idle, sum(values)
            except (OSError, ValueError, IndexError):
                return None
        try:
            idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None
            # Kernel time already includes idle time on Windows.
            return idle.value, kernel.value + user.value
        except (AttributeError, OSError):  # pragma: no cover - platform specific
            return None

    def sample(self) -> Optional[float]:
        """Return busy fraction (0..1) since the previous sample."""

        current = self._times()
        previous, self._previous = self._previous, current
        if current is None or previous is None:
            return None
        idle = current[0] - previous[0]
        total = current[1] - previous[1]
        if total <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - idle / total))


MIN_AVAILABLE_MEMORY_MB = 1536


def available_memory_mb() -> Optional[int]:
    """Free physical memory of the whole computer in MB, or ``None``."""

    if sys.platform != "win32":
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys // (1024 * 1024))
    except (AttributeError, OSError):  # pragma: no cover - platform specific
        return None


def peak_memory_mb() -> Optional[int]:
    """Peak working set of this process in MB, or ``None``."""

    if sys.platform != "win32":
        try:
            import resource

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
        except (ImportError, OSError, ValueError):
            return None
    try:
        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        function = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
        function.restype = ctypes.c_int
        # -1 is the pseudo handle of the current process.
        if not function(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize // (1024 * 1024))
    except (AttributeError, OSError):  # pragma: no cover - platform specific
        return None


@dataclass
class ResourceGovernor:
    """Pause analysis while the computer is busy; resume when it calms down."""

    pause_above: float = 0.85
    resume_below: float = 0.60
    check_interval_s: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    monitor: Optional[CpuMonitor] = None
    paused_seconds: float = 0.0
    pauses: int = 0

    def __post_init__(self) -> None:
        if self.monitor is None:
            self.monitor = CpuMonitor()
        self._last_check = 0.0

    def checkpoint(self, cancelled: Callable[[], bool] = lambda: False) -> None:
        """Call between frames or items; cheap unless a check is due."""

        now = time.monotonic()
        if now - self._last_check < self.check_interval_s:
            return
        self._last_check = now
        load = self.monitor.sample()
        if load is None or load < self.pause_above:
            return
        self.pauses += 1
        started = time.monotonic()
        logger.info("analysis paused: CPU %.0f%%", load * 100)
        while not cancelled():
            self.sleep(self.check_interval_s)
            load = self.monitor.sample()
            if load is None or load < self.resume_below:
                break
        self.paused_seconds += time.monotonic() - started


__all__ = [
    "MIN_AVAILABLE_MEMORY_MB",
    "CpuMonitor",
    "ResourceGovernor",
    "available_memory_mb",
    "peak_memory_mb",
    "limit_opencv_threads",
    "lower_process_priority",
]
