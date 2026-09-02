"""Cooperative cancellation and safe retirement for Qt worker threads."""

from __future__ import annotations

import time
from typing import Protocol

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QThread, Qt


class CooperativeThread(Protocol):
    """Worker contract used by long-running FinishReview Qt tasks."""

    def request_stop(self) -> None: ...

    def isRunning(self) -> bool: ...

    def wait(self, milliseconds: int) -> bool: ...


_ACTIVE_THREADS: set[QThread] = set()
_RETIRED_THREADS: set[QThread] = set()


def _untrack_qthread(worker: QThread) -> None:
    _ACTIVE_THREADS.discard(worker)


def track_qthread(worker: QThread) -> QThread:
    """Register a cooperative worker for application-wide shutdown."""

    if not isinstance(worker, QThread):
        return worker
    if worker not in _ACTIVE_THREADS:
        _ACTIVE_THREADS.add(worker)
        worker.finished.connect(
            lambda tracked=worker: _untrack_qthread(tracked),
            Qt.DirectConnection,
        )
        worker.destroyed.connect(
            lambda _object=None, tracked=worker: _untrack_qthread(tracked),
            Qt.DirectConnection,
        )
    return worker


def _live_threads(workers: set[QThread]) -> tuple[QThread, ...]:
    deleted = tuple(worker for worker in workers if sip.isdeleted(worker))
    for worker in deleted:
        workers.discard(worker)
        _RETIRED_THREADS.discard(worker)
    return tuple(workers)


def active_thread_count() -> int:
    return len(_live_threads(_ACTIVE_THREADS))


def _dispose_retired_thread(worker: QThread) -> None:
    _RETIRED_THREADS.discard(worker)
    worker.deleteLater()


def retired_thread_count() -> int:
    return len(_RETIRED_THREADS)


def _wait_for_threads(workers: tuple[QThread, ...], timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1_000
    for worker in workers:
        request_stop = getattr(worker, "request_stop", None)
        if callable(request_stop):
            request_stop()
    for worker in workers:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        if worker.isRunning() and remaining_ms:
            worker.wait(remaining_ms)
    return not any(worker.isRunning() for worker in workers)


def wait_for_retired_threads(timeout_ms: int = 2_000) -> bool:
    """Request cancellation and wait up to one shared deadline."""

    workers = _live_threads(_RETIRED_THREADS)
    _wait_for_threads(workers, timeout_ms)
    for worker in workers:
        if not worker.isRunning():
            _dispose_retired_thread(worker)
    return not _RETIRED_THREADS


def wait_for_active_threads(timeout_ms: int = 2_000) -> bool:
    """Cooperatively drain every registered worker during application exit."""

    workers = tuple(
        set(_live_threads(_ACTIVE_THREADS)) | set(_live_threads(_RETIRED_THREADS))
    )
    drained = _wait_for_threads(workers, timeout_ms)
    for worker in tuple(_RETIRED_THREADS):
        if not worker.isRunning():
            _dispose_retired_thread(worker)
    return drained


def install_qthread_shutdown(
    application: QCoreApplication,
    *,
    timeout_ms: int = 2_000,
) -> None:
    """Drain retired workers during the application's normal quit sequence."""

    if application.property("finishreviewQthreadShutdownInstalled"):
        return
    application.setProperty("finishreviewQthreadShutdownInstalled", True)
    application.aboutToQuit.connect(
        lambda: wait_for_active_threads(timeout_ms)
    )


def retire_qthread(worker: QThread) -> None:
    """Keep a running worker alive until cooperative shutdown completes.

    Native decoder reads cannot always be interrupted immediately. Detaching
    the worker prevents its former window from destroying a running QThread;
    the global pool releases it as soon as ``finished`` is emitted.
    """

    track_qthread(worker)
    if not worker.isRunning():
        worker.deleteLater()
        return
    worker.setParent(None)
    if worker in _RETIRED_THREADS:
        return
    _RETIRED_THREADS.add(worker)
    worker.finished.connect(lambda retired=worker: _dispose_retired_thread(retired))
