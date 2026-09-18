from dataclasses import replace
from datetime import datetime, timezone
import threading

from realtime.capture_refresh import CaptureRefreshRequest, CaptureRefreshWorker
from realtime.evidence_pipeline import EvidencePassage, EvidencePipeline, EvidenceRefreshJob
from realtime.review_clip import PassageReviewBindingStore
from realtime.review_recorder import ReviewRingBuffer, PassageReviewCoordinator, PassageReviewTimelinePublisher
from realtime.video_timeline import VideoTimelineStore


def _pipeline(tmp_path):
    start = 1_789_600_000_000
    lines = ["#EXTM3U"]
    for index in range(6):
        name = f"{index}.ts"
        (tmp_path / name).write_bytes(b"video")
        clock = datetime.fromtimestamp((start + index * 2000) / 1000, timezone.utc).isoformat()
        lines.extend([f"#EXT-X-PROGRAM-DATE-TIME:{clock}", "#EXTINF:2.000,", name])
    playlist = tmp_path / "camera.m3u8"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ring = ReviewRingBuffer(playlist, camera_index=1)
    timeline = VideoTimelineStore(tmp_path / "timeline.jsonl")
    bindings = PassageReviewBindingStore(tmp_path / "bindings.jsonl")
    publisher = PassageReviewTimelinePublisher(ring, timeline, binding_store=bindings)
    pipeline = EvidencePipeline({1: PassageReviewCoordinator(ring)}, {1: publisher}, bindings)
    passage = EvidencePassage("rider", 1, "race", start + 4000)
    return ring, timeline, bindings, publisher, pipeline, passage


def test_evidence_publication_and_pin_writes_run_off_caller_thread(tmp_path, monkeypatch):
    ring, timeline, bindings, publisher, pipeline, passage = _pipeline(tmp_path)
    caller = threading.get_ident()
    threads = []
    original = publisher.publish_many

    def publish(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(publisher, "publish_many", publish)
    completed = threading.Event()
    results = []
    worker = CaptureRefreshWorker(lambda result: (results.append(result), completed.set()))
    worker.start()
    try:
        worker.submit(CaptureRefreshRequest(1, (ring,), (), False, passage.timestamp_ms,
                                             evidence_job=EvidenceRefreshJob(pipeline, (passage,))))
        assert completed.wait(3)
        assert not results[0].error
        assert threads and all(thread != caller for thread in threads)
        assert len(timeline.segments()) == 1
        assert len(bindings.active_bindings(passage.event_id, 1)) == 1
    finally:
        assert worker.stop()


def test_failed_publication_retries_and_withdrawal_survives_restart(tmp_path, monkeypatch):
    ring, timeline, bindings, publisher, pipeline, passage = _pipeline(tmp_path)
    worker = CaptureRefreshWorker(lambda _result: None)
    original = publisher.publish_many
    monkeypatch.setattr(publisher, "publish_many", lambda *a, **k: (_ for _ in ()).throw(OSError("disk busy")))
    request = CaptureRefreshRequest(1, (ring,), (), True, passage.timestamp_ms + 1_000_000,
                                    evidence_job=EvidenceRefreshJob(pipeline, (passage,)))
    failed = worker._process(request)
    assert "disk busy" in failed.error
    assert (tmp_path / "0.ts").exists()
    monkeypatch.setattr(publisher, "publish_many", original)
    assert not worker._process(replace(request, cleanup=False)).error
    assert len(timeline.segments()) == 1
    assert not worker._process(replace(request, cleanup=False)).error
    assert len(timeline.segments()) == 1
    withdrawn = replace(passage, revision=2, active=False)
    pipeline.refresh((withdrawn,), lambda: False)
    reopened = PassageReviewBindingStore(bindings.journal_path)
    assert reopened.active_bindings(passage.event_id, 1) == ()
    # A later active revision can be prepared independently of the tombstone.
    pipeline.refresh((replace(passage, revision=3),), lambda: False)
    assert len(bindings.active_bindings(passage.event_id, 3)) == 1


def test_pending_snapshot_coalescing_preserves_withdrawal_before_cleanup(tmp_path):
    ring, _, bindings, _, pipeline, passage = _pipeline(tmp_path)
    worker = CaptureRefreshWorker(lambda _result: None)
    request = CaptureRefreshRequest(1, (ring,), (), False, passage.timestamp_ms,
                                    evidence_job=EvidenceRefreshJob(pipeline, (passage,)))
    worker._process(request)
    withdrawn = replace(passage, revision=2, active=False)
    worker.submit(replace(request, evidence_job=EvidenceRefreshJob(pipeline, (withdrawn,))))
    worker.submit(replace(request, evidence_job=None, scan=False, apply_state=False, cleanup=True))
    result = worker._process(worker._pending)
    assert result.evidence.passages == (withdrawn,)
    assert bindings.active_bindings(passage.event_id, 1) == ()


def test_leaving_stage_discards_pending_window_but_preserves_durable_binding(tmp_path):
    ring, _, bindings, _, pipeline, passage = _pipeline(tmp_path)
    ring.scan()
    pipeline.refresh((passage,), lambda: False)
    original = bindings.active_bindings(passage.event_id, 1)
    assert original
    outside_stage = replace(passage, eligible=False)
    snapshot = pipeline.refresh((outside_stage,), lambda: False)
    assert snapshot.windows == ((1, ()),)
    assert bindings.active_bindings(passage.event_id, 1) == original
    pipeline.refresh((passage,), lambda: False)
    assert bindings.active_bindings(passage.event_id, 1) == original
