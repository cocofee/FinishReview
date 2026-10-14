"""Drift-aware chip -> recorder clock mapping for offline crossing review.

The correction is added exactly once. Source frame positions never change when
a new calibration is fitted. This module does not modify manual calibrations.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
import hashlib
import json
import math

import numpy as np


@dataclass(frozen=True)
class CrossingClock:
    reference_ms: int
    offset_ms: float
    drift_ms_per_hour: float
    start_ms: int
    end_ms: int
    anchors: int
    residual_median_ms: float
    coarse_offset_ms: float
    version: str

    def __post_init__(self):
        if not all(math.isfinite(x) for x in (
            self.offset_ms, self.drift_ms_per_hour,
            self.residual_median_ms, self.coarse_offset_ms,
        )) or self.end_ms < self.start_ms:
            raise ValueError("invalid crossing clock")
        if abs(self.drift_ms_per_hour) >= 3_600_000:
            raise ValueError("clock mapping must be invertible")

    def correction(self, chip_ms: float) -> float:
        return self.offset_ms + self.drift_ms_per_hour * (chip_ms - self.reference_ms) / 3_600_000

    def to_video(self, chip_ms: float) -> float:
        return chip_ms + self.correction(chip_ms)

    def to_chip(self, recorder_ms: float) -> float:
        slope = self.drift_ms_per_hour / 3_600_000
        return self.reference_ms + (recorder_ms - self.reference_ms - self.offset_ms) / (1 + slope)

    def manual_difference(self, chip_ms: int, manual_offset_ms: int) -> float:
        return self.correction(chip_ms) - manual_offset_ms

    def to_payload(self):
        return asdict(self)


def fit_crossing_clock(chip_times, video_times) -> CrossingClock:
    """Find the ±90 s histogram peak and robustly fit isolated arrivals.

    Refuse ambiguous peaks, sparse anchors and unstable fits. No manual offset
    fallback: an unsupported automatic fit must not authorize automatic passes.
    """
    chips = sorted(int(t) for t in chip_times)
    videos = sorted(float(t) for t in video_times)
    if len(chips) < 5 or len(videos) < 5:
        raise ValueError("校时等待：至少需要五次可用过线")
    bins = np.arange(-90_000, 90_001, 100, dtype=float)
    histogram = np.zeros(len(bins) - 1, dtype=np.int64)
    for t in chips:
        nearby = np.asarray(videos[bisect_left(videos, t - 90_000):bisect_right(videos, t + 90_000)])
        histogram += np.histogram(nearby - t, bins=bins)[0]
    smooth = np.convolve(histogram, np.ones(3), "same")
    peak = int(np.argmax(smooth))
    coarse = float(bins[peak] + 50)
    distant = smooth.copy()
    distant[max(0, peak - 20):peak + 21] = 0
    if smooth[peak] < 5 or distant.max() >= smooth[peak] * .85:
        raise ValueError("校时等待：偏移峰不唯一，请检查录像与赛事范围")
    pairs = []
    for i, t in enumerate(chips):
        before = t - chips[i - 1] if i else math.inf
        after = chips[i + 1] - t if i + 1 < len(chips) else math.inf
        if min(before, after) < 5_000:
            continue
        nearby = videos[bisect_left(videos, t + coarse - 1_500):bisect_right(videos, t + coarse + 1_500)]
        if len(nearby) == 1:
            pairs.append((t, nearby[0] - t))
    if len(pairs) < 5:
        raise ValueError("校时等待：缺少单独过线的校时锚点")
    reference = int(np.median([p[0] for p in pairs]))
    x = np.asarray([p[0] - reference for p in pairs], dtype=float)
    y = np.asarray([p[1] for p in pairs], dtype=float)
    keep = np.ones(len(x), dtype=bool)
    for _ in range(6):
        if keep.sum() < 5 or np.ptp(x[keep]) < 60_000:
            raise ValueError("校时等待：有效锚点不足或时间跨度不足一分钟")
        slope, offset = np.polyfit(x[keep], y[keep], 1)
        residual = y - (offset + slope * x)
        next_keep = np.abs(residual) <= max(80., 2.5 * float(np.median(np.abs(residual[keep]))))
        if np.array_equal(keep, next_keep):
            break
        keep = next_keep
    if keep.sum() < 5 or np.ptp(x[keep]) < 60_000:
        raise ValueError("校时等待：离群点剔除后锚点不足")
    slope, offset = np.polyfit(x[keep], y[keep], 1)
    mad = float(np.median(np.abs(y[keep] - offset - slope * x[keep])))
    if mad > 50 or abs(slope * 3_600_000) > 5_000:
        raise ValueError("校时等待：残差或漂移超出验收范围")
    params = dict(reference_ms=reference, offset_ms=float(offset),
                  drift_ms_per_hour=float(slope * 3_600_000),
                  start_ms=int(reference + x[keep].min()), end_ms=int(reference + x[keep].max()),
                  anchors=int(keep.sum()), residual_median_ms=mad, coarse_offset_ms=coarse)
    version = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    return CrossingClock(**params, version=version)


def align_crossings(chip_times, video_times, clock: CrossingClock):
    """Order-preserving one-to-one alignment using a sparse dynamic program.

    Return input indices and residuals. A Fenwick tree stores the best preceding
    match, bounding memory to candidate pairs rather than a whole-race matrix.
    """
    chips = sorted(enumerate(chip_times), key=lambda p: p[1])
    videos = sorted(enumerate(video_times), key=lambda p: p[1])
    times = [p[1] for p in videos]
    tree = [(0., -1)] * (len(videos) + 1)
    nodes = []

    def query(end):
        best = (0., -1)
        while end:
            if tree[end][0] > best[0]:
                best = tree[end]
            end -= end & -end
        return best

    for ci, ct in chips:
        expected = clock.to_video(ct)
        updates = []
        for j in range(bisect_left(times, expected - 700), bisect_right(times, expected + 700)):
            residual = times[j] - expected
            gain = 3.2 - .5 * (residual / 150.) ** 2
            if gain <= 0:
                continue
            score, previous = query(j)
            nodes.append((previous, ci, videos[j][0], float(residual)))
            updates.append((j + 1, (score + gain, len(nodes) - 1)))
        # Apply after the chip's candidates so one chip can never match twice.
        for j, value in updates:
            while j < len(tree):
                if value[0] > tree[j][0]:
                    tree[j] = value
                j += j & -j
    _, node = query(len(videos))
    result = []
    while node >= 0:
        previous, ci, vi, residual = nodes[node]
        result.append((ci, vi, residual))
        node = previous
    return list(reversed(result))
