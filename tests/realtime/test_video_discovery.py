from realtime.video_discovery import VideoDiscoveryStore


def test_video_discovery_store_recovers_and_updates_records(tmp_path):
    path = tmp_path / "video_discoveries.jsonl"
    store = VideoDiscoveryStore(path)
    record = store.add(
        discovery_id="discovery-1",
        race_id="race-1",
        stage_id="stage-1",
        batch_id="batch:passage-1",
        bib="319",
        camera_index=1,
        frame_index=406,
        position_ms=16_250,
        started_at_ms=15_000,
        ended_at_ms=18_000,
        segment_id="segment-1",
        video_path=str(tmp_path / "recording.mkv"),
        media_locator="original-stream",
        clock_offset_ms=-250,
    )
    assert record.status == "pending_manual_entry"
    store.update_batch("discovery-1", "batch:passage-2")
    store.update_status("discovery-1", "resolved")

    reopened = VideoDiscoveryStore(path)
    recovered = reopened.records()
    assert len(recovered) == 1
    assert recovered[0].bib == "319"
    assert recovered[0].batch_id == "batch:passage-2"
    assert recovered[0].status == "resolved"
    assert recovered[0].segment_id == "segment-1"
    assert recovered[0].video_path == str(tmp_path / "recording.mkv")
    assert recovered[0].media_locator == "original-stream"
    assert recovered[0].clock_offset_ms == -250


def test_legacy_video_discovery_without_media_reference_remains_readable(tmp_path):
    import json
    from realtime.video_discovery import VideoDiscoveryRecord

    path = tmp_path / "video_discoveries.jsonl"
    payload = VideoDiscoveryRecord("legacy", "race", "stage", "batch", "319", 1, 10, 400, 1000, 2000).to_payload()
    for name in ("segment_id", "video_path", "media_locator", "clock_offset_ms"):
        payload.pop(name)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    record, = VideoDiscoveryStore(path).records()
    assert record.bib == "319"
    assert record.segment_id == ""
    assert record.video_path == ""
