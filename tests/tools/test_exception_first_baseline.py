import json

import pytest

from tools.exception_first_baseline import main, synthetic_gaps


def test_synthetic_probe_reports_expectation_separately_from_observation():
    report = synthetic_gaps()
    assert report["eight_chips_one_crossing"]["expected_main_pending"] == 1
    assert isinstance(report["eight_chips_one_crossing"]["main_pending"], int)
    assert "expected" in report["following_crossing_500ms"]


def test_baseline_refuses_to_write_inside_source(tmp_path):
    with pytest.raises(SystemExit):
        main(["--workspace", str(tmp_path), "--output", str(tmp_path / "report.json")])
    assert not (tmp_path / "report.json").exists()


def test_baseline_writes_only_independent_output(tmp_path, monkeypatch):
    from tools import exception_first_baseline as baseline

    source = tmp_path / "source"
    source.mkdir()
    marker = source / "marker.json"
    marker.write_text("original", encoding="utf-8")
    monkeypatch.setattr(baseline, "inspect_workspace", lambda *_args, **_kw: {"scope": "test"})
    output = tmp_path / "validation" / "report.json"
    assert main(["--workspace", str(source), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["preflight"] == {"scope": "test"}
    assert marker.read_text(encoding="utf-8") == "original"
