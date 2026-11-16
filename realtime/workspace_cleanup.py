"""Old race folders left by tests: list them, delete the ones the operator ticks.

11-14: before a race the cameras and chips are tried again and again, and every
try used to leave a folder ("文件夹实在太乱、太多了").  Nothing here decides
what a test is: the operator sees what each folder holds and chooses.  The
folder in use, the root itself and anything outside the root are never offered.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .arrival_review import ARRIVAL_REVIEWS_FILENAME
from .event_workspace import EventWorkspaceError, discover_event_workspaces
from .problem_analysis import OUTPUT_DIRNAME
from .recording_lock import FolderInUseError, RecordingFolderLock
from .workspace_context import is_event_folder_under

PASSAGES = "cyclerace_passage_events.jsonl"
VIDEO_SUFFIXES = {".mkv", ".avi", ".mp4", ".ts"}


@dataclass(frozen=True)
class OldFolder:
    path: Path
    label: str            # 比赛 / 文件夹, as it reads under the root
    modified_at_ms: int
    chips: int
    videos: int
    video_bytes: int
    judged: int           # lines of what people decided here

    @property
    def empty(self) -> bool:
        """Nothing a person would miss: no chip read, no recording, nothing judged."""
        return not self.chips and not self.videos and not self.judged


def _lines(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            return sum(1 for line in stream if line.strip())
    except OSError:
        return 0


def old_folders(root, current=None) -> list[OldFolder]:
    """Every saved race under ROOT except the root itself and CURRENT, oldest first."""
    root = Path(root).expanduser().resolve()
    current = Path(current).expanduser().resolve() if current is not None else None
    try:
        workspaces = discover_event_workspaces(root)
    except EventWorkspaceError:
        return []
    rows = []
    for workspace in workspaces:
        path = Path(workspace.path).resolve()
        if path == root or path == current or not is_event_folder_under(path, root):
            continue
        videos, size = 0, 0
        try:
            for entry in (path / "videos").iterdir():
                if entry.suffix.lower() in VIDEO_SUFFIXES and entry.is_file():
                    videos += 1
                    size += entry.stat().st_size
        except OSError:
            pass
        rows.append(OldFolder(path, str(path.relative_to(root)).replace("\\", " / "),
                              workspace.modified_at_ms, _lines(path / PASSAGES), videos, size,
                              _lines(path / OUTPUT_DIRNAME / ARRIVAL_REVIEWS_FILENAME)))
    return sorted(rows, key=lambda row: (row.modified_at_ms, row.label))


def delete_folders(paths, root, current=None) -> list[str]:
    """Remove each folder of PATHS; what could not go is said, one line each."""
    root = Path(root).expanduser().resolve()
    current = Path(current).expanduser().resolve() if current is not None else None
    failures = []
    for path in paths:
        path = Path(path).resolve()
        if path == root or path == current or not is_event_folder_under(path, root) or not path.is_dir():
            failures.append(f"{path.name}：不是可清理的赛事文件夹，未改动")
            continue
        lock = RecordingFolderLock()
        try:
            lock.acquire(path)       # a window recording into it keeps it
        except FolderInUseError as error:
            failures.append(f"{path.name}：另一个 FinishReview{error.owner}正在向它录像，未改动")
            continue
        except OSError as error:
            failures.append(f"{path.name}：{error}")
            continue
        lock.release()
        try:
            shutil.rmtree(path)
        except OSError as error:
            failures.append(f"{path.name}：{error}")
            continue
        parent = path.parent     # a competition's folder with no race left in it goes too
        try:
            if parent != root and parent.parent == root and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    return failures


class OldFolderDialog(QDialog):
    """Tick the folders to delete.  Folders with recordings or judging start unticked."""

    COLUMNS = ("删除", "比赛 / 文件夹", "最后改动", "芯片", "录像", "人工记录")

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("清理旧赛事文件夹")
        self.resize(820, 460)
        self.rows = list(rows)
        layout = QVBoxLayout(self)
        note = QLabel("勾选要删除的文件夹。删除后不能恢复；正在使用的这一场和保存根目录不在列表里。\n"
                      "芯片读数、录像、人工记录都没有的已替你勾上；有任何一样的标成橙色，默认不勾。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(self.rows), len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        for index, row in enumerate(self.rows):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            tick.setCheckState(Qt.Checked if row.empty else Qt.Unchecked)
            when = datetime.fromtimestamp(row.modified_at_ms / 1000).strftime("%Y-%m-%d %H:%M")
            video = f"{row.videos} 个 / {row.video_bytes / 1e9:.1f} GB" if row.videos else "无"
            cells = (tick, QTableWidgetItem(row.label), QTableWidgetItem(when), QTableWidgetItem(str(row.chips)),
                     QTableWidgetItem(video), QTableWidgetItem(f"{row.judged} 条" if row.judged else "无"))
            for column, cell in enumerate(cells):
                if not row.empty:
                    cell.setBackground(QColor("#fff1d6"))
                self.table.setItem(index, column, cell)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._count)
        layout.addWidget(self.table, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Cancel).setText("不删，关闭")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._count()

    def chosen(self) -> list[OldFolder]:
        return [row for index, row in enumerate(self.rows)
                if self.table.item(index, 0).checkState() == Qt.Checked]

    def _count(self, *_):
        chosen = self.chosen()
        button = self.buttons.button(QDialogButtonBox.Ok)
        button.setText(f"删除勾选的 {len(chosen)} 个文件夹")
        button.setEnabled(bool(chosen))


def clean_old_folders(parent, root, current=None) -> str:
    """The whole errand; returns one line for the operator ("" when nothing was done)."""
    rows = old_folders(root, current)
    if not rows:
        QMessageBox.information(parent, "清理旧赛事文件夹", "保存目录里没有别的赛事文件夹。")
        return ""
    dialog = OldFolderDialog(rows, parent)
    if dialog.exec_() != QDialog.Accepted:
        return ""
    chosen = dialog.chosen()
    kept = [row for row in chosen if not row.empty]
    words = f"将永久删除 {len(chosen)} 个赛事文件夹，不能恢复。"
    if kept:
        words += f"\n其中 {len(kept)} 个有芯片读数、录像或人工记录：\n" + "\n".join(row.label for row in kept[:8])
    if QMessageBox.question(parent, "清理旧赛事文件夹", words + "\n\n确定删除？",
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
        return ""
    failures = delete_folders([row.path for row in chosen], root, current)
    if failures:
        QMessageBox.warning(parent, "部分文件夹未能删除", "\n".join(failures[:8]))
    return f"已删除 {len(chosen) - len(failures)} 个旧赛事文件夹。"
