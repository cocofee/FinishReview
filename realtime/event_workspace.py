"""Discovery and validation for saved FinishReview event workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from .passage_evidence import (
        HIGH_SPEED_SOURCE,
        REGULAR_SOURCE,
        PassageEvidenceAssociationStore,
    )
    from .passage_receiver import PassageEventStore
    from .preflight import PreflightJournal
    from .race_metadata import RaceMetadata, RaceMetadataStore
except ImportError:
    from passage_evidence import (
        HIGH_SPEED_SOURCE,
        REGULAR_SOURCE,
        PassageEvidenceAssociationStore,
    )
    from passage_receiver import PassageEventStore
    from preflight import PreflightJournal
    from race_metadata import RaceMetadata, RaceMetadataStore


METADATA_FILENAME = "cyclerace_race_metadata.json"
PASSAGE_FILENAME = "cyclerace_passage_events.jsonl"
ASSOCIATION_FILENAME = "passage_evidence_associations.jsonl"
PREFLIGHT_FILENAME = "preflight_tests.jsonl"
_UPDATE_FILENAMES = (
    METADATA_FILENAME,
    PASSAGE_FILENAME,
    ASSOCIATION_FILENAME,
    "video_timeline.jsonl",
    "终点复核清单.csv",
)


class EventWorkspaceError(RuntimeError):
    """Raised when a saved event workspace cannot be opened safely."""


@dataclass(frozen=True, slots=True)
class EventWorkspaceDescriptor:
    path: Path
    metadata: RaceMetadata
    modified_at_ms: int
    is_root: bool = False

    @property
    def race_name(self) -> str:
        return self.metadata.race_name.strip() or self.metadata.race_id

    @property
    def stage_name(self) -> str:
        return self.metadata.stage_name.strip() or self.metadata.stage_id


@dataclass(frozen=True, slots=True)
class EventWorkspaceSummary:
    passage_count: int
    confirmed_count: int


def _workspace_modified_at_ms(path: Path) -> int:
    timestamps = []
    for filename in _UPDATE_FILENAMES:
        candidate = path / filename
        try:
            if candidate.is_file():
                timestamps.append(candidate.stat().st_mtime_ns // 1_000_000)
        except OSError:
            continue
    if timestamps:
        return max(timestamps)
    try:
        return path.stat().st_mtime_ns // 1_000_000
    except OSError:
        return 0


def _metadata_for_workspace(path: Path) -> RaceMetadata:
    metadata_path = path / METADATA_FILENAME
    if not metadata_path.is_file():
        raise EventWorkspaceError("缺少 CycleRace 赛事信息文件")
    try:
        metadata = RaceMetadataStore(metadata_path).current()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise EventWorkspaceError(f"赛事信息无法读取：{error}") from error
    if metadata is None:
        raise EventWorkspaceError("CycleRace 赛事信息为空")
    return metadata


def validate_event_workspace(
    path: str | Path,
    workspace_root: str | Path,
) -> EventWorkspaceDescriptor:
    root = Path(workspace_root).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    if candidate != root and candidate.parent != root:
        raise EventWorkspaceError("赛事目录不在当前保存根目录中")
    if not candidate.is_dir():
        raise EventWorkspaceError("赛事目录不存在")
    metadata = _metadata_for_workspace(candidate)
    return EventWorkspaceDescriptor(
        path=candidate,
        metadata=metadata,
        modified_at_ms=_workspace_modified_at_ms(candidate),
        is_root=candidate == root,
    )


def discover_event_workspaces(
    workspace_root: str | Path,
) -> tuple[EventWorkspaceDescriptor, ...]:
    root = Path(workspace_root).expanduser().resolve()
    candidates = [root]
    try:
        candidates.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and path.name != ".finishreview"
        )
    except OSError as error:
        raise EventWorkspaceError(f"赛事保存根目录无法读取：{error}") from error

    workspaces = []
    for candidate in candidates:
        if not (candidate / METADATA_FILENAME).is_file():
            continue
        try:
            workspaces.append(validate_event_workspace(candidate, root))
        except EventWorkspaceError:
            continue
    return tuple(
        sorted(
            workspaces,
            key=lambda item: (
                -item.modified_at_ms,
                item.race_name.casefold(),
                str(item.path).casefold(),
            ),
        )
    )


def summarize_event_workspace(
    workspace: EventWorkspaceDescriptor,
) -> EventWorkspaceSummary:
    passage_path = workspace.path / PASSAGE_FILENAME
    if not passage_path.is_file():
        return EventWorkspaceSummary(0, 0)
    try:
        passage_store = PassageEventStore(passage_path)
        events = tuple(
            event
            for event in passage_store.events()
            if event.race_id == workspace.metadata.race_id
            and event.stage_id == workspace.metadata.stage_id
        )
        preflight_path = workspace.path / PREFLIGHT_FILENAME
        preflight_keys = (
            PreflightJournal(preflight_path).event_keys()
            if preflight_path.is_file()
            else frozenset()
        )
        events = tuple(
            event
            for event in events
            if (event.race_id, event.stage_id, event.event_id)
            not in preflight_keys
        )
        association_path = workspace.path / ASSOCIATION_FILENAME
        if not association_path.is_file():
            return EventWorkspaceSummary(len(events), 0)
        association_store = PassageEvidenceAssociationStore(association_path)
        confirmed_count = sum(
            association_store.get(event.event_id, REGULAR_SOURCE) is not None
            or association_store.get(event.event_id, HIGH_SPEED_SOURCE) is not None
            for event in events
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise EventWorkspaceError(f"赛事记录无法读取：{error}") from error
    return EventWorkspaceSummary(len(events), confirmed_count)


__all__ = [
    "ASSOCIATION_FILENAME",
    "EventWorkspaceDescriptor",
    "EventWorkspaceError",
    "EventWorkspaceSummary",
    "METADATA_FILENAME",
    "PASSAGE_FILENAME",
    "discover_event_workspaces",
    "summarize_event_workspace",
    "validate_event_workspace",
]
