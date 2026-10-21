import json
import sqlite3

from realtime.passage_receiver import PassageEvent
from tools.chip_self_check import run_self_check


RACE = "event-a-competition-b"
DAY = 1_788_019_200_000  # 2026-08-30 00:00 Beijing time
GUN = 33_420_931  # 09:17:00.931


def _event(passage_id, bib, time_ms, *, revision=1, active=True, delay=500, group="女子公路公开组"):
    return PassageEvent(
        event_id=f"{RACE}-stage-1-passage-{passage_id}", race_id=RACE, stage_id="stage-1",
        group_id="4", sequence=passage_id, bib=bib, passage_time_ms=time_ms, lap=0,
        revision=revision, is_active=active, passage_timestamp_ms=DAY + time_ms,
        received_at_ms=DAY + time_ms + delay + revision, group_name=group,
    )


def _workspace(tmp_path, events):
    root = tmp_path / "event"
    root.mkdir()
    (root / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(event.to_payload(), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return root


def _race_db(path, passages):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        create table race (id integer primary key, name text);
        create table stage (id integer primary key, race_id integer, name text, date text,
                            distance_km real, stage_type text);
        create table category (id integer primary key, name text);
        create table athlete (id integer primary key, bib text, name text, category_id integer);
        create table timing_raw (id integer primary key, chip_id text, raw_timestamp_ms integer);
        create table timing_passage (id integer primary key, stage_id integer, athlete_id integer,
                                     passage_time_ms integer, selected_raw_id integer,
                                     is_manual integer, is_active integer);
        create table gun_start (id integer primary key, stage_id integer, category_id integer,
                                gun_time_ms integer, is_valid integer);
        create table athlete_stage_status (id integer primary key, stage_id integer,
                                           athlete_id integer, status text);
        create table chip_mapping (id integer primary key, chip_internal_id text, bib text,
                                   athlete_id integer, is_active integer);
        insert into race values (1, '公路赛');
        insert into stage values (1, 1, '公路', '2026-08-30', 61.0, 'RR');
        insert into category values (4, '女子公路公开组');
        """
    )
    connection.execute("insert into gun_start values (1, 1, 4, ?, 1)", (GUN,))
    for passage_id, bib, time_ms, manual, active in passages:
        connection.execute("insert into athlete values (?, ?, '', 4)", (passage_id, bib))
        connection.execute(
            "insert into timing_passage values (?, 1, ?, ?, null, ?, ?)",
            (passage_id, passage_id, time_ms, manual, active),
        )
    connection.commit()
    connection.close()


def test_self_check_without_cyclerace_data_lists_missing_inputs(tmp_path):
    # Both 550 passages were appended after the later automatic read of 601,
    # out of CycleRace's crossing order: one rider entered manually twice.
    events = [
        _event(1, "601", GUN + 6_500_000),
        _event(2, "550", GUN + 6_000_000, delay=420_000),
        _event(3, "550", GUN + 6_000_652, delay=1_213_000),
        _event(4, "901", GUN - 600_000, group="测试组"),
    ]
    report = run_self_check(_workspace(tmp_path, events), tmp_path / "out")

    assert report["counts"] == {"duplicate_manual": 1, "test_chip": 1}
    assert any("发枪时间" in item for item in report["missing_inputs"])
    assert "重复补录" in (tmp_path / "out" / "chip_checks.txt").read_text(encoding="utf-8")


def test_self_check_with_race_db_copy_adds_early_reads_and_sync(tmp_path):
    events = [
        _event(3, "746", GUN + 696_000, revision=7),
        _event(3, "746", GUN + 696_000, revision=8, active=False),
        _event(4, "601", GUN + 6_500_000),
    ]
    database = tmp_path / "race.db"
    _race_db(database, [(3, "746", GUN + 696_000, 0, 0), (4, "601", GUN + 6_500_000, 0, 1),
                        (5, "319", GUN + 5_900_000, 1, 1)])

    report = run_self_check(_workspace(tmp_path, events), tmp_path / "out", race_db=database)

    early = next(item for item in report["items"] if item["kind"] == "early_read")
    assert early["bibs"] == ["746"] and early["status"] == "closed_upstream"
    sync = next(item for item in report["items"] if item["kind"] == "sync")
    assert sync["bibs"] == ["319"] and sync["evidence"]["sync_kind"] == "missing_in_review"
    assert (tmp_path / "out" / "cyclerace_copy" / "race.db").is_file()
