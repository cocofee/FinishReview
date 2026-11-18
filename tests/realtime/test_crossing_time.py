from dataclasses import replace
from types import SimpleNamespace

from realtime.clock_sync import ClockModel
from realtime.crossing_time import item_crossing_time, recorder_crossing_time
from realtime.problem_items import CATEGORY_VIDEO, KIND_BUNCH, KIND_MANUAL_REVIEW, ProblemItem


T = 1_789_784_000_000


def model(camera=1, offset=1000, version=1, **extra):
    return ClockModel(camera, offset, 0, T, T, T + 20_000, 10, 10, 0, offset, 10, version=version, **extra)


class Clocks:
    def __init__(self, *models):
        self.models = {(value.camera_index, value.version): value for value in models}
        self.latest_calls = []

    def get(self, camera, version):
        return self.models.get((camera, version))

    def latest(self, camera):
        self.latest_calls.append(camera)
        available = [value for value in self.models.values() if value.camera_index == camera]
        return max(available, key=lambda value: value.version) if available else None


def item(identity, stamp=None, camera=1, version=1, **evidence):
    frame = dict(camera_index=camera, session_id="session")
    return ProblemItem(identity, KIND_MANUAL_REVIEW, CATEGORY_VIDEO, identity,
                       calibration_version=version,
                       evidence=dict(frame=frame, **({"recorder_ms": stamp} if stamp is not None else {}), **evidence))


def test_actual_crossing_order_includes_manual_riders_without_chip_time():
    entries = [item("later", T + 1300), item("first", T + 1100), item("manual", T + 1200)]
    untouched = [entry.to_payload() for entry in entries]
    clocks = Clocks(model())
    ordered = sorted(entries, key=lambda entry: item_crossing_time(entry, clocks=clocks).value_ms)
    assert [entry.item_id for entry in ordered] == ["first", "manual", "later"]
    assert all(item_crossing_time(entry, clocks=clocks).source == "aligned" for entry in entries)
    assert [entry.to_payload() for entry in entries] == untouched
    assert all(entry.chip_time_ms is None for entry in entries)


def test_each_camera_uses_the_evidence_version_and_session_offset_once():
    first = model(offset=1000, session_offsets=(("session", 400),))
    second = model(camera=2, offset=5000)
    clocks = Clocks(first, second, model(offset=8000, version=2), model(camera=2, offset=9000, version=2))
    one = item("one", T + 1600)
    two = item("two", T + 5200, camera=2)
    assert item_crossing_time(one, clocks=clocks).value_ms == T + 200
    assert item_crossing_time(two, clocks=clocks).value_ms == T + 200
    assert clocks.latest_calls == []


def test_a_missing_pinned_clock_cannot_use_a_newer_fit():
    clocks = Clocks(model(version=2))
    pinned = item("pinned", T + 1200)
    value = item_crossing_time(pinned, clocks=clocks)
    assert value.value_ms == T + 1200 and value.source == "video"
    assert clocks.latest_calls == []
    unversioned = replace(pinned, calibration_version=0)
    assert item_crossing_time(unversioned, clocks=clocks).value_ms == T + 200
    assert clocks.latest_calls == [1]


def test_live_first_rider_calibration_converts_before_auto_model(tmp_path):
    from realtime.passage_evidence import VideoClockCalibrationStore

    store = VideoClockCalibrationStore(tmp_path / "video_clock_calibrations.jsonl")
    store.record(camera_index=1, session_key="camera_01_review|videos|camera_01_20261018_105720_440309",
                 offset_ms=2138, anchor_event_id="event", anchor_bib="902", calibrated_at_ms=1)
    frame = {
        "camera_index": 1,
        "video_path": r"D:\录像\race\review_buffer\camera_01\camera_01_20261018_105720_440309_review_0000000000000019.ts",
        "recorder_ms": T + 5000,
    }
    value = recorder_crossing_time(T + 5000, frame, workspace=tmp_path)
    assert value.value_ms == T + 2862 and value.source == "aligned"


def test_context_resolves_the_clicked_segment_session_instead_of_group_anchor():
    fit = model(session_offsets=(("second", 400),))
    segment = SimpleNamespace(segment=SimpleNamespace(segment_id="b"), session_id="second")
    context = SimpleNamespace(model=fit, camera=1, segments=(segment,))
    frame = {"camera_index": 1, "segment_id": "b"}
    assert recorder_crossing_time(T + 1600, frame, context=context).value_ms == T + 200
    derived = {"camera_index": 1, "calibration_version": 1, "video_path": "second_archive_0003.mkv"}
    assert recorder_crossing_time(T + 1600, derived, clocks=Clocks(fit)).value_ms == T + 200
    drift = replace(fit, slope=.0001)
    stamp = drift.to_recorder(T + 10_000, "second")
    assert recorder_crossing_time(stamp, frame, context=replace_context(context, drift)).value_ms == T + 10_000


def replace_context(context, fit):
    return SimpleNamespace(model=fit, camera=context.camera, segments=context.segments)


def test_a_group_starts_at_first_observed_rider_and_fallbacks_are_labelled():
    observed = replace(item("group", T + 1200, recorder_start_ms=T - 500), kind=KIND_BUNCH)
    clocks = Clocks(model())
    assert item_crossing_time(observed, clocks=clocks).value_ms == T + 200
    boundary = replace(observed, evidence={"frame": observed.evidence["frame"], "recorder_start_ms": T + 1000})
    assert item_crossing_time(boundary, clocks=clocks).source == "expected"
    official = replace(item("official"), chip_time_ms=T + 99)
    assert item_crossing_time(official, clocks=clocks).value_ms == T + 99
    assert item_crossing_time(official, clocks=clocks).source == "chip"
    assert item_crossing_time(item("untimed"), clocks=clocks) is None


def test_a_click_after_the_fit_rules_at_once_and_a_later_fit_takes_over(tmp_path):
    """11-18 "校完立刻全部生效": table and Space must not wait for the next analysis."""
    from realtime.clock_sync import MANUAL_ANCHOR_SOURCE
    from realtime.passage_evidence import VideoClockCalibrationStore

    session = "camera_01_20261104_112206_629703"
    store = VideoClockCalibrationStore(tmp_path / "video_clock_calibrations.jsonl")
    # An older click under the folder the race was in before it was moved (11-05) ...
    store.record(camera_index=1, session_key=f"camera_01_review|D:\\old\\videos|{session}", offset_ms=1738,
                 anchor_event_id="e20", anchor_bib="20", calibrated_at_ms=T + 10)
    # ... then the operator's new click on the same recording.
    store.record(camera_index=1, session_key=f"camera_01_review|D:\\new\\videos|{session}", offset_ms=2699,
                 anchor_event_id="e1", anchor_bib="1", calibrated_at_ms=T + 50)
    frame = {"camera_index": 1, "session_id": session}
    fitted = model(offset=1738, source=MANUAL_ANCHOR_SOURCE, session_ids=(session,), created_at_ms=T + 20)
    value = recorder_crossing_time(T + 10_000, frame, tmp_path, clocks=Clocks(fitted))
    assert value.value_ms == T + 10_000 - 2699 and value.source == "aligned"

    refitted = model(offset=2600, version=2, created_at_ms=T + 60)
    assert recorder_crossing_time(T + 10_000, frame, tmp_path, clocks=Clocks(refitted)).value_ms == T + 10_000 - 2600
    # A model saved without a creation time keeps its old precedence.
    legacy = model(offset=2600, version=3)
    assert recorder_crossing_time(T + 10_000, frame, tmp_path, clocks=Clocks(legacy)).value_ms == T + 10_000 - 2600
