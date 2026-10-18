"""Offline finish problem-list analysis over archived recordings.

Stages, each cached on disk so an interrupted run resumes:

1. line sampling per recording file (``strips/``);
2. crossing detection per camera session (``crossings_camera<N>.json``);
3. calibration, versioned in the workspace's clock-model journal: the
   automatic fit, or an operator-confirmed first rider of the analysed
   recording when the automatic fit cannot start yet;
4. order-preserving pairing of crossings with chip reads;
5. bicycle check of unpaired crossings (optional YOLOX model);
6. merge rules, problem items, crossing cards (``problem_list.json``).

Run it in a separate, low-priority process (``--analyze-problems``); it only
reads the event workspace and writes into its own output folder.
"""

from __future__ import annotations

import json
import hashlib
import logging
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np

from .analysis_resources import (
    MIN_AVAILABLE_MEMORY_MB,
    ResourceGovernor,
    available_memory_mb,
    limit_opencv_threads,
    lower_process_priority,
    peak_memory_mb,
)
from .analysis_workspace import AnalysisWorkspace, ArchiveSegment
from .analysis_settings import (read_settings, resolved_model_path, settings_token, model_fingerprint,
                                profile_for_segment, crosses_profile_boundary)
from .batch_review import (BATCH_IDENTITY_FILENAME, aggregate_batches, identity_chain, previous_items,
                           read_snapshots, save_identity, unambiguous_auto_passed)
from .bike_check import (
    DECISION_DISPUTED,
    DECISION_KEEP,
    LABEL_TEXT,
    LABEL_PEDESTRIAN,
    LABEL_UNCHECKED,
    LABEL_VEHICLE,
    BikeVerdict,
    CropPlan,
    FrameReader,
    YoloxDetector,
    classify_frames,
    decide_verdict,
)
from .chip_checks import ChipCheckContext, chip_check_items
from .chip_history import ChipRead, calibration_chip_times, format_chip_time
from .clock_sync import (
    CLOCK_MODEL_FILENAME,
    MANUAL_ANCHOR_TOLERANCE_MS,
    AnchorCheck,
    ClockAnchor,
    ClockCrossing,
    ClockModel,
    ClockModelStore,
    calibrate_camera,
    check_anchors,
    manual_anchor_model,
)
from .crossing_cards import render_cards
from .crossing_detection import DetectionParams, DetectionResult, LineCrossing, detect_crossings, frame_interval_ms
from .crossing_pairing import pair_crossings
from .evidence_clock import align_evidence_timestamp
from .line_sampling import (
    DEFAULT_DECODE_THREADS,
    DEFAULT_SAMPLES,
    LineGeometry,
    line_config_digest,
    load_strip,
    read_strip_meta,
    sample_video_file,
    save_strip,
)
from .problem_items import (
    KIND_NO_CHIP_CROSSING,
    KIND_LINE_ACTIVITY,
    KIND_NO_VIDEO,
    KIND_WAITING,
    STATUS_WAITING,
    CATEGORY_VIDEO,
    STATUS_OPEN,
    stable_item_id,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    ProblemItem,
    sort_items,
)
from .problem_rules import CrossingRecord, Obstruction, build_video_items, merge_obstructions
from .target_crossing import TargetCrossingCheck, VERIFICATION_OFFSETS_MS, VERIFICATION_VERSION, check_target_tracks


logger = logging.getLogger("FinishReview.ProblemAnalysis")

OUTPUT_DIRNAME = "finish_problems"
PROBLEM_LIST_FILENAME = "problem_list.json"


@dataclass
class AnalysisConfig:
    workspace: Path
    output_dir: Optional[Path] = None
    finish_lines: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    cameras: tuple[int, ...] = ()
    samples: int = DEFAULT_SAMPLES
    decode_threads: int = DEFAULT_DECODE_THREADS
    model_path: Optional[Path] = None
    model_input: int = 640
    crop_modes: Mapping[int, str] = field(default_factory=dict)
    race_id: str = ""
    stage_id: str = ""
    clock_model_path: Optional[Path] = None
    render_cards: bool = True
    check_paired_bikes: bool = False
    min_available_mb: int = MIN_AVAILABLE_MEMORY_MB
    live: bool = False
    evidence_delay_ms: int = 10_000
    sampling_profiles: tuple[dict, ...] = ()
    settings_token: str = ""
    fast_decode: bool = True

    def resolved_output(self) -> Path:
        return Path(self.output_dir) if self.output_dir else Path(self.workspace) / OUTPUT_DIRNAME


ProgressCallback = Callable[[str, int, int], None]


def _noop_progress(_stage: str, _done: int, _total: int) -> None:
    return None


def strip_path(output_dir: Path, segment: ArchiveSegment) -> Path:
    return output_dir / "strips" / f"camera_{segment.camera_index:02d}" / (segment.path.stem + ".npz")


def _strip_is_current(path: Path, segment: ArchiveSegment, digest: str, samples: int) -> bool:
    meta = read_strip_meta(path) if path.is_file() else None
    if not meta:
        return False
    try:
        stat = segment.path.stat()
    except OSError:
        return False
    return (
        meta.get("line_digest") == digest
        and int(meta.get("samples", 0)) == int(samples)
        and int(meta.get("video_size", -1)) == int(stat.st_size)
        and meta.get("segment_id") == segment.segment.segment_id
        and meta.get("excluded_ranges", []) == [list(value) for value in segment.excluded_ranges]
    )


