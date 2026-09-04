import subprocess
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from realtime.review_clip import PassageReviewBindingStore
from realtime.review_recorder import (
    ArchiveTimelinePublisher,
    FfmpegReviewRecorder,
    PassageReviewCoordinator,
    PassageReviewState,
    PassageReviewTimelinePublisher,
    ReviewRingBuffer,
    discover_directshow_video_device_choices,
    discover_directshow_video_devices,
    is_supported_review_source,
    load_archive_recording_sessions,
    make_directshow_source,
    parse_directshow_source,
)
from realtime.stream_recorder import RecordingError
from realtime.video_timeline import VideoTimelineStore


class _InspectableBytesIO(BytesIO):
    def close(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = _InspectableBytesIO()
        self.stderr = BytesIO()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1


class _ProcessFactory:
    def __init__(self):
        self.calls = []
        self.process = _FakeProcess()

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.process


def _write_playlist(root: Path, entries):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for name, started_at, duration in entries:
        (root / name).write_bytes(b"video")
        lines.extend(
            [
                f"#EXT-X-PROGRAM-DATE-TIME:{started_at}",
                f"#EXTINF:{duration:.3f},",
                name,
            ]
        )
    playlist = root / "camera_01.m3u8"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist


def test_directshow_device_discovery_returns_video_inputs_only(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"exe")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout="",
            stderr=(
                '[dshow @ 0001] "DJI Osmo Action 5 Pro" (video)\n'
                '[dshow @ 0001] "Microphone" (audio)\n'
                '[dshow @ 0001] "DJI Osmo Action 5 Pro" (video)\n'
                '[dshow @ 0001] "ToDesk Camera" (video)\n'
            ),
        )

    assert discover_directshow_video_devices(
        ffmpeg,
        run_factory=fake_run,
    ) == ("DJI Osmo Action 5 Pro", "ToDesk Camera")
    assert calls[0][0][1:] == [
        "-hide_banner",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]


def test_directshow_device_choices_keep_identical_cameras_separate(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"exe")

    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            stdout="",
            stderr=(
                '[dshow @ 0001] "DJI Osmo Action 5 Pro" (video)\n'
                '[dshow @ 0001]   Alternative name "@device_pnp_dji_one"\n'
                '[dshow @ 0001] "DJI Osmo Action 5 Pro" (video)\n'
                '[dshow @ 0001]   Alternative name "@device_pnp_dji_two"\n'
            ),
        )

    choices = discover_directshow_video_device_choices(
        ffmpeg,
        run_factory=fake_run,
    )

    assert [choice.display_name for choice in choices] == [
        "DJI Osmo Action 5 Pro（1）",
        "DJI Osmo Action 5 Pro（2）",
    ]
    assert [choice.input_name for choice in choices] == [
        "@device_pnp_dji_one",
        "@device_pnp_dji_two",
    ]
    assert [choice.friendly_name for choice in choices] == [
        "DJI Osmo Action 5 Pro",
        "DJI Osmo Action 5 Pro",
    ]


