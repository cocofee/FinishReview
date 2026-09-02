"""Shared pytest lifecycle guards for Qt tests."""

from __future__ import annotations

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent, QThread  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def dispose_qt_top_level_widgets_after_test():
    """Release windows and native Qt resources before the next test.

    QWidget.close() only hides a top-level widget unless delete-on-close is set.
    The GUI suites create hundreds of dialogs in one process, so relying on
    Python's nondeterministic collection can exhaust Windows desktop/native
    resources and terminate pytest without a Python traceback.
    """

    yield

    app = QApplication.instance()
    if app is not None:
        widgets = tuple(app.topLevelWidgets())
        threads = tuple(
            thread
            for widget in widgets
            for thread in widget.findChildren(QThread)
        )
        for widget in widgets:
            widget.close()
        # Production keeps interaction responsive by retiring filmstrip and
        # activity workers asynchronously. Tests must not let those workers
        # outlive their temporary media directories or overlap the next test.
        for thread in threads:
            if thread.isRunning():
                thread.wait(2_000)
        for widget in widgets:
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    gc.collect()