def extract_strips(
    config: AnalysisConfig,
    segments: tuple[ArchiveSegment, ...],
    line_config: Mapping[str, Any],
    *,
    progress: ProgressCallback = _noop_progress,
    checkpoint: Optional[Callable[[], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[Path]:
    """Sample every recording file of one camera; reuse up-to-date strips."""

    output_dir = config.resolved_output()
    if config.sampling_profiles and not config.race_id:
        scope = AnalysisWorkspace(config.workspace, allow_empty_scope=True)
        config = replace(config, race_id=scope.race_id, stage_id=scope.stage_id)
    paths = []
    legacy = None
    for index, segment in enumerate(segments):
        if cancelled():
            break
        if crosses_profile_boundary(config.sampling_profiles, segment, config.race_id, config.stage_id):
            continue
        effective = profile_for_segment(config.sampling_profiles, segment, config.race_id, config.stage_id)
        if effective is None:
            effective = line_config
            if effective is not None and not line_covers_segment(effective, segment):
                # The drawn-once default begins at the save session; earlier
                # recordings keep using the workspace's legacy line.
                if legacy is None:
                    legacy = AnalysisWorkspace(config.workspace, race_id=config.race_id, stage_id=config.stage_id,
                                               allow_empty_scope=True)
                effective = legacy.finish_lines().get(segment.camera_index)
        if not effective or not line_covers_segment(effective, segment):
            continue
        digest = line_config_digest(effective)
        target = strip_path(output_dir, segment)
        progress("线取样", index, len(segments))
        if not _strip_is_current(target, segment, digest, config.samples):
            started = time.perf_counter()
            strip = sample_video_file(
                segment.path, effective,
                samples=config.samples, threads=config.decode_threads,
                snap=bool(effective.get("snap")), checkpoint=checkpoint, cancelled=cancelled,
                fast_decode=config.fast_decode,
            )
            if cancelled():
                break
            save_started = time.perf_counter()
            save_strip(target, strip, {
                "line_digest": digest,
                "line_config": dict(effective),
                "samples": config.samples,
                "segment_id": segment.segment.segment_id,
                "session_id": segment.session_id,
                "camera_index": segment.camera_index,
                "media_started_at_ms": segment.start_ms,
                "media_duration_ms": segment.end_ms - segment.start_ms,
                "video_size": segment.path.stat().st_size,
                "excluded_ranges": [list(value) for value in segment.excluded_ranges],
            })
            logger.info(
                "sampled %s: %d frames in %.3f s (%s line); stages=%s save=%.3fs size=%dx%d backend=%s",
                segment.path.name, strip.frame_count, time.perf_counter() - started, strip.geometry.source,
                strip.stage_seconds, time.perf_counter() - save_started, strip.geometry.width, strip.geometry.height,
                strip.decode_backend,
            )
        paths.append(target)
    progress("线取样", len(paths), len(segments))
    return paths


def default_line_for_camera(config: AnalysisConfig, workspace: AnalysisWorkspace, camera: int):
    line = config.finish_lines.get(camera)
    # The saved line is the camera default for later sessions and stages too:
    # an empty race (the pre-race draw) matches any scope, while a line from
    # another race in the same folder must not leak into this one.  A stage
    # that moves the camera gets a new save, which replaces this default.
    if line and (not line.get("race_id") or line.get("race_id") == workspace.race_id):
        return line
    return None


def line_for_camera(config: AnalysisConfig, workspace: AnalysisWorkspace, camera: int) -> Optional[Mapping[str, Any]]:
    return default_line_for_camera(config, workspace, camera) or workspace.finish_lines().get(camera)


def line_covers_segment(line, segment) -> bool:
    """A drawn-once default starts at its save session; older recordings keep theirs."""

    effective = line.get("effective_from_ms") if isinstance(line, Mapping) else None
    return effective is None or int(effective) <= segment.start_ms


def line_for_segment(config, workspace, segment):
    if crosses_profile_boundary(config.sampling_profiles, segment, workspace.race_id, workspace.stage_id):
        return None
    profile = profile_for_segment(config.sampling_profiles, segment, workspace.race_id, workspace.stage_id)
    if profile:
        return profile
    line = default_line_for_camera(config, workspace, segment.camera_index)
    if line is None or not line_covers_segment(line, segment):
        line = workspace.finish_lines().get(segment.camera_index)
    return line if line and line_covers_segment(line, segment) else None


def load_line_overrides(path: Optional[Path]) -> dict[int, dict]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("finish_lines", payload) if isinstance(payload, dict) else payload
    return {int(item["camera_index"]): dict(item) for item in records}


@dataclass
class _FileStrip:
    path: Path
    meta: dict
    start_ms: int
    pts: np.ndarray
    geometry: Any
    strip: Optional[np.ndarray] = None

    @property
    def end_ms(self) -> int:
        return self.start_ms + int(self.pts[-1]) if self.pts.size else self.start_ms


CONTEXT_MS = 5_000
CONTIGUOUS_MS = 1_500
COVERAGE_MARGIN_MS = 2_000
PRE_RACE_MARGIN_MS = 120_000
LIVE_BATCH_MS = 6_000


class _VerificationRuntime:
    """Bounded resources shared by incremental publications in one worker."""

    def __init__(self):
        self.detector = None
        self.model_key = None
        self.readers = OrderedDict()
        self.detections = OrderedDict()
        self.files = {}
        self.line_detections = {}

    def reader(self, path, threads):
        path = Path(path)
        stamp = path.stat()
        key = (str(path), stamp.st_size, stamp.st_mtime_ns, threads)
        if key not in self.readers:
            for previous in list(self.readers):
                if previous[0] == key[0]:
                    self.readers.pop(previous).close()
            self.readers[key] = FrameReader(key[0], threads)
        self.readers.move_to_end(key)
        while len(self.readers) > 2:
            self.readers.popitem(last=False)[1].close()
        return self.readers[key]

    def close(self):
        for reader in self.readers.values():
            reader.close()
        self.readers.clear()
        self.detections.clear()
        self.files.clear()
        self.line_detections.clear()
        self.detector = None


def _load_file_strips(paths: list[Path], runtime=None) -> list[_FileStrip]:
    files = []
    for path in paths:
        if not path.is_file():
            continue
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        saved = runtime.files.get(path) if runtime is not None else None
        if saved is not None and saved[0] == stamp:
            files.append(saved[1])
            continue
        # Do not decompress every historical image strip on each live tick.
        with np.load(path) as data:
            meta = json.loads(bytes(data["meta"]).decode("utf-8"))
            geometry = LineGeometry(int(meta["width"]), int(meta["height"]),
                                    data["xs"], data["ys"], data["nx"], data["ny"],
                                    meta.get("line_source", "configured"), meta.get("snap_offset_px", 0.0))
            files.append(_FileStrip(path, meta, int(meta["media_started_at_ms"]), data["pts_ms"], geometry))
        if runtime is not None:
            runtime.files[path] = (stamp, files[-1])
    files.sort(key=lambda item: item.start_ms)
    return files


def _segment_points(geometry, r0: int, r1: int) -> tuple[tuple[float, float], ...]:
    rows = np.linspace(r0, r1, 9).round().astype(np.int64)
    return tuple(geometry.point(int(row)) for row in rows)


def uncovered_ranges(start, end, excluded):
    cursor = start
    for left, right in sorted(excluded):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            yield cursor, min(left, end)
        cursor = max(cursor, right)
    if cursor < end:
        yield cursor, end


def detect_session_crossings(
    files: list[_FileStrip],
    camera_index: int,
    params: Optional[DetectionParams] = None,
    *,
    checkpoint: Optional[Callable[[], None]] = None,
    cache: bool = False,
    memory_cache: Optional[dict] = None,
) -> tuple[list[CrossingRecord], list[Obstruction], list[tuple[int, int, str]], list[dict]]:
    """Detect crossings file by file with a few seconds of neighbour context."""

    crossings: list[CrossingRecord] = []
    obstructions: list[Obstruction] = []
    coverage: list[tuple[int, int, str]] = []
    raw: list[dict] = []
    loaded: dict[int, np.ndarray] = {}

    def strip_of(index: int) -> np.ndarray:
        if index not in loaded:
            loaded[index] = load_strip(files[index].path).strip
            for key in [key for key in loaded if abs(key - index) > 1]:
                loaded.pop(key)
        return loaded[index]

    def contiguous(a: int, b: int) -> bool:
        return (
            0 <= a < len(files) and 0 <= b < len(files)
            and files[a].meta.get("session_id") == files[b].meta.get("session_id")
            and files[a].meta.get("line_digest") == files[b].meta.get("line_digest")
            and abs(files[b].start_ms + float(files[b].pts[0]) - files[a].end_ms) <= CONTIGUOUS_MS
        )

    for index, item in enumerate(files):
        session = str(item.meta.get("session_id", ""))
        frame_ms = frame_interval_ms(item.pts)
        context = max(1, int(round(CONTEXT_MS / frame_ms)))
        previous_index, next_index = index - 1, index + 1
        while previous_index >= 0 and files[previous_index].meta.get("excluded_ranges"):
            previous_index -= 1
        while next_index < len(files) and files[next_index].meta.get("excluded_ranges"):
            next_index += 1
        if item.meta.get("excluded_ranges"):
            previous_index, next_index = -1, len(files)
        neighbours = [index]
        if contiguous(previous_index, index):
            neighbours.insert(0, previous_index)
        if contiguous(index, next_index):
            neighbours.append(next_index)
        offsets = min(context, len(files[previous_index].pts)) if previous_index in neighbours else 0
        signature = {"version": 1, "params": asdict(params or DetectionParams()), "files": [
            [str(files[n].path), files[n].path.stat().st_mtime_ns, files[n].path.stat().st_size]
            for n in neighbours
        ]} if cache else None
        cache_path = item.path.with_suffix(".detections.json")
        result = None
        remembered = memory_cache.get(item.path) if memory_cache is not None else None
        if cache and remembered is not None and remembered[0] == signature:
            result = remembered[1]
        if cache and result is None:
            try:
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                if saved["signature"] == signature:
                    result = DetectionResult(tuple(LineCrossing(**value) for value in saved["crossings"]),
                                             tuple(tuple(value) for value in saved["obstructions"]), frame_ms)
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if result is None:
            own = strip_of(index)
            parts = []
            if offsets:
                parts.append(strip_of(previous_index)[:, -context:])
            parts.append(own)
            if next_index in neighbours:
                parts.append(strip_of(next_index)[:, :context])
            joined = np.concatenate(parts, axis=1) if len(parts) > 1 else own
            result = detect_crossings(joined, item.pts, params)
            if cache:
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(dict(signature=signature, crossings=[asdict(c) for c in result.crossings],
                                                      obstructions=result.obstructions)), encoding="utf-8")
                temporary.replace(cache_path)
        if cache and memory_cache is not None:
            memory_cache[item.path] = (signature, result)
            if len(memory_cache) > 512:
                memory_cache.pop(next(iter(memory_cache)))
        total = len(item.pts)
        for crossing in result.crossings:
            column = crossing.c0 - offsets
            if not 0 <= column < total or not crossing.rider_like:
                continue
            position = int(round(float(item.pts[column])))
            if any(left <= item.start_ms + position < right for left, right in item.meta.get("excluded_ranges", [])):
                continue
            record = CrossingRecord(
                camera_index=camera_index,
                session_id=session,
                segment_id=str(item.meta.get("segment_id", "")),
                video_path=str(item.meta.get("video_path", "")),
                recorder_ms=item.start_ms + position,
                frame_index=int(column),
                position_ms=position,
                row=int(crossing.row), r0=int(crossing.r0), r1=int(crossing.r1),
                in_obstruction=bool(crossing.in_obstruction),
                contact=item.geometry.point(crossing.row),
                segment_points=_segment_points(item.geometry, crossing.r0, crossing.r1),
            )
            crossings.append(record)
            raw.append({
                "recorder_ms": record.recorder_ms, "session_id": session, "frame_index": record.frame_index,
                "video_path": record.video_path, "row": record.row, "in_obstruction": record.in_obstruction,
            })
        for c0, c1, _r0, _r1 in result.obstructions:
            a, b = max(0, c0 - offsets), min(total - 1, c1 - offsets)
            if a <= b and c0 - offsets < total and c1 - offsets >= 0:
                obstructions.extend(Obstruction(camera_index, left, right) for left, right in uncovered_ranges(
                    item.start_ms + int(item.pts[a]), item.start_ms + int(item.pts[b]), item.meta.get("excluded_ranges", []),
                ))
        coverage.extend((left, right, session) for left, right in uncovered_ranges(
            item.start_ms + int(item.pts[0]), item.end_ms, item.meta.get("excluded_ranges", []),
        ))
        if checkpoint is not None:
            checkpoint()
    merged: list[list] = []
    for start, end, session in sorted(coverage):
        if merged and merged[-1][2] == session and start - merged[-1][1] <= 1_000:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, session])
    crossings.sort(key=lambda record: record.recorder_ms)
    return crossings, obstructions, [tuple(item) for item in merged], raw


def _chip_locator(files: list[_FileStrip], version: int):
    def locate(read: ChipRead, expected_ms: float) -> Optional[dict]:
        for item in files:
            if any(left <= expected_ms < right for left, right in item.meta.get("excluded_ranges", [])):
                continue
            if item.start_ms + item.pts[0] <= expected_ms <= item.end_ms:
                index = int(np.argmin(np.abs(item.start_ms + item.pts - expected_ms)))
                position = int(round(float(item.pts[index])))
                return {
                    "frame": {
                        "camera_index": int(item.meta.get("camera_index", 1)),
                        "segment_id": str(item.meta.get("segment_id", "")),
                        "video_path": str(item.meta.get("video_path", "")),
                        "frame_index": index,
                        "position_ms": position,
                        "session_id": str(item.meta.get("session_id", "")),
                        "calibration_version": version,
                    },
                    "segment_points": [list(point) for point in _segment_points(
                        item.geometry, 0, item.geometry.samples - 1)],
                }
        return None

    return locate


def bike_cache_key(model_key, plan, crossing):
    try:
        stat = Path(crossing.video_path).stat()
        video_stamp = [stat.st_size, stat.st_mtime_ns]
    except OSError:
        video_stamp = None
    geometry = hashlib.sha256(json.dumps([crossing.segment_points, video_stamp], sort_keys=True).encode()).hexdigest()
    return f"{model_key}|{plan.mode}|{plan.offsets_ms}|{crossing.key}|{geometry}"


