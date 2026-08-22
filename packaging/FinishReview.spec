# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parent
binaries = []

ffmpeg_value = os.environ.get("FINISH_REVIEW_FFMPEG") or shutil.which("ffmpeg")
ffmpeg_path = Path(ffmpeg_value).expanduser().resolve() if ffmpeg_value else None
if ffmpeg_path is None or not ffmpeg_path.is_file():
    raise SystemExit("Required FFmpeg executable is missing; set FINISH_REVIEW_FFMPEG")
binaries.append((str(ffmpeg_path), "."))

a = Analysis(
    [str(ROOT / "realtime" / "review_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "modelscope",
        "onnxruntime",
        "openvino",
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FinishReviewConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FinishReviewConsole",
)
