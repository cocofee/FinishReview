import csv
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from realtime.batch_dialog import BatchReviewDialog
from realtime.batch_review import (BatchDecisionStore, aggregate_batches, member_key, read_snapshots,
                                   unambiguous_auto_passed)
from realtime.chip_checks import chip_check_items
from realtime.chip_history import ChipRead
from realtime.clock_sync import ClockModel
from realtime.live_chip_checks import merge_chip_snapshot
from realtime.problem_items import KIND_BATCH_READS, ProblemItem, CATEGORY_CHIP
from realtime.problem_panel import ProblemListPanel
from realtime.problem_resolutions import ProblemResolutionStore, export_lists
from realtime.problem_rules import CrossingRecord, build_video_items


T = 1_789_784_000_000
SCOPE = ("race", "stage")


def reads(count=8, start=0):
    return [ChipRead(f"event-{i}", str(i), f"chip-{i}", "g", "race group", "", T+i*20,
                     i*20, 0, T+i*20, 1, 1, True, False) for i in range(start, count)]


def batch(values=None, previous=()):
    values = reads() if values is None else values
    items = aggregate_batches(chip_check_items(values), read_snapshots(values), SCOPE, previous)
    return next(i for i in items if i.evidence.get("batch_members"))


def event(member, **changes):
    values = dict(event_id=member["event_id"], bib=member["bib"], chip_id=member["chip_id"],
                  timeline_timestamp_ms=member["chip_ms"], revision=member["revision"], is_active=True)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_eight_chips_one_crossing_becomes_one_batch_not_overlapping_tasks():
    values = reads()
    crossing = CrossingRecord(1, "session", "segment", "synthetic.mkv", T+1000, 20, 1000, 100, 80, 120)
    model = ClockModel(camera_index=1, intercept_ms=1000, slope=0, reference_ms=T,
                       valid_from_ms=T, valid_to_ms=T+60000, support=50, candidates=60,
                       residual_mad_ms=25, coarse_offset_ms=1000, coarse_support=100)
    video = build_video_items(camera_index=1, model=model, matched=[(crossing, values[0], 0)],
                             chip_only=values[1:], video_only=[], no_video=[])
    items = aggregate_batches([*chip_check_items(values), *video.items], read_snapshots(values), SCOPE)
    top = [i for i in items if i.pending]
    assert len(top) == 1
    assert top[0].evidence["chip_count"] == 8
    assert top[0].evidence["video_count"] == 1
    assert len(top[0].evidence["batch_members"]) == 8
    assert all(i.grouped == top[0].item_id for i in items if i != top[0])
    assert unambiguous_auto_passed(items, video.auto_passed) == []


def test_bounded_bursts_do_not_chain_across_whole_sprint():
    values = reads(140)
    items = chip_check_items(values)
    groups = [i for i in items if i.kind == KIND_BATCH_READS]
    assert len(groups) >= 3
    used = []
    by_id = {r.event_id: r for r in values}
    for item in groups:
        times = [by_id[e].chip_ms for e in item.event_ids]
        assert max(times)-min(times) <= 1000
        used.extend(item.event_ids)
    assert len(used) == len(set(used))


def test_duplicate_transport_and_normal_sprint_are_not_batch():
    assert not any(i.kind == KIND_BATCH_READS for i in chip_check_items(reads(1)*8))
    sprint = [replace(r, chip_ms=T+i*110) for i, r in enumerate(reads(9))]
    assert not any(i.kind == KIND_BATCH_READS for i in chip_check_items(sprint))


def test_late_earlier_member_keeps_identity_and_does_not_inherit_decision(tmp_path):
    first = batch(reads(8, start=1))
    store = BatchDecisionStore(tmp_path)
    member = first.evidence["batch_members"][0]
    store.save(first, [member["event_id"]], "keep", "verified")
    grown = batch(reads(), [first])
    assert grown.item_id == first.item_id
    assert grown.evidence["batch_revision"] == 2
    assert store.state(grown, member) == "已确认有效"
    newcomer = next(m for m in grown.evidence["batch_members"] if m["event_id"] == "event-0")
    assert store.state(grown, newcomer) == "待处理"
    assert batch(reads(), [grown]).evidence["batch_revision"] == 2


