"""Reproduce interval experiments without importing reference code or race truth."""

import argparse
import hashlib
import json
from pathlib import Path

import cv2

from .data import OUTPUT, cache, capture, video
from .tracker import RiderTracker, bicycle_admitted, centre, fuse

GROUPS = [
    {'id': 'G08A', 'segment': '0008', 'first': 1208, 'last': 1244, 'split': 'development'},
    {'id': 'G08B', 'segment': '0008', 'first': 1245, 'last': 1310, 'split': 'development'},
    {'id': 'G08C', 'segment': '0008', 'first': 1315, 'last': 1340, 'split': 'development'},
    {'id': 'G07A', 'segment': '0007', 'first': 150, 'last': 193, 'split': 'held_out'},
    {'id': 'G09A', 'segment': '0009', 'first': 1666, 'last': 1715, 'split': 'held_out'},
    {'id': 'G11A', 'segment': '0011', 'first': 1260, 'last': 1325, 'split': 'held_out'},
]


def run(group, highres=None):
    segment = group['segment']
    people, meta, person_path = cache(segment, 0)
    bikes, bike_meta, bike_path = cache(segment, 1)
    if meta != bike_meta or len(people) != len(bikes):
        raise ValueError('Cache metadata/length mismatch')
    if highres:
        regenerated = json.loads(Path(highres).read_text(encoding='utf-8'))
        if regenerated['meta']['source'] != str(video(segment)):
            raise ValueError('Regenerated cache source mismatch')
    cap = capture(segment)
    if int(cap.get(3)) != meta['width'] or int(cap.get(4)) != meta['height'] or cap.get(5) != meta['fps']:
        raise ValueError('Cache/source media mismatch')
    cap.set(cv2.CAP_PROP_POS_FRAMES, group['first'])
    baseline = RiderTracker(group['id'] + ':baseline', meta['width'], meta['fps'], baseline=True)
    fused = RiderTracker(group['id'] + ':fused', meta['width'], meta['fps'])
    fusion_audit = []
    for index in range(group['first'], group['last'] + 1):
        ok, frame = cap.read()
        if not ok or abs(cap.get(cv2.CAP_PROP_POS_FRAMES) - index - 1) > .5:
            raise ValueError(f'Unexpected decode at {index}')
        bicycle = [{'box': list(b), 'kind': 'bicycle', 'point': centre(b)} for b in bikes[index]
                   if bicycle_admitted(b, meta['width'], meta['height'], near=False)]
        baseline.update(index, bicycle, frame)
        p, b = people[index], bikes[index]
        if highres:
            p, b = regenerated['frames'][str(index)]['0'], regenerated['frames'][str(index)]['1']
        instances, audit = fuse(p, b, frame)
        fusion_audit.append({'frame': index, **audit})
        fused.update(index, instances, frame)
    cap.release()
    result = {'group': group, 'video': str(video(segment)), 'fps': meta['fps'],
              'inputs': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in (person_path, bike_path)],
              'baseline': baseline.result(), 'fused': fused.result(), 'fusion_audit': fusion_audit}
    if highres:
        result['regenerated_input'] = str(Path(highres).resolve())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    out = OUTPUT / f'{group["id"]}{"_highres" if highres else ""}_results.json'
    out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'{group["id"]}: bicycle={len(baseline.events)} fused={len(fused.events)} '
          f'tentative={sum(e["status"] == "tentative" for e in fused.events)} -> {out}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--group', choices=[g['id'] for g in GROUPS])
    parser.add_argument('--highres', type=Path)
    args = parser.parse_args()
    for group in GROUPS:
        if args.group is None or args.group == group['id']:
            run(group, args.highres)
