"""Session identity, owned services, and immutable preparation snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .event_ingestion import merge_events
from .finish_line import FinishLineStore
from .passage_evidence import (ContinuousMarkerStore, PassageEvidenceAssociationStore,
                               VideoClockCalibrationStore)
from .passage_receiver import PassageEvent, PassageEventStore
from .preflight import PreflightJournal
from .race_metadata import RaceMetadata, RaceMetadataStore
from .review_clip import PassageReviewBindingStore
from .review_recorder import ArchiveTimelinePublisher, load_archive_recording_sessions
from .video_arrival import VideoArrivalCandidateStore
from .video_discovery import VideoDiscoveryStore
from .video_review import VideoReviewJournal
from .video_timeline import VideoTimelineStore


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    generation: int
    provider: str
    output_dir: Path
    metadata: RaceMetadata | None
    events: tuple[PassageEvent, ...]


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Service handles are owned mutable services, not a read-only snapshot."""
    snapshot: SessionSnapshot
    passage_store: PassageEventStore
    metadata_store: RaceMetadataStore | None
    timeline_store: VideoTimelineStore
    review_binding_store: PassageReviewBindingStore
    association_store: PassageEvidenceAssociationStore
    calibration_store: VideoClockCalibrationStore
    preflight_journal: PreflightJournal
    archive_publishers: tuple[ArchiveTimelinePublisher, ...]
    receiver_passage_store: PassageEventStore
    receiver_metadata_store: RaceMetadataStore
    finish_line_store: FinishLineStore
    video_review_journal: VideoReviewJournal
    video_arrival_store: VideoArrivalCandidateStore
    video_discovery_store: VideoDiscoveryStore
    continuous_marker_store: ContinuousMarkerStore


def prepare_session(output_dir: Path, provider: str, generation: int,
                    inbox: PassageEventStore, inbox_metadata: RaceMetadataStore,
                    *, workspace_root: Path | None = None,
                    metadata: RaceMetadata | None = None,
                    merge_sources: tuple[PassageEventStore, ...] = ()) -> SessionContext:
    output_dir.mkdir(parents=True, exist_ok=True)
    if workspace_root is not None:
        inbox_dir = workspace_root / ".finishreview"
        inbox = PassageEventStore(inbox_dir / "cyclerace_passage_inbox.jsonl")
        inbox_metadata = RaceMetadataStore(inbox_dir / "cyclerace_metadata_inbox.json")
    passages = PassageEventStore(output_dir / f"{provider}_passage_events.jsonl")
    metadata_store = (RaceMetadataStore(output_dir / "cyclerace_race_metadata.json")
                      if provider == "cyclerace" else None)
    if metadata is not None and metadata_store is not None:
        metadata_store.store(metadata)
    loaded_metadata = metadata_store.current() if metadata_store else None
    if loaded_metadata is not None:
        # Opening a historical race must also replay an inbox-only withdrawal
        # left by a failed projection or a previous shutdown.
        merge_events(passages, (inbox, *merge_sources), loaded_metadata.race_id)
    timeline = VideoTimelineStore(output_dir / "video_timeline.jsonl")
    return SessionContext(
        snapshot=SessionSnapshot(generation, provider, output_dir,
                                 loaded_metadata,
                                 passages.events(include_inactive=True)),
        passage_store=passages, metadata_store=metadata_store, timeline_store=timeline,
        review_binding_store=PassageReviewBindingStore(output_dir / "review_clips.jsonl"),
        association_store=PassageEvidenceAssociationStore(output_dir / "passage_evidence_associations.jsonl"),
        calibration_store=VideoClockCalibrationStore(output_dir / "video_clock_calibrations.jsonl"),
        preflight_journal=PreflightJournal(output_dir / "preflight_tests.jsonl"),
        archive_publishers=tuple(ArchiveTimelinePublisher(session, timeline)
                                 for session in load_archive_recording_sessions(output_dir)),
        receiver_passage_store=inbox, receiver_metadata_store=inbox_metadata,
        finish_line_store=FinishLineStore(output_dir / "finish_lines.json"),
        video_review_journal=VideoReviewJournal(output_dir / "video_review.jsonl"),
        video_arrival_store=VideoArrivalCandidateStore(output_dir / "video_arrival_candidates.jsonl"),
        video_discovery_store=VideoDiscoveryStore(output_dir / "video_discoveries.jsonl"),
        continuous_marker_store=ContinuousMarkerStore(output_dir / "continuous_markers.jsonl"),
    )
