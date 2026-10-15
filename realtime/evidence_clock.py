"""Date-independent evidence lookup without changing official chip times."""

from datetime import datetime, timedelta, timezone


BEIJING_TIMEZONE = timezone(timedelta(hours=8))
LIVE_DATE_TOLERANCE_MS = 5 * 60 * 1000


def align_evidence_timestamp(
    timestamp_ms: int, received_at_ms: int, *, tolerance_ms: int = LIVE_DATE_TOLERANCE_MS,
) -> int:
    """Use receipt date only when time-of-day supports a live observation.

    Neighbouring dates cover midnight. Receipt time is not substituted for
    chip time: delayed imports must not become apparent live crossings.
    """
    if received_at_ms <= 0:
        return int(timestamp_ms)
    formal = datetime.fromtimestamp(timestamp_ms / 1000, BEIJING_TIMEZONE)
    received = datetime.fromtimestamp(received_at_ms / 1000, BEIJING_TIMEZONE)
    if formal.date() == received.date():
        return int(timestamp_ms)
    candidates = (
        datetime.combine(received.date() + timedelta(days=offset), formal.timetz())
        for offset in (-1, 0, 1)
    )
    candidate = min(candidates, key=lambda value: abs(value.timestamp() * 1000 - received_at_ms))
    value = round(candidate.timestamp() * 1000)
    return value if abs(value - received_at_ms) <= max(0, tolerance_ms) else int(timestamp_ms)
