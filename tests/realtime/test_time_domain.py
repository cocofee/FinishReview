import pytest

from realtime.time_domain import (
    ClockOffsetMs,
    DurationMs,
    MediaPositionMs,
    MediaWindow,
    WallClockMs,
    WallClockWindow,
    clock_offset_ms,
    duration_ms,
    media_position_ms,
    wall_clock_ms,
)


def test_time_domains_remain_json_compatible_integers():
    assert int(wall_clock_ms(1_700_000_000_000)) == 1_700_000_000_000
    assert int(media_position_ms(1_250)) == 1_250
    assert int(duration_ms(500)) == 500
    assert int(clock_offset_ms(-75)) == -75

    assert WallClockMs.__supertype__ is int
    assert MediaPositionMs.__supertype__ is int
    assert DurationMs.__supertype__ is int
    assert ClockOffsetMs.__supertype__ is int


def test_non_negative_time_domains_reject_invalid_values():
    with pytest.raises(ValueError):
        wall_clock_ms(-1)
    with pytest.raises(ValueError):
        media_position_ms(-1)
    with pytest.raises(ValueError):
        duration_ms(-1)

    assert clock_offset_ms(-1) == -1


def test_media_and_wall_clock_windows_validate_their_own_domains():
    media_window = MediaWindow.from_milliseconds(1_000, 2_500)
    clock_window = WallClockWindow.from_milliseconds(
        1_700_000_000_000,
        1_700_000_003_000,
    )

    assert media_window.duration == 1_500
    assert clock_window.duration == 3_000
    with pytest.raises(ValueError):
        MediaWindow.from_milliseconds(20, 10)
    with pytest.raises(ValueError):
        WallClockWindow.from_milliseconds(20, 10)
