"""Constants, race-folder helpers and video-candidate records shared by the finish review window."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .evidence_clock import LIVE_DATE_TOLERANCE_MS, align_evidence_timestamp
from .passage_receiver import PassageEvent
from .race_metadata import RaceMetadata, RaceMetadataStore


@dataclass(frozen=True, slots=True)
class _VideoCandidateDelivery:
    generation: int
    candidates: tuple[object, ...]
    persisted: bool = True


@dataclass(frozen=True, slots=True)
class _VideoCandidatePersistenceFailure:
    generation: int
    message: str


IS_WINDOWS = os.name == "nt"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
HIGH_SPEED_INDEX_FILENAME = ".videopipe_auyat_index.json"
LIVE_EVIDENCE_DATE_TOLERANCE_MS = LIVE_DATE_TOLERANCE_MS
# How far the picture may sit from a problem's own frame before the panel calls it
# "browsing elsewhere"; one second covers stepping a few frames around a crossing.
PROBLEM_FRAME_TOLERANCE_MS = 1000
CYCLERACE_INBOX_DIRNAME = ".finishreview"
_TEST_GROUP_NAMES = frozenset({"test", "testgroup"})
_TEST_GROUP_MARKERS = ("测试", "检测")
_INVALID_EVENT_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _format_point_playback_time(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=BEIJING_TIMEZONE)
    return value.strftime("%H:%M:%S.%f")[:-3]


def _high_speed_target_dates(
    events: tuple[PassageEvent, ...],
    timestamp_overrides: dict[str, tuple[int, int]] | None = None,
) -> frozenset[date]:
    overrides = timestamp_overrides or {}
    dates = {
        datetime.fromtimestamp(
            (
                overrides.get(
                    event.event_id,
                    (event.timeline_timestamp_ms, event.timeline_timestamp_ms),
                )[1]
                / 1000.0
            ),
            tz=BEIJING_TIMEZONE,
        ).date()
        for event in events
        if event.timeline_timestamp_ms >= 86_400_000
    }
    return frozenset(dates or {datetime.now(BEIJING_TIMEZONE).date()})


def _align_live_evidence_timestamp(
    timestamp_ms: int,
    received_at_ms: int,
    *,
    tolerance_ms: int = LIVE_EVIDENCE_DATE_TOLERANCE_MS,
) -> int:
    return align_evidence_timestamp(timestamp_ms, received_at_ms, tolerance_ms=tolerance_ms)


def _is_test_group_name(value: str) -> bool:
    normalized = re.sub(r"[\s_-]+", "", str(value or "")).casefold()
    return normalized in _TEST_GROUP_NAMES or any(
        marker in normalized for marker in _TEST_GROUP_MARKERS
    )


def _historical_evidence_timestamp_overrides(
    events: tuple[PassageEvent, ...],
) -> dict[str, tuple[int, int]]:
    overrides = {}
    for event in events:
        timestamp_ms = int(event.timeline_timestamp_ms)
        if (
            not event.is_active
            or (event.passage_timestamp_ms is None and timestamp_ms < 86_400_000)
        ):
            continue
        aligned_timestamp_ms = timestamp_ms
        if event.received_at_ms > 0:
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                timestamp_ms,
                event.received_at_ms,
            )
        elif event.emitted_at_ms > 0:
            aligned_timestamp_ms = _align_live_evidence_timestamp(
                timestamp_ms,
                event.emitted_at_ms,
            )
        if aligned_timestamp_ms != timestamp_ms:
            overrides[event.event_id] = (timestamp_ms, aligned_timestamp_ms)
    return overrides


def _safe_event_folder_name(value: str, fallback: str) -> str:
    name = _INVALID_EVENT_FOLDER_CHARS.sub("_", str(value).strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = _INVALID_EVENT_FOLDER_CHARS.sub("_", str(fallback).strip())
        name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "赛事"
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:80].rstrip(" .") or "赛事"


def _saved_race_id(folder: Path) -> str | None:
    """The race a saved folder holds, from its metadata, else from its review scope."""
    try:
        existing = RaceMetadataStore(folder / "cyclerace_race_metadata.json").current()
    except (OSError, RuntimeError, ValueError):
        existing = None
    if existing is not None:
        return existing.race_id
    scope_path = folder / "review_event_scope.json"
    if scope_path.is_file():
        import json
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(scope, dict):
            return scope.get("race_id")
    return None


def _saved_race_folders(root: Path) -> list[tuple[Path, str, str]]:
    """Race folders in the root and in its competition folders, with their race id and name."""

    found = []
    try:
        level_one = sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return found
    for path in level_one:
        folders = [path]
        if not (path / "cyclerace_race_metadata.json").is_file() and not (path / "review_event_scope.json").is_file():
            try:
                folders = sorted(child for child in path.iterdir() if child.is_dir() and not child.name.startswith("."))
            except OSError:
                continue
        for folder in folders:
            try:
                saved = RaceMetadataStore(folder / "cyclerace_race_metadata.json").current()
            except (OSError, RuntimeError, ValueError):
                saved = None
            race_id = saved.race_id if saved is not None else _saved_race_id(folder)
            if race_id:
                found.append((folder, race_id, saved.race_name if saved is not None else ""))
    return found


def _event_workspace_dir(root: Path, metadata: RaceMetadata, *, today: date | None = None) -> Path:
    root = Path(root).expanduser().resolve()
    try:
        own = RaceMetadataStore(root / "cyclerace_race_metadata.json").current()
    except (OSError, RuntimeError, ValueError):
        own = None
    if own is not None and own.race_id == metadata.race_id:
        # An older layout keeps the race in the root itself: its recordings are
        # there, so coming back to it must not open an empty folder beside them.
        return root
    folder_name = _safe_event_folder_name(metadata.race_name, metadata.race_id)
    # A race already saved stays where it is: straight in the root (before
    # 11-05) or inside its competition's folder.
    saved = _saved_race_folders(root)
    for candidate, race_id, _race_name in saved:
        if race_id == metadata.race_id:
            return candidate
    # 11-05: one folder per competition, one folder per race in it, so the
    # 现代五项 days and its 男子/女子, A/B 组 races sit together.  The
    # competition's folder may have been renamed shorter by hand; a race of
    # the same name already filed there shows where it is.
    competition = next((candidate.parent for candidate, _race_id, race_name in saved
                        if candidate.parent != root and race_name == metadata.race_name), root / folder_name)
    # A new race is filed under the day it first arrived, not CycleRace's stage
    # date, which can be wrong (特奥 ran 10-28 with a stage dated 09-20).
    day = (today or datetime.now(BEIJING_TIMEZONE).date()).isoformat()
    stage_name = _safe_event_folder_name(metadata.stage_name, "") if str(metadata.stage_name or "").strip() else ""
    base = f"{day} {stage_name}" if stage_name and stage_name != folder_name else day
    if (competition / "cyclerace_race_metadata.json").is_file():
        # An older race folder already has the competition's name: keep the new race beside it.
        competition, base = root, f"{day} {folder_name}" + (f" {stage_name}" if base != day else "")
    candidates = [competition / base]
    candidates.extend(competition / f"{base}_{index}" for index in range(2, 100))
    for candidate in candidates:
        if not candidate.exists():
            return candidate
        try:
            is_empty = not any(candidate.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            return candidate
    raise RuntimeError("无法为赛事创建唯一保存目录")
