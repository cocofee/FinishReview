import json

from realtime.chip_history import (
    EXCLUDED_SPARE,
    EXCLUDED_TEST,
    calibration_chip_times,
    chip_reads,
    format_chip_time,
    read_passage_history,
)
from realtime.passage_receiver import PassageEvent


DAY = 1_789_747_200_000  # 2026-09-19 00:00 Beijing time


def _event(event_id, bib, chip_ms, *, revision=1, active=True, received=None, group="男子青年组", race="race"):
    return PassageEvent(
        event_id=event_id, race_id=race, stage_id="stage-1", group_id="1", sequence=1,
        bib=bib, chip_id=f"chip-{bib}", passage_time_ms=(chip_ms - DAY) % 86_400_000, lap=0,
        revision=revision, is_active=active, passage_timestamp_ms=chip_ms,
        group_name=group, received_at_ms=received if received is not None else chip_ms - 500,
    )


def test_reads_use_first_active_revision_and_keep_later_withdrawal():
    t = DAY + 10 * 3_600_000
    history = [
        _event("a", "020", t, revision=1),
        _event("a", "020", t, revision=2, active=False, received=t + 600_000),
        _event("b", "023", t + 20),
    ]

    reads = chip_reads(history, race_id="race")

    assert [read.bib for read in reads] == ["020", "023"]
    withdrawn = reads[0]
    assert withdrawn.revision == 1 and withdrawn.latest_revision == 2
    assert not withdrawn.active_now
    assert reads[1].active_now


def test_reads_skip_reused_event_ids_from_another_day():
    t = DAY + 10 * 3_600_000
    history = [
        _event("reused", "418", t - 6 * 86_400_000, revision=4, active=True),
        _event("reused", "418", t, revision=5),
        _event("other", "500", t + 1_000),
    ]

    reads = chip_reads(history, race_date="2026-09-19")

    reused = next(read for read in reads if read.event_id == "reused")
    assert reused.chip_ms == t and reused.revision == 5


def test_late_received_passages_are_manual_entries_and_groups_are_excluded():
    t = DAY + 11 * 3_600_000
    history = [
        _event("manual", "307", t, received=t + 2_064_000),
        _event("spare", "901", t + 5_000, group="备用组"),
        _event("test", "902", t + 6_000, group="测试组"),
        _event("chip", "100", t + 7_000),
    ]

    reads = {read.bib: read for read in chip_reads(history)}

    assert reads["307"].manual_entry
    assert reads["901"].excluded == EXCLUDED_SPARE
    assert reads["902"].excluded == EXCLUDED_TEST
    assert calibration_chip_times(reads.values()) == [t + 7_000]


def test_local_deletions_mark_reads_inactive_now():
    t = DAY + 10 * 3_600_000
    reads = chip_reads([_event("gone", "1", t)], deleted_event_ids=["gone"])

    assert not reads[0].active_now


def test_history_reader_returns_all_revisions_and_tolerates_torn_tail(tmp_path):
    t = DAY + 10 * 3_600_000
    path = tmp_path / "cyclerace_passage_events.jsonl"
    lines = [
        json.dumps(_event("a", "1", t).to_payload(), ensure_ascii=False),
        json.dumps(_event("a", "1", t, revision=2, active=False).to_payload(), ensure_ascii=False),
    ]
    path.write_text("\n".join(lines) + "\n" + lines[0][:40], encoding="utf-8")

    history = read_passage_history(path)

    assert [event.revision for event in history] == [1, 2]
    assert path.read_text(encoding="utf-8").endswith(lines[0][:40])


def test_chip_time_formatting_uses_beijing_time():
    assert format_chip_time(DAY + 10 * 3_600_000 + 61_234) == "10:01:01.234"
    assert format_chip_time(None) == "--"
