"""Camera clock offsets and continuous-session calibration of the passage review surface."""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Optional

from .clock_sync import CLOCK_MODEL_FILENAME, MANUAL_ANCHOR_TOLERANCE_MS, ClockModelStore
from .passage_evidence import REGULAR_SOURCE, PassageEvidenceAssociation
from .passage_receiver import PassageEvent
from .video_timeline import PassageVideoLocation
from .passage_review_common import _ARCHIVE_SEGMENT_SUFFIX_RE
from .passage_evidence_pane import PassageEvidencePane

logger = logging.getLogger("FinishReview.Review")


class PassageReviewClockMixin:
    """Camera clock offsets, saved calibrations and the automatic clock model."""

    def _on_offset_changed(self, value: int) -> None:
        self.clock_offset_ms = int(value)
        if self._clock_offset_by_camera:
            for camera_index in self._configured_regular_camera_indexes:
                self._clock_offset_by_camera[camera_index] = self.clock_offset_ms
        self.clock_offset_changed.emit(self.clock_offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def set_camera_clock_offset(self, camera_index: int, offset_ms: int) -> None:
        """Set a camera-specific review offset without changing race timing."""

        camera_index = max(1, int(camera_index))
        self._clock_offset_by_camera[camera_index] = int(offset_ms)
        self._lookup_cache.clear()
        self.refresh()

    def _clock_offset_for_camera(self, camera_index: int) -> int:
        return int(
            self._clock_offset_by_camera.get(
                max(1, int(camera_index)),
                self.clock_offset_ms,
            )
        )

    def _camera_one_pane(self) -> PassageEvidencePane:
        """Return the authoritative continuous-review pane (camera 1)."""

        by_camera = getattr(self, "_regular_panes_by_camera", {})
        pane = by_camera.get(1)
        if pane is not None:
            return pane
        # A camera-1 pane is the normal layout. Keep a safe fallback for
        # camera-less or legacy layouts used by integrations and tests.
        regular_panes = getattr(self, "regular_panes", ())
        if regular_panes:
            return regular_panes[0]
        return self.regular_pane

    def _continuous_offset_for_location(
        self,
        location: Optional[PassageVideoLocation],
    ) -> int:
        if location is None:
            return self._clock_offset_for_camera(1)
        camera_index = max(1, int(location.segment.camera_index))
        session_key = self._recording_session_key(location)
        return self._calibrated_clock_offset(camera_index, session_key)

    def _calibrated_clock_offset(self, camera_index: int, session_key: str) -> int:
        calibration = self.calibration_store.get(camera_index, session_key)
        if calibration is None:
            # Older versions keyed HLS calibrations by the generated window name.
            aliases = []
            for saved in self.calibration_store.calibrations():
                if saved.camera_index != camera_index:
                    continue
                parts = saved.session_key.split("|", 2)
                if len(parts) != 3:
                    continue
                path = Path(parts[1]) / (parts[2] + ".m3u8")
                if path.is_file() and self._recording_session_key_from_path(parts[0], path) == session_key:
                    aliases.append(saved)
            if aliases:
                calibration = max(aliases, key=lambda saved: saved.calibrated_at_ms)
        return int(
            self._continuous_clock_offsets.get(
                (camera_index, session_key),
                calibration.offset_ms if calibration is not None else self._clock_offset_for_camera(camera_index),
            )
        )

    @staticmethod
    def _recording_session_key_from_path(source_id: str, video_path: Path) -> str:
        from .runtime_paths import recording_session_path
        path = recording_session_path(Path(video_path).absolute())
        stem = _ARCHIVE_SEGMENT_SUFFIX_RE.sub("", path.stem)
        match = re.fullmatch(
            r"(camera_\d+)_\d{8}_\d{6}_\d{6}(?:_review_\d+)?", stem,
        )
        parent = path.parent
        if match is not None:
            stem = re.sub(r"_review_\d+$", "", stem)
            if parent.parent.name == "review_buffer":
                parent = parent.parent.parent / "videos"
            source_id = match.group(1) + "_review"
        return f"{source_id}|{parent}|{stem}"

    @classmethod
    def _recording_session_key(cls, location: PassageVideoLocation) -> str:
        return cls._recording_session_key_from_path(
            location.segment.source_id,
            location.video_path,
        )

    def _seed_continuous_clock_offsets(self) -> None:
        events_by_id = {
            event.event_id: event
            for event in self._events_for_current_metadata(
                self.passage_store.events()
            )
        }
        changed = False
        for calibration in self.calibration_store.calibrations():
            parts = calibration.session_key.split("|", 2)
            session_key = (
                self._recording_session_key_from_path(parts[0], Path(parts[1]) / parts[2])
                if len(parts) == 3 else calibration.session_key
            )
            key = (calibration.camera_index, session_key)
            if key not in self._continuous_clock_offsets:
                self._continuous_clock_offsets[key] = int(calibration.offset_ms)
                changed = True
        associations = sorted(
            self._current_associations(),
            key=lambda item: (
                events_by_id.get(item.passage_event_id).timeline_timestamp_ms
                if item.passage_event_id in events_by_id
                else 2**63 - 1,
                item.confirmed_at_ms,
            ),
        )
        for association in associations:
            if association.confirmed_source != REGULAR_SOURCE:
                continue
            event = events_by_id.get(association.passage_event_id)
            segment = self.timeline_store.get_segment(association.segment_id)
            if (
                event is None
                or segment is None
                or segment.media_started_at_ms is None
            ):
                continue
            continuous_lookup = self.timeline_store.locate_passage(
                event.timeline_timestamp_ms,
                clock_offset_ms=self._clock_offset_for_camera(
                    segment.camera_index
                ),
                pre_roll_ms=self.CONTINUOUS_SKIP_LEAD_MS,
                race_id=event.race_id,
                prefer_continuous_media=True,
            )
            continuous_location = self._regular_location_for_camera(
                continuous_lookup,
                segment.camera_index,
            )
            session_segment = (
                continuous_location.segment
                if continuous_location is not None
                else segment
            )
            video_path = (
                continuous_location.video_path
                if continuous_location is not None
                else self.timeline_store.resolve_video_path(segment)
            )
            key = (
                session_segment.camera_index,
                self._recording_session_key_from_path(
                    session_segment.source_id,
                    video_path,
                ),
            )
            if key in self._continuous_clock_offsets:
                continue
            self._continuous_clock_offsets[key] = (
                int(segment.media_started_at_ms)
                + int(association.position_ms)
                - int(event.timeline_timestamp_ms)
            )
            changed = True
        if changed:
            self._continuous_calibration_revision += 1

    def _continuous_calibration_summary(self) -> str:
        pane = self._active_playback_pane()
        location = pane.location
        if pane.source_kind != REGULAR_SOURCE or location is None:
            self.batch_context_label.setToolTip("")
            return ""
        camera_index = int(location.segment.camera_index)
        key = (camera_index, self._recording_session_key(location))
        offset_ms = self._continuous_clock_offsets.get(key)
        calibration = self.calibration_store.get(*key)
        if offset_ms is None and calibration is not None:
            offset_ms = int(calibration.offset_ms)
        if offset_ms is None:
            self.batch_context_label.setToolTip(
                "找到首位运动员的实际过线画面并确认，后续选手沿用该机位时间偏移。"
            )
            return f"机位{camera_index} 待首人校时"
        auto_label, auto_tip = self._auto_clock_cross_check(
            camera_index, offset_ms, calibration,
        )
        self.batch_context_label.setToolTip(
            f"机位 {camera_index} 已按首人校准，时间偏移 {offset_ms:+d} ms；"
            f"后续运动员沿用此偏移定位。\n录像会话：{key[1]}"
            + (f"\n校时锚点：{calibration.anchor_bib}" if calibration is not None else "")
            + auto_tip
        )
        return f"机位{camera_index} 已校准 {offset_ms:+d} ms" + auto_label

    def _auto_clock_model(self, camera_index: int):
        """Latest automatic calibration published by the offline analysis."""

        path = self.passage_store.journal_path.with_name(CLOCK_MODEL_FILENAME)
        try:
            stat = path.stat()
            signature = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = getattr(self, "_auto_clock_store_cache", None)
        if cached is None or cached[0] != signature:
            try:
                cached = (signature, ClockModelStore(path))
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning("automatic clock models unavailable: %s", error)
                return None
            self._auto_clock_store_cache = cached
        return cached[1].latest(camera_index)

    def _auto_clock_cross_check(self, camera_index, offset_ms, calibration) -> tuple[str, str]:
        """Compare the first-rider anchor with the automatic model; never override it."""

        model = self._auto_clock_model(camera_index)
        if model is None:
            return "", ""
        anchor = (
            self.passage_store.get(calibration.anchor_event_id)
            if calibration is not None else None
        )
        anchor_time = (
            int(anchor.timeline_timestamp_ms)
            if anchor is not None else (model.valid_from_ms + model.valid_to_ms) // 2
        )
        auto_offset = model.offset_at(anchor_time)
        difference = int(offset_ms) - auto_offset
        tip = (
            f"\n自动校时 v{model.version}：该时刻偏移 {auto_offset:+d} ms，"
            f"漂移 {model.slope_ms_per_hour:+.0f} ms/小时；人工与自动相差 {difference:+d} ms"
        )
        if abs(difference) > MANUAL_ANCHOR_TOLERANCE_MS:
            return (
                f" · 与自动校时差 {difference:+d} ms，请核对",
                tip + f"，超过 {MANUAL_ANCHOR_TOLERANCE_MS} ms，请核对首人校时；未自动覆盖。",
            )
        return "", tip

    def _calibrate_continuous_session(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        association: PassageEvidenceAssociation,
    ) -> bool:
        return self._calibrate_continuous_session_at_position(
            event,
            pane,
            int(association.position_ms),
            int(association.confirmed_at_ms),
        )

    def _calibrate_continuous_session_at_position(
        self,
        event: PassageEvent,
        pane: PassageEvidencePane,
        position_ms: int,
        calibrated_at_ms: int,
    ) -> bool:
        location = pane.location
        if (
            pane.source_kind != REGULAR_SOURCE
            or location is None
            or location.segment.media_started_at_ms is None
        ):
            return False
        key = (
            location.segment.camera_index,
            self._recording_session_key(location),
        )
        if key in self._continuous_clock_offsets or self.calibration_store.get(*key) is not None:
            return False
        offset_ms = (
            int(location.segment.media_started_at_ms)
            + int(position_ms)
            - int(event.timeline_timestamp_ms)
        )
        self._continuous_clock_offsets[key] = offset_ms
        anchor_frame = {
            "segment_id": str(location.segment.segment_id),
            "position_ms": int(position_ms),
        }
        try:
            self.calibration_store.record(
                camera_index=location.segment.camera_index,
                session_key=key[1],
                offset_ms=offset_ms,
                anchor_event_id=event.event_id,
                anchor_bib=event.bib.strip() or "未知",
                calibrated_at_ms=int(calibrated_at_ms),
                anchor_event_revision=int(event.revision),
                anchor_chip_time_ms=int(event.timeline_timestamp_ms),
                anchor_frame=anchor_frame,
                anchor_race_id=event.race_id,
                anchor_stage_id=event.stage_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            logger.warning("failed to persist camera calibration: %s", error)
        self._continuous_calibration_revision += 1
        self._update_batch_controls(event.event_id)
        self._lookup_cache.clear()
        events = self._events_for_current_metadata(self.passage_store.events())
        self._lookups = {
            candidate.event_id: self._cached_lookup(candidate)
            for candidate in events
        }
        self.batch_context_label.setToolTip(
            f"机位 {location.segment.camera_index} 已按首人校准 {offset_ms:+d} ms"
        )
        return True
