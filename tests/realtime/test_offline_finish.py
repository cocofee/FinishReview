from dataclasses import asdict
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from realtime.finish_line import FinishLine
from realtime.offline_finish import analyze_request, detect_strip, sample_source
from tests.realtime.test_finish_problems import inputs


def make_archive(path):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (100, 100))
    assert writer.isOpened()
    starts = [100, 290, 580, 890, 1370, 1650, 1740]
    for index in range(1800):
        frame = np.full((100, 100, 3), 30, np.uint8)
        if any(start <= index < start + 3 for start in starts):
            frame[40:60, 40:65] = 220
        writer.write(frame)
    writer.release()


def test_real_decode_sampling_clock_source_cards_and_json(tmp_path):
    path = tmp_path / "archive.avi"
    make_archive(path)
    request, _ = inputs()
    request["sources"] = [dict(segment_id="video", video_path=str(path), origin_ms=200,
                                camera_index=1, race_id="race")]
    request["finish_line"] = asdict(FinishLine(1, .2, .5, .8, .5))
    report = analyze_request(request, tmp_path / "run")
    assert len(report["automatic_passes"]) == 6
    assert [p["kind"] for p in report["problems"]] == ["no_chip"]
    row = report["problems"][0]
    assert row["locator"]["frame_index"] == 1740
    assert row["locator"]["position_ms"] == 174_000
    assert row["locator"]["recorder_ms"] == 174_200
    card = cv2.imdecode(np.frombuffer(Path(row["card_path"]).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert card is not None and card[50, 50].mean() > 200
    saved = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert saved["problems"][0]["fingerprint"] == row["fingerprint"]
    assert len(list((tmp_path / "run").glob("*.npz"))) >= 3


def test_nonmonotonic_timestamps_are_rejected():
    with pytest.raises(ValueError):
        detect_strip(np.zeros((500, 3, 3), np.uint8), np.array([0, 100, 100]))


def test_source_modified_during_decode_is_rejected(tmp_path):
    path = tmp_path / "archive.avi"
    make_archive(path)
    source = dict(segment_id="video", video_path=str(path), origin_ms=0, camera_index=1)
    def mutate(_message):
        import os
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError, match="变化"):
        sample_source(source, FinishLine(1, .2, .5, .8, .5), tmp_path, mutate)


def test_crossings_at_chunk_boundaries_are_emitted_once(tmp_path):
    path = tmp_path / "boundaries.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (100, 100))
    assert writer.isOpened()
    for index in range(1300):
        frame = np.full((100, 100, 3), 30, np.uint8)
        if 599 <= index < 602 or 1200 <= index < 1203:
            frame[40:60, 40:65] = 220
        writer.write(frame)
    writer.release()
    source = dict(segment_id="video", video_path=str(path), origin_ms=0, camera_index=1)
    events, _, _, _ = sample_source(source, FinishLine(1, .2, .5, .8, .5), tmp_path)
    assert [e["locator"]["frame_index"] for e in events] == [599, 1200]
