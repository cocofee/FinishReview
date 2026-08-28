import json

from realtime.review_clip import PassageReviewBindingStore


def _clip(store, tmp_path):
    playlist = tmp_path / "review_buffer" / "camera_01" / "shared.m3u8"
    playlist.parent.mkdir(parents=True, exist_ok=True)
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    return store.get_or_add_clip(
        race_id="race-1",
        camera_index=1,
        source_id="camera_01_review",
        started_at_ms=10_000,
        ended_at_ms=18_000,
        playlist_path=playlist,
        segment_signature="signature-1",
        timeline_segment_id="timeline-1",
    )


def test_binding_store_preserves_revision_history_and_active_binding(tmp_path):
    journal = tmp_path / "review_clips.jsonl"
    store = PassageReviewBindingStore(journal)
    clip = _clip(store, tmp_path)

    store.bind(
        event_id="passage-1",
        revision=1,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=13_000,
        passage_offset_ms=3_000,
    )
    store.bind(
        event_id="passage-1",
        revision=2,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=14_000,
        passage_offset_ms=4_000,
    )

    assert store.active_bindings("passage-1", 1) == ()
    assert store.active_bindings("passage-1", 2)[0].passage_offset_ms == 4_000

    restored = PassageReviewBindingStore(journal)

    restored_clip = restored.get_clip(clip.clip_id)
    assert restored_clip is not None
    assert restored.resolve_playlist_path(restored_clip).name == "shared.m3u8"
    assert restored.active_bindings("passage-1", 1) == ()
    assert restored.active_bindings("passage-1", 2)[0].clip_id == clip.clip_id


def test_binding_store_deactivates_current_revision_and_recovers_partial_tail(
    tmp_path,
):
    journal = tmp_path / "review_clips.jsonl"
    store = PassageReviewBindingStore(journal)
    clip = _clip(store, tmp_path)
    store.bind(
        event_id="passage-1",
        revision=2,
        camera_index=1,
        clip_id=clip.clip_id,
        passage_timestamp_ms=14_000,
        passage_offset_ms=4_000,
    )
    store.deactivate("passage-1", 2)
    with journal.open("ab") as output:
        output.write(b'{"schema_version":1,"record_type":"binding_added"')

    restored = PassageReviewBindingStore(journal)

    assert restored.active_bindings("passage-1", 2) == ()
    for line in journal.read_text(encoding="utf-8").splitlines():
        json.loads(line)
