"""Bounded asynchronous session preparation, barriers and shutdown operations.

The controller never owns a QWidget. Callers apply completed service contexts
on the GUI thread; a failed preparation leaves the active context untouched.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
import threading


class SessionPhase(str, Enum):
    ACTIVE = "active"
    PREPARING = "preparing"
    DRAINING = "draining"
    ACTIVATING = "activating"
    CLOSING = "closing"


class SessionController:
    def __init__(self):
        self.context = None
        self.generation = 0
        self.phase = SessionPhase.ACTIVE
        self._lock = threading.Lock()
        self._busy = False
        self._transition = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session")

    def submit(self, operation, *, phase=SessionPhase.PREPARING) -> Future:
        with self._lock:
            if self._busy or self._closed:
                future = Future()
                future.set_exception(RuntimeError("赛事切换正在进行"))
                return future
            self._busy = True
            self.phase = phase
        def run():
            try:
                return operation()
            finally:
                with self._lock:
                    self._busy = False
        return self._executor.submit(run)

    def begin_transition(self) -> bool:
        with self._lock:
            if self._transition or self._closed:
                return False
            self._transition = True
            self.phase = SessionPhase.PREPARING
            return True

    def drain_events(self, ingestion) -> Future:
        self.phase = SessionPhase.DRAINING
        return ingestion.close()

    def drain_evidence(self, worker, generation: int) -> Future:
        self.phase = SessionPhase.DRAINING
        worker.invalidate(generation)
        # Explicit FIFO tasks are durable work. Invalidating scans alone is
        # insufficient: this marker executes after all accepted explicit tasks.
        return worker.submit_task(lambda: None)

    def activate(self, context):
        self.phase = SessionPhase.ACTIVATING
        self.context = context
        self.generation = context.snapshot.generation
        self.phase = SessionPhase.ACTIVE

    def reset(self):
        self._transition = False
        self.phase = SessionPhase.ACTIVE

    def shutdown(self):
        self._closed = True
        self.phase = SessionPhase.CLOSING
        self._executor.shutdown(wait=False)
