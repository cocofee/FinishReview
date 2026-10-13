"""Automatic clock calibration report for one camera of an event workspace.

    python -m tools.clock_calibration_report --workspace <event folder> \
        --crossings <crossings.json> --output-dir <work folder>

``crossings.json`` lists detected line crossings in recorder time
(``{"camera_index": 1, "crossings": [{"recorder_ms": ..., "session_id": ...}]}``).
The workspace is only read. The fitted model is stored as a new calibration
version in ``<output-dir>/video_clock_models.jsonl``; manual calibrations and
operator-confirmed frames are compared with it, never used to replace it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from realtime.analysis_workspace import AnalysisWorkspace
from realtime.chip_history import calibration_chip_times, format_chip_time
from realtime.clock_sync import (
    CLOCK_MODEL_FILENAME,
    MANUAL_ANCHOR_TOLERANCE_MS,
    ClockCrossing,
    ClockModelStore,
    calibrate_camera,
    check_anchors,
)


def load_crossings(path: Path) -> tuple[int, list[ClockCrossing]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    camera = int(payload.get("camera_index", 1))
    crossings = [
        ClockCrossing(int(item["recorder_ms"]), str(item.get("session_id", "")))
        for item in payload.get("crossings", [])
        if item.get("rider_like", True)
    ]
    return camera, crossings


def operator_statistics(model, anchors) -> dict:
    """Agreement between operator-confirmed frames and the automatic model.

    Operators mark a slightly different frame than the automatic line contact,
    so the median difference is reported as a bias and removed before the
    300 ms comparison. Large deviations are usually confirmations on the wrong
    rider and are listed individually.
    """

    if not anchors:
        return {"count": 0}
    differences = np.asarray([anchor.offset_ms - model.offset_at(anchor.chip_ms) for anchor in anchors], dtype=float)
    bias = float(np.median(differences))
    checks = check_anchors(model, anchors, bias_ms=-int(round(bias)))
    outliers = [
        {"bib": check.anchor.label, "chip_time": format_chip_time(check.anchor.chip_ms),
         "difference_ms": check.difference_ms}
        for check in checks if not check.within_tolerance
    ]
    return {
        "count": len(anchors),
        "bias_ms": round(bias, 1),
        "within_300ms_after_bias": sum(check.within_tolerance for check in checks),
        "median_abs_after_bias_ms": float(np.median(np.abs(differences - bias))),
        "outliers": sorted(outliers, key=lambda item: -abs(item["difference_ms"])),
    }


def run_report(
    workspace_dir: Path,
    crossings_path: Path,
    output_dir: Path,
    *,
    race_id: str = "",
    stage_id: str = "",
) -> dict:
    workspace = AnalysisWorkspace(workspace_dir, race_id=race_id, stage_id=stage_id)
    camera, crossings = load_crossings(crossings_path)
    chips = calibration_chip_times(workspace.reads)
    result = calibrate_camera(crossings, chips, camera_index=camera)
    report: dict = {
        "workspace": str(workspace.root),
        "race_id": workspace.race_id,
        "race_date": workspace.race_date,
        "camera_index": camera,
        "crossings": len(crossings),
        "chips_for_calibration": len(chips),
        "warnings": list(result.warnings),
    }
    if result.coarse is not None:
        report["coarse"] = {
            "offset_ms": result.coarse.offset_ms,
            "support": result.coarse.support,
            "background": result.coarse.background,
            "second_support": result.coarse.second_support,
            "distinct": result.coarse.distinct,
        }
    if result.model is None:
        report["model"] = None
        _write(output_dir, report)
        return report
    output_dir.mkdir(parents=True, exist_ok=True)
    model = ClockModelStore(output_dir / CLOCK_MODEL_FILENAME).publish(result.model)
    kept_residuals = np.asarray([r for r, k in zip(result.residuals_ms, result.kept) if k], dtype=float)
    report["model"] = {
        "version": model.version,
        "slope_ms_per_hour": round(model.slope_ms_per_hour, 1),
        "offset_start_ms": model.offset_at(model.valid_from_ms),
        "offset_end_ms": model.offset_at(model.valid_to_ms),
        "valid_from": format_chip_time(model.valid_from_ms),
        "valid_to": format_chip_time(model.valid_to_ms),
        "isolated_riders": model.candidates,
        "kept": model.support,
        "residual_median_abs_ms": round(float(np.median(np.abs(kept_residuals))), 1),
        "residual_median_ms": round(float(np.median(kept_residuals)), 1),
        "session_medians_ms": {key: round(value, 1) for key, value in result.session_medians_ms.items()},
    }
    sessions = set(model.session_ids)
    anchors = workspace.manual_anchors(camera)
    # Only anchors set on the analysed recordings share their time base. Short
    # evidence clips have their own media start estimate and are listed only.
    manual = check_anchors(
        model, [anchor for anchor in anchors if anchor.reference in sessions],
        tolerance_ms=MANUAL_ANCHOR_TOLERANCE_MS,
    )
    report["manual_anchors_other_recordings"] = [
        {
            "bib": anchor.label,
            "session": anchor.reference,
            "manual_offset_ms": anchor.offset_ms,
            "difference_ms": anchor.offset_ms - model.offset_at(anchor.chip_ms),
        }
        for anchor in anchors if anchor.reference not in sessions
    ]
    report["manual_anchors"] = [
        {
            "bib": check.anchor.label,
            "session": check.anchor.reference,
            "chip_time": format_chip_time(check.anchor.chip_ms),
            "manual_offset_ms": check.anchor.offset_ms,
            "auto_offset_ms": check.auto_offset_ms,
            "difference_ms": check.difference_ms,
            "within_tolerance": check.within_tolerance,
            "message": check.message,
        }
        for check in manual
    ]
    report["operator_confirmations"] = operator_statistics(model, workspace.operator_anchors(camera))
    _write(output_dir, report)
    return report


def report_lines(report: dict) -> Iterable[str]:
    yield f"机位 {report['camera_index']}：过线 {report['crossings']} 次，可用芯片 {report['chips_for_calibration']} 条"
    coarse = report.get("coarse")
    if coarse:
        yield (
            f"粗校峰值 {coarse['offset_ms']} ms（峰 {coarse['support']}，背景 {coarse['background']:.1f}，"
            f"次峰 {coarse['second_support']}，{'单一明显' if coarse['distinct'] else '不明显'}）"
        )
    model = report.get("model")
    if not model:
        yield "自动校时失败"
    else:
        yield (
            f"校时版本 v{model['version']}：偏移 {model['offset_start_ms']} ms（{model['valid_from']}）→ "
            f"{model['offset_end_ms']} ms（{model['valid_to']}），漂移 {model['slope_ms_per_hour']} ms/小时"
        )
        yield (
            f"单独过线者 {model['isolated_riders']} 人，采用 {model['kept']} 人，"
            f"残差中位数 |r| {model['residual_median_abs_ms']} ms"
        )
    for warning in report.get("warnings", ()):
        yield f"提示：{warning}"
    for anchor in report.get("manual_anchors", ()):
        yield f"人工校时 {anchor['session']}：{anchor['message']}"
    others = report.get("manual_anchors_other_recordings", ())
    if others:
        differences = sorted(abs(item["difference_ms"]) for item in others)
        yield (
            f"另有 {len(others)} 条人工校时建立在短片上（时间基准不同，只列出不比较），"
            f"与整段录像自动校时的差值中位数 {differences[len(differences) // 2]} ms"
        )
    stats = report.get("operator_confirmations") or {}
    if stats.get("count"):
        yield (
            f"人工确认 {stats['count']} 条：系统差 {stats['bias_ms']} ms，扣除后 300 ms 内 "
            f"{stats['within_300ms_after_bias']} 条；偏差大的：{stats['outliers'][:10]}"
        )


def _write(output_dir: Path, report: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    camera = report["camera_index"]
    (output_dir / f"clock_report_camera{camera}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / f"clock_report_camera{camera}.txt").write_text(
        "\n".join(report_lines(report)) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--crossings", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--race-id", default="")
    parser.add_argument("--stage-id", default="")
    arguments = parser.parse_args(argv)
    report = run_report(
        arguments.workspace, arguments.crossings, arguments.output_dir,
        race_id=arguments.race_id, stage_id=arguments.stage_id,
    )
    model = report.get("model") or {}
    print(f"camera {report['camera_index']}: version {model.get('version')} slope {model.get('slope_ms_per_hour')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
