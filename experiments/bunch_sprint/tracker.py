"""Conservative rider candidates with stable IDs and explicit association uncertainty.

No labels or expected counts are inputs. A merged bicycle is evidence for each
compatible person, never an additional rider. Ambiguous matches are not certified.
"""

from dataclasses import dataclass, field
from math import hypot

import cv2
import numpy as np

VERSION = 'rider-evidence-v1'
LINE_X = .518


def centre(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def coverage(inner, outer):
    area = max(0, inner[2] - inner[0]) * max(0, inner[3] - inner[1])
    intersection = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0])) * max(
        0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    return intersection / max(area, 1)


def road(box, width, height):
    # Spectators/banner occupy the top strip; do not relax confidence globally.
    return box[3] > .17 * height and box[2] - box[0] > .03 * width


def bicycle_admitted(box, width, height, near=True):
    bw = box[2] - box[0]
    strict = box[4] >= .30 and bw < .45 * width
    close = near and box[4] >= .10 and .35 * width <= bw < .60 * width
    return road(box, width, height) and (strict or close)


def appearance(frame, box):
    x0, y0, x1, y1 = box[:4]
    # Use central upper body; wheels and road must not dominate the descriptor.
    bw, bh = x1 - x0, y1 - y0
    xa, xb = max(0, int(x0 + .15 * bw)), min(frame.shape[1], int(x1 - .15 * bw))
    ya, yb = max(0, int(y0 + .10 * bh)), min(frame.shape[0], int(y0 + .60 * bh))
    crop = frame[ya:yb, xa:xb]
    if not crop.size:
        return np.zeros(64, np.float32)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 2], None, [8, 8], [0, 180, 0, 256]).ravel()
    return hist / max(float(hist.sum()), 1)


def fuse(people, bicycles, frame):
    height, width = frame.shape[:2]
    accepted = [list(b) for b in people if b[4] >= .25 and road(b, width, height)
                and b[2] - b[0] < .60 * width]
    kept, suppressed = [], []
    for box in sorted(accepted, key=lambda b: -b[4]):
        duplicate = None
        for other in kept:
            bw = min(box[2] - box[0], other[2] - other[0])
            bh = max(box[3] - box[1], other[3] - other[1])
            if (abs(centre(box)[0] - centre(other)[0]) < .12 * bw
                    and abs(box[3] - other[3]) < .08 * bh
                    and max(coverage(box, other), coverage(other, box)) > .90):
                duplicate = other
                break
        if duplicate is not None:
            suppressed.append({'box': box, 'kept': duplicate, 'reason': 'nested_same_axis'})
        else:
            kept.append(box)
    observations = [{'box': b, 'kind': 'person', 'bicycles': [], 'merged_bike': False}
                    for b in kept]
    merged = []
    for bike in bicycles:
        if not bicycle_admitted(bike, width, height):
            continue
        associated = []
        for i, obs in enumerate(observations):
            person = obs['box']
            cx, cy = centre(person)
            if (bike[0] - .1 * (bike[2] - bike[0]) <= cx <= bike[2]
                    and person[3] >= bike[1] and person[1] <= bike[3]
                    and cy <= bike[3]):
                associated.append(i)
        if associated:
            for i in associated:
                observations[i]['bicycles'].append(list(bike))
                observations[i]['merged_bike'] |= len(associated) > 1
            if len(associated) > 1:
                merged.append({'bike': list(bike), 'person_indices': associated})
        else:
            # A fallback is tentative: bike centre is not interchangeable with torso centre.
            observations.append({'box': list(bike), 'kind': 'bicycle',
                                 'bicycles': [list(bike)], 'merged_bike': False})
    for obs in observations:
        obs['point'] = centre(obs['box'])
        obs['appearance'] = appearance(frame, obs['box'])
    return observations, {'suppressed': suppressed, 'merged': merged}


@dataclass
class Track:
    id: str
    observations: list = field(default_factory=list)
    velocity: tuple = (0., 0.)
    counted: bool = False
    uncertain: bool = False
    descriptor: object = None

    @property
    def last(self):
        return self.observations[-1]


