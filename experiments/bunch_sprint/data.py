"""Read only timeline locations, original video and versioned detector caches."""

import json
import os
import pickle
from pathlib import Path

import cv2

RACE = Path(os.environ.get('BUNCH_SPRINT_RACE', 'race'))
REFERENCE = Path(os.environ.get('BUNCH_SPRINT_REFERENCE', '../finish_probe'))
WEIGHTS = Path(os.environ.get('BUNCH_SPRINT_WEIGHTS', '../VideoPipe/yolo26n.pt'))
OUTPUT = Path(__file__).resolve().parents[2] / 'artifacts' / 'bunch_sprint'


def videos(race=RACE):
    locations = {}
    for line in (race / 'video_timeline.jsonl').read_text(encoding='utf-8').splitlines():
        record = json.loads(line)
        if record.get('record_type') != 'segment_started':
            continue
        relative = record['video_path'].replace('\\', '/')
        path = Path(relative)
        path = path if path.is_absolute() else race / path
        locations[path.name] = path.resolve()
    return locations


def video(segment):
    matches = [path for name, path in videos().items()
               if name.startswith('camera_01_20260830_101717_')
               and name.endswith(f'_{segment}.mkv')]
    if len(matches) != 1:
        raise ValueError(f'Expected one video for {segment}: {matches}')
    return matches[0]


def cache(segment, class_id):
    path = REFERENCE / 'yolo_cache' / f'{video(segment).name}.yolo26n.1280.{class_id}.pickle'
    # These are existing, local, trusted experiment files; never accept remote pickle inputs.
    with path.open('rb') as source:
        frames, meta = pickle.load(source)
    if len(frames) == 0 or not all(key in meta for key in ('fps', 'width', 'height')):
        raise ValueError(f'Invalid cache: {path}')
    return frames, meta, path


def capture(segment):
    path = video(segment)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f'Cannot open {path}')
    return cap


def write_image(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix, frame)
    if not ok:
        raise ValueError(f'Cannot encode {path}')
    path.write_bytes(encoded.tobytes())
