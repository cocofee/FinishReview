import json
from urllib.request import Request, urlopen

from realtime.passage_receiver import PassageEventReceiver, PassageEventStore
from realtime.video_timeline import VideoTimelineStore


def test_http_passage_is_durably_mapped_to_video_preroll(tmp_path):
    passage_time_ms = 48_179_215
    passage_timestamp_ms = 1_786_252_979_215
    video_path = tmp_path / "videos" / "camera_01.mkv"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")

    timeline = VideoTimelineStore(tmp_path / "video_timeline.jsonl")
    segment = timeline.start_segment(
        source_id="camera_01",
        camera_index=1,
        video_path=video_path,
        started_at_ms=passage_timestamp_ms - 8_000,
        timing_error_ms=1_500,
    )
    timeline.finish_segment(
        segment.segment_id,
        ended_at_ms=passage_timestamp_ms + 8_000,
        media_duration_ms=16_000,
        media_started_at_ms=passage_timestamp_ms - 8_000,
    )

    lookups = []
    passage_store = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        passage_store,
        on_accepted=lambda event: lookups.append(
            timeline.locate_passage(
                event.timeline_timestamp_ms,
                clock_offset_ms=250,
                pre_roll_ms=3_000,
            )
        ),
    )
    receiver.start()
    try:
        payload = {
            "schema_version": 1,
            "message_type": "passage",
            "event_id": "race-1-stage-1-passage-7",
            "race_id": "race-1",
            "stage_id": "stage-1",
            "group_id": "men-open",
            "sequence": 7,
            "chip_id": "chip-23",
            "bib": "23",
            "passage_time_ms": passage_time_ms,
            "passage_timestamp_ms": passage_timestamp_ms,
            "lap": 1,
            "source": "cyclerace",
            "emitted_at_ms": passage_timestamp_ms + 100,
            "revision": 1,
        }
        request = Request(
            f"http://127.0.0.1:{receiver.listen_port}/api/v1/passage-events",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2.0) as response:
            ack = json.loads(response.read().decode("utf-8"))
            assert response.status == 201
            assert ack["status"] == "accepted"
    finally:
        receiver.stop()

    assert passage_store.get(payload["event_id"]) is not None
    assert lookups[0].status == "located"
    location = lookups[0].locations[0]
    assert location.video_path == video_path.absolute()
    assert location.passage_position_ms == 8_250
    assert location.playback_position_ms == 5_250
