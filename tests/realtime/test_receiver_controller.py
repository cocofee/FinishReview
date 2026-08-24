from __future__ import annotations

from typing import ClassVar

import pytest

from realtime.receiver_controller import ReceiverController


class _FakeReceiver:
    instances: ClassVar[list["_FakeReceiver"]] = []

    def __init__(
        self,
        host,
        port,
        store,
        *,
        on_accepted,
        metadata_store=None,
        on_metadata_accepted=None,
        on_focus_accepted=None,
    ):
        self.host = host
        self.port = port
        self.store = store
        self.on_accepted = on_accepted
        self.metadata_store = metadata_store
        self.on_metadata_accepted = on_metadata_accepted
        self.on_focus_accepted = on_focus_accepted
        self.is_running = False
        self.stop_calls = 0
        type(self).instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


class _FakeRaceTigerClient:
    instances: ClassVar[list["_FakeRaceTigerClient"]] = []

    def __init__(self, base_url, token, *, pc, rid):
        self.base_url = base_url
        self.token = token
        self.pc = pc
        self.rid = rid
        type(self).instances.append(self)


class _FakeRaceTigerSource:
    instances: ClassVar[list["_FakeRaceTigerSource"]] = []

    def __init__(
        self,
        client,
        store,
        *,
        race_id,
        stage_id,
        poll_interval_seconds,
        on_event,
        on_status,
    ):
        self.client = client
        self.store = store
        self.race_id = race_id
        self.stage_id = stage_id
        self.poll_interval_seconds = poll_interval_seconds
        self.on_event = on_event
        self.on_status = on_status
        self.is_running = False
        self.stop_calls = 0
        type(self).instances.append(self)

    def start(self):
        self.is_running = True

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


@pytest.fixture(autouse=True)
def clear_fakes():
    _FakeReceiver.instances.clear()
    _FakeRaceTigerClient.instances.clear()
    _FakeRaceTigerSource.instances.clear()


def _controller(*, receiver_factory=_FakeReceiver, source_factory=_FakeRaceTigerSource):
    return ReceiverController(
        receiver_factory=receiver_factory,
        racetiger_client_factory=_FakeRaceTigerClient,
        racetiger_source_factory=source_factory,
    )


def test_cyclerace_receiver_start_stop_and_stop_idempotence():
    controller = _controller()
    accepted = []
    metadata = []
    focus = []
    store = object()
    metadata_store = object()

    receiver = controller.start_cyclerace(
        "127.0.0.1",
        18765,
        store,
        on_accepted=accepted.append,
        metadata_store=metadata_store,
        on_metadata_accepted=metadata.append,
        on_focus_accepted=focus.append,
    )

    assert controller.receiver is receiver
    assert receiver.is_running
    assert receiver.store is store
    assert receiver.metadata_store is metadata_store
    assert receiver.on_metadata_accepted == metadata.append
    assert receiver.on_focus_accepted == focus.append

    controller.stop()
    controller.stop()

    assert controller.receiver is None
    assert receiver.stop_calls == 1


def test_cyclerace_receiver_supports_legacy_factory_signature():
    class _LegacyReceiver:
        def __init__(self, host, port, store, *, on_accepted):
            self.host = host
            self.port = port
            self.store = store
            self.on_accepted = on_accepted
            self.is_running = False

        def start(self):
            self.is_running = True

        def stop(self):
            self.is_running = False

    controller = _controller(receiver_factory=_LegacyReceiver)

    receiver = controller.start_cyclerace(
        "127.0.0.1",
        18765,
        object(),
        on_accepted=lambda _event: None,
        metadata_store=object(),
        on_metadata_accepted=lambda _metadata: None,
        on_focus_accepted=lambda _focus: None,
    )

    assert receiver.is_running
    controller.stop()


def test_cyclerace_receiver_checks_optional_factory_arguments_individually():
    class _PartialReceiver:
        def __init__(
            self,
            host,
            port,
            store,
            *,
            on_accepted,
            metadata_store=None,
        ):
            self.host = host
            self.port = port
            self.store = store
            self.on_accepted = on_accepted
            self.metadata_store = metadata_store
            self.is_running = False

        def start(self):
            self.is_running = True

        def stop(self):
            self.is_running = False

    controller = _controller(receiver_factory=_PartialReceiver)
    metadata_store = object()

    receiver = controller.start_cyclerace(
        "127.0.0.1",
        18765,
        object(),
        on_accepted=lambda _event: None,
        metadata_store=metadata_store,
        on_metadata_accepted=lambda _metadata: None,
        on_focus_accepted=lambda _focus: None,
    )

    assert receiver.is_running
    assert receiver.metadata_store is metadata_store
    controller.stop()


