import json
import os
from pathlib import Path

import pytest

from tools.live_performance_baseline import (
    _LiveSampleStats,
    build_report,
    load_samples,
    main,
    parse_log_lines,
    process_snapshot,
    resolve_log_paths,
    summarize_problem_lists,
    summarize_rounds,
    summarize_samples,
)

LOG = "FinishReview.ProblemAnalysis"
WINDOW = "FinishReview"


def line(timestamp, message, *, level="INFO", logger=LOG):
    return f"2026-11-01 {timestamp} {level} {logger} MainThread: {message}"


def synthetic_log():
    return [
        line("10:00:00,000", "FinishReview starting; log=D:\\logs\\finish_review.log", logger=WINDOW),
        line("10:00:05,000", "Recording session started for cameras: 1, 2"),
        line("10:00:09,000", "analysis process starting live=True finalizing=False"),
        line("10:00:10,000", "live batch camera=1 video=6.00s sampling=0.500s analysis=2.000s "
                             "publish=0.100s total=2.600s pending=3"),
        line("10:00:20,000", "live batch camera=1 video=6.00s sampling=0.400s analysis=1.800s "
                             "publish=0.100s total=2.300s pending=1"),
        line("10:00:29,000", "analysis process starting live=False finalizing=True"),
        line("10:00:30,000", "live batch camera=1 video=6.00s sampling=0.300s analysis=1.500s "
                             "publish=0.050s total=1.850s pending=0"),
        line("10:00:31,000", "analysis paused: CPU 91%", level="INFO", logger="FinishReview.AnalysisResources"),
        line("10:00:32,000", "ROI decode unavailable for camera_01.mp4; use full-frame decoder: no ffmpeg"),
        line("10:00:33,000", "sampled camera_01_000.mp4: 100 frames in 1.200 s (config line); "
                             "stages={} save=0.100s size=1280x720 backend=roi"),
        line("10:00:40,000", "Runtime metrics operation=analysis_batch count=3 items_total=9 failures=2 "
                             "p50=12.5ms p95=20.0ms max=31.0ms"),
        line("10:00:59,000", "Recording session stopped"),
        line("10:01:00,000", "FinishReview stopped with exit code 0", logger=WINDOW),
    ]


def test_parse_log_extracts_rounds_sessions_and_exit_metrics():
    parsed = parse_log_lines(synthetic_log())

    assert parsed["log_lines"] == 13
    assert parsed["unparsed_lines"] == 0
    assert parsed["log_span_seconds"] == 60.0
    assert parsed["session_seconds"] == 60.0
    assert len(parsed["sessions"]) == 1
    assert parsed["recording_seconds"] == 54.0
    assert parsed["configured_cameras"] == [1, 2]
    assert parsed["analysis_process_starts"] == [
        {"at": "2026-11-01 10:00:09,000", "live": True, "finalizing": False},
        {"at": "2026-11-01 10:00:29,000", "live": False, "finalizing": True},
    ]
    assert [value["pending_segments"] for value in parsed["rounds"]] == [3, 1, 0]
    assert [value["cumulative_video_seconds"] for value in parsed["rounds"]] == [6.0, 12.0, 18.0]
    assert [value["elapsed_seconds"] for value in parsed["rounds"]] == [0.0, 10.0, 20.0]
    assert parsed["analysis_pauses"] == 1
    assert parsed["roi_fallbacks"] == 1
    assert parsed["strip_samples"] == 1
    assert parsed["strip_sampling_seconds"] == 1.2
    assert parsed["runtime_metrics"][0]["failures"] == 2
    assert parsed["runtime_metrics"][0]["p95_ms"] == 20.0
    assert parsed["levels"] == {"INFO": 13}


def test_parse_log_ignores_unparseable_lines_and_missing_start():
    parsed = parse_log_lines(["not a log line", "", line("10:00:10,000", "something else")])

    assert parsed["log_lines"] == 1
    assert parsed["unparsed_lines"] == 1
    assert parsed["rounds"] == []
    assert parsed["sessions"] == []
    assert parsed["log_span_seconds"] == 0.0


def test_summarize_rounds_reports_per_round_cost_and_pending_recovery():
    parsed = parse_log_lines(synthetic_log())
    summary = summarize_rounds(parsed["rounds"])

    assert summary["logged_rounds"] == 3
    assert summary["publications"] == 3
    assert summary["logged_rounds_by_camera"] == {"1": 3}
    assert summary["latest_pending_by_camera"] == {"1": 0}
    assert summary["all_pending_recovered"] is True
    assert summary["overall"]["video_seconds"] == 18.0
    assert summary["overall"]["round_total_seconds"] == 6.75
    assert summary["overall"]["processing_ratio"] == 0.375
    assert summary["overall"]["analysis_ratio"] == 0.2944
    assert summary["overall"]["pending_segments"] == {
        "first": 3, "last": 0, "max": 3, "recovered": True,
    }
    assert summary["overall"]["round_total_distribution"]["max"] == 2.6