def _bike_verdicts(
    config: AnalysisConfig,
    camera: int,
    crossings: list[CrossingRecord],
    crowded,
    *,
    progress: ProgressCallback,
    checkpoint: Optional[Callable[[], None]],
) -> dict[str, BikeVerdict]:
    if not config.model_path:
        return {}
    cache_path = config.resolved_output() / f"bike_camera{camera}.json"
    model_key = model_fingerprint(config.model_path, config.model_input)
    cache: dict = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    plan = CropPlan.for_mode(config.crop_modes.get(camera, "up"))
    detector = None
    reader: Optional[FrameReader] = None
    verdicts: dict[str, BikeVerdict] = {}
    # One decoder at a time, files in order: an open decoder per file would
    # keep every file's frame buffers in memory for the whole race.
    ordered = sorted(crossings, key=lambda item: (item.video_path, item.position_ms))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for index, crossing in enumerate(ordered):
            key = bike_cache_key(model_key, plan, crossing)
            # Legacy filename-only caches are intentionally not trusted.
            entry = cache.get(key)
            if entry is not None:
                verdict = BikeVerdict(**{
                    name: tuple(value) if name == "details" else value
                    for name, value in entry.items()
                })
                if verdict.label in (LABEL_PEDESTRIAN, LABEL_VEHICLE) and verdict.details:
                    # Re-decide from the cached per-frame scores (rules may change).
                    verdict = decide_verdict(
                        verdict.label, verdict.score, verdict.details,
                        edge_cut=bool(entry.get("edge_cut", "人被画面边缘切掉" in verdict.reason)),
                        crowded=bool(crowded(crossing.recorder_ms)), vehicle=verdict.vehicle,
                    )
                verdicts[crossing.key] = verdict
                continue
            if detector is None:
                detector = YoloxDetector(config.model_path, config.model_input)
            progress("认车", index, len(crossings))
            if reader is None or str(reader.path) != str(Path(crossing.video_path)):
                if reader is not None:
                    reader.close()
                reader = FrameReader(crossing.video_path, config.decode_threads)
            frames = [
                frame for frame, _pts in reader.frames_at(
                    [crossing.position_ms + offset for offset in plan.offsets_ms]
                )
            ]
            verdict = classify_frames(
                detector, frames, crossing.segment_points, plan,
                crowded=crowded(crossing.recorder_ms),
            )
            verdicts[crossing.key] = verdict
            cache[key] = {
                "label": verdict.label, "score": verdict.score, "decision": verdict.decision,
                "reason": verdict.reason, "frames": verdict.frames, "vehicle": verdict.vehicle,
                "details": [list(item) for item in verdict.details], "edge_cut": verdict.edge_cut,
            }
            if checkpoint is not None:
                checkpoint()
            if index % 20 == 0:
                _save_bike_cache(cache_path, cache)
    finally:
        if reader is not None:
            reader.close()
        _save_bike_cache(cache_path, cache)
    return verdicts


