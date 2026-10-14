"""Optional YOLOX ONNX recognition via OpenCV; no weights or downloads bundled."""

from pathlib import Path

import cv2
import numpy as np


class CrossingClassifier:
    def __init__(self, model_path, input_size=640):
        if not Path(model_path).is_file() or input_size not in (416, 640):
            raise ValueError("请选择 YOLOX ONNX 模型及对应的 416 / 640 输入尺寸")
        self.size = input_size
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        grids, strides = [], []
        for stride in (8, 16, 32):
            y, x = np.meshgrid(np.arange(input_size // stride), np.arange(input_size // stride), indexing="ij")
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            strides.append(np.full((len(grids[-1]), 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.stride = np.concatenate(strides).astype(np.float32)

    def classify(self, frame, point):
        h, w = frame.shape[:2]
        cx, cy = point
        left, top = max(0, round(cx - 600)), max(0, round(cy - 850))
        crop = frame[top:min(h, top + 1200), left:min(w, left + 1200)]
        ch, cw = crop.shape[:2]
        ratio = min(self.size / ch, self.size / cw)
        padded = np.full((self.size, self.size, 3), 114, np.uint8)
        rh, rw = max(1, round(ch * ratio)), max(1, round(cw * ratio))
        padded[:rh, :rw] = cv2.resize(crop, (rw, rh))
        self.net.setInput(padded.transpose(2, 0, 1)[None].astype(np.float32))
        out = self.net.forward()[0].copy()
        if out.ndim != 2 or out.shape != (len(self.grid), 85):
            raise ValueError("模型输出不符合 YOLOX COCO 契约；未自动排除任何活动")
        out[:, :2] = (out[:, :2] + self.grid) * self.stride
        out[:, 2:4] = np.exp(np.clip(out[:, 2:4], -10, 10)) * self.stride
        best = {}
        for cls, label in ((0, "person"), (1, "bicycle"), (2, "car"), (3, "motorcycle"), (7, "truck")):
            scores = out[:, 4] * out[:, 5 + cls]
            for i in np.flatnonzero(scores >= .5):
                x, y, bw, bh = out[i, :4] / ratio
                x0, y0 = x - bw / 2 + left, y - bh / 2 + top
                if not (x0 - bw * .2 <= cx <= x0 + bw * 1.2 and y0 - bh * .2 <= cy <= y0 + bh * 1.2):
                    continue
                clipped = x - bw / 2 < 5 or y - bh / 2 < 5 or x + bw / 2 >= cw - 5 or y + bh / 2 >= ch - 5
                if scores[i] > best.get(label, {}).get("confidence", 0):
                    best[label] = dict(label=label, confidence=float(scores[i]), clipped=bool(clipped))
        if "bicycle" in best:
            return best["bicycle"]
        return max(best.values(), key=lambda x: x["confidence"], default=dict(label="uncertain", confidence=0, clipped=False))


def combine_classifications(predictions, obstructed=False):
    if obstructed or len(predictions) != 3:
        return dict(excluded=False, reason="遮挡或截图不完整，留给人工核对")
    labels = {p["label"] for p in predictions}
    if labels == {"bicycle"}:
        return dict(excluded=False, reason="检测到自行车")
    if (len(labels) == 1 and labels <= {"person", "motorcycle", "car", "truck"}
            and all(p["confidence"] >= .8 and not p["clipped"] for p in predictions)):
        return dict(excluded=True, reason="三个候选帧均为高置信度行人或车辆")
    return dict(excluded=False, reason="认车结论不一致或证据不足，留给人工核对")
