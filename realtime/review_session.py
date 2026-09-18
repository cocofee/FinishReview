"""Pure review-session state transitions.

The reducer carries operator intent and media identity without owning a Qt
widget or a decoder.  A late frame is accepted only when it belongs to the
current event and request generation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class ReviewMode(str, Enum):
    FILMSTRIP = "filmstrip"
    JUDGING = "judging"


@dataclass(frozen=True, slots=True)
class FrameSelection:
    source: str
    camera_index: int
    media_id: str
    frame_index: int
    position_ms: int
    request_generation: int = 0


@dataclass(frozen=True, slots=True)
class ReviewSessionState:
    event_id: str = ""
    event_revision: int = 0
    mode: ReviewMode = ReviewMode.FILMSTRIP
    frame: FrameSelection | None = None
    hd_frame: FrameSelection | None = None
    filmstrip_position_ms: int | None = None
    pending_marker: tuple[float, float, int, int] | None = None
    confirmed_revision: int | None = None
    generation: int = 0

    def select_identity(self, event_id: str, revision: int, *, preserve_frame: bool) -> "ReviewSessionState":
        """Select a roster identity while optionally retaining the viewed frame."""
        keep = preserve_frame and self.frame is not None
        return replace(
            self,
            event_id=str(event_id),
            event_revision=int(revision),
            frame=self.frame if keep else None,
            hd_frame=None,
            pending_marker=None,
            confirmed_revision=None if int(revision) != self.event_revision else self.confirmed_revision,
            generation=self.generation + 1,
        )

    def locate_identity(self, event_id: str, revision: int) -> "ReviewSessionState":
        """Start an explicit chip-time locate on the continuous recording."""
        return replace(
            self.select_identity(event_id, revision, preserve_frame=False),
            mode=ReviewMode.JUDGING,
        )

    def enter_judging(self, frame: FrameSelection | None = None) -> "ReviewSessionState":
        return replace(self, mode=ReviewMode.JUDGING, frame=frame or self.frame)

    def return_to_filmstrip(self, position_ms: int | None = None) -> "ReviewSessionState":
        """Keep the browse position while releasing the full-resolution frame."""
        return replace(
            self,
            mode=ReviewMode.FILMSTRIP,
            filmstrip_position_ms=(
                self.filmstrip_position_ms if position_ms is None else int(position_ms)
            ),
            hd_frame=None,
        )

    def request_frame(self, frame: FrameSelection) -> "ReviewSessionState":
        return replace(self, frame=frame, generation=self.generation + 1)

    def accept_frame(self, frame: FrameSelection, *, event_id: str, request_generation: int) -> "ReviewSessionState":
        """Apply a decoder result only if it still targets the active identity."""
        if str(event_id) != self.event_id or int(request_generation) != self.generation:
            return self
        return replace(self, frame=frame)

    def accept_hd_frame(self, frame: FrameSelection, *, event_id: str, request_generation: int) -> "ReviewSessionState":
        if self.mode is not ReviewMode.JUDGING:
            return self
        accepted = self.accept_frame(frame, event_id=event_id, request_generation=request_generation)
        if accepted is self:
            return self
        return replace(accepted, hd_frame=frame)

    def set_marker(self, marker: tuple[float, float, int, int] | None) -> "ReviewSessionState":
        return replace(self, pending_marker=marker)

    def confirm(self) -> "ReviewSessionState":
        if not self.event_id or self.pending_marker is None:
            return self
        return replace(self, confirmed_revision=self.event_revision, pending_marker=None)

    def clear_selection(self) -> "ReviewSessionState":
        return replace(self, event_id="", event_revision=0, frame=None, hd_frame=None,
                       pending_marker=None, confirmed_revision=None,
                       generation=self.generation + 1)
