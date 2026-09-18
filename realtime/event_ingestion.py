"""Ordered projection of durable receiver events, independent of media work.

The inbox remains the recovery journal. Overflow/failure recovery projects the
latest revision (including tombstones); intermediate audit revisions remain in
the inbox. No unbounded retry list and no thread per passage.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import threading
import time
from typing import Callable

from .passage_receiver import PassageEvent, PassageEventStore


@dataclass(frozen=True, slots=True)
class IngestionContext:
    generation: int
    provider: str
    race_id: str
    source: PassageEventStore
    target: PassageEventStore


@dataclass(frozen=True, slots=True)
class EventCommit:
    generation: int
    journal: str
    event: PassageEvent
    durable_at: float
    committed_at: float
    recovered: bool = False


class EventIngestion:
    def __init__(self, context: IngestionContext, on_ready: Callable[["EventIngestion"], None],
                 on_error: Callable[[str], None], *, capacity: int = 256,
                 retry_seconds: float = 1.0, known_revisions: dict[str, int] | None = None):
        self.context = context
        self.on_ready = on_ready
        self.on_error = on_error
        self.capacity = max(1, int(capacity))
        self.retry_seconds = max(.01, float(retry_seconds))
        self._condition = threading.Condition()
        self._queue = deque()
        self._dirty = False
        self._recovering = False
        self._active = False
        self._closing = False
        self._closed = Future()
        self._delivered: dict[str, int] = dict(known_revisions or {})
        self._completed = deque()
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="event-ingestion", daemon=True)
        self._thread.start()

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def idle(self) -> bool:
        with self._condition:
            return not (self._queue or self._dirty or self._active or self._recovering)

    def take_completed(self) -> tuple[EventCommit, ...]:
        """Consume a bounded mailbox; only its empty-to-nonempty edge signals Qt."""
        with self._condition:
            values = tuple(self._completed)
            self._completed.clear()
            self._condition.notify_all()
            return values

    def _report_error(self, error: str):
        if error != self._last_error:
            self._last_error = error
            self.on_error(error)

    def submit(self, event: PassageEvent) -> bool:
        """Nonblocking notification; False means replay from the durable inbox."""
        with self._condition:
            if self._closing:
                return False
            if self.context.race_id and event.race_id != self.context.race_id:
                return False
            if self._dirty or self._recovering or len(self._queue) >= self.capacity:
                self._dirty = True
                queued = False
            else:
                self._queue.append(event)
                queued = True
            self._condition.notify_all()
            return queued

    def recover(self) -> None:
        with self._condition:
            if not self._closing:
                self._dirty = True
                self._condition.notify_all()

    def close(self) -> Future:
        """Drain writes without requiring a live UI consumer.

        Closing may evict old UI notifications from the bounded mailbox. The
        committed store (and the inbox on failure) remains the recovery source.
        """
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        return self._closed

    def _project(self, event: PassageEvent, *, recovered=False):
        context = self.context
        if context.race_id and event.race_id != context.race_id:
            return
        # RaceTiger (and an inbox that is also the target) is already committed.
        if context.source.journal_path != context.target.journal_path:
            context.target.append(event)
        committed_store = (context.source if context.source.journal_path == context.target.journal_path
                           else context.target)
        current = committed_store.get(event.event_id)
        if current is None or current.revision != event.revision:
            return
        if self._delivered.get(event.event_id, 0) >= event.revision:
            return
        commit = EventCommit(
            context.generation, str(context.target.journal_path), current,
            event.durable_monotonic if not recovered else 0.0,
            time.perf_counter(), recovered,
        )
        with self._condition:
            while len(self._completed) >= self.capacity and not self._closing:
                self._condition.wait()
            if len(self._completed) >= self.capacity:
                self._completed.popleft()
            notify = not self._completed
            self._completed.append(commit)
        if notify:
            self.on_ready(self)
        self._delivered[event.event_id] = event.revision

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._dirty and not self._closing:
                        self._condition.wait()
                    if not self._queue and not self._dirty and self._closing:
                        break
                    event = self._queue[0] if self._queue else None
                    recovering = event is None
                    self._active = True
                    if recovering:
                        self._dirty = False
                        self._recovering = True
                try:
                    if recovering:
                        for item in self.context.source.events(include_inactive=True):
                            self._project(item, recovered=True)
                    else:
                        self._project(event)
                    self._report_error("")
                    with self._condition:
                        if event is not None:
                            self._queue.popleft()
                except Exception as error:
                    self._report_error(str(error))
                    with self._condition:
                        self._dirty = True
                        if self._closing:
                            raise
                        self._condition.wait(timeout=self.retry_seconds)
                finally:
                    with self._condition:
                        self._active = False
                        self._recovering = False
                        self._condition.notify_all()
        except Exception as error:
            self._closed.set_exception(error)
        else:
            self._closed.set_result(None)


def merge_events(target: PassageEventStore, sources: tuple[PassageEventStore, ...],
                 race_id: str) -> int:
    """Rebuild the latest projection without resurrecting withdrawn passages."""
    merged = 0
    newest = {}
    for store in (target, *sources):
        for event in store.events(include_inactive=True):
            if event.race_id == race_id:
                current = newest.get(event.event_id)
                if current is None or event.revision > current.revision:
                    newest[event.event_id] = event
    for event in newest.values():
        current = target.get(event.event_id)
        if current is None or current.revision < event.revision:
            target.append(event)
            merged += 1
    return merged
