"""Optional interval-only extraction with the existing local checkpoint."""

import argparse
import hashlib
import json

import cv2

from .data import OUTPUT, WEIGHTS, capture, video


def extract(segment, first, last, imgsz):
    import torch
    import ultralytics
    from ultralytics import YOLO

    if not WEIGHTS.is_file():
        raise FileNotFoundError(WEIGHTS)
    model = YOLO(str(WEIGHTS))
    cap = capture(segment)
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    frames = {}
    for index in range(first, last + 1):
        ok, frame = cap.read()
        if not ok:
            raise ValueError(f'Decode ended at frame {index}')
        result = model.predict(frame, imgsz=imgsz, conf=.05, classes=[0, 1],
                               device=0 if torch.cuda.is_available() else 'cpu', verbose=False)[0]
        frames[index] = {cls: [] for cls in (0, 1)}
        for box in result.boxes:
            frames[index][int(box.cls.item())].append(
                box.xyxy[0].tolist() + [float(box.conf.item())])
    metadata = {'source': str(video(segment)), 'weights': str(WEIGHTS),
                'weights_sha256': hashlib.sha256(WEIGHTS.read_bytes()).hexdigest(),
                'imgsz': imgsz, 'confidence_floor': .05, 'classes': [0, 1],
                'ultralytics': ultralytics.__version__, 'torch': torch.__version__,
                'fps': cap.get(cv2.CAP_PROP_FPS), 'first': first, 'last': last}
    cap.release()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f'{segment}_{first}_{last}_yolo26n_{imgsz}.json'
    path.write_text(json.dumps({'meta': metadata, 'frames': frames}), encoding='utf-8')
    print(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('segment')
    parser.add_argument('first', type=int)
    parser.add_argument('last', type=int)
    parser.add_argument('--imgsz', type=int, default=1920)
    args = parser.parse_args()
    extract(args.segment, args.first, args.last, args.imgsz)
