from dataclasses import replace

from realtime.review_session import FrameSelection, ReviewMode, ReviewSessionState


def frame(index=12, generation=0):
    return FrameSelection("regular", 1, "segment-1", index, index * 40, generation)


def test_identity_click_preserves_frame_but_releases_hd():
    current = ReviewSessionState(event_id="one", event_revision=1, mode=ReviewMode.JUDGING,
                                 frame=frame(), hd_frame=frame(12), generation=4)
    selected = current.select_identity("two", 1, preserve_frame=True)
    assert selected.event_id == "two"
    assert selected.frame == current.frame
    assert selected.hd_frame is None
    assert selected.mode is ReviewMode.JUDGING


def test_locate_is_explicit_and_drops_old_frame():
    current = ReviewSessionState(event_id="one", event_revision=1, frame=frame(), generation=2)
    selected = current.locate_identity("two", 3)
    assert selected.mode is ReviewMode.JUDGING
    assert selected.frame is None
    assert selected.generation == 3


def test_return_to_filmstrip_preserves_position_and_releases_hd():
    current = ReviewSessionState(event_id="one", mode=ReviewMode.JUDGING,
                                 frame=frame(), hd_frame=frame(13))
    returned = current.return_to_filmstrip(880)
    assert returned.mode is ReviewMode.FILMSTRIP
    assert returned.filmstrip_position_ms == 880
    assert returned.hd_frame is None
    assert returned.frame == current.frame


def test_late_frame_cannot_replace_new_identity_or_generation():
    current = ReviewSessionState(event_id="one", generation=4)
    requested = current.request_frame(replace(frame(), request_generation=5))
    assert requested.accept_frame(frame(), event_id="one", request_generation=4) is requested
    assert requested.accept_frame(frame(13), event_id="two", request_generation=5) is requested
    accepted = requested.accept_frame(frame(14), event_id="one", request_generation=5)
    assert accepted.frame.frame_index == 14


def test_confirmation_is_revision_bound():
    current = ReviewSessionState(event_id="one", event_revision=2, pending_marker=(.1, .2, 3, 4))
    confirmed = current.confirm()
    assert confirmed.confirmed_revision == 2
    assert confirmed.pending_marker is None
