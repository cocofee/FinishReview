import threading

import pytest

from realtime.runtime_metrics import RuntimeMetrics


def test_runtime_metrics_keeps_bounded_latency_summary():
    metrics = RuntimeMetrics(sample_limit=3)

    for value in (10, 20, 30, 40):
        metrics.observe("operation", value, item_count=2)

    summary = metrics.snapshot("operation")
    assert summary.count == 4
    assert summary.item_count == 8
    assert summary.failure_count == 0
    assert summary.p50_ms == 30
    assert summary.p95_ms == 40
    assert summary.max_ms == 40


def test_runtime_metrics_measure_records_failure_and_reraises():
    metrics = RuntimeMetrics()

    with pytest.raises(RuntimeError, match="boom"):
        with metrics.measure("operation"):
            raise RuntimeError("boom")

    summary = metrics.snapshot("operation")
    assert summary.count == 1
    assert summary.failure_count == 1


def test_runtime_metrics_is_safe_for_concurrent_observers():
    metrics = RuntimeMetrics()

    def observe_many() -> None:
        for _ in range(100):
            metrics.observe("operation", 1, item_count=1)

    threads = [threading.Thread(target=observe_many) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    summary = metrics.snapshot("operation")
    assert summary.count == 400
    assert summary.item_count == 400
