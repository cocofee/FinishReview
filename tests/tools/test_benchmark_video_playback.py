from pathlib import Path

import cv2
import numpy as np

from tools.benchmark_video_playback import (
    _reverse_sequence_summary,
    benchmark_video,
    main,
)


def _write_video(video_path: Path, frame_count: int = 30) -> None:
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        10.0,
        (64, 48),
    )
    assert writer.isOpened()
    try:
        for index in range(frame_count):
            frame = np.full((48, 64, 3), index, dtype=np.uint8)
            writer.write(frame)
    finally:
        writer.release()


def test_benchmark_video_measures_real_capture_paths(tmp_path):
    video_path = tmp_path / "sample.avi"
    _write_video(video_path)

    result = benchmark_video(
        video_path,
        sequential_frames=10,
        seek_count=5,
        reverse_frames=5,
    )

    assert result["metadata"]["frame_count"] == 30
    assert result["sequential"]["decoded_frames"] == 10
    assert result["random_seek"]["decoded_frames"] == 5
    assert result["reverse_random_seek"]["decoded_frames"] == 5
    assert result["reverse_window"]["decoded_frames"] == 5
    assert result["reverse_window"]["cache_bytes"] <= result["reverse_window"][
        "cache_limit_bytes"
    ]
    assert result["random_seek"]["latency"]["p95_ms"] >= 0
    assert result["reverse_window"]["first_frame_ms"] >= 0
    assert result["reverse_window"]["boundary_latency"]["p95_ms"] >= 0
    assert result["reverse_prefetch_playback"]["displayed_frames"] == 5
    assert result["reverse_prefetch_playback"]["skipped_frames"] == 0
    assert result["reverse_prefetch_playback"]["out_of_order_pairs"] == 0
    assert result["reverse_prefetch_playback"]["duplicate_frames"] == 0
    assert result["reverse_prefetch_playback"]["strictly_descending"]
    assert result["reverse_prefetch_playback"]["sequence_valid"]
    assert result["reverse_prefetch_playback"]["frame_gap_latency"]["p95_ms"] >= 0
    assert result["reverse_prefetch_playback"]["error"] == ""


def test_reverse_sequence_summary_rejects_duplicates_reordering_and_skips():
    repeated = _reverse_sequence_summary([100, 99, 100, 99], 4)
    skipped = _reverse_sequence_summary([100, 98, 97], 3)

    assert repeated["out_of_order_pairs"] == 1
    assert repeated["duplicate_frames"] == 2
    assert not repeated["strictly_descending"]
    assert not repeated["sequence_valid"]
    assert skipped["skipped_frames"] == 1
    assert not skipped["sequence_valid"]


def test_benchmark_cli_writes_json_report(tmp_path, capsys):
    video_path = tmp_path / "sample.avi"
    output_path = tmp_path / "benchmark.json"
    _write_video(video_path, frame_count=12)

    assert main(
        [
            str(video_path),
            "--sequential-frames",
            "4",
            "--seek-count",
            "3",
            "--reverse-frames",
            "3",
            "--output",
            str(output_path),
        ]
    ) == 0

    assert output_path.is_file()
    assert '"decoded_frames": 4' in output_path.read_text(encoding="utf-8")
    assert '"videos"' in capsys.readouterr().out
