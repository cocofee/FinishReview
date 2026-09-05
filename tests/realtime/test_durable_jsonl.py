import json
import threading

import pytest

import realtime.durable_jsonl as durable_jsonl
from realtime.durable_jsonl import append_jsonl_records


def test_durable_jsonl_streams_records_with_one_fsync(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    fsync_calls = []
    monkeypatch.setattr(
        durable_jsonl.os,
        "fsync",
        lambda file_descriptor: fsync_calls.append(file_descriptor),
    )

    append_jsonl_records(
        path,
        (json.dumps({"value": value}).encode("utf-8") for value in range(3)),
        description="test journal",
    )

    assert len(fsync_calls) == 1
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"value": 0},
        {"value": 1},
        {"value": 2},
    ]


def test_durable_jsonl_serializes_rollback_with_second_store(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "records.jsonl"
    first_fsync_started = threading.Event()
    release_first_fsync = threading.Event()
    second_finished = threading.Event()
    errors = []
    fsync_count = 0
    count_lock = threading.Lock()

    def controlled_fsync(_file_descriptor):
        nonlocal fsync_count
        with count_lock:
            fsync_count += 1
            call = fsync_count
        if call == 1:
            first_fsync_started.set()
            assert release_first_fsync.wait(2)
            raise OSError("disk full")

    monkeypatch.setattr(durable_jsonl.os, "fsync", controlled_fsync)

    def first_writer():
        try:
            append_jsonl_records(
                path,
                (b'{"writer":1}',),
                description="test journal",
            )
        except RuntimeError as error:
            errors.append(error)

    def second_writer():
        append_jsonl_records(
            path,
            (b'{"writer":2}',),
            description="test journal",
        )
        second_finished.set()

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    assert first_fsync_started.wait(2)
    second.start()
    assert not second_finished.wait(0.05)

    release_first_fsync.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(errors) == 1
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_finished.is_set()
    assert path.read_text(encoding="utf-8") == '{"writer":2}\n'


def test_durable_jsonl_rolls_back_iterator_failure(tmp_path):
    path = tmp_path / "records.jsonl"

    def records():
        yield b'{"value":1}'
        raise TypeError("serialization failed")

    with pytest.raises(RuntimeError, match="failed to append"):
        append_jsonl_records(
            path,
            records(),
            description="test journal",
        )

    assert path.read_bytes() == b""


def test_durable_jsonl_reports_rollback_failure(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(
        durable_jsonl.os,
        "fsync",
        lambda _file_descriptor: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(RuntimeError, match="append and roll back"):
        append_jsonl_records(
            path,
            (b'{"value":1}',),
            description="test journal",
        )
