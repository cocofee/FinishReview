"""Regression coverage for immutable live media and archive handover."""

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import pytest
from PyQt5.QtTest import QSignalSpy
from PyQt5.QtWidgets import QApplication

from realtime.passage_review import PassageReviewSurface
from realtime.race_filmstrip import recording_sources, RaceRecordingIndex, RaceThumbnailWorker
from realtime.review_recorder import ReviewRingBuffer, ArchiveTimelinePublisher, ArchiveRecordingSession
from realtime.review_window import FinishReviewWindow
from realtime.stream_recorder import find_ffmpeg_executable
from realtime.video_playback import VideoPlaybackWorker
from realtime.video_timeline import VideoTimelineStore

SESSION = "camera_01_20260917_091639_094654"
BASE = 1_789_600_000_000


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def write_playlist(root, entries, *, sequence=0, create_files=True):
    root.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-MEDIA-SEQUENCE:{sequence}"]
    for name, start, duration in entries:
        if create_files:
            (root / name).write_bytes(b"ts")
        date = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
        lines.extend([f"#EXTINF:{duration / 1000:.3f},", f"#EXT-X-PROGRAM-DATE-TIME:{date}", name])
    path = root / f"{SESSION}.m3u8"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def context(ring):
    pane = SimpleNamespace(camera_index=1)
    window = SimpleNamespace(
        _ring_buffers={1: ring}, _selected_event_id="", pre_roll_ms=3000,
        passage_store=SimpleNamespace(get=lambda _: None),
        _current_metadata=lambda: SimpleNamespace(race_id="race-1"),
    )
    window._live_locations_for_filmstrip = lambda p: FinishReviewWindow._live_locations_for_filmstrip(window, p)
    return window, pane


def test_missing_playlist_head_and_pinned_old_session_do_not_hide_tail(tmp_path):
    root = tmp_path / "review_buffer" / "camera_01"
    names = [f"{SESSION}_review_{i:016d}.ts" for i in range(4)]
    entries = [(name, BASE + i * 2500, 2500) for i, name in enumerate(names)]
    playlist = write_playlist(root, entries[:2])
    ring = ReviewRingBuffer(playlist, camera_index=1)
    ring.scan()
    ring.pin_window("old-pin", started_at_ms=BASE + 1, ended_at_ms=BASE + 1000, scan=False)
    write_playlist(root, entries[1:], sequence=1)
    (root / names[1]).unlink()  # The actual rolling manifest still names it.
    ring.scan()
    window, pane = context(ring)
    locations = window._live_locations_for_filmstrip(pane)
    assert [loc.video_path.name for loc in locations] == names[2:]
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    sources, _ = recording_sources(store, 1, "race-1", live_locations=locations)
    assert all(s.available for s in sources)
    assert [s.start_ms for s in sources] == [BASE + 5000, BASE + 7500]
    selected = FinishReviewWindow._live_location_for_filmstrip(window, pane, BASE + 8250)
    assert selected.passage_position_ms == 750
    assert FinishReviewWindow._live_location_for_filmstrip(window, pane, BASE + 3000) is None
    assert FinishReviewWindow._live_location_for_filmstrip(window, pane, BASE + 11000) is None


def test_live_sources_keep_real_gap_and_identity_after_rollover(tmp_path):
    root = tmp_path / "review_buffer" / "camera_01"
    entries = [(f"{SESSION}_review_{i:016d}.ts", BASE + offset, 2000)
               for i, offset in enumerate((0, 4000, 6000))]
    ring = ReviewRingBuffer(write_playlist(root, entries[:2]), camera_index=1)
    ring.scan()
    window, pane = context(ring)
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    sources, _ = recording_sources(store, 1, live_locations=window._live_locations_for_filmstrip(pane))
    assert RaceRecordingIndex(sources).span_at(BASE + 3000).source is None
    key = sources[1].key
    write_playlist(root, entries[1:], sequence=1)
    ring.scan()
    sources, _ = recording_sources(store, 1, live_locations=window._live_locations_for_filmstrip(pane))
    assert sources[0].key == key


