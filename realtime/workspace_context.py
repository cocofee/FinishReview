"""Local event selection and recovery for older, mixed race directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .passage_evidence import PassageEvidenceAssociationStore
from .passage_receiver import PassageEventStore
from .race_metadata import RaceGroupMetadata, RaceMetadata, RaceMetadataStore
from .video_timeline import VideoTimelineStore


SCOPE_FILENAME = "review_event_scope.json"
ARCHIVE_FILENAME = "review_workspace.json"


def _read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _write_object(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def saved_archive_path(root):
    """Restore only a root or direct-child archive inside the configured root."""
    root = Path(root).resolve()
    value = _read_object(root / ".finishreview" / ARCHIVE_FILENAME)
    relative = value.get("relative_path")
    if value.get("mode") != "archive" or not isinstance(relative, str):
        return None
    candidate = (root / relative).resolve()
    if ((candidate == root or candidate.parent == root) and candidate.is_dir()
            and any((candidate / name).is_file() for name in
                    ("cyclerace_passage_events.jsonl", "cyclerace_race_metadata.json"))):
        return candidate
    return None


def recovered_archive_path(root):
    """Prefer a uniquely identified legacy recording over newer inbox metadata."""
    root = Path(root)
    if ((root / "cyclerace_race_metadata.json").exists()
            or not (root / "cyclerace_passage_events.jsonl").is_file()
            or not (root / "video_timeline.jsonl").is_file()):
        return None
    timeline = VideoTimelineStore(root / "video_timeline.jsonl")
    store = EventWorkspaceMetadataStore(root / "cyclerace_race_metadata.json",
                                        PassageEventStore(root / "cyclerace_passage_events.jsonl"), timeline)
    return root.resolve() if timeline.segments() and (store.recovered or len(store.candidates()) > 1) else None


def save_workspace_mode(root, path, mode):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path != root and path.parent != root:
        raise ValueError("赛事目录不在当前保存根目录中")
    _write_object(root / ".finishreview" / ARCHIVE_FILENAME, {
        "schema_version": 1,
        "mode": mode,
        "relative_path": str(path.relative_to(root)),
    })


class EventWorkspaceMetadataStore(RaceMetadataStore):
    """Keep recovered/local context separate from authoritative metadata revisions."""

    def __init__(self, snapshot_path, passage_store, timeline_store):
        super().__init__(snapshot_path)
        self.scope_path = self.snapshot_path.parent / SCOPE_FILENAME
        self._recovered = None
        self._candidates = {}
        events = passage_store.events(include_inactive=True)
        by_scope = {}
        for event in events:
            by_scope.setdefault((event.race_id, event.stage_id), []).append(event)
        for scope, members in by_scope.items():
            groups = {event.group_id: event.group_name or event.group_id for event in members}
            latest = max(members, key=lambda event: (event.received_at_ms, event.revision))
            self._candidates[scope] = RaceMetadata(
                race_id=scope[0], stage_id=scope[1], revision=1, emitted_at_ms=0,
                race_name=latest.race_name or scope[0], stage_name=latest.stage_name or scope[1],
                groups=tuple(RaceGroupMetadata(key, name) for key, name in sorted(groups.items())),
            )
        if self._current is not None:
            self._candidates[(self._current.race_id, self._current.stage_id)] = self._current
        selected = _read_object(self.scope_path)
        scope = (selected.get("race_id"), selected.get("stage_id"))
        if all(isinstance(value, str) for value in scope):
            self._recovered = self._candidates.get(scope)
        if self._recovered is not None or self._current is not None:
            return

        # A unique recording race is stronger evidence than the latest chip
        # delivery, which may belong to an unrelated race received much later.
        recording_races = {segment.race_id for segment in timeline_store.segments() if segment.race_id}
        candidates = set(self._candidates)
        if recording_races:
            candidates = {scope for scope in candidates if scope[0] in recording_races}
        if len(candidates) != 1:
            association_path = self.snapshot_path.parent / "passage_evidence_associations.jsonl"
            if association_path.is_file():
                associated_ids = {item.passage_event_id
                                  for item in PassageEvidenceAssociationStore(association_path).associations()}
                confirmed_scopes = {(event.race_id, event.stage_id) for event in events
                                    if event.event_id in associated_ids}
                if confirmed_scopes:
                    candidates &= confirmed_scopes
        if len(candidates) == 1:
            self._recovered = self._candidates[next(iter(candidates))]

    @property
    def recovered(self):
        return self._recovered is not None and self._current is None

    def candidates(self):
        return tuple(sorted(self._candidates.values(),
                            key=lambda item: (item.race_name, item.stage_name, item.race_id, item.stage_id)))

    def current(self):
        with self._lock:
            return self._recovered or self._current

    def select(self, race_id, stage_id):
        metadata = self._candidates.get((race_id, stage_id))
        if metadata is None:
            raise ValueError("所选赛事不在此目录中")
        _write_object(self.scope_path, {"schema_version": 1, "race_id": race_id, "stage_id": stage_id})
        self._recovered = metadata
        return metadata

    def store(self, metadata):
        # Recovered metadata is never passed to the server's revision logic.
        result = super().store(metadata)
        self._recovered = None
        self.scope_path.unlink(missing_ok=True)
        self._candidates[(metadata.race_id, metadata.stage_id)] = metadata
        return result
