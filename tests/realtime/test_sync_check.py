from realtime.passage_receiver import PassageEvent
from realtime.sync_check import (
    CHANGED_UPSTREAM,
    DELETED_UPSTREAM,
    MISSING_IN_REVIEW,
    REVIVED_UPSTREAM,
    VOID_NOT_SYNCED,
    UpstreamPassage,
    check_sync,
    latest_events,
    passage_id_from_event_id,
)


RACE = "event-x-competition-y"


def _event(passage_id, bib, time_ms, *, revision=1, active=True, race_id=RACE):
    return PassageEvent(
        event_id=f"{race_id}-stage-1-passage-{passage_id}",
        race_id=race_id,
        stage_id="stage-1",
        group_id="1",
        sequence=passage_id,
        bib=bib,
        passage_time_ms=time_ms,
        lap=0,
        revision=revision,
        is_active=active,
        passage_timestamp_ms=1_788_000_000_000 + time_ms,
        received_at_ms=1_788_000_000_500 + time_ms + revision,
    )


def test_passage_id_comes_from_event_id_suffix():
    assert passage_id_from_event_id(f"{RACE}-stage-1-passage-504") == 504
    assert passage_id_from_event_id("unrelated") is None


def test_sync_check_lists_the_three_helan_style_gaps():
    upstream = [
        UpstreamPassage(366, "550", 39_807_896, True, True),     # current manual entry
        UpstreamPassage(492, "701", 44_129_286, False, False),   # voided upstream
        UpstreamPassage(503, "701", 47_969_842, True, True),     # manual re-entry
        UpstreamPassage(504, "319", 39_490_466, True, True),     # manual entry
        UpstreamPassage(7, "4", 38_008_217, True, False),
    ]
    events = [
        _event(366, "550", 39_807_896),
        _event(247, "550", 39_807_244),            # deleted upstream, still active here
        _event(492, "701", 44_129_286),            # void never arrived
        _event(7, "4", 38_008_217),
    ]

    issues = check_sync(upstream, events)

    by_kind = {(issue.kind, issue.bib) for issue in issues}
    assert by_kind == {
        (VOID_NOT_SYNCED, "701"),
        (MISSING_IN_REVIEW, "701"),
        (MISSING_IN_REVIEW, "319"),
        (DELETED_UPSTREAM, "550"),
    }
    missing = next(issue for issue in issues if issue.bib == "319")
    assert missing.is_manual
    assert "未同步" in missing.message and "10:58:10.466" in missing.message


def test_sync_check_uses_latest_revision_and_reports_changes_and_revivals():
    upstream = [
        UpstreamPassage(1, "10", 1_000, True),
        UpstreamPassage(2, "21", 2_000, True),
        UpstreamPassage(3, "30", 3_000, True),
    ]
    events = [
        _event(1, "10", 1_000, revision=1, active=False),
        _event(1, "10", 1_000, revision=2, active=True),   # latest is active: in sync
        _event(2, "20", 2_000),                            # bib changed upstream
        _event(3, "30", 3_000, active=False),              # revived upstream only
    ]

    issues = check_sync(upstream, latest_events(events).values())

    assert [(issue.kind, issue.passage_id) for issue in issues] == [
        (CHANGED_UPSTREAM, 2),
        (REVIVED_UPSTREAM, 3),
    ]


def test_sync_check_ignores_other_races_and_inactive_local_leftovers():
    upstream = [UpstreamPassage(1, "10", 1_000, True)]
    events = [
        _event(1, "10", 1_000),
        _event(9, "90", 9_000, race_id="other-race"),
        _event(8, "80", 8_000, active=False),
    ]

    assert check_sync(upstream, events, race_id=RACE) == ()
