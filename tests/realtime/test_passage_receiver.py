import json
import socket
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import realtime.passage_receiver as passage_receiver_module
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


def post_json(
    receiver,
    payload,
    path="/api/v1/passage-events",
    *,
    headers=None,
):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        f"http://127.0.0.1:{receiver.listen_port}{path}",
        data=body,
        headers=request_headers,
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
        "auth_required": False,
    }


def test_discovery_responder_reports_required_authentication():
    responder = PassageDiscoveryResponder(
        18765,
        discovery_port=0,
        host_name="secure-finish-laptop",
        auth_required=True,
    )
    responder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(1.0)
    try:
        client.sendto(DISCOVERY_REQUEST, ("127.0.0.1", responder.listen_port))
        raw, _sender = client.recvfrom(4096)
    finally:
        client.close()
        responder.stop()

    payload = json.loads(raw.decode("utf-8"))
    assert payload["auth_required"] is True


def test_optional_shared_token_rejects_unauthorized_requests(tmp_path):
    accepted = []
    store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        store,
        discovery_port=None,
        shared_token="shared-secret",
        on_accepted=accepted.append,
    )
    receiver.start()
    try:
        missing_status, missing_ack = post_json(receiver, passage_payload())
        wrong_status, wrong_ack = post_json(
            receiver,
            passage_payload(),
            headers={"Authorization": "Bearer wrong-secret"},
        )
    finally:
        receiver.stop()

    assert missing_status == 401
    assert missing_ack["status"] == "rejected"
    assert wrong_status == 401
    assert wrong_ack["status"] == "rejected"
    assert len(store) == 0
    assert accepted == []


def test_shared_token_is_checked_before_request_body_headers(tmp_path):
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
        discovery_port=None,
        shared_token="shared-secret",
    )
    receiver.start()
    connection = HTTPConnection("127.0.0.1", receiver.listen_port, timeout=2.0)
    try:
        connection.putrequest("POST", "/api/v1/passage-events")
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        receiver.stop()

    assert response.status == 401
    assert response.getheader("WWW-Authenticate") == "Bearer"
    assert body["status"] == "rejected"


def test_optional_shared_token_accepts_matching_bearer_token(tmp_path):
    accepted = []
    store = PassageEventStore(tmp_path / "passage-events.jsonl")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        store,
        discovery_port=None,
        shared_token="shared-secret",
        on_accepted=accepted.append,
    )
    receiver.start()
    try:
        status, ack = post_json(
            receiver,
            passage_payload(),
            headers={"Authorization": "Bearer shared-secret"},
        )
    finally:
        receiver.stop()

    assert status == 201
    assert ack["status"] == "accepted"
    assert len(store) == 1
    assert [event.event_id for event in accepted] == [
        "race-1-stage-1-passage-7"
    ]


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
    assert event.received_at_ms == 0
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


def test_receiver_persists_trusted_local_receipt_time(
    running_receiver,
    monkeypatch,
):
    receiver, store, accepted = running_receiver
    received_at_ms = 1_787_578_789_623
    monkeypatch.setattr(
        passage_receiver_module.time,
        "time",
        lambda: received_at_ms / 1000.0,
    )

    status, ack = post_json(
        receiver,
        passage_payload(
            emitted_at_ms=1_787_393_724_380,
            received_at_ms=1,
        ),
    )

    assert status == 201
    assert ack["status"] == "accepted"
    event = store.get("race-1-stage-1-passage-7")
    assert event is not None
    assert event.received_at_ms == received_at_ms
    assert accepted == [event]
    restored = PassageEventStore(store.journal_path).get(event.event_id)
    assert restored is not None
    assert restored.received_at_ms == received_at_ms


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


def test_deleted_passage_stays_deleted_after_restart_and_upstream_updates(tmp_path):
    path = tmp_path / "passages.jsonl"
    store = PassageEventStore(path)
    event = PassageEvent.from_payload(passage_payload())
    other = PassageEvent.from_payload(passage_payload(event_id="other", bib="442"))
    store.append(event)
    store.append(other)
    assert store.delete(event.event_id)
    assert not store.delete(event.event_id)
    assert not store.delete("missing")
    assert store.events() == (other,)
    assert store.events(include_inactive=True) == (other,)
    assert len(store) == 1

    reopened = PassageEventStore(path)
    reopened.append(event)
    assert reopened.events() == (other,)
    same_record = PassageEvent.from_payload(passage_payload(revision=2))
    reopened.append(same_record)
    assert reopened.events() == (other,)
    # CycleRace reuses record ids: a changed bib is a new record to look at.
    updated = PassageEvent.from_payload(passage_payload(revision=3, bib="443"))
    reopened.append(updated)
    assert reopened.events() == (updated, other)
    assert reopened.changed_deletions() == (updated,)
    assert reopened.get(event.event_id) == updated
    assert PassageEventStore(path).events() == (updated, other)
    independent = PassageEventStore(tmp_path / "another-race.jsonl")
    independent.append(event)
    assert independent.events() == (event,)


