"""Versioned, session-scoped sampling settings, separate from motion gates."""

from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from pathlib import Path


CONFIG_PATH = Path("finish_problems/analysis_config.json")
CROP_MODES = ("up", "side-right", "side-left")


def read_settings(root: Path) -> dict:
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("分析配置必须是JSON对象")
    return payload


def normalized_points(line: dict) -> list[list[float]]:
    raw = line["points"] if "points" in line else [[line["x1"], line["y1"]], [line["x2"], line["y2"]]]
    points = [[float(x), float(y)] for x, y in raw]
    if len(points) < 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for p in points for v in p):
        raise ValueError("取样线至少需要两个节点，坐标必须在画面内")
    if any(a == b for a, b in zip(points, points[1:])):
        raise ValueError("相邻节点不能重合")
    return points


def resolved_model_path(root: Path, settings: dict) -> Path | None:
    value = settings.get("model_path")
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(root) / path


def settings_token(root: Path) -> str:
    settings = read_settings(root)
    if not settings:
        return ""
    model = resolved_model_path(root, settings)
    stamp = None
    if model:
        try:
            stat = model.stat()
            stamp = [str(model), stat.st_size, stat.st_mtime_ns]
        except OSError:
            stamp = [str(model), "missing"]
    return hashlib.sha256(json.dumps([settings, stamp], sort_keys=True).encode()).hexdigest()


def model_fingerprint(path: Path, input_size: int) -> str:
    digest = hashlib.sha256()
    digest.update(str(input_size).encode())
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_for_segment(profiles, segment, race_id: str, stage_id: str) -> dict | None:
    matches = [p for p in profiles if p.get("race_id") == race_id and p.get("stage_id", "") == stage_id
               and int(p["camera_index"]) == segment.camera_index and p["session_id"] == segment.session_id
               and int(p["effective_from_ms"]) <= segment.start_ms]
    if not matches:
        return None
    return max(matches, key=lambda p: (int(p["effective_from_ms"]), int(p["revision"])))


def crosses_profile_boundary(profiles, segment, race_id: str, stage_id: str) -> bool:
    # A partially retained archive may straddle a later short-segment setting.
    # Do not sample its uncovered tail with the earlier geometry.
    return any(p.get("race_id") == race_id and p.get("stage_id", "") == stage_id
               and int(p["camera_index"]) == segment.camera_index and p["session_id"] == segment.session_id
               and segment.start_ms < int(p["effective_from_ms"]) < segment.end_ms for p in profiles)


class AnalysisSettingsStore:
    """Save only after validation and atomic replacement; retain every line revision."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / CONFIG_PATH
        self.original = self.path.read_bytes() if self.path.exists() else None
        self.payload = read_settings(root)

    def save(self, *, race_id: str, stage_id: str, segment, line: dict,
             model_path: str, crop_mode: str) -> dict:
        points = normalized_points(line)
        if not race_id or not segment.session_id or crop_mode not in CROP_MODES:
            raise ValueError("缺少赛事/录制会话，或取景方式无效")
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self.original:
            raise ValueError("配置已被其他窗口修改，请重新打开后编辑")
        payload = deepcopy(self.payload)
        profiles = payload.setdefault("sampling_profiles", [])
        revision = max((int(p["revision"]) for p in profiles), default=0) + 1
        profile = dict(camera_index=segment.camera_index, race_id=race_id, stage_id=stage_id,
                       session_id=segment.session_id, effective_from_ms=segment.start_ms,
                       revision=revision, points=points, snap=bool(line.get("snap", False)))
        previous = profile_for_segment(profiles, segment, race_id, stage_id)
        if previous and previous["points"] == points and bool(previous.get("snap")) == profile["snap"]:
            profile = previous
        else:
            profiles.append(profile)
        payload["model_path"] = model_path.strip()
        payload.setdefault("crop_modes", {})[str(segment.camera_index)] = crop_mode
        payload["settings_revision"] = int(payload.get("settings_revision", 0)) + 1
        # Existing camera filters must not silently disable a newly configured camera.
        if payload.get("cameras"):
            payload["cameras"] = sorted(set(map(int, payload["cameras"])) | {segment.camera_index})
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.original is not None:
            backup = self.path.with_name(self.path.name + ".bak")
            backup.write_bytes(self.original)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self.original, self.payload = data, payload
        return profile
