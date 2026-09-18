"""Responsive modal waits for explicit disk preparation and lifecycle barriers."""

from PyQt5.QtCore import QEventLoop, QTimer


def wait_for_background(future, parent):
    """Keep Qt delivery/painting alive while preventing reentrant user actions.

    Disk work is owned by CaptureRefreshWorker. This is used only at explicit
    modal playback and lifecycle boundaries, never for periodic refreshes.
    """
    enabled = parent.isEnabled()
    parent.setEnabled(False)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if future.done() else None)
    try:
        if not future.done():
            timer.start()
            loop.exec_()
        return future.result()
    finally:
        timer.stop()
        parent.setEnabled(enabled)
