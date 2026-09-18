"""Synthetic full-window I/O latency probe, usable against v0.24.8 and later.

No camera or production data: temporary JSONL workspaces, offscreen Qt paints.
Run with ``python -m tools.benchmark_session_io --output result.json``.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from realtime.passage_receiver import PassageEvent
from realtime.race_metadata import RaceMetadata, RaceMetadataStore
import realtime.review_window as module


def summary(values):
    ordered = sorted(values)
    return {"samples_ms": values, "p50_ms": ordered[len(ordered) // 2],
            "p95_ms": ordered[round((len(ordered) - 1) * .95)], "max_ms": max(ordered)}


def run(output: Path, iterations: int, delay: float):
    app = QApplication.instance() or QApplication([])
    samples = {key: [] for key in ("durable_to_commit", "commit_to_visible", "durable_to_visible",
                                   "callback", "open_archive", "return_live", "gui_gap",
                                   "gui_gap_ingestion", "gui_gap_open_archive", "gui_gap_return_live")}
    phase = ["ingestion"]
    with tempfile.TemporaryDirectory(prefix="finishreview-session-bench-") as temporary:
        root = Path(temporary)
        window = module.FinishReviewWindow("", root, passage_batch_interval_ms=150)
        window.show()
        live = RaceMetadata(race_id="live", stage_id="finish", revision=1, emitted_at_ms=1)
        window._receiver_metadata_store.store(live)
        window._activate_cyclerace_workspace(live)
        history = root / "history"
        RaceMetadataStore(history / "cyclerace_race_metadata.json").store(
            replace(live, race_id="history"))
        last = [time.perf_counter()]
        timer = QTimer()
        timer.setInterval(10)
        def tick():
            now = time.perf_counter()
            samples["gui_gap"].append((now - last[0]) * 1000)
            samples["gui_gap_" + phase[0]].append((now - last[0]) * 1000)
            last[0] = now
        timer.timeout.connect(tick)
        timer.start()
        def pump_until(predicate):
            deadline = time.monotonic() + 15
            while not predicate():
                app.processEvents()
                if time.monotonic() > deadline:
                    raise TimeoutError("benchmark operation did not complete")
                time.sleep(.001)
        append = window.passage_store.append
        committed = {}
        def slow_append(event):
            time.sleep(delay)
            result = append(event)
            committed[event.event_id] = time.perf_counter()
            return result
        window.passage_store.append = slow_append
        for index in range(iterations):
            event = PassageEvent(event_id=f"e-{index}", race_id="live", stage_id="finish",
                                 group_id="g", sequence=index + 1, bib=str(index + 1),
                                 received_at_ms=int(time.time() * 1000))
            window._receiver_passage_store.append(event)
            event = window._receiver_passage_store.get(event.event_id)
            durable = time.perf_counter()
            window._on_passage_received(event)
            samples["callback"].append((time.perf_counter() - durable) * 1000)
            pump_until(lambda: window.table.rowCount() == index + 1)
            visible = time.perf_counter()
            samples["durable_to_commit"].append((committed[event.event_id] - durable) * 1000)
            samples["commit_to_visible"].append((visible - committed[event.event_id]) * 1000)
            samples["durable_to_visible"].append((visible - durable) * 1000)
        window.passage_store.append = append
        # Delay passage-log loading, and accepted explicit evidence work.
        original_load = module.PassageEventStore._load_existing
        def slow_load(store):
            time.sleep(delay)
            return original_load(store)
        module.PassageEventStore._load_existing = slow_load
        try:
            for _ in range(iterations):
                phase[0] = "open_archive"
                window._capture_refresh_worker.submit_task(lambda: time.sleep(delay))
                started = time.perf_counter()
                assert window._open_saved_event_workspace(history)
                samples["open_archive"].append((time.perf_counter() - started) * 1000)
                app.processEvents()
                phase[0] = "return_live"
                started = time.perf_counter()
                assert window._return_to_live_event()
                samples["return_live"].append((time.perf_counter() - started) * 1000)
                app.processEvents()
        finally:
            module.PassageEventStore._load_existing = original_load
            timer.stop()
            window.close()
            app.processEvents()
    result = {"sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path("realtime").glob("*.py")},
              "platform": platform.platform(), "processor": platform.processor(),
              "logical_cpus": os.cpu_count(), "python": platform.python_version(),
              "injected_delay_ms": delay * 1000, "iterations": iterations,
              "media": None, "real_recording": False, "render": "Qt offscreen main window",
              "cache": "OS cache not cleared; temporary test JSONL; no media decoding",
              "failures": 0, "metrics": {key: summary(value) for key, value in samples.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--delay", type=float, default=.2)
    args = parser.parse_args()
    run(args.output, args.iterations, args.delay)
