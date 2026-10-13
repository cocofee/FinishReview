import json
import sqlite3

import pytest

from realtime.passage_receiver import PassageEvent
from tools.cyclerace_db_copy import copy_race_db, read_snapshot
from tools.cyclerace_sync_check import run_sync_check


RACE = "event-a-competition-b"
DAY = 1_788_019_200_000  # 2026-08-30 00:00 Beijing time


def _make_race_db(path):
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
        insert into category values (1, '女子公路公开组'), (2, '测试组');
        insert into athlete values (1, '319', 'a', 1), (2, '701', 'b', 1), (3, '550', 'c', 1), (4, '901', 't', 2);
        insert into timing_raw values (10, '245376', 39490466), (11, '294225', 44129286), (12, '261680', 39807896);
        insert into timing_passage values
            (504, 1, 1, 39490466, 10, 1, 1),
            (492, 1, 2, 44129286, 11, 0, 0),
            (366, 1, 3, 39807896, 12, 1, 1);
        insert into gun_start values (1, 1, 1, 33420931, 1), (2, 1, 1, 33000000, 0);
        insert into athlete_stage_status values (1, 1, 2, 'OTL');
        insert into chip_mapping values (1, '245376', '319', 1, 1), (2, '294225', '701', 2, 1);
        """
    )
    connection.commit()
    connection.close()


def _event(passage_id, bib, time_ms, *, active=True):
    return PassageEvent(
        event_id=f"{RACE}-stage-1-passage-{passage_id}", race_id=RACE, stage_id="stage-1",
        group_id="1", sequence=passage_id, bib=bib, passage_time_ms=time_ms, lap=0,
        is_active=active, passage_timestamp_ms=DAY + time_ms, received_at_ms=DAY + time_ms + 500,
    )


def test_copy_refuses_to_write_next_to_the_live_database(tmp_path):
    source_dir = tmp_path / "CycleRace" / "001"
    source_dir.mkdir(parents=True)
    _make_race_db(source_dir / "race.db")
    with pytest.raises(ValueError):
        copy_race_db(source_dir / "race.db", source_dir)

    copied = copy_race_db(source_dir / "race.db", tmp_path / "work")
    assert copied.parent == (tmp_path / "work").resolve()
    snapshot = read_snapshot(copied)
    assert snapshot.stage_date == "2026-08-30"
    assert snapshot.distance_km == 61.0
    assert [gun.group_name for gun in snapshot.gun_starts] == ["女子公路公开组"]
    assert snapshot.gun_starts[0].gun_ms == DAY + 33_420_931
    assert snapshot.statuses == {"701": "OTL"}
    assert snapshot.roster["901"] == "测试组"
    assert {row.passage_id: row.chip_id for row in snapshot.passages}[504] == "245376"


def test_sync_check_reports_unsynced_manual_entry_void_and_deleted_duplicate(tmp_path):
    source_dir = tmp_path / "CycleRace" / "001"
    source_dir.mkdir(parents=True)
    database = source_dir / "race.db"
    _make_race_db(database)
    before = database.read_bytes()
    workspace = tmp_path / "event"
    workspace.mkdir()
    events = [
        _event(492, "701", 44_129_286),
        _event(366, "550", 39_807_896),
        _event(247, "550", 39_807_244),
    ]
    (workspace / "cyclerace_passage_events.jsonl").write_text(
        "".join(json.dumps(event.to_payload(), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    report = run_sync_check(database, workspace, tmp_path / "out")

    kinds = sorted((item["kind"], item["bib"]) for item in report["issues"])
    assert kinds == [
        ("deleted_upstream", "550"),
        ("missing_in_review", "319"),
        ("void_not_synced", "701"),
    ]
    assert database.read_bytes() == before
    text = (tmp_path / "out" / "sync_check.txt").read_text(encoding="utf-8")
    assert "未同步" in text and "319" in text
