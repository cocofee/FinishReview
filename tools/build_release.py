"""Repeatable Windows packages, isolated smoke tests and build provenance."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from realtime import __version__

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def build(ffmpeg, mode):
    artifacts = ROOT / "artifacts"
    record_dir = artifacts / "build-records" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record_dir.mkdir(parents=True)
    # Generate the Windows resource from the runtime version; verify the project
    # version as well so the two cannot silently diverge between releases.
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if not re.search(r'^version\s*=\s*"' + re.escape(__version__) + r'"\s*$', project, re.M):
        raise ValueError("Project and runtime versions differ")
    parts = tuple(int(part) for part in __version__.split("."))
    numeric = (*parts, *(0 for _ in range(4 - len(parts))))
    template = (ROOT / "packaging/version_info.txt").read_text(encoding="utf-8")
    template = re.sub(r'(filevers|prodvers)=\([^)]*\)', lambda m: f"{m[1]}={numeric!r}", template)
    template = re.sub(r'(StringStruct\("(?:FileVersion|ProductVersion)", )"[^"]+"',
                      lambda m: m[1] + repr(__version__), template)
    version_file = record_dir / "version_info.txt"
    version_file.write_text(template, encoding="utf-8")
    source_paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    sources = {name: digest(ROOT / name) for name in source_paths if name and (ROOT / name).is_file()}
    record = {
        "started_utc": datetime.now(timezone.utc).isoformat(), "version": __version__,
        "commit": git("rev-parse", "HEAD"), "working_tree": git("status", "--short"),
        "source_sha256": sources, "python": sys.version, "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in
                         ("PyInstaller", "pyinstaller-hooks-contrib", "PyQt5", "PyQt5-Qt5", "numpy", "opencv-python")},
        "ffmpeg_sha256": digest(ffmpeg), "packages": [], "status": "building",
        "ffmpeg_version": subprocess.check_output([str(ffmpeg), "-version"]).decode("utf-8", "replace").splitlines()[0],
    }
    environment = dict(os.environ, FINISH_REVIEW_FFMPEG=str(ffmpeg),
                       FINISH_REVIEW_VERSION_INFO=str(version_file), PYTHONUTF8="1")
    try:
        for kind in (("onedir", "onefile") if mode == "both" else (mode,)):
            destination = artifacts / "dist"
            if kind == "onefile":
                destination /= "onefile"
            command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                       "--distpath", str(destination), "--workpath", str(artifacts / "build" / kind),
                       str(ROOT / "packaging/FinishReview.spec")]
            log = record_dir / f"{kind}.log"
            print(f"Building {kind}; log: {log}", flush=True)
            with log.open("w", encoding="utf-8") as stream:
                subprocess.run(command, cwd=ROOT, env=dict(environment, FINISH_REVIEW_BUILD_MODE=kind),
                               stdout=stream, stderr=subprocess.STDOUT, check=True)
            executable = destination / ("FinishReviewConsole.exe" if kind == "onefile"
                                         else "FinishReviewConsole/FinishReviewConsole.exe")
            from PyInstaller.utils.win32.versioninfo import read_version_info_from_executable
            resource = read_version_info_from_executable(str(executable))
            strings = {item.name: item.val for child in resource.kids
                       for table in getattr(child, "kids", []) for item in getattr(table, "kids", [])
                       if hasattr(item, "name") and hasattr(item, "val")}
            if any(strings.get(key) != __version__ for key in ("FileVersion", "ProductVersion")):
                raise ValueError("Packaged Windows string version mismatch")
            version_words = (numeric[0] << 16 | numeric[1], numeric[2] << 16 | numeric[3])
            if ((resource.ffi.fileVersionMS, resource.ffi.fileVersionLS) != version_words
                    or (resource.ffi.productVersionMS, resource.ffi.productVersionLS) != version_words):
                raise ValueError("Packaged Windows numeric version mismatch")
            archive = None
            if kind == "onedir":
                subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(ROOT / "packaging/assert_clean_distribution.ps1"),
                                "-AppDir", str(executable.parent)], check=True)
                archive = destination / "FinishReviewConsole.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                    for path in sorted(executable.parent.rglob("*")):
                        if path.is_file():
                            zipped.write(path, path.relative_to(destination))
            # Clear all inherited Qt/Python lookup overrides: packages must find
            # their own libraries and launch from a directory outside the repo.
            smoke_environment = {key: value for key, value in os.environ.items()
                                 if not key.startswith(("QT_", "PYTHON", "FINISH_REVIEW_"))}
            with tempfile.TemporaryDirectory(prefix="finishreview-smoke-") as temporary:
                isolated = Path(temporary)
                if archive is not None:
                    with zipfile.ZipFile(archive) as zipped:
                        zipped.extractall(isolated)
                    smoke_executable = isolated / "FinishReviewConsole/FinishReviewConsole.exe"
                else:
                    smoke_executable = isolated / executable.name
                    shutil.copy2(executable, smoke_executable)
                local_data = isolated / "localappdata"
                local_data.mkdir()
                smoke_environment["LOCALAPPDATA"] = str(local_data)
                smoke_environment["PATH"] = ""
                smoke_cases = []
                for qt_platform in ("offscreen", "windows"):
                    for override in ("unset", "missing"):
                        case_env = dict(smoke_environment, QT_QPA_PLATFORM=qt_platform)
                        if override == "missing":
                            case_env["FINISH_REVIEW_FFMPEG"] = str(isolated / "removed" / "ffmpeg.exe")
                        subprocess.run([str(smoke_executable), "--smoke-test"], cwd=temporary,
                                       env=case_env, timeout=60, check=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        smoke_cases.append({"platform": qt_platform, "ffmpeg_override": override,
                                            "path": "", "exit_code": 0})
                for log_file in local_data.rglob("*.log"):
                    shutil.copy2(log_file, record_dir / f"{kind}-{log_file.name}")
            files = ([executable] if kind == "onefile" else
                     sorted(path for path in executable.parent.rglob("*") if path.is_file()))
            checksums = {str(path.relative_to(destination)): digest(path) for path in files}
            if archive is not None:
                checksums[archive.name] = digest(archive)
            (record_dir / f"{kind}-sha256.txt").write_text(
                "".join(f"{value}  {name}\n" for name, value in checksums.items()), encoding="utf-8")
            record["packages"].append({"mode": kind, "executable": str(executable),
                                       "sha256": digest(executable), "smoke_exit_code": 0,
                                       "smoke_platforms": ["offscreen", "windows"],
                                       "ffmpeg_smoke_cases": smoke_cases,
                                       "files_sha256": checksums, "command": command})
        record["status"] = "passed"
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        raise
    finally:
        record["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (record_dir / "build.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Build record: {record_dir / 'build.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--mode", choices=("onedir", "onefile", "both"), default="onedir")
    args = parser.parse_args()
    build(args.ffmpeg.resolve(strict=True), args.mode)
