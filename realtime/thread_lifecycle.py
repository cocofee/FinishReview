"""Cooperative cancellation and safe retirement for Qt worker threads."""

from __future__ import annotations

import time
from typing import Protocol

from PyQt5.QtCore import QCoreApplication, QThread


class CooperativeThread(Protocol):
    """Worker contract used by long-running FinishReview Qt tasks."""

    def request_stop(self) -> None: ...

    def isRunning(self) -> bool: ...

    def wait(self, milliseconds: int) -> bool: ...


_RETIRED_THREADS: set[QThread] = set()


def _dispose_retired_thread(worker: QThread) -> None:
    _RETIRED_THREADS.discard(worker)
    worker.deleteLater()


def retired_thread_count() -> int:
    return len(_RETIRED_THREADS)


def wait_for_retired_threads(timeout_ms: int = 2_000) -> bool:
    """Request cancellation and wait up to one shared deadline."""

    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1_000
    for worker in tuple(_RETIRED_THREADS):
        request_stop = getattr(worker, "request_stop", None)
        if callable(request_stop):
            request_stop()
    for worker in tuple(_RETIRED_THREADS):
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        if worker.isRunning() and remaining_ms:
            worker.wait(remaining_ms)
        if not worker.isRunning():
            _dispose_retired_thread(worker)
    return not _RETIRED_THREADS


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
        lambda: wait_for_retired_threads(timeout_ms)
    )


def retire_qthread(worker: QThread) -> None:
    """Keep a running worker alive until cooperative shutdown completes.

    Native decoder reads cannot always be interrupted immediately. Detaching
    the worker prevents its former window from destroying a running QThread;
    the global pool releases it as soon as ``finished`` is emitted.
    """

    if not worker.isRunning():
        worker.deleteLater()
        return
    worker.setParent(None)
    if worker in _RETIRED_THREADS:
        return
    _RETIRED_THREADS.add(worker)
    worker.finished.connect(lambda retired=worker: _dispose_retired_thread(retired))
