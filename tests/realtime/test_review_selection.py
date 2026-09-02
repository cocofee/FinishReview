from pathlib import Path
from types import SimpleNamespace

from realtime.review_selection import ReviewSelectionController


class _Pane:
    def __init__(self, camera_index, location=None):
        self.camera_index = camera_index
        self.location = location

    def _media_context(self, location):
        return None if location is None else Path(location.video_path)

    def matches_passage_context(self, _event, location):
        return self.location == location


class _Review:
    def __init__(self, locations, *, batch_mode=False):
        self._locations = locations
        self._batch_mode = batch_mode
        self._selected_event_id = "old"
        self._show_high_speed_pane = False
        self.regular_panes = [_Pane(index) for index in sorted(locations)]
        self.high_speed_pane = _Pane(99)
        self.evidence_panes = [*self.regular_panes, self.high_speed_pane]
        self._active_pane = self.regular_panes[0]

    def _regular_location_for_camera(self, _lookup, camera_index):
        return self._locations[camera_index]

    def _regular_summary_location(self, _event_id, _lookup):
        return self._locations[min(self._locations)]

    def _active_playback_pane(self):
        return self.regular_panes[0]

    def _location_on_current_media(self, _event, _pane):
        return None


def _location(path, status="located"):
    return SimpleNamespace(video_path=Path(path), status=status)


def test_prepare_falls_back_to_first_openable_camera(tmp_path):
    missing = _location(tmp_path / "missing.mkv", "missing_file")
    available_path = tmp_path / "available.mkv"
    available_path.touch()
    available = _location(available_path)
    review = _Review({1: missing, 2: available})
    controller = ReviewSelectionController(
        review,
        high_speed_location=lambda _lookup: None,
        openable_statuses=frozenset({"located"}),
    )

    plan = controller.prepare(
        SimpleNamespace(event_id="new"),
        SimpleNamespace(),
    )

    assert plan.active_pane is review.regular_panes[1]
    assert plan.switching_batch_event is False
    assert plan.preserve_media is False


def test_prepare_reuses_same_media_during_batch_switch(tmp_path):
    video_path = tmp_path / "continuous.mkv"
    video_path.touch()
    location = _location(video_path)
    review = _Review({1: location}, batch_mode=True)
    review.regular_panes[0].location = location
    controller = ReviewSelectionController(
        review,
        high_speed_location=lambda _lookup: None,
        openable_statuses=frozenset({"located"}),
    )

    plan = controller.prepare(
        SimpleNamespace(event_id="new"),
        SimpleNamespace(),
    )

    assert plan.reuse_continuous_media is True
    assert plan.same_batch_media is True
    assert plan.preserve_media is True
    assert plan.switching_batch_event is True
