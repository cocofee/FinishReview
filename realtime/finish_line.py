"""Persisted finish-line geometry for ordinary-video assistance."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FinishLine:
    """A normalized line and analysis band for one camera."""

    camera_index: int
    x1: float
    y1: float
    x2: float
    y2: float
    band_width: float = 0.08

    def __post_init__(self) -> None:
        if self.camera_index <= 0:
            raise ValueError("camera_index must be positive")
        values = (self.x1, self.y1, self.x2, self.y2, self.band_width)
        if not all(0 <= value <= 1 for value in values):
            raise ValueError("终点线坐标和宽度必须在0到1之间")
        if self.band_width <= 0:
            raise ValueError("band_width must be positive")
        if self.x1 == self.x2 and self.y1 == self.y2:
            raise ValueError("终点线长度不能为零")

    @property
    def roi(self) -> tuple[float, float, float, float]:
        """Return a conservative rectangular ROI surrounding the line."""
        half = self.band_width / 2
        return (
            max(0.0, min(self.x1, self.x2) - half),
            max(0.0, min(self.y1, self.y2) - half),
            min(1.0, max(self.x1, self.x2) + half),
            min(1.0, max(self.y1, self.y2) + half),
        )

    def signed_side(self, x: float, y: float) -> float:
        """Return the cross-product side of a normalized point."""
        return (self.x2 - self.x1) * (y - self.y1) - (
            self.y2 - self.y1
        ) * (x - self.x1)

    def projection(self, x: float, y: float) -> float:
        """Return the point projection ratio along the line segment."""
        dx = self.x2 - self.x1
        dy = self.y2 - self.y1
        length_squared = dx * dx + dy * dy
        return ((x - self.x1) * dx + (y - self.y1) * dy) / length_squared

    def contains_crossing(
        self,
        previous: tuple[float, float],
        current: tuple[float, float],
    ) -> bool:
        """Check whether a motion point crossed this line near its segment."""
        previous_side = self.signed_side(*previous)
        current_side = self.signed_side(*current)
        if previous_side == 0 or current_side == 0:
            crossed = True
        else:
            crossed = previous_side * current_side < 0
        if not crossed:
            return False
        margin = max(0.05, self.band_width)
        return (
            -margin <= self.projection(*previous) <= 1 + margin
            or -margin <= self.projection(*current) <= 1 + margin
        )


class FinishLineStore:
    """Small atomic JSON store, independent from official timing data."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lines: dict[int, FinishLine] = {}
        self._rois: dict[int, tuple[float, float, float, float]] = {}
        self.load()

    def load(self) -> tuple[FinishLine, ...]:
        self._lines = {}
        self._rois = {}
        if not self.path.is_file():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = (
                payload.get("finish_lines", payload)
                if isinstance(payload, dict)
                else payload
            )
            if not isinstance(records, list):
                return ()
            for record in records:
                line = FinishLine(**record)
                self._lines[line.camera_index] = line
            raw_rois = payload.get("rois", {}) if isinstance(payload, dict) else {}
            if isinstance(raw_rois, dict):
                for camera_index, raw_roi in raw_rois.items():
                    if not isinstance(raw_roi, (list, tuple)) or len(raw_roi) != 4:
                        continue
                    roi = tuple(float(value) for value in raw_roi)
                    left, top, right, bottom = roi
                    if 0 <= left < right <= 1 and 0 <= top < bottom <= 1:
                        self._rois[int(camera_index)] = roi
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._lines = {}
            self._rois = {}
        return self.lines()

    def lines(self) -> tuple[FinishLine, ...]:
        return tuple(self._lines[index] for index in sorted(self._lines))

    def get(self, camera_index: int) -> FinishLine | None:
        return self._lines.get(int(camera_index))

    def rois(self) -> dict[int, tuple[float, float, float, float]]:
        result = {line.camera_index: line.roi for line in self.lines()}
        result.update(self._rois)
        return result

    def get_roi(self, camera_index: int) -> tuple[float, float, float, float] | None:
        return self.rois().get(int(camera_index))

    def set_roi(
        self,
        camera_index: int,
        roi: tuple[float, float, float, float],
    ) -> None:
        camera_index = int(camera_index)
        if camera_index <= 0:
            raise ValueError("camera_index must be positive")
        values = tuple(float(value) for value in roi)
        if len(values) != 4:
            raise ValueError("roi must contain four values")
        left, top, right, bottom = values
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("roi must be normalized and non-empty")
        self._rois[camera_index] = values
        self._save()

    def set(self, line: FinishLine) -> None:
        self._lines[line.camera_index] = line
        # An explicit rectangle is only a fallback when no line is configured.
        self._rois.pop(line.camera_index, None)
        self._save()

    def remove(self, camera_index: int) -> None:
        camera_index = int(camera_index)
        self._lines.pop(camera_index, None)
        self._rois.pop(camera_index, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "finish_lines": [asdict(line) for line in self.lines()],
            "rois": {
                str(camera_index): list(roi)
                for camera_index, roi in sorted(self._rois.items())
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


__all__ = ["FinishLine", "FinishLineStore"]
