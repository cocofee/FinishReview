"""The people the analysis saw cross the line, where each stood, and the chips they can be labelled with.

A wave whose picture holds exactly as many people as it has final-lap chips
can be labelled by itself: the chips in time order take the people in the
order they crossed (see lap_wave.match_wave for every condition).  The
labels are a shortcut for the operator and nothing more: they are never
saved, never exported and never a confirmation; the operator looks the wave
through and presses Enter, which counts those people as if they had been
clicked.  CycleRace keeps the official time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .bunch_review import CROSSINGS_NAME, detected_crossings
from .clock_sync import CLOCK_MODEL_FILENAME
from .crossing_time import MANUAL_CALIBRATION_FILENAME, recorder_crossing_time, workspace_clocks
from .lap_wave import WaveMatch, match_wave

STRIPS_DIR = "finish_problems/strips/camera_{:02d}"
# A person is taken for a wave when they cross this close, in chip time, to its
# first or last chip (the same reach the chip/crossing pairing uses).
WAVE_REACH_MS = 700


@dataclass(frozen=True)
class DetectedPerson:
    """One person the analysis saw cross the line, on the frame they were seen on."""

    key: str
    camera: int
    recorder_ms: int
    chip_ms: int  # the crossing in chip time, through the clock model
    x: float  # where they stood on the frame, 0..1
    y: float
    in_obstruction: bool
    aligned: bool  # chip time through a clock fit, not raw recorder time
    frame: dict  # what a click on this person would carry (see QuickReviewPanel.add_mark)


_GEOMETRY: dict[tuple, dict | None] = {}


def _geometry(path: Path):
    """The line's pixel points and the recording's identity, from one strip file (cached by file stamp)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if stamp not in _GEOMETRY:
        try:
            with np.load(path, allow_pickle=False) as data:
                meta = json.loads(bytes(data["meta"]).decode("utf-8"))
                found = {"xs": data["xs"].copy(), "ys": data["ys"].copy(), "meta": meta}
        except (OSError, KeyError, ValueError):
            found = None
        if len(_GEOMETRY) > 64:
            _GEOMETRY.clear()
        _GEOMETRY[stamp] = found
    return _GEOMETRY[stamp]


def detected_people(workspace, camera, *, clocks=None) -> list[DetectedPerson]:
    """Everyone detected on CAMERA's line, in chip time, with where they stood.

    A detection without its strip file (so without a place on the frame) is
    left out: it could be counted, not shown.  Empty when nothing was analysed.
    """
    workspace = Path(workspace)
    entries = detected_crossings(workspace, camera)
    if not entries:
        return []
    clocks = clocks if clocks is not None else workspace_clocks(workspace)
    folder = workspace / STRIPS_DIR.format(int(camera))
    people = []
    for entry in entries:
        try:
            video, row = str(entry["video_path"]), int(entry["row"])
            recorder_ms, frame_index = int(entry["recorder_ms"]), int(entry["frame_index"])
            session = str(entry["session_id"])
        except (KeyError, TypeError, ValueError):
            continue
        found = _geometry(folder / (Path(video).stem + ".npz"))
        if found is None:
            continue
        meta, xs, ys = found["meta"], found["xs"], found["ys"]
        width, height = int(meta.get("width") or 0), int(meta.get("height") or 0)
        if not width or not height or not 0 <= row < xs.size:
            continue
        frame = {"camera_index": int(camera), "segment_id": str(meta.get("segment_id", "")), "video_path": video,
                 "frame_index": frame_index, "session_id": session, "recorder_ms": recorder_ms}
        start = meta.get("media_started_at_ms")
        if start is not None:
            frame["position_ms"] = recorder_ms - int(start)
        crossing = recorder_crossing_time(recorder_ms, frame, workspace, clocks=clocks)
        if crossing is None:
            continue
        people.append(DetectedPerson(
            f"{int(camera)}|{video}|{frame_index}|{row}", int(camera), recorder_ms, int(crossing.value_ms),
            float(xs[row]) / width, float(ys[row]) / height, bool(entry.get("in_obstruction")),
            crossing.source != "video", frame))
    people.sort(key=lambda person: person.chip_ms)
    return people


_PEOPLE: dict[tuple, list[DetectedPerson]] = {}


def _stamp(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def people_on_line(workspace, camera) -> list[DetectedPerson]:
    """detected_people, read again only when the detections or the clocks change on disk."""
    workspace = Path(workspace)
    key = (str(workspace), int(camera), _stamp(workspace / CROSSINGS_NAME.format(int(camera))),
           _stamp(workspace / CLOCK_MODEL_FILENAME), _stamp(workspace / MANUAL_CALIBRATION_FILENAME))
    if key[2] is None:
        return []
    if key not in _PEOPLE:
        if len(_PEOPLE) > 4:
            _PEOPLE.clear()
        _PEOPLE[key] = detected_people(workspace, camera)
    return _PEOPLE[key]


def people_of_wave(people, first_chip_ms: int, last_chip_ms: int, reach_ms: int = WAVE_REACH_MS):
    """The people who crossed from REACH before a wave's first chip to REACH after its last."""
    return [person for person in people if first_chip_ms - reach_ms <= person.chip_ms <= last_chip_ms + reach_ms]


def label_wave(
    chips: list[tuple[str, int]],
    people: list[DetectedPerson],
    *,
    clock_ok: bool,
    short_rider_count: int = 0,
    maybe_several: bool = False,
    near_counted: bool = False,
) -> WaveMatch:
    """Pair a wave's final-lap CHIPS ((key, chip ms)) with the PEOPLE seen crossing for it, or say why not.

    The pairs are (chip key, person key).  A person seen with the view blocked
    cannot be trusted to be one person, so the wave is left to hand.
    """
    near = people_of_wave(people, min(time for _key, time in chips), max(time for _key, time in chips)) if chips else []
    return match_wave(
        chips, [(person.key, person.chip_ms) for person in near],
        clock_ok=clock_ok and all(person.aligned for person in near), short_rider_count=short_rider_count, maybe_several=maybe_several,
        near_counted=near_counted, obstructed=any(person.in_obstruction for person in near))


__all__ = ["DetectedPerson", "WAVE_REACH_MS", "detected_people", "label_wave", "people_of_wave", "people_on_line"]
