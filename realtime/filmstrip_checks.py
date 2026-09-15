"""Manual inspection coverage, independent of athlete judgments."""

from __future__ import annotations

import json
from pathlib import Path

from .durable_jsonl import append_jsonl_records, locked_jsonl


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(tuple(pair) for pair in ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def subtract_ranges(ranges, removed):
    result = merge_ranges(ranges)
    for left, right in merge_ranges(removed):
        remaining = []
        for start, end in result:
            if end <= left or start >= right:
                remaining.append((start, end))
            else:
                if start < left:
                    remaining.append((start, left))
                if end > right:
                    remaining.append((right, end))
        result = tuple(remaining)
    return result


class FilmstripCheckStore:
    """Append-only check/uncheck actions scoped to the race and camera."""

    def __init__(self, path: Path, race_id: str, camera_index: int):
        self.path = Path(path)
        self.race_id = race_id
        self.camera_index = camera_index
        self.ranges = self._read()

    def _read(self):
        ranges = ()
        if not self.path.exists():
            return ranges
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if (not isinstance(record, dict) or record.get("schema_version") != 1
                    or not isinstance(record.get("checked"), bool)
                    or not isinstance(record.get("race_id"), str)
                    or type(record.get("camera_index")) is not int
                    or not isinstance(record.get("ranges"), list)):
                raise ValueError("检查进度记录格式无效")
            for pair in record["ranges"]:
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(type(value) is not int for value in pair)
                        or not 0 <= pair[0] < pair[1]):
                    raise ValueError("检查进度的时间范围无效")
            if (record["race_id"], record["camera_index"]) != (self.race_id, self.camera_index):
                continue
            changes = record["ranges"]
            ranges = (merge_ranges((*ranges, *changes)) if record["checked"]
                      else subtract_ranges(ranges, changes))
        return ranges

    def set_checked(self, ranges, checked: bool):
        changes = merge_ranges(ranges)
        if not changes:
            return
        if any(type(value) is not int for pair in changes for value in pair) or changes[0][0] < 0:
            raise ValueError("检查进度的时间范围无效")
        record = {"schema_version": 1, "race_id": self.race_id,
                  "camera_index": self.camera_index, "checked": bool(checked),
                  "ranges": changes}
        with locked_jsonl(self.path):
            current = self._read()
            updated = (merge_ranges((*current, *changes)) if checked
                       else subtract_ranges(current, changes))
            append_jsonl_records(self.path, [json.dumps(record).encode("utf-8")],
                                 description="filmstrip inspection coverage", already_locked=True)
            self.ranges = updated