class RiderTracker:
    def __init__(self, prefix, width, fps, baseline=False):
        self.prefix, self.width, self.fps = prefix, width, fps
        self.baseline = baseline
        self.tracks, self.active, self.events, self.audit = [], [], [], []
        self.previous_frame = None

    def update(self, index, observations, frame):
        self.active = [t for t in self.active if index - t.last['frame'] <= (12 if self.baseline else 4)]
        costs = []
        for ti, track in enumerate(self.active):
            gap = index - track.last['frame']
            x, y = track.last['point']
            px, py = x + track.velocity[0] * gap, y + track.velocity[1] * gap
            for oi, obs in enumerate(observations):
                cx, cy = obs['point']
                distance = hypot(cx - px, cy - py)
                if self.baseline:
                    cost = distance
                    admitted = distance <= 120 + 60 * gap
                else:
                    old = track.last
                    bh = max(old['box'][3] - old['box'][1], obs['box'][3] - obs['box'][1])
                    colour = float(np.abs(track.descriptor - obs['appearance']).sum()) / 2
                    # Reject reverse reacquisitions and class-anchor changes.
                    admitted = (distance < 200 + 30 * (gap - 1) and abs(cy - y) < max(100, .35 * bh)
                                and (len(track.observations) < 2 or cx >= x - 45)
                                and old['kind'] == obs['kind'] and colour < .80)
                    cost = distance + colour * 100
                if admitted:
                    costs.append((cost, ti, oi))
        costs.sort()
        used_t, used_o = set(), set()
        for cost, ti, oi in costs:
            if ti in used_t or oi in used_o:
                continue
            track, obs = self.active[ti], observations[oi]
            gap = index - track.last['frame']
            ambiguous = False
            if not self.baseline:
                alternatives = [c for c, t, o in costs if (t == ti and o != oi and o not in used_o)
                                or (o == oi and t != ti and t not in used_t)]
                ambiguous = bool(alternatives and min(alternatives) - cost < 25)
                if ambiguous:
                    self.audit.append({'frame': index, 'track': track.id, 'reason': 'ambiguous_association',
                                       'observation': oi, 'cost': cost})
                    track.uncertain = True
            used_t.add(ti)
            used_o.add(oi)
            previous = track.last
            cx, cy = obs['point']
            vx = (cx - previous['point'][0]) / gap
            vy = (cy - previous['point'][1]) / gap
            line = self.width * LINE_X
            if previous['point'][0] < line <= cx and not track.counted and len(track.observations) >= 2:
                fraction = (line - previous['point'][0]) / (cx - previous['point'][0])
                forward = vx > 0 and (self.baseline or track.velocity[0] > 0)
                if forward:
                    uncertain = track.uncertain or gap > 1 or obs['kind'] == 'bicycle' and not self.baseline
                    self.events.append({'id': f'{track.id}:crossing', 'track_id': track.id,
                                        'frame_before': previous['frame'], 'frame_after': index,
                                        'frame_position': previous['frame'] + fraction * gap,
                                        'seconds': (previous['frame'] + fraction * gap) / self.fps,
                                        'gap': gap, 'kind': obs['kind'], 'direction': 'forward',
                                        'status': 'tentative' if uncertain else 'observed_candidate'})
                    track.counted = True
            track.velocity = (vx, vy)
            self._observe(track, index, obs, gap, ambiguous)
        for oi, obs in enumerate(observations):
            if oi not in used_o:
                track = Track(f'{self.prefix}:T{len(self.tracks) + 1:04d}')
                self._observe(track, index, obs, 0, False)
                self.tracks.append(track)
                self.active.append(track)

    @staticmethod
    def _observe(track, index, obs, gap, ambiguous):
        track.observations.append({'frame': index, 'box': obs['box'], 'point': list(obs['point']),
                                   'kind': obs['kind'], 'gap': gap, 'ambiguous': ambiguous,
                                   'merged_bike': obs.get('merged_bike', False),
                                   'bicycles': obs.get('bicycles', [])})
        descriptor = obs.get('appearance')
        if descriptor is not None:
            track.descriptor = descriptor if track.descriptor is None else .7 * track.descriptor + .3 * descriptor

    def result(self):
        return {'version': 'bicycle-cv-growing-v1' if self.baseline else VERSION,
                'tracks': [{'id': t.id, 'uncertain': t.uncertain, 'observations': t.observations}
                           for t in self.tracks], 'events': self.events, 'audit': self.audit,
                'line': {'x_fraction': LINE_X, 'anchor': 'box-centre proxy; not front-wheel finish timing'}}
