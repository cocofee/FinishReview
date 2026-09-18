"""Slow durable writes must not hold up UI reads or expose uncommitted state."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from realtime.review_clip import PassageReviewBindingStore
from realtime.video_timeline import DEFAULT_CLOCK_SOURCE, VideoTimelineStore


@pytest.mark.parametrize("operation", ["start", "finish", "completed", "clip", "bind", "deactivate"])
def test_readers_keep_committed_state_during_slow_journal_write(tmp_path, monkeypatch, operation):
    if operation in {"start", "finish", "completed"}:
        store = VideoTimelineStore(tmp_path / "timeline.jsonl")
        kwargs = dict(source_id="camera", camera_index=1, video_path=tmp_path / "video.mkv")
        if operation == "start":
            write = lambda: store.start_segment(**kwargs, started_at_ms=1000)
        elif operation == "finish":
            segment = store.start_segment(**kwargs, started_at_ms=1000)
            write = lambda: store.finish_segment(segment.segment_id, ended_at_ms=2000)
        else:
            write = lambda: store.add_completed_segment(
                **kwargs, media_started_at_ms=1000, media_duration_ms=1000,
                clock_source=DEFAULT_CLOCK_SOURCE, timing_error_ms=0, end_reason="archive",
            )
        read = lambda: (store.revision, store.segments())
    else:
        store = PassageReviewBindingStore(tmp_path / "bindings.jsonl")
        add_clip = lambda: store.get_or_add_clip(
            race_id="race", camera_index=1, source_id="camera", started_at_ms=1000,
            ended_at_ms=2000, playlist_path=tmp_path / "clip.m3u8",
            segment_signature="signature", timeline_segment_id="segment",
        )
        if operation == "clip":
            write = add_clip
        else:
            clip = add_clip()
            bind = lambda: store.bind(
                event_id="passage", revision=1, camera_index=1, clip_id=clip.clip_id,
                passage_timestamp_ms=1500, passage_offset_ms=500,
            )
            if operation == "bind":
                write = bind
            else:
                bind()
                write = lambda: store.deactivate("passage", 1)
        read = lambda: (store.revision, store.clips(), store.active_bindings("passage", 1))

    before = read()
    writing = threading.Event()
    release = threading.Event()
    append = store._append_records

    def slow_append(payloads):
        writing.set()
        assert release.wait(5)
        append(payloads)

    monkeypatch.setattr(store, "_append_records", slow_append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        try:
            assert writing.wait(2)
            assert pool.submit(read).result(timeout=1) == before
        finally:
            release.set()
        writer.result(timeout=2)
    assert read() != before
    reopened = type(store)(store.journal_path)
    assert reopened.revision == store.revision
