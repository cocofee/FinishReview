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
# A line drawn before the race scope was known may belong to an earlier race
# of the same folder; it carries over to other sessions for a day only.
PRE_RACE_LINE_CARRY_MS = 24 * 60 * 60 * 1000


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


def profile_scope_matches(profile, race_id: str, stage_id: str) -> bool:
    """An empty race/stage is a pre-race line: it applies once the scope is known.

    The session id still binds the profile to this recording, so the wildcard
    cannot leak a line into another race of the same folder.
    """

    return ((not profile.get("race_id") or profile.get("race_id") == race_id)
            and (not profile.get("stage_id") or profile.get("stage_id") == stage_id))


def profile_for_segment(profiles, segment, race_id: str, stage_id: str) -> dict | None:
    matches = [p for p in profiles if profile_scope_matches(p, race_id, stage_id)
               and int(p["camera_index"]) == segment.camera_index and p["session_id"] == segment.session_id
               and int(p["effective_from_ms"]) <= segment.start_ms]
    if not matches:
        return None
    return max(matches, key=lambda p: (int(p["effective_from_ms"]), int(p["revision"])))


def inherited_profile(profiles, camera_index: int, at_ms: int, race_id: str) -> dict | None:
    """The camera's latest line drawn before ``at_ms``, from any recording session.

    A restart, a build switch or a second instance starts a session without a
    profile of its own, and re-saving the line later must not orphan it: the
    camera did not move because the program restarted.  A line carries over
    within its race (into later stages too, like the camera default); a
    pre-race line only for ``PRE_RACE_LINE_CARRY_MS``.
    """

    matches = [p for p in profiles if int(p["camera_index"]) == int(camera_index)
               and int(p["effective_from_ms"]) <= int(at_ms)
               and (p.get("race_id") == race_id if p.get("race_id")
                    else int(at_ms) - int(p["effective_from_ms"]) < PRE_RACE_LINE_CARRY_MS)]
    if not matches:
        return None
    return max(matches, key=lambda p: (int(p["effective_from_ms"]), int(p["revision"])))


def crosses_profile_boundary(profiles, segment, race_id: str, stage_id: str) -> bool:
    # A partially retained archive may straddle a later short-segment setting.
    # Do not sample its uncovered tail with the earlier geometry.
    return any(profile_scope_matches(p, race_id, stage_id)
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
             model_path: str, crop_mode: str, effective_from_ms: int | None = None) -> dict:
        points = normalized_points(line)
        # An empty race_id is the pre-race case: the line is tied to the
        # recording session now and picks up the race scope when it arrives.
        if not segment.session_id or crop_mode not in CROP_MODES:
            raise ValueError("缺少录制会话，或取景方式无效")
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self.original:
            raise ValueError("配置已被其他窗口修改，请重新打开后编辑")
        payload = deepcopy(self.payload)
        profiles = payload.setdefault("sampling_profiles", [])
        revision = max((int(p["revision"]) for p in profiles), default=0) + 1
        starts_at = int(effective_from_ms) if effective_from_ms is not None else int(segment.start_ms)
        profile = dict(camera_index=segment.camera_index, race_id=race_id, stage_id=stage_id,
                       session_id=segment.session_id, effective_from_ms=min(starts_at, int(segment.start_ms)),
                       revision=revision, points=points, snap=bool(line.get("snap", False)))
        previous = profile_for_segment(profiles, segment, race_id, stage_id)
        if previous and previous["points"] == points and bool(previous.get("snap")) == profile["snap"]:
            profile = previous
        else:
            profiles.append(profile)
        # Draw once at the start of the race.  This copy is the camera's
        # default line: a new recording session (app restart, build switch,
        # second instance) has no profile of its own, and without a default
        # the batch detector silently skips it.  A later session profile still
        # overrides this copy when the camera moves.
        defaults = payload.get("finish_lines")
        defaults = defaults if isinstance(defaults, list) else []
        camera_index = int(segment.camera_index)
        defaults = [item for item in defaults
                    if not (isinstance(item, dict) and int(item.get("camera_index", 0) or 0) == camera_index)]
        defaults.append(dict(camera_index=camera_index, race_id=race_id, stage_id=stage_id,
                             effective_from_ms=min(starts_at, int(segment.start_ms)),
                             points=points, snap=bool(line.get("snap", False))))
        payload["finish_lines"] = defaults
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
