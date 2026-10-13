"""Reconcile external video counts against received chip revisions, retaining anomalies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from bisect import bisect_left, bisect_right


def observation_key(scope, row):
    """A count review belongs to these observations, not to a mutable chip roster."""
    bounds = None if row.get("video_event_uids") or row.get("frame_locator") else [row["start_ms"], row["end_ms"]]
    values = [scope, row["window_id"], bounds,
              sorted(row.get("video_event_uids", [])), row.get("evidence", {}), row.get("frame_locator")]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_jsonl(path):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    result = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            # A writer may still be appending the final, unterminated record.
            if index == len(lines) - 1 and not line.endswith((b"\n", b"\r")):
                break
            raise
        if not isinstance(value, dict):
            raise ValueError("journal records must be objects")
        result.append(value)
    return result


def _latest(records, cutoff=None):
    latest = {}
    for event in records:
        received = event.get("received_at_ms")
        if cutoff is not None and (type(received) is not int or received > cutoff):
            continue
        revision = event.get("revision")
        if type(revision) is not int or revision <= 0 or not isinstance(event.get("event_id"), str):
            raise ValueError("chip journal requires event IDs and positive revisions")
        previous = latest.get(event["event_id"])
        if previous is None or revision > previous["revision"]:
            latest[event["event_id"]] = event
    return latest


def _members(latest, start, end):
    return sorted((e for e in latest.values() if e.get("is_active", True)
                   and type(e.get("passage_timestamp_ms")) is int
                   and start <= e["passage_timestamp_ms"] <= end),
                  key=lambda e: (e["passage_timestamp_ms"], e["event_id"]))


def reconcile_report(source, revisions, *, as_of_ms, settlement_ms=5000, reviews=(), retained=()):
    """Consume anonymous counts; neither create chip events nor decide rider identity."""
    if type(as_of_ms) is not int or as_of_ms < 0 or type(settlement_ms) is not int or settlement_ms < 0:
        raise ValueError("invalid reconciliation time")
    scope = source["scope"]
    if any(not isinstance(scope.get(k), str) or not scope[k].strip() for k in ("race_id", "stage_id")):
        raise ValueError("chip history requires a race and stage")
    events = [e for e in revisions if e.get("race_id") == scope["race_id"]
              and e.get("stage_id") == scope["stage_id"]]
    receipt_missing = any(type(e.get("received_at_ms")) is not int for e in events)
    current = _latest(events, as_of_ms)
    # Records without receipt times can still supply the current roster, but not historical evidence.
    for uid, event in _latest([e for e in events if type(e.get("received_at_ms")) is not int]).items():
        if uid not in current or event["revision"] > current[uid]["revision"]:
            current[uid] = event
    histories = {}
    for event in events:
        histories.setdefault(event["event_id"], []).append(event)
    timed = sorted((e["passage_timestamp_ms"], e["event_id"]) for e in events
                   if type(e.get("passage_timestamp_ms")) is int)
    times = [t for t, uid in timed]
    active_events = sorted((e for e in current.values() if e.get("is_active", True)
                            and type(e.get("passage_timestamp_ms")) is int),
                           key=lambda e:(e["passage_timestamp_ms"],e["event_id"]))
    active_times = [e["passage_timestamp_ms"] for e in active_events]
    old = {r["case_id"]: r for r in retained if r.get("scope") == scope}
    review_index = {r["window_id"]: r for r in reviews if r.get("scope") == scope
                    and r.get("verified_at_ms", 0) <= as_of_ms}
    groups, cases = [], []
    ready_through = min(source.get("cutoff_ms") or 0, as_of_ms)
    calibrated = source.get("clock", {}).get("calibrated", False)
    source_rows = list(source["groups"])
    present = {r["window_id"] for r in source_rows}
    # Keep an already-raised case even if the producer removes its now-matching group.
    source_rows.extend(dict(r["source_row"], history_only=True) for r in old.values()
                       if r["window_id"] not in present and "source_row" in r)
    for source_row in source_rows:
        row = dict(source_row)
        start, end = row["start_ms"], row["end_ms"]
        case_id = hashlib.sha256(json.dumps([scope, row["window_id"]], sort_keys=True).encode()).hexdigest()[:24]
        baseline = old.get(case_id)
        deadline = end + settlement_ms
        ids = {uid for t, uid in timed[bisect_left(times, start):bisect_right(times, end)]}
        group_events = [e for uid in ids for e in histories[uid]]
        initial = _members(_latest(group_events, min(deadline, as_of_ms)), start, end)
        active = active_events[bisect_left(active_times, start):bisect_right(active_times, end)]
        raw_count = row["video_count"]
        reasons = [r for r in row.get("reasons", []) if r != "count_difference"]
        if source_row.get("status") in ("waiting", "rescanning"):
            reasons.append("observation_insufficient")
        pending = row.get("pending_count", 0)
        review = review_index.get(row["window_id"])
        reviewed = False
        if review is not None:
            verified = review.get("verified_video_count")
            if type(verified) is not int or verified < 0:
                raise ValueError("verified video count must be a nonnegative integer")
            if review.get("observation_key") == observation_key(scope, source_row):
                row["video_count"] = verified
                pending = 0
                reviewed = True
                reasons = [r for r in reasons if r.startswith("chip_") or r == "clock_uncertain"] + ["manual_count_verified"]
            else:
                pending = max(1, pending)
                reasons.append("manual_review_stale")
        if receipt_missing:
            reasons.append("chip_receipt_missing")
        if not calibrated:
            reasons.append("clock_uncertain")
        difference = row["video_count"] - len(active)
        if difference:
            reasons.append("count_difference")
        complete = ready_through >= deadline
        original_video = baseline["initial_video_count"] if baseline else raw_count
        original_chips = baseline["initial_chip_passages"] if baseline else initial
        original_pending = baseline.get("initial_pending_count", 0) if baseline else row.get("pending_count", 0)
        initial_difference = original_video - len(original_chips)
        anomaly = baseline is not None or (complete and not receipt_missing
                  and (initial_difference != 0 or original_pending > 0))
        before_ids = {e["event_id"] for e in original_chips}
        after_ids = {e["event_id"] for e in active}
        if before_ids - current.keys():
            reasons.append("chip_history_incomplete")
        changes = []
        for e in original_chips:
            if e["event_id"] not in after_ids:
                kind = "withdrawn" if not current.get(e["event_id"], {}).get("is_active", True) else "moved"
                changes.append(dict(kind=kind, event_id=e["event_id"], bib=e.get("bib", ""),
                                    revision=current.get(e["event_id"], e)["revision"]))
        for e in active:
            if e["event_id"] not in before_ids:
                changes.append(dict(kind="added", event_id=e["event_id"], bib=e.get("bib", ""), revision=e["revision"]))
        can_match = complete and calibrated and not receipt_missing and not pending and not difference
        # Quality reasons supplied by the detector keep equal counts under review.
        quality = [r for r in reasons if r != "manual_count_verified"]
        can_match = can_match and not quality
        if not complete or not calibrated:
            status = "waiting"
        elif can_match:
            status = "resolved" if anomaly else "matched"
        else:
            status = "unresolved"
        row.update(chip_count=len(active), chip_event_ids=[e["event_id"] for e in active],
                   chip_passages=active, pending_count=pending,
                   status=status, reasons=list(dict.fromkeys(reasons)),
                   detector_video_count=raw_count, manual_count_verified=reviewed)
        if anomaly:
            row.update(case_id=case_id, initial_video_count=original_video,
                       initial_chip_count=len(original_chips), initial_chip_passages=original_chips,
                       initial_pending_count=original_pending, chip_changes=changes)
            cases.append(dict(scope=scope, case_id=case_id, window_id=row["window_id"],
                              initial_video_count=original_video, initial_chip_passages=original_chips,
                              initial_pending_count=original_pending, status=status,
                              chip_changes=changes, observation_key=observation_key(scope, source_row),
                              source_row=source_row))
        groups.append(row)
    report = dict(source, groups=groups, updated_at_ms=as_of_ms,
                  chip_history_as_of_ms=as_of_ms, settlement_ms=settlement_ms)
    return report, cases


def apply_reviews(report, reviews):
    """Immediately show an operator's count while awaiting the next producer report."""
    index = {r["window_id"]: r for r in reviews if r.get("scope") == report["scope"]}
    groups = []
    for original in report["groups"]:
        row = dict(original)
        review = index.get(row["window_id"])
        if review is not None:
            count = review.get("verified_video_count")
            if type(count) is not int or count < 0:
                raise ValueError("verified video count must be a nonnegative integer")
            if review.get("observation_key") != observation_key(report["scope"], original):
                row["pending_count"] = max(1, row["pending_count"])
                row["reasons"] = list(dict.fromkeys(row["reasons"] + ["manual_review_stale"]))
                row["status"] = "unresolved"
            else:
                row.setdefault("initial_video_count", row["video_count"])
                row.setdefault("initial_chip_count", row["chip_count"])
                row.setdefault("detector_video_count", row["video_count"])
                row.update(video_count=count, pending_count=0, manual_count_verified=True)
                reasons = [r for r in row["reasons"] if r.startswith("chip_") or r == "clock_uncertain"]
                reasons.append("manual_count_verified")
                if original["status"] == "waiting" or not report.get("clock", {}).get("calibrated", False):
                    row["status"] = "waiting"
                elif count == row["chip_count"] and len(reasons) == 1:
                    row["status"] = "resolved"
                else:
                    row["status"] = "unresolved"
                    if count != row["chip_count"]:
                        reasons.append("count_difference")
                row["reasons"] = reasons
        groups.append(row)
    return dict(report, groups=groups)


def history_hint(row):
    if "initial_chip_count" not in row:
        return ""
    text = f"原始视频 {row['initial_video_count']} / 芯片 {row['initial_chip_count']}"
    labels = {"withdrawn": "已撤销", "added": "已补入", "moved": "已移出"}
    for change in row.get("chip_changes", []):
        text += f"；{labels[change['kind']]} {change.get('bib') or change['event_id']}"
    if row.get("manual_count_verified"):
        text += f"；核片人数 {row['video_count']}"
    return text
