"""The people the analysis saw cross the line, where each stood, and the chips they can be labelled with.

A wave whose picture holds exactly as many people as it has passage chips
can be labelled by itself only when the chips and crossings also line up
through the time-ordered pairing, one to one and in the same order (see
lap_wave.match_wave for every condition).  The labels are a shortcut for the
operator and nothing more: proposed pairs are not saved or exported and never
confirm a rider's identity. Enter explicitly accepts the bound evidence, with
its automatic origin retained separately from manual clicks or viewing history.
CycleRace keeps the official time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from zipfile import BadZipFile

import numpy as np

from .bunch_review import CROSSINGS_NAME, detected_crossings
from .clock_sync import CLOCK_MODEL_FILENAME
from .crossing_pairing import WINDOW_MS
from .crossing_time import MANUAL_CALIBRATION_FILENAME, recorder_crossing_time, workspace_clocks
from .lap_wave import WaveMatch, match_wave

STRIPS_DIR = "finish_problems/strips/camera_{:02d}"
# A person is taken for a wave when they cross this close, in chip time, to its
# first or last chip (the same reach the chip/crossing pairing uses).
WAVE_REACH_MS = int(WINDOW_MS)


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
        except (OSError, EOFError, BadZipFile, KeyError, ValueError):
            # A strip can be incomplete while analysis is copying it. Skip this
            # file for now; one bad camera file must not stop the review table.
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
_STRIP_STAMPS: dict[tuple, tuple[float, tuple]] = {}
# 原地修复不会可靠改变目录时间；最多每秒核对一次，避免每次 UI 刷新都扫描。
STRIP_RECHECK_SECONDS = 1.0


def _stamp(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def people_on_line(workspace, camera) -> list[DetectedPerson]:
    """detected_people, read again when detections, strips or clocks change on disk."""
    workspace = Path(workspace)
    crossings_stamp = _stamp(workspace / CROSSINGS_NAME.format(int(camera)))
    entries = detected_crossings(workspace, camera)
    if not entries:
        return []
    strip_dir = workspace / STRIPS_DIR.format(int(camera))
    stamp_key = (str(workspace), int(camera), crossings_stamp, _stamp(strip_dir))
    now = monotonic()
    checked = _STRIP_STAMPS.get(stamp_key)
    if checked is None or now - checked[0] >= STRIP_RECHECK_SECONDS:
        videos = {str(entry.get("video_path", "")) for entry in entries}
        strip_stamps = tuple(sorted(
            (video, _stamp(strip_dir / (Path(video).stem + ".npz"))) for video in videos
        ))
        if len(_STRIP_STAMPS) > 8:
            _STRIP_STAMPS.clear()
        _STRIP_STAMPS[stamp_key] = (now, strip_stamps)
    else:
        strip_stamps = checked[1]
    key = (str(workspace), int(camera), crossings_stamp, strip_stamps,
           _stamp(workspace / CLOCK_MODEL_FILENAME), _stamp(workspace / MANUAL_CALIBRATION_FILENAME))
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
    """Pair a wave's passage CHIPS ((key, chip ms)) with the PEOPLE seen crossing for it, or say why not.

    The pairs are (chip key, person key).  A person seen with the view blocked
    cannot be trusted to be one person, so the wave is left to hand.
    """
    near = people_of_wave(people, min(time for _key, time in chips), max(time for _key, time in chips)) if chips else []
    return match_wave(
        chips, [(person.key, person.chip_ms) for person in near],
        clock_ok=clock_ok and all(person.aligned for person in near), short_rider_count=short_rider_count, maybe_several=maybe_several,
        near_counted=near_counted, obstructed=any(person.in_obstruction for person in near))


__all__ = ["DetectedPerson", "WAVE_REACH_MS", "detected_people", "label_wave", "people_of_wave", "people_on_line"]
