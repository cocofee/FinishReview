from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from realtime.review_selection import ReviewSelectionContext, ReviewSelectionController


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


def _selection_context(location):
    """Standalone services, with no review window or Qt objects."""
    pane = _Pane(1, location)
    state = SimpleNamespace(
        events={"new": SimpleNamespace(event_id="new", revision=1)},
        lookups={"new": {1: location}},
        panes=[pane],
        active=pane,
        high_speed=_Pane(99),
        batch=False,
        selected="old",
    )
    context = ReviewSelectionContext(
        event_for_id=lambda event_id: state.events.get(event_id),
        lookup_for_id=lambda event_id: state.lookups.get(event_id),
        regular_panes=lambda: state.panes,
        evidence_panes=lambda: state.panes,
        high_speed_pane=lambda: state.high_speed,
        active_pane=lambda: state.active,
        batch_mode=lambda: state.batch,
        selected_event_id=lambda: state.selected,
        regular_location_for_camera=lambda lookup, camera: lookup.get(camera),
        regular_summary_location=lambda _event_id, lookup: lookup.get(1),
        location_on_current_media=lambda _event, pane: pane.location,
        active_playback_pane=lambda: state.panes[0],
        show_high_speed_pane=lambda: False,
        apply_selection_plan=Mock(),
        refresh=Mock(),
        continuous_lookup_for_camera=Mock(return_value={1: location}),
        clear_active_video_identity=Mock(),
    )
    controller = ReviewSelectionController(
        context=context,
        high_speed_location=lambda _lookup: None,
        openable_statuses=frozenset({"located"}),
    )
    return state, context, controller


def test_context_selection_keeps_current_media_and_reads_replaced_data(tmp_path):
    video_path = tmp_path / "continuous.mkv"
    video_path.touch()
    location = _location(video_path)
    state, context, controller = _selection_context(location)
    # Refresh and race activation replace containers rather than mutate them.
    state.events = {"new": SimpleNamespace(event_id="new", revision=2)}
    state.lookups = {"new": {1: _location(tmp_path / "other.mkv")}}
    pane = state.panes[0]

    controller.select("new", preserve_current_frame=pane)

    plan = context.apply_selection_plan.call_args.args[0]
    assert plan.event is state.events["new"]
    assert plan.lookup is state.lookups["new"]
    assert plan.regular_locations[1] is location
    assert plan.preserve_media is True
    assert plan.locate_target is False
    assert pane.location is location
    assert state.selected == "old"  # Only the application callback changes identity.
    context.apply_selection_plan.assert_called_once_with(
        plan, preserve_current_frame=pane,
    )
    context.clear_active_video_identity.assert_called_once_with()
    context.continuous_lookup_for_camera.assert_not_called()
    context.refresh.assert_not_called()


def test_context_locate_uses_continuous_recording_and_current_panes(tmp_path):
    path = tmp_path / "continuous.mkv"
    path.touch()
    location = _location(path)
    state, context, controller = _selection_context(location)
    state.panes = [_Pane(1, location)]
    state.active = state.high_speed
    state.batch = True
    state.selected = "new"
    state.lookups = {"new": {1: _location(tmp_path / "saved-evidence.mkv")}}

    plan = controller.prepare(
        state.events["new"], state.lookups["new"],
        preserve_current_frame=state.panes[0], locate_target=True,
    )

    assert plan.active_pane is state.panes[0]
    assert plan.regular_locations[1] is location
    assert plan.preserve_media is False
    assert plan.reuse_continuous_media is False
    context.continuous_lookup_for_camera.assert_called_once_with(state.events["new"], 1)
    context.apply_selection_plan.assert_not_called()
    context.clear_active_video_identity.assert_not_called()


@pytest.mark.parametrize("missing", ["events", "lookups"])
def test_context_missing_selection_refreshes_without_applying_plan(tmp_path, missing):
    state, context, controller = _selection_context(_location(tmp_path / "missing.mkv"))
    setattr(state, missing, {})

    controller.select("new")

    context.refresh.assert_called_once_with()
    context.apply_selection_plan.assert_not_called()
    context.continuous_lookup_for_camera.assert_not_called()


def test_legacy_review_keyword_reads_replaced_lookup_and_store(tmp_path):
    path = tmp_path / "continuous.mkv"
    path.touch()
    review = _Review({1: _location(path)})
    review.passage_store = {}
    review._lookups = {}
    review._apply_selection_plan = Mock()
    review.refresh = Mock()
    controller = ReviewSelectionController(
        review=review,
        high_speed_location=lambda _lookup: None,
        openable_statuses=frozenset({"located"}),
    )
    review.passage_store = {"new": SimpleNamespace(event_id="new", revision=3)}
    review._lookups = {"new": object()}

    controller.select("new")

    plan = review._apply_selection_plan.call_args.args[0]
    assert plan.event is review.passage_store["new"]
    assert plan.lookup is review._lookups["new"]
    review.refresh.assert_not_called()


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


def test_prepare_locate_target_does_not_preserve_current_media(tmp_path):
    video_path = tmp_path / "continuous.mkv"
    video_path.touch()
    location = _location(video_path)
    stale_location = _location(tmp_path / "stale.mkv")
    review = _Review({1: location}, batch_mode=True)
    review.regular_panes[0].location = location
    review._selected_event_id = "new"
    review._location_on_current_media = lambda _event, _pane: stale_location
    controller = ReviewSelectionController(
        review,
        high_speed_location=lambda _lookup: None,
        openable_statuses=frozenset({"located"}),
    )

    plan = controller.prepare(
        SimpleNamespace(event_id="new"),
        SimpleNamespace(),
        preserve_current_frame=review.regular_panes[0],
        locate_target=True,
    )

    assert plan.regular_locations[1] is location
    assert plan.reuse_continuous_media is False
    assert plan.same_batch_media is False
    assert plan.preserve_media is False
