from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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

    build_index = script.index("python -m PyInstaller")
    assert script.index("$ResolvedFfmpeg =") < build_index
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
