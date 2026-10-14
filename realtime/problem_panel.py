"""Finish problem list: the operator's main entry after the race.

Shows only what needs a decision: auto-passed riders are counted, not listed.
Each item has a crossing card, a one-second-either-side mini filmstrip, a
jump to the original frame, and the actions that fit its type. Decisions are
journaled with reason, frame and calibration version; the three hand-off
lists for CycleRace are exported from them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QProcess, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .chip_history import format_chip_time
from .problem_analysis import OUTPUT_DIRNAME, PROBLEM_LIST_FILENAME
from .problem_items import (
    ACTION_CHANGE,
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_LABELS,
    ACTION_RECORD_BIB,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    STATUS_INFO,
    ProblemItem,
)
from .problem_resolutions import (
    RESOLUTIONS_FILENAME,
    ProblemResolutionStore,
    export_lists,
    same_frame_conflicts,
)


STATE_PENDING = "待处理"
STATE_DISPUTED = "有争议"
STATE_RESOLVED = "已处理"
STATE_UPSTREAM = "CycleRace 已处理"
STATE_EXCLUDED = "已排除"
STATE_INFO = "参考"
STATE_STALE = "上游已修改，请重新处理"


class ProblemListPanel(QFrame):
    jump_requested = pyqtSignal(object)
    summary_changed = pyqtSignal(str)
    analysis_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reviewPanel")
        self.problem_dir: Optional[Path] = None
        self.workspace_dir: Optional[Path] = None
        self.payload: Optional[dict] = None
        self.items: list[ProblemItem] = []
        self.rows: list[ProblemItem] = []
        self.store: Optional[ProblemResolutionStore] = None
        self._signature = None
        self._events_provider: Callable[[], object] = lambda: ()
        self._process: Optional[QProcess] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        controls = QHBoxLayout()
        self.summary = QLabel("问题清单：尚未分析录像", self)
        self.summary.setWordWrap(True)
        controls.addWidget(self.summary, 1)
        self.show_closed = QCheckBox("显示已处理和已排除", self)
        self.show_closed.toggled.connect(self._render)
        controls.addWidget(self.show_closed)
        self.expand_groups = QCheckBox("展开集团和拥挤时段", self)
        self.expand_groups.setToolTip("集团和拥挤时段里的问题归在一条里核对；展开后逐条显示")
        self.expand_groups.toggled.connect(self._render)
        controls.addWidget(self.expand_groups)
        self.analyze_button = QPushButton("分析录像", self)
        self.analyze_button.setToolTip("在单独的低优先级进程中分析整段录像，不影响录像和计时")
        self.analyze_button.clicked.connect(self._toggle_analysis)
        controls.addWidget(self.analyze_button)
        self.export_button = QPushButton("导出清单", self)
        self.export_button.setToolTip("导出补录、建议删除、建议改号改圈三张清单给 CycleRace")
        self.export_button.clicked.connect(self.export)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)
        splitter = QSplitter(Qt.Horizontal, self)
        self.table = QTableWidget(0, 5, splitter)
        self.table.setObjectName("finishProblemTable")
        self.table.setHorizontalHeaderLabels(["时间", "类型", "号码", "状态", "说明"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((96, 136, 80, 120)):
            self.table.setColumnWidth(column, width)
        self.table.currentCellChanged.connect(lambda row, *_args: self._show(row))
        self.table.cellDoubleClicked.connect(lambda *_args: self._jump())
        detail = QWidget(splitter)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(4, 0, 0, 0)
        self.title = QLabel("选择一条问题查看过线卡片", detail)
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setWordWrap(True)
        detail_layout.addWidget(self.title)
        self.detail = QLabel("", detail)
        self.detail.setWordWrap(True)
        detail_layout.addWidget(self.detail)
        images = QHBoxLayout()
        self.card = QLabel(detail)
        self.card.setFixedSize(240, 240)
        self.card.setAlignment(Qt.AlignCenter)
        self.card.setStyleSheet("background: #e9edf0;")
        images.addWidget(self.card)
        self.filmstrip = QLabel(detail)
        self.filmstrip.setMinimumHeight(80)
        self.filmstrip.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.filmstrip.setToolTip("前后各约 1 秒，每 0.25 秒一张")
        images.addWidget(self.filmstrip, 1)
        detail_layout.addLayout(images)
        inputs = QHBoxLayout()
        self.bib_input = QLineEdit(detail)
        self.bib_input.setPlaceholderText("号码（补录 / 要删除 / 改成）")
        inputs.addWidget(self.bib_input)
        self.reason_input = QLineEdit(detail)
        self.reason_input.setPlaceholderText("原因")
        inputs.addWidget(self.reason_input, 2)
        detail_layout.addLayout(inputs)
        self.action_row = QHBoxLayout()
        self.jump_button = QPushButton("跳到原帧", detail)
        self.jump_button.clicked.connect(self._jump)
        self.action_row.addWidget(self.jump_button)
        self.action_buttons: list[QPushButton] = []
        self.reopen_button = QPushButton("重新处理", detail)
        self.reopen_button.clicked.connect(lambda: self._resolve("reopen"))
        self.action_row.addWidget(self.reopen_button)
        self.action_row.addStretch(1)
        detail_layout.addLayout(self.action_row)
        detail_layout.addStretch(1)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.hint = QLabel("", self)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #667085; font-size: 9pt;")
        layout.addWidget(self.hint)
        self._show(-1)

    # ----- data -------------------------------------------------------
    def set_workspace(self, workspace_dir) -> None:
        workspace_dir = Path(workspace_dir)
        if workspace_dir == self.workspace_dir:
            return
        self.workspace_dir = workspace_dir
        self.problem_dir = workspace_dir / OUTPUT_DIRNAME
        self.store = None
        self._signature = None
        self.refresh()

    def set_events_provider(self, provider: Callable[[], object]) -> None:
        self._events_provider = provider

    def refresh(self) -> None:
        if self.problem_dir is None:
            return
        path = self.problem_dir / PROBLEM_LIST_FILENAME
        try:
            stat = path.stat()
        except OSError:
            self.payload, self.items = None, []
            self._signature = None
            self._render()
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != self._signature:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                items = [ProblemItem.from_payload(item) for item in payload.get("items", [])]
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.hint.setText(f"问题清单读取失败：{error}")
                return
            self.payload, self.items = payload, items
            self._signature = signature
        if self.store is None:
            self.store = ProblemResolutionStore(self.problem_dir / RESOLUTIONS_FILENAME)
        self._render()

    def _upstream_closed(self, item: ProblemItem) -> bool:
        """Every chip record of the item was voided or deleted upstream since."""

        if not item.event_ids:
            return False
        try:
            current = {event.event_id: event for event in self._events_provider()}
        except Exception:  # noqa: BLE001 - the list must stay usable
            return False
        if not current:
            return False
        involved = [current.get(event_id) for event_id in item.event_ids]
        return all(event is None or not event.is_active for event in involved)

    def state(self, item: ProblemItem) -> str:
        if item.status == STATUS_EXCLUDED:
            return STATE_EXCLUDED
        if item.status == STATUS_INFO:
            return STATE_INFO
        if item.status == STATUS_CLOSED_UPSTREAM or (item.pending and self._upstream_closed(item)):
            return STATE_UPSTREAM
        if self.store is not None:
            resolution, stale = self.store.state(item)
            if resolution is not None:
                return STATE_STALE if stale else f"{STATE_RESOLVED}：{resolution.label}"
        return STATE_DISPUTED if item.status == STATUS_DISPUTED else STATE_PENDING

    @staticmethod
    def is_pending_state(state: str) -> bool:
        return state in (STATE_PENDING, STATE_DISPUTED, STATE_STALE)

    def summary_text(self) -> str:
        if self.payload is None:
            return "问题清单：尚未分析录像"
        states = [self.state(item) for item in self.items if not item.grouped]
        pending = sum(self.is_pending_state(state) for state in states)
        done = sum(state.startswith(STATE_RESOLVED) or state == STATE_UPSTREAM for state in states)
        summary = self.payload.get("summary", {})
        return (
            f"自动通过 {summary.get('auto_passed', 0)} 人 · 待处理 {pending} 条 · "
            f"无法核实 {summary.get('unverifiable', 0)} 人 · 已处理 {done} 条"
        )

    def _render(self) -> None:
        selected = self.rows[self.table.currentRow()].item_id if 0 <= self.table.currentRow() < len(self.rows) else None
        show_all = self.show_closed.isChecked()
        members: dict[str, list[ProblemItem]] = {}
        for item in self.items:
            if item.grouped:
                members.setdefault(item.grouped, []).append(item)
        self.rows = []
        for item in self.items:
            if item.grouped:
                continue
            if show_all or self.is_pending_state(self.state(item)) or self.state(item) == STATE_INFO:
                self.rows.append(item)
            if self.expand_groups.isChecked():
                self.rows += [
                    member for member in members.get(item.item_id, [])
                    if show_all or self.is_pending_state(self.state(member))
                ]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        restore = -1
        for index, item in enumerate(self.rows):
            state = self.state(item)
            values = (
                format_chip_time(item.chip_time_ms) if item.chip_time_ms is not None else "--",
                ("└ " if item.grouped else "") + item.title,
                "、".join(item.bibs[:4]) + ("…" if len(item.bibs) > 4 else ""),
                state, item.detail,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 3 and self.is_pending_state(state):
                    cell.setForeground(QColor("#b54747"))
                self.table.setItem(index, column, cell)
            if item.item_id == selected:
                restore = index
        self.table.blockSignals(False)
        if restore >= 0:
            self.table.setCurrentCell(restore, 0)
        self._show(self.table.currentRow())
        text = self.summary_text()
        self.summary.setText(text)
        self.summary_changed.emit(text)

    def current_item(self) -> Optional[ProblemItem]:
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    # ----- detail -----------------------------------------------------
    def _show(self, row: int) -> None:
        item = self.rows[row] if 0 <= row < len(self.rows) else None
        for button in self.action_buttons:
            self.action_row.removeWidget(button)
            button.deleteLater()
        self.action_buttons = []
        self.card.clear()
        self.filmstrip.clear()
        if item is None:
            self.title.setText("选择一条问题查看过线卡片")
            self.detail.setText("")
            self.jump_button.setEnabled(False)
            self.reopen_button.setEnabled(False)
            return
        state = self.state(item)
        self.title.setText(
            f"{item.title} · {format_chip_time(item.chip_time_ms) if item.chip_time_ms else '--'} · {state}"
        )
        detail = item.detail + (f"\n{item.status_note}" if item.status_note else "")
        bike = item.evidence.get("bike")
        if bike:
            detail += f"\n认车：{bike.get('label')} {bike.get('score')}" + (f"（{bike['reason']}）" if bike.get("reason") else "")
        if item.calibration_version:
            detail += f"\n校时版本 v{item.calibration_version}"
        self.detail.setText(detail)
        for key, target in (("card", self.card), ("filmstrip", self.filmstrip)):
            relative = item.evidence.get(key)
            if relative and self.problem_dir is not None:
                pixmap = QPixmap(str(self.problem_dir / relative))
                if not pixmap.isNull():
                    if key == "card":
                        target.setPixmap(pixmap.scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    else:
                        target.setPixmap(pixmap.scaledToHeight(80, Qt.SmoothTransformation))
        self.jump_button.setEnabled(bool(item.evidence.get("frame")))
        self.reopen_button.setEnabled(state.startswith(STATE_RESOLVED))
        for action in item.actions:
            button = QPushButton(ACTION_LABELS.get(action, action), self)
            button.setProperty("problemAction", action)
            button.clicked.connect(lambda _checked=False, action=action: self._resolve(action))
            self.action_row.insertWidget(self.action_row.count() - 1, button)
            self.action_buttons.append(button)

    def _jump(self) -> None:
        item = self.current_item()
        if item is not None and item.evidence.get("frame"):
            self.jump_requested.emit(dict(item.evidence["frame"]))
            self.hint.setText("已跳到原帧；在机位画面中逐帧核对号码，再回到清单处理。")

    def _claims(self, exclude: str) -> list:
        """Bibs already tied to frames: auto-passed riders and other decisions."""

        claims = []
        for passed in (self.payload or {}).get("auto_passed", []):
            claims.append((passed.get("bib", ""), passed.get("frame") or {}, ()))
        if self.store is not None:
            for resolution in self.store.resolutions():
                if resolution.item_id != exclude and resolution.action == ACTION_RECORD_BIB:
                    claims.append((resolution.bib, resolution.frame, ()))
        return claims

    def _resolve(self, action: str) -> None:
        item = self.current_item()
        if item is None or self.store is None:
            return
        bib = self.bib_input.text().strip()
        reason = self.reason_input.text().strip()
        if action == ACTION_RECORD_BIB:
            if not bib:
                self.hint.setText("认号补录需要先填写号码牌上的号码。")
                return
            frame = item.evidence.get("frame") or {}
            conflicts = same_frame_conflicts(frame, (), bib, self._claims(item.item_id)) if frame else []
            if conflicts and QMessageBox.question(
                self, "同一帧已有其他号码",
                f"这一帧同一次过线已对应 {'、'.join(conflicts)}。\n确认补录 {bib} 前请再看号码牌，是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        if action in (ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP) and not reason:
            self.hint.setText("建议删除需要填写原因。")
            return
        if action == ACTION_DELETE_EXTRA_CHIP and len(item.bibs) > 1 and bib not in item.bibs:
            self.hint.setText(f"请在号码框填写要删除的芯片号码：{'、'.join(item.bibs)}。")
            return
        if action == ACTION_CHANGE and not bib:
            self.hint.setText("建议改号改圈需要在号码框填写改成的号码或圈数。")
            return
        try:
            self.store.resolve(
                item, action,
                bib=bib if action == ACTION_RECORD_BIB else "",
                target_bib=bib if action in (ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP, ACTION_CHANGE) else "",
                reason=reason,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"处理未保存：{error}")
            return
        self.bib_input.clear()
        self.reason_input.clear()
        self.hint.setText(f"已记录：{ACTION_LABELS.get(action, '重新处理')}。")
        self._render()

    def export(self) -> Optional[dict]:
        if self.store is None or self.problem_dir is None:
            return None
        try:
            counts = export_lists(self.items, self.store, self.problem_dir / "exports")
        except OSError as error:
            self.hint.setText(f"导出失败：{error}")
            return None
        self.hint.setText(
            "已导出到 " + str(self.problem_dir / "exports") + "：" + "，".join(
                f"{name.removesuffix('.csv')} {count} 条" for name, count in counts.items()
            )
        )
        return counts

    # ----- background analysis ----------------------------------------
    def analysis_command(self) -> tuple[str, list[str]]:
        if getattr(sys, "frozen", False):
            return sys.executable, ["--analyze-problems", str(self.workspace_dir)]
        return sys.executable, ["-m", "realtime.review_main", "--analyze-problems", str(self.workspace_dir)]

    def _toggle_analysis(self) -> None:
        if self._process is not None:
            self._process.kill()
            return
        if self.workspace_dir is None:
            return
        process = QProcess(self)
        program, arguments = self.analysis_command()
        process.setProgram(program)
        process.setArguments(arguments)
        process.readyReadStandardOutput.connect(self._read_progress)
        process.finished.connect(self._analysis_done)
        self._process = process
        self.analyze_button.setText("停止分析")
        self.hint.setText("正在后台分析录像（低优先级，可随时停止，下次从已完成的部分继续）……")
        process.start()

    def _read_progress(self) -> None:
        if self._process is None:
            return
        for raw in bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace").splitlines():
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("stage") == "done":
                continue
            if message.get("stage") == "skipped":
                self._skip_message = str(message.get("message", ""))
                continue
            total = message.get("total") or 0
            self.hint.setText(f"后台分析：{message.get('stage')} {message.get('done')}/{total}")

    def _analysis_done(self, *_args) -> None:
        self._process = None
        self.analyze_button.setText("分析录像")
        self._signature = None
        self.refresh()
        skipped, self._skip_message = getattr(self, "_skip_message", ""), ""
        if skipped:
            self.hint.setText(skipped)
        else:
            self.hint.setText("录像分析结束。" if self.payload else "录像分析未完成，请查看日志。")
        self.analysis_finished.emit()


__all__ = ["ProblemListPanel"]
