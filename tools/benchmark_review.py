"""Repeatable synthetic performance baseline for the review workspace."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PyQt5.QtWidgets import QApplication

from realtime.auyat_rgb import AuyatRgbCapture, AuyatRgbCatalog
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.passage_review import PassageReviewDialog
from realtime.video_timeline import RecordingSegment, VideoTimelineStore


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _measure(callback: Callable[[], None], iterations: int) -> dict[str, float]:
    samples = []
    for _ in range(max(1, int(iterations))):
        started = time.perf_counter()
        callback()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def _events(count: int, started_at_ms: int) -> list[PassageEvent]:
    return [
        PassageEvent(
            event_id=f"event-{index}",
            race_id="benchmark-race",
            stage_id="finish",
            group_id=f"group-{index % 4}",
            sequence=index + 1,
            bib=str(index + 1),
            passage_time_ms=started_at_ms + index * 100,
            emitted_at_ms=started_at_ms + index * 100,
            athlete_id=str(index + 1),
            athlete_name=f"Athlete {index + 1}",
        )
        for index in range(count)
    ]


def _populate_passage_store(store: PassageEventStore, events: list[PassageEvent]) -> None:
    store._events = {event.event_id: event for event in events}
    store._event_order = [event.event_id for event in events]
    store._race_ids = {event.race_id for event in events}


def _populate_timeline_store(
    store: VideoTimelineStore,
    segments: list[RecordingSegment],
) -> None:
    store._segments = {segment.segment_id: segment for segment in segments}
    store._segment_order = [segment.segment_id for segment in segments]


def _write_benchmark_video(video_path: Path) -> None:
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        10.0,
        (32, 32),
    )
    if not writer.isOpened():
        raise RuntimeError("could not create benchmark video")
    try:
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        for _ in range(3):
            writer.write(frame)
    finally:
        writer.release()


def _review_workspace_benchmark(
    event_count: int,
    ui_iterations: int,
) -> dict[str, object]:
    started_at_ms = 1_787_388_800_000
    with tempfile.TemporaryDirectory(prefix="finish-review-benchmark-") as temp_dir:
        root = Path(temp_dir)
        passage_store = PassageEventStore(root / "passages.jsonl")
        timeline_store = VideoTimelineStore(root / "timeline.jsonl")
        events = _events(event_count, started_at_ms)
        _populate_passage_store(passage_store, events)
        video_path = root / "camera-1.avi"
        _write_benchmark_video(video_path)
        _populate_timeline_store(
            timeline_store,
            [
                RecordingSegment(
                    segment_id="camera-1",
                    source_id="camera-1",
                    camera_index=1,
                    video_path=str(video_path),
                    started_at_ms=started_at_ms - 1_000,
                    ended_at_ms=started_at_ms + event_count * 100 + 1_000,
                    media_started_at_ms=started_at_ms - 1_000,
                    media_duration_ms=event_count * 100 + 2_000,
                    race_id="benchmark-race",
                )
            ],
        )

        created_at = time.perf_counter()
        dialog = PassageReviewDialog(passage_store, timeline_store)
        QApplication.processEvents()
        initial_render_ms = (time.perf_counter() - created_at) * 1000.0

        full_refresh = _measure(
            lambda: (dialog.refresh(), QApplication.processEvents()),
            ui_iterations,
        )
        changed = events[event_count // 2]
        incremental_refresh = _measure(
            lambda: (
                dialog.refresh_events((changed.event_id,)),
                QApplication.processEvents(),
            ),
            max(3, ui_iterations),
        )
        dialog.close()
        QApplication.processEvents()
        return {
            "events": event_count,
            "initial_render_ms": round(initial_render_ms, 3),
            "full_refresh": full_refresh,
            "single_event_refresh": incremental_refresh,
        }


def _timeline_lookup_benchmark(segment_count: int, iterations: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="finish-review-timeline-") as temp_dir:
        store = VideoTimelineStore(Path(temp_dir) / "timeline.jsonl")
        segments = [
            RecordingSegment(
                segment_id=str(index),
                source_id="camera-1",
                camera_index=1,
                video_path=f"{index}.mp4",
                started_at_ms=index * 1_000,
                ended_at_ms=index * 1_000 + 900,
                media_started_at_ms=index * 1_000,
                media_duration_ms=900,
                race_id="benchmark-race",
            )
            for index in range(segment_count)
        ]
        _populate_timeline_store(store, segments)
        target_ms = (segment_count - 1) * 1_000 + 450
        store.locate_passage(target_ms, race_id="benchmark-race")
        return {
            "segments": segment_count,
            "lookup": _measure(
                lambda: store.locate_passage(target_ms, race_id="benchmark-race"),
                iterations,
            ),
        }


def _high_speed_lookup_benchmark(
    capture_count: int,
    iterations: int,
) -> dict[str, object]:
    catalog = AuyatRgbCatalog(None)
    captures = tuple(
        AuyatRgbCapture(
            file_path=Path(f"capture-{index}.RGB"),
            capture_date=date(2026, 8, 24),
            start_record=index,
            end_record=index,
            start_tick=index * 2_000,
            end_tick=index * 2_000 + 1_000,
        )
        for index in range(capture_count)
    )
    catalog._captures = captures
    target_ms = captures[-1].media_started_at_ms
    catalog.locate(target_ms, race_id="benchmark-race")
    return {
        "captures": capture_count,
        "lookup": _measure(
            lambda: catalog.locate(target_ms, race_id="benchmark-race"),
            iterations,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[500, 2000, 5000])
    parser.add_argument("--ui-iterations", type=int, default=3)
    parser.add_argument("--lookup-iterations", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sizes = [size for size in args.sizes if size > 0]
    if not sizes:
        raise SystemExit("at least one positive size is required")
    app = QApplication.instance() or QApplication([])
    results = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        },
        "review_workspace": [
            _review_workspace_benchmark(size, args.ui_iterations) for size in sizes
        ],
        "timeline_lookup": [
            _timeline_lookup_benchmark(size, args.lookup_iterations) for size in sizes
        ],
        "high_speed_lookup": [
            _high_speed_lookup_benchmark(size, args.lookup_iterations) for size in sizes
        ],
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
