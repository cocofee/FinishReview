"""Chip reads as they stood during the race, rebuilt from the passage journal.

The passage store keeps only the latest revision of each event. Problem
detection needs the history too: a duplicate read that CycleRace voided later
must still be found and then shown as closed upstream. This module reads the
journal without modifying it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .passage_receiver import PassageEvent, PassageEventError
from .evidence_clock import align_evidence_timestamp


BEIJING_TIMEZONE = timezone(timedelta(hours=8))
LATE_MANUAL_MS = 60_000
TEST_GROUP_KEYWORDS = ("测试",)
SPARE_GROUP_KEYWORDS = ("备用",)

EXCLUDED_TEST = "test_group"
EXCLUDED_SPARE = "spare_group"
EXCLUDED_OTHER_DAY = "other_day"


@dataclass(frozen=True, slots=True)
class ChipRead:
    """One CycleRace passage, preserving its official timestamp."""

    event_id: str
    bib: str
    chip_id: str
    group_id: str
    group_name: str
    athlete_name: str
    chip_ms: int
    passage_time_ms: int
    lap: int
    received_at_ms: int
    revision: int
    latest_revision: int
    active_now: bool
    manual_entry: bool
    excluded: str = ""
    latest_bib: str = ""
    latest_chip_ms: int = 0
    replaced: bool = False

    @property
    def read_id(self) -> str:
        """Unique per read even when CycleRace reuses the passage id."""

        return f"{self.event_id}#{self.revision}"

    @property
    def receive_delay_ms(self) -> int:
        return (int(self.received_at_ms) - align_evidence_timestamp(self.chip_ms, self.received_at_ms)
                if self.received_at_ms else 0)

    @property
    def changed_upstream(self) -> bool:
        return self.active_now and (
            self.latest_bib != self.bib or self.latest_chip_ms != self.chip_ms
        )


def read_passage_history(journal_path: str | Path) -> tuple[PassageEvent, ...]:
    """Return every journal revision in file order; tolerates a torn tail."""

    path = Path(journal_path)
    if not path.is_file():
        return ()
    events = []
    lines = path.read_bytes().splitlines(keepends=True)
    for number, raw in enumerate(lines, start=1):
        stripped = raw.rstrip(b"\r\n")
        if not stripped:
            continue
        try:
            events.append(PassageEvent.from_payload(json.loads(stripped.decode("utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError, PassageEventError):
            if number == len(lines) and not raw.endswith((b"\n", b"\r")):
                break
            raise
    return tuple(events)


def local_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000.0, BEIJING_TIMEZONE).strftime("%Y-%m-%d")


def group_exclusion(group_name: str) -> str:
    name = str(group_name or "")
    if any(keyword in name for keyword in TEST_GROUP_KEYWORDS):
        return EXCLUDED_TEST
    if any(keyword in name for keyword in SPARE_GROUP_KEYWORDS):
        return EXCLUDED_SPARE
    return ""


def dominant_race_date(events: Iterable[PassageEvent]) -> str:
    counter = Counter(
        local_date(event.timeline_timestamp_ms)
        for event in events
        if event.is_active and event.timeline_timestamp_ms > 0
    )
    return counter.most_common(1)[0][0] if counter else ""


def chip_reads(
    history: Sequence[PassageEvent],
    *,
    race_id: str = "",
    stage_id: str = "",
    race_date: str = "",
    deleted_event_ids: Iterable[str] = (),
    late_manual_ms: int = LATE_MANUAL_MS,
) -> tuple[ChipRead, ...]:
    """Rebuild chip reads from all journal revisions.

    A read uses the first revision that was active, i.e. what the
    finish saw when the read arrived. ``active_now`` reflects the latest
    revision and local deletions. CycleRace does not send a manual-entry flag,
    so a passage received more than ``late_manual_ms`` after its evidence time
    is treated as a manual entry (补录), not as a chip read. Only an explicit
    ``race_date`` restricts dates; metadata dates never imply a filter.
    """

    scoped = [
        event for event in history
        if (not race_id or event.race_id == race_id)
        and (not stage_id or event.stage_id == stage_id)
    ]
    deleted = set(deleted_event_ids)
    by_event: dict[str, list[PassageEvent]] = {}
    for event in scoped:
        by_event.setdefault(event.event_id, []).append(event)
    reads = []
    for event_id, revisions in by_event.items():
        ordered = sorted(revisions, key=lambda item: (item.revision, item.received_at_ms))
        latest = ordered[-1]
        # CycleRace reuses a passage id for a different bib (e.g. test chips
        # replaced by a real read). Each bib/chip identity is its own read.
        versions: list[list[PassageEvent]] = []
        for item in ordered:
            identity = (item.bib.strip(), item.chip_id.strip())
            if (versions and (versions[-1][0].bib.strip(), versions[-1][0].chip_id.strip()) == identity
                    and local_date(versions[-1][0].timeline_timestamp_ms) == local_date(item.timeline_timestamp_ms)):
                versions[-1].append(item)
            else:
                versions.append([item])
        latest_identity = (latest.bib.strip(), latest.chip_id.strip())
        for version in versions:
            candidates = [
                item for item in version
                if item.is_active and item.timeline_timestamp_ms > 0
                and (not race_date or local_date(item.timeline_timestamp_ms) == race_date)
            ]
            if not candidates:
                continue
            first = candidates[0]
            current = version[-1] is latest
            chip_ms = int(first.timeline_timestamp_ms)
            received = int(first.received_at_ms or 0)
            reads.append(
                ChipRead(
                    event_id=event_id,
                    bib=first.bib.strip(),
                    chip_id=first.chip_id.strip(),
                    group_id=first.group_id,
                    group_name=first.group_name,
                    athlete_name=first.athlete_name,
                    chip_ms=chip_ms,
                    passage_time_ms=int(first.passage_time_ms),
                    lap=int(first.lap),
                    received_at_ms=received,
                    revision=int(first.revision),
                    latest_revision=int(latest.revision),
                    active_now=current and bool(latest.is_active) and event_id not in deleted,
                    manual_entry=bool(received) and (
                        received - align_evidence_timestamp(chip_ms, received) > int(late_manual_ms)
                    ),
                    excluded=group_exclusion(first.group_name),
                    latest_bib=latest_identity[0],
                    latest_chip_ms=int(latest.timeline_timestamp_ms),
                    replaced=not current,
                )
            )
    reads.sort(key=lambda item: (item.chip_ms, item.event_id, item.revision))
    return tuple(reads)


def calibration_chip_times(reads: Iterable[ChipRead]) -> list[int]:
    """Chip reads usable for automatic calibration: real reads only."""

    return [read.chip_ms for read in reads if not read.excluded and not read.manual_entry]


def format_chip_time(timestamp_ms: Optional[int], *, date: bool = False) -> str:
    if timestamp_ms is None:
        return "--"
    try:
        value = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, BEIJING_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return f"{timestamp_ms} ms"
    return value.strftime("%Y-%m-%d %H:%M:%S.%f" if date else "%H:%M:%S.%f")[:-3]


__all__ = [
    "BEIJING_TIMEZONE",
    "ChipRead",
    "EXCLUDED_OTHER_DAY",
    "EXCLUDED_SPARE",
    "EXCLUDED_TEST",
    "LATE_MANUAL_MS",
    "calibration_chip_times",
    "chip_reads",
    "dominant_race_date",
    "format_chip_time",
    "group_exclusion",
    "local_date",
    "read_passage_history",
]
