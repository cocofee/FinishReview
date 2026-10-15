import json
from dataclasses import replace

import pytest

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.passage_deletions import decode_deletions
from realtime.passage_receiver import PassageEvent, PassageEventStore, PassageJournalError


def saved_workspace(tmp_path):
    path = tmp_path / "cyclerace_passage_events.jsonl"
    event = PassageEvent(
        event_id="race-stage-1-passage-1", race_id="race", stage_id="stage-1",
        group_id="3", sequence=1, bib="443", chip_id="261892", lap=0,
        revision=13, passage_time_ms=36_000_000, passage_timestamp_ms=1_789_783_200_000,
    )
    store = PassageEventStore(path)
    store.append(event)
    mark = {
        "event_id": event.event_id,
        "basis": {"race_id": event.race_id, "stage_id": event.stage_id,
                  "group_id": event.group_id, "bib": event.bib, "chip_id": event.chip_id,
                  "lap": event.lap, "revision": event.revision, "is_active": True,
                  "timeline_timestamp_ms": event.timeline_timestamp_ms},
        "reason": "saved operator reason", "deleted_at_ms": 123,
    }
    store.deletions_path.write_text(json.dumps({"schema_version": 2, "records": [mark]}), encoding="utf-8")
    return path, event, mark


def test_nested_legacy_deletions_match_in_ui_and_analysis_without_rewriting(tmp_path):
    path, event, mark = saved_workspace(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    reopened = PassageEventStore(path)
    assert reopened.events() == ()
    loaded = reopened.deletion(event.event_id)
    assert loaded["reason"] == mark["reason"]
    assert loaded["basis"] == mark["basis"]
    assert loaded["deleted_at_ms"] == 123
    assert not AnalysisWorkspace(tmp_path).reads[0].active_now
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("change", [{"bib": "444"}, {"chip_id": "other"},
                                   {"passage_timestamp_ms": 1_789_783_201_000}])
def test_reused_record_is_not_hidden_by_old_basis(tmp_path, change):
    path, event, _ = saved_workspace(tmp_path)
    store = PassageEventStore(path)
    updated = replace(event, revision=14, **change)
    store.append(updated)
    assert PassageEventStore(path).events() == (updated,)
    assert AnalysisWorkspace(tmp_path).deleted_event_ids == ()


def test_next_deletion_preserves_old_reason_and_identity(tmp_path):
    path, event, mark = saved_workspace(tmp_path)
    store = PassageEventStore(path)
    other = replace(event, event_id="another", bib="900")
    store.append(other)
    store.delete(other.event_id, reason="new reason")
    reopened = PassageEventStore(path)
    assert reopened.events() == ()
    assert reopened.deletion(event.event_id)["basis"] == mark["basis"]
    assert reopened.deletion(event.event_id)["reason"] == mark["reason"]
    assert not any(read.active_now for read in AnalysisWorkspace(tmp_path).reads)


@pytest.mark.parametrize("payload", [
    {"schema_version": 2, "records": [{"event_id": "x"}]},
    {"records": [{"event_id": "x", "basis": {"bib": "1", "chip_id": "2"}}]},
    {"records": [], "deletions": []},
    {"records": "invalid"},
])
def test_invalid_basis_is_never_silently_treated_as_id_only_deletion(payload):
    with pytest.raises(ValueError):
        decode_deletions(payload)


def test_unreadable_file_reports_path_and_analysis_does_not_ignore_it(tmp_path):
    path, _, _ = saved_workspace(tmp_path)
    deletion_path = path.with_name("cyclerace_passage_events_deleted.json")
    deletion_path.write_text('{"records": [', encoding="utf-8")
    with pytest.raises(PassageJournalError) as error:
        PassageEventStore(path)
    assert str(deletion_path) in str(error.value)
    with pytest.raises(ValueError) as error:
        AnalysisWorkspace(tmp_path)
    assert str(deletion_path) in str(error.value)
