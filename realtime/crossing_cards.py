"""Crossing cards and mini filmstrips for problem items.

A card is the decoded frame at the line contact, cropped around the rider and
marked at the contact point. The mini filmstrip shows nine crops from one
second before to one second after, at the same place, so the operator sees
the rider pass without opening the video. Frames come from the original
recording; seeking always starts early and reads forward.

Images are written with ``cv2.imencode`` because ``cv2.imwrite`` cannot open
non-ASCII paths on Windows. No text is drawn into images; titles are shown
by the UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

from .bike_check import FrameReader
from .problem_items import ProblemItem


logger = logging.getLogger("FinishReview.CrossingCards")

FILMSTRIP_OFFSETS_MS = (-1000, -750, -500, -250, 0, 250, 500, 750, 1000)
CARD_SIZE = 360
FILMSTRIP_TILE = 180


def safe_name(item_id: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in item_id)


def write_jpeg(path: Path, image: np.ndarray, quality: int = 85) -> None:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise OSError(f"failed to encode {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.tobytes())


def read_jpeg(path: Path) -> Optional[np.ndarray]:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _crop_box(width: int, height: int, cx: float, cy: float, size: int) -> tuple[int, int, int]:
    size = int(min(size, width, height))
    x0 = int(np.clip(cx - size / 2, 0, width - size))
    y0 = int(np.clip(cy - 0.6 * size, 0, height - size))
    return x0, y0, size


def _resolve(video_path: str, workspace_root: Path) -> Path:
    path = Path(video_path)
    return path if path.is_absolute() else workspace_root / path


def _item_geometry(item: ProblemItem) -> tuple[Optional[tuple[float, float]], list]:
    evidence = item.evidence
    points = [tuple(point) for point in evidence.get("segment_points", [])]
    contact = evidence.get("contact")
    if contact and any(contact):
        return (float(contact[0]), float(contact[1])), points
    if points:
        return None, points
    return None, []


def _attach(item: ProblemItem, name: str, pts=None) -> None:
    evidence = item.evidence
    if isinstance(evidence, dict):
        evidence["card"] = f"cards/{name}.jpg"
        evidence["filmstrip"] = f"cards/{name}_strip.jpg"
        evidence["filmstrip_offsets_ms"] = list(FILMSTRIP_OFFSETS_MS)
        if pts is not None:
            evidence["filmstrip_pts_ms"] = pts


def render_item(item: ProblemItem, reader: FrameReader, output_dir: Path) -> bool:
    frame_ref = item.evidence.get("frame")
    if not frame_ref:
        return False
    name = safe_name(item.item_id)
    if (output_dir / "cards" / f"{name}.jpg").is_file() and (output_dir / "cards" / f"{name}_strip.jpg").is_file():
        # Item ids follow their source frame, so an existing card is current.
        _attach(item, name)
        return True
    position = float(frame_ref["position_ms"])
    targets = [position + offset for offset in FILMSTRIP_OFFSETS_MS]
    frames = reader.frames_at(targets, tolerance_ms=30.0)
    if not frames:
        return False
    contact, points = _item_geometry(item)
    sample = frames[0][0]
    height, width = sample.shape[:2]
    if contact is None:
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            size = int(max(max(xs) - min(xs), max(ys) - min(ys)) * 1.1)
        else:
            centre, size = (width / 2, height / 2), min(width, height)
    else:
        centre, size = contact, int(0.234 * width)
    x0, y0, size = _crop_box(width, height, centre[0], centre[1] + (0.1 * size if contact else 0.1 * size), size)
    main = min(frames, key=lambda item_frame: abs(item_frame[1] - position))[0]
    card = main[y0:y0 + size, x0:x0 + size].copy()
    scale = CARD_SIZE / max(1, size)
    card = cv2.resize(card, (CARD_SIZE, CARD_SIZE), interpolation=cv2.INTER_AREA)
    if len(points) >= 2:
        polyline = np.asarray([[(x - x0) * scale, (y - y0) * scale] for x, y in points], dtype=np.int32)
        cv2.polylines(card, [polyline], False, (0, 220, 255), 1, cv2.LINE_AA)
    if contact is not None:
        cv2.circle(card, (int((contact[0] - x0) * scale), int((contact[1] - y0) * scale)), 8, (0, 0, 255), 2, cv2.LINE_AA)
    tiles = []
    for frame, _pts in frames:
        tile = cv2.resize(frame[y0:y0 + size, x0:x0 + size], (FILMSTRIP_TILE, FILMSTRIP_TILE), interpolation=cv2.INTER_AREA)
        tiles.append(tile)
    while len(tiles) < len(FILMSTRIP_OFFSETS_MS):
        tiles.append(np.full((FILMSTRIP_TILE, FILMSTRIP_TILE, 3), 245, np.uint8))
    write_jpeg(output_dir / "cards" / f"{name}.jpg", card)
    write_jpeg(output_dir / "cards" / f"{name}_strip.jpg", np.hstack(tiles), quality=80)
    _attach(item, name, [round(pts, 1) for _frame, pts in frames])
    return True


def render_cards(
    items: Sequence[ProblemItem],
    output_dir: Path,
    workspace_root: Path,
    *,
    progress: Callable[[str, int, int], None] = lambda *_args: None,
    decode_threads: int = 2,
    checkpoint: Optional[Callable[[], None]] = None,
) -> int:
    """Render cards for every item pinned to a source frame; returns the count."""

    pending = [item for item in items if item.evidence.get("frame")]
    pending.sort(key=lambda item: (item.evidence["frame"]["video_path"], item.evidence["frame"]["position_ms"]))
    readers: dict[str, FrameReader] = {}
    rendered = 0
    try:
        for index, item in enumerate(pending):
            progress("卡片", index, len(pending))
            path = _resolve(item.evidence["frame"]["video_path"], Path(workspace_root))
            reader = readers.get(str(path))
            if reader is None:
                for other in readers.values():
                    other.close()
                readers = {str(path): FrameReader(path, decode_threads)}
                reader = readers[str(path)]
            try:
                rendered += int(render_item(item, reader, Path(output_dir)))
            except (OSError, cv2.error) as error:
                logger.warning("card for %s failed: %s", item.item_id, error)
            if checkpoint is not None:
                checkpoint()
    finally:
        for reader in readers.values():
            reader.close()
    progress("卡片", len(pending), len(pending))
    return rendered


__all__ = [
    "FILMSTRIP_OFFSETS_MS",
    "read_jpeg",
    "render_cards",
    "render_item",
    "safe_name",
    "write_jpeg",
]