def test_summarize_rounds_counts_publications_across_two_cameras():
    lines = [
        line("10:00:10,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=1"),
        line("10:00:11,000", "live batch camera=2 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=1"),
        line("10:00:20,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=0"),
        line("10:00:21,000", "live batch camera=2 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=0"),
    ]
    summary = summarize_rounds(parse_log_lines(lines)["rounds"])

    assert summary["logged_rounds"] == 4
    assert summary["publications"] == 4
    assert summary["logged_rounds_by_camera"] == {"1": 2, "2": 2}
    assert summary["latest_pending_by_camera"] == {"1": 0, "2": 0}
    assert summary["cameras"]["1"]["pending_segments"]["recovered"] is True


def test_summarize_problem_lists_computes_backlog_and_recovery():
    first = {
        "generated_at_ms": 1000,
        "race_id": "race", "stage_id": "stage", "live": True,
        "summary": {"pending": 7},
        "cameras": {
            "1": {"sealed_through_ms": 120000, "scanned_through_ms": 110000,
                  "analyzed_through_ms": 90000, "pending_segments": 2, "files": 5,
                  "performance": {"processing_ratio": 0.4}},
        },
    }
    second = {
        "generated_at_ms": 2000,
        "summary": {"pending": 1},
        "cameras": {
            "1": {"sealed_through_ms": 180000, "analyzed_through_ms": 180000,
                  "pending_segments": 0, "files": 8},
        },
    }
    summary = summarize_problem_lists([second, first])

    assert [value["generated_at_ms"] for value in summary["snapshots"]] == [1000, 2000]
    assert summary["max_backlog_ms"] == 30000
    assert summary["max_pending_segments"] == 2
    assert summary["recovered"] is True
    assert summary["last_by_camera"]["1"]["pending_segments"] == 0
    assert summary["series"]["1"][0]["backlog_ms"] == 30000
    assert summarize_problem_lists([{"generated_at_ms": 3000, "cameras": {"1": {}}}])["recovered"] is None


def test_build_report_states_measured_bounds_and_never_claims_field_acceptance():
    parsed = parse_log_lines(synthetic_log())
    report = build_report(
        parsed,
        context={
            "app_version": "0.25.6", "device": "field laptop", "cameras": 1,
            "cyclerace_concurrent": True, "review_concurrent": True,
        },
        required_minutes=0.5,
        target_ratio=0.5,
    )

    assert report["field_verified"] is False
    assert report["observed"]["run_seconds"] == 60.0
    assert report["observed"]["logged_rounds"] == 3
    assert report["observed"]["analysis_process_starts"] == 2
    assert len(report["observed"]["analysis_process_start_events"]) == 2
    assert report["observed"]["processing_ratio"] == 0.375
    assert report["observed"]["processing_ratio_source"] == "log_rounds"
    assert report["observed"]["processing_ratio_required_cameras"] == 1
    assert report["observed"]["processing_ratio_required_cameras_source"] == "context"
    assert report["observed"]["pending_segments"]["latest_by_camera"] == {"1": 0}
    assert report["observed"]["pending_segments"]["all_recovered"] is True
    assert report["acceptance"]["run_at_least_required_minutes"]["pass"] is True
    assert report["acceptance"]["processing_ratio_below_one"]["pass"] is True
    assert report["acceptance"]["processing_ratio_at_or_below_target"]["pass"] is True
    assert report["acceptance"]["runtime_metric_failures_zero"]["pass"] is False
    assert report["acceptance"]["targets_met"] is False
    assert any("problem_list.json" in value for value in report["missing_signals"])
    assert any("进程采样" in value for value in report["missing_signals"])
    assert report["not_measured"]

    long_report = build_report(parsed, context={"app_version": "x"}, required_minutes=30)
    assert long_report["acceptance"]["run_at_least_required_minutes"]["pass"] is False
    assert "device" in long_report["context_missing"]


def test_analysis_process_starts_stays_unknown_without_starting_event():
    lines = [
        line("10:00:00,000", "FinishReview starting", logger=WINDOW),
        line("10:00:10,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=0"),
        line("10:00:20,000", "FinishReview stopped with exit code 0", logger=WINDOW),
    ]
    report = build_report(parse_log_lines(lines), required_minutes=0.1)

    assert report["observed"]["analysis_process_starts"] is None
    assert report["observed"]["logged_rounds"] == 1
    assert any("analysis process starting" in value for value in report["missing_signals"])


