"""自动对上异常恢复与新旧元数据联机兼容回归。"""

import json
from dataclasses import replace

import pytest

from realtime.arrival_review import ArrivalReviewStore
from realtime.passage_receiver import (
    PassageEventDeliveryError, PassageEventStore, _RaceMetadataIngestor,
)
from realtime.race_metadata import RaceGroupMetadata, RaceMetadata, RaceMetadataStore
from realtime.video_timeline import VideoTimelineStore
from realtime.workspace_context import EventWorkspaceMetadataStore
from tests.realtime.test_arrival_review import SCOPE
from tests.realtime.test_auto_label import START, workspace
from tests.realtime.test_passage_receiver import metadata_payload
from tests.realtime.test_quick_review_panel import (
    auto_people, look_through, qapp, row, saved, states, wave_panel,
)


def test_enriched_metadata_is_delivered_and_callback_can_retry(tmp_path):
    store = RaceMetadataStore(tmp_path / "metadata.json")
    old = RaceMetadata.from_payload(metadata_payload(
        revision=1, groups=[{"group_id": "1", "name": "男子精英组", "target_laps": 4}]))
    store.store(old)
    attempts = []

    def deliver(metadata):
        attempts.append(metadata)
        if len(attempts) == 1:
            raise RuntimeError("retry")

    ingest = _RaceMetadataIngestor(store, deliver)
    incoming = RaceMetadata.from_payload(metadata_payload(revision=2, race_name="更新赛事名"))
    with pytest.raises(PassageEventDeliveryError):
        ingest.ingest(incoming)
    assert ingest.ingest(incoming).value == "duplicate"
    assert len(attempts) == 2
    assert attempts[-1] == store.current()
    assert attempts[-1].race_name == "更新赛事名"
    assert attempts[-1].groups[0].target_laps == 4
    ingest.ingest(incoming)
    ingest.ingest(old)
    assert len(attempts) == 2


def test_workspace_selection_keeps_enriched_metadata_and_rejects_stale_candidate(tmp_path):
    store = EventWorkspaceMetadataStore(
        tmp_path / "metadata.json", PassageEventStore(tmp_path / "events.jsonl"),
        VideoTimelineStore(tmp_path / "timeline.jsonl"))
    known = RaceMetadata(race_id="race", stage_id="stage", revision=1, emitted_at_ms=1,
                         groups=(RaceGroupMetadata("g", "组", 4),))
    store.store(known)
    incoming = replace(known, revision=2, emitted_at_ms=2, race_name="新名称",
                       groups=(RaceGroupMetadata("g", "组"),))
    store.store(incoming)
    enriched = store.current()
    assert enriched.groups[0].target_laps == 4
    store.store(known)
    store.store(replace(known, race_id="other"))
    assert store.select("race", "stage") == enriched
    assert store.current().revision == 2
    assert store.current().race_name == "新名称"
    assert store.current().groups[0].target_laps == 4


def test_operation_undo_never_removes_deduplicated_or_later_unrelated_entries(tmp_path):
    store = ArrivalReviewStore(tmp_path / "review.jsonl")
    old = store.save(SCOPE, "candidate:old", "frame", "bicycle", operation_id="old")
    assert store.save(SCOPE, "candidate:old", "frame", "bicycle", operation_id="new") == old
    assert store.undo(SCOPE, operation_id="new") is None
    added = store.save(SCOPE, "candidate:new", "new-frame", "bicycle", operation_id="new")
    later = store.save(SCOPE, "candidate:later", "frame", "held", operation_id="later")
    assert store.undo(SCOPE, operation_id="new")["id"] == added["id"]
    assert store.active(SCOPE) == [old, later]
    assert ArrivalReviewStore(store.path).active(SCOPE) == [old, later]


def test_empty_strip_and_in_place_repair_are_rechecked_without_per_render_scan(tmp_path, monkeypatch):
    from realtime import auto_label as module

    base = workspace(tmp_path, [(START, 0, False)])
    strip = next((base / "finish_problems" / "strips" / "camera_01").glob("*.npz"))
    valid = strip.read_bytes()
    strip.write_bytes(b"")
    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    assert module.people_on_line(base, 1) == []
    strip.write_bytes(valid)
    now[0] = module.STRIP_RECHECK_SECONDS + 1
    assert len(module.people_on_line(base, 1)) == 1
    original = module._stamp
    checked = []

    def stamp(path):
        if str(path).endswith(".npz"):
            checked.append(path)
        return original(path)

    monkeypatch.setattr(module, "_stamp", stamp)
    for _ in range(10):
        assert len(module.people_on_line(base, 1)) == 1
    assert checked == []
    now[0] += module.STRIP_RECHECK_SECONDS + 1
    module.people_on_line(base, 1)
    assert len(checked) == 1


