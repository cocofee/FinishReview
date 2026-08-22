import json
import socket
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from realtime.passage_receiver import (
    DISCOVERY_REQUEST,
    DISCOVERY_SERVICE,
    PassageEvent,
    PassageEventReceiver,
    PassageEventStore,
    PassageDiscoveryResponder,
    RaceFocus,
)
from realtime.race_metadata import RaceMetadataStore


def passage_payload(**overrides):
    payload = {
        "schema_version": 1,
        "message_type": "passage",
        "event_id": "race-1-stage-1-passage-7",
        "race_id": "race-1",
        "stage_id": "stage-1",
        "group_id": "men-open",
        "sequence": 7,
        "chip_id": "chip-23",
        "bib": "23",
        "passage_time_ms": 1_787_217_138_520,
        "lap": 1,
        "source": "cyclerace",
        "emitted_at_ms": 1_787_217_138_700,
        "revision": 1,
        "race_name": "2026 城市自行车赛",
        "stage_name": "第一赛段",
        "group_name": "男子公开组",
        "athlete_id": "101",
        "athlete_name": "张三",
        "team_name": "示例车队",
    }
    payload.update(overrides)
    return payload


def metadata_payload(**overrides):
    payload = {
        "schema_version": 1,
        "message_type": "race_metadata",
        "race_id": "race-11",
        "stage_id": "stage-1",
        "revision": 1_787_388_800_000,
        "emitted_at_ms": 1_787_388_800_000,
        "race_name": "11",
        "stage_name": "1",
        "stage_date": "2026-08-22",
        "groups": [
            {"group_id": "1", "name": "男子精英组"},
        ],
        "athletes": [
            {
                "athlete_id": "15",
                "bib": "15",
                "name": "测试运动员",
                "team_name": "示例队",
                "group_id": "1",
                "chip_ids": ["261623"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def focus_payload(**overrides):
    payload = {
        "schema_version": 1,
        "message_type": "race_focus",
        "race_id": "race-11",
        "stage_id": "stage-1",
        "athlete_id": "15",
        "bib": "15",
        "group_id": "1",
        "emitted_at_ms": 1_787_388_800_100,
    }
    payload.update(overrides)
    return payload


def post_json(receiver, payload, path="/api/v1/passage-events"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{receiver.listen_port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


@pytest.fixture
def running_receiver(tmp_path):
    accepted = []
    store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        store,
        discovery_port=None,
        on_accepted=accepted.append,
    )
    receiver.start()
    try:
        yield receiver, store, accepted
    finally:
        receiver.stop()


def test_discovery_responder_reports_receiver_without_shared_path():
    responder = PassageDiscoveryResponder(
        18765,
        discovery_port=0,
        host_name="finish-laptop",
    )
    responder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(1.0)
    try:
        client.sendto(
            DISCOVERY_REQUEST,
            ("127.0.0.1", responder.listen_port),
        )
        raw, _sender = client.recvfrom(4096)
    finally:
        client.close()
        responder.stop()

    payload = json.loads(raw.decode("utf-8"))
    assert payload == {
        "schema_version": 1,
        "message_type": "discovery_response",
        "service": DISCOVERY_SERVICE,
        "host_name": "finish-laptop",
        "port": 18765,
    }


def test_accepts_and_deduplicates_one_revision(running_receiver):
    receiver, store, accepted = running_receiver

    first_status, first_ack = post_json(receiver, passage_payload())
    second_status, second_ack = post_json(receiver, passage_payload())

    assert first_status == 201
    assert first_ack["status"] == "accepted"
    assert second_status == 200
    assert second_ack["status"] == "duplicate"
    assert len(store) == 1
    assert [event.event_id for event in accepted] == [
        "race-1-stage-1-passage-7"
    ]
    assert len(store.journal_path.read_text(encoding="utf-8").splitlines()) == 1


def test_accepts_race_metadata_without_creating_a_passage(tmp_path):
    accepted_metadata = []
    passage_store = PassageEventStore(tmp_path / "passage-events.jsonl")
    metadata_store = RaceMetadataStore(tmp_path / "race-metadata.json")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        passage_store,
        discovery_port=None,
        metadata_store=metadata_store,
        on_metadata_accepted=accepted_metadata.append,
    )
    receiver.start()
    try:
        first_status, first_ack = post_json(receiver, metadata_payload())
        second_status, second_ack = post_json(receiver, metadata_payload())
    finally:
        receiver.stop()

    assert first_status == 201
    assert first_ack["message_type"] == "race_metadata_ack"
    assert second_status == 200
    assert second_ack["status"] == "duplicate"
    assert len(passage_store) == 0
    metadata = metadata_store.current()
    assert metadata is not None
    assert metadata.race_name == "11"
    assert metadata.stage_name == "1"
    assert metadata.groups[0].name == "男子精英组"
    assert metadata.athletes[0].chip_ids == ("261623",)
    assert [item.race_id for item in accepted_metadata] == ["race-11"]


def test_accepts_race_focus_without_creating_a_passage(tmp_path):
    accepted_focus = []
    passage_store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        passage_store,
        discovery_port=None,
        on_focus_accepted=accepted_focus.append,
    )
    receiver.start()
    try:
        status, ack = post_json(receiver, focus_payload())
    finally:
        receiver.stop()

    assert status == 201
    assert ack["message_type"] == "race_focus_ack"
    assert ack["status"] == "accepted"
    assert len(passage_store) == 0
    assert accepted_focus == [
        RaceFocus(
            race_id="race-11",
            stage_id="stage-1",
            athlete_id="15",
            bib="15",
            group_id="1",
            emitted_at_ms=1_787_388_800_100,
        )
    ]


def test_new_race_metadata_context_replaces_higher_revision_snapshot(tmp_path):
    metadata_store = RaceMetadataStore(tmp_path / "race-metadata.json")
    passage_store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        passage_store,
        discovery_port=None,
        metadata_store=metadata_store,
    )
    receiver.start()
    try:
        first_status, _ = post_json(
            receiver,
            metadata_payload(revision=100, race_name="old-race"),
        )
        second_status, _ = post_json(
            receiver,
            metadata_payload(
                race_id="race-12",
                revision=1,
                emitted_at_ms=1,
                race_name="new-race",
            ),
        )
    finally:
        receiver.stop()

    assert first_status == 201
    assert second_status == 201
    assert metadata_store.current().race_id == "race-12"
    assert metadata_store.current().race_name == "new-race"


def test_optional_field_preserves_positional_constructor_contract():
    event = PassageEvent(
        "event-1",
        "race-1",
        "stage-1",
        "group-1",
        7,
        "chip-23",
        "23",
        123_456,
        2,
        "cyclerace",
        456_789,
        3,
        1,
        "passage",
    )

    assert event.lap == 2
    assert event.message_type == "passage"
    assert event.passage_timestamp_ms is None
    assert event.timeline_timestamp_ms == 123_456


def test_optional_absolute_passage_timestamp_is_preserved(running_receiver):
    receiver, store, accepted = running_receiver
    absolute_timestamp_ms = 1_786_252_979_215

    status, ack = post_json(
        receiver,
        passage_payload(
            passage_time_ms=48_179_215,
            passage_timestamp_ms=absolute_timestamp_ms,
        ),
    )

    assert status == 201
    assert ack["status"] == "accepted"
    event = store.get("race-1-stage-1-passage-7")
    assert event.passage_time_ms == 48_179_215
    assert event.passage_timestamp_ms == absolute_timestamp_ms
    assert event.timeline_timestamp_ms == absolute_timestamp_ms
    assert accepted == [event]


def test_cyclerace_display_metadata_is_persisted(running_receiver):
    receiver, store, accepted = running_receiver

    status, ack = post_json(receiver, passage_payload())

    assert status == 201
    assert ack["status"] == "accepted"
    event = store.get("race-1-stage-1-passage-7")
    assert event is not None
    assert event.race_name == "2026 城市自行车赛"
    assert event.stage_name == "第一赛段"
    assert event.group_name == "男子公开组"
    assert event.athlete_id == "101"
    assert event.athlete_name == "张三"
    assert event.team_name == "示例车队"
    assert accepted == [event]


def test_same_revision_with_different_content_is_rejected(running_receiver):
    receiver, store, _ = running_receiver
    assert post_json(receiver, passage_payload())[0] == 201

    status, ack = post_json(receiver, passage_payload(bib="99"))

    assert status == 409
    assert ack["status"] == "rejected"
    assert store.get("race-1-stage-1-passage-7").bib == "23"


def test_race_journal_accepts_passage_from_another_race(running_receiver):
    receiver, store, accepted = running_receiver
    assert post_json(receiver, passage_payload())[0] == 201

    status, ack = post_json(
        receiver,
        passage_payload(
            event_id="race-2-stage-1-passage-1",
            race_id="race-2",
            sequence=1,
        ),
    )

    assert status == 201
    assert ack["status"] == "accepted"
    assert len(store) == 2
    assert [event.race_id for event in accepted] == ["race-1", "race-2"]


def test_newer_revision_replaces_latest_and_stale_revision_is_duplicate(
    running_receiver,
):
    receiver, store, accepted = running_receiver
    assert post_json(receiver, passage_payload())[0] == 201

    corrected = passage_payload(revision=2, bib="24", emitted_at_ms=1_787_217_139_000)
    corrected_status, corrected_ack = post_json(receiver, corrected)
    stale_status, stale_ack = post_json(receiver, passage_payload())

    assert corrected_status == 201
    assert corrected_ack["status"] == "accepted"
    assert stale_status == 200
    assert stale_ack["status"] == "duplicate"
    assert store.get("race-1-stage-1-passage-7").revision == 2
    assert store.get("race-1-stage-1-passage-7").bib == "24"
    assert [event.revision for event in accepted] == [1, 2]
    assert len(store.journal_path.read_text(encoding="utf-8").splitlines()) == 2


def test_inactive_revision_is_retained_for_audit_but_hidden_from_active_events(
    running_receiver,
):
    receiver, store, accepted = running_receiver
    assert post_json(receiver, passage_payload())[0] == 201

    status, ack = post_json(
        receiver,
        passage_payload(
            revision=2,
            is_active=False,
            emitted_at_ms=1_787_217_139_000,
        ),
    )

    assert status == 201
    assert ack["status"] == "accepted"
    assert len(store) == 0
    assert store.events() == ()
    audit_events = store.events(include_inactive=True)
    assert len(audit_events) == 1
    assert audit_events[0].is_active is False
    assert store.get(audit_events[0].event_id) == audit_events[0]
    assert [event.is_active for event in accepted] == [True, False]
    assert len(store.journal_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},
        passage_payload(schema_version=2),
        passage_payload(message_type="result"),
        passage_payload(sequence=0),
        passage_payload(sequence="7"),
        passage_payload(chip_id="", bib=""),
        passage_payload(passage_time_ms=-1),
        passage_payload(passage_timestamp_ms=-1),
        passage_payload(lap=-1),
        passage_payload(revision=0),
        passage_payload(is_active=1),
    ],
)
def test_invalid_payload_is_rejected(running_receiver, payload):
    receiver, store, accepted = running_receiver

    status, ack = post_json(receiver, payload)

    assert status == 400
    assert ack["status"] == "rejected"
    assert len(store) == 0
    assert accepted == []