def _save_bike_cache(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _verification_sources(files, crossing):
    """Resolve real neighbouring frames, never borrowing another line/session."""
    own = next((item for item in files if str(item.meta.get("video_path")) == crossing.video_path), None)
    if own is None:
        return []
    sources = []
    for offset in VERIFICATION_OFFSETS_MS:
        target = crossing.recorder_ms + offset
        for item in files:
            if (item.meta.get("session_id") != crossing.session_id
                    or item.meta.get("line_digest") != own.meta.get("line_digest")
                    or (item.geometry.width, item.geometry.height) != (own.geometry.width, own.geometry.height)
                    or not len(item.pts)):
                continue
            if not item.start_ms + item.pts[0] - 25 <= target <= item.end_ms + 25:
                continue
            position = float(item.pts[int(np.argmin(np.abs(item.start_ms + item.pts - target)))])
            stamp = item.start_ms + position
            if any(left <= stamp <= right for left, right in item.meta.get("excluded_ranges", [])):
                continue
            if abs(stamp - target) <= 50:
                sources.append((item, position))
                break
    return sources


def _verify_crossing_candidates(config, camera, files, crossings, *, progress, checkpoint, runtime=None):
    """Run the target gate only in short windows triggered by line changes."""
    cache_path = config.resolved_output() / f"target_crossings_camera{camera}.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    checks, detector = {}, None
    try:
        model_key = model_fingerprint(config.model_path, config.model_input)
    except (OSError, ValueError) as error:
        logger.warning("crossing verification model unavailable: %s", error)
        return {c.key: TargetCrossingCheck("unverified", "model_failed") for c in crossings}
    if runtime is not None:
        if runtime.model_key != model_key:
            runtime.close()
            runtime.model_key = model_key
        detector = runtime.detector
    for index, crossing in enumerate(crossings):
        sources = _verification_sources(files, crossing)
        signature = []
        for item, position in sources:
            path = Path(item.meta["video_path"])
            try:
                stat = path.stat()
                stamp = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                stamp = None
            signature.append([str(path), position, stamp, item.meta.get("geometry_digest"),
                              item.meta.get("line_digest"), item.meta.get("excluded_ranges", [])])
        key = hashlib.sha256(json.dumps([VERIFICATION_VERSION, model_key, crossing.key, crossing.segment_points,
                                        signature], sort_keys=True).encode()).hexdigest()
        saved = cache.get(key)
        if saved is not None:
            checks[crossing.key] = TargetCrossingCheck(**dict(saved, observed_ms=tuple(saved["observed_ms"])))
            continue
        progress("跨线验证", index, len(crossings))
        if len(sources) < len(VERIFICATION_OFFSETS_MS):
            checks[crossing.key] = TargetCrossingCheck("unverified", "missing_context")
            continue  # A later sealed neighbour can complete this window.
        reader = None
        observations = []
        try:
            if detector is None:
                detector = YoloxDetector(config.model_path, config.model_input)
                if runtime is not None:
                    runtime.detector = detector
            width, height = sources[0][0].geometry.width, sources[0][0].geometry.height
            cx, cy = crossing.contact
            size = min(1200, width, height)
            x0 = int(np.clip(cx - size / 2, 0, width - size))
            y0 = int(np.clip(cy - size * .65, 0, height - size))
            for item, position in sources:
                if checkpoint is not None:
                    checkpoint()
                path = item.meta["video_path"]
                stamp = Path(path).stat()
                detection_key = (str(Path(path)), stamp.st_size, stamp.st_mtime_ns,
                                 item.start_ms, position, x0, y0, size, width, height)
                if runtime is not None and detection_key in runtime.detections:
                    runtime.detections.move_to_end(detection_key)
                    observations.append(runtime.detections[detection_key])
                    continue
                if runtime is not None:
                    reader = runtime.reader(path, config.decode_threads)
                elif reader is None or str(reader.path) != str(Path(path)):
                    if reader is not None:
                        reader.close()
                    reader = FrameReader(path, config.decode_threads)
                frames = reader.frames_at([position])
                if not frames or abs(frames[0][1] - position) > 50:
                    continue
                frame, pts = frames[0]
                if frame.shape[:2] != (height, width):
                    continue
                detections = detector.detect(frame[y0:y0 + size, x0:x0 + size])
                # Coordinates always stay in the original decoded frame.
                boxes = [replace(d, x=d.x + x0, y=d.y + y0) for d in detections
                         if d.x > 2 and d.y > 2 and d.x + d.w < size - 3 and d.y + d.h < size - 3]
                observations.append((item.start_ms + pts, boxes))
                if runtime is not None:
                    runtime.detections[detection_key] = observations[-1]
                    while len(runtime.detections) > 256:
                        runtime.detections.popitem(last=False)
            check = check_target_tracks(observations, crossing.segment_points, frame_size=(width, height))
        except Exception as error:
            logger.warning("crossing verification unavailable for %s: %s", crossing.key, error)
            check = TargetCrossingCheck("unverified", "model_failed")
            if detector is None:
                return {c.key: checks.get(c.key, check) for c in crossings}
        finally:
            if reader is not None and runtime is None:
                reader.close()
        checks[crossing.key] = check
        if check.reason != "model_failed":
            cache[key] = asdict(check)
        if checkpoint is not None:
            checkpoint()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _save_bike_cache(cache_path, cache)
    return checks


def _unchecked_line_items(crossings, checks, camera, model=None):
    if not checks:
        return []
    items = []
    reasons = {
        "missing_context": "缺少前后封存画面",
        "discontinuous_context": "前后画面不连续",
        "clipped_target": "目标被画面边缘截断",
        "insufficient_track": "同一目标的连续轨迹不足",
        "ambiguous_tracks": "多个目标的轨迹关联不清",
        "model_failed": "模型加载或推理失败",
    }
    for crossing in crossings:
        check = checks[crossing.key]
        if check.state != "unverified":
            continue
        version = model.version if model else 0
        items.append(ProblemItem(
            stable_item_id(KIND_LINE_ACTIVITY, camera, crossing.key), KIND_LINE_ACTIVITY, CATEGORY_VIDEO,
            "终点线发生变化，但尚未确认目标穿越；不计过线、不自动配对芯片；"
            + reasons.get(check.reason, "轨迹证据不足"),
            chip_time_ms=model.to_chip(crossing.recorder_ms, crossing.session_id) if model else None,
            status=STATUS_DISPUTED, status_note="遮挡或轨迹证据不足，待核实", priority=35,
            evidence={"frame": crossing.frame_payload(version), "contact": list(crossing.contact),
                      "segment_points": [list(p) for p in crossing.segment_points],
                      "recorder_ms": crossing.recorder_ms, "crossing_check": asdict(check),
                      "calibration_pending": model is None}, calibration_version=version,
        ))
    return items


def _uncalibrated_video_items(
    config: AnalysisConfig,
    camera: int,
    crossings: list[CrossingRecord],
    *,
    periods: list[tuple[int, int]],
    progress: ProgressCallback,
    checkpoint: Optional[Callable[[], None]],
    verified_verdicts: Optional[dict[str, BikeVerdict]] = None,
) -> tuple[list[ProblemItem], dict[str, BikeVerdict]]:
    """Classify finish-line candidates before chip/video clock calibration.

    Calibration is required for chip pairing, but it is not required to answer
    the independent camera-test question (person, bicycle, or vehicle).  Keep
    these candidates in recorder time and mark pairing as pending.
    """

    candidates = list(crossings)

    def crowded(recorder_ms: int) -> bool:
        return any(start - 5_000 <= recorder_ms <= end + 5_000 for start, end in periods)

    verdicts = verified_verdicts if verified_verdicts is not None else _bike_verdicts(
        config, camera, candidates, crowded,
        progress=progress, checkpoint=checkpoint,
    )
    items: list[ProblemItem] = []
    for crossing in candidates:
        verdict = verdicts.get(crossing.key)
        status = STATUS_OPEN
        detail = "视频候选待首人校时"
        evidence = {
            "frame": crossing.frame_payload(0),
            "contact": list(crossing.contact),
            "segment_points": [list(point) for point in crossing.segment_points],
            "recorder_ms": int(crossing.recorder_ms),
            "row": int(crossing.row),
            "calibration_pending": True,
        }
        if verdict is not None:
            detail += f"；模型判断 {LABEL_TEXT.get(verdict.label, verdict.label)} ({float(verdict.score):.2f})"
            if verdict.vehicle:
                detail += f"（{verdict.vehicle}）"
            if verdict.decision == DECISION_DISPUTED:
                status = STATUS_DISPUTED
            evidence["bike"] = {
                "label": verdict.label,
                "score": round(float(verdict.score), 3),
                "decision": verdict.decision,
                "reason": verdict.reason,
                "vehicle": verdict.vehicle,
            }
        items.append(ProblemItem(
            item_id=stable_item_id(KIND_NO_CHIP_CROSSING, camera, crossing.key),
            kind=KIND_NO_CHIP_CROSSING,
            category=CATEGORY_VIDEO,
            detail=detail,
            status=status,
            status_note="确认第一名原始帧后开始芯片配对",
            priority=10,
            evidence=evidence,
            calibration_version=0,
        ))
    return items, verdicts


def calibrate_evidence_clock(crossings, reads, *, camera_index):
    """Try receipt-supported calendar offsets, still requiring visual calibration.

    Dates alone never reject a read. A receipt can suggest an integer-day
    offset, but cannot by itself establish a chip/video match.
    """
    offsets = {0}
    for read in reads:
        if not read.excluded and not read.manual_entry:
            offsets.add(align_evidence_timestamp(read.chip_ms, read.received_at_ms) - read.chip_ms)
    results = [calibrate_camera(
        crossings, calibration_chip_times(reads), camera_index=camera_index,
        date_offset_ms=offset,
    ) for offset in sorted(offsets)]
    valid = [result for result in results if result.model is not None]
    if not valid:
        return results[0]
    valid.sort(key=lambda result: result.model.support, reverse=True)
    if len(valid) > 1 and valid[0].model.support == valid[1].model.support:
        return replace(valid[0], model=None, warnings=("多段录像的日期映射存在歧义，请核对录像会话",))
    return valid[0]


def anchor_chip_record_stands(anchor: ClockAnchor, reads: Iterable[ChipRead]) -> bool:
    """Whether the chip record behind one manual anchor still stands.

    The calibration journal stores only the passage id, bib and offset: no
    revision and no original chip time, so a later revision of the time cannot
    be proven here. What the pipeline itself can check is that a read for this
    bib and chip time is still live (not voided, deleted or replaced) and is a
    real chip read, not a manual entry. A read keeps the first active
    revision's timestamp, so a time revision shows up as a mismatch.
    """

    return any(
        read.active_now and not read.excluded and not read.manual_entry
        and str(read.bib) == str(anchor.label) and int(read.chip_ms) == int(anchor.chip_ms)
        for read in reads
    )


def manual_anchors_for_run(
    workspace: AnalysisWorkspace,
    camera: int,
    reads: Iterable[ChipRead],
    sessions: set[str],
) -> tuple[tuple[ClockAnchor, ...], tuple[ClockAnchor, ...], tuple[ClockAnchor, ...]]:
    """Split this camera's manual calibrations for one analysis run.

    Returns ``(usable, other_sessions, changed)``: only anchors confirmed on
    one of this run's own recording sessions, with a chip record that still
    stands, may start pairing. Anchors of other recordings or races and stale
    ones are returned so the run can say why they were not used.
    """

    usable, other_sessions, changed = [], [], []
    for anchor in workspace.manual_anchors(camera):
        if str(anchor.reference) not in sessions:
            other_sessions.append(anchor)
        elif not anchor_chip_record_stands(anchor, reads):
            changed.append(anchor)
        else:
            usable.append(anchor)
    return tuple(usable), tuple(other_sessions), tuple(changed)


@dataclass(frozen=True, slots=True)
class _CalibrationChoice:
    """Which model starts pairing, and what the operator must see about it."""

    model: Optional[ClockModel]
    checks: tuple[AnchorCheck, ...] = ()
    warnings: tuple[str, ...] = ()
    manual: bool = False
    paired_sessions: frozenset[str] = frozenset()


def choose_calibration_model(
    auto_model: Optional[ClockModel],
    anchors: tuple[ClockAnchor, ...],
    *,
    camera_index: int,
    sessions: set[str],
) -> _CalibrationChoice:
    """Pick the model that starts pairing and expose manual-anchor conflicts.

    The automatic model is used while it agrees with the operator's manual
    first-rider anchors (within the conventional tolerance). When it disagrees,
    the confirmed fixed offset stays in force for its own recording sessions
    and the conflict is reported instead of silently replacing the operator's
    calibration; without any automatic model, a manual anchor starts pairing on
    its own. A manual start serves only the sessions an anchor names.
    """

    if auto_model is not None:
        checks = check_anchors(auto_model, anchors)
        conflicts = [check for check in checks if not check.within_tolerance]
        if conflicts:
            worst = max(conflicts, key=lambda check: abs(int(check.difference_ms)))
            manual = manual_anchor_model(anchors, camera_index=camera_index, sessions=sessions)
            if manual is None:
                warning = (
                    f"自动校时与人工首人锚点 {worst.anchor.label or '-'} 相差 {worst.difference_ms:+d} ms，"
                    f"超过 {MANUAL_ANCHOR_TOLERANCE_MS} ms，且没有可用的人工固定偏移，请核对"
                )
                return _CalibrationChoice(auto_model, checks, (warning,))
            warning = (
                f"自动校时与人工首人锚点 {worst.anchor.label or '-'} 相差 {worst.difference_ms:+d} ms，"
                f"超过 {MANUAL_ANCHOR_TOLERANCE_MS} ms；本次仍按人工锚点固定偏移配对，未采用自动模型，请核对"
            )
            return _CalibrationChoice(manual, checks, (warning,), True, frozenset(manual.session_ids))
        return _CalibrationChoice(auto_model, checks)
    manual = manual_anchor_model(anchors, camera_index=camera_index, sessions=sessions)
    if manual is None:
        return _CalibrationChoice(None)
    warning = f"自动校时未就绪：{manual.notes[0]}"
    return _CalibrationChoice(manual, (), (warning,), True, frozenset(manual.session_ids))


WAITING_TAIL_REASON = "等待相邻短段和芯片数据到齐，暂不判定漏读。"
WAITING_UNCALIBRATED_REASON = "没有自动校时或人工首人锚点，该录像会话的芯片暂不配对，等待核对"


def analyse_camera(
    config: AnalysisConfig,
    workspace: AnalysisWorkspace,
    camera: int,
    strip_paths: list[Path],
    *,
    progress: ProgressCallback = _noop_progress,
    checkpoint: Optional[Callable[[], None]] = None,
    reads: Optional[tuple[ChipRead, ...]] = None,
    params: Optional[DetectionParams] = None,
    runtime: Optional[_VerificationRuntime] = None,
) -> dict:
    """Crossings, calibration, pairing, bicycle check and rules for one camera.

    ``reads`` overrides the workspace chip reads (used by injection tests).
    """

    output_dir = config.resolved_output()
    reads = workspace.reads if reads is None else reads
    files = _load_file_strips(strip_paths, runtime)
    detection_started = time.perf_counter()
    progress("过线检测", 0, len(files))
    crossings, obstructions, coverage, raw = detect_session_crossings(
        files, camera, params, checkpoint=checkpoint, cache=config.live,
        memory_cache=runtime.line_detections if runtime is not None else None,
    )
    line_candidates = list(crossings)
    detection_seconds = time.perf_counter() - detection_started
    verification_started = time.perf_counter()
    crossing_checks = {}
    verified_verdicts = None
    if config.model_path:
        crossing_checks = _verify_crossing_candidates(
            config, camera, files, line_candidates, progress=progress, checkpoint=checkpoint, runtime=runtime,
        )
        # A line change the tracker cannot follow (bunch, shadow, occlusion) is
        # still evidence that somebody crossed.  Dropping those candidates made
        # the whole main bunch unpaired and left hundreds of "line activity"
        # rows with no answer; only a clearly absent target is discarded.
        crossings = [c for c in line_candidates
                     if crossing_checks[c.key].state != "not_crossed"
                     and crossing_checks[c.key].reason != "model_failed"]
        verified_verdicts = {}
        periods = merge_obstructions(obstructions)
        for crossing in crossings:
            check = crossing_checks[crossing.key]
            crowded = crossing.in_obstruction or any(a - 5_000 <= crossing.recorder_ms <= b + 5_000 for a, b in periods)
            if check.state == "confirmed":
                reason = ("遮挡时段，目标跨线已验证，仍需人工确认" if crowded
                          else "目标轨迹跨线已验证；不自动判定选手身份")
                decision = DECISION_DISPUTED if crowded or check.label != "bicycle" else DECISION_KEEP
            else:
                reason = "目标轨迹不足，按芯片时间参与配对，仍需人工确认"
                decision = DECISION_DISPUTED
            verified_verdicts[crossing.key] = BikeVerdict(
                check.label if check.state == "confirmed" else LABEL_UNCHECKED,
                check.score, decision, reason,
                len(check.observed_ms), vehicle=check.vehicle,
            )
        for entry in raw:
            key = f"{camera}|{entry['video_path']}|{entry['frame_index']}|{entry['row']}"
            entry["crossing_check"] = asdict(crossing_checks[key])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"crossings_camera{camera}.json").write_text(json.dumps(
        {"schema_version": 1, "camera_index": camera,
         # Group counting needs every line change the camera saw; an
         # "unverified" track is still a person-sized crossing.  Only a target
         # the model positively ruled out is dropped from this list.
         "crossings": [entry for entry in raw if not crossing_checks
                       or (entry["crossing_check"]["state"] != "not_crossed"
                           and entry["crossing_check"].get("reason") != "model_failed")],
         "line_events": raw,
         "coverage": [list(item) for item in coverage],
         "obstructions": [[item.start_ms, item.end_ms] for item in obstructions]},
        ensure_ascii=False,
    ), encoding="utf-8")

    # A confirmed non-bicycle target (pedestrian, vehicle) must never absorb a
    # chip.  An unverified candidate, on the other hand, is exactly the rider
    # the model lost track of, so it stays eligible for time pairing.
    pairing_crossings = [c for c in crossings if verified_verdicts is None
                         or crossing_checks[c.key].state != "confirmed"
                         or verified_verdicts[c.key].label == "bicycle"]
    calibration = calibrate_evidence_clock(
        [ClockCrossing(item.recorder_ms, item.session_id) for item in pairing_crossings],
        reads,
        camera_index=camera,
    )
    sessions = {session for _start, _end, session in coverage if session}
    anchors, other_recording_anchors, changed_anchors = manual_anchors_for_run(
        workspace, camera, reads, sessions,
    )
    choice = choose_calibration_model(
        calibration.model, anchors, camera_index=camera, sessions=sessions,
    )
    warnings = [*calibration.warnings, *choice.warnings]
    result: dict = {
        "camera_index": camera,
        "stage_seconds": {"line_detection": round(detection_seconds, 3),
                          "crossing_verification": round(time.perf_counter() - verification_started, 3)},
        "files": len(files),
        "crossings": len(crossings),
        "line_candidates": len(line_candidates),
        "crossing_verification": {
            "state": ("not_configured" if not config.model_path else
                      "enabled" if line_candidates else "no_candidates"),
            "confirmed": sum(c.state == "confirmed" for c in crossing_checks.values()),
            "not_crossed": sum(c.state == "not_crossed" for c in crossing_checks.values()),
            "unverified": sum(c.state == "unverified" for c in crossing_checks.values()),
        },
        "coverage": [list(item) for item in coverage],
        "calibration_warnings": warnings,
        "items": [],
        "auto_passed": [],
        "bike_status": {"state": "not_configured" if not config.model_path else "waiting_calibration",
                        "message": "认车未配置，降噪未启用" if not config.model_path else "认车等待校时及候选",
                        "crop_mode": config.crop_modes.get(camera, "up")},
    }
    if any(check.reason == "model_failed" for check in crossing_checks.values()):
        result["crossing_verification"]["state"] = "failed"
        result["bike_status"].update(state="failed", message="跨线模型运行失败；线事件保留待核，不自动配对")
    if choice.model is None:
        # A clock is required for chip pairing, but not for the independent
        # finish-line object check.  Keep that check running so a camera can be
        # tested by walking through its configured line before race chips exist.
        if other_recording_anchors:
            warnings.append(
                f"另有 {len(other_recording_anchors)} 个人工校时锚点属于其他录像会话，不能用于本次配对"
            )
        if changed_anchors:
            warnings.append(
                f"{len(changed_anchors)} 个人工校时锚点对应的芯片记录已作废、替换或已变更，不能用于本次配对"
            )
        result["calibration_warnings"] = warnings

        # A chip can be opened before video clock calibration.  The receipt
        # timestamp is only a calendar hint, so the resulting frame is marked
        # as reference-only and is never used for pairing or auto-pass.
        reference_locator = None
        if coverage:
            locate_reference = _chip_locator(files, 0)

            def reference_locator(read: ChipRead):
                evidence_ms = align_evidence_timestamp(
                    int(read.chip_ms), int(getattr(read, "received_at_ms", 0) or 0),
                )
                located = locate_reference(read, evidence_ms)
                if not located:
                    return None
                frame = dict(located.get("frame", {}))
                frame["reference_only"] = True
                frame["date_aligned"] = int(evidence_ms) != int(read.chip_ms)
                return {
                    "reference_frame": frame,
                    "reference_reason": "按芯片接收日期定位；尚未完成视频校时，不代表已确认过线",
                    "evidence_timestamp_ms": int(evidence_ms),
                }
        if config.model_path and coverage:
            try:
                video_items, verdicts = _uncalibrated_video_items(
                    config, camera, crossings,
                    periods=merge_obstructions(obstructions),
                    progress=progress, checkpoint=checkpoint,
                    verified_verdicts=verified_verdicts,
                )
            except Exception as error:  # A model failure must not hide crossings.
                logger.warning("uncalibrated bike check unavailable: %s", error)
                video_items, verdicts = _uncalibrated_video_items(
                    config, camera, crossings, periods=[], progress=progress,
                    checkpoint=checkpoint, verified_verdicts={},
                )
                result["bike_status"].update(
                    state="failed",
                    message=f"认车加载/推理失败，候选保留：{error}",
                )
            # Candidates already rendered from ``crossings`` must not appear
            # again as a second "line activity" row; only a candidate excluded
            # from pairing (e.g. the model failed on it) remains here.
            paired_keys = {item.key for item in crossings}
            result["items"] = video_items + _unchecked_line_items(
                [c for c in line_candidates if c.key not in paired_keys], crossing_checks, camera,
            )
            for item in video_items:
                frame = item.evidence.get("frame", {})
                key = f"{camera}|{frame.get('video_path')}|{frame.get('frame_index')}|{item.evidence.get('row')}"
                if key in crossing_checks:
                    item.evidence["crossing_check"] = asdict(crossing_checks[key])
            if result["bike_status"].get("state") != "failed":
                result["bike_status"].update(
                    state="enabled" if verdicts else "waiting_crossing" if line_candidates else "no_candidates",
                    message=(
                        "认车已运行；等待第一名校时后进行芯片配对"
                        if verdicts else "尚无确认穿越的目标；线变化与遮挡不计过线"
                    ),
                    model_digest=model_fingerprint(config.model_path, config.model_input),
                )
            result["calibration_pending"] = True
            first = next(iter(pairing_crossings), None)
            if first is not None:
                result["first_rider_candidate"] = {
                    "recorder_ms": int(first.recorder_ms),
                    "frame": first.frame_payload(0),
                    "session_id": first.session_id,
                }
            if config.live and coverage:
                result["analyzed_through_ms"] = max(
                    end for _start, end, _session in coverage
                ) - config.evidence_delay_ms
            # A hotel test may have no chip reads; a live race can have reads
            # waiting for the first anchor. Both remain visible in the queue.
            if reads:
                result["items"] += waiting_items(
                    reads, camera, "等待第一名原始帧校时后进行芯片配对",
                    evidence_factory=reference_locator,
                )
            by_segment = {f.meta.get("segment_id"): f for f in files}
            for item in result["items"]:
                frame = item.evidence.get("frame")
                source = by_segment.get(frame.get("segment_id")) if frame else None
                if source is not None:
                    meta = source.meta
                    frame.update(line_config=meta.get("line_config", {}),
                                 line_digest=meta.get("line_digest", ""),
                                 geometry_digest=meta.get("geometry_digest", ""),
                                 geometry_path=str(source.path.parent / meta["geometry_file"])
                                 if meta.get("geometry_file") else "",
                                 line_source=meta.get("line_source", "configured"),
                                 snap_offset_px=meta.get("snap_offset_px", 0))
            return result
        # Without a configured detector there is no independent video result.
        result["error"] = "自动校时失败：" + "；".join(warnings)
        if config.live:
            result["items"] = waiting_items(
                reads, camera, result["error"], evidence_factory=reference_locator,
            )
        return result
    store = ClockModelStore(config.clock_model_path or output_dir / CLOCK_MODEL_FILENAME)
    line_signature = hashlib.sha256(json.dumps(sorted({(item.meta.get("line_digest", ""), item.meta.get("geometry_digest", ""))
                                                       for item in files})).encode()).hexdigest()
    model = store.publish(replace(choice.model, evidence_signature=line_signature))
    if coverage:
        result["analyzed_through_ms"] = max(end for _start, end, _session in coverage)
    # A manual start serves only the sessions an operator anchor names; every
    # other recording session waits for its own anchor or the automatic fit.
    served_sessions = choice.paired_sessions if choice.manual else None
    pairing_reads = [read for read in reads if not read.excluded and not read.manual_entry]
    manual_entries = [read for read in reads if read.manual_entry and not read.excluded]
    by_session: dict[str, list[ChipRead]] = {}
    no_video: list[ChipRead] = []
    uncalibrated: list[ChipRead] = []
    expected_ms: dict[str, int] = {}
    for read in pairing_reads:
        expected = model.to_recorder(read.chip_ms)
        session = next(
            (name for start, end, name in coverage
             if start + COVERAGE_MARGIN_MS <= expected <= end - COVERAGE_MARGIN_MS),
            None,
        )
        if session is None:
            no_video.append(read)
        elif served_sessions is not None and session not in served_sessions:
            uncalibrated.append(read)
        else:
            by_session.setdefault(session, []).append(read)
            expected_ms[read.read_id] = int(model.to_recorder(read.chip_ms, session))
    matched, video_only, chip_only = [], [], []
    for session in {item[2] for item in coverage}:
        if served_sessions is not None and session not in served_sessions:
            continue
        session_crossings = [item for item in pairing_crossings if item.session_id == session]
        session_reads = by_session.get(session, [])
        pairing = pair_crossings(
            session_crossings, [item.recorder_ms for item in session_crossings],
            session_reads, [expected_ms[read.read_id] for read in session_reads],
        )
        matched += pairing.matched
        video_only += pairing.video_only
        if verified_verdicts is not None:
            # Confirmed pedestrians/vehicles stay listed even if a chip time is
            # nearby.  Unverified crossings already arrive through
            # ``pairing.video_only``; adding them again would double-count.
            video_only += [c for c in crossings if c.session_id == session
                           and crossing_checks[c.key].state == "confirmed"
                           and verified_verdicts[c.key].label != "bicycle"]
        chip_only += pairing.chip_only
    uncalibrated_sessions = sorted({
        name for _start, _end, name in coverage
        if name and served_sessions is not None and name not in served_sessions
    })
    if uncalibrated:
        warnings.append(
            f"录像会话 {'、'.join(uncalibrated_sessions)} 没有自动校时或人工首人锚点，"
            f"其中的 {len(uncalibrated)} 条芯片暂不配对，请先确认该会话的首位选手"
        )
    residuals = np.asarray([item[2] for item in matched], dtype=np.float64)
    result["pairing"] = {
        "chips_with_video": len(matched) + len(chip_only),
        "auto_paired": len(matched),
        "chip_only": len(chip_only),
        "video_only": len(video_only),
        "video_only_in_obstruction": sum(item.in_obstruction for item in video_only),
        "no_video": len(no_video),
        "uncalibrated": len(uncalibrated),
        "residual_median_ms": round(float(np.median(residuals)), 1) if residuals.size else None,
        "residual_p90_abs_ms": round(float(np.percentile(np.abs(residuals), 90)), 1) if residuals.size else None,
    }

    def anchor_entries() -> list[dict]:
        if choice.manual and not choice.checks:
            return [
                {"bib": anchor.label, "difference_ms": None, "within_tolerance": True,
                 "message": f"人工首人锚点固定偏移 {int(anchor.offset_ms):+d} ms（尚无自动校时可比）"}
                for anchor in anchors
            ]
        entries = []
        for check in choice.checks:
            if choice.manual and not check.within_tolerance:
                # The operator's offset is the one in force here; check_anchors'
                # own wording is for runs where the automatic model wins.
                message = (
                    f"人工首人锚点 {check.anchor.label or '-'} 与自动校时相差 {check.difference_ms:+d} ms，"
                    f"超过 {MANUAL_ANCHOR_TOLERANCE_MS} ms；本次仍按人工偏移配对，请核对"
                )
            else:
                message = check.message
            entries.append({"bib": check.anchor.label, "difference_ms": check.difference_ms,
                            "within_tolerance": check.within_tolerance, "message": message})
        return entries

    conflicts = [check for check in choice.checks if not check.within_tolerance]
    result["calibration"] = {
        "version": model.version,
        "source": str(model.source),
        "source_text": model.source_text,
        "slope_ms_per_hour": round(model.slope_ms_per_hour, 1),
        "offset_start_ms": model.offset_at(model.valid_from_ms),
        "offset_end_ms": model.offset_at(model.valid_to_ms),
        "isolated": model.candidates,
        "kept": model.support,
        "residual_mad_ms": round(model.residual_mad_ms, 1),
        "coarse_offset_ms": model.coarse_offset_ms,
        "coarse_distinct": bool(calibration.coarse and calibration.coarse.distinct),
        "session_offsets_ms": {key: value for key, value in model.session_offsets},
        "manual_conflict": bool(conflicts),
        "manual_conflict_ms": max((abs(int(check.difference_ms)) for check in conflicts), default=None),
        "anchors": anchor_entries(),
    }
    if choice.manual:
        result["calibration"]["manual_anchor_ms"] = int(round(model.intercept_ms))
        result["calibration"]["paired_sessions"] = sorted(choice.paired_sessions)
    if config.live and coverage:
        # The latest seconds still need neighbouring video and delayed chip
        # delivery. They are not evidence of a missing rider or a missing read.
        watermark = max(end for _start, end, _session in coverage) - config.evidence_delay_ms
        result["analyzed_through_ms"] = watermark
        uncalibrated_ids = {read.read_id for read in uncalibrated}
        waiting = tuple(
            read for read in reads
            if read.read_id not in uncalibrated_ids and model.to_recorder(read.chip_ms) > watermark
        )
        result["waiting_reads"] = len(waiting)

    # The same crowded periods the list reports, so a disputed crossing and
    # its period item agree.
    periods = merge_obstructions(obstructions)

    def crowded(recorder_ms: int) -> bool:
        return any(start - 5_000 <= recorder_ms <= end + 5_000 for start, end in periods)

    to_check = list(video_only)
    if config.check_paired_bikes:
        to_check += [item[0] for item in matched]
    try:
        verdicts = verified_verdicts if verified_verdicts is not None else _bike_verdicts(
            config, camera, to_check, crowded, progress=progress, checkpoint=checkpoint,
        )
        if config.model_path and result["bike_status"]["state"] != "failed":
            result["bike_status"].update(state="enabled" if to_check else "no_candidates",
                                         message="认车已启用（含有效缓存）" if to_check else "已配置；本轮无待认车候选",
                                         model_digest=model_fingerprint(config.model_path, config.model_input))
    except Exception as error:  # A model/candidate failure must never erase the unclassified crossings.
        logger.warning("bike check unavailable: %s", error)
        verdicts = {}
        result["bike_status"].update(state="failed", message=f"认车加载/推理失败，候选保留：{error}")
    if config.check_paired_bikes and verdicts:
        labels = [verdicts[item[0].key].label for item in matched if item[0].key in verdicts]
        result["paired_bicycle_share"] = round(sum(label == "bicycle" for label in labels) / max(1, len(labels)), 4)
        result["paired_not_bicycle"] = [
            (item[1].bib, format_chip_time(item[1].chip_ms), verdicts[item[0].key].label)
            for item in matched if item[0].key in verdicts and verdicts[item[0].key].label != "bicycle"
        ]
    outcome = build_video_items(
        camera_index=camera, model=model, matched=matched, video_only=video_only,
        chip_only=chip_only, no_video=no_video, obstructions=obstructions, verdicts=verdicts,
        manual_entries=manual_entries, chip_locator=_chip_locator(files, model.version),
        expected_ms=expected_ms,
        race_start_ms=(
            model.to_recorder(min(read.chip_ms for read in pairing_reads)) - PRE_RACE_MARGIN_MS
            if pairing_reads else None
        ),
    )
    result["rules"] = dict(outcome.stats, late_pairs=outcome.late_pairs)
    result["late_pair_frames"] = outcome.late_pair_frames
    result["fragments"] = outcome.fragments
    result["near_crossings"] = outcome.near_crossings
    result["matched"] = [[read.bib, read.chip_ms, round(residual, 1)] for _crossing, read, residual in matched]
    # Once calibrated, an unverified crossing is paired or listed as a
    # video-only rider; only a candidate excluded from pairing (model failure)
    # still needs its own "line changed, not verified" row.
    paired_keys = {item.key for item in crossings}
    result["items"] = list(outcome.items) + _unchecked_line_items(
        [c for c in line_candidates if c.key not in paired_keys], crossing_checks, camera, model,
    )
    result["auto_passed"] = outcome.auto_passed
    recognition = result["bike_status"]
    evidence_signature = hashlib.sha256(json.dumps([line_signature, recognition], sort_keys=True).encode()).hexdigest()
    by_segment = {item.meta.get("segment_id"): dict(item.meta, geometry_path=str(item.path.parent / item.meta["geometry_file"])
                                                  if item.meta.get("geometry_file") else "") for item in files}

    def evidence_frame(frame):
        meta = by_segment.get(frame.get("segment_id"), {})
        return dict(frame, line_digest=meta.get("line_digest", ""), line_config=meta.get("line_config", {}),
                    geometry_digest=meta.get("geometry_digest", ""), line_source=meta.get("line_source", "configured"),
                    geometry_path=meta.get("geometry_path", ""),
                    snap_offset_px=meta.get("snap_offset_px", 0))

    result["items"] = [replace(item, evidence=dict(item.evidence, analysis_signature=evidence_signature,
                        **({"frame": evidence_frame(item.evidence["frame"])} if item.evidence.get("frame") else {})))
                       for item in result["items"]]
    result["auto_passed"] = [dict(item, frame=evidence_frame(item["frame"])) for item in result["auto_passed"]]
    if config.live and coverage:
        # Pair with the full neighbour context, then publish only settled
        # evidence. Cutting pairing inputs at the watermark creates false
        # missing-chip problems for riders straddling that boundary.
        settled = [item for item in result["items"]
                   if item.chip_time_ms is not None and model.to_recorder(item.chip_time_ms) <= watermark
                   and item.evidence.get("recorder_end_ms", watermark) <= watermark]
        visible_ids = {item.item_id for item in settled}
        settled = [item for item in settled if not item.grouped or item.grouped in visible_ids]
        if config.evidence_delay_ms > 0:
            settled += waiting_items(waiting, camera, WAITING_TAIL_REASON)
        else:
            # The settle run after recording stopped (``--finish-live-problems``):
            # no more video will be written, so a chip the recording never
            # covered is a coverage gap to report as such, not a wait that the
            # operator could sit out.  Everything inside the sealed range is
            # already settled, because the evidence delay is over.
            shown = {item.item_id for item in settled}
            settled += [item for item in result["items"] if item.kind == KIND_NO_VIDEO
                        and item.item_id not in shown]
            result["waiting_reads"] = 0
        result["items"] = settled
        result["auto_passed"] = [item for item in result["auto_passed"] if model.to_recorder(item["chip_time_ms"]) <= watermark]
    if uncalibrated:
        # Chips of a recording session without any calibration are never paired
        # with a borrowed offset; they stay visible as waiting, also in a
        # settle run, until that session gets its own anchor or automatic fit.
        reason = WAITING_UNCALIBRATED_REASON
        if uncalibrated_sessions:
            reason += f"（录像会话 {'、'.join(uncalibrated_sessions)}）"
        result["items"] = [*result["items"], *waiting_items(uncalibrated, camera, reason)]
        if config.live and coverage:
            result["waiting_reads"] = int(result.get("waiting_reads") or 0) + len(uncalibrated)
    result["model"] = model
    return result


