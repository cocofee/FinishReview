from __future__ import annotations

import hashlib
import json
from urllib.request import Request, urlopen

import pytest

from realtime.auyat_rgb import scan_rgb_file
from realtime.passage_receiver import PassageEventReceiver, PassageEventStore
from realtime.race_metadata import RaceMetadataStore
from tools.generate_field_validation_fixture import (
    FIXTURE_ID,
    SCENARIOS,
    generate_fixture,
    main,
)


def _files(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _post_json(receiver, payload):
    request = Request(
        f"http://127.0.0.1:{receiver.listen_port}/api/v1/passage-events",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_fixture_is_deterministic_and_loadable(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    first_manifest_path = generate_fixture(first_root)
    second_manifest_path = generate_fixture(second_root)

    assert _files(first_root) == _files(second_root)
    manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    assert second_manifest_path.name == first_manifest_path.name
    assert manifest["fixture_id"] == FIXTURE_ID
    assert manifest["contains_real_personal_data"] is False
    assert manifest["regular_video_provided"] is False
    assert manifest["expected"] == {
        "auyat_capture_count": 1,
        "cyclerace_ack_sequence": [
            {
                "http_status": 201,
                "message_type": "race_metadata_ack",
                "status": "accepted",
            },
            {
                "http_status": 201,
                "message_type": "passage_ack",
                "status": "accepted",
            },
            {
                "http_status": 200,
                "message_type": "passage_ack",
                "status": "duplicate",
            },
            {
                "http_status": 201,
                "message_type": "passage_ack",
                "status": "accepted",
            },
            {
                "http_status": 201,
                "message_type": "race_focus_ack",
                "status": "accepted",
            },
        ],
        "cyclerace_request_count": 5,
        "duplicate_passage_event_id": "sample-race-finish-101",
        "passage_event_count": 2,
    }
    assert manifest["runtime_output_dir"] == "runtime/finish_review"
    assert manifest["mutable_files"] == ["validation_result.json"]
    assert (first_root / manifest["runtime_output_dir"]).is_dir()
    assert not any((first_root / manifest["runtime_output_dir"]).iterdir())

    metadata = RaceMetadataStore(
        first_root
        / "expected"
        / "finish_review"
        / "cyclerace_race_metadata.json"
    ).current()
    assert metadata is not None
    assert metadata.race_id == "sample-race"
    assert [athlete.bib for athlete in metadata.athletes] == ["101", "102"]

    passages = PassageEventStore(
        first_root
        / "expected"
        / "finish_review"
        / "cyclerace_passage_events.jsonl"
    ).events()
    assert [event.bib for event in passages] == ["101", "102"]
    captures = scan_rgb_file(
        first_root / "auyat" / "Photo" / "validation_sample.RGB"
    )
    assert len(captures) == 1
    assert captures[0].media_started_at_ms <= passages[0].timeline_timestamp_ms
    assert captures[0].media_ended_at_ms >= passages[-1].timeline_timestamp_ms

    for relative_path, expected_digest in manifest["immutable_files"].items():
        actual_digest = hashlib.sha256(
            (first_root / relative_path).read_bytes()
        ).hexdigest()
        assert actual_digest == expected_digest
    assert "validation_result.json" not in manifest["immutable_files"]


def test_fixture_replays_through_receiver_into_empty_runtime(tmp_path):
    root = tmp_path / "fixture"
    manifest_path = generate_fixture(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_dir = root / manifest["runtime_output_dir"]
    passage_store = PassageEventStore(
        runtime_dir / "cyclerace_passage_events.jsonl"
    )
    metadata_store = RaceMetadataStore(
        runtime_dir / "cyclerace_race_metadata.json"
    )
    receiver = PassageEventReceiver(
        "127.0.0.1",
        0,
        passage_store,
        discovery_port=None,
        metadata_store=metadata_store,
    )
    receiver.start()
    try:
        actual_responses = []
        for line in (root / "cyclerace_requests.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            http_status, ack = _post_json(receiver, json.loads(line))
            actual_responses.append(
                {
                    "message_type": ack["message_type"],
                    "http_status": http_status,
                    "status": ack["status"],
                }
            )
    finally:
        receiver.stop()

    assert actual_responses == manifest["expected"]["cyclerace_ack_sequence"]
    assert [event.bib for event in passage_store.events()] == ["101", "102"]
    assert len(
        (runtime_dir / "cyclerace_passage_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ) == 2
    assert metadata_store.current() is not None


def test_fixture_contains_duplicate_request_and_not_run_result(tmp_path):
    root = tmp_path / "fixture"
    generate_fixture(root)

    requests = [
        json.loads(line)
        for line in (root / "cyclerace_requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert requests[1] == requests[2]
    assert requests[1]["event_id"] == "sample-race-finish-101"

    result = json.loads(
        (root / "validation_result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "not_run"
    assert [scenario["id"] for scenario in result["scenarios"]] == list(SCENARIOS)
    assert {scenario["status"] for scenario in result["scenarios"]} == {"not_run"}
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    assert "token" not in serialized
    assert "password" not in serialized


def test_fixture_refuses_to_overwrite_non_empty_directory(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        generate_fixture(output)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cli_prints_manifest_path(tmp_path, capsys):
    output = tmp_path / "cli"

    assert main(["--output", str(output)]) == 0

    printed = capsys.readouterr().out.strip()
    assert printed == str((output / "fixture_manifest.json").absolute())
