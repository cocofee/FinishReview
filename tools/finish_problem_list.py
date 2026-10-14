"""Offline finish problem list for an archived event workspace.

    python -m tools.finish_problem_list --workspace <event folder> --output-dir <work folder> \
        [--finish-lines lines.json] [--camera 1] [--model yolox_s.onnx] [--stage strips|all]

The workspace and its recordings are only read. Runs at low priority with at
most two decode threads per camera and a single-threaded YOLOX.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from realtime.analysis_resources import ResourceGovernor, limit_opencv_threads, lower_process_priority
from realtime.analysis_workspace import AnalysisWorkspace
from realtime.problem_analysis import (
    AnalysisConfig,
    analyse_camera,
    extract_strips,
    line_for_camera,
    load_line_overrides,
    write_problem_list,
)


def _progress(stage: str, done: int, total: int) -> None:
    print(f"[{stage}] {done}/{total}", flush=True)


def build_config(arguments: argparse.Namespace) -> AnalysisConfig:
    crop_modes = {}
    for value in arguments.crop_mode or ():
        camera, mode = value.split(":", 1)
        crop_modes[int(camera)] = mode
    return AnalysisConfig(
        workspace=arguments.workspace,
        output_dir=arguments.output_dir,
        finish_lines=load_line_overrides(arguments.finish_lines),
        cameras=tuple(arguments.camera or ()),
        decode_threads=arguments.decode_threads,
        model_path=arguments.model,
        model_input=arguments.model_input,
        crop_modes=crop_modes,
        race_id=arguments.race_id,
        render_cards=not arguments.no_cards,
        check_paired_bikes=arguments.check_paired_bikes,
    )


def run(config: AnalysisConfig, stage: str) -> dict:
    lower_process_priority()
    limit_opencv_threads(1)
    governor = ResourceGovernor()
    workspace = AnalysisWorkspace(config.workspace, race_id=config.race_id)
    cameras = config.cameras or workspace.camera_indexes()
    summary: dict = {"race_id": workspace.race_id, "cameras": {}}
    results = []
    for camera in cameras:
        line = line_for_camera(config, workspace, camera)
        segments = workspace.archive_segments(camera)
        if line is None or not segments:
            summary["cameras"][camera] = {"skipped": "没有终点线或整段录像"}
            continue
        paths = extract_strips(config, segments, line, progress=_progress, checkpoint=governor.checkpoint)
        summary["cameras"][camera] = {"strips": len(paths)}
        if stage == "strips":
            continue
        result = analyse_camera(config, workspace, camera, paths, progress=_progress, checkpoint=governor.checkpoint)
        results.append(result)
        summary["cameras"][camera] = {
            key: value for key, value in result.items() if key not in ("items", "auto_passed", "model")
        }
    if stage != "strips":
        path = write_problem_list(config, workspace, results, progress=_progress)
        summary["problem_list"] = str(path)
        summary["summary"] = json.loads(path.read_text(encoding="utf-8"))["summary"]
    summary["paused_seconds"] = round(governor.paused_seconds, 1)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--finish-lines", type=Path, help="终点线配置（覆盖赛事目录中的 finish_lines.json）")
    parser.add_argument("--camera", type=int, action="append")
    parser.add_argument("--race-id", default="")
    parser.add_argument("--decode-threads", type=int, default=2)
    parser.add_argument("--model", type=Path, help="YOLOX ONNX 模型路径；不指定则跳过认车")
    parser.add_argument("--model-input", type=int, default=640)
    parser.add_argument("--crop-mode", action="append", help="机位:模式，如 1:up 或 2:side-right")
    parser.add_argument("--stage", choices=("strips", "all"), default="all")
    parser.add_argument("--no-cards", action="store_true")
    parser.add_argument("--check-paired-bikes", action="store_true", help="同时对已配对骑手认车（只用于验收）")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(message)s")
    summary = run(build_config(arguments), arguments.stage)
    print(json.dumps(summary, ensure_ascii=False, default=str)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
