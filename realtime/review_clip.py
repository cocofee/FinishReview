"""Durable shared review clips and event-to-clip bindings."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1


class ReviewClipError(RuntimeError):
    """Raised when the review clip journal cannot be read or updated."""


@dataclass(frozen=True, slots=True)
class ReviewClip:
    clip_id: str
    race_id: str
    camera_index: int
    source_id: str
    started_at_ms: int
    ended_at_ms: int
    playlist_path: str
    segment_signature: str
    timeline_segment_id: str
    state: str = "sealed"

    def __post_init__(self) -> None:
        if not self.clip_id.strip():
            raise ReviewClipError("clip_id is required")
        if self.camera_index <= 0:
            raise ReviewClipError("camera_index must be positive")
        if not self.source_id.strip():
            raise ReviewClipError("source_id is required")
        if self.started_at_ms < 0 or self.ended_at_ms < self.started_at_ms:
            raise ReviewClipError("invalid clip time range")
        if not self.playlist_path.strip():
            raise ReviewClipError("playlist_path is required")
        if not self.segment_signature.strip():
            raise ReviewClipError("segment_signature is required")
        if not self.timeline_segment_id.strip():
            raise ReviewClipError("timeline_segment_id is required")


@dataclass(frozen=True, slots=True)
class PassageReviewBinding:
    event_id: str
    revision: int
    camera_index: int
    clip_id: str
    passage_timestamp_ms: int
    passage_offset_ms: int
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ReviewClipError("event_id is required")
        if self.revision <= 0:
            raise ReviewClipError("revision must be positive")
        if self.camera_index <= 0:
            raise ReviewClipError("camera_index must be positive")
        if not self.clip_id.strip():
            raise ReviewClipError("clip_id is required")
        if self.passage_timestamp_ms < 0 or self.passage_offset_ms < 0:
            raise ReviewClipError("binding timestamps must be non-negative")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewClipError(f"{name} must be an integer")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ReviewClipError(f"{name} must be a string")
    return value


class PassageReviewBindingStore:
    """Append-only clip and binding journal scoped to one event workspace."""

    def __init__(
        self,
        journal_path: str | Path,
        *,
        recover_incomplete_tail: bool = True,
    ):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._clips: dict[str, ReviewClip] = {}
        self._clip_ids_by_signature: dict[tuple[str, int, str, str], str] = {}
        self._bindings: dict[tuple[str, int, int], PassageReviewBinding] = {}
        self._active_bindings: dict[tuple[str, int], PassageReviewBinding] = {}
        self._active_camera_indexes_by_event: dict[str, set[int]] = {}
        self._revision = 0
        self._recovered_incomplete_tail = False
        self._recover_incomplete_tail = bool(recover_incomplete_tail)
        self._load_existing()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @staticmethod
    def _clip_key(
        race_id: str,
        camera_index: int,
        source_id: str,
        segment_signature: str,
    ) -> tuple[str, int, str, str]:
        return (
            str(race_id),
            int(camera_index),
            str(source_id),
            str(segment_signature),
        )

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise ReviewClipError(
                f"failed to read review clip journal: {self.journal_path}"
            ) from error
        offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith(b"\n") or raw_line.endswith(b"\r")
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                offset += len(raw_line)
                continue
            try:
                payload = json.loads(stripped.decode("utf-8"))
                self._merge_record(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ReviewClipError) as error:
                if line_number == len(lines) and not terminated:
                    if self._recover_incomplete_tail:
                        self._truncate(offset)
                    self._recovered_incomplete_tail = True
                    return
                raise ReviewClipError(
                    f"invalid review clip journal line {line_number}: {error}"
                ) from error
            offset += len(raw_line)

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(int(size))
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise ReviewClipError(
                f"failed to recover review clip journal: {self.journal_path}"
            ) from error

    def _merge_record(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise ReviewClipError("review clip record must be an object")
        if _integer(payload.get("schema_version"), "schema_version") != SCHEMA_VERSION:
            raise ReviewClipError("unsupported review clip schema_version")
        record_type = _string(payload.get("record_type"), "record_type")
        if record_type == "clip_added":
            clip = ReviewClip(
                clip_id=_string(payload.get("clip_id"), "clip_id"),
                race_id=_string(payload.get("race_id", ""), "race_id"),
                camera_index=_integer(payload.get("camera_index"), "camera_index"),
                source_id=_string(payload.get("source_id"), "source_id"),
                started_at_ms=_integer(payload.get("started_at_ms"), "started_at_ms"),
                ended_at_ms=_integer(payload.get("ended_at_ms"), "ended_at_ms"),
                playlist_path=_string(payload.get("playlist_path"), "playlist_path"),
                segment_signature=_string(
                    payload.get("segment_signature"),
                    "segment_signature",
                ),
                timeline_segment_id=_string(
                    payload.get("timeline_segment_id"),
                    "timeline_segment_id",
                ),
                state=_string(payload.get("state", "sealed"), "state"),
            )
            if clip.clip_id in self._clips:
                raise ReviewClipError(f"duplicate clip_id: {clip.clip_id}")
            key = self._clip_key(
                clip.race_id,
                clip.camera_index,
                clip.source_id,
                clip.segment_signature,
            )
            if key in self._clip_ids_by_signature:
                raise ReviewClipError("duplicate review clip signature")
            self._clips[clip.clip_id] = clip
            self._clip_ids_by_signature[key] = clip.clip_id
        elif record_type == "binding_added":
            binding = PassageReviewBinding(
                event_id=_string(payload.get("event_id"), "event_id"),
                revision=_integer(payload.get("revision"), "revision"),
                camera_index=_integer(payload.get("camera_index"), "camera_index"),
                clip_id=_string(payload.get("clip_id"), "clip_id"),
                passage_timestamp_ms=_integer(
                    payload.get("passage_timestamp_ms"),
                    "passage_timestamp_ms",
                ),
                passage_offset_ms=_integer(
                    payload.get("passage_offset_ms"),
                    "passage_offset_ms",
                ),
            )
            if binding.clip_id not in self._clips:
                raise ReviewClipError(f"unknown clip_id: {binding.clip_id}")
            key = (binding.event_id, binding.revision, binding.camera_index)
            if key in self._bindings:
                raise ReviewClipError("duplicate passage review binding")
            self._bindings[key] = binding
            active_key = (binding.event_id, binding.camera_index)
            current = self._active_bindings.get(active_key)
            if current is None or binding.revision >= current.revision:
                if current is not None:
                    self._bindings[
                        (current.event_id, current.revision, current.camera_index)
                    ] = replace(current, is_active=False)
                self._active_bindings[active_key] = binding
                self._active_camera_indexes_by_event.setdefault(
                    binding.event_id,
                    set(),
                ).add(binding.camera_index)
        elif record_type == "binding_deactivated":
            event_id = _string(payload.get("event_id"), "event_id")
            revision = _integer(payload.get("revision"), "revision")
            camera_indexes = tuple(
                self._active_camera_indexes_by_event.get(event_id, ())
            )
            for camera_index in camera_indexes:
                active_key = (event_id, camera_index)
                binding = self._active_bindings[active_key]
                if binding.revision > revision:
                    continue
                self._bindings[
                    (binding.event_id, binding.revision, binding.camera_index)
                ] = replace(binding, is_active=False)
                self._active_bindings.pop(active_key, None)
                active_cameras = self._active_camera_indexes_by_event.get(event_id)
                if active_cameras is not None:
                    active_cameras.discard(binding.camera_index)
                    if not active_cameras:
                        self._active_camera_indexes_by_event.pop(event_id, None)
        else:
            raise ReviewClipError(f"unsupported review clip record_type: {record_type}")
        self._revision += 1

    def _append_records(self, payloads: tuple[Mapping[str, Any], ...]) -> None:
        if not payloads:
            return
        data = b"".join(
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for payload in payloads
        )
        try:
            with self.journal_path.open("ab") as journal:
                journal.write(data)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise ReviewClipError(
                f"failed to append review clip journal: {self.journal_path}"
            ) from error

    def _append_record(self, payload: Mapping[str, Any]) -> None:
        self._append_records((payload,))

    def get_clip(self, clip_id: str) -> ReviewClip | None:
        with self._lock:
            return self._clips.get(str(clip_id))

    def _portable_path(self, path: str | Path) -> str:
        resolved = Path(path).expanduser().absolute()
        try:
            return resolved.relative_to(self.journal_path.parent).as_posix()
        except ValueError:
            return str(resolved)

    def resolve_playlist_path(self, clip: ReviewClip) -> Path:
        path = Path(clip.playlist_path)
        if path.is_absolute():
            return path
        return (self.journal_path.parent / path).absolute()

    def get_or_add_clip(
        self,
        *,
        race_id: str,
        camera_index: int,
        source_id: str,
        started_at_ms: int,
        ended_at_ms: int,
        playlist_path: str | Path,
        segment_signature: str,
        timeline_segment_id: str,
    ) -> ReviewClip:
        key = self._clip_key(race_id, camera_index, source_id, segment_signature)
        with self._lock:
            existing_id = self._clip_ids_by_signature.get(key)
            if existing_id is not None:
                return self._clips[existing_id]
            clip = ReviewClip(
                clip_id=f"clip-{uuid.uuid4().hex}",
                race_id=str(race_id),
                camera_index=int(camera_index),
                source_id=str(source_id),
                started_at_ms=int(started_at_ms),
                ended_at_ms=int(ended_at_ms),
                playlist_path=self._portable_path(playlist_path),
                segment_signature=str(segment_signature),
                timeline_segment_id=str(timeline_segment_id),
            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "clip_added",
                "clip_id": clip.clip_id,
                "race_id": clip.race_id,
                "camera_index": clip.camera_index,
                "source_id": clip.source_id,
                "started_at_ms": clip.started_at_ms,
                "ended_at_ms": clip.ended_at_ms,
                "playlist_path": clip.playlist_path,
                "segment_signature": clip.segment_signature,
                "timeline_segment_id": clip.timeline_segment_id,
                "state": clip.state,
            }
            self._append_record(payload)
            self._merge_record(payload)
            return clip

    def bind(
        self,
        *,
        event_id: str,
        revision: int,
        camera_index: int,
        clip_id: str,
        passage_timestamp_ms: int,
        passage_offset_ms: int,
    ) -> PassageReviewBinding:
        return self.bind_many(
            (
                PassageReviewBinding(
                    event_id=str(event_id),
                    revision=int(revision),
                    camera_index=int(camera_index),
                    clip_id=str(clip_id),
                    passage_timestamp_ms=int(passage_timestamp_ms),
                    passage_offset_ms=int(passage_offset_ms),
                ),
            )
        )[0]

    def bind_many(
        self,
        bindings: tuple[PassageReviewBinding, ...],
    ) -> tuple[PassageReviewBinding, ...]:
        requested_by_key = {
            (binding.event_id, binding.revision, binding.camera_index): binding
            for binding in bindings
        }
        if len(requested_by_key) != len(bindings):
            raise ReviewClipError("duplicate binding in one batch")
        with self._lock:
            payloads = []
            for key, requested in requested_by_key.items():
                existing = self._bindings.get(key)
                if existing is not None:
                    if replace(existing, is_active=True) != requested:
                        raise ReviewClipError(
                            "passage review binding conflicts with journal"
                        )
                    continue
                if requested.clip_id not in self._clips:
                    raise ReviewClipError(f"unknown clip_id: {requested.clip_id}")
                payloads.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "record_type": "binding_added",
                        "event_id": requested.event_id,
                        "revision": requested.revision,
                        "camera_index": requested.camera_index,
                        "clip_id": requested.clip_id,
                        "passage_timestamp_ms": requested.passage_timestamp_ms,
                        "passage_offset_ms": requested.passage_offset_ms,
                    }
                )
            self._append_records(tuple(payloads))
            for payload in payloads:
                self._merge_record(payload)
            return tuple(
                self._bindings[key]
                for key in requested_by_key
            )

    def clips(self) -> tuple[ReviewClip, ...]:
        with self._lock:
            return tuple(
                self._clips[clip_id]
                for clip_id in sorted(self._clips)
            )

    def bindings(self, *, active_only: bool = True) -> tuple[PassageReviewBinding, ...]:
        with self._lock:
            source = (
                self._active_bindings.values()
                if active_only
                else self._bindings.values()
            )
            return tuple(
                sorted(
                    source,
                    key=lambda binding: (
                        binding.event_id,
                        binding.camera_index,
                        binding.revision,
                    ),
                )
            )

    def active_bindings(
        self,
        event_id: str,
        revision: int,
    ) -> tuple[PassageReviewBinding, ...]:
        with self._lock:
            camera_indexes = self._active_camera_indexes_by_event.get(
                str(event_id),
                (),
            )
            return tuple(
                sorted(
                    (
                        self._active_bindings[(str(event_id), camera_index)]
                        for camera_index in camera_indexes
                        if self._active_bindings[
                            (str(event_id), camera_index)
                        ].revision
                        == int(revision)
                    ),
                    key=lambda binding: binding.camera_index,
                )
            )

    def deactivate(self, event_id: str, revision: int) -> None:
        event_id = str(event_id).strip()
        revision = int(revision)
        if not event_id or revision <= 0:
            return
        with self._lock:
            camera_indexes = self._active_camera_indexes_by_event.get(event_id, ())
            if not any(
                self._active_bindings[(event_id, camera_index)].revision <= revision
                for camera_index in camera_indexes
            ):
                return
            payload = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "binding_deactivated",
                "event_id": event_id,
                "revision": revision,
            }
            self._append_record(payload)
            self._merge_record(payload)


__all__ = [
    "PassageReviewBinding",
    "PassageReviewBindingStore",
    "ReviewClip",
    "ReviewClipError",
]
