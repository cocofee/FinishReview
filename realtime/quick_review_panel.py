"""One chronological time/bib table beside the original recording."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QAbstractSpinBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from .auto_label import WAVE_REACH_MS, label_wave, people_on_line
from .arrival_review import (
    candidate_binding, event_binding,
)
from .extra_person import fit_wave
from .bunch_review import add_viewed, is_excluded, mark_on_frame, rider_count
from .crossing_time import item_crossing_time, recorder_crossing_time, workspace_clocks
from .problem_items import (
    CATEGORY_VIDEO, KIND_CHIP_NO_CROSSING, STATUS_EXCLUDED,
    ProblemItem,
)
from .problem_panel import ProblemListPanel
from .wave_slots import WaveSlotRow
from .race_strip import (RaceStripPanel, earlier_arrival, first_crossing, frame_at, line_next_entry,
                         line_arrivals)
from .quick_review_common import (
    ArrivalRow,
    CLOSE_BEHIND_MS,
    EXTRA_CHIP,
    KEEP,
    LOCAL_CHIP_EXCLUSION,
    RECORD_BIB,
    REVIEW_STALL_MS,
    RUN_FAST_MIN_MS,
    RUN_FAST_SPEED,
    RUN_NEAR_MS,
    RUN_SLOW_MS,
    RUN_STALE_MS,
    RUN_STOP_LEAD_MS,
    RUN_TAIL_MS,
    SAME_BODY_MS,
    SWEEP_REACH_MS,
    WAVE_GAP_MS,
    WAVE_NO_PERSON,
    WAVE_VIEW_MARGIN_MS,
    WAVE_VIEW_REACH_MS,
    WavePlan,
    row_moment,
)
from .quick_review_rows import QuickReviewRowsMixin
from .quick_review_display import QuickReviewDisplayMixin
from .quick_review_waves import QuickReviewWavesMixin
from .quick_review_play import QuickReviewPlayMixin
from .quick_review_picture import QuickReviewPictureMixin
from .quick_review_judgments import QuickReviewJudgmentsMixin
from .quick_review_export import QuickReviewExportMixin


class QuickReviewPanel(
    QuickReviewRowsMixin,
    QuickReviewDisplayMixin,
    QuickReviewWavesMixin,
    QuickReviewPlayMixin,
    QuickReviewPictureMixin,
    QuickReviewJudgmentsMixin,
    QuickReviewExportMixin,
    ProblemListPanel,
):
    quick_review = True
    confirmation_preparing = pyqtSignal()
    candidate_preparing = pyqtSignal()
    sampling_configured = pyqtSignal()
    sampling_preparing = pyqtSignal()
    # A chip record the operator wants gone (spare chip, misread); the window asks why.
    delete_requested = pyqtSignal(str)
    # 校时: the window learns the video offset from the current frame and a chip record.
    calibration_requested = pyqtSignal()
    run_speed = pyqtSignal(float)               # how fast the run plays (RUN_FAST_SPEED)
    # True: play the recording on from the frame shown; False: hold it (see RUN_NEAR_MS).
    run_requested = pyqtSignal(bool)

    def __init__(self, parent=None):
        self._quick_ready = False
        self._active_item = None
        self._arrival_rows = []
        self._arrival_store = None
        self._arrival_scope = ("", "")
        self._selected_key = None
        # The ruling just saved, until the operator picks another row; undo may
        # reach it after the automatic step to the next row, and nothing older.
        self._last_saved_key = None
        # Chip rows grouped into waves (see WAVE_GAP_MS), and which wave each
        # row and each counting picture belongs to; rebuilt on every render.
        self._waves = []
        self._wave_of = {}
        self._candidate_wave = {}
        self._decided = {}
        self._plan_cache = None
        self._quick_render_signature = None
        self._simultaneous = Counter()
        self._table_builds = 0  # times the table was filled again (a click fills it once, not twice)
        self._frame_provider = lambda: None
        self._shown_ms = None
        self._landing_pending = None
        # A 不是运动员 (1, or the run from the right click) still playing the unseen part of its rows
        # before excluding them.
        self._auto_review = None
        # The chip-clock moment the recording is playing on to after a click,
        # whether that is the wave's tail (nobody left to count before it), and
        # the moment of the click a run is still to start from.
        self._run_to = None
        self._run_fast, self._run_swept = False, False
        self._run_between = None
        self._run_tail = False
        self._run_from = None
        self._clicked_at = None  # chip time of the person just clicked (see CLOSE_BEHIND_MS)
        self._close_behind_ms = CLOSE_BEHIND_MS
        self._run_counted = None  # mark index a run to someone already clicked lands on
        self._offset_cache = None  # (key, _run_offset result): one Space asks for it several times
        # 只看要处理的: the table shows only the rows still to settle, and those
        # settled for the timing system (补录, 多余芯片).
        self._attention_only = False
        self._show_excluded = False
        self._show_unverified = False
        self._unverified_items = frozenset()
        self._unverified_groups = 0
        # True while a row is opened whose picture the panel itself places (or
        # keeps): the window must not reopen the item's stored frame over it.
        self.holds_picture = False
        super().__init__(parent)
        self.jump_requested.connect(self._forget_run)  # a jump elsewhere ends any run
        self.header.hide()
        self.detail_scroll.hide()
        self.timeline.hide()
        self.timeline_help.hide()
        self.table.currentCellChanged.disconnect()
        # 可能多人: crossing (item id, moment) -> when someone else came onto the
        # line before it (None: nobody), read off the strip once (earlier_arrival).
        self._earlier = {}
        self._metadata_provider = lambda: None  # CycleRace's race metadata: the groups' target laps
        self._target_laps = {}  # group id -> target laps of this race (CycleRace's metadata), when sent
        self._short_bibs = ()  # riders whose last read is short of the target, once the group has a finisher
        # 自动对上 (auto_label): chip row key -> the person the analysis saw cross
        # for it, on waves nobody has counted yet.  Shown only, never saved.
        self._auto_labels = {}
        self._auto_reasons = {}  # wave index -> why it was not paired (shown on its rows)
        self._auto_landing_key = None  # only keep a paired wave after opening its bound evidence
        self._earlier_shown = frozenset()  # the 可能多人 the table shows
        self._near_counted_times = ()  # their moments: hidden from the table, still a doubt for 自动对上
        self._near_counted_items = frozenset()  # item ids of crossings within COUNTED_REACH_MS of someone counted
        self._strip_end = (None, 0.0)  # (pieces, the strip's last chip-clock moment)
        self._earlier_timer = QTimer(self)
        self._earlier_timer.setSingleShot(True)
        self._earlier_timer.setInterval(30)
        self._earlier_timer.timeout.connect(self._read_earlier)
        self._review_timer = QTimer(self)
        self._review_timer.setSingleShot(True)
        self._review_timer.setInterval(REVIEW_STALL_MS)
        self._review_timer.timeout.connect(
            lambda: self._stop_auto_review("自动补看没有画面了（录像缺口或已到末尾），这一行未排除；请手动看完再按 1 排除。"))
        self.table.cellDoubleClicked.disconnect()
        self.table.cellClicked.connect(self._arrival_clicked)
        self.table.cellDoubleClicked.connect(self._arrival_clicked)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["顺序", "号码", "时间", "状态", "组别", "圈数"])
        self.table.horizontalHeaderItem(2).setToolTip(
            "校时后的芯片域时间；悬浮单元格可看录像原时刻"
        )
        for column, width in enumerate((42, 66, 137, 72)):
            self.table.setColumnWidth(column, width)
        # The group column only appears when the race has more than one group.
        self.table.setColumnHidden(4, True)
        # The lap column only appears when CycleRace sends a group's target laps (多圈).
        self.table.setColumnWidth(5, 46)
        self.table.setColumnHidden(5, True)
        self.table.setMinimumWidth(340)
        self.table.setMaximumWidth(440)
        self.table.setToolTip("点一行看这一刻；有人压线就在画面上点他，滚轮前后一帧；选中后按 1 不是运动员、2 双芯片、3 补录号码、4 撤销（右键同样）")
        # Everything a row still needs is settled from the row itself (10-29),
        # by 1 / 2 / 3 / 4 or its right click: the picture only counts people.
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        # Search sits on top of the table: a bib or a name, Enter jumps to the row.
        column = self.table_column = QWidget(self.splitter)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(3)
        self.search = QLineEdit(column)
        self.search.setPlaceholderText("搜号码 / 姓名，回车跳到")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._find)
        self.attention_button = QPushButton("只看要处理的", column)
        self.attention_button.setCheckable(True)
        self.attention_button.toggled.connect(self._set_attention_only)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.attention_button)
        column_layout.addLayout(search_row)
        self.splitter.insertWidget(0, column)
        column_layout.addWidget(self.table)
        self.quick_toolbar = QWidget(self)
        toolbar_layout = QVBoxLayout(self.quick_toolbar)
        toolbar_layout.setContentsMargins(2, 3, 2, 3)
        toolbar_layout.setSpacing(3)
        self.status_widget = QWidget(self.quick_toolbar)
        self.status_bar = QHBoxLayout(self.status_widget)
        self.status_bar.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        toolbar_layout.addLayout(bar)
        bar.addWidget(self.status_widget, 1)
        self.quick_title = QLabel("到达记录", column)
        column_layout.insertWidget(0, self.quick_title)
        # 校时 is the first thing done at every recording: until it is done every
        # chip mark sits seconds away from its rider (11-03: 3.2 s).  It used to
        # hide under the picture, behind 查找原录像.
        self.calibrate_button = QPushButton("先校时", self.quick_toolbar)
        self.calibrate_button.clicked.connect(self.calibration_requested.emit)
        bar.addWidget(self.calibrate_button)
        self._calibration_needed = None
        self.row_action_buttons = {}
        for key, label in ((Qt.Key_1, "1 非运动员"), (Qt.Key_2, "2 双芯片"), (Qt.Key_3, "3 补录")):
            button = QPushButton(label, self.quick_toolbar)
            button.setToolTip("处理左侧选中行")
            button.clicked.connect(lambda _checked=False, value=key: self._row_key(value))
            self.row_action_buttons[key] = button
            bar.addWidget(button)
        self.defer_button = QPushButton("暂放", self.quick_toolbar)
        self.confirm_arrival_button = QPushButton("确认过线", self.quick_toolbar)
        self.confirm_arrival_button.setToolTip(
            "先选左侧运动员，再将画面停在其前轮压终点线的一帧，最后点击确认过线"
        )
        self.quick_undo_button = QPushButton("4 撤销", self.quick_toolbar)
        for button in (self.defer_button, self.confirm_arrival_button, self.quick_undo_button):
            bar.addWidget(button)
        self.confirm_arrival_button.hide()
        self.defer_button.hide()
        self.quick_undo_button.setToolTip("4 或退格：撤销所选这一行的判定；刚判完自动跳到下一行时，撤销刚才那一条")
        self.defer_button.clicked.connect(lambda: self._classify("held"))
        self.confirm_arrival_button.clicked.connect(self._confirm_selected_arrival)
        self.quick_undo_button.clicked.connect(self._undo_arrival)
        more = self.more_button = QPushButton("更多", self.quick_toolbar)
        menu = QMenu(more)
        menu.addAction("终点线 / 模型", self._configure_sampling)
        self.show_excluded_action = menu.addAction("显示已排除")
        self.show_excluded_action.setCheckable(True)
        self.show_excluded_action.toggled.connect(self._set_show_excluded)
        self.show_unverified_action = menu.addAction("显示待核画面（不计到达）")
        self.show_unverified_action.setCheckable(True)
        self.show_unverified_action.toggled.connect(self._set_show_unverified)
        menu.addSeparator()
        menu.addAction("分析录像", self._toggle_analysis)
        menu.addAction("导出给计时系统（补录 / 建议删除）", self.export)
        menu.addAction("删除选中的芯片记录…", self._request_delete)
        menu.addSeparator()
        menu.addAction("清空试验数据", self.clear_data_requested.emit)
        more.setMenu(menu)
        bar.addWidget(more)
        self.layout().insertWidget(0, self.quick_toolbar)
        # Judging actions stay visible; routine pipeline details belong in hover text.
        self.finish_line_button = QPushButton("设置终点线", self.quick_toolbar)
        self.finish_line_button.setStyleSheet("color:#9a5b12; font-weight:600;")
        self.finish_line_button.setToolTip(
            "绘制本机位终点线，选择本机 YOLOX ONNX 模型并试运行；只分析已封存录像"
        )
        self.finish_line_button.clicked.connect(self._configure_sampling)
        self.finish_line_status = QLabel("未设终点线", self.quick_toolbar)
        self.confirmation_status = QLabel("", self.quick_toolbar)
        self.confirmation_status.setStyleSheet("color:#374151;")
        self.confirmation_status.setMinimumWidth(0)
        self.confirmation_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_bar.addWidget(self.confirmation_status, 1)
        self.finish_line_status.hide()  # Missing configuration is an actionable button, not duplicate text.
        self.lap_status = QLabel("", self.quick_toolbar)
        self.lap_status.setStyleSheet("color:#9a5b12; font-weight:600;")
        self.status_bar.addWidget(self.lap_status)
        bar.addWidget(self.finish_line_button)
        self.race_strip = RaceStripPanel(self)
        self.race_strip.seek_requested.connect(self._seek_race_strip)
        self.race_strip.ready.connect(self._strip_ready)
        self.layout().insertWidget(1, self.race_strip)
        # The head count; the window puts it above the picture, in the place of 已过 N 人 (see wave_slots).
        self.wave_slots = WaveSlotRow(self)
        self.wave_slots.mark_clicked.connect(self._slot_clicked)
        self._slot_cache, self.slot_cache_hits = None, 0
        self._target_row_state, self._target_cells = None, []
        self.layout().insertWidget(2, self.wave_slots)
        self._quick_ready = True
        QApplication.instance().installEventFilter(self)
        self._render()

    def eventFilter(self, watched, event):
        if (self._quick_ready and watched is self.race_strip.view and event.type() == QEvent.KeyPress
                and self.isVisible() and self.race_strip.view.navigate_key(event)):
            return True
        if (self._quick_ready and event.type() == QEvent.KeyPress and self.isVisible()
                and isinstance(watched, QWidget) and watched.window() is self.window()
                and not isinstance(watched, (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit))
                and self._queue_key(event)):
            return True
        return super().eventFilter(watched, event)

    def set_frame_provider(self, provider):
        self._frame_provider = provider

    def set_metadata_provider(self, provider):
        self._metadata_provider = provider

    def set_workspace(self, workspace_dir):
        if self._quick_ready:
            self.race_strip.load(workspace_dir)
        if Path(workspace_dir) != self.workspace_dir:
            self._active_item = None
            self._selected_key = None
            self._last_saved_key = None
            self._arrival_store = None
            self._arrival_rows = []
            self._quick_render_signature = None
        super().set_workspace(workspace_dir)

    def reset_data(self):
        self._active_item = None
        self._selected_key = None
        self._last_saved_key = None
        self._arrival_store = None
        self._arrival_rows = []
        self._quick_render_signature = None
        super().reset_data()
        self._render()

    def set_scope(self, race_id, stage_id):
        if (str(race_id or ""), str(stage_id or "")) != self._scope:
            self._active_item = None
            self._selected_key = None
            self._last_saved_key = None
            self._quick_render_signature = None
        super().set_scope(race_id, stage_id)

    def current_item(self):
        if not self._quick_ready:
            return super().current_item()
        return self._active_item

    def _show(self, row):
        if not self._quick_ready:
            super()._show(row)

    def _build_rows(self):
        events = self._events()
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        rows, represented = [], set()
        # Parents hold the candidate picture; their members are individual selectable events.
        items = sorted(self.items, key=lambda item: bool(item.grouped))
        by_id = {item.item_id: item for item in items}
        for raw in items:
            # Chip-only placeholders are rebuilt from the live event provider.
            # The legacy refresh retains the selected item as superseded; that
            # retained placeholder must not shadow the current official event.
            if raw.item_id.startswith("arrival:") and raw.evidence.get("superseded"):
                continue
            item = raw
            if (item.status == STATUS_EXCLUDED and (item.evidence.get("bike") or {}).get("decision") == "exclude"):
                item = replace(item, status="open", status_note="")
            members = list(item.evidence.get("batch_members") or ())
            pairs = [(str(m.get("event_id", "")), str(m.get("bib", ""))) for m in members]
            pairs += list(zip(item.event_ids, item.bibs))
            if item.grouped in by_id:
                item = by_id[item.grouped]
            evidence_frame = item.evidence.get("frame") or {}
            raw_recorder_ms = item.evidence.get("recorder_ms", evidence_frame.get("recorder_ms"))
            if not pairs:
                if raw.grouped:
                    continue
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("candidate:" + item.item_id, item,
                                       time_ms=crossing.value_ms if crossing else None,
                                       recorder_ms=raw_recorder_ms,
                                       reference=crossing is None or crossing.source in ("expected", "chip")))
            for event_id, bib in pairs:
                # Every lap keeps its chip row, without duplicating the bound picture.
                if not event_id or event_id in represented:
                    continue
                represented.add(event_id)
                event = events.get(event_id)
                if event is not None:
                    bib = event.bib
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("event:" + event_id, item, bib, event,
                                       int(event.timeline_timestamp_ms) if event else
                                       crossing.value_ms if crossing else None,
                                       recorder_ms=raw_recorder_ms))
        for event_id, event in events.items():
            if event_id in represented or not event.is_active:
                continue
            item = ProblemItem("arrival:" + event_id, KIND_CHIP_NO_CROSSING, CATEGORY_VIDEO,
                               "", chip_time_ms=int(event.timeline_timestamp_ms),
                               bibs=(event.bib,), event_ids=(event_id,))
            rows.append(ArrivalRow("event:" + event_id, item, event.bib, event,
                                   int(event.timeline_timestamp_ms)))
        anchors = {row.item.item_id: row.item for row in rows}
        for row in rows:
            if self._arrival_store is not None and row.event is None:
                entered = self._arrival_store.latest(self._arrival_scope, "bib:" + row.item.item_id,
                                                     candidate_binding(row.item))
                if entered is not None and entered["action"] == "held" and entered.get("reason") == RECORD_BIB:
                    row.entered, row.bib = entered, entered["bib"]
            if self._arrival_store is not None and row.event is not None:
                previous = self._arrival_store.latest(self._arrival_scope, row.key)
                if previous is not None:
                    anchor = anchors.get(previous.get("candidate_id"))
                    if (previous["action"] == "held" and previous.get("reason") == LOCAL_CHIP_EXCLUSION
                            and previous["binding"] == event_binding(row.event)):
                        row.note = "已排除"
                        continue
                    if (previous["action"] == "held" and previous.get("reason") == EXTRA_CHIP
                            and previous["binding"] == event_binding(row.event)):
                        row.note = "多余芯片"  # needs no picture of its own: nobody crossed for it
                        row.carrier = str(previous.get("carrier_bib") or "")
                        continue
                    row.stale = (previous["binding"] != event_binding(row.event)
                                 or anchor is None
                                 or previous.get("candidate_binding") != candidate_binding(anchor))
                    if (not row.stale and previous["action"] == "confirm" and row.event.is_active
                            and not row.item.evidence.get("configuration_stale")):
                        row.decision = previous
                        row.item = anchor
                        frame = previous.get("frame") or {}
                        raw_recorder_ms = frame.get("recorder_ms")
                        row.recorder_ms = raw_recorder_ms
                        # New confirmations store the already-calibrated crossing
                        # time; older ones stored the raw recorder time. Convert
                        # only the latter, so a live calibration is never applied
                        # twice.
                        stored_ms = previous["crossing_ms"]
                        if raw_recorder_ms is None or int(stored_ms) == int(raw_recorder_ms):
                            crossing = recorder_crossing_time(
                                stored_ms, frame, self.workspace_dir, clocks=clocks,
                            )
                            row.time_ms = crossing.value_ms if crossing is not None else stored_ms
                        else:
                            row.time_ms = stored_ms
                        row.reference = False
                    elif not row.stale and previous.get("reason") == WAVE_NO_PERSON:
                        row.note = "疑似双芯片"
        assigned = Counter(row.decision["candidate_id"] for row in rows if row.decision)
        counting = set(self._wave_candidates().values())
        moments = self._counted_moments(counting)
        self._unverified_items = frozenset(row.item.item_id for row in rows
                                          if row.event is None and self._unverified_picture(row.item))
        rows = self._fold_unverified(rows, assigned, counting)
        self._unverified_groups = sum(self._row_state(row) == "画面待核" for row in rows)
        rows = [row for row in rows if row.event is not None or self._candidate_shown(row.item, assigned, counting)]
        # 11-15: a crossing the analysis saw within COUNTED_REACH_MS of someone
        # counted is no longer a row (it was 已点附近: "这样会造成困惑").  People
        # are counted one by one now and Space stops on whoever comes onto the
        # line right behind (line_next_entry).  Nothing on disk changes.
        near = frozenset(row.item.item_id for row in rows
                         if row.event is None and self._near_counted_moment(row.item, assigned, moments))
        # Still a doubt for 自动对上: a wave with such a crossing is left to hand counting.
        self._near_counted_times = tuple(row.time_ms for row in rows
                                         if row.item.item_id in near and row.time_ms is not None)
        self._near_counted_items = frozenset()
        rows = self._without_repeats(rows)
        # Folded first, then hidden: the later pieces of a hidden crossing go with
        # it instead of standing as rows of their own beside the counted rider.
        rows = self._fold_fragments(rows, assigned)
        rows = [row for row in rows if row.item.item_id not in near or row.event is not None]
        # Arrival order is chip time.  Not the order the records came in: a chip
        # added later (古浪 6 号) sat after the whole wave and gave its rider's
        # click to the next bib.  A corrected video time never moves a chip.
        chips = sorted((row for row in rows if row.event is not None), key=lambda row: (
            int(row.event.timeline_timestamp_ms), row.event.sequence, row.key))
        # A crossing without a chip is read in arrival order too: it sits at its
        # own time between the chips, never collected after the last finisher.
        others = sorted((row for row in rows if row.event is None), key=lambda row: (
            row.time_ms if row.time_ms is not None else 2**62, row.key))
        merged = []
        for chip in chips:
            while others and others[0].time_ms is not None and others[0].time_ms < chip.event.timeline_timestamp_ms:
                merged.append(others.pop(0))
            merged.append(chip)
        return merged + others

    def _earlier_arrival(self, row, read=True):
        """When someone else came onto the line up to 2 s before this crossing without a chip (可能多人).

        None if nobody did, or not known yet: READ reads it off the strip now,
        once the strip holds the moment (else the table's pass reads it later).
        """
        if row is None or row.event is not None or row.time_ms is None or self._row_state(row) != "待定":
            return None
        key = (row.item.item_id, int(row.time_ms))
        if key not in self._earlier and read:
            pieces = self.race_strip.view.pieces
            if pieces and self._strip_end[0] is not pieces:
                self._strip_end = (pieces, max(piece.chip_end for piece in pieces))
            if not pieces or self._strip_end[1] < row.time_ms + 1500:
                return None
            own = {piece.key for piece in row.fragments}
            others = [int(other.event.timeline_timestamp_ms) if other.event is not None else other.time_ms
                      for other in self._arrival_rows
                      if other is not row and other.key not in own and other.mark is None
                      and other.note != "已排除"
                      and (other.event is None or other.event.is_active)
                      and (other.time_ms is not None or other.event is not None)]
            others += [piece.time_ms for other in self._arrival_rows if other is not row
                       for piece in other.fragments if piece.time_ms is not None]
            self._earlier[key] = earlier_arrival(pieces, self.race_strip.view.cache, int(row.time_ms), others)
            if self._earlier[key] is not None and not self._earlier_timer.isActive():
                self._earlier_timer.start()  # read on opening the row: the table shows it too
        return self._earlier.get(key)

    def _auto_label_waves(self, plans):
        """自动对上: pair each uncounted wave's chips with the people the analysis saw cross for it.

        Only where every condition of auto_label.label_wave holds: as many
        people as chips, timed, apart enough, and no 可能多人
        or 已点附近 close by.  Any doubt leaves the whole wave to hand.  Nothing
        is saved until Enter explicitly accepts the bound evidence; its automatic
        origin remains distinct from manual clicks. 4 withdraws the proposal.
        """
        self._auto_labels, self._auto_reasons = {}, {}
        if not self.workspace_dir or not self._waves:
            return
        people = people_on_line(self.workspace_dir, int(self.race_strip.camera or 1))
        if not people:
            return
        by_key = {person.key: person for person in people}
        withdrawn = set(self._arrival_drafts.get("auto_withdrawn", ()))
        loose = [row for row in self._arrival_rows if row.event is None and row.time_ms is not None]
        for index, wave in enumerate(self._waves):
            plan = plans.get(index)
            if plan is not None and (plan.marks or plan.split):
                continue  # counted by hand
            if self._auto_key(wave) in withdrawn:
                self._auto_reasons[index] = ("已按 4 撤回",)
                continue
            if any(row.decision or row.note or row.stale or row.entered for row in wave):
                continue
            stamps = [int(row.event.timeline_timestamp_ms) for row in wave]
            start, end = min(stamps) - WAVE_REACH_MS, max(stamps) + WAVE_REACH_MS
            near = [row for row in loose if start <= row.time_ms <= end]
            match = label_wave(
                [(row.key, stamp) for row, stamp in zip(wave, stamps)], people, clock_ok=True,
                maybe_several=any(self._earlier_arrival(row, read=False) is not None for row in near),
                near_counted=any(start <= moment <= end for moment in self._near_counted_times))
            if match.reasons:
                self._auto_reasons[index] = match.reasons
                continue
            for chip_key, person_key in match.pairs:
                self._auto_labels[chip_key] = by_key[person_key]

    def _with_extra_rows(self, rows, plans):
        """Everyone counted beyond a wave's chips gets a row of their own, at their own time.

        So a 多 1 can be found: the row opens the frame they were counted on,
        where 4 (无芯片), 3 (非运动员) or a right click settles them.
        """
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        extra = []
        for plan in plans.values():
            for index in sorted(plan.extra):
                mark = plan.marks[index]
                crossing = (recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {}, self.workspace_dir,
                                                   clocks=clocks) if self.workspace_dir else None)
                extra.append(ArrivalRow(
                    f"extra:{plan.item.item_id}:{mark['recorder_ms']}:{mark['x']}:{mark['y']}", plan.item,
                    time_ms=crossing.value_ms if crossing else int(mark["recorder_ms"]),
                    recorder_ms=int(mark["recorder_ms"]), note="多出", mark=mark))
        if not extra:
            return rows
        extra.sort(key=lambda row: row.time_ms)
        merged = []
        for row in rows:
            moment = row_moment(row)
            while extra and moment is not None and extra[0].time_ms < moment:
                merged.append(extra.pop(0))
            merged.append(row)
        return merged + extra

    def queue_text(self):
        if self._unverified_items and not any(row.event is not None or not self._unverified_picture(row.item)
                                              for row in self._arrival_rows):
            return f"尚未确认目标穿越 · 待核画面 {self._unverified_groups} 组（{len(self._unverified_items)} 段，不计到达）"
        return super().queue_text()

    def _landing(self, row, keep=True):
        """Where the picture goes when this row is opened; the one rule for every way in.

        A decided row uses its saved frame. A proposed automatic wave opens its
        first paired person's exact frame; member changes keep the picture.
        Unpaired chips use the strip's first crossing without playback padding,
        falling back to chip time. Video-only passages use their small lead, or
        the earlier arrival when several people may share one crossing.
        KEEP means stay; None means the row's stored frame or chip reference.
        """
        self._landing_pending = None
        if row.decision and row.decision.get("frame"):
            return dict(row.decision["frame"])
        if row.mark is not None:
            return dict(row.mark.get("frame") or {}) or None
        stamps = [int(chip.event.timeline_timestamp_ms) for chip in self._row_wave(row) or ()
                  if chip.event is not None and chip.event.is_active]
        if not stamps:
            return self._passage_start(row)
        if not self._quick_ready:
            return None
        first, last = min(stamps), max(stamps)
        wave = self._row_wave(row) or ()
        paired = [self._auto_labels[chip.key] for chip in wave if chip.key in self._auto_labels]
        visible = self._shown_ms is not None and first - WAVE_GAP_MS / 2 <= self._shown_ms <= last + WAVE_GAP_MS / 2
        if paired and len(paired) == len(wave):
            binding = (str(self.workspace_dir), self._arrival_scope,
                       tuple((chip.key, self._auto_labels[chip.key].key) for chip in wave))
            if keep and visible and self._auto_landing_key == binding:
                return KEEP
            self._auto_landing_key = binding
            # Same evidence as the labels and acceptance, not another strip search.
            person = min(paired, key=lambda entry: entry.chip_ms)
            return dict(person.frame, reference_only=True, auto_pair=True)
        if keep and visible:
            return KEEP
        if not self.race_strip.view.pieces:
            self._landing_pending = row.key  # unpaired reference awaits the strip
            return None
        pieces, cache = self.race_strip.view.pieces, self.race_strip.view.cache
        moment = first_crossing(pieces, cache, first)
        moment = first if moment is None else moment
        frame = frame_at(pieces, moment - WAVE_VIEW_MARGIN_MS) or frame_at(pieces, moment)
        return dict(frame, reference_only=True) if frame is not None else None

    def _passage_start(self, row):
        """Where a crossing without a chip is looked at from (_passage_lead), on its own camera.

        Without a strip aligned to the chips (a race without chips: 现代五项
        10-29) it is that far back from the row's own frame, maybe before its
        file starts: the window opens the archive or the piece that holds it.
        """
        if (not self._quick_ready or row.event is not None or row.time_ms is None
                or self._row_state(row) not in ("待定", "画面待核")):
            return None
        own = row.item.evidence.get("frame") or {}
        camera = int(own.get("camera_index") or 1)
        pieces = self.race_strip.view.pieces
        self._earlier_arrival(row)
        lead = self._passage_lead(row)
        frame = frame_at(pieces, row.time_ms - lead) if pieces else None
        if frame is not None and int(frame.get("camera_index") or 1) == camera:
            return dict(frame, reference_only=True)
        if not own.get("video_path") or own.get("position_ms") is None:
            return None
        return {"camera_index": camera, "segment_id": own.get("segment_id"), "video_path": own["video_path"],
                "position_ms": int(own["position_ms"]) - lead, "frame_index": None,
                "reference_only": True}

    def _sync_timeline(self):
        if not self._quick_ready:
            return super()._sync_timeline()
        # The whole-race picture replaces the short group ruler here.  Keep
        # its current moment for N / Shift+N, without loading a second image.
        self.timeline.hide()
        self.timeline_help.hide()

    def _wave_plan(self, item=None):
        """Line up the people counted on a wave's picture with the wave's chips.

        None when the picture on screen is not a wave's.  The people counted
        here take the wave's chips in table order, one each, in the order they
        cross, whether saved yet or not: a click before someone shifts the bibs
        after it.  A person marked 无芯片 takes none, people beyond the last chip
        are 多 1, 多 2 ...  Only a chip confirmed elsewhere (another picture, or
        without a spot) keeps its bib out of the count.
        """
        item = item or self.current_item()
        index = self._candidate_wave.get(item.item_id) if item is not None else None
        if index is None:
            return None
        chips = self._waves[index]
        marks = self._marks.get(item.item_id, [])
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        key = (item.item_id, id(chips), len(self.items), id(clocks),
               tuple((mark["recorder_ms"], mark["x"], mark["y"], bool(mark.get("no_chip")),
                      mark.get("exclusion_reason", ""), (mark.get("frame") or {}).get("session_id", ""))
                     for mark in marks))
        if self._plan_cache is not None and self._plan_cache[0] == key and self._plan_cache[1].marks is marks:
            return self._plan_cache[1]
        plan = WavePlan(item, chips, marks)
        plan.extra_chips = [row for row in chips if row.note == "多余芯片"]
        matched = set()
        for i, mark in enumerate(marks):
            row = None if is_excluded(mark) else self._decided_row(mark, item)
            if row is not None and not self._counted_here(row, item):
                plan.labels[i] = row.bib
                matched.add(row.key)
        free = [row for row in chips if (row.decision is None or self._counted_here(row, item))
                and row.note != "多余芯片"]
        def crossing_order(index):
            mark = marks[index]
            crossing = recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {},
                                              self.workspace_dir, clocks=clocks)
            return (crossing.value_ms if crossing is not None else int(mark["recorder_ms"]), index)

        ordered = sorted((i for i, mark in enumerate(marks) if i not in plan.labels and not is_excluded(mark)),
                         key=crossing_order)
        counting = [i for i in ordered if not marks[i].get("no_chip")]

        def chip_ms(row):
            # The chip's own time.  A row already saved from a click carries that
            # click's moment as its time_ms; fitting against it would just give
            # back the pairing saved click by click (11-15 盲测, 261 号: the one
            # without a chip always ended up as the wave's last person).
            return int(row.event.timeline_timestamp_ms)  # every row of a wave has its chip

        fit = None
        if len(counting) > len(free) and free:
            # 11-14 (古浪 6 号): the one without a chip is where the clicks shift
            # onto the next chip's time, not the last person of the wave.
            free.sort(key=chip_ms)
            fit = fit_wave([crossing_order(i)[0] for i in counting], [chip_ms(row) for row in free])
        if fit is not None:
            chosen = dict(zip(counting, fit.chip_of_click))
            for i in ordered:
                if marks[i].get("no_chip"):
                    plan.no_chip.append(i)
                    plan.labels[i] = "无芯片"
                elif chosen[i] is not None:
                    plan.assigned.append((i, free[chosen[i]]))
                    plan.labels[i] = free[chosen[i]].bib
                else:
                    plan.extra.append(i)
                    plan.labels[i] = f"多 {len(plan.extra)}"
            for extra in fit.extras:
                if not extra.sure:
                    plan.near[counting[extra.click]] = tuple(counting[c] for c in extra.candidates)
            ordered, free = [], []
        for i in ordered:
            if marks[i].get("no_chip"):
                plan.no_chip.append(i)
                plan.labels[i] = "无芯片"
            elif free:
                row = free.pop(0)
                plan.assigned.append((i, row))
                plan.labels[i] = row.bib
            else:
                plan.extra.append(i)
                plan.labels[i] = f"多 {len(plan.extra)}"
        plan.unmatched = free
        split = []  # a person flagged twice by an older build counts once (see _without_repeats)
        for other in self.items:
            if other.evidence.get("wave_parent") == item.item_id:
                for mark in self._marks.get(other.item_id, ()):
                    if not is_excluded(mark) and not any(
                            mark_on_frame(mark, seen.get("frame") or {}, seen["recorder_ms"])
                            and (mark["x"] - seen["x"]) ** 2 + (mark["y"] - seen["y"]) ** 2 < .02 ** 2
                            for seen in split):
                        split.append(mark)
        plan.split = len(split)
        # People confirmed elsewhere, or without a spot on this picture, count too.
        plan.people = (rider_count(marks) + plan.split
                       + sum(row.decision is not None and row.key not in matched
                             and not self._counted_here(row, item) for row in chips))
        self._plan_cache = (key, plan)
        return plan

    def record_viewed_frame(self, frame, recorder_ms):
        """Remember, in chip time, what of the recording the operator has looked at.

        Gives that chip time back (None when the frame has none) for the picture to show.
        """
        super().record_viewed_frame(frame, recorder_ms)
        self.timeline.current = int(recorder_ms)
        crossing = recorder_crossing_time(recorder_ms, frame, self.workspace_dir)
        if crossing is None or self.workspace_dir is None:
            return None
        stamp = crossing.value_ms
        self._shown_ms = stamp
        self.race_strip.set_cursor(stamp, frame.get("camera_index") or 1)
        camera = str(int(frame.get("camera_index") or 1))
        seen = self._arrival_drafts.setdefault("seen", {})
        seen[camera] = add_viewed(seen.get(camera, []), stamp, stamp - WAVE_VIEW_REACH_MS,
                                  stamp + WAVE_VIEW_REACH_MS, WAVE_VIEW_REACH_MS)
        self._draft_timer.start()
        selected = self._selected_arrival()
        plan = self._wave_plan()
        if plan is not None and (plan.marks or plan.split) and any(
                chip.decision is None and not chip.note for chip in plan.unmatched) and not self._unseen(plan):
            self._render()  # just looked through: its chips without a person turn 疑似双芯片
        elif plan is not None or (selected is not None and selected.fragments):
            self._sync_arrival_confirmation()  # "还有 x 秒没看" counts down on this very frame
        if self._auto_review is not None:
            self._advance_auto_review()
        if self._run_to is not None:
            if abs(stamp - self._run_to) > RUN_STALE_MS:
                self._run_to = None  # the operator went elsewhere: nothing to stop
                # ... nor a wave to open at its end (11-15: 63 号 Space opened 23 号, the
                # row after a wave left mid-sweep minutes before).
                self._run_between = self._run_counted = None
                self._run_slow()
                self.run_requested.emit(False)  # never left playing with nowhere to stop
            elif stamp >= self._run_to - RUN_STOP_LEAD_MS:
                tail, self._run_to = self._run_tail, None
                self._run_slow()
                self.run_requested.emit(False)
                QTimer.singleShot(0, lambda: self._run_arrived(tail))  # rule outside the frame callback
            elif stamp >= self._run_to - RUN_SLOW_MS:
                self._run_slow()  # the last stretch at normal speed, to stop on the frame
        return stamp

    def _run_target(self, plan, now, ahead=None):
        """(moment, tail) the recording plays on to from NOW, in chip time.

        () when it stays where it is, None when there is nothing left to play.

        The next person: a chip of this wave nobody is counted for, or someone
        the analysis saw cross before the wave's tail ends.  The tail: nobody
        left, so on to RUN_TAIL_MS after the last chip.
        """
        stamps = [int(row.event.timeline_timestamp_ms) for row in plan.chips]
        if not stamps:
            return None
        offset, crossing_of = self._run_offset(plan)
        end = max(stamps) + offset + RUN_TAIL_MS
        due = sorted(int(row.event.timeline_timestamp_ms) + offset for row in plan.unmatched if row.decision is None)
        if due and due[0] <= now + RUN_NEAR_MS:
            # The next chip in order is on this picture, or (clicked out of order)
            # behind it: either way nothing is to be played past.
            return ()
        limit = due[0] if due else end
        seen = []
        if self.workspace_dir:
            try:
                people = people_on_line(self.workspace_dir, int(self.race_strip.camera or 1))
            except (OSError, ValueError, KeyError):
                people = []
            counted = list(crossing_of.values())
            seen = [person.chip_ms for person in people
                    if person.aligned and now + RUN_NEAR_MS < person.chip_ms < limit
                    and not any(abs(person.chip_ms - value) <= RUN_NEAR_MS for value in counted)]
        # Whatever comes onto the line before then, chip or not, seen by the
        # analysis or not: the strip shows it (None: no strip of this stretch).
        arrivals = None
        pieces = self.race_strip.view.pieces
        if pieces and limit > now + RUN_NEAR_MS:
            arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        self._run_swept = arrivals is not None
        seen += [int(moment) for moment in arrivals or ()]
        ahead = ahead or self._next_counted(now, plan)
        if ahead is not None and ahead[0] < limit:
            seen.append(int(ahead[0]))  # someone already clicked comes first: stop on them
        if seen or due:
            return min(seen + due[:1]), False
        return (end, True) if end > now + RUN_NEAR_MS else None

    def _compute_run_offset(self, plan):
        crossing_of = {}
        if self.workspace_dir:
            for index, mark in enumerate(plan.marks):
                crossing = recorder_crossing_time(mark["recorder_ms"], mark.get("frame") or {}, self.workspace_dir)
                if crossing is not None:
                    crossing_of[index] = crossing.value_ms
        offsets = sorted(crossing_of[index] - int(row.event.timeline_timestamp_ms)
                         for index, row in plan.assigned if index in crossing_of)
        return (offsets[len(offsets) // 2] if offsets else 0), crossing_of

    def _next_on_line(self, now):
        """When someone new comes onto the line within CLOSE_BEHIND_MS (inf: nobody, None: no strip)."""
        pieces = self.race_strip.view.pieces
        if not pieces:
            return None
        return line_next_entry(pieces, self.race_strip.view.cache, now + SAME_BODY_MS, now + CLOSE_BEHIND_MS)

    def _sweep_on(self, now):
        """Space with no wave to count here: fast on to the next chip, or to what comes onto the line.

        The long empty stretches are between waves (11-14); played at normal
        speed they take as long as they did in the race, and a jump to the next
        chip would pass someone without one.  False where the strip does not
        cover the stretch: Space then plays as it always did.
        """
        pieces = self.race_strip.view.pieces
        if not pieces:
            return False
        ahead = [int(row.event.timeline_timestamp_ms) for row in self._arrival_rows
                 if row.event is not None and int(row.event.timeline_timestamp_ms) > now + RUN_NEAR_MS]
        limit = min([now + SWEEP_REACH_MS] + ahead)
        arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        if arrivals is None:
            return False
        target = int(min(arrivals + [limit]))
        self._run_to, self._run_tail = target, False
        self._run_fast = target - now >= RUN_FAST_MIN_MS
        self.hint.setText(("快扫到线上有人影的地方" if arrivals else "这一段线上没有人影，快扫到下一个芯片"
                           if limit in ahead else "这一段线上没有人影，快扫过去；再按空格继续")
                          + "。看到有人压线就按空格停下再点他。")
        self.run_speed.emit(RUN_FAST_SPEED if self._run_fast else 1.0)
        self.run_requested.emit(True)
        return True

    TARGET_ROW_STYLE = "QTableWidget::item:selected {background:#ffd84d; color:#111827;}"
    DONE_ROW_STYLE = "QTableWidget::item:selected {background:#7fdca4; color:#0b3d2a;}"
    PLAIN_ROW_STYLE = "QTableWidget::item:selected {background:#d6eeea; color:#123d36;}"

    def _sweep_between(self, now, item, there=False):
        """From a wave counted even to the next row: played fast, stopping where something comes onto the line.

        The wave used to open the next one by itself, over seconds nobody had
        looked at (11-14, 古浪 11:49:14 to 11:49:21: "感觉不是按照录像的那个顺序
        过去的，直接就跳过去的").  Where the strip does not cover the stretch
        the next row opens as it did, and the hint says so.
        """
        following = self._following_row(item)
        moment = None
        if following is not None:
            moment = (following.time_ms if following.time_ms is not None
                      else int(following.event.timeline_timestamp_ms) if following.event is not None else None)
        pieces = self.race_strip.view.pieces
        arrivals = None
        if not there and moment is not None and moment > now + RUN_NEAR_MS and pieces:
            limit = min(int(moment), now + SWEEP_REACH_MS)
            arrivals = line_arrivals(pieces, self.race_strip.view.cache, now + RUN_NEAR_MS, limit)
        if arrivals is None:
            jumped = not there and moment is not None and moment > now + RUN_FAST_MIN_MS
            self._next_wave(item)
            self.hint.setText("上一波人数与芯片对上，已到下一处。"
                              + ("中间这一段没有过线图，是直接跳过来的。" if jumped else "")
                              + ("后面没有要看的了。" if "后面没有" in self.hint.text() else ""))
            return
        target = int(min(arrivals + [limit]))
        self._run_between = (item, "figure" if arrivals else "there" if limit == int(moment) else "hop")
        self._run_to, self._run_tail = target, False
        self._run_fast = target - now >= RUN_FAST_MIN_MS
        self.hint.setText("上一波人数与芯片对上，快扫到下一波（线上一有人影就停）；回车直接跳到下一波，空格停。")
        self.run_speed.emit(RUN_FAST_SPEED if self._run_fast else 1.0)
        self.run_requested.emit(True)

    def _wave_for_frame(self, frame):
        """The wave a click on FRAME counts for: the one whose chips are nearest in time.

        Not the row selected on the left: after a wave's last rider the operator
        steps on into the next wave without touching the table (古浪: riders of
        1 / 74 / 250 were counted as 多 2 on the wave of 11 and 64).  Someone far
        from every chip is 多 1 on the nearest wave, in a row at their own time.
        """
        selected = self._row_wave(self._selected_arrival())
        crossing = (recorder_crossing_time(frame["recorder_ms"], frame, self.workspace_dir)
                    if frame and frame.get("recorder_ms") is not None and self.workspace_dir else None)
        if crossing is None:
            return selected
        best = None
        for wave in self._waves:
            stamps = [int(row.event.timeline_timestamp_ms) for row in wave]
            key = (max(min(stamps) - crossing.value_ms, crossing.value_ms - max(stamps), 0), wave is not selected)
            if best is None or key < best[0]:
                best = (key, wave)
        return best[1] if best else selected

    def _configure_sampling(self):
        self.sampling_preparing.emit()
        super()._configure_sampling()
        self.sampling_configured.emit()

    def _review_frame(self, chip_ms, row):
        """The recording at CHIP_MS: from the race strip, else by offset in the row's own video.

        Recordings not yet calibrated have no strip (特奥, 10-28: grey), yet the
        row's own picture plays; its own file then serves the second around it.
        """
        pieces = self.race_strip.view.pieces
        frame = frame_at(pieces, chip_ms) if pieces else None
        own = row.item.evidence.get("frame") or {}
        if frame is None and own.get("video_path") and own.get("position_ms") is not None:
            frame = {key: own[key] for key in ("camera_index", "segment_id", "video_path") if key in own}
            frame.update(position_ms=max(0, int(own["position_ms"]) + chip_ms - row.time_ms), frame_index=None)
        return frame

    def _confirm_arrival(self, row):
        if row.note == "已排除":
            self.hint.setText("这条芯片已在本机排除，按 4 撤回后再处理。")
            return
        if row.decision:
            self.hint.setText("该运动员已确认；若要修改，请先撤销误确认，再重新定位压线帧。")
            return
        item = self._candidate_item()
        category = self._category()
        if category is None or category.get("action") != "bicycle":
            self.hint.setText("请先确认画面中有自行车，再定位压线帧并确认运动员。")
            return
        if not self._usable(item):
            return
        events = self._events()
        event = events.get(row.event.event_id) if row.event else None
        if event is None or not event.is_active or not row.bib:
            self.hint.setText("没有明确的有效芯片记录，保留待匹配。")
            return
        if row.event is not None and event_binding(event) != event_binding(row.event):
            self._render()
            self.hint.setText("芯片记录已变化，请重新核对。")
            return
        if row.item.evidence.get("configuration_stale"):
            self.hint.setText("证据配置已变化，请重新分析。")
            return
        self.confirmation_preparing.emit()
        frame = self._frame_provider()
        if frame is None:
            self.hint.setText("请先打开有效录像，停在该选手前轮压线的原帧。")
            return
        frame = dict(frame, calibration_version=item.calibration_version)
        from .bunch_review import mark_on_frame
        marks = self.current_marks()
        mark = marks[self._editing_mark] if 0 <= self._editing_mark < len(marks) else None
        if mark and mark_on_frame(mark, frame, frame.get("recorder_ms")) and not mark.get("exclusion_reason"):
            frame.update(marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
        crossing = recorder_crossing_time(frame.get("recorder_ms"), frame, self.workspace_dir)
        try:
            self._arrival_store.save(
                self._arrival_scope, row.key, event_binding(event), "confirm", event_id=event.event_id,
                bib=event.bib, candidate_id=item.item_id, candidate_binding=candidate_binding(item),
                frame=frame, crossing_ms=crossing.value_ms if crossing else None,
                operation_id=getattr(self, "_shortcut_operation", None),
                calibration_version=item.calibration_version,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"过线未保存：{error}")
            return
        self._selected_key = row.key
        self._last_saved_key = row.key
        self._render()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(f"已确认 {event.bib} 过线；继续看下一位。")

    def add_mark(self, x, y, recorder_ms, frame=None, *, save=True):
        """A click on a person counts them; on a wave's picture it takes the next chip's bib at once.

        Someone already counted on this frame, those moved onto a 疑似漏读 row
        too, is not counted again (古浪 10-28: one rider, two 疑似漏读 rows).
        """
        item = self.current_item()
        ref = dict(frame or (item.evidence.get("frame") if item is not None else None) or {})
        if item is not None and any(owner != item.item_id and mark_on_frame(mark, ref, recorder_ms)
                                    and (mark["x"] - x) ** 2 + (mark["y"] - y) ** 2 < .02 ** 2
                                    for owner, mark in self.picture_marks()):
            self.hint.setText("这个人已点过（无芯片）；并排选手请分别点各自前轮。")
            return
        self.stop_run()  # a click while it plays counts on the frame shown, and holds it
        before = len(self.current_marks())
        super().add_mark(x, y, recorder_ms, frame)
        plan = self._wave_plan()
        if save and plan is not None and len(plan.marks) > before:
            stored = self._save_wave(plan)
            self.show_counted(self._editing_mark)
            self.marks_changed.emit(tuple(self.current_marks()))
            self.hint.setText(self.picture_mark_hint())
            if stored:
                crossing = recorder_crossing_time(recorder_ms, ref, self.workspace_dir) if self.workspace_dir else None
                self._run_from = crossing.value_ms if crossing is not None else int(recorder_ms)
                self._clicked_at = self._run_from
                QTimer.singleShot(0, self._run_on)  # once the window has drawn the click

    def _save_wave(self, plan, *, operation_id=None):
        """Make what is saved for a wave what its picture now counts.

        Runs on every click, right click, 1 and 3: each person counted is their
        chip's crossing at once, with the frame and spot they were clicked on.
        A chip whose person moved on to another bib (someone was clicked, or
        cancelled, before them) is taken back first and saved again.  Count
        differences never block; the wave shows 多 / 少 in red instead.
        """
        item = plan.item
        if not self._usable(item):
            return False
        events = self._events()
        wanted = {row.key: plan.marks[index] for index, row in plan.assigned}
        scope, binding = self._arrival_scope, candidate_binding(item)
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        changed = []
        operation_id = operation_id or getattr(self, "_shortcut_operation", None)
        self.confirmation_preparing.emit()
        try:
            category = self._category(item)
            if wanted and (category is None or category.get("action") != "bicycle"):
                picture = dict(item.evidence.get("frame") or {})
                picture.setdefault("calibration_version", item.calibration_version)
                self._arrival_store.save(scope, "candidate:" + item.item_id, binding, "bicycle",
                                         candidate_id=item.item_id, frame=picture, operation_id=operation_id,
                                         analysis_signature=item.evidence.get("analysis_signature", ""),
                                         calibration_version=item.calibration_version)
            for row in plan.chips:
                if row.decision is not None and not self._counted_here(row, item):
                    continue  # confirmed elsewhere: not this picture's to change
                mark = wanted.get(row.key)
                if row.decision is not None:
                    if mark is not None and self._decision_holds(row.decision, mark):
                        continue
                    self._arrival_store.undo(scope, key=row.key)
                if mark is None:
                    continue
                event = events.get(row.event.event_id)
                if event is None or not event.is_active or event_binding(event) != event_binding(row.event):
                    changed.append(row.bib)
                    continue
                frame = dict(mark.get("frame") or {}, calibration_version=item.calibration_version,
                             marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
                crossing = recorder_crossing_time(mark["recorder_ms"], frame, self.workspace_dir, clocks=clocks)
                self._arrival_store.save(
                    scope, row.key, event_binding(event), "confirm", event_id=event.event_id, bib=event.bib,
                    candidate_id=item.item_id, candidate_binding=binding, frame=frame,
                    crossing_ms=crossing.value_ms if crossing else None,
                    operation_id=operation_id,
                    auto_label=bool(mark.get("auto_label")),
                    review_method="auto_label_operator_accepted" if mark.get("auto_label") else "manual_click",
                    calibration_version=item.calibration_version,
                )
        except (OSError, RuntimeError, ValueError) as error:
            rollback_error = self._rollback_wave_operation(scope, operation_id) if operation_id else ""
            self._quick_render_signature = None
            self._render()
            self.hint.setText(f"过线未保存：{error}{rollback_error}")
            return False
        rollback_error = (self._rollback_wave_operation(scope, operation_id)
                          if changed and operation_id else "")
        self._quick_render_signature = None
        self._render()
        if changed:
            self.hint.setText(f"芯片记录已变化（{'、'.join(changed)}），请重新核对本波。{rollback_error}")
            return False
        return True
