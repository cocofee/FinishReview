"""Process-local durable append helpers for workspace JSONL journals."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import threading
import time
from typing import Iterable, Iterator


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCK_TIMEOUT_SECONDS = 10.0


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.expanduser().absolute()))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            deadline = time.monotonic() + _FILE_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"timed out locking JSONL journal: {path}")
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        deadline = time.monotonic() + _FILE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"timed out locking JSONL journal: {path}")
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_jsonl(path: str | Path) -> Iterator[Path]:
    """Serialize a complete read/recover or append transaction for one journal."""
    journal_path = Path(path).expanduser().absolute()
    with _path_lock(journal_path), _file_lock(journal_path):
        yield journal_path


def append_jsonl_records(
    path: str | Path,
    records: Iterable[bytes],
    *,
    description: str,
    already_locked: bool = False,
) -> int:
    """Append newline-terminated records with one flush/fsync and rollback.

    A process-wide path lock plus an operating-system file lock serialize store
    instances that target the same journal. Records are streamed rather than joined, bounding temporary
    memory while retaining one durability boundary for the batch.
    """

    journal_path = Path(path).expanduser().absolute()
    lock_context = (
        nullcontext(journal_path)
        if already_locked
        else locked_jsonl(journal_path)
    )
    with lock_context:
        try:
            original_size = (
                journal_path.stat().st_size if journal_path.exists() else 0
            )
        except OSError as error:
            raise RuntimeError(
                f"failed to inspect {description}: {journal_path}"
            ) from error

        separator = b""
        if original_size:
            try:
                with journal_path.open("rb") as journal:
                    journal.seek(-1, os.SEEK_END)
                    if journal.read(1) not in {b"\n", b"\r"}:
                        separator = b"\n"
            except OSError as error:
                raise RuntimeError(
                    f"failed to inspect {description}: {journal_path}"
                ) from error

        bytes_written = 0
        try:
            with journal_path.open("ab") as journal:
                if separator:
                    journal.write(separator)
                    bytes_written += len(separator)
                for record in records:
                    payload = bytes(record)
                    if not payload:
                        continue
                    if not payload.endswith((b"\n", b"\r")):
                        payload += b"\n"
                    journal.write(payload)
                    bytes_written += len(payload)
                journal.flush()
                os.fsync(journal.fileno())
        except Exception as error:
            try:
                with journal_path.open("r+b") as journal:
                    journal.truncate(original_size)
                    journal.flush()
                    os.fsync(journal.fileno())
            except OSError as rollback_error:
                raise RuntimeError(
                    f"failed to append and roll back {description}: {journal_path}"
                ) from rollback_error
            raise RuntimeError(
                f"failed to append {description}: {journal_path}"
            ) from error
        return bytes_written


__all__ = ["append_jsonl_records", "locked_jsonl"]
