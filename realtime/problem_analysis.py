"""Offline finish problem-list analysis over archived recordings.

Stages, each cached on disk so an interrupted run resumes:

1. line sampling per recording file (``strips/``);
2. crossing detection per camera session (``crossings_camera<N>.json``);
3. automatic calibration, versioned in the workspace's clock-model journal;
4. order-preserving pairing of crossings with chip reads;
5. bicycle check of unpaired crossings (optional YOLOX model);
6. merge rules, problem items, crossing cards (``problem_list.json``).

Run it in a separate, low-priority process (``--analyze-problems``); it only
reads the event workspace and writes into its own output folder.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

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
from .bike_check import (
    LABEL_PEDESTRIAN,
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
from .clock_sync import CLOCK_MODEL_FILENAME, ClockCrossing, ClockModelStore, calibrate_camera, check_anchors
from .crossing_cards import render_cards
from .crossing_detection import DetectionParams, DetectionResult, LineCrossing, detect_crossings, frame_interval_ms
from .crossing_pairing import pair_crossings
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
    KIND_NO_VIDEO,
    KIND_WAITING,
    STATUS_WAITING,
    CATEGORY_VIDEO,
    stable_item_id,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    ProblemItem,
    sort_items,
)
from .problem_rules import CrossingRecord, Obstruction, build_video_items, merge_obstructions


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
    digest = line_config_digest(line_config)
    paths = []
    for index, segment in enumerate(segments):
        if cancelled():
            break
        target = strip_path(output_dir, segment)
        progress("线取样", index, len(segments))
        if not _strip_is_current(target, segment, digest, config.samples):
            started = time.perf_counter()
            strip = sample_video_file(
                segment.path, line_config,
                samples=config.samples, threads=config.decode_threads,
                snap=bool(line_config.get("snap")), checkpoint=checkpoint, cancelled=cancelled,
            )
            if cancelled():
                break
            save_strip(target, strip, {
                "line_digest": digest,
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
                "sampled %s: %d frames in %.0f s (%s line)",
                segment.path.name, strip.frame_count, time.perf_counter() - started, strip.geometry.source,
            )
        paths.append(target)
    progress("线取样", len(paths), len(segments))
    return paths


def line_for_camera(config: AnalysisConfig, workspace: AnalysisWorkspace, camera: int) -> Optional[Mapping[str, Any]]:
    if camera in config.finish_lines:
        return config.finish_lines[camera]
    return workspace.finish_lines().get(camera)


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


def _load_file_strips(paths: list[Path]) -> list[_FileStrip]:
    files = []
    for path in paths:
        if not path.is_file():
            continue
        # Do not decompress every historical image strip on each live tick.
        with np.load(path) as data:
            meta = json.loads(bytes(data["meta"]).decode("utf-8"))
            geometry = LineGeometry(int(meta["width"]), int(meta["height"]),
                                    data["xs"], data["ys"], data["nx"], data["ny"],
                                    meta.get("line_source", "configured"), meta.get("snap_offset_px", 0.0))
            files.append(_FileStrip(path, meta, int(meta["media_started_at_ms"]), data["pts_ms"], geometry))
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
        if cache:
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
    model_key = f"{Path(config.model_path).name}@{config.model_input}"
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
            key = f"{model_key}|{plan.mode}|{crossing.key}"
            # Older caches added the crowded flag to the key; detections do not
            # depend on it, the decision is re-made below.
            entry = cache.get(key) or cache.get(key + "|0") or cache.get(key + "|1")
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
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    finally:
        if reader is not None:
            reader.close()
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return verdicts


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
) -> dict:
    """Crossings, calibration, pairing, bicycle check and rules for one camera.

    ``reads`` overrides the workspace chip reads (used by injection tests).
    """

    output_dir = config.resolved_output()
    reads = workspace.reads if reads is None else reads
    files = _load_file_strips(strip_paths)
    progress("过线检测", 0, len(files))
    crossings, obstructions, coverage, raw = detect_session_crossings(
        files, camera, params, checkpoint=checkpoint, cache=config.live,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"crossings_camera{camera}.json").write_text(json.dumps(
        {"schema_version": 1, "camera_index": camera, "crossings": raw,
         "coverage": [list(item) for item in coverage],
         "obstructions": [[item.start_ms, item.end_ms] for item in obstructions]},
        ensure_ascii=False,
    ), encoding="utf-8")

    calibration = calibrate_camera(
        [ClockCrossing(item.recorder_ms, item.session_id) for item in crossings],
        calibration_chip_times(reads),
        camera_index=camera,
    )
    result: dict = {
        "camera_index": camera,
        "files": len(files),
        "crossings": len(crossings),
        "coverage": [list(item) for item in coverage],
        "calibration_warnings": list(calibration.warnings),
        "items": [],
        "auto_passed": [],
    }
    if calibration.model is None:
        result["error"] = "自动校时失败：" + "；".join(calibration.warnings)
        if config.live:
            result["items"] = waiting_items(reads, camera, result["error"])
        return result
    store = ClockModelStore(config.clock_model_path or output_dir / CLOCK_MODEL_FILENAME)
    model = store.publish(calibration.model)
    if config.live and coverage:
        # The latest seconds still need neighbouring video and delayed chip
        # delivery. They are not evidence of a missing rider or a missing read.
        watermark = max(end for _start, end, _session in coverage) - config.evidence_delay_ms
        result["analyzed_through_ms"] = watermark
        result["waiting_reads"] = sum(model.to_recorder(read.chip_ms) > watermark for read in reads)
        waiting = tuple(read for read in reads if model.to_recorder(read.chip_ms) > watermark)
    sessions = set(model.session_ids)
    anchors = [anchor for anchor in workspace.manual_anchors(camera) if anchor.reference in sessions]
    result["calibration"] = {
        "version": model.version,
        "slope_ms_per_hour": round(model.slope_ms_per_hour, 1),
        "offset_start_ms": model.offset_at(model.valid_from_ms),
        "offset_end_ms": model.offset_at(model.valid_to_ms),
        "isolated": model.candidates,
        "kept": model.support,
        "residual_mad_ms": round(model.residual_mad_ms, 1),
        "coarse_offset_ms": model.coarse_offset_ms,
        "coarse_distinct": bool(calibration.coarse and calibration.coarse.distinct),
        "session_offsets_ms": {key: value for key, value in model.session_offsets},
        "anchors": [
            {"bib": check.anchor.label, "difference_ms": check.difference_ms,
             "within_tolerance": check.within_tolerance, "message": check.message}
            for check in check_anchors(model, anchors)
        ],
    }

    pairing_reads = [read for read in reads if not read.excluded and not read.manual_entry]
    manual_entries = [read for read in reads if read.manual_entry and not read.excluded]
    by_session: dict[str, list[ChipRead]] = {}
    no_video: list[ChipRead] = []
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
        else:
            by_session.setdefault(session, []).append(read)
            expected_ms[read.read_id] = int(model.to_recorder(read.chip_ms, session))
    matched, video_only, chip_only = [], [], []
    for session in {item[2] for item in coverage}:
        session_crossings = [item for item in crossings if item.session_id == session]
        session_reads = by_session.get(session, [])
        pairing = pair_crossings(
            session_crossings, [item.recorder_ms for item in session_crossings],
            session_reads, [expected_ms[read.read_id] for read in session_reads],
        )
        matched += pairing.matched
        video_only += pairing.video_only
        chip_only += pairing.chip_only
    residuals = np.asarray([item[2] for item in matched], dtype=np.float64)
    result["pairing"] = {
        "chips_with_video": len(matched) + len(chip_only),
        "auto_paired": len(matched),
        "chip_only": len(chip_only),
        "video_only": len(video_only),
        "video_only_in_obstruction": sum(item.in_obstruction for item in video_only),
        "no_video": len(no_video),
        "residual_median_ms": round(float(np.median(residuals)), 1) if residuals.size else None,
        "residual_p90_abs_ms": round(float(np.percentile(np.abs(residuals), 90)), 1) if residuals.size else None,
    }

    # The same crowded periods the list reports, so a disputed crossing and
    # its period item agree.
    periods = merge_obstructions(obstructions)

    def crowded(recorder_ms: int) -> bool:
        return any(start - 5_000 <= recorder_ms <= end + 5_000 for start, end in periods)

    to_check = [item for item in video_only if not item.in_obstruction]
    if config.check_paired_bikes:
        to_check += [item[0] for item in matched]
    verdicts = _bike_verdicts(config, camera, to_check, crowded, progress=progress, checkpoint=checkpoint)
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
    result["matched"] = [[read.bib, read.chip_ms, round(residual, 1)] for _crossing, read, residual in matched]
    result["items"] = list(outcome.items)
    result["auto_passed"] = outcome.auto_passed
    if config.live and coverage:
        # Pair with the full neighbour context, then publish only settled
        # evidence. Cutting pairing inputs at the watermark creates false
        # missing-chip problems for riders straddling that boundary.
        result["items"] = [item for item in result["items"]
                           if item.chip_time_ms is not None and model.to_recorder(item.chip_time_ms) <= watermark
                           and item.evidence.get("recorder_end_ms", watermark) <= watermark]
        visible_ids = {item.item_id for item in result["items"]}
        result["items"] = [item for item in result["items"] if not item.grouped or item.grouped in visible_ids]
        result["items"] += waiting_items(waiting, camera, "等待相邻短段和芯片数据到齐，暂不判定漏读。")
        result["auto_passed"] = [item for item in outcome.auto_passed if model.to_recorder(item["chip_time_ms"]) <= watermark]
    result["model"] = model
    return result


def waiting_items(reads, camera, reason):
    return [ProblemItem(stable_item_id(KIND_WAITING, camera, read.read_id), KIND_WAITING,
                        CATEGORY_VIDEO, reason, chip_time_ms=read.chip_ms,
                        bibs=(read.bib,), event_ids=(read.event_id,), status=STATUS_WAITING)
            for read in reads if not read.excluded and not read.manual_entry]


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
    settings: dict = {}
    try:
        settings = json.loads((output / ANALYSIS_CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    lines = settings.get("finish_lines") or []
    return AnalysisConfig(
        workspace=root,
        output_dir=output,
        finish_lines={int(item["camera_index"]): dict(item) for item in lines if "camera_index" in item},
        cameras=tuple(int(value) for value in settings.get("cameras", ())),
        model_path=Path(settings["model_path"]) if settings.get("model_path") else None,
        model_input=int(settings.get("model_input", 640)),
        crop_modes={int(key): str(value) for key, value in (settings.get("crop_modes") or {}).items()},
        clock_model_path=root / CLOCK_MODEL_FILENAME,
        min_available_mb=int(settings.get("min_available_mb", MIN_AVAILABLE_MEMORY_MB)),
    )


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
                                  include_review_segments=True)
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
    cameras = config.cameras or workspace.camera_indexes()
    outer_progress = progress

    def progress(stage_name: str, done: int, total: int) -> None:
        peak = peak_memory_mb()
        if peak is not None:
            memory["stage_peaks_mb"][stage_name] = peak
        outer_progress(stage_name, done, total)
    results = []
    for camera in cameras:
        line = line_for_camera(config, workspace, camera)
        segments = workspace.archive_segments(camera)
        if line is None or not segments:
            summary["cameras"][camera] = {"skipped": "没有终点线或整段录像"}
            continue
        paths = extract_strips(
            config, segments, line, progress=progress,
            checkpoint=lambda: governor.checkpoint(cancelled), cancelled=cancelled,
        )
        summary["cameras"][camera] = {"strips": len(paths)}
        if stage == "strips" or cancelled():
            continue
        result = analyse_camera(
            config, workspace, camera, paths, progress=progress,
            checkpoint=lambda: governor.checkpoint(cancelled),
        )
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
    roster = {}
    metadata = workspace.metadata()
    if metadata is not None and metadata.race_id == workspace.race_id:
        roster = {athlete.bib: metadata.group_label(athlete.group_id) for athlete in metadata.athletes if athlete.bib}
    items: list[ProblemItem] = list(chip_check_items(workspace.reads, ChipCheckContext(roster=roster)))
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
            key: value for key, value in result.items() if key not in ("items", "auto_passed", "model")
        }
    items = sort_items(items)
    if config.render_cards:
        render_cards(items, output_dir, workspace.root, progress=progress, decode_threads=config.decode_threads)
    payload = {
        "schema_version": 1,
        "generated_at_ms": int(time.time() * 1000),
        "race_id": workspace.race_id,
        "stage_id": workspace.stage_id,
        "race_date": workspace.race_date,
        "cameras": cameras,
        "summary": summarize_items(items, auto_passed, unverifiable),
        "items": [item.to_payload() for item in items],
        "auto_passed": auto_passed,
        "live": config.live,
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
