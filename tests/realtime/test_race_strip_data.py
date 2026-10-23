"""Backend data tests for the whole-race strip.

The picture must place each sampled recording on the chip clock, prefer the
long archive over clips cut from it, keep gaps blank, and jump to the right
moment of the original file.  Qt panel behaviour lives in its own suite.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from realtime.analysis_workspace import PASSAGE_JOURNAL, SCOPE_FILE, TIMELINE_JOURNAL
from realtime.clock_sync import CLOCK_MODEL_FILENAME, ClockModel, ClockModelStore
from realtime.line_sampling import LineGeometry, LineStrip, read_strip_meta, save_strip
from realtime.passage_receiver import PassageEvent
from realtime.race_strip import (
    STRIP_ROWS,
    StripCache,
    StripPiece,
    frame_at,
    render_strip,
    strip_folder,
    strip_pieces,
)
from realtime.video_timeline import DEFAULT_CLOCK_SOURCE, VideoTimelineStore


T = 1_789_784_000_000
FRAME_MS = 40.0
# Two frame colours as BGR, so a mixed pixel proves the averaging path.
RED = (0, 0, 255)
GREEN = (0, 255, 0)
BLUE = (255, 0, 0)
GREY = [205, 205, 205]


@pytest.fixture(autouse=True)
def aligned_cameras(tmp_path):
    # These data fixtures share the chip clock. Unaligned-session behaviour
    # is tested separately instead of depending on a missing clock journal.
    for camera in (1, 2):
        ClockModelStore(tmp_path / CLOCK_MODEL_FILENAME).publish(ClockModel(
            camera, 0, 0, T, T - 86_400_000, T + 86_400_000, 10, 10, 0, 0, 10))


def _segment(root, *, name, start_ms, duration_ms, camera=1, end_reason="stopped",
             clock_source=DEFAULT_CLOCK_SOURCE, race_id="race"):
    """A closed, playable recording in the workspace timeline."""

    store = VideoTimelineStore(root / TIMELINE_JOURNAL)
    video = root / "videos" / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    segment = store.add_completed_segment(
        source_id=f"camera_{camera:02d}_review",
        camera_index=camera,
        video_path=video,
        media_started_at_ms=int(start_ms),
        media_duration_ms=int(duration_ms),
        clock_source=clock_source,
        timing_error_ms=10,
        end_reason=end_reason,
        race_id=race_id,
    )
    return segment, video


def _chip_read(root, chip_ms, *, race_id="race", stage_id="stage"):
    """One active chip record, enough for the race clock window."""

    (root / SCOPE_FILE).write_text(
        json.dumps({"race_id": race_id, "stage_id": stage_id}), encoding="utf-8"
    )
    event = PassageEvent(
        event_id="event-1",
        race_id=race_id,
        stage_id=stage_id,
        group_id="1",
        sequence=1,
        chip_id="chip-1",
        bib="101",
        passage_time_ms=int(chip_ms),
        passage_timestamp_ms=int(chip_ms),
        emitted_at_ms=int(chip_ms),
        received_at_ms=int(chip_ms) + 100,
    )
    (root / PASSAGE_JOURNAL).write_text(
        json.dumps(event.to_payload(), ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _save(path, *, segment_id, start_ms, duration_ms, camera=1, session="s1",
          frames=25, color=(0, 0, 255), colors=None, pts=None):
    """One strip cache file with real save_strip metadata."""

    if pts is None:
        pts = np.arange(frames, dtype=np.float64) * FRAME_MS
    pts = np.asarray(pts, dtype=np.float64)
    samples = 8
    strip = np.empty((samples, pts.size, 3), np.uint8)
    if colors is None:
        strip[:] = color
    else:
        strip[:] = np.asarray(colors, dtype=np.uint8)[None, :, :]
    geometry = LineGeometry.from_config(
        {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5}, 200, 100, samples=samples
    )
    save_strip(
        path,
        LineStrip(strip=strip, pts_ms=pts, geometry=geometry,
                  video_path=str(path.with_suffix(".mkv")), fps=25.0),
        {
            "segment_id": segment_id,
            "session_id": session,
            "camera_index": int(camera),
            "media_started_at_ms": int(start_ms),
            "media_duration_ms": int(duration_ms),
        },
    )
    return path


def _save_segment(root, segment, *, path=None, **values):
    if path is None:
        path = strip_folder(root, segment.camera_index) / (
            Path(segment.video_path).stem + ".npz"
        )
    return _save(
        path,
        segment_id=segment.segment_id,
        camera=segment.camera_index,
        start_ms=segment.media_started_at_ms,
        duration_ms=segment.media_duration_ms,
        **values,
    )


def _corrupt_zip_member(path, member="strip.npy"):
    """Keep metadata readable while the strip member fails its checksum."""

    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    raw = bytearray(path.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
    assert info.compress_size > 2
    raw[start + info.compress_size // 2] ^= 0xFF
    path.write_bytes(bytes(raw))


def test_strip_pieces_uses_the_session_clock_offset_and_linear_drift(tmp_path):
    segment, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=4_000
    )
    _save_segment(tmp_path, segment, frames=20)
    fitted = ClockModelStore(tmp_path / CLOCK_MODEL_FILENAME).publish(ClockModel(
        camera_index=1,
        intercept_ms=2_400.0,
        slope=0.005,
        reference_ms=T,
        valid_from_ms=T - 10_000,
        valid_to_ms=T + 10_000,
        support=10,
        candidates=10,
        residual_mad_ms=0.0,
        coarse_offset_ms=2_400,
        coarse_support=10,
        session_offsets=(("s1", 300.0),),
    ))

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert len(pieces) == 1
    piece = pieces[0]
    end = T + 4_000
    first = fitted.to_chip(T, "s1")
    last = fitted.to_chip(end, "s1")
    assert piece.aligned is True
    assert piece.offset_ms == T - first
    assert piece.drift_ms == (end - last) - (T - first)
    assert piece.drift_ms != 0
    assert piece.chip_of(T) == pytest.approx(first, abs=0.5)
    assert piece.chip_of(end) == pytest.approx(last, abs=0.5)

    times, image = StripCache().get(piece)

    assert image.shape == (STRIP_ROWS, 20, 3)
    assert times[0] == pytest.approx(fitted.to_chip(T, "s1"), abs=1)
    assert times[-1] == pytest.approx(fitted.to_chip(T + 760, "s1"), abs=1)
    hit = frame_at(pieces, fitted.to_chip(T + 1_000, "s1"))
    assert hit is not None
    assert hit["segment_id"] == segment.segment_id
    assert hit["position_ms"] == pytest.approx(1_000, abs=2)


def test_chip_and_recorder_conversions_round_trip_for_a_realistic_drift():
    piece = StripPiece(
        Path("strip.npz"), 1, "s", Path("video.mkv"),
        T, T + 4_000, True, 2_400, 20, True,
    )

    for recorder_ms in (T, T + 1_000, T + 2_000, T + 4_000):
        assert piece.recorder_of(piece.chip_of(recorder_ms)) == pytest.approx(
            recorder_ms, abs=1
        )
    assert piece.chip_of(piece.start_ms) == pytest.approx(piece.chip_start)
    assert piece.chip_of(piece.end_ms) == pytest.approx(piece.chip_end)


def test_recorder_of_exactly_inverts_chip_of_for_a_large_but_legal_drift():
    start = T
    span = 3_600_000
    drift = 32_400                      # ClockModel accepts |slope| < 0.01
    piece = StripPiece(
        Path("strip.npz"), 1, "s", Path("video.mkv"),
        start, start + span, True, 2_400, drift, True,
    )

    assert piece.recorder_of(piece.chip_end) == pytest.approx(start + span, abs=2)


def test_archive_outranks_its_clip_and_the_clip_fills_outside_it(tmp_path):
    archive, archive_video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=12_000
    )
    clip, _clip_video = _segment(
        tmp_path, name="camera_01_s1_review_0001.ts", start_ms=T + 10_000,
        duration_ms=4_000, end_reason="retained_problem_segment",
    )
    _save_segment(tmp_path, archive, color=RED, frames=300)
    _save_segment(tmp_path, clip, color=GREEN, frames=100)

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert [piece.archive for piece in pieces] == [True, False]
    canvas = render_strip(pieces, StripCache(), T, 100.0, 170)
    assert canvas[0, 30].tolist() == list(RED)       # archive only
    assert canvas[0, 110].tolist() == list(RED)      # both; the archive wins
    assert canvas[0, 130].tolist() == list(GREEN)    # clip beyond the archive
    assert canvas[0, 160].tolist() == GREY           # no recording at all

    hit = frame_at(pieces, T + 10_500)
    assert hit is not None
    assert hit["segment_id"] == archive.segment_id
    assert hit["position_ms"] == 10_500
    assert Path(hit["video_path"]) == archive_video
    assert frame_at(pieces, T + 30_000) is None


def test_render_averages_frames_inside_a_pixel_and_holds_or_blanks_the_rest(tmp_path):
    path = _save(
        tmp_path / "average.npz", segment_id="s", start_ms=0, duration_ms=2_000,
        pts=[0, 40, 80, 120], colors=[RED, BLUE, RED, BLUE],
    )
    piece = StripPiece(path, 1, "s", tmp_path / "video.mkv", 0, 2_000, True, 0, 0, True)

    canvas = render_strip([piece], StripCache(), 0, 80.0, 4)

    assert canvas[0, 0].tolist() == [128, 0, 128]    # frames 0 and 40 averaged
    assert canvas[0, 1].tolist() == [128, 0, 128]    # frames 80 and 120 averaged
    assert canvas[0, 2].tolist() == list(BLUE)       # one frame held to the next
    assert canvas[0, 3].tolist() == GREY             # past the hold distance


def test_strip_cache_reduces_to_the_display_rows_and_reuses_the_image(tmp_path):
    path = _save(
        tmp_path / "cache.npz", segment_id="s", start_ms=T, duration_ms=4_000,
        color=(7, 8, 9), frames=60,
    )
    piece = StripPiece(path, 1, "s", tmp_path / "video.mkv", T, T + 4_000,
                       True, 0, 0, True)
    cache = StripCache()

    times, image = cache.get(piece)
    again_times, again_image = cache.get(piece)

    assert image.shape == (STRIP_ROWS, 60, 3)
    assert np.all(image == np.array([7, 8, 9], np.uint8))
    assert again_image is image
    assert again_times is times


def test_the_clip_fills_when_the_archive_strip_cannot_be_decoded(tmp_path):
    archive, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=4_000
    )
    clip, _clip_video = _segment(
        tmp_path, name="camera_01_s1_review_0001.ts", start_ms=T + 1_000,
        duration_ms=2_000, end_reason="retained_problem_segment",
    )
    archive_path = _save_segment(tmp_path, archive, color=RED, frames=100)
    _save_segment(tmp_path, clip, color=GREEN, frames=50)
    _corrupt_zip_member(archive_path)
    assert read_strip_meta(archive_path) is not None

    pieces, _metas = strip_pieces(tmp_path, 1)
    canvas = render_strip(pieces, StripCache(), T, 100.0, 30)

    assert canvas[0, 20].tolist() == list(GREEN)


def test_strip_pieces_skips_a_file_that_is_not_a_strip(tmp_path):
    segment, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000
    )
    _save_segment(tmp_path, segment, frames=50)
    (strip_folder(tmp_path, 1) / "camera_01_s1_review_9999.npz").write_bytes(b"junk")

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert [piece.segment_id for piece in pieces] == [segment.segment_id]


def test_strip_pieces_keeps_readable_strips_when_one_file_is_truncated(tmp_path):
    good, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000
    )
    bad, _bad_video = _segment(
        tmp_path, name="camera_01_s1_archive_0002.mkv", start_ms=T + 3_000, duration_ms=2_000
    )
    _save_segment(tmp_path, good, frames=50)
    bad_path = _save_segment(tmp_path, bad, frames=50)
    raw = bad_path.read_bytes()
    bad_path.write_bytes(raw[: len(raw) // 2])

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert [piece.segment_id for piece in pieces] == [good.segment_id]


def test_strip_pieces_reads_one_camera_at_a_time(tmp_path):
    one, _one_video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000
    )
    two, _two_video = _segment(
        tmp_path, name="camera_02_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000,
        camera=2,
    )
    _save_segment(tmp_path, one, frames=50)
    _save_segment(tmp_path, two, frames=50)

    pieces_one, _metas_one = strip_pieces(tmp_path, 1)
    pieces_two, _metas_two = strip_pieces(tmp_path, 2)

    assert [(piece.camera, piece.segment_id) for piece in pieces_one] == [
        (1, one.segment_id)
    ]
    assert [(piece.camera, piece.segment_id) for piece in pieces_two] == [
        (2, two.segment_id)
    ]
    assert frame_at(pieces_one, T + 100)["camera_index"] == 1
    assert Path(frame_at(pieces_two, T + 100)["video_path"]).name.startswith("camera_02")


def test_a_strip_for_another_camera_is_not_drawn_on_this_camera(tmp_path):
    other, _video = _segment(
        tmp_path, name="camera_02_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000,
        camera=2,
    )
    _save_segment(
        tmp_path, other,
        path=strip_folder(tmp_path, 1) / "camera_02_s1_archive_0001.npz",
        frames=50,
    )

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert pieces == []


def test_a_recording_with_another_clock_source_is_not_drawn_on_the_chip_clock(tmp_path):
    segment, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000,
        clock_source="external_clip_sidecar_beijing",
    )
    _save_segment(tmp_path, segment, frames=50)

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert pieces == []


def test_a_recording_from_another_visit_is_not_drawn_on_this_race(tmp_path):
    current, _video = _segment(
        tmp_path, name="camera_01_s1_archive_0001.mkv", start_ms=T, duration_ms=2_000
    )
    other_visit, _other_video = _segment(
        tmp_path, name="camera_01_s1_archive_0002.mkv",
        start_ms=T - 10 * 3_600_000, duration_ms=2_000,
    )
    _save_segment(tmp_path, current, frames=50)
    _save_segment(tmp_path, other_visit, frames=50)
    _chip_read(tmp_path, T + 1_000)

    pieces, _metas = strip_pieces(tmp_path, 1)

    assert [piece.segment_id for piece in pieces] == [current.segment_id]


def test_unaligned_archive_cannot_cover_an_aligned_clip(tmp_path):
    from dataclasses import replace

    path = _save(tmp_path / "unaligned.npz", segment_id="raw", start_ms=T,
                 duration_ms=2_000, color=RED, frames=50)
    clip_path = _save(tmp_path / "aligned.npz", segment_id="aligned", start_ms=T,
                      duration_ms=2_000, color=GREEN, frames=50)
    raw = StripPiece(path, 1, "raw", tmp_path / "raw.mkv", T, T + 2000, True, 0, aligned=False)
    aligned = replace(raw, path=clip_path, segment_id="aligned", archive=False, aligned=True)
    assert np.all(render_strip([raw], StripCache(), T, 40, 50) == 205)
    canvas = render_strip([raw, aligned], StripCache(), T, 40, 50)
    assert canvas[0, 10].tolist() == list(GREEN)
    assert frame_at([raw], T + 100) is None


def test_cache_budget_counts_timestamps_and_does_not_keep_an_oversized_item(tmp_path):
    path = _save(tmp_path / "cache.npz", segment_id="s", start_ms=T,
                 duration_ms=2_000, color=RED, frames=50)
    piece = StripPiece(path, 1, "s", tmp_path / "s.mkv", T, T + 2000, True, 0)
    cache = StripCache(budget=100)
    times, image = cache.get(piece)
    assert times.size == 50 and image.shape[1] == 50
    assert cache._bytes <= cache.budget and not cache._items
