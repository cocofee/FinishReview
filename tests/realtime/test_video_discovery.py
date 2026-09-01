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