def test_pending_gate_uses_each_camera_latest_value():
    lines = [
        line("10:00:10,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=1"),
        line("10:00:11,000", "live batch camera=2 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=1"),
        line("10:00:20,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=0"),
    ]
    report = build_report(
        parse_log_lines(lines),
        context={"app_version": "x", "device": "x", "cameras": {"count": 2},
                 "cyclerace_concurrent": True, "review_concurrent": True},
        required_minutes=0.1,
    )
    gate = report["acceptance"]["pending_recovered_by_end"]

    assert gate["value"]["by_camera"] == {"1": 0, "2": 1}
    assert gate["pass"] is False
    assert report["observed"]["processing_ratio_source"] == "log_rounds"


def test_processing_ratio_fallback_uses_worst_problem_list_camera():
    payload = {
        "generated_at_ms": 1000,
        "cameras": {
            "1": {"performance": {"processing_ratio": 0.4}},
            "2": {"performance": {"processing_ratio": 0.8}},
        },
    }
    report = build_report(
        parse_log_lines([]),
        problem_lists=[payload],
        context={"app_version": "x", "device": "x", "cameras": {"count": 2},
                 "cyclerace_concurrent": True, "review_concurrent": True},
        required_minutes=0.1,
        target_ratio=0.5,
    )

    assert report["observed"]["processing_ratio"] == 0.8
    assert report["observed"]["processing_ratio_source"] == "problem_list_max_camera"
    assert report["observed"]["processing_ratio_by_camera"] == {"1": 0.4, "2": 0.8}
    assert report["acceptance"]["processing_ratio_at_or_below_target"]["pass"] is False


def test_processing_ratio_is_unknown_when_a_problem_list_camera_has_no_ratio():
    payload = {
        "generated_at_ms": 1000,
        "cameras": {
            "1": {"performance": {"processing_ratio": 0.4}},
            "2": {},
        },
    }
    report = build_report(
        parse_log_lines([]),
        problem_lists=[payload],
        context={"cameras": {"count": 2}},
        required_minutes=0.1,
    )

    assert report["observed"]["processing_ratio"] is None
    assert report["observed"]["processing_ratio_source"] == "problem_list_incomplete"
    assert report["acceptance"]["processing_ratio_below_one"]["pass"] is None


def test_log_processing_ratio_is_unknown_when_a_required_camera_never_logged():
    lines = [
        line("10:00:05,000", "Recording session started for cameras: 1, 2"),
        line("10:00:10,000", "live batch camera=1 video=6.00s sampling=0.1s analysis=1.0s "
                             "publish=0.1s total=1.2s pending=0"),
    ]
    report = build_report(
        parse_log_lines(lines),
        context={"app_version": "x"},
        required_minutes=0.1,
    )

    assert report["observed"]["configured_cameras"] == [1, 2]
    assert report["observed"]["processing_ratio_required_cameras"] == 2
    assert report["observed"]["processing_ratio_required_cameras_source"] == "recording_log"
    assert report["observed"]["processing_ratio"] is None
    assert report["observed"]["processing_ratio_source"] == "log_rounds_incomplete"
    assert report["acceptance"]["processing_ratio_below_one"]["pass"] is None


def test_live_sample_stats_uses_bounded_recent_window_with_exact_limits():
    stats = _LiveSampleStats(retain=2)
    for index, cpu in enumerate([10.0, 999.0, 30.0, 40.0, 50.0]):
        stats.add({
            "elapsed_seconds": index, "cpu_percent": cpu,
            "working_set_mb": 100.0 + index, "disk_free_mb": 1000.0 - index, "pid": 9,
        })

    summary = stats.summary()

    assert summary["samples"] == 5
    assert summary["duration_seconds"] == 4.0
    assert summary["cpu_percent"]["count"] == 5
    assert summary["cpu_percent"]["retained"] == 2
    assert summary["cpu_percent"]["max"] == 999.0
    assert summary["cpu_percent"]["min"] == 10.0
    assert summary["cpu_percent"]["median"] == 45.0
    assert summary["cpu_percent"]["mean"] == 225.8
    assert summary["disk_free_mb"] == {"min": 996.0, "max": 1000.0, "count": 5}


