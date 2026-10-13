"""Automatic chip/recorder clock calibration with linear drift.

The chip time sent by CycleRace is the official clock. Recorded video uses the
FinishReview computer clock. Their difference is modelled once per camera as

    recorder_time = chip_time + intercept + slope * (chip_time - reference)

A coarse histogram search finds the offset without any manual anchor. The fine
fit uses isolated riders only (no other chip within five seconds) and rejects
outliers. Manual anchors remain a cross-check: they are compared with the
automatic model and never silently replace it.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from .durable_jsonl import append_jsonl_records, locked_jsonl
from .time_domain import ChipClockMs, RecorderClockMs


SCHEMA_VERSION = 1
CLOCK_MODEL_FILENAME = "video_clock_models.jsonl"
COARSE_RANGE_MS = 90_000
COARSE_BIN_MS = 100
ISOLATION_MS = 5_000
FINE_WINDOW_MS = 1_500
FIT_ITERATIONS = 5
FIT_FLOOR_MS = 80.0
FIT_MAD_FACTOR = 2.5
MIN_DRIFT_SUPPORT = 8
MANUAL_ANCHOR_TOLERANCE_MS = 300
SESSION_DEVIATION_MS = 100
_MS_PER_HOUR = 3_600_000


class ClockSyncError(RuntimeError):
    """Raised when the calibration journal is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class CoarseOffset:
    """Peak of the crossing-minus-chip histogram."""

    offset_ms: int
    support: int
    background: float
    second_support: int
    pair_count: int

    @property
    def distinct(self) -> bool:
        """A single, clear peak; otherwise the operator must check the clock."""

        return (
            self.support >= 8
            and self.support >= 3.0 * max(1.0, self.background)
            and self.support >= 1.5 * self.second_support
        )


