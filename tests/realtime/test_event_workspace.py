from realtime.event_workspace import (
    EventWorkspaceError,
    discover_event_workspaces,
    summarize_event_workspace,
    validate_event_workspace,
)
from realtime.passage_evidence import (
    REGULAR_SOURCE,
    PassageEvidenceAssociationStore,
)
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.preflight import PreflightJournal, PreflightRun
from realtime.race_metadata import RaceMetadata, RaceMetadataStore


def _metadata(race_id, race_name, *, stage_id="stage-1", stage_name="终点"):
    return RaceMetadata(
        race_id=race_id,
        stage_id=stage_id,
        revision=1,
        emitted_at_ms=1,
        race_name=race_name,
        stage_name=stage_name,
    )


def _event(event_id, race_id="race-1", *, stage_id="stage-1"):
    return PassageEvent(
        event_id=event_id,
        race_id=race_id,
        stage_id=stage_id,
        group_id="group-1",
        sequence=1,
        bib="15",
        passage_time_ms=1_000,
        emitted_at_ms=1_100,
    )


def test_discovers_root_legacy_workspace_and_direct_children(tmp_path):
    RaceMetadataStore(tmp_path / "cyclerace_race_metadata.json").store(
        _metadata("race-root", "根目录旧赛事")
    )
    child = tmp_path / "新赛事"
    RaceMetadataStore(child / "cyclerace_race_metadata.json").store(
        _metadata("race-child", "新赛事")
    )
    nested = tmp_path / "container" / "nested"
    RaceMetadataStore(nested / "cyclerace_race_metadata.json").store(
        _metadata("race-nested", "不应递归发现")
    )
    (tmp_path / ".finishreview").mkdir()

    workspaces = discover_event_workspaces(tmp_path)

    assert {workspace.metadata.race_id for workspace in workspaces} == {
        "race-root",
        "race-child",
    }
    assert next(item for item in workspaces if item.is_root).path == tmp_path.resolve()


def test_discovery_ignores_invalid_metadata_without_mutating_other_dirs(tmp_path):
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "cyclerace_race_metadata.json").write_text("{", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    assert discover_event_workspaces(tmp_path) == ()
    assert list(unrelated.iterdir()) == []


def test_validation_rejects_workspace_outside_current_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    RaceMetadataStore(outside / "cyclerace_race_metadata.json").store(
        _metadata("race-outside", "外部赛事")
    )

    try:
        validate_event_workspace(outside, root)
    except EventWorkspaceError as error:
        assert "不在当前保存根目录" in str(error)
    else:
        raise AssertionError("outside workspace should be rejected")


def test_summary_counts_current_stage_confirmations_and_excludes_preflight(tmp_path):
    metadata = _metadata("race-1", "测试赛事")
    RaceMetadataStore(tmp_path / "cyclerace_race_metadata.json").store(metadata)
    passage_store = PassageEventStore(tmp_path / "cyclerace_passage_events.jsonl")
    passage_store.append(_event("confirmed"))
    passage_store.append(_event("pending"))
    passage_store.append(_event("other-stage", stage_id="stage-2"))
    preflight_event = _event("preflight")
    passage_store.append(preflight_event)
    preflight_run = PreflightRun.start(
        (),
        started_at_ms=0,
        require_regular=False,
        require_high_speed=False,
    ).observe((preflight_event,))
    PreflightJournal(tmp_path / "preflight_tests.jsonl").append(
        preflight_run,
        recorded_at_ms=1_200,
    )
    association_store = PassageEvidenceAssociationStore(
        tmp_path / "passage_evidence_associations.jsonl"
    )
    association_store.confirm(
        passage_event_id="confirmed",
        bib="15",
        confirmed_source=REGULAR_SOURCE,
        segment_id="segment-1",
        frame_index=1,
        position_ms=100,
        marker_x_normalized=0.5,
        marker_y_normalized=0.5,
        confirmed_at_ms=2_000,
    )
    workspace = validate_event_workspace(tmp_path, tmp_path)

    summary = summarize_event_workspace(workspace)

    assert summary.passage_count == 2
    assert summary.confirmed_count == 1
