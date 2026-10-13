import json

from tools.reconcile_count_history import run_once


def test_cli_retains_receipt_history_and_does_not_duplicate_it(tmp_path):
    data = {"schema_version": 1, "scope": {"race_id": "r", "stage_id": "s"},
            "clock": {"calibrated": True}, "cutoff_ms": 20000, "updated_at_ms": 20000,
            "groups": [{"window_id": "w", "start_ms": 1000, "end_ms": 1100,
                        "video_count": 1, "chip_count": 0, "pending_count": 0, "excluded_count": 0,
                        "status": "unresolved", "reasons": [], "chip_event_ids": [], "evidence": {}}]}
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    journal = tmp_path / "chips.jsonl"
    def chip(uid, bib, received, revision=1, active=True):
        return dict(event_id=uid, bib=bib, race_id="r", stage_id="s", passage_timestamp_ms=1050,
                    received_at_ms=received, revision=revision, is_active=active)
    events = [chip("r", "023", 1020), chip("extra", "020", 1021), chip("extra", "020", 9000, 2, False)]
    journal.write_text("".join(json.dumps(e)+"\n" for e in events), encoding="utf-8")
    output = tmp_path / "result"
    run_once(path, journal, output, as_of_ms=7000)
    result = run_once(path, journal, output, as_of_ms=10000)
    assert result["groups"][0]["status"] == "resolved"
    size = (output / "count_anomaly_history.jsonl").stat().st_size
    run_once(path, journal, output, as_of_ms=10000)
    assert (output / "count_anomaly_history.jsonl").stat().st_size == size
    assert journal.read_text(encoding="utf-8") == "".join(json.dumps(e)+"\n" for e in events)
