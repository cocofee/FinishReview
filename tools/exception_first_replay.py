"""Blind, time-released replay of one sealed workspace or cached slice.

    python -m tools.exception_first_replay --source SLICE --workdir DIR --output REPORT.json
        [--camera 2] [--step-ms 120000] [--max-steps 40] [--no-model]

The source is only read.  A replay workspace is built once from its inputs and
then, for every release mark, the two journals are rewritten to the state they
would have had at that moment: a recording segment appears when it sealed, a
chip revision when CycleRace delivered it (``received_at_ms``).  Every step runs
the same live pipeline the window runs (``config.live``), so the report answers
one question: what could the operator have seen at that moment, and never a
minute later.  The last step is the post-race settle run (``evidence_delay_ms``
0), like ``--finish-live-problems``.

The report is a validation artifact, not a result: it never writes a time, and
the strip cache is reused, so decoding load is not re-measured here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from dataclasses import replace
from pathlib import Path

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.problem_analysis import AnalysisConfig, config_from_workspace, run_offline_analysis
from realtime.problem_items import ProblemItem

JOURNAL = "cyclerace_passage_events.jsonl"
TIMELINE = "video_timeline.jsonl"
CARRIED_INPUTS = (JOURNAL, TIMELINE, "cyclerace_race_metadata.json", "review_event_scope.json")
# What the replay may keep in its copy; everything else the analysis wrote is
# removed, so no earlier run can hand a later step an item identity, a candidate
# history or a calibration fitted on data the operator had not received.
KEPT_IN_FINISH_PROBLEMS = ("analysis_config.json", "finish_lines.json", "strips")
DEFAULT_STEP_MS = 60_000
DEFAULT_MAX_STEPS = 40


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, entries: list[dict]) -> None:
    temporary = Path(path).with_name(Path(path).name + ".tmp")
    temporary.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")
    temporary.replace(path)


def merge_segments(entries: list[dict]) -> dict[str, dict]:
    """Timeline lines are per-segment updates; merge them in file order."""

    merged: dict[str, dict] = {}
    for entry in entries:
        segment_id = entry.get("segment_id")
        if segment_id:
            merged.setdefault(str(segment_id), {}).update(entry)
    return merged


def sealed_timeline(entries: list[dict], mark: int) -> tuple[list[dict], set[str]]:
    """Only segments that had already sealed by ``mark``; a segment still being
    written is not released, exactly like the live recorder's playlist."""

    merged = merge_segments(entries)
    # A segment that only announced itself has no seal time yet and stays out.
    sealed = {key for key, record in merged.items()
              if record.get("ended_at_ms") is not None and int(record["ended_at_ms"]) <= mark}
    return [entry for entry in entries if str(entry.get("segment_id")) in sealed], sealed


def released_journal(entries: list[dict], mark: int) -> tuple[list[dict], set[str], int]:
    """Chip revisions delivered by ``mark``; a revision that arrived later is
    invisible, so nothing can be decided from the final corrected list."""

    kept, events, undated = [], set(), 0
    for entry in entries:
        received = entry.get("received_at_ms")
        if received is None:
            undated += 1
        elif int(received) > mark:
            continue
        kept.append(entry)
        if entry.get("event_id"):
            events.add(str(entry["event_id"]))
    return kept, events, undated


