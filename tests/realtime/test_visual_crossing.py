import numpy as np

from realtime.visual_crossing import CrossingConfig, DualGateCrossingDetector


def test_crossing_config_derives_red_and_yellow_lines_from_calibration():
    config = CrossingConfig(
        finish_line=0.45,
        gate_width=0.10,
        roi_top=0.15,
        roi_bottom=0.92,
        forward_direction="right_to_left",
    ).normalized()

    assert config.gate_a == 0.40
    assert config.gate_b == 0.50
    assert config.roi_top == 0.15
    assert config.roi_bottom == 0.92
    assert config.forward_direction == "right_to_left"


def test_dual_gate_detector_emits_only_configured_forward_direction():
    detector = DualGateCrossingDetector(
        1,
        config=CrossingConfig(
            process_width=240,
            roi_left=0.0,
            roi_top=0.0,
            roi_right=1.0,
            roi_bottom=1.0,
            finish_line=0.50,
            gate_width=0.20,
            forward_direction="left_to_right",
            min_area_ratio=0.0001,
            min_motion_px=0.5,
            max_area_ratio=0.8,
        ),
    )

    events = []
    for timestamp, x in enumerate((20, 45, 75, 110, 145), start=1):
        frame = np.zeros((80, 240, 3), dtype=np.uint8)
        frame[25:55, x : x + 18] = 255
        events.extend(detector.process(frame, timestamp * 100))

    assert events
    assert all(event.direction == "forward" for event in events)


def test_detector_allows_fast_motion_with_configured_track_distance():
    config = CrossingConfig(max_track_distance_px=220).normalized()
    assert config.max_track_distance_px == 220
    assert config.min_motion_px == 3.0
