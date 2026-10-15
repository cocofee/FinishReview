"""Independent chip snapshots; only the GUI publishes the live result file.

The video worker owns problem_list.json. Chip checks never modify it, the
recording journals, or operator decisions. The panel merges both snapshots.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .analysis_workspace import AnalysisWorkspace
from .chip_checks import ChipCheckContext, chip_check_items
from .chip_history import group_exclusion
from .batch_review import aggregate_batches, previous_items, read_snapshots, unambiguous_auto_passed
from .problem_items import CATEGORY_CHIP, KIND_NOT_ARRIVED, ProblemItem, sort_items


LIVE_CHIP_FILENAME = "live_chip_checks.json"
CHIP_INPUTS = (
    "cyclerace_passage_events.jsonl", "cyclerace_passage_events_deleted.json",
    "cyclerace_race_metadata.json", "review_event_scope.json",
)


def input_signature(root: Path) -> list:
    signature = []
    for name in CHIP_INPUTS:
        try:
            stat = (root / name).stat()
            signature.append([name, stat.st_size, stat.st_mtime_ns])
        except FileNotFoundError:
            continue
    return signature


def live_chip_payload(root: Path, *, race_id: str = "", stage_id: str = "") -> dict:
    root = Path(root)
    signature = input_signature(root)
    workspace = AnalysisWorkspace(root, race_id=race_id, stage_id=stage_id)
    metadata = workspace.metadata()
    roster = {}
    if metadata is not None and (metadata.race_id, metadata.stage_id) == (workspace.race_id, workspace.stage_id):
        roster = {athlete.bib: metadata.group_label(athlete.group_id)
                  for athlete in metadata.athletes if athlete.bib}
    context = ChipCheckContext(roster=roster, race_finished=False)
    reads = []
    for read in workspace.reads:
        event = workspace.latest_event(read.event_id)
        # Historical replaced identities stay available as closed discoveries;
        # an active identity must be checked at its revised time/group/lap.
        if read.active_now and event is not None:
            read = replace(read, chip_ms=int(event.timeline_timestamp_ms),
                           passage_time_ms=int(event.passage_time_ms), lap=int(event.lap),
                           group_id=event.group_id, group_name=event.group_name,
                           excluded=group_exclusion(event.group_name))
        reads.append(read)
    items = chip_check_items(reads, context)
    scope = (workspace.race_id, workspace.stage_id)
    members = read_snapshots(reads, metadata, workspace.latest_event)
    items = aggregate_batches(items, members, scope, previous_items(root / "finish_problems" / LIVE_CHIP_FILENAME, scope))
    scoped_history = [event for event in workspace.history if event.race_id == workspace.race_id
                      and (not workspace.stage_id or event.stage_id == workspace.stage_id)]
    return {
        "schema_version": 1, "race_id": workspace.race_id, "stage_id": workspace.stage_id,
        "input_signature": signature,
        # Duplicate transport deliveries can be acknowledged without a journal
        # append. The journal is not a raw reader-message counter.
        "counts": {"raw_messages": None, "journal_messages": len(scoped_history),
                   "effective_passages": sum(read.active_now for read in reads),
                   "video_candidates": None},
        "missing_inputs": list(context.missing_inputs()),
        "passage_members": members,
        "items": [item.to_payload() for item in items],
    }


def merge_chip_snapshot(video: dict | None, chips: dict | None) -> dict | None:
    if chips is None:
        return video
    if chips.get("schema_version") != 1:
        raise ValueError("unsupported live chip snapshot schema")
    if video and (video.get("race_id"), video.get("stage_id", "")) != (chips["race_id"], chips["stage_id"]):
        return video
    payload = dict(video or {"race_id": chips["race_id"], "stage_id": chips["stage_id"],
                             "summary": {}, "auto_passed": [], "cameras": {}})
    video_items = []
    for item in payload.get("items", []):
        video_items.extend(item.get("evidence", {}).get("batch_sources") or [item])
    items = [ProblemItem.from_payload(item) for item in video_items
             if item.get("category") != CATEGORY_CHIP
             and item.get("kind") != KIND_NOT_ARRIVED]
    old_members = payload.get("passage_members", {})
    new_members = chips.get("passage_members", {})
    def moved(event_id):
        before, after = old_members.get(event_id), new_members.get(event_id)
        return before and after and any(before.get(k) != after.get(k) for k in ("bib", "chip_id", "chip_ms", "lap"))
    items = [replace(item, evidence=dict(item.evidence, member_evidence_stale=True))
             if any(moved(e) for e in item.event_ids) else item for item in items]
    items += [ProblemItem.from_payload(item) for item in chips["items"]]
    old = [ProblemItem.from_payload(i) for i in payload.get("items", []) + chips["items"]]
    items = aggregate_batches(items, chips.get("passage_members", {}), (chips["race_id"], chips["stage_id"]), old)
    payload["passage_members"] = chips.get("passage_members", {})
    payload["items"] = [item.to_payload() for item in sort_items(items)]
    payload["chip_checks"] = chips
    original_auto = payload.get("auto_passed", [])
    payload["auto_passed"] = unambiguous_auto_passed(items, original_auto)
    payload["summary"] = dict(payload.get("summary", {}), pending=sum(item.pending for item in items),
                              auto_passed=max(0, payload.get("summary", {}).get("auto_passed", len(original_auto))
                                              - len(original_auto) + len(payload["auto_passed"])))
    return payload
