import pytest

from realtime.finish_line import FinishLine, FinishLineStore


def test_finish_line_produces_bounded_roi():
    line = FinishLine(1, 0.48, 0.1, 0.52, 0.9, band_width=0.1)
    assert line.roi == pytest.approx((0.43, 0.05, 0.57, 0.95))


def test_finish_line_store_round_trips(tmp_path):
    path = tmp_path / "finish_lines.json"
    store = FinishLineStore(path)
    line = FinishLine(2, 0.2, 0.1, 0.3, 0.9)
    store.set(line)
    assert FinishLineStore(path).get(2) == line


def test_finish_line_store_round_trips_exact_roi(tmp_path):
    path = tmp_path / "finish_lines.json"
    store = FinishLineStore(path)
    roi = (0.2, 0.1, 0.4, 0.9)
    store.set_roi(2, roi)

    assert FinishLineStore(path).get_roi(2) == pytest.approx(roi)


def test_finish_line_replaces_previous_fallback_roi(tmp_path):
    path = tmp_path / "finish_lines.json"
    store = FinishLineStore(path)
    store.set_roi(2, (0.1, 0.1, 0.3, 0.9))
    line = FinishLine(2, 0.5, 0.1, 0.5, 0.9, band_width=0.1)
    store.set(line)

    assert FinishLineStore(path).get_roi(2) == pytest.approx(line.roi)


def test_invalid_zero_length_line_is_rejected():
    try:
        FinishLine(1, 0.2, 0.2, 0.2, 0.2)
    except ValueError as error:
        assert "长度" in str(error)
    else:
        raise AssertionError("expected invalid line to fail")


def test_finish_line_detects_crossing_near_segment():
    line = FinishLine(1, 0.5, 0.1, 0.5, 0.9)

    assert line.contains_crossing((0.45, 0.5), (0.55, 0.5))
    assert not line.contains_crossing((0.45, 0.0), (0.55, 0.0))
