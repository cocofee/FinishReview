import json

from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.race_metadata import RaceMetadata
from realtime.video_timeline import VideoTimelineStore
from realtime.workspace_context import (
    EventWorkspaceMetadataStore, saved_archive_path, saved_live_path, saved_received_path, save_workspace_mode,
)


def test_recovered_context_does_not_conflict_with_first_authoritative_revision(tmp_path):
    passages = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    passages.append(PassageEvent(event_id="first", race_id="race", stage_id="finish", group_id="men",
                                 sequence=1, bib="001", passage_time_ms=1000, emitted_at_ms=1100,
                                 race_name="Old name"))
    path = tmp_path / "cyclerace_race_metadata.json"
    metadata = EventWorkspaceMetadataStore(path, passages, VideoTimelineStore(tmp_path / "video_timeline.jsonl"))
    assert metadata.current().race_name == "Old name"
    assert not path.exists()
    metadata.select("race", "finish")
    authoritative = RaceMetadata(race_id="race", stage_id="finish", revision=1, emitted_at_ms=2,
                                 race_name="Authoritative name")
    metadata.store(authoritative)
    assert metadata.current() == authoritative
    assert not metadata.scope_path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["race_name"] == "Authoritative name"


def test_saved_archive_rejects_path_outside_root_and_stale_directories(tmp_path):
    root = tmp_path / "root"
    inbox = root / ".finishreview"
    inbox.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cyclerace_passage_events.jsonl").touch()
    state = inbox / "review_workspace.json"
    state.write_text(json.dumps({"mode": "archive", "relative_path": "../outside"}), encoding="utf-8")
    assert saved_archive_path(root) is None
    state.write_text(json.dumps({"mode": "archive", "relative_path": "."}), encoding="utf-8")
    assert saved_archive_path(root) is None


def test_received_live_selection_rejects_paths_outside_root(tmp_path):
    root = tmp_path / "root"
    inbox = root / ".finishreview"
    inbox.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cyclerace_passage_events.jsonl").touch()
    state = inbox / "review_workspace.json"
    state.write_text(json.dumps({"mode": "live_received", "relative_path": "../outside"}), encoding="utf-8")
    assert saved_received_path(root) is None
    state.write_text(json.dumps({"mode": "live_received", "relative_path": "."}), encoding="utf-8")
    assert saved_received_path(root) is None


def test_saved_live_standby_restores_empty_workspace_but_rejects_outside_or_missing(tmp_path):
    root = tmp_path / "root"
    target = root / "standby"
    target.mkdir(parents=True)
    save_workspace_mode(root, target, "live_ready")
    assert saved_live_path(root) == target.resolve()
    assert saved_archive_path(root) is None
    assert saved_received_path(root) is None
    state = root / ".finishreview" / "review_workspace.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    state.write_text(json.dumps({"mode": "live_ready", "relative_path": "../outside"}), encoding="utf-8")
    assert saved_live_path(root) is None
    state.write_text(json.dumps({"mode": "live_ready", "relative_path": "missing"}), encoding="utf-8")
    assert saved_live_path(root) is None


def test_saved_live_event_restores_explicit_selection(tmp_path):
    target = tmp_path / "event"
    target.mkdir()
    (target / "cyclerace_race_metadata.json").touch()
    save_workspace_mode(tmp_path, target, "live")
    assert saved_live_path(tmp_path) == target.resolve()
    save_workspace_mode(tmp_path, target, "archive")
    assert saved_live_path(tmp_path) is None
