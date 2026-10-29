"""Where a sealed archive shows the picture a short buffer piece shows.

The recorder keeps each moment twice: two-second buffer pieces, each stamped
with the clock as it is cut, and five-minute archives whose start is only the
end of the one before (302 s each).  So the archives drift from the pieces by
tens of milliseconds a file (现代五项 10-29: 38 ms in the third archive,
194 ms in the eighteenth) and the same moment by the clock is another picture,
one to four frames apart.  Both hold the same encoded pictures, so the archive
frame is found by comparing them; its clock is then moved onto the pieces' and a
ring saved on a piece stays on the same person.
"""

from __future__ import annotations

import cv2
import numpy as np

# Archive frames either side of where the clock puts the picture; a second,
# wider look if the best one is at the edge.
REACH_FRAMES = 8
# Mean grey-level difference (0-255, at 160 x 120) of a picture with its own
# copy: 0.0 measured; next frames of a moving scene differ by about 1.
SAME_PICTURE = 0.5
# The best match is sure only this far ahead of the next best: in a still
# scene neighbouring frames look alike, and any of them would do.
NEXT_APART = 0.5
_SIZE = (160, 120)


def _grey(image):
    return cv2.cvtColor(cv2.resize(image, _SIZE, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.int16)


def _period(capture):
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    return 1000.0 / fps if 1.0 <= fps <= 240.0 else None


def archive_shift_ms(piece_path, piece_start_ms, piece_ms, archive_path, archive_start_ms, guess_ms=0,
                     capture_factory=cv2.VideoCapture):
    """How much later the archive's clock shows the picture the piece shows at PIECE_MS.

    Returns (shift_ms, sure); (None, False) when no archive frame is that picture.
    GUESS_MS is a shift already known for a neighbouring archive.
    """

    piece = capture_factory(str(piece_path))
    try:
        period = _period(piece) if piece.isOpened() else None
        if period is None:
            return None, False
        index = max(0, int(round((int(piece_ms) - int(piece_start_ms)) / period)))
        count = int(piece.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        index = min(index, count - 1) if count > 0 else index
        ok, image = piece.read() if piece.set(cv2.CAP_PROP_POS_FRAMES, index) else (False, None)
    finally:
        piece.release()
    if not ok or image is None:
        return None, False
    reference = _grey(image)
    moment = int(piece_start_ms) + index * period
    archive = capture_factory(str(archive_path))
    try:
        archive_period = _period(archive) if archive.isOpened() else None
        if archive_period is None:
            return None, False
        expected = int(round((moment + int(guess_ms) - int(archive_start_ms)) / archive_period))
        for reach in (REACH_FRAMES, 3 * REACH_FRAMES):
            first = max(0, expected - reach)
            if not archive.set(cv2.CAP_PROP_POS_FRAMES, first):
                return None, False
            scores = []
            for frame in range(first, expected + reach + 1):
                ok, image = archive.read()
                if not ok:
                    break
                scores.append((float(np.abs(_grey(image) - reference).mean()), frame))
            if not scores:
                return None, False
            ranked = sorted(scores)
            difference, frame = ranked[0]
            edge = frame == first and first > 0 or frame == expected + reach
            if difference <= SAME_PICTURE and not edge:
                break
        if difference > SAME_PICTURE:
            return None, False
        sure = len(ranked) < 2 or ranked[1][0] - difference >= NEXT_APART
        return int(round(int(archive_start_ms) + frame * archive_period - moment)), sure
    finally:
        archive.release()
