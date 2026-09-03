"""Explicit maintenance CLI for workspace consistency and projections."""

from __future__ import annotations

import argparse
from pathlib import Path

from .workspace_consistency import WorkspaceConsistencyError, WorkspaceConsistencyService


def _service_for_workspace(workspace: Path) -> WorkspaceConsistencyService:
    workspace = workspace.expanduser().resolve()
    passage_candidates = tuple(
        path
        for path in (
            workspace / "cyclerace_passage_events.jsonl",
            workspace / "racetiger_passage_events.jsonl",
        )
        if path.exists()
    )
    if len(passage_candidates) != 1:
        raise WorkspaceConsistencyError(
            "workspace must contain exactly one CycleRace or RaceTiger passage journal"
        )
    return WorkspaceConsistencyService.open_read_only(
        passage_journal=passage_candidates[0],
        timeline_journal=workspace / "video_timeline.jsonl",
        association_journal=workspace / "passage_evidence_associations.jsonl",
        calibration_journal=workspace / "video_clock_calibrations.jsonl",
        binding_journal=workspace / "review_clips.jsonl",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 FinishReview 赛事工作区")
    parser.add_argument("command", choices=("check", "rebuild"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="派生投影路径；rebuild 默认写入工作区 workspace_projection.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        service = _service_for_workspace(args.workspace)
        report = service.check()
        for issue in report.issues:
            print(
                f"{issue.severity.upper()} {issue.code} "
                f"{issue.journal} {issue.entity_id}: {issue.message}"
            )
        if not report.is_consistent:
            return 2
        if args.command == "rebuild":
            output = args.output or args.workspace / "workspace_projection.json"
            print(service.rebuild_projection(output))
        else:
            print(
                "OK "
                f"events={report.event_count} segments={report.segment_count} "
                f"associations={report.association_count} "
                f"calibrations={report.calibration_count} clips={report.clip_count} "
                f"bindings={report.binding_count}"
            )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
