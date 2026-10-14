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
    "CpuMonitor",
    "ResourceGovernor",
    "limit_opencv_threads",
    "lower_process_priority",
]
