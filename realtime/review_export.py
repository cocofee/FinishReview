"""Human-readable finish review snapshots for an event workspace."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from .passage_evidence import (
        HIGH_SPEED_SOURCE,
        REGULAR_SOURCE,
        PassageEvidenceAssociationStore,
    )
    from .passage_receiver import PassageEvent
    from .race_metadata import RaceAthleteMetadata, RaceMetadata
except ImportError:
    from passage_evidence import (
        HIGH_SPEED_SOURCE,
        REGULAR_SOURCE,
        PassageEvidenceAssociationStore,
    )
    from passage_receiver import PassageEvent
    from race_metadata import RaceAthleteMetadata, RaceMetadata


REVIEW_SUMMARY_FILENAME = "终点复核清单.csv"
REVIEW_SUMMARY_HEADERS = (
    "序号",
    "运动员编号",
    "姓名",
    "组别",
    "通过时间",
    "普通录像",
    "高速录像",
    "复核状态",
    "最后确认时间",
)
BEIJING_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class ReviewSummaryRow:
    sequence: int
    bib: str
    athlete_name: str
    group_name: str
    passage_time: str
    regular_status: str
    high_speed_status: str
    review_status: str
    last_confirmation_time: str

    def as_csv_row(self) -> tuple[object, ...]:
        return (
            self.sequence,
            self.bib,
            self.athlete_name,
            self.group_name,
            self.passage_time,
            self.regular_status,
            self.high_speed_status,
            self.review_status,
            self.last_confirmation_time,
        )


def _format_beijing_time(timestamp_ms: int, *, include_date: bool) -> str:
    try:
        value = datetime.fromtimestamp(
            int(timestamp_ms) / 1000.0,
            tz=BEIJING_TIMEZONE,
        )
    except (OSError, OverflowError, ValueError):
        return f"{int(timestamp_ms)} ms"
    pattern = "%Y-%m-%d %H:%M:%S.%f" if include_date else "%H:%M:%S.%f"
    return value.strftime(pattern)[:-3]


def _metadata_indexes(
    metadata: Optional[RaceMetadata],
) -> tuple[
    dict[str, RaceAthleteMetadata],
    dict[str, RaceAthleteMetadata],
    dict[str, str],
]:
    athletes_by_id: dict[str, RaceAthleteMetadata] = {}
    athletes_by_identity: dict[str, RaceAthleteMetadata] = {}
    group_names: dict[str, str] = {}
    if metadata is None:
        return athletes_by_id, athletes_by_identity, group_names

    group_names = {group.group_id: group.name for group in metadata.groups}
    for athlete in metadata.athletes:
        athletes_by_id[athlete.athlete_id] = athlete
        for identity in (athlete.bib, *athlete.chip_ids):
            key = identity.strip().casefold()
            if key:
                athletes_by_identity.setdefault(key, athlete)
    return athletes_by_id, athletes_by_identity, group_names


def _metadata_athlete(
    event: PassageEvent,
    athletes_by_id: dict[str, RaceAthleteMetadata],
    athletes_by_identity: dict[str, RaceAthleteMetadata],
) -> Optional[RaceAthleteMetadata]:
    if event.athlete_id:
        athlete = athletes_by_id.get(event.athlete_id)
        if athlete is not None:
            return athlete
    for identity in (event.bib, event.chip_id):
        athlete = athletes_by_identity.get(identity.strip().casefold())
        if athlete is not None:
            return athlete
    return None


def build_review_summary_rows(
    events: Iterable[PassageEvent],
    association_store: PassageEvidenceAssociationStore,
    metadata: Optional[RaceMetadata] = None,
) -> tuple[ReviewSummaryRow, ...]:
    """Build rows from active passages and current, non-deleted confirmations."""

    athletes_by_id, athletes_by_identity, group_names = _metadata_indexes(metadata)
    active_events = (
        event
        for event in events
        if event.is_active
        and (
            metadata is None
            or (
                event.race_id == metadata.race_id
                and event.stage_id == metadata.stage_id
            )
        )
    )
    sorted_events = sorted(
        active_events,
        key=lambda event: (event.timeline_timestamp_ms, event.event_id),
    )

    rows: list[ReviewSummaryRow] = []
    for sequence, event in enumerate(sorted_events, start=1):
        athlete = _metadata_athlete(
            event,
            athletes_by_id,
            athletes_by_identity,
        )
        regular = association_store.get(event.event_id, REGULAR_SOURCE)
        high_speed = association_store.get(event.event_id, HIGH_SPEED_SOURCE)
        confirmations = tuple(
            association
            for association in (regular, high_speed)
            if association is not None
        )
        confirmed = bool(confirmations)
        last_confirmation_time = (
            _format_beijing_time(
                max(item.confirmed_at_ms for item in confirmations),
                include_date=True,
            )
            if confirmed
            else ""
        )
        rows.append(
            ReviewSummaryRow(
                sequence=sequence,
                bib=event.bib.strip()
                or (athlete.bib.strip() if athlete is not None else ""),
                athlete_name=event.athlete_name.strip()
                or (athlete.name.strip() if athlete is not None else ""),
                group_name=event.group_name.strip()
                or group_names.get(event.group_id, event.group_id),
                passage_time=_format_beijing_time(
                    event.timeline_timestamp_ms,
                    include_date=False,
                ),
                regular_status="已确认" if regular is not None else "未确认",
                high_speed_status="已确认" if high_speed is not None else "未确认",
                review_status="已确认" if confirmed else "未确认",
                last_confirmation_time=last_confirmation_time,
            )
        )
    return tuple(rows)


def export_review_summary(
    event_dir: str | Path,
    events: Iterable[PassageEvent],
    association_store: PassageEvidenceAssociationStore,
    metadata: Optional[RaceMetadata] = None,
) -> Path:
    """Atomically replace the event's UTF-8 BOM CSV snapshot."""

    output_dir = Path(event_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / REVIEW_SUMMARY_FILENAME
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    rows = build_review_summary_rows(events, association_store, metadata)
    try:
        with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REVIEW_SUMMARY_HEADERS)
            writer.writerows(row.as_csv_row() for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return output_path


__all__ = [
    "REVIEW_SUMMARY_FILENAME",
    "REVIEW_SUMMARY_HEADERS",
    "ReviewSummaryRow",
    "build_review_summary_rows",
    "export_review_summary",
]
