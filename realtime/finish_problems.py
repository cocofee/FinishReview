"""Offline finish problem reports and durable, revision-bound operator decisions.

These reports are review suggestions, never official results or manual judgments.
Source journals and manual clock calibrations are read-only inputs.
"""

from __future__ import annotations

from bisect import bisect_left
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .crossing_clock import CrossingClock, align_crossings, fit_crossing_clock
from .durable_jsonl import append_jsonl_records


KINDS = {
    "no_chip": "视频过线，没有芯片", "extra_chip": "疑似多余芯片",
    "no_crossing": "有芯片，未检测到过线", "group": "集团核对",
    "obstruction": "终点线遮挡", "no_video": "录像缺口，无法核实",
    "duplicate": "重复芯片读数", "test_chip": "测试或备用芯片",
    "excluded": "已排除的活动", "clock_wait": "自动校时待核对",
}
ACTIONS = {
    "confirm": "核对无误", "non_rider": "非选手 / 往回骑或推车",
    "insufficient": "证据不足", "add": "建议补录", "delete": "建议删除",
    "change": "建议改号 / 改圈", "reopen": "重新核对",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def chip_basis(chip):
    # Transport timestamps alone do not change the meaning of a read.
    return {k: chip.get(k) for k in ("event_id", "race_id", "stage_id", "revision",
                                    "bib", "chip_id", "lap", "group_id", "group_name",
                                    "timestamp_ms", "is_active")}


def scoped_chips(chips, scope):
    return sorted((c for c in chips if c.get("race_id") == scope["race_id"]
                   and c.get("stage_id") == scope["stage_id"] and c.get("is_active", True)),
                  key=lambda c: (c["timestamp_ms"], c["event_id"]))


def report_directory(output_dir, scope):
    return Path(output_dir) / "finish-problems" / digest(scope)[:16]


def build_problem_report(request, observations, coverages, obstructions=(), warnings=()):
    scope = request["scope"]
    if not scope.get("race_id") or not scope.get("stage_id"):
        raise ValueError("请先选择赛事和赛段")
    all_chips = scoped_chips(request["chips"], scope)
    final_lap = int(request.get("final_lap", 0))
    chips = [c for c in all_chips if not final_lap or c.get("lap", 0) == final_lap]
    events = sorted(observations, key=lambda e: e["recorder_ms"])
    source_ids = {s["segment_id"] for s in request["sources"]}
    if any(e["locator"]["segment_id"] not in source_ids for e in events):
        raise ValueError("过线证据不属于本次录像范围")
    problems, passes = [], []
    clock = None

    def issue(kind, members=(), evidence=None, start=None, end=None, note=""):
        members = sorted(members, key=lambda c: (c["timestamp_ms"], c["event_id"]))
        locator = dict(evidence["locator"]) if evidence else None
        start = (members[0]["timestamp_ms"] if members else
                 round(clock.to_chip(evidence["recorder_ms"])) if clock and evidence else 0) if start is None else start
        key = {"scope": scope, "kind": kind, "members": [c["event_id"] for c in members],
               "evidence": {k: locator[k] for k in ("segment_id", "frame_index")} if locator else None,
               "start": start if not members and not locator else None}
        row = dict(problem_id=digest(key)[:24], kind=kind, chips=[chip_basis(c) for c in members],
                   start_ms=int(start), end_ms=int(end if end is not None else start),
                   locator=locator, note=note, clock_version=clock.version if clock else "",
                   input_signature=request.get("input_signature", ""),
                   status="excluded" if kind == "excluded" else "pending")
        row["fingerprint"] = digest(row)
        problems.append(row)
        return row

    flagged = set()
    test_ids = set()
    seen = {}
    for c in chips:
        if any(word in c.get("group_name", "") for word in ("测试", "备用")):
            issue("test_chip", [c], note="请核实是否为有效完赛读数")
            flagged.add(c["event_id"])
            test_ids.add(c["event_id"])
        # Compare stable identity and lap, not merely nearby read times.
        identity = (c.get("chip_id") or c.get("bib"), c.get("lap", 0))
        previous = seen.get(identity)
        if previous and c["timestamp_ms"] - previous["timestamp_ms"] <= 5_000:
            issue("duplicate", [previous, c])
            flagged.update((c["event_id"], previous["event_id"]))
        seen[identity] = c
    usable = [c for c in chips if c["event_id"] not in test_ids]
    try:
        clock = fit_crossing_clock([c["timestamp_ms"] for c in usable],
                                   [e["recorder_ms"] for e in events])
    except ValueError as error:
        issue("clock_wait", note=str(error))
    conflicts = []
    if clock:
        for anchor in request.get("manual_anchors", []):
            difference = clock.manual_difference(anchor["chip_ms"], anchor["offset_ms"])
            if abs(difference) > 300:
                conflicts.append(dict(anchor, difference_ms=round(difference)))
    report = dict(schema_version=1, scope=scope, request_fingerprint=digest(request),
                  input_signature=request.get("input_signature", ""),
                  chip_signature=digest([chip_basis(c) for c in all_chips]),
                  created_at_ms=int(time.time() * 1000), clock=clock.to_payload() if clock else None,
                  manual_conflicts=conflicts, warnings=list(warnings),
                  problems=problems, automatic_passes=passes, final_lap=final_lap)
    if not clock:
        return report
    if conflicts:
        issue("clock_wait", note="自动校时与人工锚点相差超过 300 ms，自动结果仅供核对，未覆盖人工校时")
    pairs = align_crossings([c["timestamp_ms"] for c in usable], [e["recorder_ms"] for e in events], clock)
    paired_chips = {ci: (vi, residual) for ci, vi, residual in pairs}
    paired_events = {vi: ci for ci, vi, _ in pairs}

    def covered(t):
        return any(a <= clock.to_video(t) < b for a, b in coverages)

    def closest(t):
        return min(events, key=lambda e: abs(e["recorder_ms"] - clock.to_video(t)), default=None)

    def in_obstruction(t):
        return any(a <= clock.to_video(t) <= b for a, b, _ in obstructions)

    grouped = set()
    groups = []
    for c in usable:
        if groups and c["timestamp_ms"] - groups[-1][-1]["timestamp_ms"] <= 2_000:
            groups[-1].append(c)
        else:
            groups.append([c])
    group_windows = []
    for members in groups:
        ambiguous = any(
            sum(abs(e["recorder_ms"] - clock.to_video(c["timestamp_ms"])) <= 700 for e in events) != 1
            for c in members)
        if len(members) >= 4 or len(members) >= 2 and ambiguous:
            a, b = members[0]["timestamp_ms"] - 1500, members[-1]["timestamp_ms"] + 1500
            evidence = closest(members[0]["timestamp_ms"])
            if evidence and not a <= clock.to_chip(evidence["recorder_ms"]) <= b:
                evidence = None
            issue("group", members, evidence, a, b, "密集集团必须逐人核对；人数相同也不能排除漏读")
            group_windows.append((a, b))
            grouped.update(c["event_id"] for c in members)
    for a, b, evidence in obstructions:
        members = [c for c in usable if a <= clock.to_video(c["timestamp_ms"]) <= b]
        issue("obstruction", members, evidence, round(clock.to_chip(a)), round(clock.to_chip(b)),
              "遮挡时段不自动通过或自动排除，请逐帧查看")
        flagged.update(c["event_id"] for c in members)

    for ci, c in enumerate(usable):
        if not covered(c["timestamp_ms"]):
            issue("no_video", [c], note="此时刻没有成功解码的录像覆盖")
            flagged.add(c["event_id"])
            continue
        if c["event_id"] in grouped:
            continue
        if ci not in paired_chips:
            evidence = closest(c["timestamp_ms"])
            nearby = evidence and abs(evidence["recorder_ms"] - clock.to_video(c["timestamp_ms"])) <= 700
            members = [c]
            if nearby:
                vi = next(i for i, e in enumerate(events) if e is evidence)
                if vi in paired_events:
                    other = usable[paired_events[vi]]
                    members.append(other)
                    flagged.add(other["event_id"])
            issue("extra_chip" if nearby else "no_crossing", members,
                  evidence if nearby else None)
            flagged.add(c["event_id"])
    # A second candidate beside a matched rider must prevent an automatic pass.
    for vi, e in enumerate(events):
        if vi not in paired_events and not e.get("classification", {}).get("excluded", False):
            for ci, paired_vi, _ in pairs:
                if abs(e["recorder_ms"] - events[paired_vi]["recorder_ms"]) <= 800:
                    flagged.add(usable[ci]["event_id"])
    for ci, vi, residual in pairs:
        c, e = usable[ci], events[vi]
        if (c["event_id"] not in flagged | grouped and not conflicts
                and not e.get("obstructed") and covered(c["timestamp_ms"])):
            passes.append(dict(chip=chip_basis(c), locator=e["locator"], residual_ms=round(residual),
                               clock_version=clock.version))
    for vi, e in enumerate(events):
        if vi in paired_events or e.get("obstructed") or in_obstruction(clock.to_chip(e["recorder_ms"])):
            continue
        chip_t = clock.to_chip(e["recorder_ms"])
        if any(a <= chip_t <= b for a, b in group_windows):
            continue
        label = e.get("classification", {})
        if label.get("excluded", False):
            issue("excluded", evidence=e, note=label.get("reason", "行人或车辆"))
            continue
        near_pair = any(abs(e["recorder_ms"] - events[v]["recorder_ms"]) <= 800 for _, v, _ in pairs)
        if near_pair:
            # Keep an audit row: shadows can also hide a close-following rider.
            issue("no_chip", evidence=e, note="紧邻已有过线，可能是影子或紧跟骑手，请核对")
        else:
            issue("no_chip", evidence=e, note="检测到活动；身份与是否有效冲线需人工核对")
    problems.sort(key=lambda r: (r["status"] == "excluded", r["start_ms"], r["problem_id"]))
    return report


def read_decisions(directory):
    path = Path(directory) / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def apply_decisions(report, decisions):
    latest = {}
    for record in decisions:
        if record.get("scope") == report["scope"]:
            latest[record["problem_id"]] = record
    rows = []
    for problem in report["problems"]:
        row = dict(problem)
        decision = latest.get(row["problem_id"])
        if decision and decision["fingerprint"] == row["fingerprint"]:
            row["decision"] = decision
            action = decision["action"]
            row["status"] = ("pending" if action == "reopen" else "pending_sync"
                             if action in ("add", "delete", "change") else "insufficient"
                             if action == "insufficient" else "resolved")
        elif decision:
            row["note"] += "；证据或上游记录已修订，请重新核对"
        rows.append(row)
    return rows


def record_decision(directory, report, problem, *, action, reason, bib="", lap=None, chip_event_id=""):
    current = next((r for r in report["problems"] if r["problem_id"] == problem["problem_id"]), None)
    if current is None or current["fingerprint"] != problem["fingerprint"]:
        raise ValueError("问题已更新，请重新核对")
    if action not in ACTIONS or not reason.strip():
        raise ValueError("请选择处理方式并填写原因")
    if action in ("add", "change") and not bib.strip():
        raise ValueError("补录或改号必须填写号码")
    if action == "add" and (not problem["locator"] or not report["clock"] or report["manual_conflicts"]):
        raise ValueError("补录需要原帧证据和无冲突的自动校时")
    if action in ("delete", "change") and chip_event_id not in {c["event_id"] for c in problem["chips"]}:
        raise ValueError("请选择具体芯片记录；不能批量删除整个集团")
    record = dict(scope=report["scope"], problem_id=problem["problem_id"],
                  fingerprint=problem["fingerprint"], action=action, reason=reason.strip(), bib=bib.strip(),
                  lap=lap, chip_event_id=chip_event_id, recorded_at_ms=int(time.time() * 1000),
                  clock_version=problem["clock_version"], locator=problem["locator"])
    if action == "add":
        clock = CrossingClock(**report["clock"])
        record["chip_timestamp_ms"] = round(clock.to_chip(problem["locator"]["recorder_ms"]))
    append_jsonl_records(Path(directory) / "decisions.jsonl",
                         [(json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")],
                         description="finish problem decision")
    return record


def export_suggestions(directory, report, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows = apply_decisions(report, read_decisions(directory))
    outputs = []
    headers = ["号码", "原号码", "圈号", "芯片域时间毫秒", "事件ID", "原因", "录像", "原帧号", "片内毫秒", "校时版本"]
    for action, filename in (("add", "建议补录.csv"), ("delete", "建议删除.csv"), ("change", "建议改号改圈.csv")):
        path = destination / filename
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in rows:
                d = row.get("decision")
                if row["status"] != "pending_sync" or not d or d["action"] != action:
                    continue
                old = next((c for c in row["chips"] if c["event_id"] == d["chip_event_id"]), {})
                loc = d["locator"] or {}
                values = [d["bib"] or old.get("bib", ""), old.get("bib", ""),
                          d["lap"] if d["lap"] is not None else old.get("lap", ""),
                          d.get("chip_timestamp_ms", old.get("timestamp_ms", "")), d["chip_event_id"],
                          d["reason"], loc.get("video_path", ""), loc.get("frame_index", ""),
                          loc.get("position_ms", ""), d["clock_version"]]
                # Prevent spreadsheet formulas in user-entered fields.
                writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in values])
        outputs.append(path)
    return outputs
