"""Durable CycleRace race metadata snapshots for the finish console."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1
MESSAGE_TYPE = "race_metadata"
ACK_MESSAGE_TYPE = "race_metadata_ack"


class RaceMetadataError(ValueError):
    """Raised when a race metadata payload is invalid."""


class RaceMetadataConflictError(RuntimeError):
    """Raised when a metadata revision is reused with different content."""


class RaceMetadataIngestResult(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RaceMetadataError(f"{name} is required")
    return value


def _optional_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name, "")
    if not isinstance(value, str):
        raise RaceMetadataError(f"{name} must be a string")
    return value


def _integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RaceMetadataError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class RaceGroupMetadata:
    group_id: str
    name: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RaceGroupMetadata":
        if not isinstance(payload, Mapping):
            raise RaceMetadataError("groups entries must be objects")
        return cls(
            group_id=_required_string(payload, "group_id"),
            name=_required_string(payload, "name"),
        )


@dataclass(frozen=True, slots=True)
class RaceAthleteMetadata:
    athlete_id: str
    bib: str
    name: str
    team_name: str
    group_id: str
    chip_ids: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RaceAthleteMetadata":
        if not isinstance(payload, Mapping):
            raise RaceMetadataError("athletes entries must be objects")
        chip_values = payload.get("chip_ids", [])
        if not isinstance(chip_values, list) or not all(
            isinstance(value, str) for value in chip_values
        ):
            raise RaceMetadataError("chip_ids must be a string array")
        bib = _optional_string(payload, "bib")
        chips = tuple(value.strip() for value in chip_values if value.strip())
        if not bib.strip() and not chips:
            raise RaceMetadataError("athlete bib or chip_ids is required")
        return cls(
            athlete_id=_required_string(payload, "athlete_id"),
            bib=bib,
            name=_optional_string(payload, "name"),
            team_name=_optional_string(payload, "team_name"),
            group_id=_required_string(payload, "group_id"),
            chip_ids=chips,
        )

    def matches_identity(self, value: str) -> bool:
        candidate = str(value).strip().casefold()
        return bool(candidate) and candidate in {
            self.bib.strip().casefold(),
            *(chip.casefold() for chip in self.chip_ids),
        }


@dataclass(frozen=True, slots=True)
class RaceMetadata:
    race_id: str
    stage_id: str
    revision: int
    emitted_at_ms: int
    race_name: str = ""
    stage_name: str = ""
    stage_date: str = ""
    groups: tuple[RaceGroupMetadata, ...] = ()
    athletes: tuple[RaceAthleteMetadata, ...] = ()
    schema_version: int = SCHEMA_VERSION
    message_type: str = MESSAGE_TYPE

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RaceMetadataError("unsupported race metadata schema_version")
        if self.message_type != MESSAGE_TYPE:
            raise RaceMetadataError("message_type must be race_metadata")
        if not self.race_id.strip() or not self.stage_id.strip():
            raise RaceMetadataError("race_id and stage_id are required")
        if self.revision <= 0:
            raise RaceMetadataError("revision must be positive")
        if self.emitted_at_ms < 0:
            raise RaceMetadataError("emitted_at_ms must be non-negative")
        group_ids = [group.group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise RaceMetadataError("group_id values must be unique")
        athlete_ids = [athlete.athlete_id for athlete in self.athletes]
        if len(athlete_ids) != len(set(athlete_ids)):
            raise RaceMetadataError("athlete_id values must be unique")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RaceMetadata":
        if not isinstance(payload, Mapping):
            raise RaceMetadataError("race metadata must be a JSON object")
        groups = payload.get("groups", [])
        athletes = payload.get("athletes", [])
        if not isinstance(groups, list):
            raise RaceMetadataError("groups must be an array")
        if not isinstance(athletes, list):
            raise RaceMetadataError("athletes must be an array")
        return cls(
            schema_version=_integer(payload, "schema_version"),
            message_type=_required_string(payload, "message_type"),
            race_id=_required_string(payload, "race_id"),
            stage_id=_required_string(payload, "stage_id"),
            revision=_integer(payload, "revision"),
            emitted_at_ms=_integer(payload, "emitted_at_ms"),
            race_name=_optional_string(payload, "race_name"),
            stage_name=_optional_string(payload, "stage_name"),
            stage_date=_optional_string(payload, "stage_date"),
            groups=tuple(RaceGroupMetadata.from_payload(item) for item in groups),
            athletes=tuple(
                RaceAthleteMetadata.from_payload(item) for item in athletes
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["groups"] = [asdict(group) for group in self.groups]
        payload["athletes"] = [
            {
                **asdict(athlete),
                "chip_ids": list(athlete.chip_ids),
            }
            for athlete in self.athletes
        ]
        return payload

    def group_label(self, group_id: str) -> str:
        for group in self.groups:
            if group.group_id == str(group_id):
                return group.name
        return str(group_id)


class RaceMetadataStore:
    """Atomic latest-snapshot store separate from passage evidence."""

    def __init__(self, snapshot_path: str | Path):
        self.snapshot_path = Path(snapshot_path).expanduser().absolute()
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current: Optional[RaceMetadata] = None
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.snapshot_path.exists():
            return
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            self._current = RaceMetadata.from_payload(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RaceMetadataError) as error:
            raise RaceMetadataError(
                f"invalid race metadata snapshot: {self.snapshot_path}: {error}"
            ) from error

    def store(self, metadata: RaceMetadata) -> RaceMetadataIngestResult:
        if not isinstance(metadata, RaceMetadata):
            raise TypeError("metadata must be RaceMetadata")
        with self._lock:
            current = self._current
            same_context = current is not None and (
                metadata.race_id,
                metadata.stage_id,
            ) == (
                current.race_id,
                current.stage_id,
            )
            if same_context and metadata.revision < current.revision:
                return RaceMetadataIngestResult.DUPLICATE
            if same_context and metadata.revision == current.revision:
                if metadata != current:
                    raise RaceMetadataConflictError(
                        "race metadata revision was reused with different content"
                    )
                return RaceMetadataIngestResult.DUPLICATE
            encoded = json.dumps(
                metadata.to_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            temporary = self.snapshot_path.with_suffix(
                self.snapshot_path.suffix + ".tmp"
            )
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.snapshot_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._current = metadata
            return RaceMetadataIngestResult.ACCEPTED

    def current(self) -> Optional[RaceMetadata]:
        with self._lock:
            return self._current


__all__ = [
    "ACK_MESSAGE_TYPE",
    "MESSAGE_TYPE",
    "RaceAthleteMetadata",
    "RaceGroupMetadata",
    "RaceMetadata",
    "RaceMetadataConflictError",
    "RaceMetadataError",
    "RaceMetadataIngestResult",
    "RaceMetadataStore",
]
