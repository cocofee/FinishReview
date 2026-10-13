"""Chip-only self check of one event workspace (plan 7.3).

    python -m tools.chip_self_check --workspace <event folder> --output-dir <work folder> \
        [--race-db <CycleRace race.db, copied first>]

Without ``--race-db`` only data received through the event interface is used;
the report lists which checks need CycleRace data (gun times, distance,
retirements). With ``--race-db`` a copy supplies those facts and the
two-sided sync check. Results: ``chip_checks.json`` and ``chip_checks.txt``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.chip_checks import ChipCheckContext, chip_check_items
from realtime.chip_history import BEIJING_TIMEZONE, format_chip_time
from realtime.problem_items import KIND_TITLES, STATUS_CLOSED_UPSTREAM, STATUS_INFO
from realtime.sync_check import check_sync, latest_events


def _race_date_ms(race_date: str) -> int:
    if not race_date:
        return 0
    day = datetime.strptime(race_date, "%Y-%m-%d").replace(tzinfo=BEIJING_TIMEZONE)
    return int(day.timestamp() * 1000)


def build_context(workspace: AnalysisWorkspace, race_db: Optional[Path], work_dir: Path,
                  *, max_speed_kmh: float, include_sync: bool = True) -> ChipCheckContext:
    roster: dict[str, str] = {}
    metadata = workspace.metadata()
    if metadata is not None and metadata.race_id == workspace.race_id:
        roster = {athlete.bib: metadata.group_label(athlete.group_id) for athlete in metadata.athletes if athlete.bib}
    if race_db is None:
        return ChipCheckContext(roster=roster, max_speed_kmh=max_speed_kmh)
    from tools.cyclerace_db_copy import copy_race_db, read_snapshot

    snapshot = read_snapshot(copy_race_db(race_db, work_dir / "cyclerace_copy"))
    gun_starts = {gun.group_name: gun.gun_ms for gun in snapshot.gun_starts if gun.group_name}
    mass = [gun.gun_ms for gun in snapshot.gun_starts if not gun.group_name]
    scoped = [
        event for event in latest_events(workspace.history).values()
        if event.race_id == workspace.race_id and (not workspace.stage_id or event.stage_id == workspace.stage_id)
    ]
    return ChipCheckContext(
        gun_starts=gun_starts,
        mass_start_ms=mass[-1] if mass else None,
        distance_km=snapshot.distance_km,
        max_speed_kmh=max_speed_kmh,
        roster=snapshot.roster or roster,
        statuses=snapshot.statuses,
        # The copy is the final state; comparing it with a replayed partial
        # journal would only list records that had not been sent yet.
        sync_issues=check_sync(snapshot.passages, scoped, race_id=workspace.race_id) if include_sync else (),
    )


def run_self_check(workspace_dir: Path, output_dir: Path, *, race_db: Optional[Path] = None,
                   max_speed_kmh: float = 60.0, race_id: str = "", as_of_ms: Optional[int] = None) -> dict:
    output_dir = Path(output_dir)
    workspace = AnalysisWorkspace(workspace_dir, race_id=race_id, as_of_ms=as_of_ms)
    context = build_context(workspace, race_db, output_dir, max_speed_kmh=max_speed_kmh,
                            include_sync=as_of_ms is None)
    items = chip_check_items(workspace.reads, context, race_date_ms=_race_date_ms(workspace.race_date))
    report = {
        "workspace": str(workspace.root),
        "race_id": workspace.race_id,
        "race_date": workspace.race_date,
        "as_of_ms": as_of_ms,
        "reads": len(workspace.reads),
        "missing_inputs": list(context.missing_inputs()),
        "counts": dict(Counter(item.kind for item in items)),
        "items": [item.to_payload() for item in items],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "chip_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "chip_checks.txt").write_text("\n".join(report_lines(report)) + "\n", encoding="utf-8")
    return report


def report_lines(report: dict) -> Iterable[str]:
    items = report["items"]
    pending = [item for item in items if item["status"] not in (STATUS_CLOSED_UPSTREAM, STATUS_INFO)]
    closed = [item for item in items if item["status"] == STATUS_CLOSED_UPSTREAM]
    yield f"芯片读数 {report['reads']} 条（{report['race_date']}）；待处理 {len(pending)} 条，已由 CycleRace 处理 {len(closed)} 条"
    if report["missing_inputs"]:
        yield "缺少的数据（相应检查未做）：" + "；".join(report["missing_inputs"])
    for kind, count in sorted(report["counts"].items()):
        yield f"  {KIND_TITLES.get(kind, kind)}：{count}"
    for item in items:
        status = {"closed_upstream": "已处理", "info": "参考"}.get(item["status"], "待处理")
        stamp = format_chip_time(item["chip_time_ms"]) if item["chip_time_ms"] is not None else "--"
        note = f"（{item['status_note']}）" if item["status_note"] else ""
        yield f"[{status}] {stamp} {item['title']}：{item['detail'][:300]}{note}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--race-db", type=Path)
    parser.add_argument("--race-id", default="")
    parser.add_argument("--max-speed-kmh", type=float, default=60.0)
    parser.add_argument("--as-of", default="", help="replay: only records received by HH:MM:SS on race day")
    arguments = parser.parse_args(argv)
    as_of_ms = None
    if arguments.as_of:
        workspace = AnalysisWorkspace(arguments.workspace, race_id=arguments.race_id)
        hours, minutes, seconds = (int(value) for value in arguments.as_of.split(":"))
        as_of_ms = _race_date_ms(workspace.race_date) + ((hours * 60 + minutes) * 60 + seconds) * 1000
    report = run_self_check(arguments.workspace, arguments.output_dir, race_db=arguments.race_db,
                            max_speed_kmh=arguments.max_speed_kmh, race_id=arguments.race_id,
                            as_of_ms=as_of_ms)
    print(f"items={len(report['items'])} -> {arguments.output_dir / 'chip_checks.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
