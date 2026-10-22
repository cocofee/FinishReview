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

from .bunch_timeline import BunchTimeline
from .chip_history import format_chip_time
from .live_chip_checks import LIVE_CHIP_FILENAME, input_signature, merge_chip_snapshot
from .analysis_settings import read_settings, settings_token
from .batch_review import (BATCH_IDENTITY_FILENAME, BatchDecisionStore, aggregate_batches, identity_chain,
                           previous_items, save_identity)
from .problem_analysis import OUTPUT_DIRNAME, PROBLEM_LIST_FILENAME
from .problem_items import (
    ACTION_CHANGE,
    ACTION_CONFIRM_CROSSING,
    ACTION_DELETE_CHIP,
    ACTION_DELETE_EXTRA_CHIP,
    ACTION_LABELS,
    ACTION_NOT_RIDER,
    ACTION_REASON_PRESETS,
    ACTION_RECORD_BIB,
    ACTION_RIDER,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
    ACTION_SIDE_BY_SIDE,
    ACTION_SYNCED,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    STATUS_INFO,
    STATUS_WAITING,
    KIND_AUTO_PASSED,
    KIND_BUNCH,
    KIND_MANUAL_REVIEW,
    KIND_NO_VIDEO,
    KIND_OBSTRUCTION,
    KIND_SYNC,
    KIND_TWO_CHIPS_ONE_CROSSING,
    KIND_WAITING,
    CATEGORY_VIDEO,
    CLASS_ORDER,
    CLASS_TITLES,
    TYPE_BUNCH,
    TYPE_CONFIRM,
    TYPE_ENTRY_LABELS,
    TYPE_EVIDENCE,
    TYPE_MISSED,
    TYPE_MULTI_CHIP,
    TYPE_OTHER,
    ProblemItem,
    auto_passed_item,
    operator_actions,
    problem_class,
    row_label,
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


# 待确认 keeps the row in the queue instead of deciding it: the operator looked and
# cannot settle it yet.  The standing reason says why, and stays editable.
PENDING_REASON = "路人 / 遮挡 / 看不清，先放着"

# 判读集团时画面上面那条刻度的一句话图例：四种颜色各是什么，一眼就分得清。
TIMELINE_HELP = "整团过线图 · 点刻度跳转；大画面里按住左键拖动回看，N / Shift+N 跳到下一处 / 上一处检测到的过线；都不会自动计数"
# 画面落到的帧和检测时刻最多差半帧（20 帧/秒时 25 毫秒）；在这以内算同一处。
DETECTION_SLACK_MS = 30

# The one token a row of the queue carries when its class does not already name it.
SHORT_NOTES = {
    KIND_NO_VIDEO: "无录像",
    KIND_WAITING: "等录像",
    KIND_SYNC: "不一致",
    KIND_MANUAL_REVIEW: "人工",
}

STATE_PENDING = "待处理"
STATE_DISPUTED = "有争议"
STATE_RESOLVED = "已处理"
STATE_UPSTREAM = "CycleRace 已处理"
STATE_EXCLUDED = "已排除"
STATE_INFO = "参考"
STATE_STALE = "上游已修改，请重新处理"
STATE_WAITING = "待证据"
STATE_HANDOFF = "待 CycleRace"
# 正常单人的行：芯片已经答过，不是待处理，只是一条可复查的过线记录。
STATE_AUTO = "自动对应"


class ProblemListPanel(QFrame):
    jump_requested = pyqtSignal(object)
    summary_changed = pyqtSignal(str)
    analysis_finished = pyqtSignal()
    current_changed = pyqtSignal(object)
    # The workbench listens: the picture takes the room of everything else, and
    # gives it back when the queue returns.
    big_view_changed = pyqtSignal(bool)
    # The people counted on the picture: the workbench draws them on the frame.
    marks_changed = pyqtSignal(object)
    # While the enlarged picture is up it takes the clicks; the operator is counting.
    count_mode_changed = pyqtSignal(bool)
    # Stepping the picture frame by frame, from the panel's own keys.
    frame_step_requested = pyqtSignal(int)
    # 赛前把本场试出来的记录清掉；由主窗口确认并删除文件，面板不自己动数据。
    clear_data_requested = pyqtSignal()

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
        # The people pointed at on the picture, per problem.  They are the count and
        # the positions; the moments inside them align the count with the chips.
        self._marks: dict[str, list] = {}
        self._viewed: dict[str, dict] = {}
        # The arrival table's own working state (which picture counts which wave,
        # what of the recording was looked at); saved with the other drafts.
        self._arrival_drafts: dict[str, dict] = {}
        # Confirmed group -> the doubts it still holds; filled by state().
        self._open_doubts: dict[str, frozenset] = {}
        self._group_context = None
        self._group_context_id = None
        self._group_context_error = ""
        self._editing_mark = -1
        self._shown_since = time.monotonic()
        self._operator_items = {}
        self._shown_id = None
        self._shown_frame = None
        self._scope = ("", "")
        self._browsing = False
        self._class_filter = ""
        # The big view gives the picture the room of the queue; see toggle_big_view.
        self._big_view = False
        # The action whose standing reason is already in the field, waiting for a
        # second press of the same key or Enter to be committed; see _resolve.
        self._pending_action = ""
        # Receiver, recorder, sampling, clock and analysis fail separately: a
        # single "video problem" line would hide which of them stopped.  The long
        # per-camera wording lives one hover away, on the window's status line.
        self.stage_health: dict[str, str] = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
        self.stage_detail = ""
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
        # The panel itself can hold the focus: in the big view the queue is out of
        # sight and the keys still have to reach the panel from anywhere over it.
        self.setFocusPolicy(Qt.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        # Everything that runs the queue lives in one header: the big view stands the
        # whole block aside and gives its height to the picture.  Two short rows — what
        # is left to work through, and the four classes it is worked as — because the
        # operator reads the picture, not the panel.
        self.header = QWidget(self)
        header_layout = QVBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(4)
        self.summary = QLabel("尚未分析录像", self.header)
        self.summary.setWordWrap(False)
        self.summary.setToolTip("悬浮显示本轮自动对应、无法核实和各类待处理的数量")
        controls.addWidget(self.summary, 1)
        self.queue_filter = QComboBox(self.header)
        self.queue_filter.addItems(["待处理", "待证据", "已处理", "全部"])
        self.queue_filter.setToolTip("待证据是等录像或校时的；标过「先放着」的留在待处理里，灰字显示")
        self.queue_filter.currentIndexChanged.connect(self._queue_filter_changed)
        controls.addWidget(self.queue_filter)
        self.sampling_button = QPushButton("终点线 / 模型", self.header)
        self.sampling_button.setToolTip(
            "绘制本机位终点线，选择本机 YOLOX ONNX 模型并试运行；只分析已封存录像"
        )
        self.sampling_button.clicked.connect(self._configure_sampling)
        controls.addWidget(self.sampling_button)
        self.analyze_button = QPushButton("分析", self.header)
        self.analyze_button.setToolTip("低优先级后台分析；比赛中只读取已封存短段，不等待五分钟归档")
        self.analyze_button.clicked.connect(self._toggle_analysis)
        controls.addWidget(self.analyze_button)
        self.export_button = QPushButton("导出", self.header)
        self.export_button.setToolTip("导出补录、建议删除、建议改号改圈三张清单给 CycleRace")
        self.export_button.clicked.connect(self.export)
        controls.addWidget(self.export_button)
        self.clear_data_button = QPushButton("清空数据", self.header)
        self.clear_data_button.setToolTip(
            "赛前试芯片、试画面后重新开始：删除本场的通过记录、判读和试录画面；"
            "终点线、认车模型和摄像头设置保留"
        )
        self.clear_data_button.clicked.connect(self.clear_data_requested.emit)
        controls.addWidget(self.clear_data_button)
        header_layout.addLayout(controls)
        # A recording nobody drew a line for was never looked at.  That blind spot
        # must be seen at a glance, not found in a tooltip or a waiting row.
        self.coverage_alert = QLabel("", self.header)
        self.coverage_alert.setWordWrap(True)
        self.coverage_alert.setStyleSheet(
            "background:#fdecea; color:#b42318; border:1px solid #f04438; border-radius:4px;"
            " padding:4px 6px; font-weight:600;")
        self.coverage_alert.hide()
        header_layout.addWidget(self.coverage_alert)
        # The four classes are the filter: one row of tabs, each carrying how much of
        # that class is still open, so the queue is narrowed without a second control.
        class_row = QHBoxLayout()
        class_row.setSpacing(4)
        self.class_buttons: dict[str, QPushButton] = {}
        for value in CLASS_ORDER:
            button = QPushButton(CLASS_TITLES[value], self.header)
            button.setCheckable(True)
            button.setToolTip(f"只看{CLASS_TITLES[value]}；再点一次取消，回到全部")
            button.clicked.connect(lambda _checked=False, value=value: self._filter_by_class(value))
            self.class_buttons[value] = button
            class_row.addWidget(button)
        class_row.addStretch(1)
        header_layout.addLayout(class_row)
        layout.addWidget(self.header)
        # 整团过线图 / 芯片刻度就压在画面上面，和原来那一版一样：芯片、点出来的
        # 人、画面检测到的每一个人排在同一条时间轴上。集团核对只有这一处。
        self.timeline_help = QLabel(TIMELINE_HELP, self)
        self.timeline_help.setStyleSheet("color:#667085; font-size:9pt;")
        self.timeline_help.hide()
        layout.addWidget(self.timeline_help)
        self.timeline = BunchTimeline(self)
        self.timeline.seek.connect(self._seek_group_time)
        self.timeline.hide()
        self._timeline_signature = None
        self._band_cache: dict = {}
        self._band_token = None
        # 上一次 N 跳到的检测过线：画面还没回帧就再按一次，也要接着往下跳。
        self._detection_stop = None
        layout.addWidget(self.timeline)
        splitter = self.splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        # Three columns only: when it happened, which class it is, and the one token
        # that names it.  The state and the sentence live in the tooltip and in the
        # detail beside the picture; a row is read at a glance.
        self.table = QTableWidget(0, 3, splitter)
        self.table.setObjectName("finishProblemTable")
        self.table.setStyleSheet("QTableWidget::item:selected {background:#d6eeea; color:#123d36;}")
        self.table.setHorizontalHeaderLabels(["过线时间", "类型", "画面 / 芯片"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumWidth(320)
        for column, width in enumerate((128, 80)):
            self.table.setColumnWidth(column, width)
        self.table.currentCellChanged.connect(lambda row, *_args: self._show(row))
        self.table.cellDoubleClicked.connect(lambda *_args: self._jump())
        # The table treats Enter as its own activation and never passes it on, so
        # one key has to work the same wherever the focus sits inside the panel.
        self.table.installEventFilter(self)
        detail_scroll = QScrollArea(splitter)
        self.detail_scroll = detail_scroll
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
        # 同一张表按过线先后放画面与芯片；修正操作只跟着选中的人出现。
        self.big_help = QLabel("大画面里按住左键左右拖动回看 · N 跳到下一处检测到的过线 · 点压线的人计数 · "
                               "看完整团后回车确认", detail)
        self.big_help.setWordWrap(True)
        self.big_help.setStyleSheet("color:#667085; font-size:9pt;")
        self.big_help.hide()
        detail_layout.addWidget(self.big_help)
        self.count_table = QTableWidget(0, 4, detail)
        self.count_table.setHorizontalHeaderLabels(["顺序", "过线时间", "号码", "芯片时间"])
        self.count_table.setToolTip("按画面过线先后排列；同时过线共用顺序号。未认号的芯片对应仅供参考。")
        self.count_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.count_table.setSelectionMode(QTableWidget.SingleSelection)
        self.count_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.count_table.verticalHeader().setVisible(False)
        self.count_table.horizontalHeader().setStretchLastSection(True)
        self.count_table.setWordWrap(False)
        for column, width in enumerate((58, 108, 92, 108)):
            self.count_table.setColumnWidth(column, width)
        self.count_table.setMinimumHeight(150)
        self.count_table.setMaximumHeight(300)
        self.count_table.currentCellChanged.connect(lambda row, *_args: self._count_row_changed(row))
        self._crossing_rows = []
        self.count_table.hide()
        detail_layout.addWidget(self.count_table)
        self.side_notice = QLabel("", detail)
        self.side_notice.setWordWrap(True)
        self.side_notice.setStyleSheet("font-weight:600;")
        self.side_notice.hide()
        detail_layout.addWidget(self.side_notice)
        # The decisions live in one movable box: when the picture takes the whole
        # area the box travels with it, so the operator looks and decides in the
        # same view instead of walking back to the list between the two.
        self.decision_box = QWidget(detail)
        self.decision_layout = QVBoxLayout(self.decision_box)
        self.decision_layout.setContentsMargins(0, 0, 0, 0)
        self.bib_input = QLineEdit(self.decision_box)
        self.bib_input.setPlaceholderText("号码（补录 / 要删除 / 改成）")
        self.decision_layout.addWidget(self.bib_input)
        self.reason_input = QLineEdit(self.decision_box)
        self.reason_input.setPlaceholderText("原因")
        reason_row = QHBoxLayout()
        reason_row.addWidget(self.reason_input, 1)
        self.decision_layout.addLayout(reason_row)
        self.bib_input.textChanged.connect(self._remember_draft)
        self.reason_input.textChanged.connect(self._remember_draft)
        # 一个人身上的三件事：号码、不是选手、点错了。只在放大判读时出现，
        # 就在号码框下面，认完号顺手按。
        self.person_box = QWidget(self.decision_box)
        person_row = QHBoxLayout(self.person_box)
        person_row.setContentsMargins(0, 0, 0, 0)
        person_row.setSpacing(4)
        self.assign_button = QPushButton("记号码", self.person_box)
        self.assign_button.setToolTip("把号码框里的号码记在名单中选中的人身上；留空表示暂不认号")
        self.assign_button.clicked.connect(self._assign_selected_bib)
        person_row.addWidget(self.assign_button)
        self.exclude_button = QPushButton("不是选手", self.person_box)
        self.exclude_button.setToolTip("选中的这位是行人、车辆或工作人员：不计入人数，原帧和依据一起留档")
        self.exclude_button.clicked.connect(self._exclude_selected)
        person_row.addWidget(self.exclude_button)
        self.restore_button = QPushButton("恢复为选手", self.person_box)
        self.restore_button.setToolTip("去掉排除标记，把这位放回人数里")
        self.restore_button.clicked.connect(self._restore_selected)
        person_row.addWidget(self.restore_button)
        self.remove_button = QPushButton("删除误点", self.person_box)
        self.remove_button.setToolTip("点错的人直接去掉；已排除的人先恢复再删除")
        self.remove_button.clicked.connect(self._remove_selected)
        person_row.addWidget(self.remove_button)
        self.person_box.hide()
        self.decision_layout.addWidget(self.person_box)
        # 判罚就这一列：判断在右边这一栏里从上往下按，编号和数字键一一对应。
        self.action_row = self.action_target = QVBoxLayout()
        # The class entry sits above the decisions, so the tool for this problem is
        # visible where the evidence is; there is no hidden entry to remember.
        self.batch_button = QPushButton(TYPE_ENTRY_LABELS[TYPE_MULTI_CHIP], self.decision_box)
        self.batch_button.setStyleSheet("font-weight:600;")
        self.batch_button.setToolTip("打开这条批次，按成员逐条处理；回车只管确认依据，不会打开它")
        self.batch_button.clicked.connect(self._open_batch)
        self.batch_button.hide()
        self.action_row.addWidget(self.batch_button)
        self.missed_button = QPushButton(TYPE_ENTRY_LABELS[TYPE_MISSED], self.decision_box)
        self.missed_button.setToolTip("跳到候选过线的原帧，逐帧看这一处变化；认不出号码就保留待证据，不凭猜测排除")
        self.missed_button.setStyleSheet("font-weight:600;")
        self.missed_button.clicked.connect(self._open_missed)
        self.missed_button.hide()
        self.action_row.addWidget(self.missed_button)
        self.jump_button = QPushButton("跳到原帧", self.decision_box)
        self.jump_button.setToolTip("跳到原帧；按 F 把画面放大到整个工作区（再按 F 或 Esc 收回）")
        self.jump_button.clicked.connect(self._jump)
        self.action_row.addWidget(self.jump_button)
        # Counting is shown, not typed: how many people are pointed at, and the two
        # ways to take one back.  They sit with the steps through the queue, and only
        # the enlarged picture shows them — that is where pointing happens.
        self.marks_label = QLabel("已点 0 人", self.decision_box)
        self.marks_label.setStyleSheet("font-weight: 600;")
        self.marks_label.setToolTip("画面里点几位就是几个人；左右方向键逐帧")
        self.marks_label.hide()
        self.action_row.addWidget(self.marks_label)
        self.undo_button = QPushButton("撤销", self.decision_box)
        self.undo_button.setToolTip("去掉最后点的那一个人（退格键）")
        self.undo_button.clicked.connect(self.undo_mark)
        self.undo_button.hide()
        self.action_row.addWidget(self.undo_button)
        self.clear_button = QPushButton("清空", self.decision_box)
        self.clear_button.setToolTip("把这一条点过的人全部去掉")
        self.clear_button.clicked.connect(self.clear_marks)
        self.clear_button.hide()
        self.action_row.addWidget(self.clear_button)
        # The two steps through the queue travel with the picture: with the list out of
        # sight the operator still moves problem by problem without leaving the frame.
        self.prev_button = QPushButton("↑ 上一处过线", self.decision_box)
        self.prev_button.setToolTip("按过线先后回到上一处（↑）")
        self.prev_button.clicked.connect(lambda _checked=False: self._step(-1))
        self.prev_button.hide()
        self.next_button = QPushButton("下一处过线 ↓", self.decision_box)
        self.next_button.setToolTip("按过线先后查看下一处（↓）")
        self.next_button.clicked.connect(lambda _checked=False: self._step(1))
        self.next_button.hide()
        queue_steps = QHBoxLayout()
        queue_steps.addWidget(self.prev_button)
        queue_steps.addWidget(self.next_button)
        self.action_row.addLayout(queue_steps)
        self.action_buttons: list[QPushButton] = []
        self.reopen_button = QPushButton("重新处理", self.decision_box)
        self.reopen_button.clicked.connect(self._reopen)
        self.action_row.addWidget(self.reopen_button)
        # 待确认 is the one button that is not a verdict: "先放着" — a passer-by, a
        # blocked line, a frame that cannot be read yet.  The row stays in the queue
        # marked, so nothing is decided by leaving it and nothing is lost by it.
        self.hold_button = QPushButton("待确认", self.decision_box)
        self.hold_button.setToolTip("先放着（T）：标为路人 / 遮挡 / 看不清，留在清单里下次再看；再点一次回到待处理")
        self.hold_button.clicked.connect(self._hold)
        reason_row.addWidget(self.hold_button)
        self.action_row.addStretch(1)
        self.decision_layout.addLayout(self.action_row)
        self.detail_layout = detail_layout
        detail_layout.addWidget(self.decision_box)
        detail_layout.addStretch(1)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        # The line the big view hands the decisions to; hidden until then.  The keys
        # have their own label here so the hint below stays free for what just
        # happened to the row.
        self.big_title = QLabel("", self)
        self.big_title.setStyleSheet("font-weight: 600;")
        self.big_title.hide()
        self.big_keys = QLabel("1–9 处理 · 回车确认 · ↑↓ 上下条 · F / Esc 回清单", self)
        self.big_keys.setStyleSheet("color: #667085; font-size: 9pt;")
        self.big_keys.hide()
        self.big_bar = QHBoxLayout()
        self.big_bar.addWidget(self.big_title, 1)
        self.big_bar.addWidget(self.big_keys)
        layout.addLayout(self.big_bar)
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
                                             "marks": self._marks, "viewed": self._viewed,
                                             "items": self._operator_items,
                                             "arrivals": self._arrival_drafts}, ensure_ascii=False),
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            self.hint.setText(f"草稿保存失败：{error}")

    def _hold(self):
        """Mark the row 待确认 — a passer-by, a blocked line, a frame not to be read yet.

        Nothing is decided by it and nothing is lost: the row stays in the queue,
        grey, with whatever the operator typed as its reason, or the standing one.
        """

        item = self.current_item()
        if item is None:
            return
        if item.item_id in self._held:
            self._held.pop(item.item_id)
            self.hint.setText("已回到待处理。")
        else:
            self._held[item.item_id] = self.reason_input.text().strip() or PENDING_REASON
            self.hint.setText("已标为待确认，留在清单里灰字显示；再按一次就是回到待处理。")
        self.save_drafts()
        self._render()

    def set_browsing(self, browsing):
        self._browsing = bool(browsing)
        for button in self.action_buttons:
            button.setEnabled(not self._browsing)
        item = self.current_item()
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
        self._sync_marks()
        self._pending_action = ""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._live = False
        self._browsing = False
        self._next_analysis = 0.0
        self._drafts, self._held, self._marks, self._operator_items, self._viewed = {}, {}, {}, {}, {}
        self._arrival_drafts = {}
        self._open_doubts = {}
        self._group_context, self._group_context_id = None, None
        self.stage_health = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
        self._final_analysis_pending = False
        self._finalizing_live = False
        self._last_live_signature = None
        try:
            state = json.loads((self.problem_dir / "operator_drafts.json").read_text(encoding="utf-8"))
            self._drafts, self._held = state.get("drafts", {}), state.get("held", {})
            self._marks = state.get("marks", {})
            self._viewed = state.get("viewed", {})
            self._operator_items = state.get("items", {})
            self._arrival_drafts = state.get("arrivals", {})
        except (OSError, ValueError):
            pass
        self.refresh()
        if self.current_item() is None:
            self.current_changed.emit(None)

    def reset_data(self) -> None:
        """Forget the results of a test run before the race itself starts.

        The readers stop first, and the in-memory drafts go with them: the 400 ms
        draft timer would otherwise write the cleared marks back to disk right
        after the window has deleted the files.
        """

        self.stop_analysis()
        self.stop_chip_checks()
        self._draft_timer.stop()
        self.payload = None
        self.items, self.rows = [], []
        self._signature = None
        self._render_signature = None
        self._last_live_signature = None
        self._next_analysis = 0.0
        self._final_analysis_pending = False
        self._finalizing_live = False
        self._drafts, self._held = {}, {}
        self._marks, self._viewed, self._operator_items = {}, {}, {}
        self._arrival_drafts = {}
        self._open_doubts = {}
        self._group_context, self._group_context_id = None, None
        self._editing_mark = -1
        self._shown_id = None
        self._shown_frame = None
        self._pending_action = ""
        self._browsing = False
        self.store = None
        self.bunch_store = None
        self.analysis_status = "等待已封存录像"
        self.bib_input.clear()
        self.reason_input.clear()
        self._sync_marks()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._show(-1)
        self.hint.setText("本场数据已清空；可开始正式录像，测试读数不会保留。")

    def set_scope(self, race_id, stage_id):
        scope = (str(race_id or ""), str(stage_id or ""))
        if scope == self._scope:
            return
        self.stop_analysis()
        self.stop_chip_checks()
        self.save_drafts()
        self._scope = scope
        self._group_context, self._group_context_id = None, None
        self.payload = None
        self._signature = None
        self._render_signature = None
        self._last_live_signature = None
        self.items, self.rows = [], []
        self._shown_id = None
        self.stage_health = dict.fromkeys(("接收", "录制", "取样", "校时", "分析"), "")
        self.bib_input.clear()
        self.reason_input.clear()
        self._sync_marks()
        self._pending_action = ""
        self.refresh()
        if self.current_item() is None:
            self.current_changed.emit(None)

    def set_events_provider(self, provider: Callable[[], object]) -> None:
        self._events_provider = provider

    def refresh(self) -> None:
        if self.problem_dir is None:
            return
        current = self.current_item()
        self.items = [replace(item, evidence={key: value for key, value in item.evidence.items()
                                             if key not in ("group", "split_children")})
                      if item.evidence.get("split_children") else item
                      for item in self.items if not item.evidence.get("superseded") or item == current]
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
            self.stage_detail = f"分析配置不可用：{error}"
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
        # 正常单人有自己的一行，但不在队列里：芯片已经答过，它们让「全部」成为
        # 这场比赛完整的过线记录——按时间一位位翻，想复查哪一次都行。
        self.items = [item for item in self.items if item.kind != KIND_AUTO_PASSED]
        self.items += [auto for auto in (auto_passed_item(entry)
                                         for entry in (self.payload or {}).get("auto_passed", []))
                       if auto is not None]
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
            self.stage_detail = "配置已变更：旧视频结论不可提交，等待重新取样、校时与认车；芯片自检继续。"
            self._set_coverage_alert({})
        elif token != "invalid":
            cameras = (self.payload or {}).get("cameras", {})
            self._set_coverage_alert(cameras)
            missing_lines = sum(len(value.get("missing_line_segments") or ()) for value in cameras.values())
            self.stage_health["取样"] = (f"{missing_lines} 个片段缺线或跨改线边界" if missing_lines
                                         else "已按机位配置" if cameras else "等待已封存录像")
            self.stage_health["校时"] = self.clock_health() or "等待自动校时"
            statuses = []
            for camera, value in cameras.items():
                detail = value.get('error') or value.get('bike_status', {}).get('message', '')
                for key, label in (("sealed_through_ms", "本轮封存至"), ("scanned_through_ms", "扫描至"),
                                   ("analyzed_through_ms", "分析至")):
                    if value.get(key) is not None:
                        detail += f"；{label} {format_chip_time(value[key], date=True)}"
                if value.get("missing_line_segments"):
                    detail += "；部分片段缺线或跨改线边界，尚未核清"
                if value.get("pending_segments"):
                    detail += f"；{value['pending_segments']} 个片段排队中"
                statuses.append(f"机位 {camera}：{detail}")
            self.stage_detail = "；".join(statuses) or "视频等待已封存录像及实际终点线；认车状态待分析。"
        # A manual count of several people becomes one independent crossing per
        # mark. Hide its unchanged source so recognising one never settles the rest.
        split = {}
        for child in self.items:
            source = child.evidence.get("split_source")
            if source:
                split.setdefault(source["item_id"], []).append(child)
        for index, item in enumerate(self.items):
            children = split.get(item.item_id, ())
            if children and item.to_payload() == children[0].evidence["split_source"]:
                self.items[index] = replace(item, evidence=dict(item.evidence, group=children[0].item_id,
                                                               split_children=[child.item_id for child in children]))
        self._load_bunch_store()
        self._render()

    def _bunch_signature(self) -> tuple:
        """What the group journal says about the rows: a saved count changes a row."""

        if self.bunch_store is None:
            return ()
        return tuple(sorted((item_id, int(record.get("counted_riders") or 0), bool(record.get("completed")))
                            for (item_id, _camera), record in self.bunch_store.latest.items()))

    def _load_bunch_store(self) -> None:
        """Re-read the group journal, so a count saved here shows on the row at once."""

        from .bunch_review import BunchReviewStore
        try:
            self.bunch_store = BunchReviewStore(self.problem_dir)
        except (OSError, ValueError) as error:
            self.hint.setText(f"集团核对记录读取失败：{error}")
        self._sync_differences()

    def _sync_differences(self) -> None:
        """A confirmed group's differences are rows of their own, each at its own
        time: the person more than there are chips, the chip nobody stood for."""

        self.items = [item for item in self.items if not item.evidence.get("count_difference")]
        if self.bunch_store is None or self.workspace_dir is None:
            return
        for group in [item for item in self.items if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION) and not item.grouped]:
            try:
                self.items += self.bunch_store.differences(group, self.workspace_dir)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.hint.setText(f"集团差异读取失败：{error}")

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
        self._open_doubts.pop(item.item_id, None)  # set again below, only for a confirmed group
        if item.kind == KIND_AUTO_PASSED:
            return STATE_AUTO
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
            from .bunch_review import load_context
            try:
                context = load_context(self.workspace_dir, item, record["camera"])
                checked = self.bunch_store.get(context)
                if checked is None:
                    return STATE_STALE
            except (ValueError, OSError):
                return STATE_STALE
            current = {event.event_id: event for event in self._events_provider()}
            for chip in record.get("chips", []):
                event = current.get(chip["event_id"])
                if event is not None and (not event.is_active or event.bib != chip["bib"]
                                          or int(event.timeline_timestamp_ms) != chip["chip_ms"]):
                    return STATE_STALE
            if not checked.get("completed"):
                return STATE_PENDING  # a draft, or a completion the current gate reopened
            try:
                doubts = self.bunch_store.open_doubts(context)
            except (ValueError, OSError):
                return f"{STATE_RESOLVED}：集团人工核对 · 组内疑点读取失败"
            # Confirming the count never answers the doubts inside the group:
            # those still open are left on rows of their own.
            self._open_doubts[item.item_id] = frozenset(doubts)
            return f"{STATE_RESOLVED}：集团人工核对" + (f" · 组内疑点 {len(doubts)} 条另列" if doubts else "")
        if self.store is not None:
            resolution, stale = self.store.state(item)
            if resolution is not None:
                if not stale and resolution.action == ACTION_CONFIRM_CROSSING:
                    return STATE_PENDING
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
        states = [self.state(item) for item in self.items
                  if self._is_row(item) and item.kind != KIND_AUTO_PASSED]
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
        from .crossing_time import item_crossing_time, workspace_clocks
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        signature = (tuple((repr(item.to_payload()), self.state(item)) for item in self.items),
                     self.queue_filter.currentIndex(), self._class_filter, tuple(self._held.items()),
                     self._bunch_signature(), repr((self.payload or {}).get("summary")),
                     repr((self.payload or {}).get("cameras")), repr(self._marks), clocks)
        if signature == self._render_signature:
            return
        self._render_signature = signature
        selected = self.rows[self.table.currentRow()].item_id if 0 <= self.table.currentRow() < len(self.rows) else None
        mode = self.queue_filter.currentIndex()
        self.rows = []
        for item in self.items:
            # Members of a batch or a group are worked inside their tool, not as
            # rows of their own: the queue holds what the operator decides by hand.
            if not self._is_row(item):
                continue
            state = self.state(item)
            # 自动对应的单人不是待处理：它只在「全部」里，作为这场比赛的过线记录。
            pending = (self.is_pending_state(state) or state == STATE_INFO) and item.kind != KIND_AUTO_PASSED
            visible = (mode == 3 or (mode == 0 and pending) or (mode == 1 and state == STATE_WAITING)
                       or (mode == 2 and not pending and state != STATE_WAITING and item.kind != KIND_AUTO_PASSED)
                       or (item.item_id == selected and item.evidence.get("superseded")))
            if visible and self._class_matches(item):
                self.rows.append(item)
        times = {item.item_id: item_crossing_time(item, self.workspace_dir, clocks=clocks) for item in self.rows}
        self.rows.sort(key=lambda item: (times[item.item_id].value_ms if times[item.item_id] else 2 ** 62,
                                         item.priority, item.item_id))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        restore = -1
        for index, item in enumerate(self.rows):
            state = self.state(item)
            crossing_time = times[item.item_id]
            time_text = format_chip_time(crossing_time.value_ms) if crossing_time else "--"
            if crossing_time and crossing_time.source in ("expected", "chip") and item.kind != KIND_AUTO_PASSED:
                time_text += " 参考"
            values = (time_text, row_label(item), self.short_note(item, state))
            colour = self._row_colour(item, state)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(f"{state} · {item.title}\n{item.detail}")
                if colour is not None:
                    cell.setForeground(colour)
                self.table.setItem(index, column, cell)
            if item.item_id == selected:
                restore = index
        if restore >= 0:
            self.table.setCurrentCell(restore, 0)
        elif self.rows:
            self.table.setCurrentCell(0, 0)
        self.table.blockSignals(False)
        self._show(self.table.currentRow())
        self._refresh_class_row()
        text = self.summary_text()
        self.summary.setText(self.queue_text())
        self.summary.setToolTip(text)
        self.summary_changed.emit(text)

    def short_note(self, item: ProblemItem, state: str = "") -> str:
        """The one token a row carries: the numbers to look at, never a sentence.

        A group shows how many chips were read, or how many riders the operator
        already counted — once counted, the row says so in grey even before the
        group is closed, so nothing looks untouched that someone has worked on.
        """

        cls = problem_class(item)
        if item.kind == KIND_AUTO_PASSED:
            return item.bibs[0] if item.bibs else "—"
        if cls == TYPE_BUNCH:
            record = self.bunch_store.latest_for(item) if self.bunch_store is not None else None
            from .bunch_review import rider_count
            counted = rider_count(self._marks[item.item_id]) if item.item_id in self._marks else int((record or {}).get("counted_riders") or 0)
            chips = int(item.evidence.get("chip_count") or len(item.bibs) or 0)
            video = str(counted) if counted else (str(item.evidence["video_count"]) + "?"
                                                 if "video_count" in item.evidence else "待数")
            return f"{video} 人 / {chips} 条"
        if cls == TYPE_CONFIRM:
            return SHORT_NOTES.get(item.kind, "—")
        if item.bibs:
            return "、".join(item.bibs[:4]) + ("…" if len(item.bibs) > 4 else "")
        decision, stale = self.store.state(item) if self.store is not None else (None, False)
        if decision is not None and not stale and decision.action == ACTION_CONFIRM_CROSSING:
            return "已过线·待认号"
        return "—"

    def _row_colour(self, item: ProblemItem, state: str) -> Optional[QColor]:
        """Grey for what is settled or already looked at, red for what fights back."""

        if state in (STATE_DISPUTED, STATE_STALE):
            return QColor("#b54747")
        if (not self.is_pending_state(state) or item.item_id in self._held
                or self._in_progress(item) or self._marks.get(item.item_id)):
            return QColor("#8a8f98")
        return None

    def _is_row(self, item: ProblemItem) -> bool:
        """Whether the item is a row of the queue.

        Members of a batch or a group are worked inside their tool. Once a group
        is confirmed, a doubt it still holds — or one already answered on its own
        row — is a row of its own: confirming the count never answers it.
        """

        if not item.grouped:
            return True
        doubts = self._open_doubts.get(item.grouped)
        if doubts is None:
            return False
        if item.item_id in doubts:
            return True
        decision, _stale = self.store.state(item) if self.store is not None else (None, False)
        return decision is not None

    def _in_progress(self, item: ProblemItem) -> bool:
        """A group already worked on but not closed: a draft, or a completion
        saved under an older rule and reopened."""

        if self.bunch_store is None:
            return False
        from .bunch_review import COMPLETION_POLICY_VERSION
        record = self.bunch_store.latest_for(item)
        return bool(record) and not (record.get("completed")
                                     and record.get("completion_policy_version") == COMPLETION_POLICY_VERSION)

    def _class_matches(self, item: ProblemItem) -> bool:
        return not self._class_filter or problem_class(item) == self._class_filter

    def _refresh_class_row(self) -> None:
        counts = self.class_counts()
        for value, button in self.class_buttons.items():
            button.setText(f"{CLASS_TITLES[value]} {counts[value]}")
            button.setChecked(value == self._class_filter)

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
        self._shown_since = time.monotonic()
        if changed and item is not None and item.item_id not in self._marks:
            # A group that was counted before shows its own people again: the record
            # is the draft, so reopening never starts from zero.
            record = self.bunch_store.latest_for(item) if self.bunch_store is not None else None
            if record is not None and record.get("marks"):
                self._marks[item.item_id] = [dict(mark) for mark in record["marks"]]
        self._sync_step_buttons()
        if changed:
            self._shown_id = identity
            self._group_context, self._group_context_id = None, None
            self._editing_mark = -1
            # A number already read inside the group comes along to its own row.
            bib, reason = self._drafts.get(identity, (item.evidence.get("read_bib", "") if item else "", ""))
            self.bib_input.blockSignals(True)
            self.reason_input.blockSignals(True)
            self.bib_input.setText(bib)
            self.reason_input.setText(reason)
            self.bib_input.blockSignals(False)
            self.reason_input.blockSignals(False)
            self._browsing = False
            # A standing reason belongs to the row that was on screen; another row
            # starts the two-stage bargain over instead of inheriting the claim.
            self._pending_action = ""
        for button in self.action_buttons:
            self.action_target.removeWidget(button)
            button.deleteLater()
        self._sync_marks()
        self.action_buttons = []
        self.card.clear()
        self.filmstrip.clear()
        if item is None:
            if self._big_view:
                # Nothing left to enlarge: the emptied queue takes its panes back.
                self.toggle_big_view()
            self.batch_button.hide()
            self.decision_box.hide()
            self.title.setText(self.queue_text())
            self.detail.setText("正常单人自动对应；过线记录都在左边的表里，也可以从录像直接点人补记。")
            self.jump_button.setEnabled(False)
            self.reopen_button.setEnabled(False)
            self.missed_button.hide()
            self.hold_button.setEnabled(False)
            if changed:
                self.current_changed.emit(None)
            return
        self.decision_box.show()
        self.hold_button.setEnabled(item.status != STATUS_WAITING)
        self.hold_button.setText("返回待处理" if item.item_id in self._held else "待确认")
        batch = self._batch_parent(item)
        self.batch_button.setVisible(batch is not None)
        self.missed_button.setVisible(_missed_entry(item, batch, self._bunch_parent(item)))
        self.missed_button.setEnabled(item.status != STATUS_WAITING)
        # Both fields are set further down, where the buttons say whether this row
        # takes a number at all: an empty box beside a decision that cannot use it
        # is one more thing to look past.
        self.bib_input.setVisible(False)
        # The reason is filled in from the frame with the same press; the field stays
        # in the drafts but is not a step of its own on the judging surface.
        self.reason_input.setVisible(False)
        state = self.state(item)
        headline = (
            f"{item.title} · {format_chip_time(item.chip_time_ms) if item.chip_time_ms else '--'} · {state}"
        )
        self.title.setText(headline)
        # The big view shows the same row without the list; keep it named there too.
        self.big_title.setText(headline)
        detail = item.detail + (f"\n{item.status_note}" if item.status_note else "")
        bike = item.evidence.get("bike")
        if bike:
            detail += f"\n认车：{bike.get('label')} {bike.get('score')}" + (f"（{bike['reason']}）" if bike.get("reason") else "")
        if item.calibration_version:
            detail += f"\n校时版本 v{item.calibration_version}"
        self.detail.setText(detail)
        if item.kind == KIND_AUTO_PASSED:
            # 这一行是过线记录，不是待办：说清楚，免得有人在这里等一个要按的钮。
            self.hint.setText(f"{item.bibs[0] or '该选手'} 已自动对应；这是复查，不会新建待处理。")
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
        # The row asks its own question: the buttons below are the answer, in the
        # operator's words, and one press is the whole decision — no reason to read
        # first, no second press to agree with it.
        buttons: tuple[tuple[str, str, str], ...] = ()
        is_bunch = item.kind in (KIND_BUNCH, KIND_OBSTRUCTION)
        if state.startswith(STATE_HANDOFF):
            buttons = ((ACTION_LABELS[ACTION_SYNCED], ACTION_SYNCED, ""),)
        elif not (is_bunch or resolved or item.status == STATUS_WAITING):
            buttons = operator_actions(item)
        if batch is not None:
            buttons = ()
            self.reopen_button.hide()
        if item.evidence.get("superseded"):
            buttons = ()
            self.hold_button.setEnabled(False)
        takes_bib = any(action in (ACTION_RIDER, ACTION_RECORD_BIB, ACTION_CHANGE,
                                   ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP)
                        for _label, action, _bib in buttons)
        self.bib_input.setVisible(takes_bib and item.kind != KIND_TWO_CHIPS_ONE_CROSSING)
        for label, action, bib in buttons:
            button = QPushButton(label, self)
            button.setProperty("problemAction", action)
            # Enter answers the row's first button; which chip record of a two-chip
            # crossing stays is the operator's own read, so it is never the default.
            button.setProperty("enterDefault",
                               not (item.kind == KIND_TWO_CHIPS_ONE_CROSSING and action == ACTION_DELETE_EXTRA_CHIP))
            key = len(self.action_buttons) + 1
            button.setToolTip(
                f"号码框填了就补录；没填就先记为待认号（数字键 {key}）" if action == ACTION_RIDER
                else f"{label}（数字键 {key}）"
            )
            button.clicked.connect(lambda _checked=False, action=action, bib=bib: self._resolve_operator(action, bib))
            self.action_target.insertWidget(len(self.action_buttons), button)
            self.action_buttons.append(button)
        self.set_browsing(self._browsing)
        if changed:
            self.current_changed.emit(item)

    def _reopen(self) -> None:
        item = self.current_item()
        if item is not None and item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
            context = self._inline_context()
            if context is None or self.bunch_store is None:
                self.hint.setText(self._group_context_error or "本团证据不可用，请先刷新。")
                return
            record = self.bunch_store.get(context)
            if record is None:
                self.hint.setText("集团证据已变化，请刷新后重新核对。")
                return
            marks = [dict(mark) for mark in self._marks.get(item.item_id, record.get("marks", []))]
            try:
                self.bunch_store.save(context, marks, record.get("seconds", 0), completed=False,
                                      fully_viewed=record.get("fully_viewed", False),
                                      fallback_bias_ms=record.get("fallback_bias_ms", 0),
                                      viewed_ranges=record.get("viewed_ranges", []),
                                      visited_candidates=record.get("visited_candidates", ()))
            except (OSError, RuntimeError, ValueError) as error:
                self.hint.setText(f"集团未能重新处理：{error}")
                return
            self._marks[item.item_id] = marks
            self.save_drafts()
            self._sync_differences()
            self._render()
            self.hint.setText("已重新打开本团，原点数保留；可直接修改。")
            return
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

        if not self._queue_key(event):
            super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        # The table answers printable keys with its own search and Enter with its own
        # activation, so the queue's keys are routed before it can take them.
        if (watched is self.table and event.type() == QEvent.KeyPress
                and self._queue_key(event)):
            return True
        return super().eventFilter(watched, event)

    def _queue_key(self, event) -> bool:
        """Answer the keys that work the queue; True when this press is one of them."""

        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm_pending()
            return True
        if key == Qt.Key_F:
            # Enlarging has always been F; the queue keeps the key and only changes
            # where the room comes from — the panes either side, not a second window.
            self.toggle_big_view()
            return True
        if key == Qt.Key_Escape and self._big_view:
            self.toggle_big_view()
            return True
        if key == Qt.Key_T:
            # 待确认 is the one key that must work from the picture: it is pressed
            # when the frame does not tell enough, which is exactly where the eyes are.
            self._hold()
            return True
        if self._big_view and key in (Qt.Key_Up, Qt.Key_Down):
            # With the list out of sight, the arrows walk the queue instead of it.
            self._step(-1 if key == Qt.Key_Up else 1)
            return True
        if key in (Qt.Key_Left, Qt.Key_Right):
            # The frames step from here too, so the operator never has to give the
            # keyboard away by clicking the picture first.
            self.frame_step_requested.emit(-1 if key == Qt.Key_Left else 1)
            return True
        if key == Qt.Key_N and self._jump_detection(-1 if event.modifiers() & Qt.ShiftModifier else 1):
            return True
        if key == Qt.Key_Backspace:
            self.undo_mark()
            return True
        if Qt.Key_1 <= key <= Qt.Key_9:
            self._click_action(key - Qt.Key_1 + 1)
            return True
        return False

    def _step(self, delta: int) -> None:
        """Move to the neighbouring problem without leaving the enlarged picture."""

        row = self.table.currentRow() + delta
        if 0 <= row < len(self.rows):
            self.table.setCurrentCell(row, 0)
        if self._big_view:
            self.setFocus()

    def _sync_step_buttons(self) -> None:
        """The two queue steps show with the picture only; beside the list the arrows do it."""

        row = self.table.currentRow()
        for button, delta, enabled in ((self.prev_button, -1, row > 0),
                                       (self.next_button, 1, 0 <= row < len(self.rows) - 1)):
            button.setVisible(self._big_view)
            button.setEnabled(enabled)

    # ----- counting people on the picture -------------------------------
    def current_marks(self) -> list:
        item = self.current_item()
        return list(self._marks.get(item.item_id, ())) if item is not None else []

    def add_mark(self, x: float, y: float, recorder_ms: int, frame=None) -> None:
        """One click on the picture is one person at the moment the frame shows.

        The position is kept as a fraction of the frame, so the mark stays on the
        same person whatever the window is resized or zoomed to; the moment is what
        the chips are compared against.
        """

        item = self.current_item()
        if (item is None or item.evidence.get("configuration_stale") or item.evidence.get("superseded")
                or item.status == STATUS_WAITING or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0)):
            return
        from .bunch_review import mark_on_frame, rider_count
        ref = dict(frame or item.evidence.get("frame") or {})
        marks = self._marks.setdefault(item.item_id, [])
        if any(mark_on_frame(mark, ref, recorder_ms)
               and (mark["x"] - x) ** 2 + (mark["y"] - y) ** 2 < .02 ** 2 for mark in marks):
            self.hint.setText("这个位置已计过数；并排选手请分别点各自前轮。")
            return
        marks.append({"x": round(float(x), 4), "y": round(float(y), 4), "recorder_ms": int(recorder_ms),
                      "bib": "", "exclusion_reason": "", "frame": ref})
        self._editing_mark = len(marks) - 1
        self._marks_updated()
        # 人数等于芯片条数也不收尾：实际可能多一位没读到芯片的人，要看完整团再确认。
        context = self._inline_context()
        if context is not None and rider_count(marks) == len(context.chips):
            self.hint.setText(f"已点 {rider_count(marks)} 人，与芯片条数相同；看完整团、确认没有漏点再按回车。")
        else:
            self.hint.setText(f"已点 {rider_count(marks)} 人；按过线时间排序，点错按退格撤销。")

    def undo_mark(self) -> None:
        item = self.current_item()
        marks = self._marks.get(item.item_id, []) if item is not None else []
        if not marks:
            return
        marks.pop()
        self._editing_mark = len(marks) - 1
        self._marks_updated()
        self.hint.setText(f"已撤销最后一个标记，剩余 {len(marks)} 个标记。")

    def clear_marks(self) -> None:
        item = self.current_item()
        if item is None or not self._marks.pop(item.item_id, None):
            return
        self._editing_mark = -1
        self._marks_updated()
        self.hint.setText("已清空这一条的计数。")

    def _sync_marks(self) -> None:
        """Show the count beside the picture and hand it to whoever draws it."""

        from .bunch_review import rider_count
        marks = self.current_marks()
        self.marks_label.setText(f"已点 {rider_count(marks)} 人")
        show = self.current_item() is not None and bool(self.current_item().evidence.get("frame"))
        self.marks_label.setVisible(show and not self._big_view)
        self.undo_button.hide()  # 退格仍可撤销；所选那个人用「删除误点」
        self.clear_button.hide()
        self.undo_button.setEnabled(bool(marks))
        self.clear_button.setEnabled(bool(marks))
        self._sync_sidebar()
        self.marks_changed.emit(tuple(marks))

    def _inline_context(self):
        item = self.current_item()
        if item is None or item.kind not in (KIND_BUNCH, KIND_OBSTRUCTION) or self.workspace_dir is None:
            return None
        key = repr(item.to_payload())
        if key != self._group_context_id:
            from .bunch_review import load_context
            self._group_context, self._group_context_error = None, ""
            self._group_context_id = key
            try:
                self._group_context = load_context(self.workspace_dir, item)
            except (OSError, ValueError) as error:
                self._group_context_error = str(error)
        return self._group_context

    def _view_state(self, context):
        key = f"{context.item.item_id}|{context.camera}"
        state = self._viewed.get(key)
        if state is None or state.get("signature") != context.signature:
            record = self.bunch_store.get(context) if self.bunch_store else None
            state = {"signature": context.signature, "ranges": (record or {}).get("viewed_ranges", []),
                     "fully_viewed": False}
            self._viewed[key] = state
        return state

    def record_viewed_frame(self, frame, recorder_ms):
        context = self._inline_context()
        if context is None or int(frame.get("camera_index", 0)) != context.camera:
            return
        if not context.start_ms - 50 <= recorder_ms <= context.end_ms + 50:
            return
        if str(frame.get("segment_id")) not in {s.segment.segment_id for s in context.segments}:
            return
        from .bunch_review import add_viewed, view_reach
        state = self._view_state(context)
        state["ranges"] = add_viewed(state["ranges"], recorder_ms, context.start_ms, context.end_ms,
                                     view_reach(context.item))
        self.timeline.current = int(recorder_ms)
        self._draft_timer.start()
        self._sync_sidebar()

    def _sync_sidebar(self):
        """把这次过线的人和芯片记录放在同一张表里。"""

        item, marks = self.current_item(), self.current_marks()
        chips = self._sidebar_chips(item)
        self._fill_count_table(marks)
        self.side_notice.setText(self._sidebar_notice(item, marks, chips))
        self._sync_person_buttons()
        self._sync_timeline()
        if self._big_view:
            for widget in (self.big_help, self.count_table, self.side_notice):
                widget.setVisible(True)

    def _sync_timeline(self):
        """画面上面那条刻度：本团的芯片、点出来的人、检测到的每一个人。

        只在放大判读集团时出现，清单里那一屏留给队列。同一画面每秒重画几十次
        是白费的，画的东西没变就不重画。
        """

        from .bunch_review import is_excluded
        context = self._inline_context() if self._big_view else None
        self.timeline.setVisible(context is not None)
        self.timeline_help.setVisible(context is not None)
        if context is None:
            self.timeline.context = None
            self._timeline_signature = None
            return
        changed = self.timeline.context is not context
        if changed:
            # 换了另一团：紫色标线和上一团的图片带都不能留下。
            self.timeline.current = None
            self.timeline.image = None
        marks = self.current_marks()
        cursor = (None if self.timeline.current is None else
                  round((self.timeline.current - context.start_ms) / max(1, context.end_ms - context.start_ms)
                        * max(1, self.timeline.width() - 1)))
        signature = (id(context), len(context.chips), len(context.detected), cursor,
                     tuple((mark['recorder_ms'], is_excluded(mark)) for mark in marks))
        if signature == self._timeline_signature:
            return
        self._timeline_signature = signature
        self.timeline.context = context
        self._load_group_band(context)
        self.timeline.detected = context.detected
        self.timeline.marks = marks
        self.timeline.update()

    def _load_group_band(self, context) -> None:
        """本团的过线图片带：取样条拼一张要一两百毫秒，等判读屏画好再补上。

        同一团只算一次（记住这张图），换团时旧图先撤掉，免得看着像这一团的。
        """

        key = (str(context.workspace), context.camera, context.start_ms, context.end_ms)
        cached = self._band_cache.get(key)
        if cached is not None:
            self.timeline.image = cached
            return
        if self._band_token == key:
            return
        self._band_token = key
        QTimer.singleShot(80, lambda: self._finish_group_band(key, context))

    def _finish_group_band(self, key, context) -> None:
        if self._band_token != key or context is not self._inline_context():
            return                                  # 早换团了，这一张已经没用
        from .bunch_timeline import group_strip, pixmap_of
        try:
            image, present = group_strip(context)
        except (OSError, ValueError, RuntimeError) as error:
            self.hint.setText(f"过线图没读出来：{error}；仍可逐帧点数。")
            return
        if self._band_token != key:
            return
        if len(self._band_cache) > 3:
            self._band_cache.clear()
        self._band_cache[key] = pixmap_of(image)
        self.timeline.image = self._band_cache[key]
        self.timeline.update()
        if not present:
            self.hint.setText("本团没有已生成的过线图缓存；仍可逐帧点数。灰色区域不代表无人过线。")

    def _seek_group_time(self, recorder_ms: int) -> bool:
        """点到刻度上哪一刻，画面就跳到那一刻的原帧。"""

        context = self._inline_context()
        if context is None:
            return False
        for piece in context.segments:
            if piece.start_ms <= recorder_ms <= piece.end_ms:
                self.jump_requested.emit({
                    "camera_index": context.camera, "segment_id": piece.segment.segment_id,
                    "video_path": str(piece.path), "position_ms": int(recorder_ms - piece.start_ms),
                    "frame_index": None,
                })
                self.hint.setText("已跳到刻度上那一刻；在大画面按住左键拖动回看，单击压线的人计数。")
                return True
        return False

    def _jump_detection(self, direction: int) -> bool:
        """N / Shift+N：跳到本团下一处 / 上一处检测到的过线。

        检测到的过线只是停靠点，不是人数：一团 12 人检测出 9 次，漏掉的 3 人
        还是靠逐帧看、在画面上点出来。这里只挪画面，一个人也不加。
        """

        context = self._inline_context()
        if context is None:
            return False
        stops = []
        for moment in sorted(int(entry["recorder_ms"]) for entry in context.detected):
            if not stops or moment - stops[-1] > DETECTION_SLACK_MS:   # 同一帧上的两人是同一处
                stops.append(moment)
        if not stops:
            self.hint.setText("本团没有检测到的过线；按住画面左右拖动逐帧看，点压线的人计数。")
            return True
        # 画面落到的帧和检测时刻差半帧以内；刚跳过来、画面还没回帧或没挪开，
        # 就从这一处接着数，免得连按 N 停在同一处。
        reference = self.timeline.current
        last = self._detection_stop
        if last is not None and last[0] == context.signature and (
                reference == last[2] or reference is None
                or abs(reference - last[1]) <= DETECTION_SLACK_MS * 5):
            reference = last[1]
        if reference is None:
            reference = context.start_ms - DETECTION_SLACK_MS - 1 if direction > 0 else context.end_ms + DETECTION_SLACK_MS + 1
        ahead = ([stop for stop in stops if stop > reference + DETECTION_SLACK_MS] if direction > 0
                 else [stop for stop in stops if stop < reference - DETECTION_SLACK_MS])
        if not ahead:
            self.hint.setText(f"已是本团{'最后' if direction > 0 else '第一'}一处检测到的过线（共 {len(stops)} 处）；"
                              "检测会漏人，仍请把整团看完。")
            return True
        target = ahead[0] if direction > 0 else ahead[-1]
        before = self.timeline.current
        if not self._seek_group_time(target):
            self.hint.setText("这一处检测过线不在录像里（录像缺口）；请拖动画面看其余时段。")
            return True
        self._detection_stop = (context.signature, target, before)
        self.hint.setText(f"检测到的过线 第 {stops.index(target) + 1} / {len(stops)} 处 · 只跳画面不计数；"
                          "看到压线的人请单击计数，检测会漏人。")
        return True

    def _sidebar_chips(self, item) -> list:
        return [(chip.bib, chip.chip_ms) for chip in self._crossing_chips(item)]

    def _crossing_chips(self, item) -> list:
        """每块芯片保留自己的官方时间，附近的参考芯片不当成此人身份。"""

        from .bunch_review import BunchChip
        if item is None:
            return []
        context = self._inline_context()
        if context is not None:
            return context.chips
        try:
            events = {event.event_id: event for event in self._events_provider()} if item.event_ids else {}
        except Exception:  # noqa: BLE001 - 对照表不能因为上游掉线就打不开
            events = {}
        members = {member["event_id"]: member for member in item.evidence.get("batch_members", [])}
        chips = []
        for index, bib in enumerate(item.bibs):
            event_id = item.event_ids[index] if index < len(item.event_ids) else ""
            event = events.get(event_id)
            member = members.get(event_id, {})
            chip_ms = (int(event.timeline_timestamp_ms) if event is not None else
                       member.get("chip_ms"))
            if chip_ms is None and len(item.bibs) == 1:
                chip_ms = item.chip_time_ms
            chips.append(BunchChip(event_id, bib, chip_ms, 0))
        return sorted(chips, key=lambda chip: chip.chip_ms if chip.chip_ms is not None else 2 ** 62)

    def _sidebar_notice(self, item, marks, chips) -> str:
        """人数差、整团看了多少；逐人时间参考只提醒，不增加集团完成门槛。"""

        from .bunch_review import rider_count, uncovered_intervals
        if item is None:
            return ""
        counted = rider_count(marks)
        text = f"画面 {counted} 人 · 芯片 {len(chips)} 条"
        context = self._inline_context()
        if context is None:
            return text + (f" · {self._group_context_error}" if self._group_context_error else "")
        if counted and counted == len(chips):
            text += " · 人数一致"
            # 「待看」是按时间没对上人的芯片：可能只是校时偏差，也可能是漏读加多余芯片。
            waiting = sum(row["mark_index"] is None for row in self._crossing_rows)
            if waiting:
                text += f"（{waiting} 条芯片按时间未对上，仅参考）"
        elif counted < len(chips):
            text += f" · 还差 {len(chips) - counted} 人"
        else:
            text += f" · 多出 {counted - len(chips)} 人（疑似漏读）待核"
        if context.gaps:
            return text + f" · 录像缺口 {len(context.gaps)} 处，不能确认完整"
        unseen = sum(right - left for left, right in uncovered_intervals(
            context.start_ms, context.end_ms, self._view_state(context)["ranges"], tolerance=100))
        if unseen:
            return text + f" · 整团还有 {unseen / 1000:.1f} 秒没看"
        if not counted:
            return text + " · 已看完整团"
        return text + " · 已看完整团，回车确认" + ("；人数差异另列待处理" if counted != len(chips) else "")

    def _fill_count_table(self, marks) -> None:
        """过线顺序、过线时间、认出号码与芯片时间在同一行里。"""

        from .bunch_review import is_excluded
        from .crossing_rows import crossing_rows
        from .crossing_time import recorder_crossing_time
        item = self.current_item()
        context = self._inline_context()
        chips = self._crossing_chips(item)
        self._crossing_rows = crossing_rows(marks, chips, time_reference=context is not None)
        self.count_table.blockSignals(True)
        self.count_table.setRowCount(len(self._crossing_rows))
        selected = -1
        for row, entry in enumerate(self._crossing_rows):
            index, chip_index = entry["mark_index"], entry["chip_index"]
            chip = chips[chip_index] if chip_index is not None else None
            mark = marks[index] if index is not None else None
            excluded = mark is not None and is_excluded(mark)
            if mark is not None:
                frame = dict(mark.get("frame") or {})
                if item is not None and item.calibration_version:
                    frame["calibration_version"] = item.calibration_version
                crossing = recorder_crossing_time(mark["recorder_ms"], frame, self.workspace_dir, context=context)
                time_text = format_chip_time(crossing.value_ms)
                bib = "已排除" if excluded else (mark.get("bib") or (f"{chip.bib} 参考" if chip else "未认号"))
                chip_text = format_chip_time(chip.chip_ms) if chip else "—"
                tooltip = ("画面过线时间已按本条校时换算；芯片时间为官方读数。"
                           if crossing.source == "aligned" else "录像原时刻（尚未换算）；芯片时间为官方读数。")
                tooltip += f"\n录像原时刻：{format_chip_time(mark['recorder_ms'])}"
                if entry["reference"]:
                    tooltip += "\n按时间对应，仅作参考；未确认此人号码。"
                if excluded:
                    tooltip += f"\n排除依据：{mark.get('exclusion_reason', '')}"
                entry["frame"] = frame
            else:
                time_text, bib, chip_text = "待看画面", chip.bib, format_chip_time(chip.chip_ms)
                tooltip = "芯片已有读数，尚未在画面中确认对应的人；不编排过线名次。"
            values = (entry["order"], time_text, bib, chip_text)
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setData(Qt.UserRole, index)
                cell.setToolTip(tooltip)
                if excluded or mark is None:
                    cell.setForeground(QColor("#8a8f98"))
                self.count_table.setItem(row, column, cell)
            if index is not None and index == self._editing_mark:
                selected = row
        if selected >= 0:
            self.count_table.setCurrentCell(selected, 0)
        else:
            self.count_table.setCurrentCell(-1, -1)
        self.count_table.blockSignals(False)

    def _sync_person_buttons(self) -> None:
        """一个人身上的三件事，只在放大判读、而且名单里真的选中一位时出现。"""

        from .bunch_review import is_excluded
        marks = self.current_marks()
        valid = self._big_view and 0 <= self._editing_mark < len(marks)
        excluded = valid and is_excluded(marks[self._editing_mark])
        takes_bib = any(button.property("problemAction") in (ACTION_RIDER, ACTION_RECORD_BIB, ACTION_CHANGE,
                                                             ACTION_DELETE_CHIP, ACTION_DELETE_EXTRA_CHIP)
                        for button in self.action_buttons)
        item = self.current_item()
        self.bib_input.setVisible((valid and not excluded) or
                                  (takes_bib and item is not None and item.kind != KIND_TWO_CHIPS_ONE_CROSSING))
        self.person_box.setVisible(valid)
        for widget in (self.assign_button, self.exclude_button):
            widget.setVisible(not excluded)
        self.restore_button.setVisible(bool(excluded))
        for widget in (self.assign_button, self.exclude_button, self.remove_button):
            widget.setEnabled(valid and not excluded)

    def _marks_updated(self):
        context = self._inline_context()
        if context is not None:
            self._view_state(context)["fully_viewed"] = False
        self.save_drafts()
        self._sync_marks()
        # The row and its card show progress without requiring a separate Save.
        row = self.table.currentRow()
        item = self.current_item()
        if item is not None and row >= 0:
            self.table.item(row, 2).setText(self.short_note(item))
            for column in range(self.table.columnCount()):
                self.table.item(row, column).setForeground(QColor("#8a8f98"))

    def _count_row_changed(self, row: int) -> None:
        """名单里选了一行，这位就是下面的号码和纠正的对象；画面不动。

        整团是逐帧一起看完、一起判的：跳到某个人的那一帧，操作员就认不出
        画面上谁是谁了。要看那一刻，点刻度。
        """

        if not 0 <= row < len(self._crossing_rows):
            return
        index = self._crossing_rows[row]["mark_index"]
        self._editing_mark = index if index is not None else -1
        if index is not None:
            self.bib_input.setText(str(self.current_marks()[index].get("bib") or ""))
        self._sync_person_buttons()

    def _assign_selected_bib(self) -> None:
        """号码框里的号码，记在名单中选中的那一位身上。"""

        index = self._editing_mark
        marks = self.current_marks()
        if not 0 <= index < len(marks):
            self.hint.setText("先在名单里点一位，再记号码。")
            return
        from .bunch_review import is_excluded
        bib = self.bib_input.text().strip()
        if is_excluded(marks[index]):
            self.hint.setText("已排除的人不填号码；先「恢复为选手」再认号。")
            return
        if bib and any(i != index and not is_excluded(mark) and mark.get("bib") == bib
                       for i, mark in enumerate(marks)):
            self.hint.setText("这个号码已记在另一人身上，请先核对。")
            return
        marks[index]["bib"] = bib
        self._marks_updated()
        self.hint.setText("号码已记住；普通计数不必逐人认号。")

    def _exclude_selected(self) -> None:
        index = self._editing_mark
        if not 0 <= index < len(self.current_marks()):
            self.hint.setText("先在名单里点一位，再记为非选手。")
            return
        from .crossing_widgets import EXCLUSION_STANDING_REASON
        self._exclude_mark(index, EXCLUSION_STANDING_REASON)

    def _restore_selected(self) -> None:
        index = self._editing_mark
        if not 0 <= index < len(self.current_marks()):
            return
        self._exclude_mark(index, "")

    def _remove_selected(self) -> None:
        index = self._editing_mark
        if not 0 <= index < len(self.current_marks()):
            self.hint.setText("先在名单里点一位，再删除。")
            return
        self._remove_mark(index)

    def _exclude_mark(self, index, reason):
        marks = self.current_marks()
        if not 0 <= index < len(marks):
            return
        marks[index]["exclusion_reason"] = reason.strip()
        self._editing_mark = index
        self._marks_updated()
        self.hint.setText("已记为非选手，证据保留。" if reason else "已恢复为选手计数。")

    def _remove_mark(self, index):
        item = self.current_item()
        if item is None or not 0 <= index < len(self.current_marks()):
            return
        self._marks[item.item_id].pop(index)
        self._editing_mark = min(index, len(self._marks[item.item_id]) - 1)
        self._marks_updated()

    def _confirm_pending(self) -> None:
        """Enter answers the row: the count first, else the first button it shows.

        A two-chip crossing is the one question Enter will not answer by itself:
        which record stays is the operator's own read of the frame, so it waits
        for a number key or a click.
        """

        if self.current_marks():
            # People counted on the picture and nothing else chosen: for the class
            # that is settled by counting, the count itself is the claim.
            self._confirm_count()
            return
        buttons = [button for button in self.action_buttons
                   if button.isVisibleTo(self) and button.isEnabled() and button.property("enterDefault")]
        if buttons:
            buttons[0].click()
            return
        self.hint.setText("回车是第一个按钮；这一条要先按数字键或点按钮选一项。" if self.action_buttons
                          else "回车确认要先选一项处理：按数字键。")

    def _confirm_count(self) -> None:
        """Turn the counted people into the decision of this problem's class."""

        item = self.current_item()
        if item is None:
            return
        from .bunch_review import rider_count
        counted = rider_count(self.current_marks())
        if item.kind in (KIND_BUNCH, KIND_OBSTRUCTION):
            self._confirm_bunch_count(item, counted)
            return
        if len(self.current_marks()) > 1 and ACTION_CONFIRM_CROSSING in item.actions:
            self._split_counted_crossings(item)
            return
        if not counted:
            reasons = list(dict.fromkeys(mark.get("exclusion_reason", "") for mark in self.current_marks()
                                         if mark.get("exclusion_reason")))
            self.reason_input.setText("；".join(reasons))
            self._resolve(ACTION_NOT_RIDER)
            return
        if item.evidence.get("batch_members"):
            self.hint.setText(f"已点 {counted} 人；请在画面下选择具体记录，再确认有效或建议作废。")
            return
        if problem_class(item) == TYPE_MULTI_CHIP:
            # One person on one crossing with two chips is one vehicle carrying two
            # chips; two people are two riders crossing side by side.
            self._resolve(ACTION_DELETE_EXTRA_CHIP if counted == 1 else ACTION_SIDE_BY_SIDE)
            return
        if problem_class(item) == TYPE_MISSED:
            if ACTION_CONFIRM_CROSSING not in item.actions:
                self.hint.setText("本条已有芯片记录，请选择确认有效或证据不足；另一个漏读者可从原帧补记过线。")
                return
            if counted > 1:
                self._split_counted_crossings(item)
                return
            bib = self.bib_input.text().strip()
            if not bib and counted == 1:
                bib = next((mark.get("bib", "") for mark in self.current_marks() if not mark.get("exclusion_reason")), "")
                self.bib_input.setText(bib)
            if bib:
                self._resolve(ACTION_RECORD_BIB)
            else:
                self._resolve(ACTION_CONFIRM_CROSSING)
            return
        self._hold()

    def _split_counted_crossings(self, item: ProblemItem) -> None:
        """Keep independently counted people as independently identifiable crossings."""

        from .problem_items import stable_item_id
        marks = self.current_marks()
        path = self.problem_dir / "manual_problems.json"
        try:
            children = []
            for mark in marks:
                frame = dict(mark.get("frame") or item.evidence.get("frame") or {})
                if not frame:
                    raise ValueError("每位过线者都需要对应原帧")
                key = stable_item_id("manual", item.item_id, mark["recorder_ms"], mark["x"], mark["y"])
                children.append(ProblemItem(key, KIND_MANUAL_REVIEW, CATEGORY_VIDEO,
                                            "人工点出的过线者；芯片状态待核实",
                                            evidence={"frame": frame, "recorder_ms": mark["recorder_ms"],
                                                      "operator_scope": self._scope,
                                                      "position": [mark["x"], mark["y"]],
                                                      **({"card": item.evidence["card"]} if item.evidence.get("card") else {}),
                                                      "split_source": item.to_payload()}))
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            ids = {value["item_id"] for value in existing}
            existing.extend(child.to_payload() for child in children if child.item_id not in ids)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            self._operator_items[item.item_id] = item.to_payload()
            for child, mark in zip(children, marks):
                self._marks[child.item_id] = [dict(mark)]
                self._drafts[child.item_id] = [mark.get("bib", ""), ""]
                self.store.resolve(child, ACTION_NOT_RIDER if mark.get("exclusion_reason") else ACTION_CONFIRM_CROSSING,
                                   reason=mark.get("exclusion_reason") or "人工逐人确认过线",
                                   position=(mark["x"], mark["y"]))
            self._marks.pop(item.item_id, None)
            self.save_drafts()
            self.refresh()
            self.hint.setText(f"已分别保存 {len(children)} 位过线者；未认号者各自保留在待处理里。")
        except (OSError, ValueError, RuntimeError) as error:
            self.save_drafts()
            self.hint.setText(f"逐人过线未全部保存，现有点数已保留：{error}")

    def _confirm_bunch_count(self, item: ProblemItem, counted: int) -> None:
        """Confirm the group as fully viewed and counted, or keep the count as progress.

        Enter on a group is the operator's statement that the whole group was on
        screen and nobody crossing in it was left uncounted; the gate still checks
        that every moment of it was actually viewed and that the count settles.
        """

        if self.workspace_dir is None or self.bunch_store is None:
            return
        from .bunch_review import load_context
        marks = [dict(mark) for mark in self._marks.get(item.item_id, [])]
        try:
            context = load_context(self.workspace_dir, item)
        except (ValueError, OSError) as error:
            self.hint.setText(f"本条集团打不开：{error}")
            return
        cached = self._inline_context()
        if cached is not None and cached.signature != context.signature:
            self.hint.setText("集团证据已变化，点数已保留；请重新打开核对。")
            self._group_context_id = None
            return
        seconds = max(0.0, time.monotonic() - self._shown_since)
        view_state = self._view_state(context)
        completed = False
        try:
            record = self.bunch_store.save(context, marks, seconds, completed=True, fully_viewed=True,
                                           viewed_ranges=view_state["ranges"])
            completed = bool(record.get("completed"))
        except (ValueError, OSError) as error:
            # The group cannot be closed on this count; the count is still worth
            # keeping, and the reason is the operator's next step.
            try:
                self.bunch_store.save(context, marks, seconds, completed=False,
                                      fully_viewed=view_state.get("fully_viewed", False),
                                      viewed_ranges=view_state["ranges"])
            except (ValueError, OSError) as saving:
                self.hint.setText(f"集团计数未保存：{saving}")
                return
            self.hint.setText(f"{error} 已先记住这 {counted} 人。")
        else:
            self.hint.setText(f"本集团已完整查看并完成人数对照：画面 {counted} 人 / 芯片 {len(context.chips)} 条。")
        if completed:
            # The group is closed and its people live in the record now; an unfinished
            # one keeps its count in hand, ready for the next rider pointed at.
            self._marks.pop(item.item_id, None)
        self._pending_action = ""
        self.save_drafts()
        self._load_bunch_store()
        if completed and self.bunch_store is not None:
            try:
                doubts = self.bunch_store.open_doubts(context)
                differences = {row.item_id for row in self.bunch_store.differences(item, self.workspace_dir)}
            except (ValueError, OSError, KeyError, TypeError):
                doubts, differences = [], set()
            counted_out = sum(row in differences for row in doubts)
            if counted_out:
                self.hint.setText(self.hint.text() + f" 人数差异 {counted_out} 条（多出的人、没对上人的芯片）"
                                  "已按时间单独列入待处理。")
            if len(doubts) > counted_out:
                self.hint.setText(self.hint.text() + f" 组内另有 {len(doubts) - counted_out} 条疑点，已单独列入待处理。")
        self._render()
        (self.setFocus if self._big_view else self.table.setFocus)()

    @property
    def big_view(self) -> bool:
        """True while the picture, not the queue, is the surface being worked on."""

        return self._big_view

    def toggle_big_view(self) -> None:
        """放大以后就是判读那一屏：左边大画面，右边点出来的人和本处芯片。

        画面在窗口里不动，只是队列让开；右边这一栏收起卡片图片，换成一张按过线
        时间排列的名单，号码和芯片时间同排，下面就是要按的那几个答案。
        """

        item = self.current_item()
        # A problem with no frame still opens: the operator pressed F to work on
        # this problem, so the key must answer.  The big screen says why it is empty
        # instead of leaving a dead press (operator: "F does not work").
        frame = dict(item.evidence.get("frame") or {}) if item is not None else {}
        self._big_view = not self._big_view
        self.detail_scroll.setMinimumWidth(404 if self._big_view else 260)
        self.table.setVisible(not self._big_view)
        # The header hands its height to the picture: a 4:3 frame can only grow with
        # the room above and below it, and on a laptop there is little to spare.
        self.header.setVisible(not self._big_view)
        self.big_title.setVisible(self._big_view)
        self.big_keys.setVisible(self._big_view)
        # 判读那一栏：卡片图片让位给统一的过线表。
        self.card.setVisible(not self._big_view)
        self.filmstrip.setVisible(not self._big_view)
        self.detail.setVisible(not self._big_view)
        for widget in (self.big_help, self.count_table, self.side_notice):
            widget.setVisible(self._big_view)
        self._sync_step_buttons()
        self._sync_marks()
        # The picture counts people while it is the big one, and only then.
        self.count_mode_changed.emit(self._big_view)
        if self._big_view:
            self.big_title.setText(f"{item.title} · {self.state(item)}" if item is not None else "录像判读 · 点人即可补记过线")
            self.decision_box.setVisible(item is not None)
            if frame and not self._browsing:
                self.jump_requested.emit(frame)
            elif item is not None and not frame:
                self.big_title.setText(
                    f"{item.title} · 此疑点没有原帧（录像里没有对应画面）：按芯片信息处理"
                )
            # The queue is gone from the screen, so the panel keeps the keys.
            self.setFocus()
        else:
            self.table.setFocus()
            self.hint.setText("已回到清单：1–9 处理，回车确认依据，F 看大图。")
        self._sync_person_buttons()
        # The workbench answers this by giving the picture the rest of its own room.
        self.big_view_changed.emit(self._big_view)

    def hideEvent(self, event) -> None:
        # Leaving the workbench must not leave the queue hidden behind a stale
        # big view, so the panes and the decisions come back before it goes away.
        if self._big_view:
            self.toggle_big_view()
        super().hideEvent(event)

    def _click_action(self, number: int) -> None:
        """Digits press the decision buttons of the row on screen, in the order shown."""

        buttons = [button for button in self.action_buttons
                   if button.isVisibleTo(self) and button.isEnabled()]
        if 1 <= number <= len(buttons):
            buttons[number - 1].click()

    def _filter_by_class(self, value: str) -> None:
        """A class tab narrows the queue to its own work; pressing it again lets go."""

        self._class_filter = "" if self._class_filter == value else value
        for key, button in self.class_buttons.items():
            button.setChecked(key == self._class_filter)
        self._render()

    def _queue_filter_changed(self) -> None:
        self._render()

    def class_counts(self) -> dict[str, int]:
        """Open work per class: each item is counted once, in the row the operator works in."""

        counts = dict.fromkeys(CLASS_ORDER, 0)
        for item in self.items:
            if not self._is_row(item):
                continue  # members are handled inside their batch or group
            state = self.state(item)
            if self._is_waiting_work(item, state) or self.is_pending_state(state):
                counts[problem_class(item)] += 1
        return counts

    def _is_waiting_work(self, item: ProblemItem, state: str = "") -> bool:
        """Open work that only waits for evidence: nobody's decision to make today."""

        state = state or self.state(item)
        if state == STATE_WAITING:
            return True
        if item.task_type != TYPE_EVIDENCE:
            return False
        return not (state.startswith((STATE_RESOLVED, STATE_HANDOFF))
                    or state in (STATE_UPSTREAM, STATE_EXCLUDED))

    def queue_text(self) -> str:
        """The one line the queue says about itself; the long count is its tooltip."""

        if self.payload is None and not self.items:
            return "尚未分析录像"
        counts = self.class_counts()
        parts = [f"{CLASS_TITLES[value]} {counts[value]}" for value in CLASS_ORDER if counts[value]]
        if parts:
            return "疑点 " + " · ".join(parts)
        cameras = (self.payload or {}).get("cameras", {})
        if (self.payload or {}).get("settings_stale"):
            return "配置已变更，等待重新分析"
        through = [value["analyzed_through_ms"] for value in cameras.values()
                   if value.get("analyzed_through_ms") and not value.get("error")]
        if any(value.get("error") for value in cameras.values()):
            return "部分机位等待录像或校时" if through else "等待录像 / 首人校时"
        if through:
            return f"已分析到 {format_chip_time(min(through)).split('.')[0]} · 当前无待处理疑点"
        if not cameras:
            return "芯片自检已更新 · 视频尚未分析"
        return "等待录像 / 首人校时"

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
        waiting = sum(1 for item in self.items if self._is_row(item) and self._is_waiting_work(item))
        unverifiable = int(summary.get("unverifiable", 0))
        if not waiting and not unverifiable:
            return "证据缺口 无"
        return f"证据缺口 {waiting} 条待证据" + (f" · {unverifiable} 人无录像可核" if unverifiable else "")

    def _set_coverage_alert(self, cameras) -> None:
        parts = []
        for camera, value in sorted(cameras.items(), key=lambda pair: str(pair[0])):
            ranges = value.get("missing_line_ranges") or []
            if ranges:
                shown = "、".join(f"{format_chip_time(left)[:8]}–{format_chip_time(right)[:8]}"
                                 for left, right in ranges[:3])
                parts.append(f"机位 {camera} 录像 {shown}" + (f" 等 {len(ranges)} 段" if len(ranges) > 3 else ""))
            elif value.get("missing_line_segments"):
                parts.append(f"机位 {camera} 有 {len(value['missing_line_segments'])} 段录像")
        self.coverage_alert.setText("⚠ 未检测，不能算核清：" + "；".join(parts)
                                    + " 没有终点线。点「终点线 / 模型」给这段录像画线。" if parts else "")
        self.coverage_alert.setVisible(bool(parts))

    def health_text(self) -> str:
        parts = [f"{name} {self.stage_health.get(name) or '—'}" for name in ("接收", "录制", "取样", "校时", "分析")]
        return " · ".join(parts) + (f" · {self.stage_detail}" if self.stage_detail else "")

    def clock_health(self) -> str:
        """Clock state per camera, kept apart from sampling and analysis failures."""

        texts = []
        for camera, value in sorted((self.payload or {}).get("cameras", {}).items(), key=lambda pair: str(pair[0])):
            calibration = value.get("calibration") or {}
            if not calibration:
                if value.get("calibration_pending"):
                    texts.append(f"机位 {camera} 视频已分析，待首人校时")
                else:
                    texts.append(f"机位 {camera} 等待校时")
                continue
            source = calibration.get("source_text", "自动校时")
            note = f"机位 {camera} {source} v{calibration.get('version')}"
            off = [anchor for anchor in calibration.get("anchors", ()) if not anchor.get("within_tolerance", True)]
            suffix = f"，{len(off)} 个人工锚点超差待核对" if off else (
                "，等待后续独立过线检查漂移" if calibration.get("source") == "manual_anchor" else "，锚点一致")
            texts.append(note + suffix)
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

    def _jump(self) -> None:
        item = self.current_item()
        if item is not None and item.evidence.get("frame"):
            self.jump_requested.emit(dict(item.evidence["frame"]))
            self.hint.setText("已跳到原帧；在机位画面中逐帧核对号码，再回到清单处理。")

    def _resolve_operator(self, action: str, bib: str = "") -> None:
        """One press on a row's own button is the decision.

        「是选手」is the same button whether the number was read or not: with a
        number typed it records the bib, empty it keeps the crossing and waits for
        the number.  A two-chip button carries the record it discards, so the
        label can name the one that stays.
        """

        if action == ACTION_RIDER:
            action = ACTION_RECORD_BIB if self.bib_input.text().strip() else ACTION_CONFIRM_CROSSING
        elif bib:
            self.bib_input.setText(bib)
        self._resolve(action)

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
        from .bunch_review import rider_count
        marks = self.current_marks()
        counted = rider_count(marks)
        counted_mark = next((mark for mark in marks if not mark.get("exclusion_reason")), None) if counted == 1 else None
        evidence_frame = (counted_mark or {}).get("frame")
        position = ((counted_mark["x"], counted_mark["y"]) if counted_mark is not None
                    else tuple(item.evidence.get("position", ())))
        if counted:
            # The count is what the operator saw on the frame: it becomes the reason
            # when they wrote none, and rides along when they did.
            note = f"画面点出 {counted} 人"
            reason = f"{reason}；{note}" if reason else note
        if action in (ACTION_RECORD_BIB, ACTION_CONFIRM_CROSSING) and len(marks) > 1:
            self._split_counted_crossings(item)
            return
        if action == ACTION_RECORD_BIB:
            if not bib:
                self.hint.setText("认号补录需要先填写号码牌上的号码。")
                return
            frame = evidence_frame or item.evidence.get("frame") or {}
            conflicts = same_frame_conflicts(frame, position, bib, self._claims(item.item_id)) if frame else []
            if conflicts and QMessageBox.question(
                self, "同一帧已有其他号码",
                f"这一帧同一次过线已对应 {'、'.join(conflicts)}。\n确认补录 {bib} 前请再看号码牌，是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        if action in ACTION_REASON_PRESETS and not reason:
            # The frame is what was looked at; the standing reason says which claim
            # that look makes.  It rides into the journal with the same press — one
            # look, one click, nothing to read and agree with afterwards.
            reason = ACTION_REASON_PRESETS[action]
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
                position=position,
                frame=evidence_frame,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"处理未保存：{error}")
            return
        self.bib_input.clear()
        self.reason_input.clear()
        if action != ACTION_CONFIRM_CROSSING:
            self._marks.pop(item.item_id, None)
        self._sync_marks()
        self._pending_action = ""
        self._held.pop(item.item_id, None)
        self._operator_items[item.item_id] = replace(item, evidence=dict(item.evidence, operator_scope=self._scope,
                                                                       operator_settings_token=operator_token)).to_payload()
        self.save_drafts()
        self.hint.setText("已确认过线，原帧和点数已保存；留在待处理里稍后认号。" if action == ACTION_CONFIRM_CROSSING
                          else f"已记录：{ACTION_LABELS.get(action, '重新处理')}。")
        self._render()
        # The list drops the decided row and lands back on the oldest one; keep the
        # queue under the fingers so the next row is one Down away.  In the big view
        # the queue is hidden, so the keys stay with the panel that is on screen.
        (self.setFocus if self._big_view else self.table.setFocus)()

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
            self.analysis_status = "等待终点线配置（在“终点线 / 模型”中配置）"
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

    def add_manual_problem(self, frame, reason="人工确认过线", *, recorder_ms=None, thumbnail=None):
        if not reason.strip() or not frame or self.problem_dir is None:
            return None
        evidence = {"frame": dict(frame), "operator_scope": self._scope}
        if recorder_ms is not None:
            evidence["recorder_ms"] = int(recorder_ms)
        item = ProblemItem("manual:" + uuid.uuid4().hex, KIND_MANUAL_REVIEW, CATEGORY_VIDEO,
                           reason.strip() + "；芯片状态待核实", evidence=evidence)
        if thumbnail is not None and not thumbnail.isNull():
            from .crossing_cards import safe_name
            card = self.problem_dir / "cards" / f"{safe_name(item.item_id)}.jpg"
            card.parent.mkdir(parents=True, exist_ok=True)
            if thumbnail.scaled(360, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(card), "JPG", 85):
                evidence["card"] = f"cards/{card.name}"
        path = self.problem_dir / "manual_problems.json"
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        items.append(item.to_payload())
        self.problem_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        self._class_filter = ""
        self.queue_filter.blockSignals(True)
        self.queue_filter.setCurrentIndex(0)
        self.queue_filter.blockSignals(False)
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
            if message.get("stage") == "清单已发布":
                self.refresh()
                self.analysis_finished.emit()
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
        self._next_analysis = time.monotonic() + (5 if _args and _args[0] != 0 else 0.25)
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
