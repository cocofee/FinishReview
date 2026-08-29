"""Durable operator decisions for advisory video anomaly batches."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import time


VALID_VIDEO_REVIEW_STATUSES = frozenset({"pending", "verified", "ignored"})


@dataclass(frozen=True, slots=True)
class VideoReviewRecord:
    candidate_id: str
    status: str = "pending"
    bib: str = ""
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("candidate_id is required")
        if self.status not in VALID_VIDEO_REVIEW_STATUSES:
            raise ValueError("invalid video review status")


class VideoReviewJournal:
    """Append-only JSONL journal scoped to one event workspace."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, VideoReviewRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                record = VideoReviewRecord(
                    candidate_id=str(payload["candidate_id"]),
                    status=str(payload.get("status", "pending")),
                    bib=str(payload.get("bib", "")).strip(),
                    updated_at_ms=int(payload.get("updated_at_ms", 0)),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            self._records[record.candidate_id] = record

    def records(self) -> tuple[VideoReviewRecord, ...]:
        return tuple(
            self._records[key] for key in sorted(self._records)
        )

    def get(self, candidate_id: str) -> VideoReviewRecord | None:
        return self._records.get(str(candidate_id))

    def update(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        bib: str | None = None,
    ) -> VideoReviewRecord:
        candidate_id = str(candidate_id).strip()
        previous = self._records.get(candidate_id)
        record = VideoReviewRecord(
            candidate_id=candidate_id,
            status=status or (previous.status if previous else "pending"),
            bib=(bib if bib is not None else (previous.bib if previous else "")),
            updated_at_ms=int(time.time() * 1000.0),
        )
        self._append(record)
        self._records[candidate_id] = record
        return record

    def _append(self, record: VideoReviewRecord) -> None:
        payload = (json.dumps(asdict(record), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        try:
            with self.path.open("ab") as journal:
                journal.write(payload)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise RuntimeError(f"failed to append video review journal: {self.path}") from error


__all__ = [
    "VALID_VIDEO_REVIEW_STATUSES",
    "VideoReviewJournal",
    "VideoReviewRecord",
]
