import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from realtime.race_metadata import RaceGroupMetadata, RaceMetadata, RaceMetadataStore
from realtime.workspace_cleanup import OldFolderDialog, delete_folders, old_folders


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def race(root, competition, stage, *, chips=0, videos=0, judged=0):
    folder = root / competition / stage
    (folder / "finish_problems").mkdir(parents=True)
    RaceMetadataStore(folder / "cyclerace_race_metadata.json").store(RaceMetadata(
        race_id=f"{competition}-{stage}", stage_id="stage-1", revision=1, emitted_at_ms=1,
        race_name=competition, stage_name=stage, groups=(RaceGroupMetadata("open", "公开组"),)))
    if chips:
        (folder / "cyclerace_passage_events.jsonl").write_text("{}\n" * chips, encoding="utf-8")
    if videos:
        (folder / "videos").mkdir()
        for index in range(videos):
            (folder / "videos" / f"camera_01_archive_{index:04d}.mkv").write_bytes(b"x" * 10)
    if judged:
        (folder / "finish_problems" / "arrival_reviews.jsonl").write_text("{}\n" * judged, encoding="utf-8")
    return folder


def test_folders_are_listed_with_what_they_hold_and_the_one_in_use_is_left_out(tmp_path):
    """11-14：测试留下的文件夹太多太乱。列出每个里面有什么，由人勾选；正在用的这一场不列。"""

    test = race(tmp_path, "接力测试赛", "2026-11-12 接力赛段")
    real = race(tmp_path, "古浪站", "2026-10-18 比赛", chips=3, videos=2, judged=5)
    live = race(tmp_path, "古浪站", "2026-10-19 复核")
    rows = old_folders(tmp_path, live)
    found = {row.path: row for row in rows}
    assert set(found) == {test, real}
    assert found[test].empty and (found[test].chips, found[test].videos, found[test].judged) == (0, 0, 0)
    assert not found[real].empty
    assert (found[real].chips, found[real].videos, found[real].video_bytes, found[real].judged) == (3, 2, 20, 5)
    assert found[real].label == "古浪站 / 2026-10-18 比赛"


def test_only_ticked_folders_go_and_an_emptied_competition_folder_goes_with_them(tmp_path):
    test = race(tmp_path, "接力测试赛", "2026-11-12 接力赛段")
    real = race(tmp_path, "古浪站", "2026-10-18 比赛", videos=1)
    live = race(tmp_path, "古浪站", "2026-10-19 复核")

    assert delete_folders([test], tmp_path, live) == []
    assert not test.exists() and not test.parent.exists()      # the competition had nothing else
    assert real.is_dir() and live.is_dir()

    # Never the folder in use, the root, or something outside the root.
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    failures = delete_folders([live, tmp_path, outside], tmp_path, live)
    assert len(failures) == 3 and live.is_dir() and tmp_path.is_dir() and outside.is_dir()
    assert real.parent.is_dir()                                 # 古浪站 still holds races


def test_the_dialog_ticks_only_folders_nobody_would_miss(qapp, tmp_path):
    test = race(tmp_path, "接力测试赛", "2026-11-12 接力赛段")
    real = race(tmp_path, "古浪站", "2026-10-18 比赛", videos=1, judged=2)
    race(tmp_path, "可克达拉电力杯", "2026-09-29 个人计时赛", chips=290)   # chips only: a real race's reads
    rows = old_folders(tmp_path)
    dialog = OldFolderDialog(rows)
    assert [row.path for row in dialog.chosen()] == [test]
    index = next(i for i, row in enumerate(dialog.rows) if row.path == real)
    dialog.table.item(index, 0).setCheckState(Qt.Checked)      # the operator's own choice
    assert {row.path for row in dialog.chosen()} == {test, real}
    for i in range(len(dialog.rows)):
        dialog.table.item(i, 0).setCheckState(Qt.Unchecked)
    assert not dialog.buttons.buttons()[0].isEnabled() or not dialog.chosen()
