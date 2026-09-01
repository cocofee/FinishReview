"""Durable operator records for bibs seen in video but absent from chips."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
_VALID_STATUSES = {"pending_manual_entry", "resolved", "ignored"}


class VideoDiscoveryError(RuntimeError):
    """Raised when the video-discovery journal is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class VideoDiscoveryRecord:
    discovery_id: str
    race_id: str
    stage_id: str
    batch_id: str
    bib: str
    camera_index: int
    frame_index: int
    position_ms: int
    started_at_ms: int
    ended_at_ms: int
    status: str = "pending_manual_entry"
    created_at_ms: int = 0
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.discovery_id.strip():
            raise VideoDiscoveryError("discovery_id is required")
        if not self.race_id.strip() or not self.stage_id.strip():
            raise VideoDiscoveryError("race_id and stage_id are required")
        if not self.batch_id.strip() or not self.bib.strip():
            raise VideoDiscoveryError("batch_id and bib are required")
        if self.camera_index <= 0:
            raise VideoDiscoveryError("camera_index must be positive")
        if self.frame_index < -1 or self.position_ms < 0:
            raise VideoDiscoveryError("invalid video position")
        if self.started_at_ms < 0 or self.ended_at_ms < self.started_at_ms:
            raise VideoDiscoveryError("invalid batch time range")
        if self.status not in _VALID_STATUSES:
            raise VideoDiscoveryError("unsupported discovery status")
        if self.created_at_ms < 0 or self.revision <= 0:
            raise VideoDiscoveryError("invalid discovery metadata")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["record_type"] = "video_discovery"
        return payload


class VideoDiscoveryStore:
    """Append-only journal of video-only bib discoveries."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._latest: dict[str, VideoDiscoveryRecord] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise VideoDiscoveryError(
                f"failed to read video discovery journal: {self.journal_path}"
            ) from error
        lines = content.splitlines(keepends=True)
        offset = 0
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith((b"\n", b"\r"))
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                offset += len(raw_line)
                continue
            try:
                payload = json.loads(stripped.decode("utf-8"))
                self._merge(payload)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                AttributeError,
                VideoDiscoveryError,
            ) as error:
                if line_number == len(lines) and not terminated:
                    self._truncate(offset)
                    return
                raise VideoDiscoveryError(
                    f"invalid video discovery journal line {line_number}: {error}"
                ) from error
            offset += len(raw_line)

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(int(size))
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise VideoDiscoveryError(
                f"failed to recover video discovery journal: {self.journal_path}"
            ) from error

    @staticmethod
    def _record_from_payload(payload: Mapping[str, Any]) -> VideoDiscoveryRecord:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise VideoDiscoveryError("unsupported video discovery schema_version")
        if payload.get("record_type") != "video_discovery":
            raise VideoDiscoveryError("unsupported video discovery record_type")
        try:
            fields = {
                name: payload[name]
                for name in (
                    "discovery_id",
                    "race_id",
                    "stage_id",
                    "batch_id",
                    "bib",
                    "camera_index",
                    "frame_index",
                    "position_ms",
                    "started_at_ms",
                    "ended_at_ms",
                    "status",
                    "created_at_ms",
                    "revision",
                )
            }
            return VideoDiscoveryRecord(**fields)
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise VideoDiscoveryError("invalid video discovery record") from error

    def _merge(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise VideoDiscoveryError("video discovery record must be an object")
        record = self._record_from_payload(payload)
        current = self._latest.get(record.discovery_id)
        if current is not None and record.revision < current.revision:
            return
        self._latest[record.discovery_id] = record

    def _append(self, record: VideoDiscoveryRecord) -> None:
        payload = json.dumps(
            record.to_payload(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        original_size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        try:
            with self.journal_path.open("ab") as journal:
                journal.write(payload)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            try:
                with self.journal_path.open("r+b") as journal:
                    journal.truncate(original_size)
            except OSError:
                pass
            raise VideoDiscoveryError(
                f"failed to append video discovery journal: {self.journal_path}"
            ) from error
        self._latest[record.discovery_id] = record

    def add(
        self,
        *,
        discovery_id: str,
        race_id: str,
        stage_id: str,
        batch_id: str,
        bib: str,
        camera_index: int,
        frame_index: int,
        position_ms: int,
        started_at_ms: int,
        ended_at_ms: int,
    ) -> VideoDiscoveryRecord:
        with self._lock:
            record = VideoDiscoveryRecord(
                discovery_id=str(discovery_id),
                race_id=str(race_id),
                stage_id=str(stage_id),
                batch_id=str(batch_id),
                bib=str(bib),
                camera_index=int(camera_index),
                frame_index=int(frame_index),
                position_ms=int(position_ms),
                started_at_ms=int(started_at_ms),
                ended_at_ms=int(ended_at_ms),
                created_at_ms=int(time.time() * 1000),
            )
            if record.discovery_id in self._latest:
                raise VideoDiscoveryError("duplicate discovery_id")
            self._append(record)
            return record

    def update_batch(self, discovery_id: str, batch_id: str) -> VideoDiscoveryRecord | None:
        with self._lock:
            current = self._latest.get(str(discovery_id))
            if current is None:
                return None
            record = replace(
                current,
                batch_id=str(batch_id),
                revision=current.revision + 1,
            )
            self._append(record)
            return record

    def update_status(self, discovery_id: str, status: str) -> VideoDiscoveryRecord | None:
        with self._lock:
            current = self._latest.get(str(discovery_id))
            if current is None:
                return None
            record = replace(
                current,
                status=str(status),
                revision=current.revision + 1,
            )
            self._append(record)
            return record

    def records(self) -> tuple[VideoDiscoveryRecord, ...]:
        with self._lock:
            return tuple(self._latest.values())


__all__ = ["VideoDiscoveryError", "VideoDiscoveryRecord", "VideoDiscoveryStore"]