def test_selected_ts_is_durable_across_cleanup_and_ring_restart(tmp_path):
    root = tmp_path / "review_buffer" / "camera_01"
    entries = [(f"{SESSION}_review_{i:016d}.ts", BASE + i * 2000, 2000) for i in range(3)]
    playlist = write_playlist(root, entries)
    ring = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=6)
    ring.scan()
    window, pane = context(ring)
    loc = window._live_locations_for_filmstrip(pane)[1]
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    saved = ring.retain_filmstrip_segment(loc.segment, store)
    assert ring.retain_filmstrip_segment(loc.segment, store) == saved
    assert len(store.segments()) == 1
    ring.cleanup(current_time_ms=BASE + 100000)
    assert loc.video_path.is_file()
    assert not (root / entries[0][0]).exists()
    assert not (root / entries[2][0]).exists()
    reopened = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=6)
    reopened.cleanup(current_time_ms=BASE + 200000)
    restored = VideoTimelineStore(store.journal_path).get_segment(saved.segment_id)
    assert restored.media_started_at_ms == BASE + 2000
    assert store.resolve_video_path(restored) == loc.video_path
    assert loc.video_path.is_file()


def test_cleanup_before_click_refuses_to_publish_wrong_media(tmp_path):
    root = tmp_path / "review_buffer" / "camera_01"
    ring = ReviewRingBuffer(write_playlist(root, [("old.ts", BASE, 2000)]), camera_index=1, retention_seconds=1)
    ring.scan()
    window, pane = context(ring)
    loc = window._live_locations_for_filmstrip(pane)[0]
    ring.cleanup(current_time_ms=BASE + 100000)
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    with pytest.raises(ValueError):
        ring.retain_filmstrip_segment(loc.segment, store)
    assert store.segments() == ()


@pytest.mark.parametrize(("direction", "frame_index", "gap_ms", "archive", "expected"), [
    (1, 49, 0, False, (2, 0, 0)),
    (-1, 0, 0, False, (0, 1960, 49)),
    (-1, 1, 0, False, None),  # Frame 1 must step to frame 0 before changing files.
    (1, 48, 0, False, None),
    (1, 49, 500, False, None),
    (-1, 0, 500, False, None),
    (-1, 0, 1, False, None),  # Even a gap shorter than one frame is not contiguous.
    (1, 49, 0, True, (2, 0, 0)),  # The newest archive must also reach live media.
])
def test_live_boundary_step_keeps_exact_neighbor_and_does_not_bridge_gaps(
    tmp_path, direction, frame_index, gap_ms, archive, expected,
):
    root = tmp_path / "review_buffer" / "camera_01"
    entries = [(f"{SESSION}_review_{i:016d}.ts", BASE + i * (2000 + gap_ms), 2000)
               for i in range(3)]
    ring = ReviewRingBuffer(write_playlist(root, entries), camera_index=1)
    ring.scan()
    window, pane = context(ring)
    locations = window._live_locations_for_filmstrip(pane)
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    sources, _ = recording_sources(store, 1, live_locations=locations)
    pane.location = locations[1]
    if archive:
        pane.location = replace(pane.location, video_path=tmp_path / "archive.mkv")
    pane._worker = object()
    pane._current_frame_index = frame_index
    pane._current_position_ms = frame_index * 40
    pane._frame_count = 50
    pane._duration_ms = 2000
    pane._fps = 25
    pane.frame_duration_ms = lambda: 40
    window._camera_one_pane = lambda: pane
    window.video_filmstrip = SimpleNamespace(full_race=SimpleNamespace(index=RaceRecordingIndex(sources)))
    opened = []

    def seek(location, position, frame):
        saved = ring.retain_filmstrip_segment(location.segment, store)
        opened.append((saved, position, frame))
        return True

    window._seek_retained_camera_frame = seek
    crossed = PassageReviewSurface._step_across_recording_boundary(window, pane, direction)
    assert crossed is (expected is not None)
    if expected is None:
        assert not opened
    else:
        neighbor, position, frame = expected
        saved, actual_position, actual_frame = opened[0]
        assert store.resolve_video_path(saved) == locations[neighbor].video_path
        assert (actual_position, actual_frame) == (position, frame)
        assert saved.media_started_at_ms + actual_position == (
            pane.location.segment.media_started_at_ms + pane._current_position_ms + direction * 40
        )


