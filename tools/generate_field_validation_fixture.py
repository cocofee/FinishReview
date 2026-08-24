"""Generate deterministic, non-private data for field validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from realtime.auyat_rgb import (
    CAPTURE_END_FLAG,
    CAPTURE_START_FLAG,
    HEADER_SIZE,
    RECORD_SIZE,
    TICKS_PER_DAY,
    TICKS_PER_MILLISECOND,
)
from realtime.passage_receiver import PassageEvent, PassageEventStore, RaceFocus
from realtime.race_metadata import (
    RaceAthleteMetadata,
    RaceGroupMetadata,
    RaceMetadata,
    RaceMetadataStore,
)


BEIJING_TIMEZONE = timezone(timedelta(hours=8))
FIXTURE_ID = "finish-review-field-v1"
SAMPLE_DATE = date(2026, 8, 24)
SAMPLE_STARTED_AT = datetime(2026, 8, 24, 10, 0, tzinfo=BEIJING_TIMEZONE)
SCENARIOS = (
    "cyclerace_push",
    "dual_camera_recording",
    "high_speed_share",
    "evidence_confirmation",
    "application_restart",
    "rtsp_interruption",
    "cyclerace_slow_request",
    "high_speed_share_offline",
    "low_disk_space",
    "long_running_session",
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, payloads: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        for payload in payloads
    )
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _write_auyat_rgb(path: Path) -> None:
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<H", header, 24, 1024)
    struct.pack_into("<H", header, 36, SAMPLE_DATE.year)
    header[38] = SAMPLE_DATE.month
    header[39] = SAMPLE_DATE.day

    base_tick = TICKS_PER_DAY + 10 * 60 * 60 * 20_000
    offsets_ms = (0, 50, 100, 150, 200)
    records = []
    for index, offset_ms in enumerate(offsets_ms):
        record = bytearray(RECORD_SIZE)
        flag = 0
        if index == 0:
            flag |= CAPTURE_START_FLAG
        if index == len(offsets_ms) - 1:
            flag |= CAPTURE_END_FLAG
        struct.pack_into(
            "<II",
            record,
            0,
            base_tick + offset_ms * TICKS_PER_MILLISECOND,
            flag,
        )
        record[8:] = bytes((30 + index, 80 + index, 130 + index)) * 1024
        records.append(record)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + b"".join(records))


def _sample_metadata(sample_started_at_ms: int) -> RaceMetadata:
    group_id = "sample-open"
    return RaceMetadata(
        race_id="sample-race",
        stage_id="sample-finish",
        revision=1,
        emitted_at_ms=sample_started_at_ms - 1_000,
        race_name="FinishReview Validation Sample",
        stage_name="Sample Finish",
        stage_date=SAMPLE_DATE.isoformat(),
        groups=(RaceGroupMetadata(group_id, "Sample Open"),),
        athletes=(
            RaceAthleteMetadata(
                athlete_id="sample-athlete-101",
                bib="101",
                name="Sample Rider 101",
                team_name="Sample Team A",
                group_id=group_id,
                chip_ids=("SAMPLE-CHIP-101",),
            ),
            RaceAthleteMetadata(
                athlete_id="sample-athlete-102",
                bib="102",
                name="Sample Rider 102",
                team_name="Sample Team B",
                group_id=group_id,
                chip_ids=("SAMPLE-CHIP-102",),
            ),
        ),
    )


def _sample_events(sample_started_at_ms: int) -> tuple[PassageEvent, ...]:
    values = (
        (101, 100, "Sample Rider 101", "Sample Team A"),
        (102, 150, "Sample Rider 102", "Sample Team B"),
    )
    return tuple(
        PassageEvent(
            event_id=f"sample-race-finish-{bib}",
            race_id="sample-race",
            stage_id="sample-finish",
            group_id="sample-open",
            sequence=index,
            chip_id=f"SAMPLE-CHIP-{bib}",
            bib=str(bib),
            passage_time_ms=10 * 60 * 60 * 1_000 + offset_ms,
            passage_timestamp_ms=sample_started_at_ms + offset_ms,
            lap=1,
            emitted_at_ms=sample_started_at_ms + offset_ms + 50,
            race_name="FinishReview Validation Sample",
            stage_name="Sample Finish",
            group_name="Sample Open",
            athlete_id=f"sample-athlete-{bib}",
            athlete_name=athlete_name,
            team_name=team_name,
        )
        for index, (bib, offset_ms, athlete_name, team_name) in enumerate(
            values,
            start=1,
        )
    )


def _focus_payload(sample_started_at_ms: int) -> dict[str, Any]:
    focus = RaceFocus(
        race_id="sample-race",
        stage_id="sample-finish",
        athlete_id="sample-athlete-101",
        bib="101",
        group_id="sample-open",
        emitted_at_ms=sample_started_at_ms + 500,
    )
    return asdict(focus)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output_dir(output_dir: str | Path) -> Path:
    output = Path(output_dir).expanduser().absolute()
    if output.exists() and not output.is_dir():
        raise FileExistsError(f"output path is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def generate_fixture(output_dir: str | Path) -> Path:
    """Generate one complete fixture and return its manifest path."""

    output = _prepare_output_dir(output_dir)
    sample_started_at_ms = int(SAMPLE_STARTED_AT.timestamp() * 1_000)
    metadata = _sample_metadata(sample_started_at_ms)
    events = _sample_events(sample_started_at_ms)

    finish_review_dir = output / "expected" / "finish_review"
    metadata_path = finish_review_dir / "cyclerace_race_metadata.json"
    passage_path = finish_review_dir / "cyclerace_passage_events.jsonl"
    RaceMetadataStore(metadata_path).store(metadata)
    passage_store = PassageEventStore(passage_path)
    for event in events:
        passage_store.append(event)

    runtime_dir = output / "runtime" / "finish_review"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    requests_path = output / "cyclerace_requests.jsonl"
    _write_jsonl(
        requests_path,
        (
            metadata.to_payload(),
            events[0].to_payload(),
            events[0].to_payload(),
            events[1].to_payload(),
            _focus_payload(sample_started_at_ms),
        ),
    )

    auyat_path = output / "auyat" / "Photo" / "validation_sample.RGB"
    _write_auyat_rgb(auyat_path)

    result_path = output / "validation_result.json"
    _write_json(
        result_path,
        {
            "schema_version": 1,
            "fixture_id": FIXTURE_ID,
            "status": "not_run",
            "environment": {
                "finish_review_commit": "",
                "cyclerace_commit": "",
                "windows_version": "",
                "timezone": "",
                "device_name": "",
                "camera_models": [],
                "network_summary": "",
            },
            "scenarios": [
                {
                    "id": scenario_id,
                    "status": "not_run",
                    "started_at": "",
                    "ended_at": "",
                    "log_paths": [],
                    "evidence_paths": [],
                    "notes": "",
                }
                for scenario_id in SCENARIOS
            ],
        },
    )

    immutable_files = (
        metadata_path,
        passage_path,
        requests_path,
        auyat_path,
    )
    manifest_path = output / "fixture_manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "fixture_id": FIXTURE_ID,
            "sample_started_at": SAMPLE_STARTED_AT.isoformat(),
            "contains_real_personal_data": False,
            "regular_video_provided": False,
            "expected": {
                "passage_event_count": len(events),
                "cyclerace_request_count": 5,
                "auyat_capture_count": 1,
                "duplicate_passage_event_id": events[0].event_id,
                "cyclerace_ack_sequence": [
                    {
                        "message_type": "race_metadata_ack",
                        "http_status": 201,
                        "status": "accepted",
                    },
                    {
                        "message_type": "passage_ack",
                        "http_status": 201,
                        "status": "accepted",
                    },
                    {
                        "message_type": "passage_ack",
                        "http_status": 200,
                        "status": "duplicate",
                    },
                    {
                        "message_type": "passage_ack",
                        "http_status": 201,
                        "status": "accepted",
                    },
                    {
                        "message_type": "race_focus_ack",
                        "http_status": 201,
                        "status": "accepted",
                    },
                ],
            },
            "runtime_output_dir": runtime_dir.relative_to(output).as_posix(),
            "immutable_files": {
                path.relative_to(output).as_posix(): _sha256(path)
                for path in immutable_files
            },
            "mutable_files": [result_path.relative_to(output).as_posix()],
        },
    )
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New or empty directory that will receive the fixture",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = generate_fixture(args.output)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FIXTURE_ID", "SCENARIOS", "generate_fixture", "main"]
