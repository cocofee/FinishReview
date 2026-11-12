"""Let a counted person's number tag ride along with them for a moment (11-12).

The operator clicks a person on the finish line and the recording plays on;
the tag following that person for about a second shows who is already
counted.  Plain template matching on a small grey picture, forward over
consecutive frames only: when the match is weak, after a jump, or past
FOLLOW_MS the tag stops instead of guessing.  It is a display aid only and
never changes a count, a bib or a saved spot.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

FOLLOW_MS = 1500
WORK_WIDTH = 480  # px of the grey picture matched on
PATCH = 14  # template half-size, px at WORK_WIDTH
SEARCH = 40  # how far the person may move between two shown frames, px at WORK_WIDTH
MAX_FRAME_GAP = 3
MIN_SCORE = 0.5


@dataclass
class _Track:
    key: tuple
    label: str
    camera: int
    segment: object
    frame_index: int
    start_ms: int
    x: float
    y: float
    template: np.ndarray


def grey_picture(image) -> np.ndarray | None:
    """A QImage as a small grey array (WORK_WIDTH wide)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage

    if image is None or image.isNull() or image.width() <= 0:
        return None
    small = image.scaledToWidth(WORK_WIDTH, Qt.FastTransformation).convertToFormat(QImage.Format_Grayscale8)
    width, height, stride = small.width(), small.height(), small.bytesPerLine()
    pointer = small.constBits()
    pointer.setsize(stride * height)
    return np.frombuffer(pointer, np.uint8).reshape(height, stride)[:, :width].copy()


def _patch(grey, x, y, half):
    height, width = grey.shape
    cx, cy = int(round(x * width)), int(round(y * height))
    left, top = max(0, cx - half), max(0, cy - half)
    right, bottom = min(width, cx + half + 1), min(height, cy + half + 1)
    return grey[top:bottom, left:right], left, top


class MarkFollower:
    def __init__(self):
        # The frame shown before, kept as its picture: a click lands on a frame
        # already on the screen, and the next frame is matched against that one.
        self._last = None  # [camera, segment, frame_index, image, grey or None]
        self._tracks: dict[tuple, _Track] = {}

    def reset(self):
        self._last, self._tracks = None, {}

    @staticmethod
    def _grey_of(shown):
        if shown[4] is None:
            shown[4] = grey_picture(shown[3])
        return shown[4]

    def update(self, frame, image, marks):
        """[(x, y, label)] for the followed people on this frame.

        FRAME is the shown frame (camera_index, segment_id, frame_index,
        recorder_ms); MARKS the counted people of the picture, each a dict with
        key, x, y, label, camera, segment, frame_index, recorder_ms.
        """
        if frame is None or image is None:
            self.reset()
            return []
        camera, segment = int(frame.get("camera_index") or 1), frame.get("segment_id")
        index, now = int(frame["frame_index"]), int(frame["recorder_ms"])
        last, current = self._last, [camera, segment, index, image, None]
        self._last = current
        wanted = {mark["key"]: mark for mark in marks
                  if mark["camera"] == camera and mark["segment"] == segment
                  and -FOLLOW_MS <= now - mark["recorder_ms"] <= FOLLOW_MS}
        self._tracks = {key: track for key, track in self._tracks.items() if key in wanted}
        if not wanted:
            return []
        same_piece = last is not None and last[0] == camera and last[1] == segment
        for key, mark in wanted.items():
            if key in self._tracks:
                continue
            # Clicked on this very frame (shown again), or on the one shown before.
            source = current if mark["frame_index"] == index else (
                last if same_piece and mark["frame_index"] == last[2] else None)
            if source is None or self._grey_of(source) is None:
                continue
            template, _left, _top = _patch(self._grey_of(source), mark["x"], mark["y"], PATCH)
            if template.shape[0] > PATCH and template.shape[1] > PATCH:
                self._tracks[key] = _Track(key, mark["label"], camera, segment, source[2],
                                           int(mark["recorder_ms"]), mark["x"], mark["y"], template)
        if not self._tracks:
            return []
        grey = self._grey_of(current)
        if grey is None:
            self.reset()
            return []
        shown = []
        for key, track in list(self._tracks.items()):
            gap = index - track.frame_index
            if gap == 0:  # the same frame drawn again (playback starting, a sharper copy)
                if (track.x, track.y) != (wanted[key]["x"], wanted[key]["y"]):
                    shown.append((track.x, track.y, track.label))
                continue
            if not 0 < gap <= MAX_FRAME_GAP or now - track.start_ms > FOLLOW_MS:
                del self._tracks[key]
                continue
            area, left, top = _patch(grey, track.x, track.y, PATCH + SEARCH * gap)
            if area.shape[0] < track.template.shape[0] or area.shape[1] < track.template.shape[1]:
                del self._tracks[key]
                continue
            scores = cv2.matchTemplate(area, track.template, cv2.TM_CCOEFF_NORMED)
            _low, best, _where, (bx, by) = cv2.minMaxLoc(scores)
            if not np.isfinite(best) or best < MIN_SCORE:
                del self._tracks[key]
                continue
            height, width = grey.shape
            half_h, half_w = track.template.shape[0] // 2, track.template.shape[1] // 2
            track.x = (left + bx + half_w) / width
            track.y = (top + by + half_h) / height
            track.frame_index = index
            track.template, _left, _top = _patch(grey, track.x, track.y, PATCH)
            shown.append((track.x, track.y, track.label))
        return shown
