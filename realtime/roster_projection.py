"""Incremental, widget-free roster projection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RosterFilter:
    group_id: str = ""
    query: str = ""
    review_status: str = "all"


@dataclass(frozen=True, slots=True)
class RosterDelta:
    added: frozenset[str]
    removed: frozenset[str]
    updated: frozenset[str]
    order_changed: bool
    visible_ids: tuple[str, ...]


def _event_id(event) -> str:
    return str(getattr(event, "event_id", ""))


class RosterProjection:
    """Keep event order and visible rows independent from table widgets."""

    def __init__(self) -> None:
        self._events: dict[str, object] = {}
        self._order: tuple[str, ...] = ()
        self._statuses: dict[str, str] = {}
        self._filter = RosterFilter()
        self._projected_filter = self._filter
        self._projected_statuses: dict[str, str] = {}

    @property
    def events(self) -> tuple[object, ...]:
        return tuple(self._events[event_id] for event_id in self._order if event_id in self._events)

    @property
    def filter(self) -> RosterFilter:
        return self._filter

    def set_filter(self, value: RosterFilter) -> None:
        self._filter = value

    def set_statuses(self, statuses: Mapping[str, str]) -> None:
        self._statuses = {str(key): str(value) for key, value in statuses.items()}

    def _matches(self, event, filter_value: RosterFilter | None = None,
                 statuses: Mapping[str, str] | None = None) -> bool:
        filter_value = self._filter if filter_value is None else filter_value
        if filter_value.group_id and str(getattr(event, "group_id", "")) != filter_value.group_id:
            return False
        query = filter_value.query.casefold().strip()
        if query:
            values = (
                str(getattr(event, "bib", "")),
                str(getattr(event, "athlete_name", "")),
                str(getattr(event, "athlete_id", "")),
            )
            if not any(query in value.casefold() for value in values):
                return False
        status = (self._statuses if statuses is None else statuses).get(_event_id(event), "待核对")
        if filter_value.review_status == "confirmed":
            return status == "已确认"
        if filter_value.review_status == "blocked":
            return status == "受阻"
        if filter_value.review_status == "pending":
            return status == "待核对"
        return True

    def visible_events(self) -> tuple[object, ...]:
        return tuple(event for event in self.events if self._matches(event))

    def replace(self, events: Iterable[object], *, changed_event_ids: Iterable[str] | None = None) -> RosterDelta:
        previous_order = self._order
        previous_events = self._events
        previous_filter = self._projected_filter
        previous_statuses = self._projected_statuses
        incoming = tuple(events)
        self._events = {_event_id(event): event for event in incoming if _event_id(event)}
        self._order = tuple(_event_id(event) for event in incoming if _event_id(event))
        visible = self.visible_events()
        old_visible_ids = tuple(
            event_id for event_id in previous_order
            if event_id in previous_events and self._matches(
                previous_events[event_id], previous_filter, previous_statuses
            )
        )
        visible_ids = tuple(_event_id(event) for event in visible)
        changed = {str(event_id) for event_id in changed_event_ids or ()}
        added = frozenset(set(visible_ids) - set(old_visible_ids))
        removed = frozenset(set(old_visible_ids) - set(visible_ids))
        updated = frozenset(
            event_id for event_id in changed
            if event_id in set(visible_ids) and event_id in set(old_visible_ids)
        )
        self._projected_filter = self._filter
        self._projected_statuses = dict(self._statuses)
        return RosterDelta(added, removed, updated, old_visible_ids != visible_ids, visible_ids)
