from types import SimpleNamespace

import pytest

from realtime.preflight import PreflightJournal, PreflightRun, validate_event_network


def _event(
    event_id: str,
    *,
    group_id: str = "test-group",
    emitted_at_ms: int = 10_000,
):
    return SimpleNamespace(
        event_id=event_id,
        group_id=group_id,
        emitted_at_ms=emitted_at_ms,
        sequence=1,
        is_active=True,
        bib="101",
        chip_id="chip-101",
        race_id="race-1",
        stage_id="stage-1",
    )


def test_preflight_accepts_first_locally_received_event_from_any_group():
    previous = _event("old-event")
    run = PreflightRun.start(
        (previous,),
        started_at_ms=9_000,
        require_regular=True,
        require_high_speed=True,
    )

    cross_clock_event = _event(
        "new-event",
        group_id="formal-group",
        emitted_at_ms=1_000,
    )
    observed = run.observe(
        (previous, cross_clock_event),
        received_order={
            ("race-1", "stage-1", "new-event"): 6,
        },
    )
    assert observed.event_id == "new-event"
    assert observed.group_id == "formal-group"
    assert observed.bib == "101"
    assert observed.status == "waiting_evidence"


def test_preflight_ignores_events_not_received_after_local_start():
    run = PreflightRun.start(
        (),
        started_at_ms=9_000,
        started_receive_sequence=10,
        require_regular=True,
        require_high_speed=True,
    )
    event = _event("new-event", emitted_at_ms=99_000)

    assert run.observe(
        (event,),
        received_order={("race-1", "stage-1", "new-event"): 10},
    ).event_id == ""


def test_preflight_passes_only_after_required_evidence_is_ready():
    run = PreflightRun.start(
        (),
        started_at_ms=9_000,
        require_regular=True,
        require_high_speed=True,
    ).observe((_event("new-event"),))

    assert not run.with_evidence(regular_ready=True, high_speed_ready=False).passed
    assert run.with_evidence(regular_ready=True, high_speed_ready=True).passed


def test_preflight_can_skip_a_disabled_evidence_source():
    run = PreflightRun.start(
        (),
        started_at_ms=9_000,
        require_regular=False,
        require_high_speed=False,
    ).observe((_event("new-event"),))

    assert run.passed


def test_preflight_journal_recovers_test_event_ids(tmp_path):
    journal = PreflightJournal(tmp_path / "preflight_tests.jsonl")
    run = PreflightRun.start(
        (),
        started_at_ms=9_000,
        require_regular=False,
        require_high_speed=False,
    ).observe((_event("new-event"),))

    journal.append(run, recorded_at_ms=10_100)

    assert journal.event_ids() == frozenset({"new-event"})
    assert journal.event_keys() == frozenset(
        {("race-1", "stage-1", "new-event")}
    )

    journal.restore(
        ("race-1", "stage-1", "new-event"),
        recorded_at_ms=10_200,
    )

    assert journal.event_keys() == frozenset()
    assert journal.latest_entry() is None


def test_validate_event_network_rejects_duplicate_or_mixed_subnets():
    validate_event_network(
        ("192.168.50.10", "192.168.50.20", "192.168.50.30", "192.168.50.2")
    )
    with pytest.raises(ValueError, match="不能重复"):
        validate_event_network(("192.168.50.10", "192.168.50.10"))
    with pytest.raises(ValueError, match="同一/24网段"):
        validate_event_network(("192.168.50.10", "192.168.60.20"))
    for address in ("192.168.50.0", "192.168.50.255", "127.0.0.1"):
        with pytest.raises(ValueError, match="不是可用的赛事设备主机地址"):
            validate_event_network((address, "192.168.50.20"))
