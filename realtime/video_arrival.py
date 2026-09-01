"""Persistent video-arrival candidates and operator review batches.

Video arrivals are advisory evidence. They are deliberately independent from
chip passages and never create or modify an official timing record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading
from typing import Iterable

from .video_passage_detector import VideoPassageCandidate


SCHEMA_VERSION = 1
DEFAULT_ARRIVAL_BATCH_GAP_MS = 8_000
DEFAULT_ARRIVAL_SUBWAVE_GAP_MS = 3_000


@dataclass(frozen=True, slots=True)
class VideoArrivalBatch:
    """One continuous operator-review interval on one camera."""

    batch_id: str
    camera_index: int
    started_at_ms: int
    ended_at_ms: int
    candidate_ids: tuple[str, ...]
    subwave_breaks: tuple[int, ...] = ()
    contains_group_detection: bool = False

    @property
    def size(self) -> int:
        return len(self.candidate_ids)

    @property
    def duration_ms(self) -> int:
        return max(0, self.ended_at_ms - self.started_at_ms)

    @property
    def is_large(self) -> bool:
        return self.size >= 10 or self.duration_ms >= 8_000


def build_video_arrival_batches(
    candidates: Iterable[VideoPassageCandidate],
    *,
    batch_gap_ms: int = DEFAULT_ARRIVAL_BATCH_GAP_MS,
    subwave_gap_ms: int = DEFAULT_ARRIVAL_SUBWAVE_GAP_MS,
) -> tuple[VideoArrivalBatch, ...]:
    """Group visual candidates by adjusted time without using chip records."""

    batch_gap_ms = max(0, int(batch_gap_ms))
    subwave_gap_ms = max(0, int(subwave_gap_ms))
    by_camera: dict[int, list[VideoPassageCandidate]] = {}
    for candidate in candidates:
        if candidate.is_camera_motion or not str(candidate.candidate_id).strip():
            continue
        by_camera.setdefault(max(1, int(candidate.camera_index)), []).append(candidate)

    batches: list[VideoArrivalBatch] = []
    for camera_index, camera_candidates in by_camera.items():
        ordered = sorted(
            camera_candidates,
            key=lambda item: (int(item.peak_at_ms), str(item.candidate_id)),
        )
        if not ordered:
            continue
        current = [ordered[0]]
        subwave_breaks: list[int] = []
        previous_peak = int(ordered[0].peak_at_ms)
        for candidate in ordered[1:]:
            peak = int(candidate.peak_at_ms)
            gap = peak - previous_peak
            if gap > batch_gap_ms:
                batches.append(
                    _make_video_arrival_batch(
                        camera_index,
                        current,
                        subwave_breaks,
                    )
                )
                current = [candidate]
                subwave_breaks = []
            else:
                if gap > subwave_gap_ms:
                    subwave_breaks.append(len(current))
                current.append(candidate)
            previous_peak = peak
        batches.append(
            _make_video_arrival_batch(
                camera_index,
                current,
                subwave_breaks,
            )
        )
    return tuple(
        sorted(
            batches,
            key=lambda item: (item.started_at_ms, item.camera_index, item.batch_id),
        )
    )


def _make_video_arrival_batch(
    camera_index: int,
    candidates: list[VideoPassageCandidate],
    subwave_breaks: list[int],
) -> VideoArrivalBatch:
    first = candidates[0]
    return VideoArrivalBatch(
        batch_id=f"arrival:{camera_index}:{first.candidate_id}",
        camera_index=max(1, int(camera_index)),
        started_at_ms=min(int(item.started_at_ms) for item in candidates),
        ended_at_ms=max(int(item.ended_at_ms) for item in candidates),
        candidate_ids=tuple(str(item.candidate_id) for item in candidates),
        subwave_breaks=tuple(int(value) for value in subwave_breaks),
        contains_group_detection=any(item.is_group for item in candidates),
    )


class VideoArrivalCandidateStore:
    """Append-only event-workspace index of scanned video candidates."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._candidates: dict[str, VideoPassageCandidate] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            content = self.path.read_bytes()
        except OSError:
            return
        offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith((b"\n", b"\r"))
            line = raw_line.rstrip(b"\r\n")
            if not line.strip():
                offset += len(raw_line)
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
                if payload.get("schema_version") != SCHEMA_VERSION:
                    offset += len(raw_line)
                    continue
                values = dict(payload["candidate"])
                candidate = VideoPassageCandidate(**values)
            except (
                UnicodeDecodeError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                if line_number == len(lines) and not terminated:
                    self._truncate(offset)
                    break
                offset += len(raw_line)
                continue
            self._candidates[candidate.candidate_id] = candidate
            offset += len(raw_line)

    def _truncate(self, size: int) -> None:
        try:
            with self.path.open("r+b") as journal:
                journal.truncate(int(size))
                journal.flush()
                os.fsync(journal.fileno())
        except OSError:
            return

    def candidates(self) -> tuple[VideoPassageCandidate, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._candidates.values(),
                    key=lambda item: (
                        int(item.camera_index),
                        int(item.peak_at_ms),
                        str(item.candidate_id),
                    ),
                )
            )

    def add_many(
        self,
        candidates: Iterable[VideoPassageCandidate],
    ) -> tuple[VideoPassageCandidate, ...]:
        added = []
        with self._lock:
            for candidate in candidates:
                candidate_id = str(candidate.candidate_id).strip()
                if not candidate_id or candidate_id in self._candidates:
                    continue
                self._append(candidate)
                self._candidates[candidate_id] = candidate
                added.append(candidate)
        return tuple(added)

    def _append(self, candidate: VideoPassageCandidate) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "video_arrival_candidate",
            "candidate": asdict(candidate),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        separator = b""
        if self.path.exists():
            try:
                with self.path.open("rb") as journal:
                    if journal.seek(0, os.SEEK_END) > 0:
                        journal.seek(-1, os.SEEK_END)
                        if journal.read(1) not in {b"\n", b"\r"}:
                            separator = b"\n"
            except OSError as error:
                raise RuntimeError(
                    f"failed to inspect video arrival candidate journal: {self.path}"
                ) from error
        try:
            with self.path.open("ab") as journal:
                journal.write(separator + encoded)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise RuntimeError(
                f"failed to append video arrival candidate: {self.path}"
            ) from error


__all__ = [
    "DEFAULT_ARRIVAL_BATCH_GAP_MS",
    "DEFAULT_ARRIVAL_SUBWAVE_GAP_MS",
    "VideoArrivalBatch",
    "VideoArrivalCandidateStore",
    "build_video_arrival_batches",
]
