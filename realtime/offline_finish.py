"""Bounded offline line sampling and problem-list worker (CPU, one process).

Run with ``python -m realtime.offline_finish REQUEST.json``. Only finalized
archives are accepted. Frames are decoded in order; header FPS is never used
to assign an evidence time or frame number. Analysis outputs are separate from
the original event and recording journals.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import sys

import cv2
import numpy as np

from .finish_line import FinishLine
from .finish_problems import atomic_json, build_problem_report, digest


def limit_resources():
    cv2.setNumThreads(1)
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
    else:
        os.nice(10)


def open_video(path):
    # OpenCV 4.10+: cap FFmpeg at two decoder threads. No global environment changes.
    params = [cv2.CAP_PROP_N_THREADS, 2] if hasattr(cv2, "CAP_PROP_N_THREADS") else []
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, params)
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"录像无法打开：{path}")
    return capture


def detect_strip(strip, pts):
    """Prototype detector generalized to actual PTS and bounded chunks.

    Return candidate offsets and obstruction ranges in the supplied chunk.
    Results identify changes at the line, not rider identity or official times.
    """
    n, total, _ = strip.shape
    if total < 3:
        return [], []
    intervals = np.diff(pts)
    if not np.all(np.isfinite(pts)) or np.any(intervals <= 0):
        raise ValueError("录像帧时间不连续，不能生成可靠过线证据")
    dt = float(np.median(intervals))
    second = max(1, round(1000 / dt))
    s = strip.astype(np.float32)
    step = max(1, second)
    centers = np.arange(0, total, step)
    meds = np.stack([np.median(s[:, max(0, c - 3 * second):min(total, c + 3 * second + 1):2], axis=1)
                     for c in centers], axis=1)
    if len(centers) == 1:
        bg = np.broadcast_to(meds[:, :1], s.shape)
    else:
        idx = np.clip(np.searchsorted(centers, np.arange(total)), 1, len(centers) - 1)
        w = ((np.arange(total) - centers[idx - 1]) / (centers[idx] - centers[idx - 1]))[None, :, None]
        bg = meds[:, idx - 1] * (1 - w) + meds[:, idx] * w
    delta = np.linalg.norm(s - bg, axis=2)
    delta -= np.median(delta, axis=0, keepdims=True)
    mask = cv2.morphologyEx((delta > 40).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 1), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components = []
    for i in range(1, count):
        x, y, width, height, area = map(int, stats[i])
        if area >= 10 and height >= max(3, round(n * .012)):
            components.append(dict(c0=x, c1=x + width - 1, r0=y, r1=y + height - 1,
                                   row=int(np.median(np.flatnonzero(labels[:, x] == i))), area=area))
    components.sort(key=lambda e: e["c0"])
    events = []
    for c in components:
        for e in reversed(events):
            if pts[c["c0"]] - pts[e["c1"]] > 200:
                continue
            same_start = abs(pts[c["c0"]] - pts[e["c0"]]) <= 150 and c["r0"] <= e["r1"] + 40 and e["r0"] <= c["r1"] + 40
            still_there = c["c0"] <= e["c1"] + 1 and c["r0"] <= e["r1"] + 10 and e["r0"] <= c["r1"] + 10
            if same_start or still_there:
                e.update(r0=min(e["r0"], c["r0"]), r1=max(e["r1"], c["r1"]),
                         c1=max(e["c1"], c["c1"]), area=e["area"] + c["area"])
                break
        else:
            events.append(dict(c))
    min_height = max(3, round(n * .04))
    candidates, obstructed = [], []
    motion = np.zeros((n, total), np.float32)
    motion[:, 1:] = np.linalg.norm(s[:, 1:] - s[:, :-1], axis=2)
    mmask = cv2.morphologyEx((motion > 45).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 1), np.uint8))
    for e in events:
        duration = pts[e["c1"]] - pts[e["c0"]] + dt
        if e["r1"] - e["r0"] + 1 >= min_height and duration <= 1000 and e["area"] >= 40:
            candidates.append(dict(e, obstructed=False))
        elif duration > 1000:
            obstructed.append((int(e["c0"]), int(e["c1"])))
            sub = mmask[e["r0"]:e["r1"] + 1, e["c0"]:e["c1"] + 1]
            count, labels, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
            for i in range(1, count):
                x, y, width, height, area = map(int, stats[i])
                if height >= min_height and width * dt <= 500 and area >= 40:
                    c0 = e["c0"] + x
                    candidates.append(dict(c0=c0, c1=c0 + width - 1, r0=e["r0"] + y,
                                           r1=e["r0"] + y + height - 1,
                                           row=e["r0"] + y + height // 2, area=area, obstructed=True))
    candidates.sort(key=lambda e: e["c0"])
    kept = []
    for e in candidates:
        if any(abs(pts[e["c0"]] - pts[p["c0"]]) <= 150 and e["r0"] <= p["r1"] + 40 and p["r0"] <= e["r1"] + 40
               for p in kept[-12:]):
            continue
        kept.append(e)
    return kept, obstructed


def sample_source(source, line, run_dir, progress=lambda message: None):
    path = Path(source["video_path"])
    signature = (path.stat().st_size, path.stat().st_mtime_ns)
    cap = open_video(path)
    expected_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    observations, obstructions, chunks = [], [], []
    cols, pts, indices = [], [], []
    first_pts = last_pts = None
    frame_number = 0
    core_start = 0.
    maps = None
    px = py = None

    def flush(final=False):
        nonlocal cols, pts, indices, core_start
        if len(pts) < 3:
            return
        values = np.stack(cols, axis=1)
        times = np.asarray(pts)
        ev, blocked = detect_strip(values, times)
        core_end = float("inf") if final else pts[-1] - 6000
        if core_end <= core_start:
            raise ValueError("录像帧密度超出离线采样内存上限")
        for e in ev:
            position = float(times[e["c0"]])
            if not core_start <= position < core_end:
                continue
            row = int(e["row"])
            locator = dict(segment_id=source["segment_id"], camera_index=source["camera_index"],
                           video_path=str(path), frame_index=int(indices[e["c0"]]),
                           position_ms=round(position), recorder_ms=round(source["origin_ms"] + position),
                           source_signature=list(signature))
            observations.append(dict(recorder_ms=locator["recorder_ms"], locator=locator,
                                     obstructed=e["obstructed"], point=[float(px[row]), float(py[row])]))
        for a, b in blocked:
            if times[b] >= core_start and times[a] < core_end:
                i = a
                locator = dict(segment_id=source["segment_id"], camera_index=source["camera_index"],
                               video_path=str(path), frame_index=int(indices[i]), position_ms=round(times[i]),
                               recorder_ms=round(source["origin_ms"] + times[i]), source_signature=list(signature))
                evidence = dict(locator=locator, recorder_ms=locator["recorder_ms"])
                obstructions.append((source["origin_ms"] + max(core_start, times[a]),
                                     source["origin_ms"] + min(core_end, times[b]), evidence))
        chunk_path = run_dir / f"{digest(source['segment_id'])[:12]}-{len(chunks):04d}.npz"
        np.savez_compressed(chunk_path, pts_ms=times, frames=np.asarray(indices),
                            strip=values, px=px, py=py)
        chunks.append(str(chunk_path))
        if not final:
            core_start = core_end
            keep = int(np.searchsorted(times, core_start - 6000))
            cols, pts, indices = cols[keep:], pts[keep:], indices[keep:]
            progress(f"线取样：{path.name} {round(core_start / 1000)} 秒")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = float(cap.get(cv2.CAP_PROP_POS_MSEC))
            if not math.isfinite(t) or t < 0 or last_pts is not None and t <= last_pts:
                raise ValueError(f"帧时间不可验证：{path.name} 第 {frame_number + 1} 帧")
            if maps is None:
                h, w = frame.shape[:2]
                px = np.linspace(line.x1 * (w - 1), line.x2 * (w - 1), 500)
                py = np.linspace(line.y1 * (h - 1), line.y2 * (h - 1), 500)
                dx, dy = px[-1] - px[0], py[-1] - py[0]
                length = math.hypot(dx, dy)
                offsets = np.arange(-2, 2)
                maps = ((px[:, None] + dy / length * offsets).astype(np.float32),
                        (py[:, None] - dx / length * offsets).astype(np.float32))
            cols.append(cv2.remap(frame, *maps, cv2.INTER_LINEAR).mean(axis=1).astype(np.uint8))
            pts.append(t)
            indices.append(frame_number)
            frame_number += 1
            first_pts = t if first_pts is None else first_pts
            last_pts = t
            # Also cap by frame count for unusually high FPS inputs.
            if len(pts) >= 8000 or pts[-1] - core_start >= 66_000:
                flush()
        flush(final=True)
    finally:
        cap.release()
    if frame_number < 3:
        raise ValueError(f"录像没有足够的可用帧：{path}")
    if expected_frames > 0 and frame_number + 1 < expected_frames:
        raise ValueError(f"录像解码提前中断：{path.name}")
    if signature != (path.stat().st_size, path.stat().st_mtime_ns):
        raise ValueError("分析期间录像发生变化，请封口后重试")
    intervals = np.diff(pts)
    end = last_pts + (float(np.median(intervals)) if len(intervals) else 1)
    return observations, obstructions, (source["origin_ms"] + first_pts, source["origin_ms"] + end), chunks


def render_cards(request, report, run_dir, progress=lambda message: None):
    jobs = defaultdict(list)
    sources = {s["segment_id"]: s for s in request["sources"]}
    for row in report["problems"]:
        locator = row["locator"]
        if locator:
            jobs[locator["segment_id"]].append(row)
    for segment_id, rows in jobs.items():
        source = sources[segment_id]
        cap = open_video(source["video_path"])
        targets = defaultdict(list)
        for row in rows:
            targets[row["locator"]["frame_index"]].append(row)
        last = max(targets)
        try:
            for index in range(last + 1):
                ok, frame = cap.read()
                if not ok:
                    raise ValueError("卡片原帧解码中断")
                if index not in targets:
                    continue
                actual_pts = float(cap.get(cv2.CAP_PROP_POS_MSEC))
                for row in targets[index]:
                    loc = row["locator"]
                    if abs(actual_pts - loc["position_ms"]) > 1:
                        raise ValueError("卡片原帧时间发生变化")
                    h, w = frame.shape[:2]
                    image = cv2.resize(frame, (min(w, 960), max(1, round(h * min(w, 960) / w))))
                    path = run_dir / f"{row['problem_id']}.jpg"
                    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if not ok:
                        raise ValueError("卡片保存失败")
                    path.write_bytes(encoded.tobytes())
                    row["card_path"] = str(path)
        finally:
            cap.release()
        path = Path(source["video_path"])
        signature = [path.stat().st_size, path.stat().st_mtime_ns]
        if any(row["locator"]["source_signature"] != signature for row in rows):
            raise ValueError("录像已变化，请重新分析")
        progress(f"卡片已生成：{Path(source['video_path']).name}")


def classify_candidates(request, observations, progress):
    from .crossing_classifier import CrossingClassifier, combine_classifications
    from .crossing_clock import align_crossings, fit_crossing_clock
    from .finish_problems import scoped_chips
    if not request.get("model_path"):
        return "未配置认车模型：所有配不上的活动保留供人工核对"
    chips = [c for c in scoped_chips(request["chips"], request["scope"])
             if (not request.get("final_lap") or c.get("lap", 0) == request["final_lap"])
             and not any(word in c.get("group_name", "") for word in ("测试", "备用"))]
    try:
        clock = fit_crossing_clock([c["timestamp_ms"] for c in chips], [e["recorder_ms"] for e in observations])
    except ValueError:
        return "自动校时未就绪，认车暂缓"
    paired = {vi for _, vi, _ in align_crossings([c["timestamp_ms"] for c in chips],
                                               [e["recorder_ms"] for e in observations], clock)}
    classifier = CrossingClassifier(request["model_path"], request.get("model_size", 640))
    by_source = defaultdict(list)
    for i, e in enumerate(observations):
        if i not in paired and not e["obstructed"]:
            by_source[e["locator"]["segment_id"]].append(e)
    sources = {s["segment_id"]: s for s in request["sources"]}
    for segment_id, events in by_source.items():
        jobs = sorted((max(0, e["locator"]["position_ms"] + offset), i, slot)
                      for i, e in enumerate(events) for slot, offset in enumerate((-400, -200, 0)))
        results = defaultdict(list)
        capture = open_video(sources[segment_id]["video_path"])
        job_index = 0
        try:
            while job_index < len(jobs):
                ok, frame = capture.read()
                if not ok:
                    break
                pts = capture.get(cv2.CAP_PROP_POS_MSEC)
                while job_index < len(jobs) and pts >= jobs[job_index][0]:
                    target, i, _slot = jobs[job_index]
                    if pts - target <= 100:
                        results[i].append(classifier.classify(frame, events[i]["point"]))
                    job_index += 1
        finally:
            capture.release()
        for i, e in enumerate(events):
            # Close arrivals are never auto-excluded, even with confident labels.
            crowded = sum(abs(o["recorder_ms"] - e["recorder_ms"]) < 1000 for o in observations) > 1
            e["classification"] = combine_classifications(results[i], crowded)
        progress(f"候选认车已完成：{Path(sources[segment_id]['video_path']).name}")
    return ""


def analyze_request(request, run_dir, progress=lambda message: None):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    line = FinishLine(**request["finish_line"])
    if any(s["camera_index"] != line.camera_index for s in request["sources"]):
        raise ValueError("一次分析只能使用同一机位的终点线")
    observations, blocked, coverages, warnings = [], [], [], []
    chunk_map = {}
    for source in request["sources"]:
        if source.get("race_id") and source["race_id"] != request["scope"]["race_id"]:
            raise ValueError("录像所属赛事不匹配")
        try:
            ev, obstruction, coverage, chunks = sample_source(source, line, run_dir, progress)
            observations.extend(ev)
            blocked.extend(obstruction)
            coverages.append(coverage)
            chunk_map[source["segment_id"]] = chunks
        except (OSError, ValueError, cv2.error) as error:
            warnings.append(str(error))
            progress(str(error))
    if not coverages:
        raise ValueError("没有成功解码的录像，无法分析；" + "；".join(warnings))
    try:
        classifier_warning = classify_candidates(request, observations, progress)
        if classifier_warning:
            warnings.append(classifier_warning)
    except (ValueError, OSError, cv2.error) as error:
        # Invalid model output must leave every candidate available to a person.
        for observation in observations:
            observation.pop("classification", None)
        warnings.append(f"认车未完成，保留全部候选：{error}")
    report = build_problem_report(request, observations, coverages, blocked, warnings)
    # Chip-only rows still need a source frame. Select from actual decoded PTS,
    # never infer frame_index from the file's advertised FPS.
    if report["clock"]:
        from .crossing_clock import CrossingClock
        clock = CrossingClock(**report["clock"])
        sources = {s["segment_id"]: s for s in request["sources"]}
        for row in report["problems"]:
            if row["locator"] or row["kind"] not in {"no_crossing", "group", "extra_chip", "duplicate", "test_chip"}:
                continue
            target = clock.to_video(row["start_ms"])
            best = None
            for segment_id, chunks in chunk_map.items():
                source = sources[segment_id]
                for chunk in chunks:
                    with np.load(chunk, allow_pickle=False) as data:
                        absolute = source["origin_ms"] + data["pts_ms"]
                        if not absolute[0] <= target <= absolute[-1]:
                            continue
                        i = int(np.argmin(np.abs(absolute - target)))
                        distance = abs(float(absolute[i]) - target)
                        if best is None or distance < best[0]:
                            path = Path(source["video_path"])
                            best = (distance, dict(segment_id=segment_id, camera_index=source["camera_index"],
                                                   video_path=str(path), frame_index=int(data["frames"][i]),
                                                   position_ms=round(float(data["pts_ms"][i])), recorder_ms=round(float(absolute[i])),
                                                   source_signature=[path.stat().st_size, path.stat().st_mtime_ns]))
            if best:
                row["locator"] = best[1]
                row["fingerprint"] = digest({k: v for k, v in row.items() if k != "fingerprint"})
    render_cards(request, report, run_dir, progress)
    report["coverages"] = coverages
    atomic_json(run_dir / "report.json", report)
    progress("离线分析完成")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="FinishReview 离线问题清单")
    parser.add_argument("request")
    args = parser.parse_args(argv)
    try:
        limit_resources()
        path = Path(args.request)
        request = json.loads(path.read_text(encoding="utf-8"))
        analyze_request(request, path.parent, lambda text: print(text, flush=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, cv2.error) as error:
        print(f"分析失败：{error}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
