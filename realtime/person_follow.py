"""A number that walks with its runner on the picture.

The analysis keeps, for every person it saw cross the line, where their feet
were from a little before to a little after the crossing.  The review picture
uses it to carry a number — a chip's automatic pairing, or the operator's own
mark — along with the runner, instead of leaving it on the line.

The number comes from the chip a crossing was paired with, or from the
operator; nothing here reads a bib.  An automatic number is only a pairing,
never a confirmation.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# Same folder as problem_analysis.OUTPUT_DIRNAME (kept apart to stay Qt-free).
OUTPUT_DIRNAME = "finish_problems"
FOLLOW_VERSION = 1
# The number walks with its runner this long before and after the crossing.
FOLLOW_BEFORE_MS = 1500
FOLLOW_AFTER_MS = 1500
# Two looks further apart than this are not joined: the person was not seen.
MAX_GAP_MS = 450
# A mark belongs to the person whose feet were this close (in body heights)
# and this close in time.
MATCH_HEIGHTS = 0.6
MATCH_MS = 250


def follow_path(output_dir: Path, camera_index: int) -> Path:
    return Path(output_dir) / f"person_tracks_camera{int(camera_index)}.json"


@dataclass(frozen=True)
class FollowTrack:
    crossing_ms: int
    bib: str
    times: tuple[float, ...]
    # Feet (bottom centre of the box) and the body height, normalised 0..1.
    xs: tuple[float, ...]
    ys: tuple[float, ...]
    heights: tuple[float, ...]

    def at(self, when: float, *, reach_ms: float = 0.0) -> Optional[tuple[float, float, float]]:
        """Feet and height at ``when``, between two looks no more than ``MAX_GAP_MS`` apart."""

        times = self.times
        if not times or when < times[0] - reach_ms or when > times[-1] + reach_ms:
            return None
        index = bisect_left(times, when)
        if index < len(times) and times[index] == when:
            return self.xs[index], self.ys[index], self.heights[index]
        if index == 0 or index == len(times):
            nearest = 0 if index == 0 else len(times) - 1
            return self.xs[nearest], self.ys[nearest], self.heights[nearest]
        left, right = index - 1, index
        span = times[right] - times[left]
        if span > MAX_GAP_MS:
            return None
        weight = (when - times[left]) / span
        return (self.xs[left] + weight * (self.xs[right] - self.xs[left]),
                self.ys[left] + weight * (self.ys[right] - self.ys[left]),
                self.heights[left] + weight * (self.heights[right] - self.heights[left]))

    def near(self, now_ms: float, around_ms: float) -> bool:
        return -FOLLOW_BEFORE_MS <= now_ms - around_ms <= FOLLOW_AFTER_MS


def follow_payload(people, matched_bibs: dict, camera_index: int) -> dict:
    """The file the picture reads: each crossing person's path, with their chip's number if paired."""

    geometry = people.files[-1].geometry
    width, height = float(geometry.width), float(geometry.height)
    tracks = []
    for crossing in people.crossings:
        path = people.paths.get(crossing.record.key) or []
        if len(path) < 2:
            continue
        tracks.append({
            "crossing_ms": int(crossing.record.recorder_ms),
            "bib": str(matched_bibs.get(crossing.record.key, "")),
            "points": [[int(t), round((x + w / 2) / width, 4), round((y + h) / height, 4), round(h / height, 4)]
                       for t, x, y, w, h in path],
        })
    return {"version": FOLLOW_VERSION, "camera_index": int(camera_index),
            "video_size": [int(width), int(height)], "tracks": tracks}


def _parse(payload) -> tuple[tuple[FollowTrack, ...], float]:
    if not isinstance(payload, dict) or payload.get("version") != FOLLOW_VERSION:
        return (), 1.0
    size = payload.get("video_size") or [1, 1]
    aspect = float(size[0]) / max(1.0, float(size[1]))
    tracks = []
    for item in payload.get("tracks", []):
        points = sorted(item.get("points") or [])
        if len(points) < 2:
            continue
        tracks.append(FollowTrack(
            crossing_ms=int(item.get("crossing_ms", 0)), bib=str(item.get("bib", "")),
            times=tuple(float(p[0]) for p in points), xs=tuple(float(p[1]) for p in points),
            ys=tuple(float(p[2]) for p in points), heights=tuple(float(p[3]) for p in points)))
    tracks.sort(key=lambda track: track.crossing_ms)
    return tuple(tracks), aspect


class FollowTracks:
    """The saved paths of one camera, reread only when the analysis rewrites them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp = None
        self.tracks: tuple[FollowTrack, ...] = ()
        self.aspect = 1.0
        self._starts: list[int] = []

    def refresh(self) -> "FollowTracks":
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return self
        self._stamp = stamp
        try:
            self.tracks, self.aspect = _parse(json.loads(self.path.read_text(encoding="utf-8"))) if stamp else ((), 1.0)
        except (OSError, ValueError, TypeError, IndexError):
            self.tracks, self.aspect = (), 1.0
        self._starts = [track.crossing_ms for track in self.tracks]
        return self

    def around(self, now_ms: float) -> Sequence[FollowTrack]:
        """Tracks whose crossing is close enough to ``now_ms`` to be followed."""

        lo = bisect_left(self._starts, now_ms - FOLLOW_AFTER_MS - MATCH_MS)
        hi = bisect_left(self._starts, now_ms + FOLLOW_BEFORE_MS + MATCH_MS + 1)
        return self.tracks[lo:hi]

    def owner(self, candidates: Sequence[FollowTrack], when_ms: float, x: float, y: float) -> Optional[FollowTrack]:
        """The person a mark at (x, y) on the frame at ``when_ms`` was put on."""

        best, best_distance = None, MATCH_HEIGHTS
        for track in candidates:
            here = track.at(when_ms, reach_ms=MATCH_MS)
            if here is None:
                continue
            fx, fy, body = here
            distance = (((fx - x) * self.aspect) ** 2 + (fy - y) ** 2) ** 0.5 / max(1e-6, body)
            if distance < best_distance:
                best, best_distance = track, distance
        return best


__all__ = ["FOLLOW_AFTER_MS", "FOLLOW_BEFORE_MS", "FollowTrack", "FollowTracks", "follow_path",
           "follow_payload"]
