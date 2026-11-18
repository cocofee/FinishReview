import os

import pytest

from realtime import atomic_file
from realtime.atomic_file import atomic_open, atomic_write_bytes, atomic_write_text


def _leftovers(folder):
    return sorted(path.name for path in folder.iterdir() if path.name.endswith(".tmp"))


def test_write_text_replaces_content_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "drafts.json"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "新内容", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "新内容"
    assert _leftovers(tmp_path) == []


def test_text_newlines_match_path_write_text(tmp_path):
    text = "a\nb\r\nc\n"
    expected = tmp_path / "expected.txt"
    expected.write_text(text, encoding="utf-8")
    target = tmp_path / "actual.txt"

    atomic_write_text(target, text, encoding="utf-8")

    assert target.read_bytes() == expected.read_bytes()
    atomic_write_text(target, text, encoding="utf-8", newline="\n")
    assert target.read_bytes() == text.encode("utf-8")


def test_failure_while_writing_keeps_old_content(tmp_path):
    target = tmp_path / "export.csv"
    target.write_bytes(b"old")

    with pytest.raises(RuntimeError):
        with atomic_open(target, "wb") as handle:
            handle.write(b"half")
            raise RuntimeError("boom")

    assert target.read_bytes() == b"old"
    assert _leftovers(tmp_path) == []


def test_failed_replace_keeps_old_content_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_bytes(b"old")
    calls = []

    def refuse(source, destination):
        calls.append(source)
        raise PermissionError("held open")

    monkeypatch.setattr(atomic_file.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(os, "replace", refuse)

    with pytest.raises(PermissionError):
        atomic_write_bytes(target, b"new")

    assert len(calls) == atomic_file._REPLACE_ATTEMPTS
    assert target.read_bytes() == b"old"
    assert _leftovers(tmp_path) == []


def test_brief_permission_error_is_retried(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    real_replace = os.replace
    failures = [PermissionError("scanner")]

    def flaky(source, destination):
        if failures:
            raise failures.pop()
        real_replace(source, destination)

    monkeypatch.setattr(atomic_file.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(os, "replace", flaky)

    atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"


def test_durable_forces_disk_write_and_cache_does_not(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))

    atomic_write_text(tmp_path / "work.json", "{}", encoding="utf-8")
    assert len(synced) == 1
    atomic_write_text(tmp_path / "cache.json", "{}", encoding="utf-8", durable=False)
    assert len(synced) == 1


def test_temporary_name_is_unique_per_process_and_thread(tmp_path):
    temporary = atomic_file.temporary_path_for(tmp_path / "a.json")

    assert temporary.parent == tmp_path
    assert temporary.name.startswith("a.json.")
    assert str(os.getpid()) in temporary.name
    assert temporary.suffix == ".tmp"


def test_rejects_append_mode_and_text_without_encoding(tmp_path):
    with pytest.raises(ValueError):
        with atomic_open(tmp_path / "x", "a", encoding="utf-8"):
            pass
    with pytest.raises(ValueError):
        with atomic_open(tmp_path / "x", "w"):
            pass
    assert not (tmp_path / "x").exists()
