from datetime import datetime

import pytest

from realtime.evidence_clock import align_evidence_timestamp


def ms(value):
    return round(datetime.fromisoformat(value + "+08:00").timestamp() * 1000)


@pytest.mark.parametrize("formal,received,expected", [
    ("2026-10-18T15:32:12.130", "2026-10-15T15:32:12.208", "2026-10-15T15:32:12.130"),
    ("2026-10-18T23:59:59.900", "2026-10-16T00:00:00.100", "2026-10-15T23:59:59.900"),
    ("2026-10-18T00:00:00.100", "2026-10-15T23:59:59.900", "2026-10-16T00:00:00.100"),
    ("2026-10-18T15:32:12.130", "2026-10-19T18:00:00.000", "2026-10-18T15:32:12.130"),
])
def test_date_alignment_preserves_time_of_day_and_midnight(formal, received, expected):
    assert align_evidence_timestamp(ms(formal), ms(received)) == ms(expected)
