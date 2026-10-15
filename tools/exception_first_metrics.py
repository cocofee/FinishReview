"""Reduction and end-to-end metrics of one finish problem list.

    python -m tools.exception_first_metrics --problem-list LIST.json --output REPORT.json
        [--camera 1] [--replay replay.json] [--known-cases known.json] [--history dingbian|tongxin]

The report is a measurement, never a result: every number is read from a list
that was already written, or from a replay report of the same workspace.  What
cannot be measured offline (operator clicking time, device latency under a real
receiver, the fourth-race blind test) is listed as not measured instead of being
estimated.

Definitions used here, so two reports can be compared:

* 主问题数 - pending items an operator sees at the top level (open/disputed, not
  group members, one entry per batch).  Sub-items and single chips are not
  separate problems.
* 自动通过 - riders paired automatically; a rider inside a still-checked group
  or obstruction period is counted here and marked, not asked about again.
* 需认号 - pending items of the missed-read class (an unidentified crossing to
  look at) plus the members of pending multi-chip batches: identities the
  operator has to read off the video.
* 需点数 - the riders inside pending group/obstruction/standing items
  (``video_count``); counting work that is independent of identities.
* 逐人确认门槛 - a pending item whose subject is one rider who was already
  matched automatically; 0 is expected.  A group, obstruction or batch item
  listing that rider as a member is not such a confirmation, so the carriers
  are counted separately (``auto_passed_also_listed``) instead of being
  reported as a defect.
* 15% 门槛 - a whole-race caliber (top-level video tasks over chips with
  video).  On a cached slice the denominator is only that slice's riders, so
  the number is reported but does not judge unless ``--whole-race`` is given.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from realtime.problem_items import (
    CATEGORY_VIDEO,
    KIND_BUNCH,
    KIND_OBSTRUCTION,
    KIND_STANDING,
    ProblemItem,
    TYPE_BUNCH,
    TYPE_EVIDENCE,
    TYPE_MISSED,
    TYPE_MULTI_CHIP,
    TYPE_ORDER,
    TYPE_RECORD,
    TYPE_TITLES,
)

# The recorded historical baselines of the plan; they use the old counting rule
# (one entry per chip-level finding, no batch de-duplication), so a new number
# is compared, never equated, to them.
HISTORY = {
    "dingbian": {"source": "第三/四步记录：定边 视频26项 / 298条有录像芯片 / 自动通过233",
                 "main_problems": 26, "chips_with_video": 298, "auto_passed": 233},
    "tongxin": {"source": "第四步记录：同心 视频61项 / 416条有录像芯片，加芯片自检后63项",
                "main_problems": 61, "chips_with_video": 416, "auto_passed": None,
                "main_problems_with_chip_checks": 63},
}
TOP_LEVEL_RATIO_LIMIT = 0.15
RESIDUAL_MEDIAN_LIMIT_MS = 50.0
KNOWN_CASE_WINDOW_MS = 5_000

# A rider is the subject of these items; in group/obstruction/batch items the
# listed bibs are members or context, which is the group tool's business.
SUBJECT_KINDS = frozenset({
    "two_chips_one_crossing", "chip_no_crossing", "near_crossing", "repeated_passage",
    "duplicate_read", "duplicate_manual", "before_gun", "early_read", "lap_repeat",
    "lap_regress", "lap_time", "unknown_bib", "manual_review", "test_chip",
})


def recorder_time(payload: dict) -> int | None:
    evidence = payload.get("evidence") or {}
    for key in ("recorder_ms", "recorder_start_ms"):
        if evidence.get(key) is not None:
            return int(evidence[key])
    return None


def reduction(payload: dict) -> dict:
    items = [ProblemItem.from_payload(value) for value in payload.get("items") or []]
    pending = [item for item in items if item.pending]
    auto = {str(entry.get("bib")) for entry in payload.get("auto_passed") or [] if entry.get("active_now", True)}
    by_class = {value: 0 for value in TYPE_ORDER}
    for item in pending:
        by_class[item.task_type] = by_class.get(item.task_type, 0) + 1
    members = [member for item in pending for member in item.evidence.get("batch_members") or []
               if member.get("present", True) and member.get("active", True)]
    listed = {}
    for item in pending:
        for bib in set(item.bibs) & auto:
            listed.setdefault(bib, set()).add(item.kind)
    asked_again = {bib: sorted(kinds) for bib, kinds in listed.items() if kinds & SUBJECT_KINDS}
    confirmations = sum(1 for item in pending
                        if item.bibs and set(item.bibs) <= auto and item.kind in SUBJECT_KINDS)
    group_riders = sum(int(item.evidence.get("video_count") or 0)
                       for item in pending if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION, KIND_STANDING))
    return {
        "main_problems": len(pending),
        "main_problems_by_class": {TYPE_TITLES[value]: by_class[value] for value in TYPE_ORDER},
        "auto_passed": int((payload.get("summary") or {}).get("auto_passed") or len(auto)),
        "auto_passed_in_pending_period": int((payload.get("summary") or {}).get("auto_passed_in_pending_period") or 0),
        # A matched rider never gets a task of their own; 0 is expected.
        "normal_rider_confirmations": confirmations,
        # Informational: group, obstruction and batch items name the riders
        # inside them, which includes riders that matched automatically.
        "auto_passed_also_listed": {"riders": len(listed),
                                    "by_kind": {kind: sum(1 for kinds in listed.values() if kind in kinds)
                                                for kind in sorted({value for kinds in listed.values()
                                                                    for value in kinds})},
                                    "note": "集团/遮挡/批次条目把已自动对应的选手列为成员或对照，不等于要求重新确认"},
        "matched_riders_asked_again": asked_again,
        "to_identify": {
            "missed_read_items": by_class.get(TYPE_MISSED, 0),
            "batch_members": len(members),
            "total": by_class.get(TYPE_MISSED, 0) + len(members),
        },
        "to_count": {
            "group_items": sum(1 for item in pending
                               if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION, KIND_STANDING)),
            "group_riders": group_riders,
        },
        # The counting row's 待证据 cell: everything that only waits for
        # evidence, including the chips no recording covers.
        "evidence_waiting_items": sum(1 for item in items if not item.grouped
                                      and (item.task_type == TYPE_EVIDENCE or item.status == "waiting")),
    }


def gates(payload: dict, camera: int | None = None) -> dict:
    items = [ProblemItem.from_payload(value) for value in payload.get("items") or []]
    cameras = {key: value for key, value in (payload.get("cameras") or {}).items()
               if camera is None or key == str(camera)}
    video_pending = sum(1 for item in items if item.pending and item.category == CATEGORY_VIDEO)
    chips = sum(int((value.get("pairing") or {}).get("chips_with_video") or 0) for value in cameras.values())
    ratio = round(video_pending / chips, 4) if chips else None
    residuals = {key: (value.get("pairing") or {}).get("residual_median_ms") for key, value in cameras.items()}
    spread = {key: (value.get("calibration") or {}).get("residual_mad_ms") for key, value in cameras.items()}
    worst = max([value for value in residuals.values() if value is not None], default=None)
    return {
        "top_level_video_pending_ratio": {
            "value": ratio, "limit": TOP_LEVEL_RATIO_LIMIT,
            "pass": None if ratio is None else ratio <= TOP_LEVEL_RATIO_LIMIT,
            "numerator": video_pending, "denominator_chips_with_video": chips,
            "note": "同一口径的历史回归门槛，不是任意现场的保证；芯片自检项不进入分子",
        },
        "auto_clock_residual_median_ms": {
            "per_camera": residuals, "worst": worst, "limit": RESIDUAL_MEDIAN_LIMIT_MS,
            "pass": None if worst is None else abs(worst) <= RESIDUAL_MEDIAN_LIMIT_MS,
            "residual_mad_ms": spread,
            "note": "有符号残差中位数（与验收记录同口径）；分布宽度另见 residual_mad_ms 与 residual_p90_abs_ms",
        },
    }


def history_row(payload: dict, name: str) -> dict | None:
    if name not in HISTORY:
        return None
    row = dict(HISTORY[name])
    cameras = payload.get("cameras") or {}
    summary = payload.get("summary") or {}
    row.update(new_main_problems=summary.get("pending"), new_auto_passed=summary.get("auto_passed"),
               new_chips_with_video=sum(int((value.get("pairing") or {}).get("chips_with_video") or 0)
                                        for value in cameras.values()))
    return row


def known_case_report(payload: dict, cases: dict, *, window_ms: int = KNOWN_CASE_WINDOW_MS,
                      replay: dict | None = None) -> list[dict]:
    """Whether each known anomaly shows up as traceable evidence, per case."""

    items = [ProblemItem.from_payload(value) for value in payload.get("items") or []]
    auto = {str(entry.get("bib")): entry for entry in payload.get("auto_passed") or []}
    report = []
    for bib, case in sorted(cases.items()):
        when = case.get("recorder_ms")
        frame = case.get("frame") or {}
        near = []
        for item in items:
            if item.grouped:
                continue
            moment = recorder_time(item.to_payload())
            own = item.evidence.get("frame") or {}
            same_frame = (bool(frame.get("segment_id")) and own.get("segment_id") == frame["segment_id"]
                          and frame.get("frame_index") is not None
                          and abs(int(own.get("frame_index", -10**9)) - int(frame["frame_index"])) <= 60)
            same_time = when is not None and moment is not None and abs(moment - int(when)) <= window_ms
            # A rider whose identity the human already verified: the item that
            # carries that bib is where the operator would work the case.
            same_bib = str(bib) in item.bibs
            if same_time or same_frame or same_bib:
                near.append({"item_id": item.item_id, "kind": item.kind, "class": item.task_type,
                             "status": item.status, "bibs": list(item.bibs), "recorder_ms": moment,
                             "matched_by": [name for name, hit in (("time", same_time), ("frame", same_frame),
                                                                   ("bib", same_bib)) if hit],
                             "detail": item.detail[:120]})
        first = None
        if when is not None and replay:
            for step in replay.get("steps") or []:
                if any(moment is not None and abs(int(moment) - int(when)) <= window_ms
                       for moment in (step.get("item_times") or {}).values()):
                    first = {"index": step["index"], "mark": step["mark"],
                             "since_case_ms": int(step["mark"]) - int(when), "settle": step.get("settle")}
                    break
        pending_hits = [entry for entry in near if entry["status"] in ("open", "disputed")]
        report.append({
            "bib": str(bib), "camera": case.get("camera"), "recorder_ms": when,
            "frame_index": (case.get("frame") or {}).get("frame_index"), "note": case.get("note", ""),
            # ``expect``: traceable (default) - appears in some item;
            # candidate - must still be an open task for the operator.
            "expect": case.get("expect", "traceable"),
            "evidence_items": near, "pending_items": [entry["item_id"] for entry in pending_hits],
            "located": bool(near),
            "matched_anyway": str(bib) in auto,
            # When the blind replay first showed it; the final list alone cannot
            # prove the operator was warned early.
            "first_shown_in_replay": first,
        })
    return report


def realtime_row(replay: dict) -> dict:
    metrics = replay.get("metrics") or {}
    return {
        "steps": replay.get("marks"), "checks": replay.get("checks"),
        "max_backlog_ms": metrics.get("max_backlog_ms"),
        "max_seal_to_publish_ms": metrics.get("max_seal_to_publish_ms"),
        "analysis_seconds": metrics.get("analysis_seconds"),
        "peak_mb": metrics.get("peak_mb"), "paused_seconds": metrics.get("paused_seconds"),
        "note": metrics.get("note"),
    }


NOT_MEASURED = [
    "每类问题的点击数、实际操作用时、整场总用时（需要人工走查计时）",
    "人工认号与人工点数的实际人数差异（工具给出的是条目数，人工判断的例外要现场记录）",
    "芯片接收延迟、断线重连、最大积压与录像连续性的设备联调（需要现场摄像机和 CycleRun 实时对照）",
    "覆盖比赛时长的持续负载与磁盘增长",
    "未参与调参的第四场赛事盲测（三场已用于开发的录像只能算历史回归）",
]


def build_report(payload: dict, *, camera: int | None = None, replay: dict | None = None,
                 cases: dict | None = None, history: str = "", whole_race: bool = False) -> dict:
    report = {
        "race_id": payload.get("race_id"), "stage_id": payload.get("stage_id"),
        "generated_at_ms": payload.get("generated_at_ms"), "live": payload.get("live"),
        "whole_race": whole_race,
        "summary": payload.get("summary"), "reduction": reduction(payload), "gates": gates(payload, camera),
        "history": history_row(payload, history) if history else None,
        "known_cases": known_case_report(payload, cases, replay=replay) if cases else None,
        "realtime": realtime_row(replay) if replay else None,
        "not_measured": NOT_MEASURED,
    }
    checks = []
    if report["reduction"]["normal_rider_confirmations"]:
        checks.append("有条目在要求确认一位已经自动对应的正常选手")
    if report["reduction"]["matched_riders_asked_again"]:
        checks.append("自动对应的选手又成了某个条目的当事人，请逐条核对")
    for name, gate in report["gates"].items():
        # The 15% ratio is a whole-race caliber; on a slice it is informative.
        if gate["pass"] is False and (whole_race or name != "top_level_video_pending_ratio"):
            value = gate.get("value") if gate.get("value") is not None else gate.get("worst")
            checks.append(f"{name} 未达门槛：{value}（门槛 {gate['limit']}）")
    if report["known_cases"] is not None:
        for entry in report["known_cases"]:
            if not entry["located"]:
                checks.append(f"已知案例 {entry['bib']} 没有找到对应证据")
            elif entry["expect"] == "candidate" and not entry["pending_items"]:
                checks.append(f"已知案例 {entry['bib']} 只出现在已处理条目里，没有留在待核候选")
    if report["realtime"] and not all((report["realtime"]["checks"] or {}).values()):
        checks.append("盲回放检查未全部通过")
    report["findings"] = checks
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--problem-list", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--camera", type=int)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--known-cases", type=Path)
    parser.add_argument("--history", choices=tuple(HISTORY), default="")
    parser.add_argument("--whole-race", action="store_true",
                        help="这份清单覆盖整场比赛，15% 门槛参与判定")
    args = parser.parse_args(argv)
    payload = json.loads(args.problem_list.read_text(encoding="utf-8"))
    report = build_report(payload, camera=args.camera,
                          replay=json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None,
                          cases=json.loads(args.known_cases.read_text(encoding="utf-8")) if args.known_cases else None,
                          history=args.history, whole_race=args.whole_race)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"reduction": report["reduction"], "gates": report["gates"], "findings": report["findings"]},
                     ensure_ascii=False, default=str))
    print(str(args.output))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
