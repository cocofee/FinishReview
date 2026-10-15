"""Finish problem list: the operator's main entry during and after the race.

Shows only what needs a decision: auto-passed riders are counted, not listed.
Each item has a crossing card, a one-second-either-side mini filmstrip, a
jump to the original frame, and the actions that fit its type. Decisions are
journaled with reason, frame and calibration version; the three hand-off
lists for CycleRace are exported from them.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    QScrollArea,
)

from .chip_history import format_chip_time
from .problem_analysis import OUTPUT_DIRNAME, PROBLEM_LIST_FILENAME
from .problem_items import (
    ACTION_CHANGE,
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_LABELS,
    ACTION_RECORD_BIB,
    ACTION_SYNCED,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    STATUS_INFO,
    STATUS_WAITING,
    KIND_BUNCH,
    KIND_OBSTRUCTION,
    KIND_MANUAL_REVIEW,
    CATEGORY_VIDEO,
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
STATE_WAITING = "待证据"
STATE_HANDOFF = "待 CycleRace"


class ProblemListPanel(QFrame):
    jump_requested = pyqtSignal(object)
    summary_changed = pyqtSignal(str)
    analysis_finished = pyqtSignal()
    current_changed = pyqtSignal(object)

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
        self._render_signature = None
        self._events_provider: Callable[[], object] = lambda: ()
        self._process: Optional[QProcess] = None
        self._progress_buffer = b""
        self.analysis_status = "等待已封存录像"
        self.bunch_store = None
        self._drafts = {}
        self._held = {}
        self._operator_items = {}
        self._shown_id = None
        self._shown_frame = None
        self._scope = ("", "")
        self._browsing = False
        self._live = False
        self._live_paused = False
        self._next_analysis = 0.0
        self._last_live_signature = None
        self._final_analysis_pending = False
        self._finalizing_live = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(400)
        self._draft_timer.timeout.connect(self.save_drafts)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        controls = QHBoxLayout()
        self.summary = QLabel("问题清单：尚未分析录像", self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.queue_filter = QComboBox(self)
        self.queue_filter.addItems(["待处理", "待证据", "暂存", "交接 / 已处理", "全部"])
        self.queue_filter.currentIndexChanged.connect(self._render)
        controls.addWidget(self.queue_filter)
        self.show_closed = QCheckBox("显示已处理和已排除", self)
        self.show_closed.toggled.connect(self._render)
        controls.addWidget(self.show_closed)
        self.expand_groups = QCheckBox("展开集团和拥挤时段", self)
        self.expand_groups.setToolTip("集团和拥挤时段里的问题归在一条里核对；展开后逐条显示")
        self.expand_groups.toggled.connect(self._render)
        controls.addWidget(self.expand_groups)
        self.bunch_button = QPushButton("打开集团逐帧核对", self)
        self.bunch_button.clicked.connect(self._open_bunch)
        controls.addWidget(self.bunch_button)
        self.analyze_button = QPushButton("分析录像", self)
        self.analyze_button.setToolTip("低优先级后台分析；比赛中只读取已封存短段，不等待五分钟归档")
        self.analyze_button.clicked.connect(self._toggle_analysis)
        controls.addWidget(self.analyze_button)
        self.export_button = QPushButton("导出清单", self)
        self.export_button.setToolTip("导出补录、建议删除、建议改号改圈三张清单给 CycleRace")
        self.export_button.clicked.connect(self.export)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)
        splitter = self.splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        self.table = QTableWidget(0, 5, splitter)
        self.table.setObjectName("finishProblemTable")
        self.table.setStyleSheet("QTableWidget::item:selected {background:#d6eeea; color:#123d36;}")
        self.table.setHorizontalHeaderLabels(["时间", "类型", "号码", "状态", "说明"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumWidth(240)
        self.table.setColumnHidden(4, True)
        for column, width in enumerate((78, 110, 55, 85)):
            self.table.setColumnWidth(column, width)
        self.table.currentCellChanged.connect(lambda row, *_args: self._show(row))
        self.table.cellDoubleClicked.connect(lambda *_args: self._jump())
        detail_scroll = QScrollArea(splitter)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setMinimumWidth(260)
        detail = QWidget()
        detail_scroll.setWidget(detail)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(4, 0, 0, 0)
        self.title = QLabel("选择一条问题查看过线卡片", detail)
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setWordWrap(True)
        detail_layout.addWidget(self.title)
        self.detail = QLabel("", detail)
        self.detail.setWordWrap(True)
        detail_layout.addWidget(self.detail)
        images = QVBoxLayout()
        self.card = QLabel(detail)
        self.card.setFixedSize(240, 140)
        self.card.setAlignment(Qt.AlignCenter)
        self.card.setStyleSheet("background: #e9edf0;")
        images.addWidget(self.card)
        self.filmstrip = QLabel(detail)
        self.filmstrip.setFixedSize(240, 48)
        self.filmstrip.setScaledContents(True)
        self.filmstrip.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.filmstrip.setToolTip("前后各约 1 秒，每 0.25 秒一张")
        images.addWidget(self.filmstrip, 1)
        detail_layout.addLayout(images)
        inputs = QVBoxLayout()
        self.bib_input = QLineEdit(detail)
        self.bib_input.setPlaceholderText("号码（补录 / 要删除 / 改成）")
        inputs.addWidget(self.bib_input)
        self.reason_input = QLineEdit(detail)
        self.reason_input.setPlaceholderText("原因")
        reason_row = QHBoxLayout()
        reason_row.addWidget(self.reason_input, 1)
        inputs.addLayout(reason_row)
        self.bib_input.textChanged.connect(self._remember_draft)
        self.reason_input.textChanged.connect(self._remember_draft)
        detail_layout.addLayout(inputs)
        self.action_row = QVBoxLayout()
        self.jump_button = QPushButton("跳到原帧", detail)
        self.jump_button.clicked.connect(self._jump)
        self.action_row.addWidget(self.jump_button)
        self.action_buttons: list[QPushButton] = []
        self.reopen_button = QPushButton("重新处理", detail)
        self.reopen_button.clicked.connect(self._reopen)
        self.action_row.addWidget(self.reopen_button)
        self.hold_button = QPushButton("暂存", detail)
        self.hold_button.setToolTip("填写原因后暂存，不标记为已解决；可从暂存清单恢复")
        self.hold_button.clicked.connect(self._hold)
        reason_row.addWidget(self.hold_button)
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

    def _remember_draft(self):
        if self._shown_id:
            self._drafts[self._shown_id] = [self.bib_input.text(), self.reason_input.text()]
            self._draft_timer.start()

    def save_drafts(self):
        self._draft_timer.stop()
        if self.problem_dir is None:
            return
        try:
            self.problem_dir.mkdir(parents=True, exist_ok=True)
            path = self.problem_dir / "operator_drafts.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"drafts": self._drafts, "held": self._held,
                                             "items": self._operator_items}, ensure_ascii=False),
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            self.hint.setText(f"草稿保存失败：{error}")

    def _hold(self):
        item = self.current_item()
        if item is None:
            return
        if item.item_id in self._held:
            self._held.pop(item.item_id)
        elif not self.reason_input.text().strip():
            self.hint.setText("暂存需要填写原因；暂存不会将问题标记为已解决。")
            return
        else:
            self._held[item.item_id] = self.reason_input.text().strip()
        self.save_drafts()
        self._render()

    def set_browsing(self, browsing):
        self._browsing = bool(browsing)
        for button in self.action_buttons:
            button.setEnabled(not self._browsing)
        item = self.current_item()
        self.bunch_button.setEnabled(not self._browsing and not (item and item.evidence.get("superseded")))
        if browsing:
            self.hint.setText("正在浏览其他时间；当前问题与输入已保留。请回到当前问题再提交决定。")

    # ----- data -------------------------------------------------------
    def set_workspace(self, workspace_dir) -> None:
        workspace_dir = Path(workspace_dir)
        if workspace_dir == self.workspace_dir:
            return
        self.stop_analysis()
        self.save_drafts()
        self.workspace_dir = workspace_dir
        self.problem_dir = workspace_dir / OUTPUT_DIRNAME
        self.store = None
        self.bunch_store = None
        self._signature = None
        self._render_signature = None
        self._shown_id = None
        self._shown_frame = None
        self.items, self.rows = [], []
        self.bib_input.clear()
        self.reason_input.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._live = False
        self._browsing = False
        self._next_analysis = 0.0
        self._drafts, self._held, self._operator_items = {}, {}, {}
        self._final_analysis_pending = False
        self._finalizing_live = False
        self._last_live_signature = None
        try:
            state = json.loads((self.problem_dir / "operator_drafts.json").read_text(encoding="utf-8"))
            self._drafts, self._held = state.get("drafts", {}), state.get("held", {})
            self._operator_items = state.get("items", {})
        except (OSError, ValueError):
            pass
        self.refresh()
        if self.current_item() is None:
            self.current_changed.emit(None)

    def set_scope(self, race_id, stage_id):
        scope = (str(race_id or ""), str(stage_id or ""))
        if scope == self._scope:
            return
        self.stop_analysis()
        self._scope = scope
        self._signature = None
        self._render_signature = None
        self._last_live_signature = None
        self.items, self.rows = [], []
        self._shown_id = None
        self.bib_input.clear()
        self.reason_input.clear()
        self.refresh()
        if self.current_item() is None:
            self.current_changed.emit(None)

    def set_events_provider(self, provider: Callable[[], object]) -> None:
        self._events_provider = provider

    def refresh(self) -> None:
        if self.problem_dir is None:
            return
        current = self.current_item()
        self.items = [item for item in self.items if not item.evidence.get("superseded") or item == current]
        path = self.problem_dir / PROBLEM_LIST_FILENAME
        try:
            stat = path.stat()
        except OSError:
            self.payload, self.items = None, []
            self._signature = None
            stat = None
        signature = (stat.st_mtime_ns, stat.st_size) if stat else None
        if signature != self._signature:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                items = [ProblemItem.from_payload(item) for item in payload.get("items", [])]
                if self._scope[0] and (payload.get("race_id"), payload.get("stage_id", "")) != self._scope:
                    payload, items = None, []
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.hint.setText(f"问题清单读取失败：{error}")
                return
            self.payload, self.items = payload, items
            self._signature = signature
        if self.store is None:
            self.store = ProblemResolutionStore(self.problem_dir / RESOLUTIONS_FILENAME)
        ids = {item.item_id for item in self.items}
        self.items += [ProblemItem.from_payload(value) for key, value in self._operator_items.items() if key not in ids]
        try:
            manual = json.loads((self.problem_dir / "manual_problems.json").read_text(encoding="utf-8"))
            self.items = [item for item in self.items if item.kind != KIND_MANUAL_REVIEW]
            self.items += [ProblemItem.from_payload(item) for item in manual]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.hint.setText(f"人工问题读取失败：{error}")
        if self._scope[0]:
            self.items = [item for item in self.items if not item.evidence.get("operator_scope")
                          or tuple(item.evidence["operator_scope"]) == self._scope]
        if current is not None and current.item_id not in {item.item_id for item in self.items}:
            self.items.append(replace(current, status=STATUS_INFO,
                                      status_note="后台证据已更新，此旧问题仅保留供查看；请选择清单中的新问题。",
                                      evidence=dict(current.evidence, superseded=True)))
        from .bunch_review import BunchReviewStore
        try:
            self.bunch_store = BunchReviewStore(self.problem_dir)
        except (OSError, ValueError) as error:
            self.hint.setText(f"集团核对记录读取失败：{error}")
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
        if item.status == STATUS_WAITING:
            return STATE_WAITING
        if item.status == STATUS_EXCLUDED:
            return STATE_EXCLUDED
        if item.status == STATUS_INFO:
            return STATE_INFO
        if item.status == STATUS_CLOSED_UPSTREAM or (item.pending and self._upstream_closed(item)):
            return STATE_UPSTREAM
        record = self.bunch_store.latest_for(item) if self.bunch_store is not None else None
        if record is not None:
            if record.get("item_snapshot") != item.to_payload():
                return STATE_STALE
            current = {event.event_id: event for event in self._events_provider()}
            for chip in record.get("chips", []):
                event = current.get(chip["event_id"])
                if event is not None and (not event.is_active or event.bib != chip["bib"]
                                          or int(event.timeline_timestamp_ms) != chip["chip_ms"]):
                    return STATE_STALE
            return f"{STATE_RESOLVED}：集团人工核对" if record.get("completed") else STATE_PENDING
        if self.store is not None:
            resolution, stale = self.store.state(item)
            if resolution is not None:
                if not stale and resolution.action in (ACTION_RECORD_BIB, ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP, ACTION_CHANGE):
                    return f"{STATE_HANDOFF}：{resolution.label}"
                return STATE_STALE if stale else f"{STATE_RESOLVED}：{resolution.label}"
        return STATE_DISPUTED if item.status == STATUS_DISPUTED else STATE_PENDING

    @staticmethod
    def is_pending_state(state: str) -> bool:
        return state in (STATE_PENDING, STATE_DISPUTED, STATE_STALE)

    def summary_text(self) -> str:
        if self.payload is None and not self.items:
            return "问题清单：尚未分析录像"
        states = [self.state(item) for item in self.items if not item.grouped]
        pending = sum(self.is_pending_state(state) for state in states)
        done = sum(state.startswith(STATE_RESOLVED) or state == STATE_UPSTREAM for state in states)
        summary = (self.payload or {}).get("summary", {})
        text = (
            f"自动通过 {summary.get('auto_passed', 0)} 人 · 待处理 {pending} 条 · "
            f"无法核实 {summary.get('unverifiable', 0)} 人 · 已处理 {done} 条"
        )
        waiting = sum(state == STATE_WAITING for state in states)
        handoff = sum(state.startswith(STATE_HANDOFF) for state in states)
        return text + (f" · 待证据 {waiting} 条" if waiting else "") + (f" · 待 CycleRace {handoff} 条" if handoff else "")

    def _render(self) -> None:
        signature = (tuple((repr(item.to_payload()), self.state(item)) for item in self.items),
                     self.show_closed.isChecked(), self.expand_groups.isChecked(),
                     self.queue_filter.currentIndex(), tuple(self._held.items()),
                     repr((self.payload or {}).get("summary")))
        if signature == self._render_signature:
            return
        self._render_signature = signature
        selected = self.rows[self.table.currentRow()].item_id if 0 <= self.table.currentRow() < len(self.rows) else None
        show_all = self.show_closed.isChecked()
        mode = self.queue_filter.currentIndex()
        members: dict[str, list[ProblemItem]] = {}
        for item in self.items:
            if item.grouped:
                members.setdefault(item.grouped, []).append(item)
        self.rows = []
        for item in self.items:
            if item.grouped:
                continue
            state = self.state(item)
            pending = self.is_pending_state(state) or state == STATE_INFO
            held = item.item_id in self._held
            visible = (show_all or mode == 4 or (mode == 0 and pending and not held)
                       or (mode == 1 and state == STATE_WAITING) or (mode == 2 and held and pending)
                       or (mode == 3 and not pending and state != STATE_WAITING)
                       or (item.item_id == selected and item.evidence.get("superseded")))
            if visible:
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
                ("暂存" if item.item_id in self._held and self.is_pending_state(state) else state), item.detail,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(item.detail)
                if column == 3 and self.is_pending_state(state):
                    cell.setForeground(QColor("#b54747"))
                self.table.setItem(index, column, cell)
            if item.item_id == selected:
                restore = index
        if restore >= 0:
            self.table.setCurrentCell(restore, 0)
        elif self.rows:
            self.table.setCurrentCell(0, 0)
        self.table.blockSignals(False)
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
        identity = item.item_id if item else None
        changed = identity != self._shown_id
        frame = dict(item.evidence.get("frame", {})) if item else None
        if not changed and frame != self._shown_frame:
            self._browsing = True
        self._shown_frame = frame
        if changed:
            self._shown_id = identity
            bib, reason = self._drafts.get(identity, ("", ""))
            self.bib_input.blockSignals(True)
            self.reason_input.blockSignals(True)
            self.bib_input.setText(bib)
            self.reason_input.setText(reason)
            self.bib_input.blockSignals(False)
            self.reason_input.blockSignals(False)
            self._browsing = False
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
            self.bunch_button.hide()
            self.hold_button.setEnabled(False)
            if changed:
                self.current_changed.emit(None)
            return
        self.hold_button.setEnabled(item.status != STATUS_WAITING)
        self.hold_button.setText("返回待处理" if item.item_id in self._held else "暂存")
        is_bunch = item.kind in (KIND_BUNCH, KIND_OBSTRUCTION)
        self.bunch_button.setVisible(is_bunch)
        self.bib_input.setVisible(not is_bunch)
        self.reason_input.setVisible(True)
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
                        target.setPixmap(pixmap.scaled(240, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    else:
                        target.setPixmap(pixmap.scaledToHeight(80, Qt.SmoothTransformation))
        self.jump_button.setEnabled(bool(item.evidence.get("frame")))
        resolved = state.startswith((STATE_RESOLVED, STATE_HANDOFF))
        self.reopen_button.setEnabled(resolved)
        self.reopen_button.setVisible(resolved)
        actions = (ACTION_SYNCED,) if state.startswith(STATE_HANDOFF) else (() if is_bunch or resolved else item.actions)
        if item.evidence.get("superseded"):
            actions = ()
            self.hold_button.setEnabled(False)
            self.bunch_button.setEnabled(False)
        for action in actions:
            button = QPushButton(ACTION_LABELS.get(action, action), self)
            button.setProperty("problemAction", action)
            button.clicked.connect(lambda _checked=False, action=action: self._resolve(action))
            self.action_row.insertWidget(len(self.action_buttons), button)
            self.action_buttons.append(button)
        self.set_browsing(self._browsing)
        if changed:
            self.current_changed.emit(item)

    def _reopen(self) -> None:
        item = self.current_item()
        if item is not None and item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
            self._open_bunch()
        else:
            self._resolve("reopen")

    def _open_bunch(self) -> None:
        item = self.current_item()
        if item is None or self.workspace_dir is None or self._browsing or item.evidence.get("superseded"):
            return
        from .bunch_dialog import BunchReviewDialog
        try:
            dialog = BunchReviewDialog(self.workspace_dir, item, self)
        except (ValueError, RuntimeError, OSError) as error:
            self.hint.setText(f"无法打开集团核对：{error}")
            return
        paused = self._live_paused
        self._live_paused = True
        self.stop_analysis()
        try:
            dialog.exec_()
        finally:
            self._live_paused = paused
            self._last_live_signature = None
            self._next_analysis = time.monotonic() + 10
        self.refresh()
        self.hint.setText("集团核对结果已保存。" if dialog.saved_completed else "集团进度已保留，可再次打开继续核对。")

    def _jump(self) -> None:
        item = self.current_item()
        if item is not None and item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
            self._open_bunch()
            return
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
        if item.evidence.get("superseded"):
            self.hint.setText("此问题已被后台更新，请选择最新问题后处理。")
            return
        if self._browsing and action != "reopen":
            self.hint.setText("请先回到当前问题的证据画面，再提交决定。")
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
        self._held.pop(item.item_id, None)
        self._operator_items[item.item_id] = replace(item, evidence=dict(item.evidence, operator_scope=self._scope)).to_payload()
        self.save_drafts()
        self.hint.setText(f"已记录：{ACTION_LABELS.get(action, '重新处理')}。")
        self._render()

    def export(self) -> Optional[dict]:
        if self.store is None or self.problem_dir is None:
            return None
        try:
            counts = export_lists(self.items, self.store, self.problem_dir / "exports")
        except (OSError, RuntimeError, ValueError) as error:
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
        extra = ["--live-problems"] if self._live else []
        if self._finalizing_live:
            extra = ["--finish-live-problems"]
        if getattr(sys, "frozen", False):
            return sys.executable, extra + ["--analyze-problems", str(self.workspace_dir)]
        return sys.executable, ["-m", "realtime.review_main"] + extra + ["--analyze-problems", str(self.workspace_dir)]

    def stop_analysis(self):
        process, self._process = self._process, None
        if process is not None:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(3000)
            process.deleteLater()

    def live_tick(self, recording):
        if self._live and not recording:
            self._final_analysis_pending = True
        self._live = bool(recording)
        if (not recording and not self._final_analysis_pending) or self._live_paused or self._process is not None or time.monotonic() < self._next_analysis:
            return
        if not any((self.workspace_dir / name).exists() for name in ("finish_lines.json", "long_image_lines.json", "finish_problems/analysis_config.json")):
            self.analysis_status = "等待终点线配置（设置中配置终点线）"
            return
        signature = []
        for name in ("video_timeline.jsonl", "cyclerace_passage_events.jsonl", "finish_lines.json", "long_image_lines.json",
                     "finish_problems/analysis_config.json", "review_event_scope.json", "cyclerace_race_metadata.json",
                     "cyclerace_passage_events_deleted.json", "video_clock_calibrations.jsonl"):
            try:
                stat = (self.workspace_dir / name).stat()
                signature.append((name, stat.st_size, stat.st_mtime_ns))
            except OSError:
                pass
        if not signature or (signature == self._last_live_signature and not self._final_analysis_pending):
            return
        self._last_live_signature = signature
        self._finalizing_live = self._final_analysis_pending
        self._final_analysis_pending = False
        self._start_analysis()

    def add_manual_problem(self, frame, reason):
        if not reason.strip() or self.problem_dir is None:
            return None
        item = ProblemItem("manual:" + uuid.uuid4().hex, KIND_MANUAL_REVIEW, CATEGORY_VIDEO,
                           reason.strip() + "；芯片状态待核实", evidence={"frame": dict(frame), "operator_scope": self._scope})
        path = self.problem_dir / "manual_problems.json"
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        items.append(item.to_payload())
        self.problem_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        self.refresh()
        for row, value in enumerate(self.rows):
            if value.item_id == item.item_id:
                self.table.setCurrentCell(row, 0)
                break
        return item

    def _toggle_analysis(self) -> None:
        if self._process is not None:
            self._live_paused = True
            self._process.kill()
            return
        if self._live and not self._live_paused:
            self._live_paused = True
            self.analyze_button.setText("恢复实时分析")
            return
        self._live_paused = False
        self._start_analysis()

    def _start_analysis(self) -> None:
        if self.workspace_dir is None:
            return
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        program, arguments = self.analysis_command()
        process.setProgram(program)
        process.setArguments(arguments)
        process.readyReadStandardOutput.connect(self._read_progress)
        process.errorOccurred.connect(self._analysis_error)
        process.finished.connect(self._analysis_done)
        self._process = process
        self._progress_buffer = b""
        self._skip_message = ""
        if not self._live:
            self._final_analysis_pending = False
        self.analyze_button.setText("停止分析")
        self.analysis_status = "后台分析中（低优先级）"
        process.start()

    def _analysis_error(self, error):
        if error == QProcess.FailedToStart:
            self._analysis_done(-1)
            self.analysis_status = "分析进程无法启动；录像不受影响，请检查运行环境"

    def _read_progress(self) -> None:
        if self._process is None:
            return
        self._consume_progress(bytes(self._process.readAllStandardOutput()))

    def _consume_progress(self, data: bytes, *, final: bool = False) -> None:
        self._progress_buffer += data
        lines = self._progress_buffer.split(b"\n")
        self._progress_buffer = lines.pop()
        if final and self._progress_buffer:
            lines.append(self._progress_buffer)
            self._progress_buffer = b""
        for raw in lines:
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if message.get("stage") == "done":
                continue
            if message.get("stage") == "skipped":
                self._skip_message = str(message.get("message", ""))
                continue
            total = message.get("total") or 0
            self.analysis_status = f"后台分析：{message.get('stage')} {message.get('done')}/{total}"

    def _analysis_done(self, *_args) -> None:
        self._read_progress()
        self._consume_progress(b"", final=True)
        process, self._process = self._process, None
        error = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")[-400:] if process else ""
        if process is not None:
            process.deleteLater()
        self._next_analysis = time.monotonic() + 10
        if _args and _args[0] != 0:
            self._last_live_signature = None
            self._final_analysis_pending = self._finalizing_live
        self._finalizing_live = False
        self.analyze_button.setText(("恢复实时分析" if self._live_paused else "暂停实时分析") if self._live else "分析录像")
        self._signature = None
        self.refresh()
        skipped, self._skip_message = getattr(self, "_skip_message", ""), ""
        if skipped:
            self.analysis_status = skipped
        elif self._live_paused:
            self.analysis_status = "分析已暂停；录像继续"
        elif _args and _args[0] != 0:
            self.analysis_status = "分析失败，已保留上一版清单：" + (error.strip() or "请查看日志")
        else:
            self.analysis_status = "本轮分析完成，等待新短段" if self.payload else "录像分析未完成，请查看日志"
        self.analysis_finished.emit()


__all__ = ["ProblemListPanel"]
