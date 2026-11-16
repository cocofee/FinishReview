"""The strip index: same data as reading each strip, without reopening it every round."""

from pathlib import Path

import numpy as np
import pytest

from realtime import problem_analysis, strip_index
from realtime.line_sampling import LineGeometry, LineStrip, read_strip_meta, save_strip
from realtime.problem_analysis import _VerificationRuntime, _load_file_strips
from realtime.strip_index import StripIndex


def _write(folder: Path, name: str, *, start_ms=0, frames=6, line=None, segment_id=None):
    line = line or {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5}
    pts = np.arange(frames, dtype=np.float64) * 40
    geometry = LineGeometry.from_config(line, 200, 100, samples=8)
    strip = LineStrip(np.zeros((8, frames, 3), np.uint8), pts, geometry, video_path=str(folder / f"{name}.ts"), fps=25.0)
    save_strip(folder / f"{name}.npz", strip, {
        "line_digest": "digest", "samples": 8, "segment_id": segment_id or name, "session_id": "s",
        "media_started_at_ms": start_ms, "video_size": 10, "excluded_ranges": [[1, 2]], "line_config": line,
    })
    return folder / f"{name}.npz"


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "strips" / "camera_01"
    path.mkdir(parents=True)
    return path


def _same(left, right):
    assert [item.path.name for item in left] == [item.path.name for item in right]
    for one, other in zip(left, right):
        assert one.meta == other.meta and one.start_ms == other.start_ms
        assert np.array_equal(one.pts, other.pts)
        for key in ("xs", "ys", "nx", "ny"):
            assert np.array_equal(getattr(one.geometry, key), getattr(other.geometry, key))
        assert (one.geometry.width, one.geometry.height, one.geometry.source, one.geometry.snap_offset_px) == (
            other.geometry.width, other.geometry.height, other.geometry.source, other.geometry.snap_offset_px)


def test_saved_index_gives_exactly_what_reading_each_strip_gives(folder):
    other_line = {"x1": 0.2, "y1": 0.3, "x2": 0.8, "y2": 0.7}
    paths = [_write(folder, f"p{i}", start_ms=i * 2000, frames=5 + i % 3, line=other_line if i % 4 == 3 else None)
             for i in range(8)]
    direct = _load_file_strips(paths)
    direct_meta = read_strip_meta(paths[0])
    first = _VerificationRuntime()
    _same(_load_file_strips(paths, first), direct)
    first.save_strip_indexes()
    assert (folder.parent / "camera_01.index").is_file()
    assert not list(folder.glob("*.index*")) and not any(p.suffix == ".npz" for p in folder.parent.iterdir())

    # A new process: nothing is reopened, and the answer is the same.
    opened = []
    real_load = np.load
    monkey = pytest.MonkeyPatch()
    monkey.setattr(strip_index.np, "load", lambda path, *a, **k: opened.append(Path(path).name) or real_load(path, *a, **k))
    try:
        second = _VerificationRuntime()
        _same(_load_file_strips(paths, second), direct)
        assert opened == ["camera_01.index"]
        meta = second.strip_index(folder).meta(paths[0])
        assert meta == direct_meta and opened == ["camera_01.index"]
    finally:
        monkey.undo()
    loaded = _load_file_strips(paths, second)
    # A strip's arrays are its own: changing one changes neither the others nor the index.
    loaded[0].geometry.xs[:] = -1
    assert not np.array_equal(loaded[1].geometry.xs, loaded[0].geometry.xs)
    assert np.array_equal(second.strip_index(folder).data(paths[0]).xs, direct[0].geometry.xs)


def test_a_rewritten_strip_is_read_again_and_a_gone_one_is_dropped(folder):
    paths = [_write(folder, f"p{i}", start_ms=i * 2000) for i in range(3)]
    runtime = _VerificationRuntime()
    _load_file_strips(paths, runtime)
    runtime.save_strip_indexes()

    _write(folder, "p1", start_ms=999, segment_id="resampled")  # sampled again: new size / time
    paths[2].unlink()
    runtime = _VerificationRuntime()
    loaded = _load_file_strips(paths[:2], runtime)
    assert [(item.start_ms, item.meta["segment_id"]) for item in loaded] == [(0, "p0"), (999, "resampled")]
    runtime.save_strip_indexes()
    assert set(StripIndex(folder)._entries) == {"p0.npz", "p1.npz"}
    assert StripIndex(folder).meta(folder / "p2.npz") is None


@pytest.mark.parametrize("damage", [b"", b"not an index", None])
def test_a_missing_or_damaged_index_is_ignored(folder, damage):
    paths = [_write(folder, f"p{i}", start_ms=i * 2000) for i in range(3)]
    runtime = _VerificationRuntime()
    _load_file_strips(paths, runtime)
    runtime.save_strip_indexes()
    index_file = folder.parent / "camera_01.index"
    if damage is None:
        index_file.write_bytes(index_file.read_bytes()[:-40])  # cut short, as an interrupted write
    else:
        index_file.write_bytes(damage)
    _same(_load_file_strips(paths, _VerificationRuntime()), _load_file_strips(paths))


def test_an_index_of_another_version_is_ignored(folder, monkeypatch):
    paths = [_write(folder, "p0")]
    runtime = _VerificationRuntime()
    _load_file_strips(paths, runtime)
    runtime.save_strip_indexes()
    monkeypatch.setattr(strip_index, "INDEX_VERSION", strip_index.INDEX_VERSION + 1)
    assert StripIndex(folder)._entries == {}


def test_a_strip_that_cannot_be_read_is_not_current_and_still_raises_when_loaded(folder):
    broken = folder / "bad.npz"
    broken.write_bytes(b"garbage")
    index = StripIndex(folder)
    truncated = folder / "cut.npz"
    truncated.write_bytes(_write(folder, "whole").read_bytes()[:-30])  # an interrupted write
    assert index.meta(broken) is None and index.meta(truncated) is None
    assert index.meta(folder / "missing.npz") is None
    with pytest.raises(Exception):
        _load_file_strips([broken], _VerificationRuntime())


def test_live_freshness_check_uses_the_index(folder, monkeypatch):
    from types import SimpleNamespace

    path = _write(folder, "p0")
    segment = SimpleNamespace(path=Path(path).with_suffix(".ts"), segment=SimpleNamespace(segment_id="p0"),
                              excluded_ranges=((1, 2),))
    segment.path.write_bytes(b"x" * 10)
    index = StripIndex(folder)
    assert problem_analysis._strip_is_current(path, segment, "digest", 8, index)
    assert problem_analysis._strip_is_current(path, segment, "digest", 8)
    assert not problem_analysis._strip_is_current(path, segment, "other digest", 8, index)
    monkeypatch.setattr(problem_analysis, "read_strip_meta", lambda *a: pytest.fail("reopened the strip"))
    assert problem_analysis._strip_is_current(path, segment, "digest", 8, index)
