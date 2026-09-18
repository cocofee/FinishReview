"""Measure event-loop stalls separately from background operation latency."""

import time
from PyQt5.QtCore import QObject, QTimer


class UiLatencyProbe(QObject):
    def __init__(self, metrics, parent=None, *, interval_ms=50):
        super().__init__(parent)
        self.metrics = metrics
        self.interval_ms = interval_ms
        self.previous = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        now = time.perf_counter()
        self.metrics.observe("ui_event_loop_delay", max(0.0, (now - self.previous) * 1000 - self.interval_ms))
        self.previous = now

    def stop(self):
        self.timer.stop()
