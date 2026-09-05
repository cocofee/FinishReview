"""Incremental reducer for shared review-buffer pin and cleanup state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 2


class ReviewBufferJournalError(RuntimeError):
    """Raised when the shared review-buffer journal is invalid."""


class ReviewBufferJournalProjection:
    """Reduce mixed legacy/v2 records exactly once in append order."""

    def __init__(self) -> None:
        self.offset = 0
        self.file_id: tuple[int, int, int] | None = None
        self.owner_segments: dict[str, tuple[dict[str, Any], ...]] = {}
        self.owner_metadata: dict[str, dict[str, Any]] = {}
        self.cleanup_intents: dict[str, dict[str, Any]] = {}
        self.deleted_segment_ids: set[str] = set()
        self.deleted_intervals: list[tuple[int, int]] = []
        self.scan_cursors: dict[str, int] = {}

    def reset(self) -> None:
        self.offset = 0
        self.owner_segments.clear()
        self.owner_metadata.clear()
        self.cleanup_intents.clear()
        self.deleted_segment_ids.clear()
        self.deleted_intervals.clear()
        self.scan_cursors.clear()

    @staticmethod
    def _file_identity(stat_result) -> tuple[int, int, int]:
        return (
            int(getattr(stat_result, "st_dev", 0)),
            int(getattr(stat_result, "st_ino", 0)),
            int(getattr(stat_result, "st_mtime_ns", 0)),
        )

    def sync(self, path: str | Path) -> None:
        journal_path = Path(path)
        if not journal_path.is_file():
            if self.offset:
                self.reset()
            self.file_id = None
            return
        try:
            stat_result = journal_path.stat()
        except OSError as error:
            raise ReviewBufferJournalError(
                f"failed to inspect review buffer journal: {journal_path}"
            ) from error
        identity = self._file_identity(stat_result)
        if (
            self.file_id is not None
            and identity != self.file_id
            and any(identity)
            and any(self.file_id)
        ) or stat_result.st_size < self.offset:
            self.reset()
        self.file_id = identity
        if stat_result.st_size == self.offset:
            return
        try:
            with journal_path.open("rb") as source:
                source.seek(self.offset)
                content = source.read()
        except OSError as error:
            raise ReviewBufferJournalError(
                f"failed to read review buffer journal: {journal_path}"
            ) from error

        relative_offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith((b"\n", b"\r"))
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                relative_offset += len(raw_line)
                continue
            try:
                record = json.loads(stripped.decode("utf-8"))
                if not isinstance(record, Mapping):
                    raise ValueError("record must be an object")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                if line_number == len(lines) and not terminated:
                    absolute_offset = self.offset + relative_offset
                    self._truncate(journal_path, absolute_offset)
                    self.offset = absolute_offset
                    return
                raise ReviewBufferJournalError(
                    f"invalid review buffer journal line at {self.offset + relative_offset}"
                ) from error
            self.apply(record)
            relative_offset += len(raw_line)
        self.offset += relative_offset

    @staticmethod
    def _truncate(path: Path, size: int) -> None:
        try:
            with path.open("r+b") as output:
                output.truncate(int(size))
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            raise ReviewBufferJournalError(
                f"failed to recover review buffer journal: {path}"
            ) from error

    def apply(self, record: Mapping[str, Any]) -> None:
        record_type = str(record.get("record_type") or "")
        operation = str(record.get("op") or "")
        if operation == "pin" or record_type == "pin_set":
            owner_id = str(record.get("owner_id") or record.get("event_id") or "")
            if not owner_id:
                return
            raw_segments = record.get("segments") or ()
            segments = tuple(
                dict(payload)
                for payload in raw_segments
                if isinstance(payload, Mapping)
            )
            self.owner_segments[owner_id] = segments
            self.owner_metadata[owner_id] = {
                "owner_kind": str(record.get("owner_kind") or "legacy"),
                "logical_event_id": str(
                    record.get("logical_event_id") or record.get("event_id") or ""
                ),
                "revision": int(record.get("revision") or 0),
                "race_id": str(record.get("race_id") or ""),
                "window_id": str(record.get("window_id") or ""),
            }
            return
        if operation == "release" or record_type == "pin_release":
            owner_id = str(record.get("owner_id") or record.get("event_id") or "")
            self.owner_segments.pop(owner_id, None)
            self.owner_metadata.pop(owner_id, None)
            return
        if record_type == "cleanup_intent":
            transaction_id = str(record.get("transaction_id") or "")
            if transaction_id:
                self.cleanup_intents[transaction_id] = dict(record)
            return
        if record_type == "cleanup_aborted":
            self.cleanup_intents.pop(str(record.get("transaction_id") or ""), None)
            return
        if record_type == "cleanup_committed":
            self.cleanup_intents.pop(str(record.get("transaction_id") or ""), None)
            segment_id = str(record.get("segment_id") or "")
            if segment_id:
                self.deleted_segment_ids.add(segment_id)
                for owner_id, segments in tuple(self.owner_segments.items()):
                    retained = tuple(
                        payload
                        for payload in segments
                        if str(payload.get("segment_id") or "") != segment_id
                    )
                    if retained:
                        self.owner_segments[owner_id] = retained
                    else:
                        self.owner_segments.pop(owner_id, None)
                        self.owner_metadata.pop(owner_id, None)
            try:
                start = int(record["started_at_ms"])
                end = int(record["ended_at_ms"])
            except (KeyError, TypeError, ValueError):
                return
            self._add_deleted_interval(start, end)
            return
        if record_type == "live_scan_cursor":
            scanner_id = str(record.get("scanner_id") or "")
            try:
                cursor = int(record["next_core_started_at_ms"])
            except (KeyError, TypeError, ValueError):
                return
            if scanner_id:
                self.scan_cursors[scanner_id] = max(
                    cursor,
                    self.scan_cursors.get(scanner_id, cursor),
                )

    def _add_deleted_interval(self, start: int, end: int) -> None:
        start, end = sorted((int(start), int(end)))
        merged: list[tuple[int, int]] = []
        inserted = False
        for current_start, current_end in self.deleted_intervals:
            if current_end < start:
                merged.append((current_start, current_end))
            elif end < current_start:
                if not inserted:
                    merged.append((start, end))
                    inserted = True
                merged.append((current_start, current_end))
            else:
                start = min(start, current_start)
                end = max(end, current_end)
        if not inserted:
            merged.append((start, end))
        self.deleted_intervals = merged

    def owner_ids_for_event(
        self,
        event_id: str,
        revision: int,
    ) -> tuple[str, ...]:
        event_id = str(event_id)
        revision = int(revision)
        return tuple(
            owner_id
            for owner_id, metadata in self.owner_metadata.items()
            if metadata.get("logical_event_id") == event_id
            and 0 < int(metadata.get("revision") or 0) <= revision
            and metadata.get("owner_kind") == "event_window"
        )


__all__ = [
    "ReviewBufferJournalError",
    "ReviewBufferJournalProjection",
    "SCHEMA_VERSION",
]