def waiting_items(reads, camera, reason, *, evidence_factory=None):
    """Build pending-chip rows, optionally retaining a safe reference frame."""
    items = []
    for read in reads:
        if read.excluded or read.manual_entry:
            continue
        evidence = {}
        if evidence_factory is not None:
            try:
                located = evidence_factory(read)
            except (OSError, RuntimeError, ValueError, TypeError):
                located = None
            if located:
                evidence.update(located)
        items.append(ProblemItem(
            stable_item_id(KIND_WAITING, camera, read.read_id), KIND_WAITING,
            CATEGORY_VIDEO, reason, chip_time_ms=read.chip_ms,
            bibs=(read.bib,), event_ids=(read.event_id,), status=STATUS_WAITING,
            evidence=evidence,
        ))
    return items


ANALYSIS_CONFIG_FILENAME = "analysis_config.json"


def config_from_workspace(workspace_dir: str | Path) -> AnalysisConfig:
    """Settings for a GUI-started run: ``finish_problems/analysis_config.json``.

    Keys (all optional): ``model_path``, ``model_input``, ``crop_modes``
    (camera -> mode), ``finish_lines`` (list like ``finish_lines.json``),
    ``cameras``, ``min_available_mb`` (default 1536 MB). Results go to
    ``finish_problems/``; calibration versions to
    the workspace's ``video_clock_models.jsonl`` so the main window sees them.
    """

    root = Path(workspace_dir)
    output = root / OUTPUT_DIRNAME
    settings = read_settings(root)
    lines = settings.get("finish_lines") or []
    return AnalysisConfig(
        workspace=root,
        output_dir=output,
        finish_lines={int(item["camera_index"]): dict(item) for item in lines if "camera_index" in item},
        cameras=tuple(int(value) for value in settings.get("cameras", ())),
        model_path=resolved_model_path(root, settings),
        model_input=int(settings.get("model_input", 640)),
        crop_modes={int(key): str(value) for key, value in (settings.get("crop_modes") or {}).items()},
        clock_model_path=root / CLOCK_MODEL_FILENAME,
        min_available_mb=int(settings.get("min_available_mb", MIN_AVAILABLE_MEMORY_MB)),
        sampling_profiles=tuple(settings.get("sampling_profiles", ())),
        settings_token=settings_token(root),
    )


