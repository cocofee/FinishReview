"""Bounded in-process latency metrics for field diagnostics."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
from typing import Iterator


@dataclass(frozen=True, slots=True)
class RuntimeMetricSnapshot:
    operation: str
    count: int
    item_count: int
    failure_count: int
    last_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass(slots=True)
class _MetricState:
    samples_ms: deque[float]
    count: int = 0
    item_count: int = 0
    failure_count: int = 0
    last_ms: float = 0.0
    max_ms: float = 0.0


class RuntimeMetrics:
    """Collect bounded, low-cardinality timing summaries without dependencies."""

    def __init__(self, *, sample_limit: int = 256):
        self._sample_limit = max(1, int(sample_limit))
        self._lock = threading.RLock()
        self._states: dict[str, _MetricState] = {}

    @staticmethod
    def _percentile(samples: tuple[float, ...], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = round((len(ordered) - 1) * float(percentile))
        return ordered[max(0, min(index, len(ordered) - 1))]

    @staticmethod
    def _operation_name(operation: str) -> str:
        value = str(operation).strip()
        if not value:
            raise ValueError("metric operation is required")
        return value

    def observe(
        self,
        operation: str,
        duration_ms: float,
        *,
        item_count: int = 0,
        failed: bool = False,
    ) -> int:
        name = self._operation_name(operation)
        duration = float(duration_ms)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("metric duration must be non-negative and finite")
        with self._lock:
            state = self._states.get(name)
            if state is None:
                state = _MetricState(deque(maxlen=self._sample_limit))
                self._states[name] = state
            state.samples_ms.append(duration)
            state.count += 1
            state.item_count += max(0, int(item_count))
            state.failure_count += int(bool(failed))
            state.last_ms = duration
            state.max_ms = max(state.max_ms, duration)
            return state.count

    @contextmanager
    def measure(
        self,
        operation: str,
        *,
        item_count: int = 0,
    ) -> Iterator[None]:
        started = time.perf_counter()
        failed = False
        try:
            yield
        except Exception:
            failed = True
            raise
        finally:
            self.observe(
                operation,
                (time.perf_counter() - started) * 1000.0,
                item_count=item_count,
                failed=failed,
            )

    def snapshot(self, operation: str) -> RuntimeMetricSnapshot:
        name = self._operation_name(operation)
        with self._lock:
            state = self._states.get(name)
            if state is None:
                return RuntimeMetricSnapshot(name, 0, 0, 0, 0.0, 0.0, 0.0, 0.0)
            return self._snapshot_unlocked(name, state)

    def snapshots(self) -> tuple[RuntimeMetricSnapshot, ...]:
        with self._lock:
            return tuple(
                self._snapshot_unlocked(name, self._states[name])
                for name in sorted(self._states)
            )

    def _snapshot_unlocked(
        self,
        operation: str,
        state: _MetricState,
    ) -> RuntimeMetricSnapshot:
        samples = tuple(state.samples_ms)
        return RuntimeMetricSnapshot(
            operation=operation,
            count=state.count,
            item_count=state.item_count,
            failure_count=state.failure_count,
            last_ms=state.last_ms,
            p50_ms=self._percentile(samples, 0.50),
            p95_ms=self._percentile(samples, 0.95),
            max_ms=state.max_ms,
        )


__all__ = ["RuntimeMetricSnapshot", "RuntimeMetrics"]
