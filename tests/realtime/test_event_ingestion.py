from __future__ import annotations

from dataclasses import replace
import time
import threading

import pytest

from realtime.event_ingestion import EventIngestion, IngestionContext, merge_events
from realtime.passage_receiver import PassageEvent, PassageEventStore


def event(revision=1, active=True, event_id="e"):
    return PassageEvent(
        event_id=event_id, race_id="race", stage_id="finish", group_id="g",
        sequence=1, bib="1", revision=revision, is_active=active,
    )


def test_projection_is_ordered_and_revisioned(tmp_path):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    first = event()
    withdrawn = replace(first, revision=2, is_active=False)
    inbox.append(first)
    inbox.append(withdrawn)
    commits = []
    worker = EventIngestion(
        IngestionContext(7, "cyclerace", "race", inbox, target),
        lambda worker: commits.extend(worker.take_completed()),
        lambda error: (_ for _ in ()).throw(RuntimeError(error)) if error else None,
    )
    assert worker.submit(first)
    assert worker.submit(first)
    assert worker.submit(withdrawn)
    deadline = time.monotonic() + 2
    while target.get("e") is None or target.get("e").revision < 2:
        assert time.monotonic() < deadline
        time.sleep(.01)
    worker.close().result(2)
    assert target.get("e").is_active is False
    assert [commit.event.revision for commit in commits] == [1, 2]
    assert len(target.events(include_inactive=True)) == 1


def test_merge_recovery_keeps_withdrawal_and_is_idempotent(tmp_path):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    inbox.append(event())
    inbox.append(event(revision=2, active=False))
    assert merge_events(target, (inbox,), "race") == 1
    assert merge_events(target, (inbox,), "race") == 0
    assert target.get("e").revision == 2
    assert target.events() == ()


def test_close_drains_accepted_event(tmp_path):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    commits = []
    worker = EventIngestion(
        IngestionContext(1, "cyclerace", "race", inbox, target),
        lambda worker: commits.extend(worker.take_completed()),
        lambda _error: None,
    )
    inbox.append(event())
    assert worker.submit(event())
    worker.close().result(2)
    assert target.get("e") is not None
    assert commits


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


def test_overflow_recovers_latest_tombstones_after_ordered_queue(tmp_path, monkeypatch):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    entered, release = threading.Event(), threading.Event()
    append = target.append
    def slow_append(item):
        entered.set()
        assert release.wait(3)
        return append(item)
    monkeypatch.setattr(target, "append", slow_append)
    commits = []
    worker = EventIngestion(IngestionContext(1, "cyclerace", "race", inbox, target),
                            lambda owner: commits.extend(owner.take_completed()),
                            lambda _: None, capacity=2)
    try:
        inbox.append(event())
        worker.submit(inbox.get("e"))
        assert entered.wait(1)
        for revision in range(2, 9):
            item = event(revision, active=revision != 8)
            inbox.append(item)
            worker.submit(inbox.get("e"))
            assert worker.pending_count <= 2
        release.set()
        worker.close().result(3)
        assert [item.event.revision for item in commits] == [1, 2, 8]
        assert target.events() == ()
        assert len(inbox.journal_path.read_text().splitlines()) == 8
        assert len(target.journal_path.read_text().splitlines()) == 3
    finally:
        release.set()
        worker.close().result(3)


