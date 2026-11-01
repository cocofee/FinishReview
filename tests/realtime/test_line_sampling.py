import numpy as np
import pytest

from realtime.line_sampling import (
    LineGeometry,
    LineStrip,
    line_config_digest,
    load_strip,
    save_strip,
    snap_to_paint,
)


def test_polyline_geometry_is_resampled_by_arc_length():
    config = {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5, "points": [[0.1, 0.5], [0.5, 0.6], [0.9, 0.5]]}
    geometry = LineGeometry.from_config(config, 1000, 1000, samples=101)

    assert geometry.samples == 101
    steps = np.hypot(np.diff(geometry.xs), np.diff(geometry.ys))
    assert steps.max() - steps.min() < 0.5
    assert geometry.point(50) == (500.0, 600.0)
    mapx, mapy = geometry.remap_maps()
    assert mapx.shape == (101, 4) and mapy.dtype == np.float32


def test_snap_moves_the_line_onto_painted_white_band():
    frames = []
    rng = np.random.default_rng(0)
    for _ in range(5):
        frame = rng.integers(60, 110, (400, 800, 3)).astype(np.uint8)
        frame[215:221, 100:700] = 250  # painted band 18 px below the drawn line
        frames.append(frame)
    geometry = LineGeometry.from_config({"x1": 0.15, "y1": 0.5, "x2": 0.85, "y2": 0.5}, 800, 400)

    snapped = snap_to_paint(frames, geometry, search_px=40)

    assert snapped is not None and snapped.source == "snapped"
    assert abs(float(np.median(snapped.ys)) - 217.5) < 1.5
    blank = [np.full((400, 800, 3), 90, np.uint8)]
    assert snap_to_paint(blank, geometry) is None


def test_strip_round_trip_keeps_frames_geometry_and_metadata(tmp_path):
    geometry = LineGeometry.from_config({"x1": 0.2, "y1": 0.1, "x2": 0.2, "y2": 0.9}, 640, 480, samples=50)
    strip = LineStrip(
        strip=np.arange(50 * 7 * 3, dtype=np.uint8).reshape(50, 7, 3),
        pts_ms=np.arange(7) * 50.0, geometry=geometry, video_path="v.mkv", fps=20.0,
    )
    path = tmp_path / "strips" / "camera_01" / "v.npz"

    save_strip(path, strip, {"segment_id": "s1", "media_started_at_ms": 1_000})
    loaded = load_strip(path)

    assert np.array_equal(loaded.strip, strip.strip)
    assert np.array_equal(loaded.pts_ms, strip.pts_ms)
    assert loaded.meta["segment_id"] == "s1" and loaded.meta["media_started_at_ms"] == 1_000
    assert np.allclose(loaded.geometry.xs, geometry.xs)
    assert line_config_digest({"x1": 1}) != line_config_digest({"x1": 1, "snap": True})


@pytest.fixture
def sealed_h264_video(tmp_path):
    import cv2
    import subprocess
    from realtime.stream_recorder import find_ffmpeg_executable

    executable = find_ffmpeg_executable()
    if executable is None:
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 25, (640, 360))
    for index in range(30):
        frame = np.full((360, 640, 3), (30, 80, 160), np.uint8)
        cv2.rectangle(frame, (index * 15, 100), (index * 15 + 50, 300), (200, 30, 60), -1)
        writer.write(frame)
    writer.release()
    target = tmp_path / "sealed video.mkv"
    result = subprocess.run([str(executable), "-hide_banner", "-loglevel", "error", "-i", str(source),
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return target


def test_roi_fast_path_preserves_frames_timestamps_and_original_coordinates(sealed_h264_video):
    from realtime.line_sampling import sample_video_file

    line = {"points": [[.01, .64], [.99, .67]]}
    reference = sample_video_file(sealed_h264_video, line, fast_decode=False)
    fast = sample_video_file(sealed_h264_video, line)
    assert fast.decode_backend == "ffmpeg_roi"
    assert fast.frame_count == reference.frame_count == 30
    np.testing.assert_allclose(fast.pts_ms, reference.pts_ms, atol=.02)
    np.testing.assert_array_equal(fast.geometry.xs, reference.geometry.xs)
    np.testing.assert_array_equal(fast.geometry.ys, reference.geometry.ys)
    delta = np.abs(fast.strip.astype(np.int16) - reference.strip.astype(np.int16))
    assert np.percentile(delta, 99) <= 6
    assert fast.stage_seconds["ffmpeg_total"] > 0


def test_failed_roi_decode_falls_back_without_losing_first_frame(sealed_h264_video, monkeypatch):
    from realtime.line_sampling import sample_video_file
    from realtime import line_sampling_ffmpeg

    def fail(*args, **kwargs):
        raise ValueError("test decoder failure")

    monkeypatch.setattr(line_sampling_ffmpeg, "sample_roi", fail)
    line = {"points": [[.1, .5], [.9, .5]]}
    reference = sample_video_file(sealed_h264_video, line, fast_decode=False)
    fallback = sample_video_file(sealed_h264_video, line)
    assert fallback.decode_backend == "opencv"
    np.testing.assert_array_equal(fallback.strip, reference.strip)
    np.testing.assert_allclose(fallback.pts_ms, reference.pts_ms)


def test_roi_rejects_wrong_frame_count(sealed_h264_video):
    from realtime.line_sampling_ffmpeg import sample_roi
    from realtime.stream_recorder import find_ffmpeg_executable

    geometry = LineGeometry.from_config({"points": [[.1, .5], [.9, .5]]}, 640, 360)
    with pytest.raises(ValueError, match="frame count"):
        sample_roi(sealed_h264_video, geometry, find_ffmpeg_executable(), threads=1,
                   checkpoint=None, cancelled=lambda: False, expected_frames=31)
