"""Post-race check: CycleRace's own records versus what FinishReview received.

Usage (after the race, never during timing):

    python -m tools.cyclerace_sync_check --race-db <CycleRace race.db> \
        --workspace <FinishReview event folder> --output-dir <work folder>

The database is copied into ``--output-dir`` first and only the copy is read.
Results are written as ``sync_check.json`` and ``sync_check.txt`` (UTF-8).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from realtime.chip_history import local_date, read_passage_history
from realtime.passage_receiver import PassageEvent
from realtime.sync_check import (
    SYNC_ISSUE_TITLES,
    check_sync,
    latest_events,
    passage_id_from_event_id,
)
from tools.cyclerace_db_copy import CycleRaceSnapshot, copy_race_db, read_snapshot, snapshot_summary


PASSAGE_JOURNAL = "cyclerace_passage_events.jsonl"


def select_race_scope(history: Sequence[PassageEvent], snapshot: CycleRaceSnapshot) -> tuple[str, str]:
    """Pick the journal race/stage whose passage ids and bibs match the copy."""

    upstream = {row.passage_id: row.bib for row in snapshot.passages}
    scores: Counter = Counter()
    for event in latest_events(history).values():
        passage_id = passage_id_from_event_id(event.event_id)
        if passage_id is None:
            continue
        same_day = not snapshot.stage_date or local_date(event.timeline_timestamp_ms) == snapshot.stage_date
        if upstream.get(passage_id) == event.bib and same_day:
            scores[(event.race_id, event.stage_id)] += 1
    if not scores:
        raise ValueError("FinishReview journal has no records matching this CycleRace database")
    return scores.most_common(1)[0][0]


def run_sync_check(
    race_db: Path,
    workspace: Path,
    output_dir: Path,
    *,
    race_id: str = "",
    stage_id: str = "",
) -> dict:
    output_dir = Path(output_dir)
    copied = copy_race_db(race_db, output_dir / "cyclerace_copy")
    snapshot = read_snapshot(copied)
    history = read_passage_history(Path(workspace) / PASSAGE_JOURNAL)
    if not race_id:
        race_id, stage_id = select_race_scope(history, snapshot)
    latest = [
        event for event in latest_events(history).values()
        if event.race_id == race_id and (not stage_id or event.stage_id == stage_id)
    ]
    issues = check_sync(snapshot.passages, latest, race_id=race_id, stage_id=stage_id)
    report = {
        "race_id": race_id,
        "stage_id": stage_id,
        "cyclerace": snapshot_summary(snapshot),
        "received_events": len(latest),
        "received_active": sum(event.is_active for event in latest),
        "issues": [
            {
                "kind": issue.kind,
                "title": issue.title,
                "passage_id": issue.passage_id,
                "bib": issue.bib,
                "passage_time_ms": issue.passage_time_ms,
                "is_manual": issue.is_manual,
                "message": issue.message,
            }
            for issue in issues
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sync_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "sync_check.txt").write_text("\n".join(report_lines(report)) + "\n", encoding="utf-8")
    return report


def report_lines(report: dict) -> Iterable[str]:
    summary = report["cyclerace"]
    yield f"赛事：{summary['race_name'].splitlines()[0] if summary['race_name'] else '-'}（{summary['stage_date']}）"
    yield (
        f"CycleRace 通过记录 {summary['passages']} 条（有效 {summary['active_passages']}，补录 {summary['manual_passages']}）；"
        f"FinishReview 收到 {report['received_events']} 条（有效 {report['received_active']}）"
    )
    by_kind = Counter(item["kind"] for item in report["issues"])
    if not report["issues"]:
        yield "两边记录一致。"
        return
    yield "不一致：" + "，".join(f"{SYNC_ISSUE_TITLES.get(kind, kind)} {count} 条" for kind, count in by_kind.items())
    for item in report["issues"]:
        yield f"  [{item['passage_id']}] {item['message']}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--race-db", type=Path, required=True, help="CycleRace race.db（只复制，不直接读取）")
    parser.add_argument("--workspace", type=Path, required=True, help="FinishReview 赛事目录")
    parser.add_argument("--output-dir", type=Path, required=True, help="副本和结果目录")
    parser.add_argument("--race-id", default="")
    parser.add_argument("--stage-id", default="")
    arguments = parser.parse_args(argv)
    report = run_sync_check(
        arguments.race_db, arguments.workspace, arguments.output_dir,
        race_id=arguments.race_id, stage_id=arguments.stage_id,
    )
    print(f"issues={len(report['issues'])} -> {arguments.output_dir / 'sync_check.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