def test_review_recorder_uses_one_input_for_archive_and_short_hls(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    recorder = FfmpegReviewRecorder(
        "rtsp://camera/live",
        tmp_path / "race",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
        clock=lambda: datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )

    playlist = recorder.start()

    command, kwargs = factory.calls[0]
    assert command.count("-i") == 1
    assert command[command.index("-i") + 1] == "rtsp://camera/live"
    assert command.count("copy") == 2
    assert command[command.index("-segment_time") + 1] == "300"
    assert command[command.index("-hls_time") + 1] == "2"
    assert command[command.index("-hls_list_size") + 1] == "182"
    assert "temp_file" in command[command.index("-hls_flags") + 1]
    assert "program_date_time" in command[command.index("-hls_flags") + 1]
    assert playlist.parent.name == "camera_01"
    assert recorder.archive_pattern.parent.name == "videos"
    assert recorder.session_started_at_ms == 1_787_313_600_000
    assert kwargs["stdout"] is not None

    recorder.stop()
    assert factory.process.stdin.getvalue() == b"q\n"


def test_sealed_archive_remains_locatable_after_review_segments_expire(
    tmp_path,
):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    recorder = FfmpegReviewRecorder(
        "rtsp://camera/live",
        tmp_path / "race",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
        clock=lambda: datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )
    playlist = recorder.start()
    assert recorder.archive_pattern is not None
    sessions = load_archive_recording_sessions(tmp_path / "race")
    assert len(sessions) == 1
    assert sessions[0].camera_index == 1
    assert sessions[0].session_started_at_ms == 1_787_313_600_000
    assert sessions[0].archive_pattern == recorder.archive_pattern.resolve()
    first_archive = Path(str(recorder.archive_pattern).replace("%04d", "0000"))
    active_archive = Path(str(recorder.archive_pattern).replace("%04d", "0001"))
    first_archive.write_bytes(b"sealed archive")
    active_archive.write_bytes(b"active archive")
    duration_probe = (
        lambda path: 300_000 if Path(path) == first_archive else 120_000
    )
    timeline = VideoTimelineStore(tmp_path / "race" / "video_timeline.jsonl")
    publisher = ArchiveTimelinePublisher(
        recorder,
        timeline,
        duration_probe=duration_probe,
    )

    published = publisher.publish_completed(race_id="race-1", recording=True)

    assert len(published) == 1
    assert timeline.resolve_video_path(published[0]) == first_archive.resolve()
    assert published[0].media_started_at_ms == 1_787_313_600_000
    assert published[0].media_duration_ms == 300_000

    recorder.stop()
    final_published = publisher.publish_completed(
        race_id="race-1",
        recording=False,
    )
    assert len(final_published) == 1
    assert timeline.resolve_video_path(final_published[0]) == active_archive.resolve()
    assert final_published[0].media_started_at_ms == 1_787_313_900_000
    assert final_published[0].media_duration_ms == 120_000

    recovered_timeline = VideoTimelineStore(
        tmp_path / "race" / "recovered_video_timeline.jsonl"
    )
    recovered_published = ArchiveTimelinePublisher(
        sessions[0],
        recovered_timeline,
        duration_probe=duration_probe,
    ).publish_completed(race_id="race-1", recording=False)
    assert len(recovered_published) == 2
    recovered_location = recovered_timeline.locate_passage(
        1_787_313_960_000,
        race_id="race-1",
    )
    assert recovered_location.status == "located"
    assert recovered_location.locations[0].video_path == active_archive.resolve()
    assert recovered_location.locations[0].passage_position_ms == 60_000

    review_segment = playlist.parent / "expired.ts"
    review_segment.write_bytes(b"short review")
    playlist.write_text(
        "\n".join(
            (
                "#EXTM3U",
                "#EXT-X-PROGRAM-DATE-TIME:2026-08-21T12:01:58.000+00:00",
                "#EXTINF:2.000,",
                review_segment.name,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=90)
    ring_buffer.scan()
    assert ring_buffer.cleanup(current_time_ms=1_787_313_840_000) == (
        review_segment,
    )

    located = timeline.locate_passage(
        1_787_313_720_000,
        race_id="race-1",
    )

    assert located.status == "located"
    assert located.locations[0].video_path == first_archive.resolve()
    assert located.locations[0].passage_position_ms == 120_000


def test_directshow_source_round_trips_installed_usb_camera_settings():
    source = make_directshow_source(
        "DJI Osmo Action 5 Pro",
        video_size="1920x1080",
        framerate=30,
    )

    parsed = parse_directshow_source(source)

    assert parsed is not None
    assert parsed.device_name == "DJI Osmo Action 5 Pro"
    assert parsed.video_size == "1920x1080"
    assert parsed.framerate == 30.0
    assert is_supported_review_source(source)
    assert parse_directshow_source("dshow://camera?video_size=invalid") is None


def test_usb_review_recorder_encodes_once_and_tees_archive_and_hls(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    output_dir = tmp_path / "race"
    recorder = FfmpegReviewRecorder(
        make_directshow_source(
            "DJI Osmo Action 5 Pro",
            video_size="1920x1080",
            framerate=30,
        ),
        output_dir,
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
        clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )

    playlist = recorder.start()

    command, kwargs = factory.calls[0]
    assert command[command.index("-i") + 1] == "video=DJI Osmo Action 5 Pro"
    assert command[command.index("-video_size") + 1] == "1920x1080"
    assert command[command.index("-framerate") + 1] == "30"
    assert command.count("libx264") == 1
    assert command[-3:-1] == ["-f", "tee"]
    assert "f=segment" in command[-1]
    assert "f=hls" in command[-1]
    assert command[-1].count("onfail=abort") == 2
    assert "program_date_time" in command[-1]
    assert kwargs["cwd"] == str(output_dir.resolve())
    assert playlist.parent.name == "camera_01"

    recorder.stop()


def test_review_recorder_stop_reports_ffmpeg_failure_details(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    factory.process.stderr = BytesIO(b"socket failure\n")
    recorder = FfmpegReviewRecorder(
        "rtsp://camera/live",
        tmp_path / "race",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
    )
    recorder.start()
    factory.process.returncode = 7

    with pytest.raises(RecordingError, match="代码 7: socket failure"):
        recorder.stop()


def test_review_recorder_keeps_process_handle_when_shutdown_is_unconfirmed(tmp_path):
    class _UnstoppableProcess(_FakeProcess):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def terminate(self):
            raise OSError("terminate unavailable")

    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"binary")
    factory = _ProcessFactory()
    factory.process = _UnstoppableProcess()
    recorder = FfmpegReviewRecorder(
        "rtsp://camera/live",
        tmp_path / "race",
        camera_index=1,
        ffmpeg_path=ffmpeg,
        popen_factory=factory,
    )
    recorder.start()

    with pytest.raises(OSError, match="terminate unavailable"):
        recorder.stop()

    assert recorder.is_running
    factory.process.returncode = 0
    recorder.stop()
    assert not recorder.is_running


def test_ring_buffer_indexes_only_completed_playlist_segments(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("first.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("second.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1)

    discovered = buffer.scan()

    assert [segment.segment_id for segment in discovered] == ["first.ts", "second.ts"]
    assert discovered[0].started_at_ms == 1_787_313_600_000
    assert discovered[0].duration_ms == 2_000
    assert buffer.scan() == ()


def test_passages_share_segments_and_cleanup_preserves_pins(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("old.ts", "2026-08-21T11:58:00.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("after.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=90)
    first = buffer.pin_window(
        "passage-15",
        started_at_ms=1_787_313_599_000,
        ended_at_ms=1_787_313_603_000,
    )
    second = buffer.pin_window(
        "passage-16",
        started_at_ms=1_787_313_600_500,
        ended_at_ms=1_787_313_602_500,
    )

    assert [segment.segment_id for segment in first] == ["finish.ts", "after.ts"]
    assert [segment.segment_id for segment in second] == ["finish.ts", "after.ts"]
    assert buffer.pinned_event_ids("finish.ts") == frozenset(
        {"passage-15", "passage-16"}
    )

    deleted = buffer.cleanup(current_time_ms=1_787_313_700_000)

    assert deleted == (tmp_path / "old.ts",)
    assert (tmp_path / "finish.ts").is_file()
    assert (tmp_path / "after.ts").is_file()


def test_pin_journal_restores_protected_segments_after_restart(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0)],
    )
    journal = tmp_path / "pins.jsonl"
    buffer = ReviewRingBuffer(
        playlist,
        camera_index=1,
        retention_seconds=30,
        pin_journal_path=journal,
    )
    buffer.pin_window(
        "passage-15",
        started_at_ms=1_787_313_600_000,
        ended_at_ms=1_787_313_602_000,
    )

    restored = ReviewRingBuffer(
        playlist,
        camera_index=1,
        retention_seconds=30,
        pin_journal_path=journal,
    )

    assert restored.pinned_event_ids("finish.ts") == frozenset({"passage-15"})
    assert restored.cleanup(current_time_ms=1_787_313_700_000) == ()
    assert (tmp_path / "finish.ts").is_file()


def test_releasing_last_passage_allows_expired_segment_cleanup(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0)],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=30)
    buffer.pin_window(
        "passage-15",
        started_at_ms=1_787_313_600_000,
        ended_at_ms=1_787_313_602_000,
    )

    buffer.release("passage-15")

    assert buffer.pinned_event_ids("finish.ts") == frozenset()
    assert buffer.cleanup(current_time_ms=1_787_313_700_000) == (
        tmp_path / "finish.ts",
    )


def test_scan_watermark_preserves_unprocessed_expired_segments(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("old.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("new.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1, retention_seconds=30)
    buffer.scan()
    buffer.set_scan_watermark(1_787_313_600_000)

    assert buffer.cleanup(current_time_ms=1_787_313_700_000) == ()
    assert (tmp_path / "old.ts").is_file()
    assert (tmp_path / "new.ts").is_file()

    buffer.set_scan_watermark(None)
    assert buffer.cleanup(current_time_ms=1_787_313_700_000) == (
        tmp_path / "old.ts",
        tmp_path / "new.ts",
    )


def test_passage_window_waits_for_tail_then_becomes_ready(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(buffer)

    waiting = coordinator.register(
        "passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )

    assert waiting.state is PassageReviewState.WAITING
    assert [segment.segment_id for segment in waiting.segments] == [
        "before.ts",
        "finish.ts",
    ]

    _write_playlist(
        tmp_path,
        [
            ("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("after.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )

    ready = coordinator.refresh_event("passage-15")

    assert ready.state is PassageReviewState.READY
    assert [segment.segment_id for segment in ready.segments] == [
        "before.ts",
        "finish.ts",
        "after.ts",
    ]
    assert buffer.pinned_event_ids("after.ts") == frozenset({"passage-15"})


def test_passage_window_tolerates_hls_timestamp_rounding_gaps(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("first.ts", "2026-08-24T22:50:24.151+08:00", 2.033),
            ("second.ts", "2026-08-24T22:50:26.185+08:00", 2.0),
            ("third.ts", "2026-08-24T22:50:28.185+08:00", 2.0),
            ("fourth.ts", "2026-08-24T22:50:30.185+08:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(buffer)

    window = coordinator.register(
        "passage-19",
        passage_timestamp_ms=1_787_583_027_305,
    )

    assert window.state is PassageReviewState.READY
    assert [segment.segment_id for segment in window.segments] == [
        "first.ts",
        "second.ts",
        "third.ts",
        "fourth.ts",
    ]


def test_passage_window_accepts_missing_pre_roll_at_recorder_start(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("first.ts", "2026-08-24T22:56:31.409+08:00", 2.033),
            ("second.ts", "2026-08-24T22:56:33.442+08:00", 2.0),
            ("third.ts", "2026-08-24T22:56:35.442+08:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(buffer)

    window = coordinator.register(
        "passage-20",
        passage_timestamp_ms=1_787_583_393_919,
    )

    assert window.started_at_ms == 1_787_583_390_919
    assert window.segments[0].started_at_ms == 1_787_583_391_409
    assert window.state is PassageReviewState.READY


def test_waiting_window_exposes_read_only_preview_without_formal_timeline(
    tmp_path,
    monkeypatch,
):
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist = _write_playlist(
        buffer_dir,
        [
            ("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    window = PassageReviewCoordinator(ring_buffer).register(
        "race-1-stage-1-passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    publisher = PassageReviewTimelinePublisher(ring_buffer, timeline)

    preview = publisher.preview(window, race_id="race-1")

    assert window.state is PassageReviewState.WAITING
    assert preview is not None
    assert preview.end_reason == "passage_review_preview"
    assert preview.media_started_at_ms == 1_787_313_598_000
    assert preview.media_duration_ms == 4_000
    assert timeline.segments() == ()
    preview_playlist = Path(preview.video_path)
    assert preview_playlist.is_file()
    assert "finish.ts" in preview_playlist.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "realtime.review_recorder.os.replace",
        lambda *_args: pytest.fail("cached preview must not be rewritten"),
    )
    assert publisher.preview(window, race_id="race-1") == preview


def test_waiting_window_has_no_preview_before_passage_segment_is_sealed(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0)],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    window = PassageReviewCoordinator(ring_buffer).register(
        "race-1-stage-1-passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )
    publisher = PassageReviewTimelinePublisher(
        ring_buffer,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    assert publisher.preview(window, race_id="race-1") is None


def test_passage_window_reports_partial_after_timeline_crosses_a_gap(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("later.ts", "2026-08-21T12:00:06.000+00:00", 2.0),
        ],
    )
    coordinator = PassageReviewCoordinator(
        ReviewRingBuffer(playlist, camera_index=1)
    )

    window = coordinator.register(
        "passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )

    assert window.state is PassageReviewState.PARTIAL
    assert [segment.segment_id for segment in window.segments] == ["finish.ts"]


def test_corrected_passage_window_keeps_old_evidence_but_returns_current_window(
    tmp_path,
):
    playlist = _write_playlist(
        tmp_path,
        [
            ("first.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("second.ts", "2026-08-21T12:00:10.000+00:00", 2.0),
            ("tail.ts", "2026-08-21T12:00:12.000+00:00", 2.0),
        ],
    )
    buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(
        buffer,
        pre_roll_seconds=0,
        post_roll_seconds=1,
    )
    coordinator.register(
        "passage-15",
        passage_timestamp_ms=1_787_313_600_500,
    )

    corrected = coordinator.register(
        "passage-15",
        passage_timestamp_ms=1_787_313_610_500,
    )

    assert [segment.segment_id for segment in corrected.segments] == ["second.ts"]
    assert buffer.pinned_event_ids("first.ts") == frozenset({"passage-15"})


def test_ready_window_publishes_one_idempotent_playable_timeline(
    tmp_path,
    monkeypatch,
):
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist = _write_playlist(
        buffer_dir,
        [
            ("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("after.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    window = PassageReviewCoordinator(ring_buffer).register(
        "race-1-stage-1-passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    publisher = PassageReviewTimelinePublisher(ring_buffer, timeline)

    first = publisher.publish(window, race_id="race-1")
    monkeypatch.setattr(
        "realtime.review_recorder.os.replace",
        lambda *_args: pytest.fail("unchanged playlist must not be replaced"),
    )
    second = publisher.publish(window, race_id="race-1")

    assert first == second
    assert len(timeline.segments()) == 1
    assert first.race_id == "race-1"
    assert first.media_started_at_ms == 1_787_313_598_000
    assert first.media_duration_ms == 6_000
    evidence_playlist = timeline.resolve_video_path(first)
    assert evidence_playlist.is_file()
    content = evidence_playlist.read_text(encoding="utf-8")
    assert "#EXT-X-ENDLIST" in content
    assert "2026-08-21T20:00:00.000+08:00" in content
    assert "finish.ts" in content
    coordinator = PassageReviewCoordinator(ring_buffer)
    coordinator.register(
        window.event_id,
        passage_timestamp_ms=window.passage_timestamp_ms,
    )
    coordinator.discard(window.event_id)
    assert ring_buffer.cleanup(current_time_ms=1_787_313_700_000) == ()
    assert all(
        any(pin_id.startswith("published:") for pin_id in ring_buffer.pinned_event_ids(name))
        for name in ("before.ts", "finish.ts", "after.ts")
    )
    located = timeline.locate_passage(
        1_787_313_601_000,
        race_id="race-1",
    )
    assert located.status == "located"
    assert located.locations[0].passage_position_ms == 3_000


def test_identical_windows_share_one_playlist_and_timeline_segment(tmp_path):
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist = _write_playlist(
        buffer_dir,
        [
            ("before.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("after.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(ring_buffer)
    first_window = coordinator.register(
        "race-1-stage-1-passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )
    second_window = coordinator.register(
        "race-1-stage-1-passage-16",
        passage_timestamp_ms=1_787_313_601_000,
    )
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    publisher = PassageReviewTimelinePublisher(ring_buffer, timeline)

    first = publisher.publish(first_window, race_id="race-1")
    second = publisher.publish(second_window, race_id="race-1")

    assert second == first
    assert len(timeline.segments()) == 1
    evidence_playlists = tuple(buffer_dir.glob("evidence_shared_*.m3u8"))
    assert len(evidence_playlists) == 1
    assert timeline.resolve_video_path(first) == evidence_playlists[0].resolve()


def test_publish_many_merges_overlapping_windows_and_batches_bindings(tmp_path):
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist = _write_playlist(
        buffer_dir,
        [
            ("s0.ts", "2026-08-21T11:59:58.000+00:00", 2.0),
            ("s1.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("s2.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
            ("s3.ts", "2026-08-21T12:00:04.000+00:00", 2.0),
            ("s4.ts", "2026-08-21T12:00:06.000+00:00", 2.0),
            ("s5.ts", "2026-08-21T12:00:08.000+00:00", 2.0),
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(ring_buffer)
    windows = tuple(
        coordinator.register(
            f"passage-{index}",
            passage_timestamp_ms=timestamp_ms,
        )
        for index, timestamp_ms in enumerate(
            (
                1_787_313_601_000,
                1_787_313_603_000,
                1_787_313_605_000,
            ),
            start=1,
        )
    )
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    binding_store = PassageReviewBindingStore(tmp_path / "review_clips.jsonl")
    publisher = PassageReviewTimelinePublisher(
        ring_buffer,
        timeline,
        binding_store=binding_store,
    )

    segment = publisher.publish_many(
        tuple((window, 1) for window in windows),
        race_id="race-1",
    )

    assert len(timeline.segments()) == 1
    clip_ids = {
        binding_store.active_bindings(window.event_id, 1)[0].clip_id
        for window in windows
    }
    assert len(clip_ids) == 1
    offsets = [
        binding_store.active_bindings(window.event_id, 1)[0].passage_offset_ms
        for window in windows
    ]
    assert offsets == [3_000, 5_000, 7_000]
    evidence_playlist = timeline.resolve_video_path(segment)
    content = evidence_playlist.read_text(encoding="utf-8")
    assert "s0.ts" in content
    assert "s5.ts" in content


def test_fifty_passages_in_five_seconds_publish_one_shared_clip(
    tmp_path,
    monkeypatch,
):
    buffer_dir = tmp_path / "review_buffer" / "camera_01"
    buffer_dir.mkdir(parents=True)
    playlist = _write_playlist(
        buffer_dir,
        [
            (f"s{index}.ts", f"2026-08-21T11:59:{56 + index * 2:02d}.000+00:00", 2.0)
            for index in range(2)
        ]
        + [
            (f"s{index + 2}.ts", f"2026-08-21T12:00:{index * 2:02d}.000+00:00", 2.0)
            for index in range(5)
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(ring_buffer)
    base_timestamp_ms = 1_787_313_600_000
    windows = tuple(
        coordinator.register(
            f"passage-{index:02d}",
            passage_timestamp_ms=base_timestamp_ms + index * 100,
        )
        for index in range(50)
    )
    assert all(window.state is PassageReviewState.READY for window in windows)
    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    binding_store = PassageReviewBindingStore(tmp_path / "review_clips.jsonl")
    append_sizes = []
    original_append = binding_store._append_records

    def counted_append(payloads):
        append_sizes.append(len(payloads))
        original_append(payloads)

    monkeypatch.setattr(binding_store, "_append_records", counted_append)
    publisher = PassageReviewTimelinePublisher(
        ring_buffer,
        timeline,
        binding_store=binding_store,
    )

    publisher.publish_many(
        tuple((window, 1) for window in windows),
        race_id="race-1",
    )

    assert len(timeline.segments()) == 1
    assert append_sizes == [1, 50]
    clip_ids = {
        binding_store.active_bindings(window.event_id, 1)[0].clip_id
        for window in windows
    }
    assert len(clip_ids) == 1


def test_incomplete_window_is_not_published(tmp_path):
    playlist = _write_playlist(
        tmp_path,
        [("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0)],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    window = PassageReviewCoordinator(ring_buffer).register(
        "passage-15",
        passage_timestamp_ms=1_787_313_601_000,
    )
    publisher = PassageReviewTimelinePublisher(
        ring_buffer,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
    )

    with pytest.raises(ValueError, match="complete passage review window"):
        publisher.publish(window, race_id="race-1")


def test_refresh_scans_once_and_skips_completed_windows(tmp_path, monkeypatch):
    playlist = _write_playlist(
        tmp_path,
        [
            ("finish.ts", "2026-08-21T12:00:00.000+00:00", 2.0),
            ("after.ts", "2026-08-21T12:00:02.000+00:00", 2.0),
        ],
    )
    ring_buffer = ReviewRingBuffer(playlist, camera_index=1)
    coordinator = PassageReviewCoordinator(ring_buffer)
    coordinator.register(
        "ready",
        passage_timestamp_ms=1_787_313_600_500,
    )
    coordinator.register(
        "waiting-1",
        passage_timestamp_ms=1_787_313_603_000,
    )
    coordinator.register(
        "waiting-2",
        passage_timestamp_ms=1_787_313_603_500,
    )
    scan_calls = 0
    original_scan = ring_buffer.scan

    def counted_scan():
        nonlocal scan_calls
        scan_calls += 1
        return original_scan()

    monkeypatch.setattr(ring_buffer, "scan", counted_scan)

    coordinator.refresh()

    assert scan_calls == 1