def test_member_deletions_export_exact_ids_and_partial_upstream_close(tmp_path):
    item = batch()
    members = item.evidence["batch_members"]
    store = BatchDecisionStore(tmp_path)
    selected = members[:2]
    store.save(item, [m["event_id"] for m in selected], "delete", "one rider carrying chips")
    resolutions = ProblemResolutionStore(tmp_path / "problem_resolutions.jsonl")
    output = tmp_path / "exports"
    assert export_lists([item], resolutions, output)["建议删除清单.csv"] == 2
    rows = list(csv.DictReader((output / "建议删除清单.csv").open(encoding="utf-8-sig")))
    assert {row["事件ID"] for row in rows} == {m["event_id"] for m in selected}
    assert {row["记录修订"] for row in rows} == {"1"}
    assert all(json.loads(row["证据快照JSON"])["scope"] == list(SCOPE) for row in rows)
    events = [event(selected[0], is_active=False, revision=2)]
    assert store.state(item, selected[0], events) == "上游已作废"
    assert store.state(item, selected[1], events) == "待 CycleRace"
    assert store.state(item, members[2], events) == "待处理"
    assert export_lists([item], resolutions, output, events=events)["建议删除清单.csv"] == 1
    assert BatchDecisionStore(tmp_path).latest == store.latest


def test_revision_change_is_not_deletion_and_invalidates_export(tmp_path):
    item = batch()
    member = item.evidence["batch_members"][0]
    store = BatchDecisionStore(tmp_path)
    store.save(item, [member["event_id"]], "delete", "reviewed")
    changed = [event(member, revision=2)]
    assert store.state(item, member, changed) == "需要重查"
    with pytest.raises(ValueError):
        store.save(item, [member["event_id"]], "keep", "reason", changed)
    resolutions = ProblemResolutionStore(tmp_path / "problem_resolutions.jsonl")
    assert export_lists([item], resolutions, tmp_path / "out", events=changed)["建议删除清单.csv"] == 0
    # A partial snapshot's absence is not evidence of upstream deletion.
    assert store.state(item, member, []) == "待 CycleRace"


def test_missing_member_is_waiting_not_deleted_and_old_decision_is_preserved(tmp_path):
    first = batch()
    store = BatchDecisionStore(tmp_path)
    member = first.evidence["batch_members"][0]
    store.save(first, [member["event_id"]], "delete", "reason")
    changed = batch(reads(8, start=1), [first])
    absent = next(m for m in changed.evidence["batch_members"] if m["event_id"] == member["event_id"])
    assert not absent["present"]
    assert store.state(changed, absent) == "待证据"
    assert member_key(first, member) in store.latest