def _live_progress(result, segments, pending, missing, config, reads):
    """Do not turn an unfinished batch (or a cache hole) into missing evidence."""
    pending_ranges = [list(span) for segment in pending for span in uncovered_ranges(
        segment.start_ms, segment.end_ms, segment.excluded_ranges)]
    result["sealed_through_ms"] = max((s.end_ms for s in segments), default=None)
    result["pending_video_ranges"] = pending_ranges
    result["pending_segments"] = len(pending)
    result["missing_line_segments"] = [s.segment.segment_id for s in missing]
    coverage = result.get("coverage", [])
    scanned = max((end for _start, end, _session in coverage), default=None)
    # Coverage remains explicit; the headline never jumps over an unscanned hole.
    blocked_ranges = list(pending_ranges)
    blocked_ranges.extend([s.start_ms, s.end_ms] for s in missing)
    if scanned is not None:
        frontier = min([scanned, *(left for left, _right in blocked_ranges)])
        result["scanned_through_ms"] = frontier
        previous = result.get("analyzed_through_ms")
        if previous is not None:
            result["analyzed_through_ms"] = min(previous, frontier - config.evidence_delay_ms)
    model = result.get("model")
    if model is not None and blocked_ranges:
        deferred = []
        for item in result["items"]:
            expected = model.to_recorder(item.chip_time_ms) if item.chip_time_ms is not None else None
            if item.kind == KIND_NO_VIDEO and expected is not None and any(
                left - COVERAGE_MARGIN_MS <= expected <= right + COVERAGE_MARGIN_MS
                for left, right in blocked_ranges
            ):
                deferred.extend(waiting_items(
                    [read for read in reads if read.event_id in item.event_ids],
                    result["camera_index"], "录像已封存，等待分析或终点线配置，暂不判定缺少录像",
                ))
            else:
                deferred.append(item)
        result["items"] = deferred
    if missing:
        result["items"].append(ProblemItem(
            stable_item_id(KIND_WAITING, result["camera_index"], "missing_line"), KIND_WAITING, CATEGORY_VIDEO,
            f"{len(missing)} 个封存片段缺线或跨改线边界，等待分段验证，不能视为已核清", status=STATUS_WAITING,
            evidence={"segments": result["missing_line_segments"]}))


