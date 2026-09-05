import json

from realtime.durable_jsonl import append_jsonl_records
from realtime.review_buffer_journal import ReviewBufferJournalProjection


def _append(path, *records):
    append_jsonl_records(
        path,
        (json.dumps(record).encode("utf-8") for record in records),
        description="test review buffer journal",
    )


def _segment(segment_id="segment-1", started=1_000, duration=2_000):
    return {
        "segment_id": segment_id,
        "source_id": "camera_01_review",
        "camera_index": 1,
        "video_path": f"{segment_id}.ts",
        "started_at_ms": started,
        "duration_ms": duration,
    }


def test_incremental_reducer_does_not_replay_released_owner(tmp_path):
    path = tmp_path / "pins.jsonl"
    projection = ReviewBufferJournalProjection()
    _append(path, {"op": "pin", "event_id": "legacy", "segments": [_segment()]})
    projection.sync(path)
    assert "legacy" in projection.owner_segments

    _append(path, {"op": "release", "event_id": "legacy"})
    projection.sync(path)
    assert "legacy" not in projection.owner_segments

    _append(
        path,
        {
            "schema_version": 2,
            "record_type": "pin_set",
            "op": "pin",
            "event_id": "owner:new",
            "owner_id": "owner:new",
            "owner_kind": "event_window",
            "logical_event_id": "event-2",
            "revision": 2,
            "segments": [_segment("segment-2")],
        },
    )
    projection.sync(path)
    assert "legacy" not in projection.owner_segments
    assert tuple(projection.owner_segments) == ("owner:new",)


def test_cleanup_commit_reduces_tombstone_interval_and_cursor(tmp_path):
    path = tmp_path / "pins.jsonl"
    projection = ReviewBufferJournalProjection()
    _append(
        path,
        {
            "schema_version": 2,
            "record_type": "pin_set",
            "owner_id": "owner:1",
            "segments": [_segment()],
        },
        {
            "schema_version": 2,
            "record_type": "cleanup_intent",
            "transaction_id": "tx-1",
            "segment": _segment(),
            "original_name": "segment-1.ts",
            "trash_name": ".segment-1.ts.tx-1.cleanup",
        },
        {
            "schema_version": 2,
            "record_type": "cleanup_committed",
            "transaction_id": "tx-1",
            "segment_id": "segment-1",
            "started_at_ms": 1_000,
            "ended_at_ms": 3_000,
        },
        {
            "schema_version": 2,
            "record_type": "live_scan_cursor",
            "scanner_id": "camera-1:live_batch_v2",
            "next_core_started_at_ms": 120_000,
        },
        {
            "schema_version": 2,
            "record_type": "live_scan_cursor",
            "scanner_id": "camera-1:live_batch_v2",
            "next_core_started_at_ms": 60_000,
        },
    )

    projection.sync(path)

    assert projection.cleanup_intents == {}
    assert projection.deleted_segment_ids == {"segment-1"}
    assert projection.deleted_intervals == [(1_000, 3_000)]
    assert projection.owner_segments == {}
    assert projection.scan_cursors["camera-1:live_batch_v2"] == 120_000


def test_projection_recovers_torn_tail_and_accepts_later_append(tmp_path):
    path = tmp_path / "pins.jsonl"
    _append(path, {"op": "pin", "event_id": "legacy", "segments": [_segment()]})
    with path.open("ab") as output:
        output.write(b'{"record_type":')
    projection = ReviewBufferJournalProjection()

    projection.sync(path)
    assert path.read_bytes().endswith(b"\n")

    _append(path, {"op": "release", "event_id": "legacy"})
    projection.sync(path)
    assert projection.owner_segments == {}
