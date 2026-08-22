from pathlib import Path

from realtime.runtime_paths import (
    application_dir,
    resolve_output_dir,
    resolve_runtime_path,
    resolve_source,
)


def test_application_dir_uses_executable_parent_when_frozen(tmp_path):
    executable = tmp_path / "FinishReviewConsole.exe"

    assert application_dir(frozen=True, executable=str(executable)) == tmp_path.resolve()


def test_relative_runtime_paths_are_based_on_application_dir(tmp_path):
    assert resolve_runtime_path("best.pt", base_dir=tmp_path) == (tmp_path / "best.pt").resolve()
    assert resolve_output_dir("RaceData", base_dir=tmp_path) == (tmp_path / "RaceData").resolve()


def test_resolve_source_only_rebases_local_files(tmp_path):
    assert resolve_source("test.mp4", base_dir=tmp_path) == str((tmp_path / "test.mp4").resolve())
    assert resolve_source("0", base_dir=tmp_path) == "0"
    assert resolve_source("rtsp://camera/live", base_dir=tmp_path) == "rtsp://camera/live"
