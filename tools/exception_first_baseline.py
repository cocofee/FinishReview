"""Read-only exception-first baseline; no detection answers are injected.

python -m tools.exception_first_baseline --workspace EVENT_DIR --output REPORT.json
Use --verify-model to load the configured local model and run one blank frame.
This is a preflight, not a video replay or a race acceptance report.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.chip_checks import chip_check_items
from realtime.chip_history import ChipRead
from realtime.clock_sync import ClockModel, ClockModelStore
from realtime.problem_analysis import config_from_workspace, line_for_camera
from realtime.problem_rules import CrossingRecord, build_video_items


def synthetic_gaps() -> dict:
    t = 1_789_784_000_000
    read = ChipRead("e0", "0", "chip0", "g", "group", "", t, 0, 0, t, 1, 1, True, False)
    reads = [replace(read, event_id=f"e{i}", bib=str(i), chip_id=f"chip{i}", chip_ms=t + i * 20)
             for i in range(8)]
    crossing = CrossingRecord(1, "s", "segment", "synthetic.mkv", t + 1000, 20, 1000,
                              100, 80, 120, contact=(100., 100.))
    model = ClockModel(camera_index=1, intercept_ms=1000, slope=0, reference_ms=t,
                       valid_from_ms=t, valid_to_ms=t + 60_000, support=50, candidates=60,
                       residual_mad_ms=25, coarse_offset_ms=1000, coarse_support=100)
    burst = build_video_items(camera_index=1, model=model, matched=[(crossing, reads[0], 0)], chip_only=reads[1:],
                              video_only=[], no_video=[])
    chips = chip_check_items(reads)
    following = build_video_items(camera_index=1, model=model, matched=[(crossing, read, 0)], chip_only=[], no_video=[],
                                  video_only=[replace(crossing, recorder_ms=t + 1500, position_ms=1500,
                                                      frame_index=30, row=300, r0=280, r1=320)])
    return {
        "scope": "direct synthetic rule calls, not decoded riders",
        "eight_chips_one_crossing": {
            "chip_items": dict(Counter(item.kind for item in chips)),
            "video_items": dict(Counter(item.kind for item in burst.items)),
            "main_pending": sum(item.pending for item in (*chips, *burst.items)),
            "expected_main_pending": 1,
        },
        "following_crossing_500ms": {
            "main_pending": sum(item.pending for item in following.items),
            "discarded_as_duplicate": following.duplicates,
            "expected": "retain a traceable candidate; time proximity alone is not exclusion evidence",
        },
    }


def inspect_workspace(root: Path, *, verify_model=False) -> dict:
    source = AnalysisWorkspace(root)
    config = config_from_workspace(root)
    clocks = ClockModelStore(root / "video_clock_models.jsonl")
    archives = source.archive_segments()
    cameras = sorted(set(config.cameras) | set(source.camera_indexes()) | set(config.finish_lines))
    model = {"path": str(config.model_path or ""), "status": "not_configured"}
    if config.model_path:
        path = config.model_path
        model["status"] = "file_missing"
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            model.update(status="not_loaded", bytes=path.stat().st_size,
                         sha256=digest.hexdigest())
            if verify_model:
                import numpy as np
                from realtime.bike_check import YoloxDetector
                try:
                    detector = YoloxDetector(path, config.model_input)
                    detector.detect(np.zeros((320, 320, 3), dtype=np.uint8))
                    model["status"] = "loaded_blank_frame_inference_ok"
                except Exception as error:  # Report model failure without masking the rest of the preflight.
                    model.update(status="load_or_inference_failed", error=str(error))
    return {
        "workspace": str(root.resolve()), "race_id": source.race_id, "stage_id": source.stage_id,
        "journal_messages": sum(event.race_id == source.race_id
                                and (not source.stage_id or event.stage_id == source.stage_id)
                                for event in source.history),
        "effective_passages": sum(read.active_now for read in source.reads),
        "model": model,
        "cameras": {str(camera): {
            "effective_line": line_for_camera(config, source, camera),
            "crop_mode": config.crop_modes.get(camera, "up"),
            "sealed_existing_segments": sum(segment.camera_index == camera for segment in archives),
            "calibration_support": clocks.latest(camera).support if clocks.latest(camera) else None,
        } for camera in cameras},
        "limitations": ["Not a new full-video analysis", "Not a blind replay", "No operational timing measurement"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-model", action="store_true")
    args = parser.parse_args(argv)
    source, output = args.workspace.resolve(), args.output.resolve()
    if output.is_relative_to(source):
        parser.error("output must be outside the read-only source workspace")
    if args.verify_model:
        from realtime.analysis_resources import lower_process_priority

        lower_process_priority()
    report = {"synthetic": synthetic_gaps(), "preflight": inspect_workspace(source, verify_model=args.verify_model)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
