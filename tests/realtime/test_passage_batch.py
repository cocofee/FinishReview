from types import SimpleNamespace

from realtime.passage_batch import build_review_batches


def _event(event_id, timestamp, *, group_id="group-1", active=True, sequence=1):
    return SimpleNamespace(
        event_id=event_id,
        timeline_timestamp_ms=timestamp,
        group_id=group_id,
        is_active=active,
        sequence=sequence,
    )


def test_review_batches_merge_mixed_groups_with_five_second_gap():
    events = (
        _event("a", 1_000, group_id="1"),
        _event("b", 4_000, group_id="2"),
        _event("c", 8_900, group_id="3"),
        _event("d", 15_000, group_id="4"),
    )

    result = build_review_batches(events)

    assert [batch.event_ids for batch in result] == [
        ("a", "b", "c"),
        ("d",),
    ]
    assert result[0].is_large is False


def test_review_batches_keep_three_second_subwave_boundaries():
    events = (
        _event("a", 1_000),
        _event("b", 2_000),
        _event("c", 5_500),
        _event("d", 7_000),
    )

    result = build_review_batches(events)

    assert len(result) == 1
    assert result[0].event_ids == ("a", "b", "c", "d")
    assert result[0].subwave_breaks == (2,)


def test_review_batches_ignore_inactive_events():
    events = (
        _event("a", 1_000),
        _event("inactive", 1_500, active=False),
        _event("b", 2_000),
    )

    result = build_review_batches(events)

    assert result[0].event_ids == ("a", "b")