def test_atomic_multiselect_invalid_member_and_failure_do_not_partially_save(tmp_path, monkeypatch):
    from realtime import batch_review
    item = batch()
    member = item.evidence["batch_members"][0]
    store = BatchDecisionStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(item, [member["event_id"], "outsider"], "delete", "reason")
    assert not store.path.exists()
    monkeypatch.setattr(batch_review, "append_jsonl_records", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.save(item, [m["event_id"] for m in item.evidence["batch_members"][:2]], "delete", "reason")
    assert store.latest == {}


def test_legacy_delete_never_falls_back_to_first_event(tmp_path):
    store = ProblemResolutionStore(tmp_path / "problem_resolutions.jsonl")
    item = ProblemItem("legacy", KIND_BATCH_READS, CATEGORY_CHIP, "legacy", bibs=("a", "b"), event_ids=("e1", "e2"))
    for target in ("outsider", ""):
        with pytest.raises(ValueError):
            store.resolve(item, "delete_chip", target_bib=target, reason="reason")
    old = dict(schema_version=1, item_id="legacy", action="delete_chip", target_bib="outsider",
               binding={"bibs": ["a", "b"], "event_ids": ["e1", "e2"], "chip_time_ms": None})
    store.journal_path.write_text(json.dumps(old)+"\n")
    before = store.journal_path.read_bytes()
    store = ProblemResolutionStore(store.journal_path)
    assert export_lists([item], store, tmp_path / "out")["建议删除清单.csv"] == 0
    assert store.journal_path.read_bytes() == before


def test_legal_multi_chip_binding_is_displayed_not_auto_deleted(tmp_path):
    values = reads()
    metadata = SimpleNamespace(athletes=[SimpleNamespace(bib="0", chip_ids=["chip-0", "spare-0"])])
    snapshots = read_snapshots(values, metadata)
    assert snapshots["event-0"]["legal_chip_ids"] == ["chip-0", "spare-0"]
    item = next(i for i in aggregate_batches(chip_check_items(values), snapshots, SCOPE) if i.evidence.get("batch_members"))
    assert all(BatchDecisionStore(tmp_path).state(item, m) == "待处理" for m in item.evidence["batch_members"])


def test_dialog_requires_selection_and_rejects_upstream_change(qapp, tmp_path):
    item = batch()
    current = [item]
    events = []
    dialog = BatchReviewDialog(tmp_path, item, lambda _id: current[0], lambda: events, lambda frame: None)
    dialog.reason.setText("reviewed")
    dialog._save("delete")
    assert not dialog.saved
    dialog.table.item(0, 0).setCheckState(Qt.Checked)
    events.append(event(dialog.members[0], revision=2))
    dialog._save("delete")
    assert not dialog.saved
    events.clear()
    dialog._save("keep")
    assert dialog.saved
    assert dialog.store.state(item, dialog.members[0]) == "已确认有效"
    assert dialog.store.state(item, dialog.members[1]) == "待处理"
    dialog.close()


def test_panel_partial_state_and_batch_actions_cannot_close_whole_batch(qapp, tmp_path):
    item = batch()
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    (directory / "problem_list.json").write_text(json.dumps(dict(race_id="race", stage_id="stage", items=[item.to_payload()])))
    panel = ProblemListPanel()
    panel.set_scope(*SCOPE)
    panel.set_workspace(tmp_path)
    assert panel.batch_button.isVisibleTo(panel)
    assert not panel.action_buttons
    panel.batch_store.save(item, [item.event_ids[0]], "keep", "verified")
    panel.refresh()
    assert panel.state(item).startswith("部分处理")
    assert panel.is_pending_state(panel.state(item))
    panel._resolve("delete_chip")
    assert "成员" in panel.hint.text()
    panel.close()


def test_live_merge_keeps_one_batch_and_new_members_not_old_chip_snapshot():
    old = batch(reads(6))
    fresh = batch(reads(), [old])
    video = dict(race_id="race", stage_id="stage", items=[old.to_payload()], auto_passed=[])
    chips = dict(schema_version=1, race_id="race", stage_id="stage", items=[fresh.to_payload()], passage_members=read_snapshots(reads()))
    result = merge_chip_snapshot(video, chips)
    parents = [i for i in result["items"] if i["evidence"].get("batch_members")]
    assert len(parents) == 1
    assert len(parents[0]["event_ids"]) == 8
    assert result["summary"]["pending"] == 1


def test_scope_and_split_identity_do_not_transfer_entire_batch_decisions(tmp_path):
    original = batch(reads(12))
    shifted = [replace(r, chip_ms=r.chip_ms+(2000 if index >= 6 else 0)) for index, r in enumerate(reads(12))]
    items = aggregate_batches(chip_check_items(shifted), read_snapshots(shifted), SCOPE, [original])
    parents = [i for i in items if i.evidence.get("batch_members")]
    assert len(parents) == 2
    assert sum(i.item_id == original.item_id for i in parents) == 1
    assert all(len(i.event_ids) == 6 for i in parents)
    other = next(i for i in parents if i.item_id != original.item_id)
    assert original.item_id in other.evidence["related_batches"]
    different_race = aggregate_batches(chip_check_items(reads()), read_snapshots(reads()), ("other", "stage"), [original])
    assert all(i.item_id != original.item_id for i in different_race)


def test_changed_chip_time_blocks_old_video_batch_until_reanalysis():
    from realtime.problem_items import CATEGORY_VIDEO, KIND_TWO_CHIPS_ONE_CROSSING
    values = reads(2)
    source = ProblemItem("video-pair", KIND_TWO_CHIPS_ONE_CROSSING, CATEGORY_VIDEO, "pair",
                         chip_time_ms=T, bibs=("0", "1"), event_ids=("event-0", "event-1"),
                         evidence={"frame": {"camera_index": 1, "segment_id": "s", "frame_index": 20}})
    parents = aggregate_batches([source], read_snapshots(values), SCOPE)
    video = dict(race_id="race", stage_id="stage", items=[i.to_payload() for i in parents], passage_members=read_snapshots(values))
    updated = read_snapshots(values)
    updated["event-0"]["chip_ms"] += 100
    chips = dict(schema_version=1, race_id="race", stage_id="stage", items=[], passage_members=updated)
    merged = merge_chip_snapshot(video, chips)
    parent = next(i for i in merged["items"] if i["evidence"].get("batch_members"))
    assert parent["status"] == "waiting"
    assert parent["evidence"]["configuration_stale"]


def test_panel_identity_survives_late_member_restart_and_source_id_change(qapp, tmp_path):
    directory = tmp_path / "finish_problems"
    directory.mkdir()
    path = directory / "problem_list.json"
    first = batch(reads(7))
    def publish(item, values):
        path.write_text(json.dumps(dict(race_id="race", stage_id="stage", items=[item.to_payload()], passage_members=read_snapshots(values))))
    publish(first, reads(7))
    panel = ProblemListPanel()
    panel.set_scope(*SCOPE)
    panel.set_workspace(tmp_path)
    original = next(i for i in panel.items if i.evidence.get("batch_members"))
    panel.reason_input.setText("draft")
    panel.save_drafts()
    panel.close()
    fresh = batch(reads())  # independent publisher has a different set hash
    assert first.item_id != fresh.item_id
    publish(fresh, reads())
    panel = ProblemListPanel()
    panel.set_scope(*SCOPE)
    panel.set_workspace(tmp_path)
    grown = next(i for i in panel.items if i.evidence.get("batch_members"))
    assert grown.item_id == original.item_id
    assert len(grown.event_ids) == 8
    assert panel.reason_input.text() == "draft"
    panel.close()


def test_different_camera_counts_are_not_reduced_to_smaller_count():
    from realtime.problem_items import CATEGORY_VIDEO, KIND_BUNCH
    values = reads()
    sources = list(chip_check_items(values))
    for camera, count in ((1, 1), (2, 2)):
        sources.append(ProblemItem(str(camera), KIND_BUNCH, CATEGORY_VIDEO, "group", chip_time_ms=T,
                                   event_ids=tuple(r.event_id for r in values),
                                   evidence={"video_count": count, "frame": {"camera_index": camera}}))
    result = aggregate_batches(sources, read_snapshots(values), SCOPE)
    parent = next(i for i in result if i.evidence.get("batch_members"))
    assert parent.evidence["video_count"] is None
    assert "不一致" in parent.detail


def test_stale_configuration_cannot_export_member_decisions(tmp_path):
    item = batch()
    member = item.evidence["batch_members"][0]
    store = BatchDecisionStore(tmp_path)
    store.save(item, [member["event_id"]], "delete", "verified")
    changed = replace(item, evidence=dict(item.evidence, configuration_stale=True))
    assert store.state(changed, member) == "需要重查"
    with pytest.raises(ValueError):
        store.save(changed, [member["event_id"]], "delete", "again")
    resolutions = ProblemResolutionStore(tmp_path / "problem_resolutions.jsonl")
    assert export_lists([changed], resolutions, tmp_path / "out")["建议删除清单.csv"] == 0
