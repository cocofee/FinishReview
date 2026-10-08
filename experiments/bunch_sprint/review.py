"""Original-frame inspection assets; detection overlays are optional evidence."""

import argparse
import json

import cv2
import numpy as np

from .data import OUTPUT, cache, capture, video, write_image


def sheet(segment, start, end, step, boxes=False, scale=0.5):
    cap = capture(segment)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width, height = int(cap.get(3)), int(cap.get(4))
    detections = {cls: cache(segment, cls)[0] for cls in (0, 1)} if boxes else {}
    targets = list(range(round(start * fps), round(end * fps) + 1, max(1, round(step * fps))))
    tiles = []
    for index in targets:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            continue
        if abs(cap.get(cv2.CAP_PROP_POS_FRAMES) - index - 1) > 0.5:
            raise ValueError('Decoder did not return requested original frame')
        for cls, frames in detections.items():
            for number, box in enumerate(frames[index]):
                if box[4] < (0.20 if cls == 0 else 0.10):
                    continue
                x0, y0, x1, y1 = map(round, box[:4])
                colour = (0, 220, 255) if cls == 0 else (40, 255, 40)
                cv2.rectangle(frame, (x0, y0), (x1, y1), colour, 2)
                cv2.putText(frame, f'{cls}:{number} {box[4]:.2f}', (x0, max(35, y0)),
                            cv2.FONT_HERSHEY_SIMPLEX, .6, colour, 2)
        cv2.rectangle(frame, (0, 0), (750, 55), (0, 0, 0), -1)
        cv2.putText(frame, f'{segment} frame {index} / {index / fps:.2f}s', (15, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        tiles.append(cv2.resize(frame, (round(width * scale), round(height * scale))))
    cap.release()
    columns = 2
    tw, th = round(width * scale), round(height * scale)
    output = np.zeros((((len(tiles) + 1) // columns) * th, tw * columns, 3), np.uint8)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, columns)
        output[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tile
    path = OUTPUT / f'{segment}_{targets[0]}_{targets[-1]}_{"boxes" if boxes else "raw"}.jpg'
    write_image(path, output)
    print(path)


def inspect():
    rows = []
    for segment in ('0008', '0007', '0009', '0011'):
        cap = capture(segment)
        decoded = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        row = {'segment': segment, 'video': str(video(segment)), 'container_frames': decoded,
               'fps': fps, 'caches': {}}
        for cls in (0, 1):
            frames, meta, path = cache(segment, cls)
            row['caches'][cls] = {'path': str(path), 'frames': len(frames), 'meta': meta}
        rows.append(row)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'inputs.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('segment', nargs='?')
    parser.add_argument('--start', type=float, default=48)
    parser.add_argument('--end', type=float, default=56)
    parser.add_argument('--step', type=float, default=.4)
    parser.add_argument('--scale', type=float, default=.5)
    parser.add_argument('--boxes', action='store_true')
    args = parser.parse_args()
    if args.segment:
        sheet(args.segment, args.start, args.end, args.step, args.boxes, args.scale)
    else:
        inspect()
