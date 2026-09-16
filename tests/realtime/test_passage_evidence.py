import json
from dataclasses import replace

import pytest

from realtime.passage_receiver import PassageEvent

from realtime.passage_evidence import (
    ContinuousMarkerStore,
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
)


def test_continuous_marker_store_persists_unknown_markers(tmp_path):
    journal_path = tmp_path / "continuous_markers.jsonl"
    store = ContinuousMarkerStore(journal_path)

    marker = store.create(
        camera_index=1,
        segment_id="segment-camera-1",
        frame_index=42,
        position_ms=3_200,
        marker_x_normalized=0.4,
        marker_y_normalized=0.6,
        confirmed_at_ms=1_000,
    )

    reopened = ContinuousMarkerStore(journal_path)
    assert reopened.markers() == (marker,)
    assert reopened.clear(marker.marker_id, confirmed_at_ms=2_000)
    assert reopened.markers() == ()


def _confirm(store, *, source=REGULAR_SOURCE, x=0.25, confirmed_at_ms=1_000):
    return store.confirm(
        passage_event_id="passage-15",
        bib="15",
        confirmed_source=source,
        segment_id=f"segment-{source}",
        frame_index=125,
        position_ms=5_000,
        marker_x_normalized=x,
        marker_y_normalized=0.5,
        confirmed_at_ms=confirmed_at_ms,
    )


def test_association_store_persists_sources_and_revisions(tmp_path):
    journal_path = tmp_path / "passage_evidence_associations.jsonl"
    store = PassageEvidenceAssociationStore(journal_path)

    first = _confirm(store)
    second = _confirm(store, source=HIGH_SPEED_SOURCE, confirmed_at_ms=2_000)
    moved = _confirm(store, x=0.75, confirmed_at_ms=3_000)

    assert first.revision == 1
    assert second.revision == 1
    assert moved.revision == 2
    assert store.get("passage-15", REGULAR_SOURCE).marker_x_normalized == 0.75
    assert {item.confirmed_source for item in store.for_event("passage-15")} == {
        REGULAR_SOURCE,
        HIGH_SPEED_SOURCE,
    }

    reopened = PassageEvidenceAssociationStore(journal_path)
    assert reopened.get("passage-15", REGULAR_SOURCE) == moved
    assert reopened.get("passage-15", HIGH_SPEED_SOURCE) == second
    assert len(journal_path.read_text(encoding="utf-8").splitlines()) == 3


def test_association_store_clear_is_an_auditable_tombstone(tmp_path):
    journal_path = tmp_path / "passage_evidence_associations.jsonl"
    store = PassageEvidenceAssociationStore(journal_path)
    _confirm(store)

    assert store.clear("passage-15", REGULAR_SOURCE, confirmed_at_ms=2_000)
    assert store.get("passage-15", REGULAR_SOURCE) is None
    assert not store.clear("passage-15", REGULAR_SOURCE, confirmed_at_ms=3_000)

    records = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["confirmation_status"] for record in records] == [
        "confirmed",
        "deleted",
    ]
    assert records[-1]["revision"] == 2
    assert PassageEvidenceAssociationStore(journal_path).get(
        "passage-15", REGULAR_SOURCE
    ) is None


def test_association_store_recovers_an_incomplete_tail(tmp_path):
    journal_path = tmp_path / "passage_evidence_associations.jsonl"
    store = PassageEvidenceAssociationStore(journal_path)
    association = _confirm(store)
    with journal_path.open("ab") as journal:
        journal.write(b'{"schema_version":1,"passage_event_id":"partial')

    reopened = PassageEvidenceAssociationStore(journal_path)

    assert reopened.recovered_incomplete_tail
    assert reopened.get("passage-15", REGULAR_SOURCE) == association
    assert journal_path.read_bytes().endswith(b"\n")
    moved = _confirm(reopened, x=0.8, confirmed_at_ms=2_000)
    assert moved.revision == 2


def _passage(**changes):
    event = PassageEvent(
        event_id="passage-15", race_id="race", stage_id="stage", group_id="group",
        sequence=15, chip_id="chip-15", bib="15", passage_time_ms=500,
        lap=1, emitted_at_ms=500, received_at_ms=500,
    )
    return replace(event, **changes)


def test_legacy_confirmation_does_not_follow_reused_passage_id(tmp_path):
    store = PassageEvidenceAssociationStore(tmp_path / "associations.jsonl")
    original = _confirm(store, confirmed_at_ms=1_000)
    event = _passage()
    assert store.get_for_event(event, REGULAR_SOURCE) == original
    assert store.get_for_event(replace(event, is_active=False, revision=2), REGULAR_SOURCE) is None
    assert store.get_for_event(replace(event, bib="16", revision=3), REGULAR_SOURCE) is None
    # 同一人再次出发也必须重新确认。
    assert store.get_for_event(replace(event, revision=3, received_at_ms=2_000), REGULAR_SOURCE) is None
    assert store.get(event.event_id, REGULAR_SOURCE) == original


@pytest.mark.parametrize("source", [REGULAR_SOURCE, HIGH_SPEED_SOURCE])
def test_confirmation_is_bound_to_passage_revision_across_restart(tmp_path, source):
    path = tmp_path / "associations.jsonl"
    store = PassageEvidenceAssociationStore(path)
    event = _passage(revision=3)
    association = store.confirm(
        passage_event_id=event.event_id, bib=event.bib, confirmed_source=source,
        segment_id="segment", frame_index=25, position_ms=1_000,
        marker_x_normalized=.5, marker_y_normalized=.5,
        confirmed_at_ms=2_000, passage_revision=event.revision,
    )
    reopened = PassageEvidenceAssociationStore(path)
    assert reopened.get_for_event(event, source) == association
    assert reopened.get_for_event(replace(event, revision=4, passage_time_ms=600), source) is None
    assert reopened.get_for_event(replace(event, bib="16"), source) is None
    assert reopened.get_for_event(replace(event, is_active=False), source) is None