def test_hls_origin_and_session_calibration_survive_archive_handover(tmp_path):
    root = tmp_path / "review_buffer" / "camera_01"
    entries = [(f"{SESSION}_review_{i:016d}.ts", BASE + i * 2000, 2000) for i in range(2)]
    playlist = write_playlist(root, entries)
    ring = ReviewRingBuffer(playlist, camera_index=1)
    ring.scan()
    write_playlist(root, entries[1:], sequence=1)
    ring.scan()
    assert json.loads(playlist.with_suffix(".clock.json").read_text())["media_started_at_ms"] == BASE
    videos = tmp_path / "videos"
    videos.mkdir()
    archive = videos / f"{SESSION}_archive_0000.mkv"
    archive.write_bytes(b"archive")
    session = ArchiveRecordingSession(1, SESSION.removeprefix("camera_01_"), BASE - 3000,
                                      videos / f"{SESSION}_archive_%04d.mkv")
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    published = ArchiveTimelinePublisher(session, store, duration_probe=lambda _: 4000).publish_completed(race_id="race-1", recording=False)
    assert published[0].media_started_at_ms == BASE
    key = PassageReviewSurface._recording_session_key_from_path
    assert key("camera_01_review", root / entries[1][0]) == key("camera_01_review", archive)
    assert key("camera_01_review", playlist) == key("camera_01_review", archive)


def test_real_ts_thumbnail_and_full_resolution_match_after_playlist_rolls(qapp, tmp_path):
    ffmpeg = find_ffmpeg_executable()
    if ffmpeg is None:
        pytest.skip("FFmpeg is needed to generate real HLS")
    root = tmp_path / "review_buffer" / "camera_01"
    root.mkdir(parents=True)
    playlist = root / f"{SESSION}.m3u8"
    subprocess.run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "testsrc2=size=160x120:rate=25", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "50", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_flags", "program_date_time+independent_segments", "-hls_segment_filename",
        str(root / f"{SESSION}_review_%016d.ts"), str(playlist),
    ], check=True, capture_output=True, timeout=30)
    ring = ReviewRingBuffer(playlist, camera_index=1)
    ring.scan()
    window, pane = context(ring)
    loc = window._live_locations_for_filmstrip(pane)[1]
    store = VideoTimelineStore(tmp_path / "timeline.jsonl")
    sources, _ = recording_sources(store, 1, live_locations=(loc,))
    source = sources[0]
    thumbnail = RaceThumbnailWorker(((source, source.start_ms + 520),))
    ready, errors = QSignalSpy(thumbnail.frame_ready), QSignalSpy(thumbnail.failed)
    thumbnail.run()
    assert not errors and len(ready) == 1
    frame = ready[0][0]
    assert frame.frame_index == 13
    # Mutating the recorder's playlist cannot change this source or its frame.
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    ring.retain_filmstrip_segment(loc.segment, store)
    capture = cv2.VideoCapture(str(loc.video_path))
    player = VideoPlaybackWorker(loc.video_path, idle_prefetch=False)
    player._fps = 25
    full = QSignalSpy(player.full_resolution_ready)
    try:
        ok, _ = player._decode_full_resolution(capture, frame.frame_index, 0, generation=0)
        assert ok and len(full) == 1
        assert full[0][0] == frame.image
        assert full[0][1:] == [520, 13]
    finally:
        capture.release()