def _run_live_batches(config, workspace, cameras, governor, summary, progress, cancelled):
    """Publish cumulative evidence every few seconds of new media, round-robin."""
    jobs, results = [], {}
    for camera in cameras:
        segments = workspace.archive_segments(camera)
        ready, pending, missing = [], deque(), []
        for segment in segments:
            effective = line_for_segment(config, workspace, segment)
            if effective is None:
                missing.append(segment)
                continue
            path = strip_path(config.resolved_output(), segment)
            if _strip_is_current(path, segment, line_config_digest(effective), config.samples):
                ready.append(path)
            else:
                pending.append(segment)
        jobs.append(dict(camera=camera, segments=segments, ready=ready, pending=pending, missing=missing))
    runtime = _VerificationRuntime()

    def input_stamp():
        values = []
        for name in ("cyclerace_passage_events.jsonl", "cyclerace_passage_events_deleted.json",
                     "video_clock_calibrations.jsonl", "cyclerace_race_metadata.json"):
            path = workspace.root / name
            try:
                stat = path.stat()
                values.append((name, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                pass
        return values

    inputs = input_stamp()
    started = time.perf_counter()
    batches, new_video_seconds = 0, 0.0
    try:
        first_round = True
        while first_round or any(job["pending"] for job in jobs):
            for job in jobs:
                if cancelled():
                    return
                latest_inputs = input_stamp()
                if latest_inputs != inputs:
                    # First-rider anchors and late chips must take effect at the
                    # next publication, not after an entire historical backlog.
                    workspace = AnalysisWorkspace(config.workspace, race_id=config.race_id, stage_id=config.stage_id,
                                                  include_review_segments=True, allow_empty_scope=True)
                    inputs = latest_inputs
                camera, pending = job["camera"], job["pending"]
                if not first_round and not pending:
                    continue
                batch_started = time.perf_counter()
                batch, duration_ms = [], 0
                while pending and duration_ms < LIVE_BATCH_MS - 100:
                    segment = pending.popleft()
                    batch.append(segment)
                    duration_ms += sum(end - start for start, end in uncovered_ranges(
                        segment.start_ms, segment.end_ms, segment.excluded_ranges))
                sample_started = time.perf_counter()
                job["ready"].extend(extract_strips(
                    config, tuple(batch), line_for_camera(config, workspace, camera) or {}, progress=progress,
                    checkpoint=lambda: governor.checkpoint(cancelled), cancelled=cancelled,
                ))
                sampling_seconds = time.perf_counter() - sample_started
                if cancelled():
                    return
                # A stop/settle run may remove the chip delay only once its entire
                # snapshot is sampled. Intermediate batches are not final gaps.
                current_config = replace(config, evidence_delay_ms=max(10_000, config.evidence_delay_ms)) if pending else config
                analysis_started = time.perf_counter()
                if job["ready"]:
                    result = analyse_camera(
                        current_config, workspace, camera, job["ready"], progress=progress,
                        checkpoint=lambda: governor.checkpoint(cancelled), runtime=runtime,
                    )
                else:
                    reason = "等待已封存录像" if not job["segments"] else "等待实际终点线或不跨改线边界的封存片段"
                    result = dict(camera_index=camera, error=reason, items=waiting_items(workspace.reads, camera, reason),
                                  auto_passed=[], waiting_reason="recording" if not job["segments"] else "finish_line")
                _live_progress(result, job["segments"], pending, job["missing"], current_config, workspace.reads)
                result["performance"] = dict(
                    new_video_seconds=round(duration_ms / 1000, 3),
                    sampling_seconds=round(sampling_seconds, 3),
                    analysis_seconds=round(time.perf_counter() - analysis_started, 3),
                )
                results[camera] = result
                new_video_seconds += duration_ms / 1000
                # Do not temporarily erase another camera from a combined list.
                if len(results) == len(jobs) and not cancelled():
                    publication_started = time.perf_counter()
                    path = write_problem_list(config, workspace, list(results.values()), progress=progress)
                    publication_seconds = time.perf_counter() - publication_started
                    batches += 1
                    summary["problem_list"] = str(path)
                    summary["summary"] = json.loads(path.read_text(encoding="utf-8"))["summary"]
                    progress("清单已发布", batches, len(jobs))
                    logger.info("live batch camera=%s video=%.2fs sampling=%.3fs analysis=%.3fs publish=%.3fs total=%.3fs pending=%d",
                                camera, duration_ms / 1000, sampling_seconds, result["performance"]["analysis_seconds"],
                                publication_seconds, time.perf_counter() - batch_started, len(pending))
                summary["cameras"][camera] = {
                    key: value for key, value in result.items() if key not in ("items", "auto_passed", "model")
                }
            first_round = False
        elapsed = time.perf_counter() - started
        summary["performance"] = dict(new_video_seconds=round(new_video_seconds, 3), elapsed_seconds=round(elapsed, 3),
                                      processing_ratio=round(elapsed / new_video_seconds, 3) if new_video_seconds else None,
                                      publications=batches)
    finally:
        runtime.close()


def run_offline_analysis(
    config: AnalysisConfig,
    *,
    stage: str = "all",
    progress: ProgressCallback = _noop_progress,
    cancelled: Callable[[], bool] = lambda: False,
    lower_priority: bool = True,
) -> dict:
    """Whole pipeline at low priority; resumes from cached strips.

    It does not start while free memory is below ``config.min_available_mb``
    (the recorder and CycleRace come first) and records the peak working set
    per stage in ``summary["memory"]``.
    """

    workspace = AnalysisWorkspace(config.workspace, race_id=config.race_id, stage_id=config.stage_id,
                                  include_review_segments=True, allow_empty_scope=True)
    config = replace(config, race_id=workspace.race_id, stage_id=workspace.stage_id)
    summary: dict = {"race_id": workspace.race_id, "cameras": {}}
    free = available_memory_mb()
    memory: dict = {"available_at_start_mb": free, "stage_peaks_mb": {}}
    summary["memory"] = memory
    if free is not None and free < int(config.min_available_mb):
        summary["skipped"] = (
            f"可用内存只有 {free} MB，少于 {int(config.min_available_mb)} MB；"
            "为保护 CycleRace 和录像，暂不分析，请关闭其他程序后再试。"
        )
        return summary
    if lower_priority:
        lower_process_priority()
    limit_opencv_threads(1)
    governor = ResourceGovernor()
    cameras = config.cameras or tuple(sorted(set(workspace.camera_indexes()) | set(config.finish_lines)
                                             | {int(p["camera_index"]) for p in config.sampling_profiles}))
    outer_progress = progress

    def progress(stage_name: str, done: int, total: int) -> None:
        peak = peak_memory_mb()
        if peak is not None:
            memory["stage_peaks_mb"][stage_name] = peak
        outer_progress(stage_name, done, total)
    if config.live and stage != "strips":
        _run_live_batches(config, workspace, cameras, governor, summary, progress, cancelled)
        summary["paused_seconds"] = round(governor.paused_seconds, 1)
        memory["peak_mb"] = peak_memory_mb()
        return summary
    results = []
    for camera in cameras:
        line = line_for_camera(config, workspace, camera)
        segments = workspace.archive_segments(camera)
        missing = [segment for segment in segments if line_for_segment(config, workspace, segment) is None]
        if not segments or len(missing) == len(segments):
            reason = "等待已封存录像" if not segments else "等待实际终点线或不跨改线边界的封存片段"
            result = {"camera_index": camera, "error": reason, "items": waiting_items(workspace.reads, camera, reason),
                      "auto_passed": [], "waiting_reason": "recording" if not segments else "finish_line",
                      "sealed_through_ms": max((segment.end_ms for segment in segments), default=None)}
            results.append(result)
            summary["cameras"][camera] = {"skipped": reason}
            continue
        paths = extract_strips(
            config, segments, line or {}, progress=progress,
            checkpoint=lambda: governor.checkpoint(cancelled), cancelled=cancelled,
        )
        summary["cameras"][camera] = {"strips": len(paths)}
        if stage == "strips" or cancelled():
            continue
        result = analyse_camera(
            config, workspace, camera, paths, progress=progress,
            checkpoint=lambda: governor.checkpoint(cancelled),
        )
        result["missing_line_segments"] = [segment.segment.segment_id for segment in missing]
        result["sealed_through_ms"] = max(segment.end_ms for segment in segments)
        if missing:
            result["items"].append(ProblemItem(
                stable_item_id(KIND_WAITING, camera, "missing_line"), KIND_WAITING, CATEGORY_VIDEO,
                f"{len(missing)} 个封存片段缺线或跨改线边界，等待分段验证，不能视为已核清", status=STATUS_WAITING,
                evidence={"segments": result["missing_line_segments"]}))
        results.append(result)
        summary["cameras"][camera] = {
            key: value for key, value in result.items() if key not in ("items", "auto_passed", "model")
        }
    if stage != "strips" and not cancelled():
        path = write_problem_list(config, workspace, results, progress=progress)
        summary["problem_list"] = str(path)
        summary["summary"] = json.loads(path.read_text(encoding="utf-8"))["summary"]
    summary["paused_seconds"] = round(governor.paused_seconds, 1)
    memory["peak_mb"] = peak_memory_mb()
    return summary


def summarize_items(items: list[ProblemItem], auto_passed: list[dict], unverifiable: int) -> dict:
    return {
        "auto_passed": sum(1 for item in auto_passed if item.get("active_now", True)),
        # Matched automatically, but inside a period whose group is still being
        # checked: counted as no operator needed, and still marked.
        "auto_passed_in_pending_period": sum(1 for item in auto_passed if item.get("pending_period")),
        "pending": sum(1 for item in items if item.pending),
        "disputed": sum(1 for item in items if item.status == STATUS_DISPUTED and item.pending),
        "grouped": sum(1 for item in items if item.grouped and item.status in ("open", "disputed")),
        "closed_upstream": sum(1 for item in items if item.status == STATUS_CLOSED_UPSTREAM),
        "excluded": sum(1 for item in items if item.status == STATUS_EXCLUDED),
        "unverifiable": unverifiable,
    }


def write_problem_list(
    config: AnalysisConfig,
    workspace: AnalysisWorkspace,
    camera_results: list[dict],
    *,
    progress: ProgressCallback = _noop_progress,
) -> Path:
    """Combine chip self checks with the video items and write the list."""

    output_dir = config.resolved_output()
    output_dir.mkdir(parents=True, exist_ok=True)
    roster = {}
    metadata = workspace.metadata()
    if metadata is not None and metadata.race_id == workspace.race_id:
        roster = {athlete.bib: metadata.group_label(athlete.group_id) for athlete in metadata.athletes if athlete.bib}
    from .chip_history import group_exclusion
    current_reads = []
    changed = set()
    for read in workspace.reads:
        event = workspace.latest_event(read.event_id)
        if read.active_now and event is not None:
            if (read.bib, read.chip_ms, read.lap) != (event.bib, int(event.timeline_timestamp_ms), event.lap):
                changed.add(read.event_id)
            read = replace(read, chip_ms=int(event.timeline_timestamp_ms), lap=event.lap,
                           group_id=event.group_id, group_name=event.group_name, excluded=group_exclusion(event.group_name))
        current_reads.append(read)
    items: list[ProblemItem] = list(chip_check_items(current_reads, ChipCheckContext(roster=roster)))
    if config.live:
        from .problem_items import KIND_NOT_ARRIVED, KIND_LAP_MISSING
        items = [item for item in items if item.kind not in (KIND_NOT_ARRIVED, KIND_LAP_MISSING)]
    auto_passed: list[dict] = []
    unverifiable = 0
    cameras = {}
    for result in camera_results:
        items += result.get("items", [])
        auto_passed += result.get("auto_passed", [])
        unverifiable += sum(len(item.bibs) for item in result.get("items", []) if item.kind == KIND_NO_VIDEO)
        cameras[str(result["camera_index"])] = {
            key: value for key, value in result.items()
            if key not in ("items", "auto_passed", "model", "late_pair_frames", "fragments", "near_crossings")
        }
    members = read_snapshots(workspace.reads, metadata, workspace.latest_event)
    items = [replace(item, evidence=dict(item.evidence, member_evidence_stale=True))
             if item.category == CATEGORY_VIDEO and set(item.event_ids) & changed else item for item in items]
    scope = (workspace.race_id, workspace.stage_id)
    # Identity comes from the ledger the workbench also writes: a batch the
    # operator is already reading keeps that id, a new list does not rename it.
    ledger = previous_items(output_dir / BATCH_IDENTITY_FILENAME, scope)
    listed = previous_items(output_dir / PROBLEM_LIST_FILENAME, scope)
    chain = identity_chain(ledger, listed)
    items = aggregate_batches(items, members, scope, chain)
    save_identity(output_dir / BATCH_IDENTITY_FILENAME, scope,
                  [item for item in items if item.evidence.get("batch_members")], chain)
    auto_passed = unambiguous_auto_passed(items, auto_passed)
    if config.render_cards:
        render_cards(items, output_dir, workspace.root, progress=progress, decode_threads=config.decode_threads)
    # A candidate that a late chip has now claimed must not vanish without a
    # trace: the run that closes it reports the discovery history once.
    from .candidate_history import HISTORY_FILENAME, update_history

    closed_candidates = update_history(
        output_dir / HISTORY_FILENAME, items,
        (entry for result in camera_results for entry in result.get("late_pair_frames", ())),
        now_ms=int(time.time() * 1000), race_id=workspace.race_id, stage_id=workspace.stage_id,
        analysed_cameras={int(result["camera_index"]) for result in camera_results if not result.get("error")},
    )
    items = [*items, *closed_candidates]
    payload = {
        "schema_version": 1,
        "generated_at_ms": int(time.time() * 1000),
        "race_id": workspace.race_id,
        "stage_id": workspace.stage_id,
        "race_date": workspace.race_date,
        "cameras": cameras,
        "summary": dict(summarize_items(items, auto_passed, unverifiable),
                        candidates_closed=len(closed_candidates)),
        "items": [item.to_payload() for item in items],
        "auto_passed": auto_passed,
        "live": config.live,
        "settings_token": config.settings_token,
        "passage_members": members,
    }
    target = output_dir / PROBLEM_LIST_FILENAME
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    temporary.replace(target)
    return target


__all__ = [
    "AnalysisConfig",
    "OUTPUT_DIRNAME",
    "PROBLEM_LIST_FILENAME",
    "analyse_camera",
    "detect_session_crossings",
    "extract_strips",
    "line_for_camera",
    "load_line_overrides",
    "strip_path",
    "summarize_items",
    "write_problem_list",
]
