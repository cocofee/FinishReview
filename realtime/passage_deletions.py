"""Decode historical deletion marks without rewriting race evidence."""

from typing import Any


def decode_deletions(payload: Any) -> tuple[dict, ...]:
    if isinstance(payload, list):
        if any(not isinstance(value, str) or not value.strip() for value in payload):
            raise ValueError("deleted event IDs must be non-empty strings")
        return tuple(dict(event_id=value, reason="", deleted_at_ms=0, legacy=True) for value in payload)
    if not isinstance(payload, dict):
        raise ValueError("unsupported passage deletion file")
    if "records" in payload and "deletions" in payload:
        raise ValueError("ambiguous passage deletion file")
    nested = "records" in payload
    records = payload.get("records" if nested else "deletions")
    if not isinstance(records, list):
        raise ValueError("unsupported passage deletion file")
    marks = []
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("event_id"), str)
                or not record["event_id"].strip()):
            raise ValueError("deletion marks need an event_id")
        mark = dict(record)
        if nested:
            basis = record.get("basis")
            if (not isinstance(basis, dict)
                    or not isinstance(basis.get("bib"), str)
                    or not isinstance(basis.get("chip_id"), str)
                    or type(basis.get("timeline_timestamp_ms")) is not int
                    or basis["timeline_timestamp_ms"] < 0):
                # An incomplete basis must never become an ID-only deletion.
                raise ValueError("deletion record needs bib, chip_id and timeline_timestamp_ms in basis")
            mark.update(bib=basis["bib"].strip(), chip_id=basis["chip_id"].strip(),
                        passage_timestamp_ms=basis["timeline_timestamp_ms"])
        marks.append(mark)
    return tuple(marks)