@dataclass(frozen=True, slots=True)
class ClockModel:
    """One calibration version for one camera."""

    camera_index: int
    intercept_ms: float
    slope: float
    reference_ms: int
    valid_from_ms: int
    valid_to_ms: int
    support: int
    candidates: int
    residual_mad_ms: float
    coarse_offset_ms: int
    coarse_support: int
    version: int = 0
    created_at_ms: int = 0
    source: str = "auto"
    session_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if int(self.camera_index) <= 0:
            raise ValueError("camera_index must be positive")
        if not math.isfinite(float(self.intercept_ms)) or not math.isfinite(float(self.slope)):
            raise ValueError("clock model must be finite")
        if abs(float(self.slope)) >= 0.01:
            raise ValueError("clock drift is implausibly large")
        if int(self.valid_to_ms) < int(self.valid_from_ms):
            raise ValueError("clock model validity range is reversed")
        if int(self.version) < 0 or int(self.support) < 0:
            raise ValueError("invalid clock model metadata")

    @property
    def slope_ms_per_hour(self) -> float:
        return float(self.slope) * _MS_PER_HOUR

    def offset_at(self, chip_ms: int) -> int:
        """Recorder minus chip at a chip-domain time."""

        return int(round(self.intercept_ms + self.slope * (int(chip_ms) - self.reference_ms)))

    def to_recorder(self, chip_ms: int) -> RecorderClockMs:
        return RecorderClockMs(int(chip_ms) + self.offset_at(chip_ms))

    def to_chip(self, recorder_ms: int) -> ChipClockMs:
        """Invert the model; the offset is removed exactly once."""

        value = (
            float(recorder_ms) - self.intercept_ms + self.slope * self.reference_ms
        ) / (1.0 + self.slope)
        return ChipClockMs(int(round(value)))

    def same_fit(self, other: "ClockModel", *, tolerance_ms: int = 5) -> bool:
        if other.camera_index != self.camera_index:
            return False
        start = min(self.valid_from_ms, other.valid_from_ms)
        end = max(self.valid_to_ms, other.valid_to_ms)
        return all(
            abs(self.offset_at(point) - other.offset_at(point)) <= tolerance_ms
            for point in (start, (start + end) // 2, end)
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["session_ids"] = list(self.session_ids)
        payload["notes"] = list(self.notes)
        payload["schema_version"] = SCHEMA_VERSION
        payload["record_type"] = "clock_model"
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClockModel":
        if not isinstance(payload, Mapping):
            raise ValueError("clock model must be an object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported clock model schema_version")
        if payload.get("record_type") != "clock_model":
            raise ValueError("unsupported clock model record_type")
        return cls(
            camera_index=int(payload["camera_index"]),
            intercept_ms=float(payload["intercept_ms"]),
            slope=float(payload["slope"]),
            reference_ms=int(payload["reference_ms"]),
            valid_from_ms=int(payload["valid_from_ms"]),
            valid_to_ms=int(payload["valid_to_ms"]),
            support=int(payload["support"]),
            candidates=int(payload.get("candidates", payload["support"])),
            residual_mad_ms=float(payload.get("residual_mad_ms", 0.0)),
            coarse_offset_ms=int(payload.get("coarse_offset_ms", 0)),
            coarse_support=int(payload.get("coarse_support", 0)),
            version=int(payload.get("version", 0)),
            created_at_ms=int(payload.get("created_at_ms", 0)),
            source=str(payload.get("source", "auto")),
            session_ids=tuple(str(item) for item in payload.get("session_ids", ())),
            notes=tuple(str(item) for item in payload.get("notes", ())),
        )


@dataclass(frozen=True, slots=True)
class ClockCrossing:
    """A detected line crossing in recorder time, tagged with its session."""

    recorder_ms: int
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    model: Optional[ClockModel]
    coarse: Optional[CoarseOffset]
    isolated_pairs: tuple[tuple[int, int], ...] = ()
    kept: tuple[bool, ...] = ()
    residuals_ms: tuple[float, ...] = ()
    session_medians_ms: Mapping[str, float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.model is not None


@dataclass(frozen=True, slots=True)
class ClockAnchor:
    """An operator-provided offset: a manual calibration or a confirmed frame."""

    camera_index: int
    chip_ms: int
    offset_ms: int
    kind: str
    label: str = ""
    reference: str = ""


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    anchor: ClockAnchor
    auto_offset_ms: int
    difference_ms: int
    within_tolerance: bool
    message: str


def _sorted_array(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=np.float64)
    if array.size:
        array.sort()
    return array


def coarse_offset(
    crossing_ms: Iterable[float],
    chip_ms: Iterable[float],
    *,
    range_ms: int = COARSE_RANGE_MS,
    bin_ms: int = COARSE_BIN_MS,
) -> Optional[CoarseOffset]:
    """Find the dominant crossing-minus-chip offset without any anchor."""

    crossings = _sorted_array(crossing_ms)
    chips = _sorted_array(chip_ms)
    range_ms = int(range_ms)
    bin_ms = max(1, int(bin_ms))
    if not crossings.size or not chips.size or range_ms <= 0:
        return None
    lower = np.searchsorted(crossings, chips - range_ms, "left")
    upper = np.searchsorted(crossings, chips + range_ms, "right")
    pieces = [crossings[a:b] - chip for chip, a, b in zip(chips, lower, upper) if b > a]
    if not pieces:
        return None
    differences = np.concatenate(pieces)
    edges = np.arange(-range_ms, range_ms + 1, bin_ms, dtype=np.float64)
    if edges.size < 2:
        return None
    histogram, _ = np.histogram(differences, bins=edges)
    smooth = np.convolve(histogram, np.ones(3), "same")
    peak = int(np.argmax(smooth))
    offset = float(edges[peak] + bin_ms / 2.0)
    centres = edges[:-1] + bin_ms / 2.0
    far = np.abs(centres - offset) > 2_000
    return CoarseOffset(
        offset_ms=int(round(offset)),
        support=int(smooth[peak]),
        background=float(np.median(smooth)),
        second_support=int(smooth[far].max()) if far.any() else 0,
        pair_count=int(differences.size),
    )


def isolated_pairs(
    crossing_ms: Sequence[float],
    chip_ms: Sequence[float],
    expected_offset,
    *,
    isolation_ms: int = ISOLATION_MS,
    window_ms: int = FINE_WINDOW_MS,
) -> list[tuple[int, int, int, int]]:
    """Return (chip index, chip time, crossing - chip, crossing) per isolated rider.

    ``expected_offset`` is either a number or a callable of chip time. Only
    chips without another chip within ``isolation_ms`` and with exactly one
    crossing inside ``window_ms`` of the expected position are used.
    """

    crossings = _sorted_array(crossing_ms)
    order = np.argsort(np.asarray(chip_ms, dtype=np.float64), kind="stable")
    chips = np.asarray(chip_ms, dtype=np.float64)[order]
    pairs = []
    for position, chip in enumerate(chips):
        previous_gap = chip - chips[position - 1] if position else math.inf
        next_gap = chips[position + 1] - chip if position + 1 < len(chips) else math.inf
        if min(previous_gap, next_gap) < isolation_ms:
            continue
        offset = expected_offset(chip) if callable(expected_offset) else expected_offset
        target = chip + float(offset)
        a = np.searchsorted(crossings, target - window_ms, "left")
        b = np.searchsorted(crossings, target + window_ms, "right")
        if b - a == 1:
            crossing = int(round(crossings[a]))
            pairs.append((int(order[position]), int(round(chip)), crossing - int(round(chip)), crossing))
    return pairs


def robust_line_fit(
    times_ms: Sequence[float],
    values_ms: Sequence[float],
    *,
    iterations: int = FIT_ITERATIONS,
    floor_ms: float = FIT_FLOOR_MS,
    mad_factor: float = FIT_MAD_FACTOR,
    fit_slope: bool = True,
) -> tuple[float, float, float, np.ndarray, np.ndarray]:
    """Fit value = a + b * (t - mean) and reject outliers iteratively.

    Returns ``(a, b, mean, keep, residuals)``. A point is kept while its
    residual is within ``max(floor_ms, mad_factor * median |residual|)``.
    """

    t = np.asarray(times_ms, dtype=np.float64)
    y = np.asarray(values_ms, dtype=np.float64)
    if t.size == 0:
        raise ValueError("no points to fit")
    mean = float(t.mean())
    keep = np.ones(t.size, dtype=bool)
    a, b = float(np.median(y)), 0.0
    residuals = y - a
    for _ in range(max(1, int(iterations))):
        if keep.sum() == 0:
            break
        if fit_slope and keep.sum() >= 2 and np.ptp(t[keep]) > 0:
            b, a = (float(value) for value in np.polyfit(t[keep] - mean, y[keep], 1))
        else:
            b, a = 0.0, float(np.median(y[keep]))
        residuals = y - (a + b * (t - mean))
        limit = max(float(floor_ms), mad_factor * float(np.median(np.abs(residuals[keep]))))
        keep = np.abs(residuals) <= limit
    return a, b, mean, keep, residuals


def calibrate_camera(
    crossings: Sequence[ClockCrossing | float],
    chip_ms: Sequence[float],
    *,
    camera_index: int,
    range_ms: int = COARSE_RANGE_MS,
    bin_ms: int = COARSE_BIN_MS,
    isolation_ms: int = ISOLATION_MS,
    window_ms: int = FINE_WINDOW_MS,
    min_drift_support: int = MIN_DRIFT_SUPPORT,
    refinements: int = 2,
) -> CalibrationResult:
    """Coarse search plus robust drift fit for one camera.

    All sessions of one camera share the recording computer's clock, so one
    drift line is fitted across sessions. Sessions whose isolated riders sit
    more than 100 ms away from the line are reported, not corrected silently.
    """

    items = [
        item if isinstance(item, ClockCrossing) else ClockCrossing(int(item))
        for item in crossings
    ]
    crossing_times = [item.recorder_ms for item in items]
    chips = [float(value) for value in chip_ms]
    coarse = coarse_offset(crossing_times, chips, range_ms=range_ms, bin_ms=bin_ms)
    if coarse is None:
        return CalibrationResult(None, None, warnings=("没有可用于自动校时的过线或芯片记录",))
    warnings = []
    if not coarse.distinct:
        warnings.append(
            f"自动粗校峰值不明显（峰 {coarse.support}，背景 {coarse.background:.1f}，次峰 {coarse.second_support}），请人工核对"
        )

    expected = float(coarse.offset_ms)
    pairs: list[tuple[int, int, int, int]] = []
    fit = None
    for _ in range(1 + max(0, int(refinements))):
        pairs = isolated_pairs(
            crossing_times, chips, expected,
            isolation_ms=isolation_ms, window_ms=window_ms,
        )
        if not pairs:
            break
        times = [pair[1] for pair in pairs]
        values = [pair[2] for pair in pairs]
        fit = robust_line_fit(times, values, fit_slope=len(pairs) >= min_drift_support)
        a, b, mean = fit[0], fit[1], fit[2]
        expected = (lambda chip, a=a, b=b, mean=mean: a + b * (chip - mean))
    if fit is None or not pairs:
        return CalibrationResult(
            None, coarse,
            warnings=tuple(warnings) + ("没有前后 5 秒无他人的单独过线者，无法细校",),
        )
    a, b, mean, keep, residuals = fit
    kept_count = int(keep.sum())
    if kept_count < 2:
        return CalibrationResult(
            None, coarse, tuple((p[1], p[2]) for p in pairs), tuple(bool(k) for k in keep),
            tuple(float(r) for r in residuals),
            warnings=tuple(warnings) + ("单独过线者太少，无法细校",),
        )
    if len(pairs) < min_drift_support:
        warnings.append(f"单独过线者只有 {len(pairs)} 人，只估计固定偏移，不估计漂移")
    times = np.asarray([p[1] for p in pairs], dtype=np.float64)
    reference = int(round(mean))
    mad = float(np.median(np.abs(residuals[keep])))

    crossing_sessions = {}
    for item in items:
        crossing_sessions.setdefault(int(item.recorder_ms), item.session_id)
    # All isolated riders count here, including rejected ones: a session that
    # really sits off the shared line is rejected as a whole by the fit.
    by_session: dict[str, list[float]] = {}
    for pair, residual in zip(pairs, residuals):
        session = crossing_sessions.get(pair[3], "")
        by_session.setdefault(session, []).append(float(residual))
    session_medians = {key: float(np.median(values)) for key, values in by_session.items()}
    for key, values in by_session.items():
        median = session_medians[key]
        if len(values) >= 5 and abs(median) > SESSION_DEVIATION_MS:
            warnings.append(f"录像会话 {key or '-'} 与漂移线相差 {median:+.0f} ms，请核对该会话校时")

    sessions = tuple(sorted({item.session_id for item in items if item.session_id}))
    model = ClockModel(
        camera_index=int(camera_index),
        intercept_ms=float(a),
        slope=float(b),
        reference_ms=reference,
        valid_from_ms=int(times.min()),
        valid_to_ms=int(times.max()),
        support=kept_count,
        candidates=len(pairs),
        residual_mad_ms=mad,
        coarse_offset_ms=int(coarse.offset_ms),
        coarse_support=int(coarse.support),
        session_ids=sessions,
        notes=tuple(warnings),
    )
    return CalibrationResult(
        model,
        coarse,
        tuple((p[1], p[2]) for p in pairs),
        tuple(bool(k) for k in keep),
        tuple(float(r) for r in residuals),
        session_medians,
        tuple(warnings),
    )


def check_anchors(
    model: ClockModel,
    anchors: Iterable[ClockAnchor],
    *,
    tolerance_ms: int = MANUAL_ANCHOR_TOLERANCE_MS,
    bias_ms: int = 0,
) -> tuple[AnchorCheck, ...]:
    """Compare operator anchors with the automatic model.

    ``bias_ms`` removes a known systematic difference between the operator's
    frame choice and the automatic crossing definition before comparison.
    """

    checks = []
    for anchor in anchors:
        if anchor.camera_index != model.camera_index:
            continue
        auto = model.offset_at(anchor.chip_ms) - int(bias_ms)
        difference = int(anchor.offset_ms) - auto
        ok = abs(difference) <= int(tolerance_ms)
        source = "人工校时" if anchor.kind == "manual_calibration" else "人工确认"
        if ok:
            message = f"{source} {anchor.label or '-'} 与自动校时相差 {difference:+d} ms，一致"
        else:
            message = (
                f"{source} {anchor.label or '-'} 与自动校时相差 {difference:+d} ms，"
                f"超过 {int(tolerance_ms)} ms，请核对；未覆盖自动校时"
            )
        checks.append(AnchorCheck(anchor, auto, difference, ok, message))
    return tuple(checks)


class ClockModelStore:
    """Append-only calibration versions; processed problems keep their version."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self._lock = threading.RLock()
        self._models: dict[tuple[int, int], ClockModel] = {}
        self._load()

    def _load(self) -> None:
        self._models.clear()
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise ClockSyncError(f"failed to read clock models: {self.journal_path}") from error
        lines = content.splitlines(keepends=True)
        for number, raw in enumerate(lines, start=1):
            stripped = raw.rstrip(b"\r\n")
            if not stripped:
                continue
            try:
                model = ClockModel.from_payload(json.loads(stripped.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                if number == len(lines) and not raw.endswith((b"\n", b"\r")):
                    break
                raise ClockSyncError(f"invalid clock model line {number}: {error}") from error
            self._models[(model.camera_index, model.version)] = model

    def models(self, camera_index: Optional[int] = None) -> tuple[ClockModel, ...]:
        with self._lock:
            values = sorted(self._models.values(), key=lambda m: (m.camera_index, m.version))
        if camera_index is None:
            return tuple(values)
        return tuple(model for model in values if model.camera_index == int(camera_index))

    def latest(self, camera_index: int) -> Optional[ClockModel]:
        models = self.models(camera_index)
        return models[-1] if models else None

    def get(self, camera_index: int, version: int) -> Optional[ClockModel]:
        with self._lock:
            return self._models.get((int(camera_index), int(version)))

    def publish(self, model: ClockModel, *, tolerance_ms: int = 5) -> ClockModel:
        """Store a refit as a new version unless it equals the latest one."""

        with self._lock, locked_jsonl(self.journal_path) as path:
            self._load()
            latest = self.latest(model.camera_index)
            if latest is not None and latest.same_fit(model, tolerance_ms=tolerance_ms):
                return latest
            version = (latest.version if latest is not None else 0) + 1
            stored = replace(model, version=version, created_at_ms=int(time.time() * 1000))
            path.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl_records(
                path,
                [json.dumps(stored.to_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")],
                description="clock model journal",
                already_locked=True,
            )
            self._models[(stored.camera_index, stored.version)] = stored
            return stored


__all__ = [
    "AnchorCheck",
    "CLOCK_MODEL_FILENAME",
    "COARSE_RANGE_MS",
    "CalibrationResult",
    "ClockAnchor",
    "ClockCrossing",
    "ClockModel",
    "ClockModelStore",
    "ClockSyncError",
    "CoarseOffset",
    "MANUAL_ANCHOR_TOLERANCE_MS",
    "calibrate_camera",
    "check_anchors",
    "coarse_offset",
    "isolated_pairs",
    "robust_line_fit",
]
