"""Read-only live-analysis baseline recorder and summarizer.

This tool measures a running FinishReview session from signals the application
already writes.  It never opens recordings, chip journals, settings or the
network, and it never claims a field acceptance: ``field_verified`` is always
false and must stay false.

Two commands:

    python -B -m tools.live_performance_baseline sample --pid 1234 \
        --interval 5 --duration 2100 --disk-path "D:\\recording" \
        --output "%TEMP%\\finishreview-live-samples.jsonl"

    python -B -m tools.live_performance_baseline summarize \
        --log "%LOCALAPPDATA%\\FinishReview\\logs\\finish_review.log" \
        --workspace "D:\\event" --sampler "%TEMP%\\finishreview-live-samples.jsonl" \
        --context baseline-context.json --output baseline.json [--strict]

Measured from the existing log (``realtime/review_main.py`` format):
``analysis process starting`` events (unknown on historical logs without it),
run span, recording sessions/configured cameras, live batch publication rounds
and their sampling/analysis/publish timings, new video seconds, pending sealed
segments per camera, analysis pauses, ROI fallback counts, log levels and the
exit-time ``Runtime metrics`` snapshots.

Live batch lines are printed only after a publication; they count publication
rounds, not every analysis batch or worker start.  They are never substituted
for the analysis process start count.

Measured from ``finish_problems/problem_list.json`` snapshots: sealed/scanned/
analyzed watermarks, pending segments and per-camera performance.  Feed several
snapshots (or copies taken during the run) to see the backlog trend.

Measured from the optional sampler: process CPU time/percent, working set and
free disk space.  No third-party dependency is required on Windows.

Not measurable here: whether every rider was counted, operator clicking and
judgement time, real chip receive latency or camera disconnects, GUI
responsiveness, image quality, sustained disk throughput, multi-hour stability
and whether an external CycleRace process was running (only a declared context
value).  See ``docs/LIVE_PERFORMANCE_BASELINE.md``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import statistics
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) (?P<logger>\S+) (?P<thread>[^:]+): (?P<message>.*)$"
)
LIVE_BATCH_RE = re.compile(
    r"^live batch camera=(?P<camera>\d+) video=(?P<video>[\d.]+)s "
    r"sampling=(?P<sampling>[\d.]+)s analysis=(?P<analysis>[\d.]+)s "
    r"publish=(?P<publish>[\d.]+)s total=(?P<total>[\d.]+)s "
    r"pending=(?P<pending>\d+)$"
)
RUNTIME_METRIC_RE = re.compile(
    r"^Runtime metrics operation=(?P<operation>\S+) count=(?P<count>\d+) "
    r"items_total=(?P<items>\d+) failures=(?P<failures>\d+) "
    r"p50=(?P<p50>[\d.]+)ms p95=(?P<p95>[\d.]+)ms max=(?P<max>[\d.]+)ms$"
)
RECORDING_START_RE = re.compile(r"^Recording session started for cameras: (?P<cameras>.+)$")
ANALYSIS_PROCESS_RE = re.compile(
    r"^analysis process starting live=(?P<live>\S+) finalizing=(?P<finalizing>\S+)$")
STRIP_SAMPLE_RE = re.compile(r"^sampled .+: (?P<frames>\d+) frames in (?P<seconds>[\d.]+) s ")

REQUIRED_CONTEXT = (
    "app_version",
    "device",
    "cameras",
    "cyclerace_concurrent",
    "review_concurrent",
)
NOT_MEASURED = (
    "逐人计数与漏读正确性（需要回看原帧人工核对，不能由日志证明）",
    "历史日志缺少 analysis process starting 事件时的分析启动次数（缺失保持未知，不用 live batch 行代替）",
    "操作员点击数、判读用时和整场人工工作量",
    "真实芯片接收延迟、断线重连和摄像头故障（日志只能提供暂停/错误计数）",
    "GUI 实际流畅度（仅有退出时 ui_event_loop_delay 的 p50/p95/max 汇总）",
    "画面质量、镜头对焦/曝光、终点线配置是否适合当前赛事",
    "持续磁盘写入吞吐和数小时运行稳定性（采样只记录剩余空间）",
    "外部 CycleRace 进程是否同时运行（只能读取 context 中的人工声明）",
)
DEFINITIONS = {
    "logged_rounds": "日志中 live batch 行数；每行只在一次发布后打印，不能代表所有分析 batch 或 worker 启动",
    "publications": "清单发布次数；每条 live batch 行对应一次实际发布（一行是发布时触发发布的那个机位）",
    "analysis_process_starts": "日志中 analysis process starting 事件数；历史日志没有该事件时为未知，不用 live batch 行代替",
    "processing_ratio": "sum(total)/sum(video)，即同一批新视频的墙钟耗时/视频时长；小于 1 表示跟得上",
    "processing_ratio_source": "log_rounds 为全部已知机位的日志合计；problem_list_max_camera 为清单中各机位最差处理比；不完整时为未知",
    "processing_ratio_required_cameras": "期望机位数；优先取 context.cameras.count，缺失时退回录像会话日志中的机位列表",
    "analysis_ratio": "sum(analysis)/sum(video)，只含过线检测与分析阶段，不含取样和发布",
    "pending_segments": "每轮发布后仍待分析的封存片段数；验收用每个已知机位的最新值，任一路未归零即未追平",
    "backlog_ms": "问题清单中 sealed_through_ms - analyzed_through_ms，表示已封存但尚未分析到的视频时间",
    "cpu_percent": "进程 CPU 时间增量/墙钟增量；4 核满载约 400%（按单核 100% 计）",
    "run_seconds": "优先取 start/stop 日志配对；日志被轮转截断时退回日志时间跨度",
}


def _timestamp_seconds(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace(",", ".")).timestamp()
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> bool | None:
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _distribution(values) -> dict:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "median": None, "p95": None, "max": None}

    def percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "median": round(statistics.median(ordered), 3),
        "p95": percentile(0.95),
        "max": round(ordered[-1], 3),
    }


def parse_log_lines(lines) -> dict:
    """Parse FinishReview log lines into rounds, sessions and exit metrics."""

    parsed = {
        "log_lines": 0,
        "unparsed_lines": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "log_span_seconds": None,
        "sessions": [],
        "recording_sessions": [],
        "configured_cameras": [],
        "rounds": [],
        "analysis_process_starts": [],
        "runtime_metrics": [],
        "analysis_pauses": 0,
        "roi_fallbacks": 0,
        "receiver_listening": 0,
        "strip_samples": 0,
        "strip_sampling_seconds": 0.0,
        "levels": {},
    }
    active_session: tuple[float, str] | None = None
    active_recording: tuple[float, str] | None = None
    configured_cameras: set[int] = set()
    first_seconds: float | None = None
    last_seconds: float | None = None

    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        match = LOG_LINE_RE.match(line)
        if match is None:
            parsed["unparsed_lines"] += 1
            continue
        parsed["log_lines"] += 1
        parsed["levels"][match["level"]] = parsed["levels"].get(match["level"], 0) + 1
        seconds = _timestamp_seconds(match["timestamp"])
        if seconds is not None:
            first_seconds = seconds if first_seconds is None else min(first_seconds, seconds)
            last_seconds = seconds if last_seconds is None else max(last_seconds, seconds)
        message = match["message"]

        if message.startswith("FinishReview starting"):
            if active_session is not None:
                parsed["sessions"].append({"start": active_session[1], "end": match["timestamp"],
                                           "seconds": round(seconds - active_session[0], 3)
                                           if seconds is not None else None})
            active_session = (seconds, match["timestamp"]) if seconds is not None else None
        elif message.startswith("FinishReview stopped with exit code"):
            if active_session is not None and seconds is not None:
                parsed["sessions"].append({"start": active_session[1], "end": match["timestamp"],
                                           "seconds": round(seconds - active_session[0], 3)})
                active_session = None
        elif RECORDING_START_RE.match(message):
            camera_text = RECORDING_START_RE.match(message)["cameras"]
            for token in re.split(r"[,\s]+", camera_text):
                if token.isdigit():
                    configured_cameras.add(int(token))
            if active_recording is not None and seconds is not None:
                parsed["recording_sessions"].append({
                    "start": active_recording[1], "end": match["timestamp"],
                    "cameras": active_recording[2],
                    "seconds": round(seconds - active_recording[0], 3),
                })
            active_recording = ((seconds, match["timestamp"], camera_text)
                                if seconds is not None else None)
        elif message.startswith("Recording session stopped"):
            if active_recording is not None and seconds is not None:
                parsed["recording_sessions"].append({
                    "start": active_recording[1], "end": match["timestamp"],
                    "cameras": active_recording[2],
                    "seconds": round(seconds - active_recording[0], 3),
                })
                active_recording = None
        elif message.startswith("CycleRace passage receiver listening"):
            parsed["receiver_listening"] += 1
        elif message.startswith("analysis paused:"):
            parsed["analysis_pauses"] += 1
        elif "ROI decode unavailable" in message:
            parsed["roi_fallbacks"] += 1

        process_start = ANALYSIS_PROCESS_RE.match(message)
        if process_start is not None:
            parsed["analysis_process_starts"].append({
                "at": match["timestamp"],
                "live": _as_bool(process_start["live"]),
                "finalizing": _as_bool(process_start["finalizing"]),
            })
            continue

        batch = LIVE_BATCH_RE.match(message)
        if batch is not None:
            parsed["rounds"].append({
                "at": match["timestamp"],
                "epoch_seconds": seconds,
                "camera": int(batch["camera"]),
                "video_seconds": round(float(batch["video"]), 3),
                "sampling_seconds": round(float(batch["sampling"]), 3),
                "analysis_seconds": round(float(batch["analysis"]), 3),
                "publish_seconds": round(float(batch["publish"]), 3),
                "total_seconds": round(float(batch["total"]), 3),
                "pending_segments": int(batch["pending"]),
            })
            continue

        metric = RUNTIME_METRIC_RE.match(message)
        if metric is not None:
            parsed["runtime_metrics"].append({
                "at": match["timestamp"],
                "operation": metric["operation"],
                "count": int(metric["count"]),
                "items_total": int(metric["items"]),
                "failures": int(metric["failures"]),
                "p50_ms": round(float(metric["p50"]), 3),
                "p95_ms": round(float(metric["p95"]), 3),
                "max_ms": round(float(metric["max"]), 3),
            })
            continue

        sample = STRIP_SAMPLE_RE.match(message)
        if sample is not None:
            parsed["strip_samples"] += 1
            parsed["strip_sampling_seconds"] += float(sample["seconds"])

    if active_session is not None and last_seconds is not None:
        parsed["sessions"].append({"start": active_session[1], "end": None,
                                   "seconds": round(last_seconds - active_session[0], 3)})
    if active_recording is not None and last_seconds is not None:
        parsed["recording_sessions"].append({
            "start": active_recording[1], "end": None, "cameras": active_recording[2],
            "seconds": round(last_seconds - active_recording[0], 3),
        })

    parsed["configured_cameras"] = sorted(configured_cameras)
    parsed["rounds"].sort(key=lambda value: value["epoch_seconds"] or 0.0)
    cumulative: dict[int, float] = {}
    first_round_seconds = (parsed["rounds"][0]["epoch_seconds"] if parsed["rounds"] else None)
    for value in parsed["rounds"]:
        cumulative[value["camera"]] = cumulative.get(value["camera"], 0.0) + value["video_seconds"]
        value["cumulative_video_seconds"] = round(cumulative[value["camera"]], 3)
        value["elapsed_seconds"] = (round(value["epoch_seconds"] - first_round_seconds, 3)
                                    if value["epoch_seconds"] is not None
                                    and first_round_seconds is not None else None)
    if first_seconds is not None and last_seconds is not None:
        parsed["first_timestamp"] = datetime.fromtimestamp(first_seconds).isoformat(timespec="milliseconds")
        parsed["last_timestamp"] = datetime.fromtimestamp(last_seconds).isoformat(timespec="milliseconds")
        parsed["log_span_seconds"] = round(last_seconds - first_seconds, 3)
    parsed["session_seconds"] = (round(sum(value["seconds"] or 0.0 for value in parsed["sessions"]), 3)
                                 if parsed["sessions"] else None)
    parsed["recording_seconds"] = (round(sum(value["seconds"] or 0.0 for value in parsed["recording_sessions"]), 3)
                                   if parsed["recording_sessions"] else None)
    return parsed


def parse_log_file(path: Path) -> dict:
    return parse_log_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())


def resolve_log_paths(paths) -> list[Path]:
    resolved: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            candidates = [item for item in path.glob("finish_review.log*") if item.is_file()]
            candidates.sort(key=lambda item: item.stat().st_mtime)
            resolved.extend(candidates)
        else:
            resolved.append(path)
    return resolved


def summarize_rounds(rounds) -> dict:
    by_camera: dict[str, list[dict]] = {}
    for value in rounds:
        by_camera.setdefault(str(value["camera"]), []).append(value)

    def summarize(values) -> dict:
        video = sum(value["video_seconds"] for value in values)
        sampling = sum(value["sampling_seconds"] for value in values)
        analysis = sum(value["analysis_seconds"] for value in values)
        publish = sum(value["publish_seconds"] for value in values)
        total = sum(value["total_seconds"] for value in values)
        pending = [value["pending_segments"] for value in values]
        return {
            "rounds": len(values),
            "video_seconds": round(video, 3),
            "sampling_seconds": round(sampling, 3),
            "analysis_seconds": round(analysis, 3),
            "publish_seconds": round(publish, 3),
            "round_total_seconds": round(total, 3),
            "processing_ratio": round(total / video, 4) if video > 0 else None,
            "analysis_ratio": round(analysis / video, 4) if video > 0 else None,
            "round_total_distribution": _distribution(value["total_seconds"] for value in values),
            "analysis_distribution": _distribution(value["analysis_seconds"] for value in values),
            "sampling_distribution": _distribution(value["sampling_seconds"] for value in values),
            "publish_distribution": _distribution(value["publish_seconds"] for value in values),
            "pending_segments": {
                "first": pending[0] if pending else None,
                "last": pending[-1] if pending else None,
                "max": max(pending) if pending else None,
                "recovered": pending[-1] == 0 if pending else None,
            },
        }

    cameras = {camera: summarize(values) for camera, values in sorted(by_camera.items())}
    latest_pending = {camera: value["pending_segments"]["last"]
                      for camera, value in cameras.items()
                      if value["pending_segments"]["last"] is not None}
    return {
        # The log writes one line only after a publication, so a line is a
        # published round, not necessarily every analysis batch or worker start.
        "logged_rounds": len(rounds),
        "publications": len(rounds),
        "logged_rounds_by_camera": dict(sorted((camera, value["rounds"])
                                               for camera, value in cameras.items())),
        "latest_pending_by_camera": latest_pending,
        "all_pending_recovered": (all(value == 0 for value in latest_pending.values())
                                  if latest_pending else None),
        "cameras": cameras,
        "overall": summarize(rounds),
    }


def summarize_problem_lists(payloads) -> dict:
    snapshots = []
    for payload in payloads:
        cameras = {}
        for key, value in sorted((payload.get("cameras") or {}).items()):
            if not isinstance(value, dict):
                continue
            sealed = _as_int(value.get("sealed_through_ms"))
            analyzed = _as_int(value.get("analyzed_through_ms"))
            scanned = _as_int(value.get("scanned_through_ms"))
            pending = _as_int(value.get("pending_segments"))
            backlog = sealed - analyzed if sealed is not None and analyzed is not None else None
            cameras[str(key)] = {
                "pending_segments": pending,
                "sealed_through_ms": sealed,
                "scanned_through_ms": scanned,
                "analyzed_through_ms": analyzed,
                "backlog_ms": backlog,
                "files": _as_int(value.get("files")),
                "performance": value.get("performance") if isinstance(value.get("performance"), dict) else {},
            }
        snapshots.append({
            "generated_at_ms": _as_int(payload.get("generated_at_ms")),
            "race_id": payload.get("race_id"),
            "stage_id": payload.get("stage_id"),
            "live": payload.get("live"),
            "summary": payload.get("summary") or {},
            "cameras": cameras,
        })
    snapshots.sort(key=lambda value: value["generated_at_ms"] or 0)

    series: dict[str, list[dict]] = {}
    for snapshot in snapshots:
        for camera, value in snapshot["cameras"].items():
            series.setdefault(camera, []).append({
                "generated_at_ms": snapshot["generated_at_ms"],
                "pending_segments": value["pending_segments"],
                "backlog_ms": value["backlog_ms"],
                "analyzed_through_ms": value["analyzed_through_ms"],
                "sealed_through_ms": value["sealed_through_ms"],
            })
    backups = [value["backlog_ms"] for values in series.values() for value in values
               if value["backlog_ms"] is not None]
    pendings = [value["pending_segments"] for values in series.values() for value in values
                if value["pending_segments"] is not None]
    last_by_camera = {camera: values[-1] for camera, values in series.items() if values}

    def camera_recovered(value) -> bool | None:
        signals = []
        if value["pending_segments"] is not None:
            signals.append(value["pending_segments"] == 0)
        if value["backlog_ms"] is not None:
            signals.append(value["backlog_ms"] <= 0)
        return all(signals) if signals else None

    states = [camera_recovered(value) for value in last_by_camera.values()]
    recovered = None if not states or any(state is None for state in states) else all(states)
    return {
        "snapshots": snapshots,
        "series": series,
        "max_backlog_ms": max(backups) if backups else None,
        "max_pending_segments": max(pendings) if pendings else None,
        "last_by_camera": last_by_camera,
        "recovered": recovered,
    }


def process_snapshot(pid: int, *, disk_path: Path | None = None) -> dict:
    """Read CPU time, working set and free disk without changing the process."""

    row = {
        "pid": int(pid),
        "cpu_time_seconds": None,
        "working_set_mb": None,
        "disk_free_mb": None,
        "disk_path": str(disk_path) if disk_path else None,
    }
    if os.name == "nt":
        row.update(_windows_process_snapshot(int(pid)))
    else:
        try:
            import psutil  # type: ignore
        except ImportError as error:
            raise RuntimeError(
                "process sampling on this platform needs psutil; use Windows or install psutil"
            ) from error
        try:
            process = psutil.Process(int(pid))
            times = process.cpu_times()
            row["cpu_time_seconds"] = round(float(times.user) + float(times.system), 3)
            row["working_set_mb"] = round(float(process.memory_info().rss) / (1024 * 1024), 3)
        except Exception as error:  # noqa: BLE001 - report a readable field error.
            raise RuntimeError(f"cannot read process {pid}: {error}") from error
    if disk_path is not None:
        try:
            free = shutil.disk_usage(str(disk_path)).free
        except OSError as error:
            raise RuntimeError(f"cannot read disk usage for {disk_path}: {error}") from error
        row["disk_free_mb"] = round(free / (1024 * 1024), 3)
    return row


if os.name == "nt":
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _PSAPI.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    _PSAPI.GetProcessMemoryInfo.restype = wintypes.BOOL

    def _filetime_seconds(value: _FILETIME) -> float:
        ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks / 10_000_000.0

    def _windows_process_snapshot(pid: int) -> dict:
        # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION, 0x0010 = PROCESS_VM_READ.
        handle = _KERNEL32.OpenProcess(0x1010, False, pid)
        if not handle:
            raise RuntimeError(f"cannot open process {pid}: {ctypes.WinError(ctypes.get_last_error())}")
        try:
            created, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            if not _KERNEL32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                             ctypes.byref(kernel), ctypes.byref(user)):
                raise RuntimeError(f"cannot read process {pid} times: "
                                   f"{ctypes.WinError(ctypes.get_last_error())}")
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if not _PSAPI.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                raise RuntimeError(f"cannot read process {pid} memory: "
                                   f"{ctypes.WinError(ctypes.get_last_error())}")
            return {
                "cpu_time_seconds": round(_filetime_seconds(kernel) + _filetime_seconds(user), 3),
                "working_set_mb": round(int(counters.WorkingSetSize) / (1024 * 1024), 3),
            }
        finally:
            _KERNEL32.CloseHandle(handle)
else:
    def _windows_process_snapshot(_pid: int) -> dict:
        return {}


def load_samples(paths) -> list[dict]:
    rows: list[dict] = []
    for value in paths:
        path = Path(value)
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return [row for row in rows if row.get("record_type", "sample") == "sample"]


def summarize_samples(rows) -> dict:
    valid = [row for row in rows if row.get("record_type", "sample") == "sample"]
    ordered = sorted(valid, key=lambda row: float(row.get("elapsed_seconds") or 0.0))
    sessions: dict[str, list[dict]] = {}
    for row in ordered:
        sessions.setdefault(str(row.get("session_id") or ""), []).append(row)
    durations = []
    for values in sessions.values():
        elapsed = [float(value.get("elapsed_seconds") or 0.0) for value in values]
        if elapsed:
            durations.append(max(elapsed) - min(elapsed))
    cpu = [float(row["cpu_percent"]) for row in ordered if row.get("cpu_percent") is not None]
    memory = [float(row["working_set_mb"]) for row in ordered if row.get("working_set_mb") is not None]
    disk = [float(row["disk_free_mb"]) for row in ordered if row.get("disk_free_mb") is not None]
    return {
        "samples": len(ordered),
        "sessions": len(sessions),
        "duration_seconds": round(sum(durations), 3) if durations else None,
        "cpu_percent": _distribution(cpu),
        "working_set_mb": _distribution(memory),
        "disk_free_mb": {"min": min(disk) if disk else None, "max": max(disk) if disk else None,
                         "count": len(disk)},
        "disk_path": next((row.get("disk_path") for row in ordered if row.get("disk_path")), None),
        "pid": next((row.get("pid") for row in ordered if row.get("pid") is not None), None),
    }


class _LiveSampleStats:
    """Online sampler stats with a bounded recent window for percentiles."""

    METRICS = ("cpu_percent", "working_set_mb", "disk_free_mb")

    def __init__(self, *, retain: int = 4096):
        self.retain = max(1, int(retain))
        self.samples = 0
        self._first_elapsed = None
        self._last_elapsed = None
        self._recent = {key: deque(maxlen=self.retain) for key in self.METRICS}
        self._exact = {key: {"count": 0, "sum": 0.0, "min": None, "max": None}
                       for key in self.METRICS}
        self.pid = None
        self.disk_path = None

    def add(self, row: dict) -> None:
        elapsed = float(row.get("elapsed_seconds") or 0.0)
        self.samples += 1
        self._first_elapsed = elapsed if self._first_elapsed is None else self._first_elapsed
        self._last_elapsed = elapsed
        if self.pid is None and row.get("pid") is not None:
            self.pid = row.get("pid")
        if self.disk_path is None and row.get("disk_path"):
            self.disk_path = row.get("disk_path")
        for key in self.METRICS:
            value = row.get(key)
            if value is None:
                continue
            number = float(value)
            state = self._exact[key]
            state["count"] += 1
            state["sum"] += number
            state["min"] = number if state["min"] is None else min(state["min"], number)
            state["max"] = number if state["max"] is None else max(state["max"], number)
            self._recent[key].append(number)

    def _metric_summary(self, key: str) -> dict:
        state = self._exact[key]
        recent = list(self._recent[key])
        summary = _distribution(recent)
        summary.update({
            "count": state["count"],
            "retained": len(recent),
            "min": round(state["min"], 3) if state["min"] is not None else None,
            "max": round(state["max"], 3) if state["max"] is not None else None,
            "mean": round(state["sum"] / state["count"], 3) if state["count"] else None,
        })
        return summary

    def summary(self) -> dict:
        disk = self._exact["disk_free_mb"]
        return {
            "samples": self.samples,
            "sessions": 1 if self.samples else 0,
            "duration_seconds": (round(self._last_elapsed - self._first_elapsed, 3)
                                 if self._first_elapsed is not None else None),
            "cpu_percent": self._metric_summary("cpu_percent"),
            "working_set_mb": self._metric_summary("working_set_mb"),
            "disk_free_mb": {
                "min": round(disk["min"], 3) if disk["min"] is not None else None,
                "max": round(disk["max"], 3) if disk["max"] is not None else None,
                "count": disk["count"],
            },
            "disk_path": self.disk_path,
            "pid": self.pid,
            "retained_per_metric": self.retain,
            "distribution_basis": "median/p95 使用最近保留的样本；count/min/max/mean 为全程精确值",
        }


def _context_missing(context: dict) -> list[str]:
    return [key for key in REQUIRED_CONTEXT if context.get(key) in (None, "", [], {})]


def _required_camera_count(context: dict) -> int | None:
    cameras = (context or {}).get("cameras")
    if isinstance(cameras, int) and cameras > 0:
        return cameras
    if isinstance(cameras, dict):
        count = _as_int(cameras.get("count"))
        if count is not None and count > 0:
            return count
    return None


def _log_processing_ratio(rounds: dict, required_cameras: int | None) -> tuple:
    by_camera = {camera: value["processing_ratio"] for camera, value in rounds["cameras"].items()}
    known = {camera: value for camera, value in by_camera.items() if value is not None}
    if not known:
        return None, "not_available", by_camera
    if required_cameras is not None and len(known) < required_cameras:
        return None, "log_rounds_incomplete", by_camera
    return rounds["overall"]["processing_ratio"], "log_rounds", by_camera


def _problem_list_processing_ratio(progress: dict, required_cameras: int | None) -> tuple:
    if not progress["snapshots"]:
        return None, "problem_list_missing", {}
    latest = progress["snapshots"][-1]
    by_camera = {}
    for camera, value in latest["cameras"].items():
        performance = value.get("performance") or {}
        ratio = performance.get("processing_ratio")
        by_camera[camera] = float(ratio) if ratio is not None else None
    if not by_camera:
        return None, "problem_list_no_camera_performance", {}
    if any(value is None for value in by_camera.values()):
        return None, "problem_list_incomplete", by_camera
    if required_cameras is not None and len(by_camera) < required_cameras:
        return None, "problem_list_incomplete", by_camera
    # The list has no combined processing ratio.  Use the worst camera so one
    # fast lane cannot make the whole run look acceptable.
    return max(by_camera.values()), "problem_list_max_camera", by_camera


def build_report(parsed: dict, *, problem_lists=(), samples=(), context=None, sources=None,
                 required_minutes: float = 30.0, target_ratio: float = 0.5) -> dict:
    rounds = summarize_rounds(parsed.get("rounds") or [])
    progress = summarize_problem_lists(problem_lists)
    sampler = summarize_samples(samples)
    context = dict(context or {})
    missing = _context_missing(context)
    overall = rounds["overall"]
    required_cameras = _required_camera_count(context)
    required_cameras_source = "context" if required_cameras is not None else None
    if required_cameras is None and parsed.get("configured_cameras"):
        required_cameras = len(parsed["configured_cameras"])
        required_cameras_source = "recording_log"
    log_ratio, log_source, log_by_camera = _log_processing_ratio(rounds, required_cameras)
    if log_ratio is not None:
        processed_ratio, ratio_source, ratio_by_camera = log_ratio, log_source, log_by_camera
    else:
        problem_ratio, problem_source, problem_by_camera = _problem_list_processing_ratio(
            progress, required_cameras)
        if problem_ratio is not None:
            processed_ratio, ratio_source, ratio_by_camera = (
                problem_ratio, problem_source, problem_by_camera)
        elif log_source == "log_rounds_incomplete":
            processed_ratio, ratio_source, ratio_by_camera = None, "log_rounds_incomplete", log_by_camera
        else:
            processed_ratio, ratio_source, ratio_by_camera = None, problem_source, problem_by_camera
    observed_seconds = parsed.get("session_seconds")
    if observed_seconds is None:
        observed_seconds = parsed.get("log_span_seconds")
    has_runtime_metrics = bool(parsed.get("runtime_metrics"))
    failures = sum(value["failures"] for value in parsed.get("runtime_metrics") or []) if has_runtime_metrics else None
    process_start_events = parsed.get("analysis_process_starts") or []
    process_starts = len(process_start_events) if process_start_events else None

    pending_candidates: dict[str, list[int]] = {}
    for camera, value in rounds["latest_pending_by_camera"].items():
        pending_candidates.setdefault(camera, []).append(int(value))
    for camera, value in progress["last_by_camera"].items():
        if value["pending_segments"] is not None:
            pending_candidates.setdefault(camera, []).append(int(value["pending_segments"]))
    # A single overall "last line" can belong to only one camera and hide the
    # other lane.  Keep the worst latest value for every known camera.
    latest_pending_by_camera = {camera: max(values) for camera, values in sorted(pending_candidates.items())}
    all_pending_recovered = (all(value == 0 for value in latest_pending_by_camera.values())
                             if latest_pending_by_camera else None)

    missing_signals = []
    if not parsed.get("log_lines"):
        missing_signals.append("日志中没有可解析行；运行时长、分析轮次和阶段耗时缺失")
    elif not rounds["logged_rounds"]:
        missing_signals.append("日志中没有 live batch 行；逐轮耗时、发布轮次和处理比缺失；"
                               "分析启动次数本身也没有独立日志事件")
    if not parsed.get("sessions") and parsed.get("log_span_seconds") is None:
        missing_signals.append("无法确定运行时长")
    if not parsed.get("runtime_metrics"):
        missing_signals.append("进程退出前没有 Runtime metrics 摘要；内部操作 p50/p95 与失败计数缺失")
    if process_starts is None:
        missing_signals.append("日志没有 analysis process starting 事件；分析启动次数未知"
                               "（历史日志不含该事件，不能用 live batch 行代替）")
    if not progress["snapshots"]:
        missing_signals.append("没有 problem_list.json 快照；封存/分析水位和 backlog_ms 缺失")
    if not sampler["samples"]:
        missing_signals.append("没有进程采样；CPU、内存和工作目录剩余空间缺失")
    if missing:
        missing_signals.append("现场 context 缺少：" + "、".join(missing))

    def gate(value, passed):
        return {"value": value, "pass": passed}

    required_seconds = round(float(required_minutes) * 60.0, 3)
    gates = {
        "run_at_least_required_minutes": {
            "value_seconds": observed_seconds,
            "required_seconds": required_seconds,
            "pass": None if observed_seconds is None else observed_seconds >= required_seconds,
        },
        "processing_ratio_below_one": gate(
            processed_ratio, None if processed_ratio is None else processed_ratio < 1.0),
        "processing_ratio_at_or_below_target": gate(
            processed_ratio, None if processed_ratio is None else processed_ratio <= float(target_ratio)),
        "runtime_metric_failures_zero": gate(
            failures, None if failures is None else failures == 0),
        "pending_recovered_by_end": {
            "value": {"by_camera": latest_pending_by_camera,
                      "max": max(latest_pending_by_camera.values(), default=None)},
            "pass": all_pending_recovered,
        },
        "problem_list_backlog_recovered": gate(
            progress["recovered"], progress["recovered"]),
        "context_complete": {
            "missing": missing,
            "pass": not missing,
        },
    }
    known = [value for value in gates.values() if value.get("pass") is not None]
    failed = [key for key, value in gates.items() if value.get("pass") is False]
    targets_met = False if failed else (True if len(known) == len(gates) else None)
    return {
        "schema_version": 1,
        "tool": "tools/live_performance_baseline.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sources or {},
        "context": context,
        "context_missing": missing,
        "field_verified": False,
        "observed": {
            "run_seconds": observed_seconds,
            "session_seconds": parsed.get("session_seconds"),
            "log_span_seconds": parsed.get("log_span_seconds"),
            "first_timestamp": parsed.get("first_timestamp"),
            "last_timestamp": parsed.get("last_timestamp"),
            "sessions": parsed.get("sessions") or [],
            "recording_sessions": parsed.get("recording_sessions") or [],
            "recording_seconds": parsed.get("recording_seconds"),
            "receiver_listening_events": parsed.get("receiver_listening", 0),
            "logged_rounds": rounds["logged_rounds"],
            "publications": rounds["publications"],
            "logged_rounds_by_camera": rounds["logged_rounds_by_camera"],
            "analysis_process_starts": process_starts,
            "analysis_process_start_events": process_start_events,
            "analysis_process_start_count_note": "历史日志没有该事件时为 unknown；live batch 行只代表发布后的轮次",
            "video_seconds": overall["video_seconds"],
            "sampling_seconds": overall["sampling_seconds"],
            "analysis_seconds": overall["analysis_seconds"],
            "publish_seconds": overall["publish_seconds"],
            "round_total_seconds": overall["round_total_seconds"],
            "processing_ratio": processed_ratio,
            "processing_ratio_source": ratio_source,
            "processing_ratio_by_camera": ratio_by_camera,
            "processing_ratio_required_cameras": required_cameras,
            "processing_ratio_required_cameras_source": required_cameras_source,
            "configured_cameras": parsed.get("configured_cameras") or [],
            "analysis_ratio": overall["analysis_ratio"],
            "pending_segments": {
                **overall["pending_segments"],
                "by_camera": {camera: value["pending_segments"]
                              for camera, value in rounds["cameras"].items()},
                "latest_by_camera": latest_pending_by_camera,
                "all_recovered": all_pending_recovered,
                "note": "验收门槛使用每个已知机位的最新 pending 最大值，不用单条日志的最后一行",
            },
            "round_total_distribution": overall["round_total_distribution"],
            "analysis_distribution": overall["analysis_distribution"],
            "sampling_distribution": overall["sampling_distribution"],
            "publish_distribution": overall["publish_distribution"],
            "cameras": rounds["cameras"],
            "runtime_metrics": parsed.get("runtime_metrics") or [],
            "runtime_metric_failures": failures,
            "analysis_pauses": parsed.get("analysis_pauses", 0),
            "roi_fallbacks": parsed.get("roi_fallbacks", 0),
            "strip_samples": parsed.get("strip_samples", 0),
            "strip_sampling_seconds": round(parsed.get("strip_sampling_seconds", 0.0), 3),
            "levels": parsed.get("levels") or {},
        },
        "rounds": parsed.get("rounds") or [],
        "progress": progress,
        "sampler": sampler,
        "acceptance": {
            **gates,
            "targets_met": targets_met,
            "required_minutes": required_minutes,
            "target_processing_ratio": target_ratio,
            "note": "这些是按目标阈值对已记录信号的检查，不是现场验收结论；field_verified 始终为 false",
        },
        "definitions": DEFINITIONS,
        "missing_signals": missing_signals,
        "not_measured": list(NOT_MEASURED),
    }


def _load_json_file(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def _write_report(report: dict, output: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    summary = {
        "observed": {
            key: report["observed"][key] for key in (
                "run_seconds", "logged_rounds", "analysis_process_starts", "publications",
                "video_seconds", "round_total_seconds", "processing_ratio",
                "processing_ratio_source", "analysis_ratio", "pending_segments",
            )
        },
        "acceptance": report["acceptance"],
        "missing_signals": report["missing_signals"],
        "field_verified": report["field_verified"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=1, default=str))


def _summarize_main(args) -> int:
    if not (args.log or args.problem_list or args.sampler or args.workspace):
        print("summarize needs --log, --problem-list, --sampler or --workspace", file=sys.stderr)
        return 2
    if args.required_minutes <= 0 or args.target_ratio <= 0:
        print("--required-minutes and --target-ratio must be positive", file=sys.stderr)
        return 2
    log_paths = resolve_log_paths(args.log or [])
    lines = []
    for path in log_paths:
        if not Path(path).is_file():
            print(f"log not found: {path}", file=sys.stderr)
            return 2
        try:
            lines.extend(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError as error:
            print(f"cannot read log {path}: {error}", file=sys.stderr)
            return 2
    parsed = parse_log_lines(lines)
    problem_paths = [Path(value) for value in args.problem_list or []]
    if args.workspace:
        candidate = Path(args.workspace) / "finish_problems" / "problem_list.json"
        if candidate.is_file():
            problem_paths.append(candidate)
    payloads = []
    for path in problem_paths:
        if not Path(path).is_file():
            print(f"problem list not found: {path}", file=sys.stderr)
            return 2
        try:
            payloads.append(_load_json_file(Path(path)))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"cannot read problem list {path}: {error}", file=sys.stderr)
            return 2
    for path in args.sampler or []:
        if not Path(path).is_file():
            print(f"sampler output not found: {path}", file=sys.stderr)
            return 2
    try:
        samples = load_samples(args.sampler or [])
    except OSError as error:
        print(f"cannot read sampler output: {error}", file=sys.stderr)
        return 2
    try:
        context = _load_json_file(args.context) if args.context else {}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"cannot read context {args.context}: {error}", file=sys.stderr)
        return 2
    report = build_report(
        parsed,
        problem_lists=payloads,
        samples=samples,
        context=context,
        sources={
            "logs": [str(path) for path in log_paths],
            "problem_lists": [str(path) for path in problem_paths],
            "sampler": [str(path) for path in args.sampler or []],
            "context": str(args.context) if args.context else None,
        },
        required_minutes=args.required_minutes,
        target_ratio=args.target_ratio,
    )
    _write_report(report, args.output)
    if args.strict and report["acceptance"]["targets_met"] is False:
        return 1
    return 0


def _sample_main(args) -> int:
    if args.pid <= 0:
        print("--pid must be positive", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("--interval must be positive", file=sys.stderr)
        return 2
    if args.duration < 0:
        print("--duration must be zero (until Ctrl+C) or positive", file=sys.stderr)
        return 2
    if args.retain <= 0:
        print("--retain must be positive", file=sys.stderr)
        return 2
    disk_path = Path(args.disk_path) if args.disk_path else Path.cwd()
    if not disk_path.exists():
        print(f"disk path does not exist: {disk_path}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stats = _LiveSampleStats(retain=args.retain)
    started = time.monotonic()
    deadline = started + args.duration if args.duration else None
    session_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    previous_cpu = None
    previous_wall = None
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "record_type": "session", "session_id": session_id, "pid": args.pid,
            "interval": args.interval, "disk_path": str(disk_path),
            "started_at": datetime.now().isoformat(timespec="milliseconds"),
        }, ensure_ascii=False) + "\n")
        stream.flush()
        try:
            while True:
                now = time.monotonic()
                try:
                    snapshot = process_snapshot(args.pid, disk_path=disk_path)
                except RuntimeError as error:
                    stream.write(json.dumps({
                        "record_type": "error", "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                        "elapsed_seconds": round(now - started, 3), "pid": args.pid, "error": str(error),
                    }, ensure_ascii=False) + "\n")
                    stream.flush()
                    print(str(error), file=sys.stderr)
                    return 2
                cpu_percent = None
                cpu_time = snapshot.get("cpu_time_seconds")
                if cpu_time is not None and previous_cpu is not None and previous_wall is not None:
                    wall_delta = now - previous_wall
                    if wall_delta > 0:
                        cpu_percent = round((cpu_time - previous_cpu) / wall_delta * 100.0, 3)
                if cpu_time is not None:
                    previous_cpu, previous_wall = cpu_time, now
                row = {
                    "record_type": "sample",
                    "session_id": session_id,
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    "elapsed_seconds": round(now - started, 3),
                    **snapshot,
                    "cpu_percent": cpu_percent,
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                stats.add(row)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                pause = args.interval
                if deadline is not None:
                    pause = min(pause, max(0.0, deadline - time.monotonic()))
                time.sleep(pause)
        except KeyboardInterrupt:
            pass
    print(json.dumps({"output": str(output), "summary": stats.summary()},
                     ensure_ascii=True, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    summarize = subparsers.add_parser("summarize", help="summarize an existing log/problem list/sample")
    summarize.add_argument("--log", type=Path, action="append", default=[],
                           help="finish_review.log or a directory containing rotated logs; repeatable")
    summarize.add_argument("--problem-list", type=Path, action="append", default=[],
                           help="problem_list.json snapshot; repeat for a backlog series")
    summarize.add_argument("--workspace", type=Path,
                           help="event workspace; reads finish_problems/problem_list.json when present")
    summarize.add_argument("--sampler", type=Path, action="append", default=[],
                           help="JSONL produced by the sample command; repeatable")
    summarize.add_argument("--context", type=Path, help="field context JSON (version/device/cameras/concurrency)")
    summarize.add_argument("--required-minutes", type=float, default=30.0,
                           help="target run length in minutes (default 30)")
    summarize.add_argument("--target-ratio", type=float, default=0.5,
                           help="preferred processing ratio ceiling (default 0.5; below 1 is the hard target)")
    summarize.add_argument("--output", type=Path, help="write the full JSON report here")
    summarize.add_argument("--strict", action="store_true",
                           help="exit 1 when a known target fails; unknown signals are not failures")

    sample = subparsers.add_parser("sample", help="sample a running process and free disk space")
    sample.add_argument("--pid", type=int, required=True)
    sample.add_argument("--interval", type=float, default=5.0, help="seconds between samples (default 5)")
    sample.add_argument("--duration", type=float, default=0.0,
                        help="seconds to sample; 0 runs until Ctrl+C")
    sample.add_argument("--retain", type=int, default=4096,
                        help="recent values kept in memory for median/p95; count/min/max are exact")
    sample.add_argument("--disk-path", type=Path, help="recording disk to report free space for")
    sample.add_argument("--output", type=Path, required=True,
                        help="JSONL output; appends so a restarted app can continue the same file")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sample":
        return _sample_main(args)
    return _summarize_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
