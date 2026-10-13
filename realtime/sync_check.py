"""Compare CycleRace's own passage records with what FinishReview received.

FinishReview only receives passages through the CycleRace event interface. A
post-race copy of CycleRace's records (never the live database during a race)
is normalised into :class:`UpstreamPassage` rows by a tool, then compared with
the local journal here. The comparison reports differences; it never changes
either side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .passage_receiver import PassageEvent


_PASSAGE_ID_RE = re.compile(r"-passage-(\d+)$")

MISSING_IN_REVIEW = "missing_in_review"
VOID_NOT_SYNCED = "void_not_synced"
DELETED_UPSTREAM = "deleted_upstream"
REVIVED_UPSTREAM = "revived_upstream"
CHANGED_UPSTREAM = "changed_upstream"

SYNC_ISSUE_TITLES = {
    MISSING_IN_REVIEW: "CycleRace 有效记录未同步到 FinishReview",
    VOID_NOT_SYNCED: "CycleRace 已作废，FinishReview 仍为有效",
    DELETED_UPSTREAM: "CycleRace 已删除，FinishReview 仍为有效",
    REVIVED_UPSTREAM: "CycleRace 已恢复，FinishReview 仍为作废",
    CHANGED_UPSTREAM: "号码或时间与 CycleRace 不一致",
}


@dataclass(frozen=True, slots=True)
class UpstreamPassage:
    """One CycleRace passage as stored by CycleRace itself."""

    passage_id: int
    bib: str
    passage_time_ms: int
    is_active: bool
    is_manual: bool = False
    chip_id: str = ""
    group_name: str = ""


@dataclass(frozen=True, slots=True)
class SyncIssue:
    kind: str
    passage_id: int
    bib: str
    passage_time_ms: int
    is_manual: bool
    upstream: Optional[UpstreamPassage]
    event: Optional[PassageEvent]
    message: str

    @property
    def title(self) -> str:
        return SYNC_ISSUE_TITLES.get(self.kind, self.kind)


def passage_id_from_event_id(event_id: str) -> Optional[int]:
    """CycleRace event ids end with ``-passage-<timing passage id>``."""

    match = _PASSAGE_ID_RE.search(str(event_id))
    return int(match.group(1)) if match else None


def _clock_text(milliseconds: int) -> str:
    value = int(milliseconds) % 86_400_000
    return (
        f"{value // 3_600_000:02d}:{value // 60_000 % 60:02d}:"
        f"{value // 1000 % 60:02d}.{value % 1000:03d}"
    )


def latest_events(events: Iterable[PassageEvent]) -> dict[str, PassageEvent]:
    latest: dict[str, PassageEvent] = {}
    for event in events:
        current = latest.get(event.event_id)
        if current is None or (event.revision, event.received_at_ms) >= (
            current.revision,
            current.received_at_ms,
        ):
            latest[event.event_id] = event
    return latest


def check_sync(
    upstream: Sequence[UpstreamPassage],
    events: Iterable[PassageEvent],
    *,
    race_id: str = "",
    stage_id: str = "",
    time_tolerance_ms: int = 1,
) -> tuple[SyncIssue, ...]:
    """Report records whose state differs between CycleRace and FinishReview.

    ``events`` must contain the latest revision of each received event
    (inactive ones included). Passage ids are taken from event ids.
    """

    received: dict[int, PassageEvent] = {}
    for event in events:
        if race_id and event.race_id != race_id:
            continue
        if stage_id and event.stage_id != stage_id:
            continue
        passage_id = passage_id_from_event_id(event.event_id)
        if passage_id is None:
            continue
        current = received.get(passage_id)
        if current is None or (event.revision, event.received_at_ms) >= (
            current.revision,
            current.received_at_ms,
        ):
            received[passage_id] = event
    rows: Mapping[int, UpstreamPassage] = {row.passage_id: row for row in upstream}
    issues: list[SyncIssue] = []
    for passage_id, row in sorted(rows.items()):
        event = received.get(passage_id)
        kind = ""
        if event is None:
            if row.is_active:
                kind = MISSING_IN_REVIEW
        elif row.is_active and not event.is_active:
            kind = REVIVED_UPSTREAM
        elif not row.is_active and event.is_active:
            kind = VOID_NOT_SYNCED
        elif row.is_active and (
            event.bib.strip() != row.bib.strip()
            or abs(int(event.passage_time_ms) - int(row.passage_time_ms)) > time_tolerance_ms
        ):
            kind = CHANGED_UPSTREAM
        if not kind:
            continue
        manual = "补录" if row.is_manual else "芯片"
        detail = f"{manual} {row.bib or '-'} {_clock_text(row.passage_time_ms)}"
        if kind == CHANGED_UPSTREAM and event is not None:
            detail += f"；FinishReview 为 {event.bib or '-'} {_clock_text(event.passage_time_ms)}"
        issues.append(
            SyncIssue(
                kind=kind,
                passage_id=passage_id,
                bib=row.bib,
                passage_time_ms=int(row.passage_time_ms),
                is_manual=row.is_manual,
                upstream=row,
                event=event,
                message=f"{SYNC_ISSUE_TITLES[kind]}：{detail}",
            )
        )
    for passage_id, event in sorted(received.items()):
        if passage_id in rows or not event.is_active:
            continue
        issues.append(
            SyncIssue(
                kind=DELETED_UPSTREAM,
                passage_id=passage_id,
                bib=event.bib,
                passage_time_ms=int(event.passage_time_ms),
                is_manual=False,
                upstream=None,
                event=event,
                message=(
                    f"{SYNC_ISSUE_TITLES[DELETED_UPSTREAM]}："
                    f"{event.bib or '-'} {_clock_text(event.passage_time_ms)}"
                ),
            )
        )
    return tuple(issues)


__all__ = [
    "CHANGED_UPSTREAM",
    "DELETED_UPSTREAM",
    "MISSING_IN_REVIEW",
    "REVIVED_UPSTREAM",
    "SYNC_ISSUE_TITLES",
    "SyncIssue",
    "UpstreamPassage",
    "VOID_NOT_SYNCED",
    "check_sync",
    "latest_events",
    "passage_id_from_event_id",
]
