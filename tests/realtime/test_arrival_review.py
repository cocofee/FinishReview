import json
from dataclasses import replace

import pytest

from realtime.arrival_review import ArrivalReviewStore, event_binding
from realtime.passage_receiver import PassageEvent


SCOPE = ("race", "stage")
FRAME = dict(camera_index=1, segment_id="seg", video_path="a.mkv", frame_index=5,
             position_ms=250, recorder_ms=1_800_000_000_250)


def event(bib="321", identity="one", stamp=1_800_000_000_100):
    return PassageEvent(event_id=identity, race_id="race", stage_id="stage", group_id="g",
                        sequence=1, bib=bib, passage_timestamp_ms=stamp)


def confirm(store, target=None):
    target = target or event()
    return store.save(SCOPE, "event:" + target.event_id, event_binding(target), "confirm",
                      event_id=target.event_id, bib=target.bib, frame=FRAME,
                      crossing_ms=FRAME["recorder_ms"])


def test_repeated_classification_never_creates_rider_and_persists(tmp_path):
    path = tmp_path / "review.jsonl"
    store = ArrivalReviewStore(path)
    for _ in range(5):
        store.save(SCOPE, "candidate:1", "frame", "bicycle")
    assert [entry["action"] for entry in store.active(SCOPE)] == ["bicycle"]
    assert ArrivalReviewStore(path).latest(SCOPE, "candidate:1")["action"] == "bicycle"


def test_same_event_deduplicates_but_same_bib_different_event_does_not(tmp_path):
    store = ArrivalReviewStore(tmp_path / "review.jsonl")
    first = confirm(store)
    assert confirm(store)["id"] == first["id"]
    confirm(store, event(identity="next-lap"))
    assert len(store.active(SCOPE)) == 2


def test_undo_restores_previous_decision_and_is_scoped(tmp_path):
    path = tmp_path / "review.jsonl"
    store = ArrivalReviewStore(path)
    store.save(SCOPE, "candidate:1", "frame", "bicycle")
    confirm(store)
    store.save(("other", "stage"), "candidate:1", "frame", "held")
    assert store.undo(SCOPE)["action"] == "confirm"
    restored = ArrivalReviewStore(path)
    assert restored.latest(SCOPE, "event:one") is None
    assert restored.latest(SCOPE, "candidate:1")["action"] == "bicycle"
    assert restored.latest(("other", "stage"), "candidate:1")["action"] == "held"


def test_event_changes_invalidate_old_binding(tmp_path):
    store = ArrivalReviewStore(tmp_path / "review.jsonl")
    target = event()
    confirm(store, target)
    for changed in (replace(target, bib="258"), replace(target, revision=2),
                    replace(target, passage_timestamp_ms=1_800_000_001_100),
                    replace(target, is_active=False)):
        assert store.latest(SCOPE, "event:one", event_binding(changed)) is None


def test_invalid_frame_and_failed_write_leave_no_confirmation(tmp_path, monkeypatch):
    import realtime.arrival_review as module
    store = ArrivalReviewStore(tmp_path / "review.jsonl")
    with pytest.raises(ValueError):
        store.save(SCOPE, "event:one", "binding", "confirm", event_id="one", bib="321")
    monkeypatch.setattr(module, "append_jsonl_records", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        confirm(store)
    assert store.active(SCOPE) == []


def test_partial_tail_recovered_before_next_append(tmp_path):
    path = tmp_path / "review.jsonl"
    store = ArrivalReviewStore(path)
    store.save(SCOPE, "candidate:1", "frame", "bicycle")
    with path.open("ab") as output:
        output.write(b'{"action":"con')
    confirm(ArrivalReviewStore(path))
    assert len([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]) == 2


def test_two_store_instances_do_not_duplicate_same_event(tmp_path):
    path = tmp_path / "review.jsonl"
    first, second = ArrivalReviewStore(path), ArrivalReviewStore(path)
    assert confirm(first)["id"] == confirm(second)["id"]
    first.refresh()
    assert len(first.active(SCOPE)) == 1


def test_changed_candidate_can_be_confirmed_again_and_undone(tmp_path):
    store = ArrivalReviewStore(tmp_path / "review.jsonl")
    target = event()
    values = dict(event_id=target.event_id, bib=target.bib, frame=FRAME,
                  crossing_ms=FRAME["recorder_ms"])
    first = store.save(SCOPE, "event:one", event_binding(target), "confirm",
                       candidate_binding="old-evidence", **values)
    second = store.save(SCOPE, "event:one", event_binding(target), "confirm",
                        candidate_binding="new-evidence", **values)
    assert first["id"] != second["id"]
    assert store.latest(SCOPE, "event:one")["id"] == second["id"]
    store.undo(SCOPE)
    assert store.latest(SCOPE, "event:one")["id"] == first["id"]
