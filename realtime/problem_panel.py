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

from PyQt5.QtCore import QEvent, QProcess, QProcessEnvironment, QTimer, Qt, pyqtSignal
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
from .live_chip_checks import LIVE_CHIP_FILENAME, input_signature, merge_chip_snapshot
from .analysis_settings import read_settings, settings_token
from .batch_review import (BATCH_IDENTITY_FILENAME, BatchDecisionStore, aggregate_batches, identity_chain,
                           previous_items, save_identity)
from .problem_analysis import OUTPUT_DIRNAME, PROBLEM_LIST_FILENAME
from .problem_items import (
    ACTION_CHANGE,
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_LABELS,
    ACTION_NOT_RIDER,
    ACTION_RECORD_BIB,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
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
    TYPE_BUNCH,
    TYPE_ENTRY_LABELS,
    TYPE_EVIDENCE,
    TYPE_MISSED,
    TYPE_MULTI_CHIP,
    TYPE_ORDER,
    TYPE_OTHER,
    TYPE_TITLES,
    ProblemItem,
)
from .problem_resolutions import (
    RESOLUTIONS_FILENAME,
    ProblemResolutionStore,
    export_lists,
    same_frame_conflicts,
)


def _progress_group(stage: str) -> str:
    """Which of sampling / clock / analysis a worker message belongs to."""

    if any(key in stage for key in ("线取样", "提取线", "取样")):
        return "取样"
    if "校时" in stage:
        return "校时"
    return "分析"


