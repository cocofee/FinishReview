"""Explicit target-sport policy for finish-line analysis.

Cycling is the default so an existing workspace keeps the previous behaviour.
Running is opt-in: the target is a person box, never relabelled as a bicycle.
A person box proves only that a target crossed the line; it does not identify
the athlete.  The policy names the one detected class that may absorb a chip
and the classes that may not, so pairing, late pairing and the operator text
all use the same rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .bike_check import (
    LABEL_BICYCLE,
    LABEL_PEDESTRIAN,
    LABEL_TEXT,
    LABEL_VEHICLE,
    LABEL_NOTHING,
    LABEL_UNCHECKED,
)


CYCLING = "cycling"
RUNNING = "running"
SPORTS = (CYCLING, RUNNING)
DEFAULT_SPORT = CYCLING
POLICY_VERSION = 1
logger = logging.getLogger("FinishReview.TargetSport")


@dataclass(frozen=True)
class TargetSportPolicy:
    """Which detected class counts as the athlete, and how to name it."""

    name: str
    target_label: str
    non_target_labels: tuple[str, ...]
    check_name: str
    target_text: str
    help_text: str
    dialog_title: str
    model_placeholder: str
    notice: str
    # Empty keeps the cycling verification preference exactly as it was;
    # ``pedestrian`` makes a person win when both classes cross together.
    verification_preference: str = ""

    def is_target(self, label: str) -> bool:
        return label == self.target_label

    def is_non_target(self, label: str) -> bool:
        return label in self.non_target_labels

    def label_text(self, label: str) -> str:
        if label == self.target_label:
            return self.target_text
        if self.name == RUNNING and label == LABEL_BICYCLE:
            return "自行车（非目标）"
        if self.name == RUNNING and label == LABEL_VEHICLE:
            return "车辆（非目标）"
        if self.name == RUNNING and label == LABEL_NOTHING:
            return "未认出人形目标"
        if self.name == RUNNING and label == LABEL_UNCHECKED:
            return "未认人"
        return LABEL_TEXT.get(label, label)

    def payload(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "name": self.name,
            "target_label": self.target_label,
            "check_name": self.check_name,
        }


CYCLING_POLICY = TargetSportPolicy(
    name=CYCLING,
    target_label=LABEL_BICYCLE,
    non_target_labels=(LABEL_PEDESTRIAN, LABEL_VEHICLE),
    check_name="认车",
    target_text="自行车",
    help_text="按车形目标计数，不识别号码。",
    dialog_title="实际终点线与认车配置（录制中也可设置）",
    model_placeholder="可选本机 YOLOX ONNX 模型；留空保留候选但不降噪",
    notice="模型为赛事分析共用；终点线和取景方式按机位保存并作为默认沿用。不会改变录像或正式成绩。",
)

RUNNING_POLICY = TargetSportPolicy(
    name=RUNNING,
    target_label=LABEL_PEDESTRIAN,
    non_target_labels=(LABEL_BICYCLE, LABEL_VEHICLE),
    check_name="认人",
    target_text="人形目标",
    help_text="按人形目标计数，不识别身份；自行车和车辆不算跑步选手。",
    dialog_title="实际终点线与认人配置（录制中也可设置）",
    model_placeholder="可选本机 YOLOX ONNX 模型；留空保留候选但不降噪。跑步按人形目标计数，不识别身份",
    notice="跑步模式只把“人”当作过线目标，不代表已识别选手身份；自行车和车辆不会被当成跑步选手。"
           "模型、终点线和取景方式按赛事保存，不会改变录像或正式成绩。",
    verification_preference=LABEL_PEDESTRIAN,
)

POLICIES = {policy.name: policy for policy in (CYCLING_POLICY, RUNNING_POLICY)}
SPORT_CHOICES = ((CYCLING, "自行车（默认）"), (RUNNING, "跑步"))

def normalize_sport(value: Any) -> str:
    """Return a supported sport; invalid or empty values keep cycling compatibility."""

    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_SPORT
    if text not in POLICIES:
        logger.warning("Unknown target_sport=%r; using historical default %s", value, DEFAULT_SPORT)
        return DEFAULT_SPORT
    return text


def policy_for(value: Any) -> TargetSportPolicy:
    return POLICIES[normalize_sport(value)]


__all__ = [
    "CYCLING",
    "CYCLING_POLICY",
    "DEFAULT_SPORT",
    "POLICIES",
    "POLICY_VERSION",
    "RUNNING",
    "RUNNING_POLICY",
    "SPORT_CHOICES",
    "SPORTS",
    "TargetSportPolicy",
    "normalize_sport",
    "policy_for",
]
