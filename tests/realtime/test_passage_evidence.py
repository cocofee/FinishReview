import json

from realtime.passage_evidence import (
    HIGH_SPEED_SOURCE,
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
)


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