def test_samples_summary_reports_cpu_memory_and_disk(tmp_path):
    rows = [
        {"record_type": "sample", "elapsed_seconds": 0.0, "cpu_percent": None,
         "working_set_mb": 500.0, "disk_free_mb": 1000.0, "disk_path": str(tmp_path), "pid": 9},
        {"record_type": "sample", "elapsed_seconds": 5.0, "cpu_percent": 120.0,
         "working_set_mb": 600.0, "disk_free_mb": 990.0, "disk_path": str(tmp_path), "pid": 9},
    ]
    path = tmp_path / "samples.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    loaded = load_samples([path])
    summary = summarize_samples(loaded)

    assert summary["samples"] == 2
    assert summary["duration_seconds"] == 5.0
    assert summary["cpu_percent"] == {"count": 1, "median": 120.0, "p95": 120.0, "max": 120.0}
    assert summary["working_set_mb"]["max"] == 600.0
    assert summary["disk_free_mb"] == {"min": 990.0, "max": 1000.0, "count": 2}


def test_resolve_log_paths_reads_rotated_files_oldest_first(tmp_path):
    current = tmp_path / "finish_review.log"
    rotated = tmp_path / "finish_review.log.1"
    current.write_text("", encoding="utf-8")
    rotated.write_text("", encoding="utf-8")
    os.utime(current, (2000, 2000))
    os.utime(rotated, (1000, 1000))

    resolved = resolve_log_paths([tmp_path])

    assert [path.name for path in resolved] == ["finish_review.log.1", "finish_review.log"]


def test_summarize_cli_writes_report_and_strict_gate(tmp_path, capsys):
    log_path = tmp_path / "finish_review.log"
    log_path.write_text("\n".join(synthetic_log()) + "\n", encoding="utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({
        "app_version": "0.25.6", "device": "field laptop", "cameras": 1,
        "cyclerace_concurrent": True, "review_concurrent": True,
    }), encoding="utf-8")
    workspace = tmp_path / "event"
    (workspace / "finish_problems").mkdir(parents=True)
    (workspace / "finish_problems" / "problem_list.json").write_text(json.dumps({
        "generated_at_ms": 2000,
        "cameras": {"1": {"sealed_through_ms": 180000, "analyzed_through_ms": 180000,
                          "pending_segments": 0}},
    }), encoding="utf-8")
    sampler_path = tmp_path / "samples.jsonl"
    sampler_path.write_text("".join(json.dumps({
        "record_type": "sample", "session_id": "s", "elapsed_seconds": index,
        "cpu_percent": 50.0, "working_set_mb": 400.0, "disk_free_mb": 1000.0,
        "disk_path": str(tmp_path), "pid": 9,
    }) + "\n" for index in (0, 5)), encoding="utf-8")
    output = tmp_path / "baseline.json"

    assert main([
        "summarize", "--log", str(log_path), "--context", str(context_path),
        "--workspace", str(workspace), "--sampler", str(sampler_path),
        "--required-minutes", "0.5", "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["sources"]["logs"] == [str(log_path)]
    assert report["field_verified"] is False
    assert len(report["progress"]["snapshots"]) == 1
    assert report["progress"]["last_by_camera"]["1"]["pending_segments"] == 0
    assert report["sampler"]["samples"] == 2
    assert '"field_verified": false' in capsys.readouterr().out

    assert main([
        "summarize", "--log", str(log_path), "--context", str(context_path),
        "--required-minutes", "30", "--strict",
    ]) == 1
    assert "30" in capsys.readouterr().out


def test_summarize_cli_rejects_missing_inputs(tmp_path):
    assert main(["summarize", "--log", str(tmp_path / "missing.log")]) == 2
    assert main(["summarize"]) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows process sampler")
def test_process_snapshot_reads_own_process_and_disk(tmp_path):
    snapshot = process_snapshot(os.getpid(), disk_path=tmp_path)

    assert snapshot["pid"] == os.getpid()
    assert snapshot["cpu_time_seconds"] >= 0
    assert snapshot["working_set_mb"] > 0
    assert snapshot["disk_free_mb"] > 0
    assert snapshot["disk_path"] == str(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows process sampler")
def test_sample_cli_writes_jsonl(tmp_path):
    output = tmp_path / "samples.jsonl"
    exit_code = main([
        "sample", "--pid", str(os.getpid()), "--interval", "0.05",
        "--duration", "0.2", "--disk-path", str(tmp_path), "--output", str(output),
    ])
    rows = load_samples([output])

    assert exit_code == 0
    assert len(rows) >= 2
    assert rows[0]["record_type"] == "sample"
    assert rows[0]["working_set_mb"] > 0
    assert any(row["cpu_percent"] is not None for row in rows[1:])
