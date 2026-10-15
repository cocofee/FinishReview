"""Bounded multi-chip events and append-only decisions for individual passages."""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import replace
from pathlib import Path

from .durable_jsonl import append_jsonl_records, locked_jsonl
from .problem_items import (CATEGORY_CHIP, CATEGORY_VIDEO, KIND_BATCH_READS, KIND_BUNCH,
                            KIND_OBSTRUCTION, KIND_TWO_CHIPS_ONE_CROSSING, STATUS_CLOSED_UPSTREAM,
                            STATUS_OPEN, ProblemItem, sort_items, stable_item_id)
from .problem_rules import BUNCH_MARGIN_MS

MAX_BATCH_SPAN_MS = 1000
BATCH_DECISIONS = "batch_member_decisions.jsonl"


def read_snapshots(reads, metadata=None, latest_event=None):
    result = {}
    bindings = {a.bib: list(a.chip_ids) for a in metadata.athletes} if metadata else {}
    for read in sorted(reads, key=lambda r: (r.latest_revision, r.revision, r.active_now)):
        result[read.event_id] = dict(event_id=read.event_id, bib=read.bib, chip_id=read.chip_id,
                                     revision=read.latest_revision,
                                     chip_ms=(read.latest_chip_ms or read.chip_ms) if read.active_now else read.chip_ms, lap=read.lap,
                                     active=read.active_now, present=True, legal_chip_ids=bindings.get(read.bib, []))
        event = latest_event(read.event_id) if latest_event else None
        if event is not None:
            result[read.event_id].update(bib=event.bib, chip_id=event.chip_id, revision=event.revision,
                                         chip_ms=int(event.timeline_timestamp_ms), lap=event.lap,
                                         legal_chip_ids=bindings.get(event.bib, []))
    return result


