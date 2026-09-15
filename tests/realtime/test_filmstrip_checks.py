import pytest

import realtime.filmstrip_checks as checks
from realtime.filmstrip_checks import FilmstripCheckStore


def test_inspection_progress_survives_reopen_and_partial_undo(tmp_path):
    path = tmp_path / "checks.jsonl"
    store = FilmstripCheckStore(path, "race-1", 1)
    assert not path.exists()
    store.set_checked(((1000, 2000), (2000, 3000), (5000, 6000)), True)
    store.set_checked(((2500, 3000),), True)
    store.set_checked(((1500, 2500),), False)
    restored = FilmstripCheckStore(path, "race-1", 1)
    assert restored.ranges == ((1000, 1500), (2500, 3000), (5000, 6000))


def test_other_camera_race_and_concurrent_panel_do_not_overwrite_checks(tmp_path):
    path = tmp_path / "checks.jsonl"
    first = FilmstripCheckStore(path, "race-1", 1)
    second = FilmstripCheckStore(path, "race-1", 1)
    first.set_checked(((1000, 2000),), True)
    FilmstripCheckStore(path, "race-2", 1).set_checked(((7000, 8000),), True)
    FilmstripCheckStore(path, "race-1", 2).set_checked(((9000, 10000),), True)
    second.set_checked(((3000, 4000),), True)
    assert second.ranges == ((1000, 2000), (3000, 4000))
    assert FilmstripCheckStore(path, "race-2", 1).ranges == ((7000, 8000),)
    assert FilmstripCheckStore(path, "race-1", 2).ranges == ((9000, 10000),)


def test_save_failure_keeps_previous_progress_and_damaged_file_is_not_replaced(tmp_path, monkeypatch):
    path = tmp_path / "checks.jsonl"
    store = FilmstripCheckStore(path, "race-1", 1)
    store.set_checked(((1000, 2000),), True)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(checks, "append_jsonl_records", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        store.set_checked(((1000, 2000),), False)
    assert store.ranges == ((1000, 2000),)
    assert path.read_bytes() == before
    path.write_bytes(before + b'{"broken":')
    damaged = path.read_bytes()
    with pytest.raises(ValueError):
        FilmstripCheckStore(path, "race-1", 1)
    assert path.read_bytes() == damaged
