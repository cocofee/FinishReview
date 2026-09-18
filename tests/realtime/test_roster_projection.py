from dataclasses import dataclass

from realtime.roster_projection import RosterFilter, RosterProjection


@dataclass(frozen=True)
class Event:
    event_id: str
    bib: str
    athlete_name: str
    group_id: str = "g"


def test_projection_preserves_order_and_reports_incremental_changes():
    projection = RosterProjection()
    first = Event("one", "1", "Alice")
    second = Event("two", "2", "Bob")
    assert projection.replace((first, second)).visible_ids == ("one", "two")
    updated = Event("two", "2", "Bobby")
    delta = projection.replace((first, updated), changed_event_ids=("two",))
    assert delta.added == frozenset()
    assert delta.removed == frozenset()
    assert delta.updated == frozenset({"two"})
    assert not delta.order_changed


def test_filter_changes_only_visible_rows():
    projection = RosterProjection()
    events = (Event("one", "1", "Alice", "a"), Event("two", "2", "Bob", "b"))
    projection.replace(events)
    projection.set_filter(RosterFilter(group_id="b"))
    delta = projection.replace(events)
    assert delta.visible_ids == ("two",)
    assert delta.added == frozenset()
    assert delta.removed == frozenset({"one"})


def test_status_filter_and_query_are_widget_free():
    projection = RosterProjection()
    events = (Event("one", "15", "Alice"), Event("two", "16", "Bob"))
    projection.set_statuses({"one": "已确认", "two": "待核对"})
    projection.set_filter(RosterFilter(query="15", review_status="confirmed"))
    projection.replace(events)
    assert tuple(event.event_id for event in projection.visible_events()) == ("one",)