def _missed_entry(item: ProblemItem, batch, bunch_parent) -> bool:
    """A missed-read candidate is entered through its evidence, not through a decision.

    Members handled inside a batch or bunch tool keep that tool as their entry.
    """

    return (item.task_type == TYPE_MISSED and bool(item.evidence.get("frame"))
            and item.status != STATUS_WAITING and batch is None and bunch_parent is None)


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
        self.batch_store = None
        self._signature = None
        self._render_signature = None
        self._events_provider: Callable[[], object] = lambda: ()
        self._process: Optional[QProcess] = None
        self._chip_process: Optional[QProcess] = None
        self._chip_generation = 0
        self._chip_signature = None
        self._chip_retry_after = 0.0
        self.chip_status = "芯片自检等待记录"
        self._chip_timer = QTimer(self)
        self._chip_timer.setSingleShot(True)
        self._chip_timer.setInterval(400)
        self._chip_timer.timeout.connect(self._start_chip_checks)
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
        self._type_filter = ""
        # Receiver, recorder, sampling, clock and analysis fail separately: a
        # single "video problem" line would hide which of them stopped.
        self.stage_health: dict[str, str] = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
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
        # Counting row (plan step 6): what was matched without the operator, how much
        # work of each class is open, and what only waits for evidence. Clicking a
        # class narrows the queue to it; the class entry button then opens its tool.
        type_row = QHBoxLayout()
        type_row.setSpacing(4)
        self.auto_label = QLabel("无需人工处理 —— 人", self)
        self.auto_label.setWordWrap(True)
        self.auto_label.setToolTip(
            "按有效通过事件去重后的自动对应人数。其中仍落在待核集团或遮挡时段内的会另行标出，"
            "这些选手的对应不作废，但整个集团是否核清不由自动对应决定。"
        )
        type_row.addWidget(self.auto_label, 1)
        layout.addLayout(type_row)
        # Buttons on their own line: six of them must not be squeezed off a laptop screen.
        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.type_buttons: dict[str, QPushButton] = {}
        for value in TYPE_ORDER:
            button = QPushButton(TYPE_TITLES[value], self)
            button.setCheckable(True)
            button.setToolTip(f"只看{TYPE_TITLES[value]}；再点一次取消，回到全部类型")
            button.clicked.connect(lambda _checked=False, value=value: self._filter_by_type(value))
            button.hide()
            self.type_buttons[value] = button
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        settings_row = QHBoxLayout()
        self.sampling_button = QPushButton("取样线 / 认车", self)
        self.sampling_button.clicked.connect(self._configure_sampling)
        settings_row.addWidget(self.sampling_button)
        self.analysis_details = QLabel("实际终点线按机位和录制会话配置；只分析已封存录像。", self)
        self.analysis_details.setWordWrap(True)
        settings_row.addWidget(self.analysis_details, 1)
        layout.addLayout(settings_row)
        self.queue_filter = QComboBox(self)
        self.queue_filter.addItems(["待处理", "待证据", "暂存", "交接 / 已处理", "全部"])
        self.queue_filter.currentIndexChanged.connect(self._queue_filter_changed)
        controls.addWidget(self.queue_filter)
        self.show_closed = QCheckBox("显示已处理和已排除", self)
        self.show_closed.toggled.connect(self._render)
        controls.addWidget(self.show_closed)
        self.expand_groups = QCheckBox("展开集团和拥挤时段", self)
        self.expand_groups.setToolTip("集团和拥挤时段里的问题归在一条里核对；展开后逐条显示")
        self.expand_groups.toggled.connect(self._render)
        controls.addWidget(self.expand_groups)
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
        # The table treats Enter as its own activation and never passes it on, so
        # one key has to work the same wherever the focus sits inside the panel.
        self.table.installEventFilter(self)
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
        # The class entry sits above the decisions, so the tool for this problem is
        # visible where the evidence is; there is no hidden entry to remember.
        self.batch_button = QPushButton(TYPE_ENTRY_LABELS[TYPE_MULTI_CHIP], detail)
        self.batch_button.setStyleSheet("font-weight:600;")
        self.batch_button.setToolTip("打开这条批次，按成员逐条处理（回车）")
        self.batch_button.clicked.connect(self._open_batch)
        self.batch_button.hide()
        self.action_row.addWidget(self.batch_button)
        self.bunch_button = QPushButton(TYPE_ENTRY_LABELS[TYPE_BUNCH], detail)
        self.bunch_button.setStyleSheet("font-weight:600;")
        self.bunch_button.setToolTip("打开这条集团和遮挡时段，逐帧核对整团人数（回车）")
        self.bunch_button.clicked.connect(self._open_bunch)
        self.bunch_button.hide()
        self.action_row.addWidget(self.bunch_button)
        self.missed_button = QPushButton(TYPE_ENTRY_LABELS[TYPE_MISSED], detail)
        self.missed_button.setToolTip("跳到候选过线的原帧，逐帧看这一处变化；认不出号码就保留待证据，不凭猜测排除（回车）")
        self.missed_button.setStyleSheet("font-weight:600;")
        self.missed_button.clicked.connect(self._open_missed)
        self.missed_button.hide()
        self.action_row.addWidget(self.missed_button)
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

    def closeEvent(self, event):
        self.stop_chip_checks()
        self.stop_analysis()
        self.save_drafts()
        super().closeEvent(event)

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
        self.batch_button.setEnabled(not self._browsing)
        self.missed_button.setEnabled(not self._browsing)
        if browsing:
            self.hint.setText("正在浏览其他时间；当前问题与输入已保留。请回到当前问题再提交决定。")

    # ----- data -------------------------------------------------------
    def set_workspace(self, workspace_dir) -> None:
        workspace_dir = Path(workspace_dir)
        if workspace_dir == self.workspace_dir:
            return
        self.stop_analysis()
        self.stop_chip_checks()
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
        self.stage_health = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
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
        self.stop_chip_checks()
        self.save_drafts()
        self._scope = scope
        self.payload = None
        self._signature = None
        self._render_signature = None
        self._last_live_signature = None
        self.items, self.rows = [], []
        self._shown_id = None
        self.stage_health = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
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
        paths = [self.problem_dir / name for name in (PROBLEM_LIST_FILENAME, LIVE_CHIP_FILENAME)]
        signature = []
        for path in paths:
            try:
                stat = path.stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                signature.append(None)
            except OSError as error:
                self.hint.setText(f"问题清单读取失败：{error}")
                return
        try:
            token = settings_token(self.workspace_dir)
            enforce_settings = bool(read_settings(self.workspace_dir).get("settings_revision"))
        except (OSError, ValueError) as error:
            token, enforce_settings = "invalid", True
            self.analysis_details.setText(f"分析配置不可用：{error}")
        signature.append(token)
        if signature != self._signature:
            try:
                snapshots = []
                for path, stamp in zip(paths, signature):
                    snapshot = json.loads(path.read_text(encoding="utf-8")) if stamp else None
                    if snapshot and self._scope[0] and (snapshot.get("race_id"), snapshot.get("stage_id", "")) != self._scope:
                        snapshot = None
                    snapshots.append(snapshot)
                payload = merge_chip_snapshot(*snapshots)
                if payload and (enforce_settings or payload.get("settings_token")) and payload.get("settings_token", "") != token:
                    payload = dict(payload, settings_stale=True, auto_passed=[],
                                   summary=dict(payload.get("summary", {}), auto_passed=0))
                items = [ProblemItem.from_payload(item) for item in (payload or {}).get("items", [])]
                if payload and any(i.evidence.get("batch_members") for i in items):
                    scope = (payload.get("race_id", ""), payload.get("stage_id", ""))
                    identity_path = self.problem_dir / BATCH_IDENTITY_FILENAME
                    # The ledger is the identity the operator already holds; a
                    # batch the list names but the ledger has never seen keeps
                    # the list's own id instead of a fresh hash.
                    history = previous_items(identity_path, scope)
                    chain = identity_chain(history, [item for item in items
                                                     if item.evidence.get("batch_members")])
                    items = aggregate_batches(items, payload.get("passage_members", {}), scope, chain)
                    save_identity(identity_path, scope,
                                  [item for item in items if item.evidence.get("batch_members")], chain)
                    payload = dict(payload, items=[i.to_payload() for i in items])
                if snapshots[1] and self._chip_process is None and time.monotonic() >= self._chip_retry_after:
                    self._chip_status_from_payload(snapshots[1])
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.hint.setText(f"问题清单读取失败：{error}")
                return
            self.payload, self.items = payload, items
            self._signature = signature
        if self.store is None:
            self.store = ProblemResolutionStore(self.problem_dir / RESOLUTIONS_FILENAME)
        try:
            self.batch_store = BatchDecisionStore(self.problem_dir)
        except (OSError, ValueError) as error:
            self.batch_store = None
            self.hint.setText(f"批次日志不可用：{error}")
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
        fresh_ids = {value["item_id"] for value in (self.payload or {}).get("items", [])}
        self.items = [replace(item, evidence=dict(item.evidence, batch_members=[dict(m, present=False)
                      for m in item.evidence["batch_members"]]))
                      if item.evidence.get("batch_members") and item.item_id not in fresh_ids else item for item in self.items]
        self.items = [replace(item, status=STATUS_WAITING, status_note="配置已变更，保留旧建议供追溯；须重新取得适用证据。",
                              evidence=dict(item.evidence, configuration_stale=True))
                      if token and item.item_id in self._operator_items and item.item_id not in fresh_ids
                      and item.category == CATEGORY_VIDEO and item.kind != KIND_MANUAL_REVIEW
                      and item.evidence.get("operator_settings_token", "") != token else item for item in self.items]
        if (self.payload or {}).get("settings_stale"):
            self.items = [replace(item, status=STATUS_WAITING, status_note="取样线或认车配置已变更，等待重新分析。",
                                  evidence=dict(item.evidence, configuration_stale=True))
                          if item.category == CATEGORY_VIDEO and item.kind != KIND_MANUAL_REVIEW else item for item in self.items]
            self.stage_health["取样"] = "配置已变更，等待重新取样"
            self.stage_health["校时"] = "配置已变更，等待重新校时"
            self.analysis_details.setText("配置已变更：旧视频结论不可提交，等待重新取样、校时与认车；芯片自检继续。")
        elif token != "invalid":
            cameras = (self.payload or {}).get("cameras", {})
            missing_lines = sum(len(value.get("missing_line_segments") or ()) for value in cameras.values())
            self.stage_health["取样"] = (f"{missing_lines} 个片段缺线或跨改线边界" if missing_lines
                                         else "已按机位配置" if cameras else "等待已封存录像")
            self.stage_health["校时"] = self.clock_health() or "等待自动校时"
            statuses = []
            for camera, value in cameras.items():
                detail = value.get('error') or value.get('bike_status', {}).get('message', '')
                for key, label in (("sealed_through_ms", "本轮封存至"), ("analyzed_through_ms", "分析至")):
                    if value.get(key) is not None:
                        detail += f"；{label} {format_chip_time(value[key], date=True)}"
                if value.get("missing_line_segments"):
                    detail += "；部分片段缺线或跨改线边界，尚未核清"
                statuses.append(f"机位 {camera}：{detail}")
            self.analysis_details.setText("；".join(statuses) or "视频等待已封存录像及实际终点线；认车状态待分析。")
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
        # Absence from a partial receive snapshot does not prove deletion.
        return all(event is not None and not event.is_active for event in involved)

    def state(self, item: ProblemItem) -> str:
        if item.status == STATUS_WAITING:
            return STATE_WAITING
        if item.status == STATUS_EXCLUDED:
            return STATE_EXCLUDED
        if item.status == STATUS_INFO:
            return STATE_INFO
        if item.evidence.get("batch_members"):
            if self.batch_store is None:
                return STATE_WAITING
            states = [self.batch_store.state(item, m, tuple(self._events_provider())) for m in item.evidence["batch_members"]]
            pending = sum(s in ("待处理", "需要重查", "暂存", "待证据") for s in states)
            handoff = states.count("待 CycleRace")
            done = len(states) - pending - handoff
            if pending:
                return f"部分处理：待核 {pending} / 交接 {handoff} / 已处理 {done}" if handoff or done else STATE_PENDING
            return f"{STATE_HANDOFF}：{handoff} 个成员" if handoff else f"{STATE_RESOLVED}：{done} 个成员"
        if item.status == STATUS_CLOSED_UPSTREAM or (item.pending and self._upstream_closed(item)):
            return STATE_UPSTREAM
        record = self.bunch_store.latest_for(item) if self.bunch_store is not None else None
        if record is not None:
            if record.get("item_snapshot") != item.to_payload():
                return STATE_STALE
            from .bunch_review import load_context, pending_member_decisions
            try:
                context = load_context(self.workspace_dir, item, record["camera"])
                if self.bunch_store.get(context) is None:
                    return STATE_STALE
                if record.get("completed") and pending_member_decisions(context):
                    return STATE_PENDING
            except (ValueError, OSError):
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
        return state in (STATE_PENDING, STATE_DISPUTED, STATE_STALE) or state.startswith("部分处理")

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
                     self.queue_filter.currentIndex(), self._type_filter, tuple(self._held.items()),
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
            if visible and self._type_matches(item, state):
                self.rows.append(item)
            if self.expand_groups.isChecked():
                self.rows += [
                    member for member in members.get(item.item_id, [])
                    if (show_all or self.is_pending_state(self.state(member))) and self._type_matches(member)
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
        self._refresh_type_row()
        text = self.summary_text()
        self.summary.setText(text)
        self.summary_changed.emit(text)

    def _type_matches(self, item: ProblemItem, state: str = "") -> bool:
        if not self._type_filter:
            return True
        if self._type_filter == TYPE_EVIDENCE:
            return item.task_type == TYPE_EVIDENCE or (state or self.state(item)) == STATE_WAITING
        return item.task_type == self._type_filter

    def _refresh_type_row(self) -> None:
        counts = self.type_counts()
        self.auto_label.setText(self.auto_passed_text())
        for value, button in self.type_buttons.items():
            count = counts[value]
            button.setText(f"{TYPE_TITLES[value]} {count}")
            button.setVisible(value != TYPE_OTHER or count > 0)
            button.setChecked(value == self._type_filter)

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
            self.batch_button.hide()
            self.title.setText("选择一条问题查看过线卡片")
            self.detail.setText("")
            self.jump_button.setEnabled(False)
            self.reopen_button.setEnabled(False)
            self.bunch_button.hide()
            self.missed_button.hide()
            self.hold_button.setEnabled(False)
            if changed:
                self.current_changed.emit(None)
            return
        self.hold_button.setEnabled(item.status != STATUS_WAITING)
        self.hold_button.setText("返回待处理" if item.item_id in self._held else "暂存")
        is_bunch = item.kind in (KIND_BUNCH, KIND_OBSTRUCTION)
        batch = self._batch_parent(item)
        self.batch_button.setVisible(batch is not None)
        self.bunch_button.setVisible(is_bunch)
        self.bunch_button.setEnabled(item.status != STATUS_WAITING)
        self.missed_button.setVisible(_missed_entry(item, batch, self._bunch_parent(item)))
        self.missed_button.setEnabled(item.status != STATUS_WAITING)
        self.bib_input.setVisible(not is_bunch and batch is None)
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
        actions = (ACTION_SYNCED,) if state.startswith(STATE_HANDOFF) else (() if is_bunch or resolved or item.status == STATUS_WAITING else item.actions)
        if batch is not None:
            actions = ()
            self.reopen_button.hide()
        if item.evidence.get("superseded"):
            actions = ()
            self.hold_button.setEnabled(False)
            self.bunch_button.setEnabled(False)
        for action in actions:
            button = QPushButton(ACTION_LABELS.get(action, action), self)
            button.setProperty("problemAction", action)
            button.setToolTip(f"{ACTION_LABELS.get(action, action)}（数字键 {len(self.action_buttons) + 1}）")
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

    def _batch_parent(self, item):
        if item.evidence.get("batch_members"):
            return item
        return next((i for i in self.items if i.item_id == item.grouped and i.evidence.get("batch_members")), None)

    def _bunch_parent(self, item):
        """The group item that owns this member, so the group tool stays its entry."""

        if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
            return item
        return next((i for i in self.items if i.item_id == item.grouped and i.kind in (KIND_BUNCH, KIND_OBSTRUCTION)), None)

    def _open_missed(self) -> None:
        item = self.current_item()
        if item is None or not item.evidence.get("frame"):
            return
        self.jump_requested.emit(dict(item.evidence["frame"]))
        self.hint.setText("已跳到候选过线的原帧：逐帧看这一处变化，认得出号码就补录；看不清就保留待证据，不凭猜测排除。")

    # ----- working the queue by keyboard ---------------------------------
    def keyPressEvent(self, event) -> None:
        """A live race is worked in time order; reaching for the mouse must not slow it.

        Qt delivers a key here only when the focused child has no use for it, so a
        bib or reason field keeps its own characters and the table keeps its arrows;
        everything else — a button, the panel itself — reaches this handler.
        """

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._activate_current()
            return
        if Qt.Key_1 <= event.key() <= Qt.Key_9:
            self._click_action(event.key() - Qt.Key_1 + 1)
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        # The table answers Enter with its own activation and never lets it out.
        if (watched is self.table and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            self._activate_current()
            return True
        return super().eventFilter(watched, event)

    def _activate_current(self) -> None:
        """Enter opens the tool this row is worked in, and its frame when it has none."""

        if self.current_item() is None:
            return
        if self.batch_button.isVisibleTo(self):
            self._open_batch()
        elif self.bunch_button.isVisibleTo(self):
            self._open_bunch()
        elif self.missed_button.isVisibleTo(self):
            self._open_missed()
        elif self.jump_button.isEnabled():
            self._jump()
        else:
            self.hint.setText("这条问题没有工具也没有原帧：用数字键或右侧按钮直接处理。")
        self.table.setFocus()

    def _click_action(self, number: int) -> None:
        """Digits press the decision buttons of the row on screen, in the order shown."""

        buttons = [button for button in self.action_buttons
                   if button.isVisibleTo(self) and button.isEnabled()]
        if 1 <= number <= len(buttons):
            buttons[number - 1].click()

    def _filter_by_type(self, value: str) -> None:
        """The counting row works as the queue filter; there is no separate entry."""

        self._type_filter = "" if self._type_filter == value else value
        for key, button in self.type_buttons.items():
            button.setChecked(key == self._type_filter)
        self.queue_filter.blockSignals(True)
        self.queue_filter.setCurrentIndex(1 if self._type_filter == TYPE_EVIDENCE else 0)
        self.queue_filter.blockSignals(False)
        self._render()

    def _queue_filter_changed(self) -> None:
        if self._type_filter:
            self._type_filter = ""
            for button in self.type_buttons.values():
                button.setChecked(False)
        self._render()

    def type_counts(self) -> dict[str, int]:
        """Open work per class: each item is counted once, in the row the operator works in."""

        counts = dict.fromkeys(TYPE_ORDER, 0)
        for item in self.items:
            if item.grouped:
                continue  # members are handled inside their batch or group
            state = self.state(item)
            if state == STATE_WAITING or (item.task_type == TYPE_EVIDENCE
                                          and not (state.startswith(STATE_RESOLVED) or state.startswith(STATE_HANDOFF)
                                                   or state in (STATE_UPSTREAM, STATE_EXCLUDED))):
                counts[TYPE_EVIDENCE] += 1
            elif self.is_pending_state(state):
                counts[item.task_type] += 1
        return counts

    def auto_passed_text(self) -> str:
        payload = self.payload or {}
        summary = payload.get("summary", {})
        passed = payload.get("auto_passed") or ()
        count = int(summary.get("auto_passed", len(passed)))
        in_period = (sum(1 for entry in passed if entry.get("pending_period")) if passed
                     else int(summary.get("auto_passed_in_pending_period", 0)))
        text = f"无需人工处理 {count} 人"
        if in_period:
            text += f"（其中 {in_period} 人落在待核集团/遮挡时段内，不代表该时段已核清）"
        return text

    def evidence_gap_text(self) -> str:
        summary = (self.payload or {}).get("summary", {})
        waiting = self.type_counts()[TYPE_EVIDENCE]
        unverifiable = int(summary.get("unverifiable", 0))
        if not waiting and not unverifiable:
            return "证据缺口 无"
        return f"证据缺口 {waiting} 条待证据" + (f" · {unverifiable} 人无录像可核" if unverifiable else "")

    def health_text(self) -> str:
        parts = [f"{name} {self.stage_health.get(name) or '—'}" for name in ("接收", "录制", "取样", "校时", "分析")]
        return " · ".join(parts)

    def clock_health(self) -> str:
        """Clock state per camera, kept apart from sampling and analysis failures."""

        texts = []
        for camera, value in sorted((self.payload or {}).get("cameras", {}).items(), key=lambda pair: str(pair[0])):
            calibration = value.get("calibration") or {}
            if not calibration:
                texts.append(f"机位 {camera} 等待校时")
                continue
            note = f"机位 {camera} v{calibration.get('version')}"
            off = [anchor for anchor in calibration.get("anchors", ()) if not anchor.get("within_tolerance", True)]
            texts.append(note + (f"，{len(off)} 个人工锚点超差待核对" if off else "，锚点一致"))
        return "；".join(texts)

    def _open_batch(self):
        item = self.current_item()
        batch = self._batch_parent(item) if item else None
        if batch is None or self._browsing:
            return
        from .batch_dialog import BatchReviewDialog

        def current(identity):
            self.refresh()
            return next((i for i in self.items if i.item_id == identity), None)

        try:
            dialog = BatchReviewDialog(self.problem_dir, batch, current, self._events_provider,
                                       self.jump_requested.emit, self)
            dialog.exec_()
            if dialog.saved:
                item = dialog.batch
                self._operator_items[item.item_id] = replace(item, evidence=dict(item.evidence,
                    operator_scope=self._scope, operator_settings_token=settings_token(self.workspace_dir))).to_payload()
                self.save_drafts()
            self.refresh()
        except (OSError, ValueError, RuntimeError) as error:
            self.hint.setText(f"批次处理不可用：{error}")

    def _open_bunch(self) -> None:
        self.refresh()
        item = self.current_item()
        if item is None or self.workspace_dir is None or self._browsing or item.evidence.get("superseded") or item.status == STATUS_WAITING:
            return
        from .bunch_dialog import BunchReviewDialog
        try:
            dialog = BunchReviewDialog(self.workspace_dir, item, self)
        except (ValueError, RuntimeError, OSError) as error:
            self.hint.setText(f"无法打开集团核对：{error}")
            return
        paused = self._live_paused
        running = self._process is not None
        self._live_paused = True
        self.stop_analysis()
        try:
            dialog.exec_()
        finally:
            self._live_paused = paused
            self._last_live_signature = None
            self._next_analysis = time.monotonic() + 10
            if running and not paused:
                self._start_analysis()
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
        self.refresh()
        item = self.current_item()
        if item is None or self.store is None:
            return
        if self._batch_parent(item) is not None:
            self.hint.setText("请打开批次并选择具体成员，不能用整项操作代替成员决定。")
            return
        if item.evidence.get("configuration_stale") or item.status == STATUS_WAITING:
            self.hint.setText("证据等待重新分析，不能提交旧结论。")
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
        if action in (ACTION_NOT_RIDER, ACTION_RIDE_BACK, ACTION_SAME_CROSSING) and not reason:
            self.hint.setText("排除或确认同一次过线要写明依据；“看不清号码”不能作为排除依据。")
            return
        if action == ACTION_DELETE_EXTRA_CHIP and len(item.bibs) > 1 and bib not in item.bibs:
            self.hint.setText(f"请在号码框填写要删除的芯片号码：{'、'.join(item.bibs)}。")
            return
        if action == ACTION_CHANGE and not bib:
            self.hint.setText("建议改号改圈需要在号码框填写改成的号码或圈数。")
            return
        try:
            operator_token = settings_token(self.workspace_dir)
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
        self._operator_items[item.item_id] = replace(item, evidence=dict(item.evidence, operator_scope=self._scope,
                                                                       operator_settings_token=operator_token)).to_payload()
        self.save_drafts()
        self.hint.setText(f"已记录：{ACTION_LABELS.get(action, '重新处理')}。")
        self._render()
        # The list drops the decided row and lands back on the oldest one; keep the
        # queue under the fingers so the next row is one Down away.
        self.table.setFocus()

    def export(self) -> Optional[dict]:
        if self.store is None or self.problem_dir is None:
            return None
        try:
            self.refresh()
            counts = export_lists(self.items, self.store, self.problem_dir / "exports", events=tuple(self._events_provider()))
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
    def _configure_sampling(self):
        if self.workspace_dir is None:
            return
        from .sampling_dialog import SamplingSettingsDialog
        from PyQt5.QtWidgets import QDialog

        paused = self._live_paused
        running = self._process is not None
        saved = False
        self._live_paused = True
        self.stop_analysis()
        try:
            dialog = SamplingSettingsDialog(self.workspace_dir, self, race_id=self._scope[0], stage_id=self._scope[1])
            if dialog.exec_() == QDialog.Accepted:
                saved = True
                self._last_live_signature = None
                self._signature = None
                self._next_analysis = 0
                self.refresh()
        except (OSError, ValueError, KeyError) as error:
            self.hint.setText(f"配置窗口无法打开：{error}")
        finally:
            self._live_paused = paused
            if not paused and (saved or running):
                self._start_analysis()

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

    def stop_chip_checks(self):
        self._chip_timer.stop()
        self._chip_generation += 1
        self._chip_signature = None
        self._chip_retry_after = 0.0
        self.chip_status = "芯片自检等待记录"
        self.stage_health["接收"] = self.chip_status
        process, self._chip_process = self._chip_process, None
        if process is not None:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(3000)
            process.deleteLater()

    def _chip_status_from_payload(self, payload):
        pending = sum(ProblemItem.from_payload(item).pending for item in payload["items"])
        counts = payload["counts"]
        self.chip_status = (
            f"芯片自检{'发现 ' + str(pending) + ' 项疑点' if pending else '正常'}"
            f"（已落盘消息 {counts['journal_messages']} / 有效通过 {counts['effective_passages']}）"
        )
        self.stage_health["接收"] = self.chip_status
        missing = payload.get("missing_inputs", [])
        self.summary.setToolTip("原始读卡消息数未知；已落盘消息含修订，不等于人数。\n"
                                + ("未执行的检查，缺少：" + "；".join(missing) if missing else ""))

    def _chip_tick(self):
        if self.workspace_dir is None or self._chip_process is not None or time.monotonic() < self._chip_retry_after:
            return
        try:
            signature = input_signature(self.workspace_dir)
        except OSError as error:
            self.chip_status = f"芯片自检读取失败：{error}"
            self.stage_health["接收"] = self.chip_status
            return
        if not signature:
            return
        if self._chip_signature is None:
            cached = (self.payload or {}).get("chip_checks")
            if cached:
                self._chip_signature = cached.get("input_signature")
        if signature != self._chip_signature and not self._chip_timer.isActive():
            self.chip_status = "芯片自检等待更新（上一结果保留）"
            self.stage_health["接收"] = self.chip_status
            self._chip_timer.start()

    def _start_chip_checks(self):
        if self.workspace_dir is None or self._chip_process is not None:
            return
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        args = [] if getattr(sys, "frozen", False) else ["-m", "realtime.review_main"]
        args += ["--check-live-chips", str(self.workspace_dir), "--chip-race-id", self._scope[0],
                 "--chip-stage-id", self._scope[1]]
        process.setProgram(sys.executable)
        process.setArguments(args)
        generation = self._chip_generation
        process.finished.connect(lambda code, _status: self._chip_done(process, generation, code))
        process.errorOccurred.connect(lambda error: self._chip_done(process, generation, -1)
                                     if error == QProcess.FailedToStart else None)
        self._chip_process = process
        self.chip_status = "芯片自检中（视频分析独立运行）"
        self.stage_health["接收"] = self.chip_status
        process.start()

    def _chip_done(self, process, generation, code):
        if generation != self._chip_generation or process is not self._chip_process:
            return
        self._chip_process = None
        try:
            if code != 0:
                error = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")[-300:]
                raise ValueError(error.strip() or "芯片自检进程启动或运行失败")
            payload = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8"))
            if self._scope[0] and (payload["race_id"], payload["stage_id"]) != self._scope:
                raise ValueError("赛事已变化，未发布旧结果")
            # Validate before replacing the previous successful snapshot. The
            # GUI is the sole publisher; video workers use a different file.
            merge_chip_snapshot(None, payload)
            self._chip_status_from_payload(payload)
            self.problem_dir.mkdir(parents=True, exist_ok=True)
            path = self.problem_dir / LIVE_CHIP_FILENAME
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            self._chip_signature = payload["input_signature"]
            self.refresh()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.chip_status = f"芯片自检失败，保留上一结果：{error}"
            self.stage_health["接收"] = self.chip_status
            self._chip_retry_after = time.monotonic() + 5
        finally:
            process.deleteLater()

    def live_tick(self, recording):
        self._chip_tick()
        if self._live and not recording:
            self._final_analysis_pending = True
        self._live = bool(recording)
        self.stage_health["录制"] = "● 录像中" if recording else "录像未启动"
        if self.workspace_dir is None:
            return
        if (not recording and self._process is None and not self._final_analysis_pending
                and self.analysis_status == "等待已封存录像"):
            self.analysis_status = "视频分析等待录像；芯片自检独立运行"
        self.stage_health["分析"] = self.analysis_status
        if (not recording and not self._final_analysis_pending) or self._live_paused or self._process is not None or time.monotonic() < self._next_analysis:
            return
        if not any((self.workspace_dir / name).exists() for name in ("finish_lines.json", "long_image_lines.json", "finish_problems/analysis_config.json")):
            self.analysis_status = "等待终点线配置（设置中配置终点线）"
            self.stage_health["分析"] = self.analysis_status
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
        try:
            signature.append(("settings", settings_token(self.workspace_dir)))
        except (OSError, ValueError) as error:
            self.analysis_status = f"分析配置错误：{error}"
            self.stage_health["分析"] = self.analysis_status
            return
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
        self.stage_health["分析"] = self.analysis_status
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
            self.stage_health[_progress_group(str(message.get("stage", "")))] = (
                f"{message.get('stage')} {message.get('done')}/{total}")

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
        self.stage_health["分析"] = self.analysis_status
        self.analysis_finished.emit()


__all__ = ["ProblemListPanel"]
