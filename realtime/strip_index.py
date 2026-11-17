"""A one-file memory of every finish-line strip's small data, kept between rounds.

A live round is a new process, so it used to reopen every strip of the race just
to learn its metadata and sampled line (about a millisecond each).  This keeps
that small data of one camera's strips in a single file next to the ``strips``
folder.  An entry is used only while the strip's file size and modification time
are unchanged; anything else is read from the strip itself.  The file is only a
cache: if it is missing, damaged or from another version it is ignored, and it is
never the source of a decision.
"""

from __future__ import annotations

import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
from .atomic_file import atomic_open

logger = logging.getLogger("FinishReview.StripIndex")

INDEX_VERSION = 1
_GEOMETRY_KEYS = ("xs", "ys", "nx", "ny")


class StripData:
    """What a strip's npz holds apart from its picture: metadata, line, frame times."""

    __slots__ = ("meta", "xs", "ys", "nx", "ny", "pts")

    def __init__(self, meta: dict, xs, ys, nx, ny, pts) -> None:
        self.meta, self.xs, self.ys, self.nx, self.ny, self.pts = meta, xs, ys, nx, ny, pts


def _read_strip(path: Path) -> StripData:
    with np.load(path) as data:
        meta = json.loads(bytes(data["meta"]).decode("utf-8"))
        return StripData(meta, data["xs"], data["ys"], data["nx"], data["ny"], data["pts_ms"])


class StripIndex:
    """The strips of one camera folder (``finish_problems/strips/camera_NN``)."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)
        self.path = self.folder.parent / f"{self.folder.name}.index"
        self._entries: dict[str, tuple[int, int, StripData]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            with np.load(self.path, allow_pickle=False) as data:
                header = json.loads(bytes(data["header"]).decode("utf-8"))
                if header.get("version") != INDEX_VERSION:
                    return
                flat = data["geometry"]
                pts_flat = data["pts"]
                geometries = [
                    tuple(flat[offset + index * size:offset + (index + 1) * size] for index in range(4))
                    for offset, size in header["geometries"]
                ]
                entries = {}
                for name, size, mtime_ns, meta, geometry, pts_start, pts_count in header["entries"]:
                    xs, ys, nx, ny = geometries[geometry]
                    entries[name] = (int(size), int(mtime_ns), StripData(
                        meta, xs, ys, nx, ny, pts_flat[pts_start:pts_start + pts_count]))
                self._entries = entries
        except (OSError, KeyError, ValueError, IndexError, TypeError, EOFError, zipfile.BadZipFile):
            self._entries = {}  # Missing or damaged: every strip is read itself.

    def data(self, path: str | Path) -> StripData:
        """The strip's data; errors reading the strip itself are raised, as before."""

        path = Path(path)
        stat = path.stat()
        stamp = (int(stat.st_size), int(stat.st_mtime_ns))
        saved = self._entries.get(path.name)
        if saved is not None and saved[:2] == stamp:
            return saved[2]
        data = _read_strip(path)
        self._entries[path.name] = (*stamp, data)
        self._dirty = True
        return data

    def meta(self, path: str | Path) -> Optional[dict]:
        """The strip's metadata, or None if it is missing or unreadable."""

        try:
            return self.data(path).meta
        except (OSError, KeyError, ValueError, EOFError, zipfile.BadZipFile):
            return None  # A cut-short strip is simply sampled again.

    def save(self) -> None:
        """Write the index if anything was added; drop entries of strips that are gone."""

        if not self._dirty:
            return
        try:
            present = {entry.name for entry in os.scandir(self.folder)}
        except OSError:
            return
        kept = {name: value for name, value in self._entries.items() if name in present}
        geometry_ids: dict[bytes, int] = {}
        geometries, flat, pts_parts, entries, pts_total = [], [], [], [], 0
        for name, (size, mtime_ns, data) in kept.items():
            arrays = [np.ascontiguousarray(getattr(data, key), dtype=np.float64) for key in _GEOMETRY_KEYS]
            if len({array.size for array in arrays}) != 1:
                continue  # Not a line this format can hold; it is simply read each time.
            signature = b"".join(array.tobytes() for array in arrays)
            if signature not in geometry_ids:
                geometry_ids[signature] = len(geometries)
                geometries.append([sum(part.size for part in flat), arrays[0].size])
                flat.extend(arrays)
            pts = np.ascontiguousarray(data.pts, dtype=np.float64)
            entries.append([name, size, mtime_ns, data.meta, geometry_ids[signature], pts_total, int(pts.size)])
            pts_parts.append(pts)
            pts_total += int(pts.size)
        header = {"version": INDEX_VERSION, "geometries": geometries, "entries": entries}
        try:
            with atomic_open(self.path, "wb", durable=False) as output:
                np.savez(
                    output,
                    header=np.frombuffer(json.dumps(header, ensure_ascii=False).encode("utf-8"), dtype=np.uint8),
                    geometry=np.concatenate(flat) if flat else np.zeros(0),
                    pts=np.concatenate(pts_parts) if pts_parts else np.zeros(0),
                )
            self._entries = {name: value for name, value in kept.items()}
            self._dirty = False
        except (OSError, ValueError, TypeError) as error:
            logger.warning("strip index not saved: %s", error)
