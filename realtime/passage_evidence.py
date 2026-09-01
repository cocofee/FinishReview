"""Durable manual associations between CycleRace passages and video frames."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1
CONFIRMED = "confirmed"
DELETED = "deleted"
REGULAR_SOURCE = "regular"
HIGH_SPEED_SOURCE = "high_speed"
_SOURCES = {REGULAR_SOURCE, HIGH_SPEED_SOURCE}
_STATUSES = {CONFIRMED, DELETED}


def _looks_like_incomplete_json(value: str) -> bool:
    in_string = False
    escaped = False
    nesting = 0
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            nesting += 1
        elif character in "]}":
            if nesting == 0:
                return False
            nesting -= 1
    return in_string or nesting > 0


class PassageEvidenceError(RuntimeError):
    """Raised when the evidence-association journal is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class VideoClockCalibration:
    """One camera/session offset calibrated from a known passage."""

    camera_index: int
    session_key: str
    offset_ms: int
    anchor_event_id: str
    anchor_bib: str
    calibrated_at_ms: int
    revision: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported video clock calibration schema_version")
        if self.camera_index <= 0:
            raise ValueError("camera_index must be positive")
        if not self.session_key.strip():
            raise ValueError("session_key is required")
        if not self.anchor_event_id.strip():
            raise ValueError("anchor_event_id is required")
        if not self.anchor_bib.strip():
            raise ValueError("anchor_bib is required")
        if self.calibrated_at_ms < 0:
            raise ValueError("calibrated_at_ms must be non-negative")
        if self.revision <= 0:
            raise ValueError("revision must be positive")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "VideoClockCalibration":
        if not isinstance(payload, Mapping):
            raise ValueError("video clock calibration must be a JSON object")
        return cls(
            schema_version=int(payload.get("schema_version", 0)),
            camera_index=int(payload.get("camera_index", 0)),
            session_key=str(payload.get("session_key", "")),
            offset_ms=int(payload.get("offset_ms", 0)),
            anchor_event_id=str(payload.get("anchor_event_id", "")),
            anchor_bib=str(payload.get("anchor_bib", "")),
            calibrated_at_ms=int(payload.get("calibrated_at_ms", -1)),
            revision=int(payload.get("revision", 0)),
        )

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class VideoClockCalibrationStore:
    """Append-only latest-revision store for per-camera video offsets."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._latest: dict[tuple[int, str], VideoClockCalibration] = {}
        self._recovered_incomplete_tail = False
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise PassageEvidenceError(
                f"failed to read video clock calibration journal: {self.journal_path}"
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
                text = stripped.decode("utf-8")
                calibration = VideoClockCalibration.from_payload(json.loads(text))
            except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
                is_tail = line_number == len(lines) and not terminated
                if is_tail:
                    try:
                        candidate = stripped.decode("utf-8")
                    except UnicodeDecodeError:
                        candidate = ""
                    if candidate and _looks_like_incomplete_json(candidate):
                        self._truncate(offset)
                        self._recovered_incomplete_tail = True
                        return
                raise PassageEvidenceError(
                    f"invalid video clock calibration journal line {line_number}: {error}"
                ) from error
            key = (calibration.camera_index, calibration.session_key)
            current = self._latest.get(key)
            if current is None or calibration.revision > current.revision:
                self._latest[key] = calibration
            elif calibration.revision == current.revision and calibration != current:
                raise PassageEvidenceError(
                    f"conflicting video clock calibration revision at line {line_number}"
                )
            offset += len(raw_line)

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(size)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise PassageEvidenceError(
                f"failed to recover video clock calibration journal: {self.journal_path}"
            ) from error

    def _append(self, calibration: VideoClockCalibration) -> None:
        record = (
            json.dumps(
                calibration.to_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        original_size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        separator = b""
        if original_size:
            try:
                with self.journal_path.open("rb") as journal:
                    journal.seek(-1, os.SEEK_END)
                    if journal.read(1) not in {b"\n", b"\r"}:
                        separator = b"\n"
            except OSError as error:
                raise PassageEvidenceError(
                    f"failed to inspect video clock calibration journal: {self.journal_path}"
                ) from error
        try:
            with self.journal_path.open("ab") as journal:
                journal.write(separator)
                journal.write(record)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            try:
                with self.journal_path.open("r+b") as journal:
                    journal.truncate(original_size)
            except OSError:
                pass
            raise PassageEvidenceError(
                f"failed to append video clock calibration journal: {self.journal_path}"
            ) from error
        self._latest[(calibration.camera_index, calibration.session_key)] = calibration

    def record(
        self,
        *,
        camera_index: int,
        session_key: str,
        offset_ms: int,
        anchor_event_id: str,
        anchor_bib: str,
        calibrated_at_ms: int,
    ) -> VideoClockCalibration:
        with self._lock:
            key = (int(camera_index), str(session_key))
            current = self._latest.get(key)
            calibration = VideoClockCalibration(
                camera_index=key[0],
                session_key=key[1],
                offset_ms=int(offset_ms),
                anchor_event_id=str(anchor_event_id),
                anchor_bib=str(anchor_bib),
                calibrated_at_ms=int(calibrated_at_ms),
                revision=1 if current is None else current.revision + 1,
            )
            self._append(calibration)
            return calibration

    def get(
        self,
        camera_index: int,
        session_key: str,
    ) -> Optional[VideoClockCalibration]:
        with self._lock:
            return self._latest.get((int(camera_index), str(session_key)))

    def calibrations(self) -> tuple[VideoClockCalibration, ...]:
        with self._lock:
            return tuple(
                calibration
                for _key, calibration in sorted(self._latest.items())
            )


@dataclass(frozen=True, slots=True)
class PassageEvidenceAssociation:
    """One judge-confirmed identity marker in an evidence source."""

    passage_event_id: str
    bib: str
    confirmed_source: str
    segment_id: str
    frame_index: int
    position_ms: int
    marker_x_normalized: float
    marker_y_normalized: float
    confirmed_at_ms: int
    confirmation_status: str = CONFIRMED
    revision: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported evidence association schema_version")
        if not self.passage_event_id.strip():
            raise ValueError("passage_event_id is required")
        if not self.bib.strip():
            raise ValueError("bib is required")
        if self.confirmed_source not in _SOURCES:
            raise ValueError("confirmed_source must be regular or high_speed")
        if not self.segment_id.strip():
            raise ValueError("segment_id is required")
        if self.frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if self.position_ms < 0:
            raise ValueError("position_ms must be non-negative")
        if not 0.0 <= self.marker_x_normalized <= 1.0:
            raise ValueError("marker_x_normalized must be between 0 and 1")
        if not 0.0 <= self.marker_y_normalized <= 1.0:
            raise ValueError("marker_y_normalized must be between 0 and 1")
        if self.confirmed_at_ms < 0:
            raise ValueError("confirmed_at_ms must be non-negative")
        if self.confirmation_status not in _STATUSES:
            raise ValueError("confirmation_status must be confirmed or deleted")
        if self.revision <= 0:
            raise ValueError("revision must be positive")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PassageEvidenceAssociation":
        if not isinstance(payload, Mapping):
            raise ValueError("evidence association must be a JSON object")
        return cls(
            schema_version=int(payload.get("schema_version", 0)),
            passage_event_id=str(payload.get("passage_event_id", "")),
            bib=str(payload.get("bib", "")),
            confirmed_source=str(payload.get("confirmed_source", "")),
            segment_id=str(payload.get("segment_id", "")),
            frame_index=int(payload.get("frame_index", -1)),
            position_ms=int(payload.get("position_ms", -1)),
            marker_x_normalized=float(payload.get("marker_x_normalized", -1.0)),
            marker_y_normalized=float(payload.get("marker_y_normalized", -1.0)),
            confirmed_at_ms=int(payload.get("confirmed_at_ms", -1)),
            confirmation_status=str(payload.get("confirmation_status", "")),
            revision=int(payload.get("revision", 0)),
        )

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class PassageEvidenceAssociationStore:
    """Append-only latest-revision store, separate from official passage data."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._latest: dict[tuple[str, str], PassageEvidenceAssociation] = {}
        self._recovered_incomplete_tail = False
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise PassageEvidenceError(
                f"failed to read evidence association journal: {self.journal_path}"
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
                text = stripped.decode("utf-8")
                association = PassageEvidenceAssociation.from_payload(json.loads(text))
            except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
                is_tail = line_number == len(lines) and not terminated
                if is_tail:
                    try:
                        candidate = stripped.decode("utf-8")
                    except UnicodeDecodeError:
                        candidate = ""
                    if candidate and _looks_like_incomplete_json(candidate):
                        self._truncate(offset)
                        self._recovered_incomplete_tail = True
                        return
                raise PassageEvidenceError(
                    f"invalid evidence association journal line {line_number}: {error}"
                ) from error
            self._merge_loaded(association, line_number)
            offset += len(raw_line)

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(size)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise PassageEvidenceError(
                f"failed to recover evidence association journal: {self.journal_path}"
            ) from error

    def _merge_loaded(
        self,
        association: PassageEvidenceAssociation,
        line_number: int,
    ) -> None:
        key = (association.passage_event_id, association.confirmed_source)
        current = self._latest.get(key)
        if current is None or association.revision > current.revision:
            self._latest[key] = association
            return
        if association.revision == current.revision and association != current:
            raise PassageEvidenceError(
                f"conflicting evidence association revision at line {line_number}"
            )

    def _append(self, association: PassageEvidenceAssociation) -> None:
        record = (
            json.dumps(
                association.to_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        original_size = (
            self.journal_path.stat().st_size if self.journal_path.exists() else 0
        )
        separator = b""
        if original_size:
            try:
                with self.journal_path.open("rb") as journal:
                    journal.seek(-1, os.SEEK_END)
                    if journal.read(1) not in {b"\n", b"\r"}:
                        separator = b"\n"
            except OSError as error:
                raise PassageEvidenceError(
                    f"failed to inspect evidence association journal: {self.journal_path}"
                ) from error
        try:
            with self.journal_path.open("ab") as journal:
                journal.write(separator)
                journal.write(record)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            try:
                self._truncate(original_size)
            except PassageEvidenceError:
                pass
            raise PassageEvidenceError(
                f"failed to append evidence association journal: {self.journal_path}"
            ) from error
        self._latest[(association.passage_event_id, association.confirmed_source)] = association

    def confirm(
        self,
        *,
        passage_event_id: str,
        bib: str,
        confirmed_source: str,
        segment_id: str,
        frame_index: int,
        position_ms: int,
        marker_x_normalized: float,
        marker_y_normalized: float,
        confirmed_at_ms: int,
    ) -> PassageEvidenceAssociation:
        with self._lock:
            current = self._latest.get((str(passage_event_id), str(confirmed_source)))
            association = PassageEvidenceAssociation(
                passage_event_id=str(passage_event_id),
                bib=str(bib),
                confirmed_source=str(confirmed_source),
                segment_id=str(segment_id),
                frame_index=int(frame_index),
                position_ms=int(position_ms),
                marker_x_normalized=float(marker_x_normalized),
                marker_y_normalized=float(marker_y_normalized),
                confirmed_at_ms=int(confirmed_at_ms),
                revision=1 if current is None else current.revision + 1,
            )
            self._append(association)
            return association

    def clear(
        self,
        passage_event_id: str,
        confirmed_source: str,
        *,
        confirmed_at_ms: int,
    ) -> bool:
        with self._lock:
            key = (str(passage_event_id), str(confirmed_source))
            current = self._latest.get(key)
            if current is None or current.confirmation_status == DELETED:
                return False
            self._append(
                replace(
                    current,
                    confirmation_status=DELETED,
                    confirmed_at_ms=int(confirmed_at_ms),
                    revision=current.revision + 1,
                )
            )
            return True

    def get(
        self,
        passage_event_id: str,
        confirmed_source: str,
    ) -> Optional[PassageEvidenceAssociation]:
        with self._lock:
            association = self._latest.get(
                (str(passage_event_id), str(confirmed_source))
            )
            if association is None or association.confirmation_status == DELETED:
                return None
            return association

    def for_event(self, passage_event_id: str) -> tuple[PassageEvidenceAssociation, ...]:
        with self._lock:
            return tuple(
                association
                for source in (REGULAR_SOURCE, HIGH_SPEED_SOURCE)
                if (
                    association := self.get(str(passage_event_id), source)
                ) is not None
            )

    def associations(self) -> tuple[PassageEvidenceAssociation, ...]:
        """Return the current active associations in stable event/source order."""

        with self._lock:
            return tuple(
                association
                for _key, association in sorted(self._latest.items())
                if association.confirmation_status != DELETED
            )

    @property
    def recovered_incomplete_tail(self) -> bool:
        with self._lock:
            return self._recovered_incomplete_tail


__all__ = [
    "CONFIRMED",
    "DELETED",
    "HIGH_SPEED_SOURCE",
    "PassageEvidenceAssociation",
    "PassageEvidenceAssociationStore",
    "PassageEvidenceError",
    "REGULAR_SOURCE",
    "VideoClockCalibration",
    "VideoClockCalibrationStore",
]