def test_auto_accept_persists_its_review_method(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    wave_panel._enter_key()
    confirms = [entry for entry in wave_panel._arrival_store.active(SCOPE) if entry["action"] == "confirm"]
    assert len(confirms) == 3
    assert all(entry["auto_label"] is True for entry in confirms)
    assert all(entry["review_method"] == "auto_label_operator_accepted" for entry in confirms)


def test_auto_candidate_creation_error_stays_in_hint(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    monkeypatch.setattr(wave_panel, "picture_candidate", lambda *_args: (None, ""))

    def fail(*args, **kwargs):
        raise OSError("模拟创建失败")

    monkeypatch.setattr(wave_panel, "add_manual_problem", fail)
    wave_panel._enter_key()
    assert "模拟创建失败" in wave_panel.hint.text()
    assert wave_panel.holds_picture is False
    assert saved(wave_panel) == {}


def test_auto_rejected_marks_leave_no_new_empty_candidate(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    initial = {item.item_id for item in wave_panel.items}
    monkeypatch.setattr(wave_panel, "add_mark", lambda *_args, **_kwargs: None)
    wave_panel._enter_key()
    assert "未保存" in wave_panel.hint.text()
    assert "完整添加" in wave_panel.hint.text()
    assert saved(wave_panel) == {}
    assert not any(item.item_id not in initial and item.item_id.startswith("manual:")
                   for item in wave_panel.items)
    path = wave_panel.problem_dir / "manual_problems.json"
    assert not path.exists() or json.loads(path.read_text(encoding="utf-8")) == []
    assert states(wave_panel)["61"] == "自动对上"


@pytest.mark.parametrize("undo_fails", [False, True])
def test_auto_partial_write_rollback_is_scoped_and_visible(wave_panel, monkeypatch, undo_fails):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    store = wave_panel._arrival_store
    old = store.save(SCOPE, "candidate:old", "old", "held")
    original = store.save
    count = [0]

    def save(*args, **kwargs):
        if args[3] == "confirm":
            count[0] += 1
            if count[0] == 2:
                original(SCOPE, "candidate:later", "later", "held")
                raise OSError("模拟第二人写入失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "save", save)
    if undo_fails:
        def undo(*args, **kwargs):
            raise OSError("模拟撤回失败")
        monkeypatch.setattr(store, "undo", undo)
    wave_panel._enter_key()
    active = ArrivalReviewStore(store.path).active(SCOPE)
    assert old in active
    assert any(entry["key"] == "candidate:later" for entry in active)
    confirms = [entry for entry in active if entry["action"] == "confirm"]
    assert len(confirms) == (1 if undo_fails else 0)
    assert "模拟第二人写入失败" in wave_panel.hint.text()
    if undo_fails:
        assert "勿重复点人" in wave_panel.hint.text()
        assert any(wave_panel._marks.values())
    else:
        assert not any(wave_panel._marks.values())
        assert states(wave_panel)["61"] == "自动对上"


def test_wave_plan_orders_sessions_by_aligned_time(wave_panel, monkeypatch):
    from realtime import quick_review_panel as module
    from realtime.crossing_time import CrossingTime
    from tests.realtime.test_quick_review_panel import T0

    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    aligned = {}
    changed = []
    for index, person in enumerate(people):
        recorder = T0 + 5000 - index * 1000
        frame = dict(person.frame, recorder_ms=recorder, session_id=f"s{index}")
        changed.append(replace(person, recorder_ms=recorder, frame=frame))
        aligned[f"s{index}"] = person.chip_ms
    original = module.recorder_crossing_time

    def crossing(recorder, frame, *args, **kwargs):
        if frame.get("session_id") in aligned:
            return CrossingTime(aligned[frame["session_id"]], "aligned")
        return original(recorder, frame, *args, **kwargs)

    monkeypatch.setattr(module, "recorder_crossing_time", crossing)
    wave_panel._auto_labels = dict(zip([entry.key for entry in wave_panel._waves[0]], changed))
    wave_panel._enter_key()
    assert saved(wave_panel) == {"61": 5000, "20": 4000, "76": 3000}


def test_auto_status_and_enter_use_exact_same_view_interval(wave_panel, monkeypatch):
    from tests.realtime.test_quick_review_panel import T0

    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    wave_panel._arrival_drafts["seen"] = {"1": [[T0 - 500, T0 + 1400]]}
    wave_panel._sync_arrival_confirmation()
    assert "没看" in wave_panel.confirmation_status.text()
    wave_panel._enter_key()
    assert saved(wave_panel) == {}
    assert "没看" in wave_panel.hint.text()
    wave_panel._arrival_drafts["seen"] = {"1": [[T0 - 700, T0 + 1600]]}
    wave_panel._sync_arrival_confirmation()
    assert "已看完" in wave_panel.confirmation_status.text()
    wave_panel._enter_key()
    assert len(saved(wave_panel)) == 3


def test_auto_duplicate_is_rejected_before_creating_candidate(wave_panel, monkeypatch):
    people = auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    person = people[0]
    mark = dict(x=person.x, y=person.y, recorder_ms=person.recorder_ms, frame=person.frame)
    monkeypatch.setattr(wave_panel, "picture_marks", lambda: [("other", mark)])
    wave_panel._enter_key()
    assert "已点过" in wave_panel.hint.text()
    assert saved(wave_panel) == {}
    assert not (wave_panel.problem_dir / "manual_problems.json").exists()


def test_live_chip_change_rolls_back_with_actionable_message(wave_panel, monkeypatch):
    auto_people(wave_panel, monkeypatch, (100, 500, 1000))
    wave_panel._arrival_clicked(row(wave_panel, "61"))
    look_through(wave_panel, -700, 2100)
    original = wave_panel._save_wave

    def save_after_chip_changes(plan, **kwargs):
        # Candidate creation refreshes rows first; change the chip only once
        # the plan exists, to exercise a change between planning and saving.
        wave_panel.events[0] = replace(wave_panel.events[0], revision=2)
        return original(plan, **kwargs)

    monkeypatch.setattr(wave_panel, "_save_wave", save_after_chip_changes)
    wave_panel._enter_key()
    assert "芯片记录已变化" in wave_panel.hint.text()
    assert "61" in wave_panel.hint.text()
    assert saved(wave_panel) == {}
    assert not any(wave_panel._marks.values())


def test_lap_count_refreshes_when_only_lap_changes(wave_panel):
    from tests.realtime.test_quick_review_panel import three_laps

    three_laps(wave_panel)
    wave_panel.events = [replace(event, lap=3) for event in wave_panel.events]
    wave_panel.refresh()
    assert wave_panel.table.item(row(wave_panel, "61"), 5).text() == "3/3"
    wave_panel.events[0] = replace(wave_panel.events[0], lap=4)
    wave_panel.refresh()
    assert wave_panel.table.item(row(wave_panel, "61"), 5).text() == "4/3"


def test_refusal_reason_refreshes_without_row_or_pair_changes(wave_panel, monkeypatch):
    from realtime import quick_review_panel as module

    people = auto_people(wave_panel, monkeypatch, (100, 500))
    tip = lambda: wave_panel.table.item(row(wave_panel, "61"), 3).toolTip()
    assert "视线被挡" not in tip()
    monkeypatch.setattr(module, "people_on_line", lambda *_args: [replace(p, in_obstruction=True) for p in people])
    wave_panel._render()
    assert "视线被挡" in tip()


def test_same_revision_conflicting_target_is_rejected(tmp_path):
    from realtime.race_metadata import RaceMetadataConflictError

    store = RaceMetadataStore(tmp_path / "metadata.json")
    first = RaceMetadata(race_id="r", stage_id="s", revision=1, emitted_at_ms=1,
                         groups=(RaceGroupMetadata("g", "组", 4),))
    store.store(first)
    with pytest.raises(RaceMetadataConflictError):
        store.store(replace(first, groups=(RaceGroupMetadata("g", "组", 5),)))
    assert store.current() == first
