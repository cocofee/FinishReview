import cv2
import numpy as np

from realtime.archive_alignment import archive_shift_ms

ARCHIVE_START = 1_000_000


def picture(index, still=False):
    """A square sliding along; every picture differs from the next unless STILL."""
    image = np.zeros((120, 160, 3), np.uint8)
    left = 0 if still else index * 3 % 150
    cv2.rectangle(image, (left, 40), (left + 10, 80), (255, 255, 255), -1)
    return image


def write(path, pictures):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 20, (160, 120))
    for image in pictures:
        writer.write(image)
    writer.release()
    return path


def test_the_archive_frame_is_found_by_its_picture_not_by_the_clock(tmp_path):
    """现代五项 10-29：同一时刻按时钟在 5 分钟录像里差 1-4 帧，按画面找到同一帧。"""
    archive = write(tmp_path / "camera_01_x_archive_0002.avi", [picture(i) for i in range(300)])
    piece = write(tmp_path / "piece.avi", [picture(i) for i in range(100, 150)])
    piece_start = ARCHIVE_START + 100 * 50 - 150  # the archive's clock runs 150 ms (3 frames) late
    assert archive_shift_ms(piece, piece_start, piece_start + 10 * 50, archive, ARCHIVE_START) == (150, True)
    # Further off than one look reaches: found on the wider second look, or from a neighbour's shift.
    late = ARCHIVE_START + 100 * 50 - 600
    assert archive_shift_ms(piece, late, late + 500, archive, ARCHIVE_START) == (600, True)
    assert archive_shift_ms(piece, late, late + 500, archive, ARCHIVE_START, guess_ms=550) == (600, True)


def test_a_picture_the_archive_does_not_hold_is_not_matched(tmp_path):
    archive = write(tmp_path / "camera_01_x_archive_0002.avi", [picture(i) for i in range(300)])
    other = [cv2.flip(picture(i), 0) + 40 for i in range(50)]
    piece = write(tmp_path / "piece.avi", other)
    assert archive_shift_ms(piece, ARCHIVE_START + 5000, ARCHIVE_START + 5500, archive, ARCHIVE_START) == (None, False)


def test_in_a_still_scene_the_match_is_not_sure(tmp_path):
    archive = write(tmp_path / "camera_01_x_archive_0002.avi", [picture(i, still=True) for i in range(300)])
    piece = write(tmp_path / "piece.avi", [picture(i, still=True) for i in range(50)])
    shift, sure = archive_shift_ms(piece, ARCHIVE_START + 5000, ARCHIVE_START + 5500, archive, ARCHIVE_START)
    assert shift is not None and not sure
