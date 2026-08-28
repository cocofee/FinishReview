from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_project_and_runtime_versions_match_release_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    runtime_init = (ROOT / "realtime" / "__init__.py").read_text(
        encoding="utf-8"
    )

    project_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    runtime_version = re.search(
        r'^__version__ = "([^"]+)"$', runtime_init, re.MULTILINE
    )

    assert project_version is not None
    assert runtime_version is not None
    assert project_version.group(1) == "0.2.0"
    assert runtime_version.group(1) == project_version.group(1)


def test_windows_executable_version_matches_release_version():
    version_info = (ROOT / "packaging" / "version_info.txt").read_text(
        encoding="utf-8"
    )
    spec = (ROOT / "packaging" / "FinishReview.spec").read_text(
        encoding="utf-8"
    )

    assert 'version=str(ROOT / "packaging" / "version_info.txt")' in spec
    assert 'StringStruct("FileVersion", "0.2.0")' in version_info
    assert 'StringStruct("ProductVersion", "0.2.0")' in version_info
    assert "filevers=(0, 2, 0, 0)" in version_info
    assert "prodvers=(0, 2, 0, 0)" in version_info


def test_finish_review_package_excludes_detection_and_ocr_runtimes():
    spec = (ROOT / "packaging" / "FinishReview.spec").read_text(encoding="utf-8")

    assert 'ROOT / "realtime" / "review_main.py"' in spec
    assert 'name="FinishReviewConsole"' in spec
    assert "FINISH_REVIEW_FFMPEG" in spec
    assert "collect_data_files" not in spec
    for package in (
        "ultralytics",
        "torch",
        "torchvision",
        "paddle",
        "paddleocr",
        "paddlex",
        "onnxruntime",
        "tensorrt",
    ):
        assert f'"{package}"' in spec


def test_finish_review_has_a_dedicated_production_build_script():
    script = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8")

    build_index = script.index("& $ResolvedPython -m PyInstaller")
    assert script.index("$ResolvedFfmpeg =") < build_index
    assert "[string]$PythonPath" in script
    assert 'Join-Path $RepoRoot ".venv\\Scripts\\python.exe"' in script
    assert "Get-Command python" in script
    assert 'Write-Host "Build Python: $ResolvedPython"' in script
    assert '"FinishReview.spec"' in script
    assert '$AppName = "FinishReviewConsole"' in script
    assert "Stage-DistributionInput -InputPath $ResolvedFfmpeg" in script
    assert "$env:FINISH_REVIEW_FFMPEG = $ResolvedFfmpeg" in script
    assert 'assert_clean_distribution.ps1") -AppDir $AppDir' in script


def test_distribution_check_rejects_all_runtime_state_paths():
    clean_check = (
        ROOT / "packaging" / "assert_clean_distribution.ps1"
    ).read_text(encoding="utf-8")

    for runtime_path in (
        "RaceData",
        "logs",
        "config.json",
        "finish_review_config.json",
        "global_config.json",
    ):
        assert f'"{runtime_path}"' in clean_check
    assert "Distribution contains runtime state" in clean_check


def test_distribution_check_rejects_known_non_project_dependencies():
    clean_check = (
        ROOT / "packaging" / "assert_clean_distribution.ps1"
    ).read_text(encoding="utf-8")

    for dependency_name in (
        "coverage",
        "psutil",
        "pyreadline3",
        "ruff",
        "tomli",
    ):
        assert f'"{dependency_name}"' in clean_check
    assert "Distribution contains unexpected dependencies" in clean_check


def test_runtime_modules_use_package_relative_imports():
    modules = (
        "auyat_rgb.py",
        "external_clip_import.py",
        "passage_receiver.py",
        "passage_review.py",
        "point_playback.py",
        "racetiger_source.py",
        "review_recorder.py",
        "review_window.py",
        "stream_recorder.py",
    )

    for module_name in modules:
        source = (ROOT / "realtime" / module_name).read_text(encoding="utf-8")
        assert "except ImportError:" not in source, module_name