def test_wrong_endpoint_is_not_accepted(running_receiver):
    receiver, store, accepted = running_receiver

    status, ack = post_json(receiver, passage_payload(), path="/wrong")

    assert status == 404
    assert ack["status"] == "rejected"
    assert len(store) == 0
    assert accepted == []


def test_callback_failure_returns_retry_and_duplicate_retry_delivers(tmp_path):
    attempts = []

    def callback(event):
        attempts.append(event.event_id)
        if len(attempts) == 1:
            raise RuntimeError("review workspace unavailable")

    store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        store,
        on_accepted=callback,
    )
    receiver.start()
    try:
        first_status, first_ack = post_json(receiver, passage_payload())
        second_status, second_ack = post_json(receiver, passage_payload())
    finally:
        receiver.stop()

    assert first_status == 503
    assert first_ack["status"] == "retry"
    assert second_status == 200
    assert second_ack["status"] == "duplicate"
    assert attempts == [
        "race-1-stage-1-passage-7",
        "race-1-stage-1-passage-7",
    ]
    assert len(store) == 1
    assert len(store.journal_path.read_text(encoding="utf-8").splitlines()) == 1


def test_store_recovers_incomplete_tail_and_restores_latest_revision(tmp_path):
    journal = tmp_path / "passage-events.jsonl"
    first = PassageEvent.from_payload(passage_payload())
    second = PassageEvent.from_payload(
        passage_payload(revision=2, bib="24", emitted_at_ms=1_787_217_139_000)
    )
    journal.write_bytes(
        json.dumps(first.to_payload(), separators=(",", ":")).encode("utf-8")
        + b"\n"
        + json.dumps(second.to_payload(), separators=(",", ":")).encode("utf-8")
        + b"\n"
        + b'{"schema_version":1,"message_type":"passage"'
    )

    store = PassageEventStore(journal)

    assert store.recovered_incomplete_tail is True
    assert len(store) == 1
    assert store.get(first.event_id).revision == 2
    assert store.get(first.event_id).bib == "24"
    assert journal.read_bytes().endswith(b"\n")
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


def test_restart_duplicate_notifies_new_review_workspace(tmp_path):
    journal = tmp_path / "passage-events.jsonl"
    first_store = PassageEventStore(journal)
    first_store.append(PassageEvent.from_payload(passage_payload()))
    restored_store = PassageEventStore(journal)
    delivered = []
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        restored_store,
        on_accepted=delivered.append,
    )
    receiver.start()
    try:
        status, ack = post_json(receiver, passage_payload())
    finally:
        receiver.stop()

    assert status == 200
    assert ack["status"] == "duplicate"
    assert [event.event_id for event in delivered] == [
        "race-1-stage-1-passage-7"
    ]


def test_stop_is_idempotent(tmp_path):
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
    )
    receiver.start()
    assert receiver.is_running is True

    receiver.stop()
    receiver.stop()

    assert receiver.is_running is False