def aggregate_batches(items, snapshots, scope, previous=()):
    """Unify overlapping chip/video discoveries, retaining source items as children.

    Identity follows overlap with the previous snapshot, not a hash of the new
    member set. A split inherits an old id at most once; all branches keep links.
    """
    scope = list(scope)
    if not snapshots:
        snapshots = {m["event_id"]: m for i in items for m in i.evidence.get("batch_members", []) if m.get("present", True)}
    # Expand previous aggregates before merging a fresh chip-only snapshot.
    raw = {}
    for item in items:
        if item.evidence.get("batch_sources"):
            for value in item.evidence["batch_sources"]:
                child = ProblemItem.from_payload(value)
                raw.setdefault(child.item_id, child)
        else:
            raw[item.item_id] = item
    seeds = sorted((i for i in raw.values() if i.kind in (KIND_BATCH_READS, KIND_TWO_CHIPS_ONE_CROSSING)
                    and len(set(i.event_ids)) >= 2), key=lambda i: (i.chip_time_ms or 0, i.item_id))
    components = []
    for seed in seeds:
        ids = set(seed.event_ids)
        times = [snapshots[e]["chip_ms"] for e in ids if e in snapshots] or [seed.chip_time_ms or 0]
        matches = [c for c in components if c[0] & ids
                   and max(c[2] + times) - min(c[2] + times) <= MAX_BATCH_SPAN_MS]
        if matches:
            # Bound the entire union, not just each adjacent pair.
            target = matches[0]
            target[0].update(ids)
            target[1].append(seed)
            target[2].extend(times)
            for other in matches[1:]:
                if max(target[2] + other[2]) - min(target[2] + other[2]) <= MAX_BATCH_SPAN_MS:
                    target[0].update(other[0])
                    target[1].extend(other[1])
                    target[2].extend(other[2])
                    components.remove(other)
        else:
            components.append([ids, [seed], times])
    old = [i for i in previous if i.evidence.get("batch_scope") == scope]
    used_old, parents, grouped = set(), [], {}
    assigned = set().union(*(c[0] for c in components)) if components else set()
    for ids, sources, times in components:
        candidates = sorted((i for i in old if set(i.event_ids) & ids),
                            key=lambda i: (-len(set(i.event_ids) & ids), i.item_id))
        inherited = next((i for i in candidates if i.item_id not in used_old), None)
        identity = inherited.item_id if inherited else stable_item_id("chip_batch", *scope, *sorted(ids))
        used_old.add(identity)
        attached = [i for i in raw.values() if i.event_ids and set(i.event_ids) <= ids
                    and (i.category == CATEGORY_CHIP or i.kind in (KIND_TWO_CHIPS_ONE_CROSSING, KIND_BUNCH))]
        video = [i for i in attached if i.category == CATEGORY_VIDEO]
        anchor = next((i for i in video if i.evidence.get("frame")), None)
        counts = [i.evidence["video_count"] for i in video if i.kind == KIND_BUNCH and "video_count" in i.evidence]
        if not counts:
            per_camera = {}
            for item in video:
                frame = item.evidence.get("frame")
                if item.kind == KIND_TWO_CHIPS_ONE_CROSSING and frame:
                    per_camera.setdefault(frame.get("camera_index"), set()).add(
                        (frame.get("segment_id"), frame.get("frame_index"), item.evidence.get("row")))
            counts = [len(frames) for frames in per_camera.values()]
        count = counts[0] if counts and len(set(counts)) == 1 else None
        members = [dict(snapshots[e]) for e in sorted(ids) if e in snapshots]
        if inherited:
            members += [dict(m, present=False) for m in inherited.evidence.get("batch_members", []) if m["event_id"] not in assigned]
        active = sum(m.get("active", True) for m in members if m.get("present", True))
        evidence = dict(anchor.evidence if anchor else {})
        evidence.pop("group", None)
        video_evidence = sorted({json.dumps({"frame": i.evidence.get("frame", {}), "version": i.calibration_version,
                                            "signature": i.evidence.get("analysis_signature", "")}, sort_keys=True)
                                 for i in video})
        if video_evidence:
            evidence["analysis_signature"] = hashlib.sha256(json.dumps(video_evidence).encode()).hexdigest()
        evidence["batch_video_evidence"] = [json.loads(value) for value in video_evidence]
        stale = any(i.evidence.get("member_evidence_stale") for i in video)
        if stale:
            evidence["configuration_stale"] = True
        evidence.update(batch_scope=scope, batch_members=members, batch_sources=[i.to_payload() for i in attached],
                        related_batches=sorted({i.item_id for i in candidates if i.item_id != identity}),
                        chip_count=active, video_count=count, video_counts=counts,
                        batch_revision=(inherited.evidence.get("batch_revision", 1) +
                                        int(inherited.evidence.get("batch_members") != members)) if inherited else 1)
        detail = f"{max(times)-min(times)} ms 内 {active} 条有效芯片记录（共 {len(members)} 条历史成员）；"
        detail += ("不同机位视频计数不一致，证据有歧义，不能据此删除" if len(set(counts)) > 1 else
                   f"视频候选 {count} 次，疑似多芯片，须人工核实" if count is not None and count < active
                   else "视频存在对应歧义，不能据此删除" if video else "密集读数，人数待核；不能仅凭密度认定携带芯片")
        if stale:
            detail += "；成员身份或时间已改变，等待视频重新分析"
        parent = ProblemItem(identity, KIND_BATCH_READS, CATEGORY_VIDEO if video else CATEGORY_CHIP, detail,
                             chip_time_ms=min(times), bibs=tuple(m["bib"] for m in members),
                             event_ids=tuple(m["event_id"] for m in members),
                             status="waiting" if stale else STATUS_CLOSED_UPSTREAM if members and not active else STATUS_OPEN,
                             priority=15, evidence=evidence, calibration_version=anchor.calibration_version if anchor else 0)
        parents.append(parent)
        for item in attached:
            grouped[item.item_id] = identity
    children = []
    for item in raw.values():
        group = grouped.get(item.item_id) or grouped.get(item.grouped)
        children.append(replace(item, evidence=dict(item.evidence, group=group)) if group else item)
    return sort_items(children + parents)


def unambiguous_auto_passed(items, auto_passed):
    """Matches that need no operator - counted once per valid passage, and marked.

    One passage event counted twice (two cameras, or a repeated delivery) is one
    rider, not two.  A match that falls inside a period still waiting for a
    whole-group check is kept: riders with their own crossing evidence are not
    sent back to manual confirmation.  It is only marked, so the count of
    "no operator needed" never silently covers an unverified group.
    """

    involved = {event_id for item in items if item.evidence.get("batch_members") for event_id in item.event_ids}
    periods = [item for item in items
               if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION) and item.pending
               and item.evidence.get("recorder_start_ms") is not None]
    seen: set = set()
    result = []
    for entry in auto_passed:
        if entry.get("event_id") in involved:
            continue
        key = entry.get("event_id") or f"{entry.get('bib')}|{entry.get('chip_time_ms')}"
        if key in seen:
            continue
        seen.add(key)
        fresh = {name: value for name, value in entry.items()
                 if name not in ("pending_period", "pending_period_note")}
        period = _covering_period(fresh, periods)
        result.append(fresh if period is None else dict(
            fresh, pending_period=period.item_id,
            pending_period_note=f"{period.title}：{period.detail.split('；')[0]}"))
    return result


