from realtime.video_arrival import (
    VideoArrivalCandidateStore,
    build_video_arrival_batches,
)
from realtime.video_passage_detector import VideoPassageCandidate


def _candidate(
    candidate_id: str,
    timestamp_ms: int,
    *,
    camera_index: int = 1,
    kind: str = "passage",
) -> VideoPassageCandidate:
    return VideoPassageCandidate(
        candidate_id,
        camera_index,
        timestamp_ms - 100,
        timestamp_ms + 100,
        timestamp_ms,
        0.2,
        0.1,
        kind=kind,
        segment_id=f"segment-{camera_index}",
        video_path=f"camera-{camera_index}.mkv",
        video_position_ms=timestamp_ms,
    )


def test_video_arrival_batches_use_video_time_not_chip_records():
    result = build_video_arrival_batches(
        (
            _candidate("one", 1_000),
            _candidate("two", 6_000),
            _candidate("three", 15_000),
        ),
        batch_gap_ms=8_000,
        subwave_gap_ms=3_000,
    )

    assert [batch.candidate_ids for batch in result] == [
        ("one", "two"),
        ("three",),
    ]
    assert result[0].subwave_breaks == (1,)


def test_video_arrival_batches_keep_cameras_separate():
    result = build_video_arrival_batches(
        (
            _candidate("camera-one", 1_000, camera_index=1),
            _candidate("camera-two", 1_000, camera_index=2),
        )
    )

    assert [(batch.camera_index, batch.candidate_ids) for batch in result] == [
        (1, ("camera-one",)),
        (2, ("camera-two",)),
    ]


def test_video_arrival_store_persists_and_deduplicates_candidates(tmp_path):
    path = tmp_path / "video_arrival_candidates.jsonl"
    store = VideoArrivalCandidateStore(path)
    first = _candidate("candidate-one", 1_000)
    second = _candidate("candidate-two", 2_000, kind="group")

    assert store.add_many((first, second, first)) == (first, second)

    restored = VideoArrivalCandidateStore(path)
    assert restored.candidates() == (first, second)
    assert restored.add_many((first,)) == ()


def test_video_arrival_store_recovers_incomplete_tail(tmp_path):
    path = tmp_path / "video_arrival_candidates.jsonl"
    first = _candidate("candidate-one", 1_000)
    store = VideoArrivalCandidateStore(path)
    store.add_many((first,))
    with path.open("ab") as journal:
        journal.write(b'{"schema_version":1,"candidate":')

    restored = VideoArrivalCandidateStore(path)

    assert restored.candidates() == (first,)
    assert path.read_bytes().endswith(b"\n")
    second = _candidate("candidate-two", 2_000)
    restored.add_many((second,))
    assert VideoArrivalCandidateStore(path).candidates() == (first, second)
