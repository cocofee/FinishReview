import csv
import os
from datetime import datetime, timezone

from realtime import review_export
from realtime.passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
)
from realtime.passage_receiver import PassageEvent
from realtime.race_metadata import (
    RaceAthleteMetadata,
    RaceGroupMetadata,
    RaceMetadata,
)


def _timestamp_ms(hour=0, minute=0, second=0, millisecond=0):
    value = datetime(2026, 8, 24, hour, minute, second, tzinfo=timezone.utc)
    return int(value.timestamp() * 1000) + millisecond


def _event(
    event_id="passage-1",
    *,
    passage_timestamp_ms=None,
    bib="15",
    chip_id="chip-15",
    athlete_name="张三",
    group_name="男子公开组",
    athlete_id="athlete-15",
    group_id="group-1",
    is_active=True,
):
    timestamp_ms = passage_timestamp_ms or _timestamp_ms()
    return PassageEvent(
        event_id=event_id,
        race_id="race-1",
        stage_id="stage-1",
        group_id=group_id,
        sequence=1,
        chip_id=chip_id,
        bib=bib,
        passage_time_ms=timestamp_ms,
        passage_timestamp_ms=timestamp_ms,
        emitted_at_ms=timestamp_ms,
        athlete_id=athlete_id,
        athlete_name=athlete_name,
        group_name=group_name,
        is_active=is_active,
    )


def _confirm(store, event_id, source, confirmed_at_ms):
    return store.confirm(
        passage_event_id=event_id,
        bib="15",
        confirmed_source=source,
        segment_id=f"{source}-segment",
        frame_index=12,
        position_ms=480,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=confirmed_at_ms,
    )


def test_regular_confirmation_marks_overall_review_confirmed(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    _confirm(store, "passage-1", REGULAR_SOURCE, _timestamp_ms(1))

    row = review_export.build_review_summary_rows((_event(),), store)[0]

    assert row.regular_status == "已确认"
    assert row.high_speed_status == "未确认"
    assert row.review_status == "已确认"


def test_high_speed_confirmation_marks_overall_review_confirmed(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    _confirm(store, "passage-1", HIGH_SPEED_SOURCE, _timestamp_ms(1))

    row = review_export.build_review_summary_rows((_event(),), store)[0]

    assert row.regular_status == "未确认"
    assert row.high_speed_status == "已确认"
    assert row.review_status == "已确认"


def test_latest_confirmation_time_uses_newest_source(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    _confirm(store, "passage-1", REGULAR_SOURCE, _timestamp_ms(1, 2, 3, 400))
    _confirm(store, "passage-1", HIGH_SPEED_SOURCE, _timestamp_ms(2, 3, 4, 500))

    row = review_export.build_review_summary_rows((_event(),), store)[0]

    assert row.regular_status == "已确认"
    assert row.high_speed_status == "已确认"
    assert row.last_confirmation_time == "2026-08-24 10:03:04.500"


def test_deleted_confirmation_is_exported_as_unconfirmed(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    _confirm(store, "passage-1", REGULAR_SOURCE, _timestamp_ms(1))
    store.clear(
        "passage-1",
        REGULAR_SOURCE,
        confirmed_at_ms=_timestamp_ms(2),
    )

    row = review_export.build_review_summary_rows((_event(),), store)[0]

    assert row.regular_status == "未确认"
    assert row.review_status == "未确认"
    assert row.last_confirmation_time == ""


def test_rows_are_sorted_and_use_metadata_fallbacks(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    metadata = RaceMetadata(
        race_id="race-1",
        stage_id="stage-1",
        revision=1,
        emitted_at_ms=_timestamp_ms(),
        groups=(RaceGroupMetadata("group-1", "精英组"),),
        athletes=(
            RaceAthleteMetadata(
                athlete_id="athlete-15",
                bib="A0015",
                name="李明",
                team_name="城市车队",
                group_id="group-1",
            ),
        ),
    )
    events = (
        _event(
            "passage-later",
            passage_timestamp_ms=_timestamp_ms(0, 0, 2),
            bib="",
            athlete_name="",
            group_name="",
        ),
        _event(
            "passage-inactive",
            passage_timestamp_ms=_timestamp_ms(0, 0, 1),
            is_active=False,
        ),
        _event(
            "passage-earlier",
            passage_timestamp_ms=_timestamp_ms(0, 0, 1),
        ),
        PassageEvent(
            event_id="other-race",
            race_id="race-2",
            stage_id="stage-1",
            group_id="group-1",
            sequence=1,
            bib="99",
            passage_time_ms=_timestamp_ms(),
            emitted_at_ms=_timestamp_ms(),
        ),
    )

    rows = review_export.build_review_summary_rows(events, store, metadata)

    assert [row.sequence for row in rows] == [1, 2]
    assert [row.passage_time for row in rows] == ["08:00:01.000", "08:00:02.000"]
    assert rows[1].bib == "A0015"
    assert rows[1].athlete_name == "李明"
    assert rows[1].group_name == "精英组"


def test_export_is_utf8_bom_unicode_csv_and_atomically_replaced(
    tmp_path,
    monkeypatch,
):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    output_path = tmp_path / review_export.REVIEW_SUMMARY_FILENAME
    output_path.write_text("stale", encoding="utf-8")
    replace_calls = []
    real_replace = os.replace

    def tracked_replace(source, destination):
        replace_calls.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(review_export.os, "replace", tracked_replace)

    exported = review_export.export_review_summary(tmp_path, (_event(),), store)

    assert exported == output_path
    assert output_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(review_export.REVIEW_SUMMARY_HEADERS)
    assert rows[1][1:4] == ["15", "张三", "男子公开组"]
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == output_path
    assert not replace_calls[0][0].exists()
