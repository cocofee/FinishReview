# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from pathlib import Path
from PyInstaller.utils.hooks.qt import pyqt5_library_info


ROOT = Path(SPECPATH).resolve().parent
# Some Windows PyQt5 wheels encode their embedded qt.conf prefix through the
# system code page. Recover paths from the installed wheel when that prefix is
# lossy (e.g. a Chinese user directory); never modify the user's installation.
qt_info = pyqt5_library_info
qt_root = Path(qt_info.package_location) / "Qt5"
if not Path(qt_info.location["PluginsPath"]).is_dir() and (qt_root / "plugins").is_dir():
    old_prefix = qt_info.location["PrefixPath"]
    for key, value in tuple(qt_info.location.items()):
        if value.startswith(old_prefix):
            qt_info.location[key] = str(qt_root) + value[len(old_prefix):]
    qt_info.qt_inside_package = True
    qt_info.qt_lib_dir = (qt_root / "bin").resolve()

onefile = os.environ.get("FINISH_REVIEW_BUILD_MODE") == "onefile"
binaries = []
icon_path = ROOT / "assets" / "finishreview.ico"
if not icon_path.is_file():
    raise SystemExit(f"Required application icon is missing: {icon_path}")

ffmpeg_value = os.environ.get("FINISH_REVIEW_FFMPEG") or shutil.which("ffmpeg")
ffmpeg_path = Path(ffmpeg_value).expanduser().resolve() if ffmpeg_value else None
if ffmpeg_path is None or not ffmpeg_path.is_file():
    raise SystemExit("Required FFmpeg executable is missing; set FINISH_REVIEW_FFMPEG")
binaries.append((str(ffmpeg_path), "."))

a = Analysis(
    [str(ROOT / "realtime" / "review_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[(str(icon_path), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "modelscope",
        "onnxruntime",
        "openvino",
        "psutil",
        "pyreadline3",
        "paddle",
        "paddleocr",
        "paddlex",
        "rapidocr_onnxruntime",
        "tensorrt",
        "torch",
        "torchvision",
        "transformers",
        "ultralytics",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
for required_plugin in ("qwindows.dll", "qoffscreen.dll"):
    if not any(Path(entry[0]).name == required_plugin for entry in a.binaries):
        raise SystemExit(f"Required Qt platform plugin was not collected: {required_plugin}")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if onefile else [],
    a.datas if onefile else [],
    exclude_binaries=not onefile,
    name="FinishReviewConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_path),
    disable_windowed_traceback=False,
    argv_emulation=False,
    version=os.environ.get("FINISH_REVIEW_VERSION_INFO", str(ROOT / "packaging" / "version_info.txt")),
)
if not onefile:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="FinishReviewConsole",
    )
