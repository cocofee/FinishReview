"""Auditable manual identity/frame confirmations, separate from official timing."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .durable_jsonl import append_jsonl_records, locked_jsonl


ARRIVAL_REVIEWS_FILENAME = "arrival_reviews.jsonl"
ACTIONS = {"bicycle", "confirm", "not_rider", "held"}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def event_binding(event) -> str:
    return fingerprint([event.event_id, event.race_id, event.stage_id, event.bib,
                        event.chip_id, int(event.timeline_timestamp_ms), event.revision,
                        event.is_active])


def candidate_binding(item) -> str:
    return fingerprint([item.item_id, dict(item.evidence.get("frame") or {}),
                        item.calibration_version, item.evidence.get("analysis_signature", ""),
                        bool(item.evidence.get("configuration_stale")),
                        bool(item.evidence.get("superseded"))])


class ArrivalReviewStore:
    def __init__(self, path):
        self.path = Path(path)
        self._entries = []
        self._active = {}
        self._latest = {}
        self._stamp = None
        self.refresh()

    def _read_locked(self):
        entries, offset = [], 0
        data = self.path.read_bytes() if self.path.exists() else b""
        lines = data.splitlines(keepends=True)
        for index, raw in enumerate(lines):
            try:
                entry = json.loads(raw.decode("utf-8"))
                if (not isinstance(entry, dict) or entry.get("action") not in ACTIONS | {"undo"}
                        or not entry.get("id") or not isinstance(entry.get("scope"), list)):
                    raise ValueError("invalid arrival review entry")
            except (ValueError, UnicodeDecodeError) as error:
                if index == len(lines) - 1 and not raw.endswith((b"\n", b"\r")):
                    with self.path.open("r+b") as journal:
                        journal.truncate(offset)
                        journal.flush()
                        os.fsync(journal.fileno())
                    break
                raise ValueError(f"过线审核日志第 {index + 1} 行损坏") from error
            entries.append(entry)
            offset += len(raw)
        self._entries = entries
        self._reindex()
        self._update_stamp()

    def _reindex(self):
        undone = {entry["target"] for entry in self._entries if entry["action"] == "undo"}
        self._active, self._latest = {}, {}
        for entry in self._entries:
            if entry["action"] == "undo" or entry["id"] in undone:
                continue
            scope = tuple(entry["scope"])
            self._active.setdefault(scope, []).append(entry)
            self._latest[(scope, entry["key"])] = entry

    def _update_stamp(self):
        stamp = self.path.stat() if self.path.exists() else None
        self._stamp = (stamp.st_mtime_ns, stamp.st_size) if stamp else None

    def refresh(self):
        stamp = self.path.stat() if self.path.exists() else None
        current = (stamp.st_mtime_ns, stamp.st_size) if stamp else None
        if current != self._stamp:
            with locked_jsonl(self.path):
                self._read_locked()

    def active(self, scope):
        return list(self._active.get(tuple(scope), ()))

    def latest(self, scope, key, binding=None):
        entry = self._latest.get((tuple(scope), key))
        return entry if entry is not None and (binding is None or entry["binding"] == binding) else None

    def _append_locked(self, entry):
        append_jsonl_records(self.path, [json.dumps(entry, ensure_ascii=False).encode("utf-8")],
                             description="arrival reviews", already_locked=True)
        self._entries.append(entry)
        self._reindex()
        self._update_stamp()
        return entry

    def save(self, scope, key, binding, action, **values):
        if action not in ACTIONS:
            raise ValueError("无效过线审核动作")
        frame = dict(values.get("frame") or {})
        if action == "confirm":
            if (not values.get("event_id") or not values.get("bib")
                    or not frame.get("segment_id") or not frame.get("video_path")
                    or frame.get("frame_index", -1) < 0 or frame.get("position_ms", -1) < 0
                    or frame.get("recorder_ms") is None or values.get("crossing_ms") is None):
                raise ValueError("确认过线需要明确事件、号码和正在显示的原帧")
        with locked_jsonl(self.path):
            self._read_locked()
            previous = self.latest(scope, key, binding)
            if (previous is not None and previous["action"] == action
                    and (action != "confirm" or
                         previous.get("candidate_binding") == values.get("candidate_binding"))):
                return previous
            entry = dict(values, id=uuid.uuid4().hex, scope=list(scope), key=key,
                         binding=binding, action=action, saved_at_ms=int(time.time() * 1000))
            return self._append_locked(entry)

    def undo(self, scope, *, key=None):
        with locked_jsonl(self.path):
            self._read_locked()
            active = self.active(scope)
            if not active:
                return None
            subject = [entry for entry in active if entry["key"] == key] if key is not None else active[-1:]
            if not subject:
                return None
            previous = subject[-1]
            operation = previous.get("operation_id")
            if key is None:
                targets = ([entry for entry in active if entry.get("operation_id") == operation]
                           if operation else [previous])
            else:
                operations = {entry["operation_id"] for entry in subject if entry.get("operation_id")}
                targets = [entry for entry in active if entry["key"] == key or
                           (entry["action"] != "confirm" and entry.get("operation_id") in operations)]
            target_ids = {entry["id"] for entry in targets}
            shared_candidates = {entry.get("candidate_id") for entry in active
                                 if entry["action"] == "confirm" and entry["id"] not in target_ids}
            targets = [entry for entry in targets if not
                       (entry["action"] == "bicycle" and entry.get("candidate_id")
                        and entry["candidate_id"] in shared_candidates)]
            records = [dict(id=uuid.uuid4().hex, scope=list(scope), action="undo",
                            target=entry["id"], saved_at_ms=int(time.time() * 1000)) for entry in targets]
            append_jsonl_records(self.path, [json.dumps(entry, ensure_ascii=False).encode("utf-8")
                                            for entry in records], description="arrival reviews",
                                 already_locked=True)
            self._entries.extend(records)
            self._reindex()
            self._update_stamp()
            return previous
