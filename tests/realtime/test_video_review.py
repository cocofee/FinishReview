import pytest

from realtime.video_review import VideoReviewJournal, VideoReviewRecord


def test_video_review_journal_round_trips_latest_decision(tmp_path):
    path = tmp_path / "video_review.jsonl"
    journal = VideoReviewJournal(path)
    journal.update("segment-1:video-1", status="verified", bib="321")
    journal.update("segment-1:video-1", bib="322")

    restored = VideoReviewJournal(path)
    record = restored.get("segment-1:video-1")

    assert record == VideoReviewRecord(
        "segment-1:video-1",
        status="verified",
        bib="322",
        updated_at_ms=record.updated_at_ms,
    )


def test_video_review_record_rejects_invalid_status():
    with pytest.raises(ValueError):
        VideoReviewRecord("candidate-1", status="done")