def test_deletion_keeps_reason_and_reads_legacy_id_lists(tmp_path):
    path = tmp_path / "passages.jsonl"
    store = PassageEventStore(path)
    event = PassageEvent.from_payload(passage_payload())
    store.append(event)
    assert store.delete(event.event_id, reason="测试芯片经过天线")
    mark = PassageEventStore(path).deletion(event.event_id)
    assert mark["reason"] == "测试芯片经过天线" and mark["bib"] == event.bib

    legacy = tmp_path / "legacy.jsonl"
    old_store = PassageEventStore(legacy)
    old_store.append(event)
    legacy.with_name("legacy_deleted.json").write_text(json.dumps([event.event_id]), encoding="utf-8")
    assert PassageEventStore(legacy).events() == ()


def test_failed_deletion_keeps_record_and_previous_deletions(tmp_path, monkeypatch):
    path = tmp_path / "passages.jsonl"
    store = PassageEventStore(path)
    event = PassageEvent.from_payload(passage_payload())
    other = PassageEvent.from_payload(passage_payload(event_id="other"))
    store.append(event)
    store.append(other)
    store.delete(other.event_id)

    def fail_replace(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(passage_receiver_module.os, "replace", fail_replace)
    with pytest.raises(passage_receiver_module.PassageJournalError):
        store.delete(event.event_id)
    assert store.events() == (event,)
    assert PassageEventStore(path).events() == (event,)
    assert not list(tmp_path.glob("*.tmp"))


def test_clear_removes_records_and_deletion_marks_before_a_race(tmp_path):
    journal = tmp_path / "passage-events.jsonl"
    store = PassageEventStore(journal)
    event = PassageEvent.from_payload(passage_payload())
    store.append(event)
    store.delete(event.event_id, reason="赛前测试")
    assert store.events() == ()
    assert store.deletions_path.exists()

    store.clear()

    assert store.events(include_inactive=True) == ()
    assert not journal.exists()
    assert not store.deletions_path.exists()

    # 新一场比赛可以用同样的号码、芯片和时刻重新计入，不被赛前删除标记挡住。
    store.append(event)
    assert store.get(event.event_id) == event


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
    assert first_ack["error"] == "delivery_failed"
    assert second_status == 200
    assert second_ack["status"] == "duplicate"
    assert attempts == [
        "race-1-stage-1-passage-7",
        "race-1-stage-1-passage-7",
    ]
    assert len(store) == 1
    assert len(store.journal_path.read_text(encoding="utf-8").splitlines()) == 1


def test_metadata_callback_failure_retries_delivery_without_rewriting_snapshot(
    tmp_path,
):
    internal_message = r"C:\private\race\event.db failed"
    attempts = []

    def deliver_metadata(metadata):
        attempts.append(metadata.revision)
        if len(attempts) == 1:
            raise RuntimeError(internal_message)

    metadata_store = RaceMetadataStore(tmp_path / "race-metadata.json")
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
        discovery_port=None,
        metadata_store=metadata_store,
        on_metadata_accepted=deliver_metadata,
    )
    receiver.start()
    try:
        first_status, first_ack = post_json(receiver, metadata_payload())
        second_status, second_ack = post_json(receiver, metadata_payload())
    finally:
        receiver.stop()

    assert first_status == 503
    assert first_ack == {
        "schema_version": 1,
        "message_type": "race_metadata_ack",
        "status": "retry",
        "error": "delivery_failed",
    }
    assert second_status == 200
    assert second_ack == {
        "schema_version": 1,
        "message_type": "race_metadata_ack",
        "status": "duplicate",
    }
    assert attempts == [1_787_388_800_000, 1_787_388_800_000]
    assert metadata_store.current() is not None
    assert internal_message not in json.dumps(first_ack)


