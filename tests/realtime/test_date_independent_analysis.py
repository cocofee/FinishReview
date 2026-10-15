from dataclasses import replace

import pytest

from realtime.chip_history import chip_reads
from realtime.clock_sync import ClockCrossing, ClockModel
from realtime.passage_receiver import PassageEvent
from realtime.problem_analysis import calibrate_evidence_clock


DAY = 1_789_747_200_000


def observations(days):
    events = []
    crossings = []
    shift = days * 86_400_000
    for index, seconds in enumerate([0, 13, 39, 72, 120, 171, 219, 287, 343, 410]):
        formal = DAY + 36_000_000 + seconds * 1000
        events.append(PassageEvent(
            event_id=str(index), race_id="race", stage_id="stage", group_id="1",
            sequence=index + 1, bib=str(index), chip_id=str(index), lap=0,
            passage_time_ms=36_000_000 + seconds * 1000, passage_timestamp_ms=formal,
            received_at_ms=formal + shift + 50, group_name="riders",
        ))
        crossings.append(ClockCrossing(formal + shift + 1200, "session"))
    return chip_reads(events), crossings


@pytest.mark.parametrize("days", [-365, -3, 0, 3, 365])
def test_visual_calibration_maps_any_receipt_date_without_changing_official_times(days):
    reads, crossings = observations(days)
    result = calibrate_evidence_clock(crossings, reads, camera_index=1)
    assert result.model is not None
    assert result.model.support == len(reads)
    restored = ClockModel.from_payload(result.model.to_payload())
    for read, crossing in zip(reads, crossings):
        assert abs(restored.to_recorder(read.chip_ms) - crossing.recorder_ms) <= 1
        assert abs(restored.to_chip(crossing.recorder_ms) - read.chip_ms) <= 1
    assert reads[0].chip_ms == DAY + 36_000_000


def test_receipt_date_alone_is_not_sufficient_for_calibration():
    reads, _ = observations(-3)
    result = calibrate_evidence_clock([], reads, camera_index=1)
    assert result.model is None


def test_original_recording_remains_usable_after_next_day_resync():
    reads, crossings = observations(0)
    reads = tuple(replace(read, received_at_ms=read.received_at_ms + 86_400_000) for read in reads)
    result = calibrate_evidence_clock(crossings, reads, camera_index=1)
    assert result.model is not None
    assert result.model.to_recorder(reads[0].chip_ms) == crossings[0].recorder_ms


def test_ambiguous_dates_do_not_silently_choose_a_recording():
    reads, crossings = observations(-3)
    duplicate = [replace(item, recorder_ms=item.recorder_ms + 3 * 86_400_000,
                         session_id="other") for item in crossings]
    result = calibrate_evidence_clock(crossings + duplicate, reads, camera_index=1)
    assert result.model is None
    assert result.warnings