def test_cyclerace_start_failure_rolls_back():
    class _FailingReceiver(_FakeReceiver):
        def start(self):
            raise OSError("address unavailable")

    controller = _controller(receiver_factory=_FailingReceiver)

    with pytest.raises(OSError, match="address unavailable"):
        controller.start_cyclerace(
            "127.0.0.1",
            18765,
            object(),
            on_accepted=lambda _event: None,
        )

    receiver = _FailingReceiver.instances[0]
    assert controller.receiver is None
    assert receiver.stop_calls == 1


def test_stop_failure_keeps_receiver_for_retry():
    class _FailOnceStopReceiver(_FakeReceiver):
        def stop(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise OSError("shutdown unavailable")
            self.is_running = False

    controller = _controller(receiver_factory=_FailOnceStopReceiver)
    receiver = controller.start_cyclerace(
        "127.0.0.1",
        18765,
        object(),
        on_accepted=lambda _event: None,
    )

    errors = controller.stop()

    assert errors == ("CycleRace receiver: OSError",)
    assert controller.receiver is receiver
    assert receiver.is_running

    assert controller.stop() == ()
    assert controller.receiver is None
    assert receiver.stop_calls == 2


def test_racetiger_requires_complete_configuration():
    controller = _controller()

    with pytest.raises(ValueError, match="令牌"):
        controller.start_racetiger(
            "https://rqs.racetigertiming.com",
            "",
            pc="finish-pc",
            rid="RID-2026",
            store=object(),
            poll_interval_seconds=2.0,
            on_event=lambda _event, _generation: None,
            on_status=lambda _status, _generation: None,
        )

    assert not _FakeRaceTigerClient.instances
    assert not _FakeRaceTigerSource.instances


def test_racetiger_start_stop_and_late_callback_guard():
    controller = _controller()
    events = []
    statuses = []
    store = object()

    source = controller.start_racetiger(
        "https://rqs.racetigertiming.com",
        "local-test-token",
        pc="finish-pc",
        rid="RID-2026",
        store=store,
        poll_interval_seconds=2.0,
        on_event=lambda event, generation: events.append((event, generation)),
        on_status=lambda status, generation: statuses.append((status, generation)),
    )
    source.on_event("event-1")
    source.on_status("status-1")

    assert controller.racetiger_source is source
    assert source.is_running
    assert source.client.pc == "finish-pc"
    assert source.client.rid == "RID-2026"
    assert source.store is store
    assert events[0][0] == "event-1"
    assert statuses[0] == ("status-1", events[0][1])

    controller.stop()
    source.on_event("late-event")
    source.on_status("late-status")

    assert controller.racetiger_source is None
    assert source.stop_calls == 1
    assert events == [("event-1", events[0][1])]
    assert statuses == [("status-1", events[0][1])]


def test_restarted_racetiger_source_invalidates_previous_callbacks():
    controller = _controller()
    events = []

    first_source = controller.start_racetiger(
        "https://rqs.racetigertiming.com",
        "local-test-token",
        pc="finish-pc",
        rid="RID-2026",
        store=object(),
        poll_interval_seconds=2.0,
        on_event=lambda event, generation: events.append((event, generation)),
        on_status=lambda _status, _generation: None,
    )
    first_source.is_running = False
    second_source = controller.start_racetiger(
        "https://rqs.racetigertiming.com",
        "local-test-token",
        pc="finish-pc",
        rid="RID-2026",
        store=object(),
        poll_interval_seconds=2.0,
        on_event=lambda event, generation: events.append((event, generation)),
        on_status=lambda _status, _generation: None,
    )

    first_source.on_event("stale-event")
    second_source.on_event("current-event")

    assert [event for event, _generation in events] == ["current-event"]
    controller.stop()


def test_racetiger_start_failure_rolls_back():
    class _FailingSource(_FakeRaceTigerSource):
        def start(self):
            raise RuntimeError("poller unavailable")

    controller = _controller(source_factory=_FailingSource)

    with pytest.raises(RuntimeError, match="poller unavailable"):
        controller.start_racetiger(
            "https://rqs.racetigertiming.com",
            "local-test-token",
            pc="finish-pc",
            rid="RID-2026",
            store=object(),
            poll_interval_seconds=2.0,
            on_event=lambda _event, _generation: None,
            on_status=lambda _status, _generation: None,
        )

    source = _FailingSource.instances[0]
    assert controller.racetiger_source is None
    assert source.stop_calls == 1