def test_internal_receiver_error_is_not_returned_to_client(tmp_path):
    internal_message = r"C:\private\race\event.db failed"

    class FailingMetadataStore:
        def store(self, _metadata):
            raise RuntimeError(internal_message)

    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
        discovery_port=None,
        metadata_store=FailingMetadataStore(),
    )
    receiver.start()
    try:
        status, ack = post_json(receiver, metadata_payload())
    finally:
        receiver.stop()

    assert status == 500
    assert ack["message_type"] == "race_metadata_ack"
    assert ack["status"] == "error"
    assert ack["error"] == "internal_error"
    assert internal_message not in json.dumps(ack)


def test_receiver_times_out_incomplete_request_body(tmp_path):
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
        discovery_port=None,
        request_timeout_seconds=0.1,
    )
    receiver.start()
    client = socket.create_connection(("127.0.0.1", receiver.listen_port), timeout=2.0)
    client.settimeout(2.0)
    try:
        client.sendall(
            b"POST /api/v1/passage-events HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 128\r\n"
            b"Connection: close\r\n\r\n"
        )
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        client.close()
        receiver.stop()

    header, body = response.split(b"\r\n\r\n", 1)
    ack = json.loads(body.decode("utf-8"))
    assert b" 408 " in header
    assert ack["status"] == "rejected"
    assert ack["error"] == "request_timeout"


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


def test_restart_duplicate_notifies_new_review_workspace(tmp_path, monkeypatch):
    journal = tmp_path / "passage-events.jsonl"
    first_store = PassageEventStore(journal)
    first_store.append(PassageEvent.from_payload(passage_payload()))
    restored_store = PassageEventStore(journal)
    delivered = []
    received_at_ms = 1_787_578_789_623
    monkeypatch.setattr(
        passage_receiver_module.time,
        "time",
        lambda: received_at_ms / 1000.0,
    )
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
    assert delivered[0].received_at_ms == received_at_ms
    assert restored_store.get(delivered[0].event_id).received_at_ms == received_at_ms
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


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


def test_stop_keeps_thread_reference_after_join_timeout(tmp_path):
    class _StuckThread:
        def __init__(self):
            self.alive = True
            self.join_calls = 0

        def join(self, timeout=None):
            self.join_calls += 1

        def is_alive(self):
            return self.alive

    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        PassageEventStore(tmp_path / "passage-events.jsonl"),
        discovery_port=None,
    )
    thread = _StuckThread()
    receiver._thread = thread

    receiver.stop()

    assert receiver.is_running is True
    assert thread.join_calls == 1

    thread.alive = False
    receiver.stop()

    assert receiver.is_running is False
    assert thread.join_calls == 2


def test_a_group_s_target_laps_are_optional_and_kept_when_sent():
    """CycleRace sends each group's target laps in the race metadata; an older one does not (0)."""
    from realtime.race_metadata import RaceMetadata, RaceMetadataError
    laps = metadata_payload(groups=[{"group_id": "1", "name": "男子组", "target_laps": 4}])
    sent = RaceMetadata.from_payload(laps)
    assert sent.groups[0].target_laps == 4
    assert sent.to_payload()["groups"] == [{"group_id": "1", "name": "男子组", "target_laps": 4}]
    old = RaceMetadata.from_payload(metadata_payload())
    assert old.groups[0].target_laps == 0
    assert old.to_payload()["groups"] == [{"group_id": "1", "name": "男子精英组"}]
    for wrong in (-1, "4", True):
        with pytest.raises(RaceMetadataError):
            RaceMetadata.from_payload(metadata_payload(groups=[{"group_id": "1", "name": "男子组",
                                                                "target_laps": wrong}]))


def test_the_same_revision_sent_again_with_target_laps_is_kept_not_refused(tmp_path):
    """An older console kept CycleRace's snapshot without the laps; after the update the same revision
    comes again with them: it is kept, not refused (refused, CycleRace would show a sending error)."""
    from realtime.race_metadata import RaceMetadata, RaceMetadataConflictError, RaceMetadataIngestResult
    path = tmp_path / "race-metadata.json"
    RaceMetadataStore(path).store(RaceMetadata.from_payload(metadata_payload()))
    store = RaceMetadataStore(path)
    laps = metadata_payload(groups=[{"group_id": "1", "name": "男子精英组", "target_laps": 4}])
    assert store.store(RaceMetadata.from_payload(laps)) == RaceMetadataIngestResult.ACCEPTED
    assert RaceMetadataStore(path).current().groups[0].target_laps == 4
    assert store.store(RaceMetadata.from_payload(laps)) == RaceMetadataIngestResult.DUPLICATE
    other = metadata_payload(groups=[{"group_id": "1", "name": "女子组", "target_laps": 4}])
    with pytest.raises(RaceMetadataConflictError):
        store.store(RaceMetadata.from_payload(other))
