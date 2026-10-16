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
