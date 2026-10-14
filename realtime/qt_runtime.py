"""Repair Qt's plugin lookup when its compiled path loses Unicode characters."""

from pathlib import Path


def configure_qt_plugins():
    import PyQt5
    from PyQt5.QtCore import QCoreApplication, QLibraryInfo

    reported = Path(QLibraryInfo.location(QLibraryInfo.PluginsPath))
    if (reported / "platforms").is_dir():
        return
    # QLibraryInfo can report C:/Users/??/... on Windows while PyQt5.__file__
    # retains the real Unicode path. Keep all existing Qt search paths.
    for name in ("Qt5", "Qt"):
        actual = Path(PyQt5.__file__).resolve().parent / name / "plugins"
        if (actual / "platforms").is_dir():
            QCoreApplication.addLibraryPath(str(actual))
            return
