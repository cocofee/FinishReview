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

from realtime.analysis_resources import MIN_AVAILABLE_MEMORY_MB, peak_memory_mb
from realtime.problem_analysis import AnalysisConfig, load_line_overrides, run_offline_analysis


def _progress(stage: str, done: int, total: int) -> None:
    print(f"[{stage}] {done}/{total} peak {peak_memory_mb()} MB", flush=True)


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
        min_available_mb=arguments.min_free_mb,
    )


def run(config: AnalysisConfig, stage: str) -> dict:
    return run_offline_analysis(config, stage=stage, progress=_progress)


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
    parser.add_argument(
        "--min-free-mb", type=int, default=MIN_AVAILABLE_MEMORY_MB, help="可用内存低于此值时不开始分析",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(message)s")
    summary = run(build_config(arguments), arguments.stage)
    print("memory:", json.dumps(summary.get("memory", {}), ensure_ascii=False), flush=True)
    if summary.get("skipped"):
        print(summary["skipped"], flush=True)
        return 1
    print(json.dumps(summary, ensure_ascii=False, default=str)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
