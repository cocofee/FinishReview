"""Read sealed source recordings and benchmark into an isolated temporary workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.problem_analysis import config_from_workspace, run_offline_analysis
from realtime.video_timeline import VideoTimelineStore


def benchmark(source, *, count=12, camera=1, reference=False, no_model=False):
    source = Path(source).resolve()
    original = AnalysisWorkspace(source, include_review_segments=True)
    segments = original.archive_segments(camera)[:count]
    if not segments:
        raise ValueError("no sealed video segments in the selected workspace/camera")
    sandbox = Path(tempfile.mkdtemp(prefix="finish-throughput-"))
    for name in ("cyclerace_passage_events.jsonl", "cyclerace_passage_events_deleted.json",
                 "cyclerace_race_metadata.json", "review_event_scope.json", "video_clock_calibrations.jsonl",
                 "finish_lines.json", "long_image_lines.json"):
        if (source / name).is_file():
            shutil.copyfile(source / name, sandbox / name)
    settings = source / "finish_problems" / "analysis_config.json"
    if settings.is_file():
        (sandbox / "finish_problems").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings, sandbox / "finish_problems" / "analysis_config.json")
    timeline = VideoTimelineStore(sandbox / "video_timeline.jsonl")
    for segment in segments:
        timeline.add_completed_segment(
            source_id=segment.segment.source_id, camera_index=camera, video_path=segment.path,
            media_started_at_ms=segment.start_ms, media_duration_ms=segment.end_ms - segment.start_ms,
            clock_source=segment.segment.clock_source, timing_error_ms=segment.segment.timing_error_ms,
            end_reason=segment.segment.end_reason, race_id=segment.segment.race_id,
        )
    config = replace(config_from_workspace(source), workspace=sandbox, output_dir=sandbox / "finish_problems",
                     race_id=original.race_id, stage_id=original.stage_id, cameras=(camera,), live=True,
                     clock_model_path=sandbox / "clock_models.jsonl", render_cards=True,
                     fast_decode=not reference)
    if no_model:
        config = replace(config, model_path=None)
    publications = []
    started = time.perf_counter()

    def progress(stage, done, total):
        if stage == "清单已发布":
            data = json.loads((config.resolved_output() / "problem_list.json").read_text(encoding="utf-8"))
            value = data["cameras"][str(camera)]
            publications.append(dict(elapsed_seconds=round(time.perf_counter() - started, 3),
                                     files=value.get("files"), pending_segments=value.get("pending_segments"),
                                     line_candidates=value.get("line_candidates"),
                                     crossing_verification=value.get("crossing_verification"),
                                     stage_seconds=value.get("stage_seconds"), performance=value.get("performance")))

    result = run_offline_analysis(config, progress=progress)
    return dict(workspace=str(sandbox), reference=reference, model_enabled=bool(config.model_path),
                selected_segments=len(segments), performance=result.get("performance"),
                paused_seconds=result.get("paused_seconds"), skipped=result.get("skipped"), publications=publications)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--segments", type=int, default=12)
    parser.add_argument("--camera", type=int, default=1)
    parser.add_argument("--reference", action="store_true", help="use the original OpenCV full-frame decoder")
    parser.add_argument("--no-model", action="store_true", help="measure scanning only, not full inference")
    args = parser.parse_args()
    if args.segments < 1:
        parser.error("--segments must be positive")
    print(json.dumps(benchmark(args.workspace, count=args.segments, camera=args.camera,
                               reference=args.reference, no_model=args.no_model), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