def _covering_period(entry, periods):
    recorder_ms = entry.get("recorder_ms")
    camera = (entry.get("frame") or {}).get("camera_index")
    if recorder_ms is None or camera is None:
        return None
    for item in periods:
        if int(item.evidence.get("camera_index", 0) or 0) != int(camera):
            continue
        if int(item.evidence["recorder_start_ms"]) - BUNCH_MARGIN_MS <= int(recorder_ms) \
                <= int(item.evidence.get("recorder_end_ms", item.evidence["recorder_start_ms"])) + BUNCH_MARGIN_MS:
            return item
    return None


def member_key(batch, member):
    return stable_item_id("batch_member", *batch.evidence["batch_scope"], member["event_id"])


def member_binding(batch, member):
    return {"scope": batch.evidence["batch_scope"],
            "member": {key: member.get(key) for key in ("event_id", "bib", "chip_id", "revision", "chip_ms", "lap")},
            "frame": batch.evidence.get("frame", {}), "calibration_version": batch.calibration_version,
            "video_evidence": batch.evidence.get("batch_video_evidence", []),
            "analysis_signature": batch.evidence.get("analysis_signature", "")}


def member_state(batch, member, decision, events=()):
    if batch.evidence.get("configuration_stale") or batch.evidence.get("superseded"):
        return "需要重查"
    current = next((e for e in events if e.event_id == member["event_id"]), None)
    if current is not None:
        if (current.bib != member["bib"] or int(current.timeline_timestamp_ms) != member["chip_ms"]
                or current.chip_id != member.get("chip_id", "") or getattr(current, "lap", member["lap"]) != member["lap"]):
            return "需要重查"
        if not current.is_active:
            return "上游已作废"
        if not member.get("active", True) and member.get("present", True):
            return "需要重查"
        if current.revision != member["revision"]:
            return "需要重查"
    elif not member.get("present", True):
        return "待证据"
    elif not member.get("active", True):
        return "上游已作废"
    if not decision or decision["action"] == "reopen":
        return "待处理"
    if decision["binding"] != member_binding(batch, member):
        return "需要重查"
    return {"delete": "待 CycleRace", "keep": "已确认有效", "hold": "暂存"}[decision["action"]]


class BatchDecisionStore:
    """One durable transaction per selection; old whole-item logs are untouched."""

    def __init__(self, directory):
        self.path = Path(directory) / BATCH_DECISIONS
        self.latest = {}
        self.load()

    def load(self):
        latest = {}
        lines = self.path.read_bytes().splitlines(keepends=True) if self.path.exists() else []
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
                if record["schema_version"] != 1:
                    raise ValueError("不支持的批次决定版本")
                for decision in record["decisions"]:
                    latest[decision["key"]] = decision
            except (ValueError, KeyError, TypeError):
                if index == len(lines)-1 and not line.endswith(b"\n"):
                    break
                raise ValueError("批次成员日志损坏，未覆盖旧决定")
        self.latest = latest

    def state(self, batch, member, events=()):
        return member_state(batch, member, self.latest.get(member_key(batch, member)), events)

    def save(self, batch, event_ids, action, reason, events=()):
        if action not in ("delete", "keep", "hold", "reopen") or not reason.strip():
            raise ValueError("请选择有效操作并填写原因")
        members = {m["event_id"]: m for m in batch.evidence.get("batch_members", [])}
        selected = list(dict.fromkeys(event_ids))
        if not selected or any(e not in members for e in selected):
            raise ValueError("必须选择本批次的明确成员记录")
        decisions = []
        for event_id in selected:
            member = members[event_id]
            if (not member.get("present", True) or batch.status == "waiting"
                    or self.state(batch, member, events) in ("上游已作废", "待证据")
                    or member_state(batch, member, None, events) == "需要重查"):
                raise ValueError("成员或证据已变更，请刷新后重新核对")
            decisions.append(dict(key=member_key(batch, member), batch_id=batch.item_id, action=action,
                                  reason=reason.strip(), binding=member_binding(batch, member),
                                  decided_at_ms=int(time.time()*1000)))
        with locked_jsonl(self.path):
            append_jsonl_records(self.path, [json.dumps(dict(schema_version=1, decisions=decisions),
                                 ensure_ascii=False).encode()], description="batch decisions", already_locked=True)
            self.load()
        return decisions


def previous_items(path, scope):
    path = Path(path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (payload.get("race_id"), payload.get("stage_id", "")) != tuple(scope):
        return []
    return [ProblemItem.from_payload(i) for i in payload.get("items", [])]