def test_disk_failure_retry_and_restart_recover_withdrawal(tmp_path, monkeypatch):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    target.append(event())
    inbox.append(event(2, False))
    commits, errors = [], []
    append = target.append
    def fail(_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(target, "append", fail)
    worker = EventIngestion(IngestionContext(1, "cyclerace", "race", inbox, target),
                            lambda owner: commits.extend(owner.take_completed()), errors.append,
                            retry_seconds=.02)
    worker.recover()
    wait_until(lambda: bool(errors))
    assert not commits and target.get("e").is_active
    with pytest.raises(OSError):
        worker.close().result(3)
    monkeypatch.setattr(target, "append", append)
    restored_inbox = PassageEventStore(inbox.journal_path)
    restored_target = PassageEventStore(target.journal_path)
    worker = EventIngestion(IngestionContext(2, "cyclerace", "race", restored_inbox, restored_target),
                            lambda owner: commits.extend(owner.take_completed()), errors.append)
    worker.recover()
    worker.close().result(3)
    assert restored_target.events() == ()
    assert commits[-1].event.revision == 2


def test_transient_failure_clears_error_and_retries_without_new_notification(tmp_path, monkeypatch):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    inbox.append(event())
    attempts, errors = [], []
    append = target.append
    def fail_once(item):
        attempts.append(item)
        if len(attempts) == 1:
            raise OSError("temporarily locked")
        return append(item)
    monkeypatch.setattr(target, "append", fail_once)
    worker = EventIngestion(IngestionContext(1, "cyclerace", "race", inbox, target),
                            lambda owner: owner.take_completed(), errors.append, retry_seconds=.01)
    worker.submit(inbox.get("e"))
    wait_until(lambda: worker.idle)
    worker.close().result(2)
    assert errors == ["temporarily locked", ""]
    assert target.get("e") is not None


@pytest.mark.parametrize("provider", ["cyclerace", "racetiger"])
def test_same_journal_commits_without_second_append(tmp_path, monkeypatch, provider):
    inbox = PassageEventStore(tmp_path / "events.jsonl")
    inbox.append(event())
    target = PassageEventStore(inbox.journal_path)
    monkeypatch.setattr(target, "append", lambda _: pytest.fail("second write"))
    commits = []
    worker = EventIngestion(IngestionContext(1, provider, "race", inbox, target),
                            lambda owner: commits.extend(owner.take_completed()), lambda _: None)
    worker.submit(inbox.get("e"))
    worker.close().result(2)
    assert len(commits) == 1
    assert commits[0].durable_at > 0
    assert commits[0].committed_at >= commits[0].durable_at
    assert "durable_monotonic" not in inbox.journal_path.read_text()


def test_fsync_does_not_block_reads_or_expose_uncommitted_event(tmp_path, monkeypatch):
    import realtime.passage_receiver as module
    store = PassageEventStore(tmp_path / "events.jsonl")
    entered, release = threading.Event(), threading.Event()
    def slow_fsync(_):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(module.os, "fsync", slow_fsync)
    thread = threading.Thread(target=store.append, args=(event(),))
    thread.start()
    try:
        assert entered.wait(1)
        started = time.perf_counter()
        assert store.events() == () and store.get("e") is None
        assert time.perf_counter() - started < .1
    finally:
        release.set()
        thread.join(3)
    assert store.get("e") is not None


def test_completion_mailbox_is_bounded_and_only_signals_empty_edge(tmp_path):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    for index in range(10):
        inbox.append(event(event_id=str(index)))
    ready = []
    worker = EventIngestion(IngestionContext(1, "cyclerace", "race", inbox, target),
                            ready.append, lambda _: None, capacity=2)
    worker.recover()
    wait_until(lambda: len(worker._completed) == 2)
    assert len(ready) == 1
    commits = []
    deadline = time.monotonic() + 3
    while not worker.idle:
        assert time.monotonic() < deadline
        commits.extend(worker.take_completed())
        time.sleep(.005)
    commits.extend(worker.take_completed())
    worker.close().result(2)
    assert len(commits) == 10


def test_close_with_full_mailbox_does_not_need_gui_consumer(tmp_path):
    inbox = PassageEventStore(tmp_path / "inbox.jsonl")
    target = PassageEventStore(tmp_path / "target.jsonl")
    for index in range(10):
        inbox.append(event(event_id=str(index), active=index != 9))
    worker = EventIngestion(IngestionContext(1, "cyclerace", "race", inbox, target),
                            lambda _: None, lambda _: None, capacity=2)
    worker.recover()
    wait_until(lambda: len(worker._completed) == 2)
    worker.close().result(3)
    assert len(worker.take_completed()) <= 2
    restored = PassageEventStore(target.journal_path)
    assert len(restored.events(include_inactive=True)) == 10
    assert not restored.get("9").is_active
