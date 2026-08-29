from pathlib import Path

import cv2
import numpy as np

from realtime.visual_crossing import (
    CrossingConfig,
    DualGateCrossingDetector,
    VisualCrossingEvent,
    VisualCrossingEventStore,
)


def _frame(x: int) -> np.ndarray:
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    cv2.rectangle(image, (x, 70), (x + 28, 145), (255, 255, 255), -1)
    return image


def _frame_two(x: int) -> np.ndarray:
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    cv2.rectangle(image, (x, 20), (x + 28, 75), (255, 255, 255), -1)
    cv2.rectangle(image, (x, 100), (x + 28, 155), (255, 255, 255), -1)
    return image


def test_event_store_round_trip(tmp_path: Path):
    store = VisualCrossingEventStore(tmp_path / "visual.jsonl")
    event = VisualCrossingEvent(
        "abc", 1, 1234, "forward", 0.8, 160.0, 90.0, (140, 50, 30, 70)
    )
    store.append(event)
    assert store.events() == (event,)


def test_dual_gate_detector_emits_forward_candidate():
    detector = DualGateCrossingDetector(
        1,
        config=CrossingConfig(
            process_width=320,
            roi_top=0.0,
            roi_bottom=1.0,
            gate_a=0.45,
            gate_b=0.55,
            history=20,
            min_area_ratio=0.0005,
            cooldown_ms=0,
        ),
    )
    events = []
    for index, x in enumerate(range(10, 245, 8)):
        events.extend(detector.process(_frame(x), index * 125))
    assert events
    assert events[0].camera_index == 1
    assert events[0].direction == "forward"
    assert 0.0 < events[0].confidence <= 0.99


def test_detector_keeps_two_tracks_crossing_within_global_cooldown():
    detector = DualGateCrossingDetector(
        1,
        config=CrossingConfig(
            process_width=320,
            roi_top=0.0,
            roi_bottom=1.0,
            gate_a=0.45,
            gate_b=0.55,
            history=20,
            min_area_ratio=0.0005,
            cooldown_ms=350,
        ),
    )
    events = []
    for index, x in enumerate(range(10, 245, 8)):
        events.extend(detector.process(_frame_two(x), index * 125))
    assert len(events) >= 2


def test_detector_ignores_empty_frames():
    detector = DualGateCrossingDetector(1)
    assert detector.process(np.zeros((0, 0, 3), dtype=np.uint8)) == ()
