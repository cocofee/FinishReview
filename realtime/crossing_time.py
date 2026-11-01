"""Presentation times for video crossings; official chip records remain untouched."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

from .clock_sync import CLOCK_MODEL_FILENAME, ClockModelStore, ClockSyncError, MANUAL_ANCHOR_SOURCE
from .passage_evidence import PassageEvidenceError, VideoClockCalibrationStore


MANUAL_CALIBRATION_FILENAME = "video_clock_calibrations.jsonl"


@lru_cache(maxsize=4)
def _clock_snapshot(path, modified, size):
    return ClockModelStore(path)


@lru_cache(maxsize=4)
def _manual_offsets(path, modified, size):
    store = VideoClockCalibrationStore(path, recover_incomplete_tail=False)
    return {
        (int(calibration.camera_index), str(calibration.session_key).rsplit("|", 1)[-1]): int(calibration.offset_ms)
        for calibration in store.calibrations()
    }


def workspace_clocks(workspace):
    path = Path(workspace) / CLOCK_MODEL_FILENAME
    try:
        stamp = path.stat()
        return _clock_snapshot(path, stamp.st_mtime_ns, stamp.st_size)
    except (OSError, ValueError, ClockSyncError):
        return None


def manual_clock_offsets(workspace):
    """First-rider offsets published by the live calibration journal."""

    path = Path(workspace) / MANUAL_CALIBRATION_FILENAME
    try:
        stamp = path.stat()
        return _manual_offsets(str(path), stamp.st_mtime_ns, stamp.st_size)
    except (OSError, ValueError, PassageEvidenceError):
        return {}


@dataclass(frozen=True)
class CrossingTime:
    value_ms: int
    source: str


def recorder_crossing_time(recorder_ms, frame, workspace=None, *, context=None, clocks=None):
    """Put observed video time beside chip time using the evidence's own clock fit."""

    if recorder_ms is None:
        return None
    frame = frame or {}
    camera = int(frame.get("camera_index") or (context.camera if context else 0))
    version = int(frame.get("calibration_version") or 0)
    model = None
    if context and camera == context.camera and (not version or version == context.model.version):
        model = context.model
    elif camera:
        try:
            if clocks is None and workspace is not None:
                clocks = workspace_clocks(workspace)
            if clocks is not None:
                model = clocks.get(camera, version) if version else clocks.latest(camera)
        except (OSError, ValueError, ClockSyncError):
            pass
    session = frame.get("session_id", "")
    if not session and context:
        segment = next((piece for piece in context.segments
                        if piece.segment.segment_id == frame.get("segment_id")), None)
        if segment is not None:
            session = segment.session_id
    if not session and frame.get("video_path"):
        from .runtime_paths import recording_session_path
        session = re.sub(r"_review_\d+$", "", re.sub(
            r"_archive_\d+$", "", recording_session_path(Path(str(frame["video_path"]))).stem, flags=re.I))
    if model and not (model.source == MANUAL_ANCHOR_SOURCE and session not in model.session_ids):
        return CrossingTime(int(model.to_chip(int(recorder_ms), session)), "aligned")
    # The live first-rider calibration is written before offline analysis
    # publishes an automatic model; without this fallback the table shows raw
    # recorder time (about two seconds ahead of the camera OSD) until then.
    if workspace is not None and camera and session:
        offset = manual_clock_offsets(workspace).get((camera, session))
        if offset is not None:
            return CrossingTime(int(recorder_ms) - int(offset), "aligned")
    return CrossingTime(int(recorder_ms), "video")


def item_crossing_time(item, workspace=None, *, clocks=None):
    """Actual crossing first; an expected position never claims an observed rider."""

    evidence = item.evidence
    frame = dict(evidence.get("frame") or {})
    if item.calibration_version:
        frame["calibration_version"] = item.calibration_version
    if not frame.get("camera_index"):
        frame["camera_index"] = evidence.get("camera_index")
    observed = evidence.get("recorder_ms", frame.get("recorder_ms"))
    if observed is not None:
        return recorder_crossing_time(observed, frame, workspace, clocks=clocks)
    expected = evidence.get("recorder_start_ms", evidence.get("expected_recorder_ms"))
    if expected is not None:
        value = recorder_crossing_time(expected, frame, workspace, clocks=clocks)
        return CrossingTime(value.value_ms, "expected")
    if item.chip_time_ms is not None:
        return CrossingTime(int(item.chip_time_ms), "chip")
    return None