def release_marks(segment_ends: list[int], receives: list[int], *, step_ms: int, max_steps: int,
                  tail_ms: int = 3_600_000) -> list[int]:
    """Every seal, plus the reception clock thinned to ``step_ms`` and ``max_steps``.

    Chip revisions that arrive long after the last recording sealed (a batch
    re-upload days later) do not each get a step: the schedule runs to
    ``tail_ms`` past the last seal and then jumps to the final release, so the
    post-race delivery is still the last thing the replay looks at.
    """

    marks = {int(value) for value in segment_ends}
    span_end = max([*segment_ends, *receives]) if (segment_ends or receives) else 0
    grid_end = (max(segment_ends) + tail_ms) if segment_ends else span_end
    for received in receives:
        mark = -(-int(received) // step_ms) * step_ms
        if mark <= grid_end:
            marks.add(mark)
    ordered = sorted(mark for mark in marks if mark <= span_end)
    if not ordered or ordered[-1] != span_end:
        ordered.append(span_end)
    if len(ordered) > max_steps:
        seals = {int(value) for value in segment_ends}
        grid = [mark for mark in ordered if mark not in seals]
        room = max(max_steps - len(seals), 1)
        grid = grid[:: -(-len(grid) // room)]
        ordered = sorted(set(grid) | seals)
        # Thinning must never cost the last step: the post-race delivery has to
        # stay the run that sees everything.
        if ordered[-1] != span_end:
            ordered.append(span_end)
    return ordered


def provenance_leaks(items, auto_passed, released_events: set[str], sealed_segments: set[str]) -> list[dict]:
    """Items that mention a chip or a segment the operator had not received yet."""

    leaks = []
    for item in items:
        missing = sorted(set(item.event_ids) - released_events)
        frame = item.evidence.get("frame") or {}
        segment = frame.get("segment_id")
        # Batch members and sources come from earlier runs of the same step, so
        # they inherit the released set; only the item's own claims are checked.
        if missing or (segment and segment not in sealed_segments):
            leaks.append({"item_id": item.item_id, "kind": item.kind, "bibs": list(item.bibs),
                          "events_not_released": missing,
                          "segment_not_sealed": None if not segment or segment in sealed_segments else segment})
    for entry in auto_passed:
        event = entry.get("event_id")
        if event and str(event) not in released_events:
            leaks.append({"item_id": f"auto_passed:{entry.get('bib')}", "kind": "auto_passed",
                          "bibs": [entry.get("bib")], "events_not_released": [str(event)],
                          "segment_not_sealed": None})
    return leaks


def recorder_time(item_payload: dict) -> int | None:
    evidence = item_payload.get("evidence") or {}
    for key in ("recorder_ms", "recorder_start_ms", "recorder_end_ms"):
        value = evidence.get(key)
        if value is not None:
            return int(value)
    return None


def build_workspace(source: Path, workdir: Path) -> dict:
    """Inputs only; no problem list, no clock model, no cards from the source."""

    if workdir.exists() and any(workdir.iterdir()):
        raise SystemExit(f"workdir must be empty: {workdir}")
    (workdir / "finish_problems").mkdir(parents=True, exist_ok=True)
    copied = []
    for name in CARRIED_INPUTS:
        path = source / name
        if path.is_file():
            shutil.copy2(path, workdir / name)
            copied.append(name)
    for name in ("analysis_config.json", "finish_lines.json"):
        path = source / "finish_problems" / name
        if path.is_file():
            shutil.copy2(path, workdir / "finish_problems" / name)
    strips = source / "finish_problems" / "strips"
    if strips.is_dir():
        shutil.copytree(strips, workdir / "finish_problems" / "strips")
    for path in source.glob("finish_problems/bike_camera*.json"):
        shutil.copy2(path, workdir / "finish_problems" / path.name)
    return {"copied": copied, "strips_cached": strips.is_dir()}


def file_digests(root: Path, names) -> dict[str, str]:
    import hashlib

    digests = {}
    for name in names:
        path = Path(root) / name
        if path.is_file():
            digests[name] = hashlib.sha1(path.read_bytes()).hexdigest()
    return digests


def clear_outputs(workdir: Path) -> None:
    """Back to the state right after ``build_workspace``: inputs only."""

    def remove(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    for path in list(workdir.iterdir()):
        if path.is_dir():
            if path.name != "finish_problems":
                remove(path)
        elif path.name not in CARRIED_INPUTS:
            remove(path)
    problems = workdir / "finish_problems"
    for path in list(problems.iterdir()):
        keep = path.name in KEPT_IN_FINISH_PROBLEMS or path.name.startswith("bike_camera")
        if not keep:
            remove(path)


# Live runs hold back these two kinds until the race is over; the settle run is
# the one that would list them, so they are excluded from the comparison only
# where the live mode drops them by design.
LIVE_DROPPED_KINDS = {"not_arrived", "lap_missing"}
TASK_SUMMARY_KEYS = ("pending", "disputed", "grouped", "auto_passed", "auto_passed_in_pending_period",
                     "closed_upstream", "excluded")


def item_key(item: ProblemItem) -> str:
    return "|".join([item.kind, ",".join(sorted(item.bibs)), item.status, str(item.chip_time_ms)])


def step_config(workdir: Path, *, camera: int, mode: str, model: bool = True) -> AnalysisConfig:
    """The configuration one replay step runs with.

    ``model`` False is the "bike recognition not enabled" arm of the acceptance
    comparison; nothing else changes, so the two arms stay comparable.
    """

    config = config_from_workspace(workdir)
    if camera:
        # One camera per replay: a camera with no sealed segment would only add
        # its own "waiting for recording" entry to every step.
        config = replace(config, cameras=(camera,))
    if mode == "live":
        config = replace(config, live=True)
    elif mode == "settle":
        config = replace(config, live=True, evidence_delay_ms=0)
    elif mode != "offline":
        raise ValueError(f"unknown replay mode: {mode}")
    return replace(config, render_cards=False, model_path=config.model_path if model else None)


def run_step(workdir: Path, *, camera: int, mode: str, model: bool = True) -> tuple[dict, dict, float]:
    """``live`` is one in-race tick, ``settle`` is ``--finish-live-problems``
    (recording stopped, evidence delay over) and ``offline`` is the one-shot
    post-race run the acceptance numbers were produced with."""

    config = step_config(workdir, camera=camera, mode=mode, model=model)
    started = time.monotonic()
    summary = run_offline_analysis(config)
    seconds = round(time.monotonic() - started, 2)
    list_path = workdir / "finish_problems" / "problem_list.json"
    payload = json.loads(list_path.read_text(encoding="utf-8")) if list_path.is_file() else {}
    return summary, payload, seconds


def camera_entry(payload: dict, camera: int) -> dict:
    return (payload.get("cameras") or {}).get(str(camera)) or {}


def replay(source: Path, workdir: Path, *, camera: int, step_ms: int, max_steps: int,
           model: bool = True) -> dict:
    before = file_digests(source, CARRIED_INPUTS)
    build = build_workspace(source, workdir)
    timeline = read_jsonl(workdir / TIMELINE)
    journal = read_jsonl(workdir / JOURNAL)
    merged = merge_segments(timeline)
    segment_ends = [int(record["ended_at_ms"]) for record in merged.values() if record.get("ended_at_ms")]
    receives = [int(entry["received_at_ms"]) for entry in journal if entry.get("received_at_ms") is not None]
    marks = release_marks(segment_ends, receives, step_ms=step_ms, max_steps=max_steps)
    report: dict = {"source": str(source.resolve()), "workdir": str(workdir), "camera": camera,
                    "step_ms": step_ms, "bike_model": model, "inputs": build,
                    "source_span": {"segments": len(merged), "sealed_segments": len(segment_ends),
                                    "journal_revisions": len(journal), "received_span_ms": [min(receives), max(receives)]
                                    if receives else None},
                    "marks": len(marks), "steps": [], "checks": {}, "metrics": {}}

    # Reference: the same sealed evidence in one go, the way the acceptance
    # numbers were produced.  Its outputs are removed so the replay starts clean.
    full_summary, full_payload, full_seconds = run_step(workdir, camera=camera, mode="offline", model=model)
    report["offline_run"] = {
        "seconds": full_seconds, "summary": full_payload.get("summary"),
        "pending_by_kind": pending_by_kind(full_payload.get("items") or []),
        "residual_median_ms": camera_entry(full_payload, camera).get("pairing", {}).get("residual_median_ms"),
        "chips_with_video": camera_entry(full_payload, camera).get("pairing", {}).get("chips_with_video"),
        "peak_mb": full_summary.get("memory", {}).get("peak_mb"),
    }
    clear_outputs(workdir)

    first_published: dict[str, int] = {}
    first_seal_lag: dict[str, int] = {}
    previous: dict[str, dict] = {}
    leaks: list[dict] = []
    for index, mark in enumerate(marks):
        settle = index == len(marks) - 1
        entries, sealed = sealed_timeline(timeline, mark)
        revisions, released_events, undated = released_journal(journal, mark)
        write_jsonl(workdir / TIMELINE, entries)
        write_jsonl(workdir / JOURNAL, revisions)
        summary, payload, seconds = run_step(workdir, camera=camera, mode="settle" if settle else "live",
                                             model=model)
        items = [ProblemItem.from_payload(value) for value in payload.get("items") or []]
        payloads = [item.to_payload() for item in items]
        current = {value["item_id"]: value for value in payloads}
        camera_result = camera_entry(payload, camera)
        step_leaks = provenance_leaks(items, payload.get("auto_passed") or [], released_events, sealed)
        leaks += [dict(leak, mark=mark, settle=settle) for leak in step_leaks]
        merged_now = merge_segments(entries)
        sealed_ranges = [(int(record["media_started_at_ms"] if record.get("media_started_at_ms") is not None
                              else record.get("started_at_ms") or 0), int(record["ended_at_ms"]))
                         for record in merged_now.values()
                         if record.get("ended_at_ms") is not None
                         and int(record["ended_at_ms"]) <= mark]
        lags = {}
        for item_id, value in current.items():
            first_published.setdefault(item_id, mark)
            when = recorder_time(value)
            # Only riders whose crossing is inside released video: a chip that
            # lies outside the recorded range can never be "late", it is simply
            # not covered yet.
            if when is not None and any(start <= when <= end for start, end in sealed_ranges):
                lags[item_id] = mark - when
        # The delay the operator actually feels: from the moment the recording
        # sealed to the moment the operator first saw evidence about it.  A
        # revision delivered long after the race is not "late analysis", so
        # post-race steps are reported but do not enter this metric.
        post_race = bool(segment_ends) and mark > max(segment_ends)
        seal_lags = {}
        if not post_race:
            for item_id in set(current) - set(previous):
                segment_id = str(((current[item_id].get("evidence") or {}).get("frame") or {})
                                 .get("segment_id") or "")
                ended = merged.get(segment_id, {}).get("ended_at_ms")
                if ended is not None:
                    seal_lags[item_id] = mark - int(ended)
                    first_seal_lag.setdefault(item_id, seal_lags[item_id])
        step = {
            "index": index, "mark": mark, "settle": settle,
            "sealed_segments": len(sealed), "released_revisions": len(revisions), "undated_revisions": undated,
            "analyzed_through_ms": camera_result.get("analyzed_through_ms"),
            "waiting_reads": camera_result.get("waiting_reads"),
            "seconds": seconds, "skipped": summary.get("skipped"), "error": camera_result.get("error"),
            "summary": payload.get("summary"), "pending_by_kind": pending_by_kind(payloads),
            "new_items": sorted(set(current) - set(previous)), "dropped_items": sorted(set(previous) - set(current)),
            "candidates_closed": (payload.get("summary") or {}).get("candidates_closed"),
            "leaks": len(step_leaks), "paused_seconds": summary.get("paused_seconds"),
            "peak_mb": summary.get("memory", {}).get("peak_mb"),
            "max_publish_lag_ms": max(lags.values()) if lags else None,
            "max_seal_to_publish_ms": max(seal_lags.values()) if seal_lags else None,
            "post_race": post_race,
            "item_ids": sorted(current),
            # Enough to ask later when a known case first became visible.
            "item_times": {item_id: recorder_time(value) for item_id, value in sorted(current.items())},
        }
        if settle:
            step["items"] = payloads
        report["steps"].append(step)
        previous = current
        print(f"step {index + 1}/{len(marks)} mark={mark} sealed={len(sealed)} revs={len(revisions)} "
              f"through={step['analyzed_through_ms']} pending={(step['summary'] or {}).get('pending')} "
              f"waiting={step['waiting_reads']} {seconds}s", flush=True)

    finals = report["steps"][-1] if report["steps"] else {}
    watermarks = [step["analyzed_through_ms"] for step in report["steps"] if step["analyzed_through_ms"] is not None]
    lags = [step["max_publish_lag_ms"] for step in report["steps"] if step["max_publish_lag_ms"] is not None]
    # Backlog and promptness are what the operator felt during the race; the
    # steps that run after the last recording sealed report separately.
    backlog = [step["mark"] - step["analyzed_through_ms"] for step in report["steps"]
               if step["analyzed_through_ms"] is not None and not step["settle"] and not step["post_race"]]
    full_items = [ProblemItem.from_payload(value) for value in full_payload.get("items") or []]
    settle_items = [ProblemItem.from_payload(value) for value in finals.get("items") or []]
    offline_pending = {item_key(item) for item in full_items if item.pending and item.kind not in LIVE_DROPPED_KINDS}
    settle_pending = {item_key(item) for item in settle_items if item.pending}
    report["checks"] = {
        "source_untouched": before == file_digests(source, CARRIED_INPUTS),
        "marks_cover_every_seal": all(int(value) in marks for value in segment_ends),
        "watermark_monotonic": watermarks == sorted(watermarks),
        "watermark_inside_released_video": all(step["analyzed_through_ms"] is None
                                               or step["analyzed_through_ms"] <= step["mark"] for step in report["steps"]),
        "no_future_evidence": not leaks,
        "settle_is_last_and_settled": bool(finals) and finals["settle"]
        and finals["analyzed_through_ms"] == camera_entry(full_payload, camera).get("analyzed_through_ms"),
        # The live settle and the one-shot offline run are not compared on
        # everything: live mode holds back 应到未到/末圈缺读 by design.  The
        # tasks, the automatic matches and the upstream states must agree.
        "settle_tasks_match_one_shot": {key: (finals.get("summary") or {}).get(key) for key in TASK_SUMMARY_KEYS}
        == {key: (full_payload.get("summary") or {}).get(key) for key in TASK_SUMMARY_KEYS},
        "settle_pending_set_matches_one_shot": settle_pending == offline_pending,
    }
    report["metrics"] = {
        "analysis_seconds": {"median": statistics.median([step["seconds"] for step in report["steps"]]) if report["steps"] else None,
                             "max": max([step["seconds"] for step in report["steps"]], default=None)},
        "max_backlog_ms": max(backlog, default=None),
        "max_publish_lag_ms": max(lags, default=None),
        "max_seal_to_publish_ms": max(first_seal_lag.values(), default=None),
        "first_publish_marks": first_published,
        "peak_mb": max([step["peak_mb"] for step in report["steps"] if step["peak_mb"]], default=None),
        "paused_seconds": sum(step["paused_seconds"] or 0 for step in report["steps"]),
        "note": "cached strips were reused; decode load is measured by the step-2 benchmarks, not here",
    }
    report["leaks"] = leaks
    report["settle_summary"] = finals.get("summary")
    return report


def pending_by_kind(payloads) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in payloads:
        item = value if isinstance(value, ProblemItem) else ProblemItem.from_payload(value)
        if item.pending:
            counts[item.kind] = counts.get(item.kind, 0) + 1
    return dict(sorted(counts.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="sealed workspace or cached slice; read-only")
    parser.add_argument("--workdir", type=Path, required=True, help="empty directory for the replay copy")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--camera", type=int, default=1)
    parser.add_argument("--step-ms", type=int, default=DEFAULT_STEP_MS)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--no-model", dest="model", action="store_false",
                        help="bike recognition off: the other arm of the acceptance comparison")
    args = parser.parse_args(argv)
    report = replay(args.source, args.workdir, camera=args.camera, step_ms=args.step_ms,
                    max_steps=args.max_steps, model=args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    failed = [name for name, value in report["checks"].items() if not value]
    print(json.dumps({"checks": report["checks"], "metrics": report["metrics"]}, ensure_ascii=False, default=str))
    print(str(args.output))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
