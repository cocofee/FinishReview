from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import QApplication

from realtime.thread_lifecycle import (
    retired_thread_count,
    retire_qthread,
    wait_for_retired_threads,
)


class _CooperativeWorker(QThread):
    def __init__(self):
        super().__init__()
        self.delete_later_called = False

    def request_stop(self):
        self.requestInterruption()

    def run(self):
        while not self.isInterruptionRequested():
            self.msleep(1)

    def deleteLater(self):
        self.delete_later_called = True
        super().deleteLater()


def test_retired_worker_is_drained_and_removed():
    app = QApplication.instance() or QApplication([])
    worker = _CooperativeWorker()
    worker.start()
    assert worker.wait(1) is False

    retire_qthread(worker)

    assert retired_thread_count() >= 1
    assert wait_for_retired_threads(1_000)
    app.processEvents()
    assert retired_thread_count() == 0
    assert not worker.isRunning()
    assert worker.delete_later_called
