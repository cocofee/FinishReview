"""One chronological time/bib table beside the original recording."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QAbstractSpinBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from .arrival_review import (
    ARRIVAL_REVIEWS_FILENAME, ArrivalReviewStore, candidate_binding, event_binding,
)
from .chip_history import format_chip_time
from .crossing_time import item_crossing_time, recorder_crossing_time, workspace_clocks
from .problem_items import (
    CATEGORY_VIDEO, KIND_CHIP_NO_CROSSING, STATUS_EXCLUDED,
    STATUS_WAITING, ProblemItem,
)
from .problem_panel import ProblemListPanel


@dataclass
class ArrivalRow:
    key: str
    item: ProblemItem
    bib: str = ""
    event: object = None
    time_ms: int | None = None
    reference: bool = True
    decision: dict | None = None
    stale: bool = False


class QuickReviewPanel(ProblemListPanel):
    quick_review = True
    confirmation_preparing = pyqtSignal()
    candidate_preparing = pyqtSignal()
    sampling_configured = pyqtSignal()

    def __init__(self, parent=None):
        self._quick_ready = False
        self._active_item = None
        self._arrival_rows = []
        self._arrival_store = None
        self._arrival_scope = ("", "")
        self._selected_key = None
        self._quick_render_signature = None
        self._simultaneous = Counter()
        self._frame_provider = lambda: None
        super().__init__(parent)
        self.header.hide()
        self.detail_scroll.hide()
        self.timeline.hide()
        self.timeline_help.hide()
        self.table.currentCellChanged.disconnect()
        self.table.cellDoubleClicked.disconnect()
        self.table.cellClicked.connect(self._arrival_clicked)
        self.table.cellDoubleClicked.connect(lambda index, _column: self._open_row(index))
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["顺序", "号码", "时间", "状态"])
        for column, width in enumerate((42, 66, 137)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumWidth(340)
        self.table.setMaximumWidth(440)
        self.table.setToolTip("点击回看对应时段；已确认有自行车时单击只选运动员，双击仍可回看；定位压线原帧后点“确认过线”保存")
        self.quick_toolbar = QWidget(self)
        bar = QHBoxLayout(self.quick_toolbar)
        bar.setContentsMargins(2, 3, 2, 3)
        self.quick_title = QLabel("到达记录", self.quick_toolbar)
        bar.addWidget(self.quick_title, 1)
        self.bicycle_button = QPushButton("1  有自行车", self.quick_toolbar)
        self.non_rider_button = QPushButton("2  非选手", self.quick_toolbar)
        self.defer_button = QPushButton("3  暂放", self.quick_toolbar)
        self.confirm_arrival_button = QPushButton("确认过线", self.quick_toolbar)
        self.confirm_arrival_button.setToolTip(
            "先选左侧运动员，再将画面停在其前轮压终点线的一帧，最后点击确认过线"
        )
        self.quick_undo_button = QPushButton("撤销 ⌫", self.quick_toolbar)
        for button in (self.bicycle_button, self.non_rider_button, self.defer_button,
                       self.confirm_arrival_button, self.quick_undo_button):
            bar.addWidget(button)
        self.bicycle_button.clicked.connect(self._confirm_bicycle)
        self.non_rider_button.clicked.connect(lambda: self._classify("not_rider"))
        self.defer_button.clicked.connect(lambda: self._classify("held"))
        self.confirm_arrival_button.clicked.connect(self._confirm_selected_arrival)
        self.quick_undo_button.clicked.connect(self._undo_arrival)
        more = QPushButton("更多", self.quick_toolbar)
        menu = QMenu(more)
        menu.addAction("分析录像", self._toggle_analysis)
        menu.addAction("导出过线记录", self.export_arrivals)
        menu.addAction("导出交接清单", self.export)
        menu.addAction("复杂异常处理", self._open_legacy)
        menu.addSeparator()
        menu.addAction("清空试验数据", self.clear_data_requested.emit)
        more.setMenu(menu)
        bar.addWidget(more)
        self.layout().insertWidget(0, self.quick_toolbar)
        guidance = QHBoxLayout()
        self.finish_line_button = QPushButton("终点线 / 模型", self)
        self.finish_line_button.setToolTip(
            "绘制本机位终点线，选择本机 YOLOX ONNX 模型并试运行；只分析已封存录像"
        )
        self.finish_line_button.clicked.connect(self._configure_sampling)
        guidance.addWidget(self.finish_line_button)
        self.finish_line_status = QLabel("终点线未设置：请先在实际画面上画线", self)
        guidance.addWidget(self.finish_line_status)
        self.confirmation_status = QLabel("", self)
        guidance.addWidget(self.confirmation_status, 1)
        self.layout().insertLayout(1, guidance)
        self._quick_ready = True
        QApplication.instance().installEventFilter(self)
        self._render()

    def eventFilter(self, watched, event):
        if (self._quick_ready and event.type() == QEvent.KeyPress and self.isVisible()
                and isinstance(watched, QWidget) and watched.window() is self.window()
                and not isinstance(watched, (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit))
                and self._queue_key(event)):
            return True
        return super().eventFilter(watched, event)

    def set_frame_provider(self, provider):
        self._frame_provider = provider

    def set_workspace(self, workspace_dir):
        if Path(workspace_dir) != self.workspace_dir:
            self._active_item = None
            self._selected_key = None
            self._arrival_store = None
            self._arrival_rows = []
            self._quick_render_signature = None
        super().set_workspace(workspace_dir)

    def reset_data(self):
        self._active_item = None
        self._selected_key = None
        self._arrival_store = None
        self._arrival_rows = []
        self._quick_render_signature = None
        super().reset_data()
        self._render()

    def set_scope(self, race_id, stage_id):
        if (str(race_id or ""), str(stage_id or "")) != self._scope:
            self._active_item = None
            self._selected_key = None
            self._quick_render_signature = None
        super().set_scope(race_id, stage_id)

    def current_item(self):
        if not self._quick_ready:
            return super().current_item()
        return self._active_item

    def _show(self, row):
        if not self._quick_ready:
            super()._show(row)

    def _ensure_store(self):
        if self.problem_dir is None:
            return
        path = self.problem_dir / ARRIVAL_REVIEWS_FILENAME
        if self._arrival_store is None or self._arrival_store.path != path:
            self._arrival_store = ArrivalReviewStore(path)
        else:
            self._arrival_store.refresh()

    def _events(self):
        events = tuple(self._events_provider())
        scopes = {(event.race_id, event.stage_id) for event in events}
        self._arrival_scope = self._scope if self._scope[0] else next(iter(scopes)) if len(scopes) == 1 else self._scope
        return {event.event_id: event for event in events
                if (event.race_id, event.stage_id) == self._arrival_scope}

    def _category(self, item=None):
        item = item or self._candidate_item()
        return (self._arrival_store.latest(self._arrival_scope, "candidate:" + item.item_id,
                                           candidate_binding(item))
                if self._arrival_store is not None and item is not None else None)

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
            if not pairs:
                if raw.grouped:
                    continue
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("candidate:" + item.item_id, item,
                                       time_ms=crossing.value_ms if crossing else None,
                                       reference=crossing is None or crossing.source in ("expected", "chip")))
            for event_id, bib in pairs:
                if not event_id or event_id in represented:
                    continue
                represented.add(event_id)
                event = events.get(event_id)
                if event is not None:
                    bib = event.bib
                crossing = item_crossing_time(item, self.workspace_dir, clocks=clocks)
                rows.append(ArrivalRow("event:" + event_id, item, bib, event,
                                       int(event.timeline_timestamp_ms) if event else
                                       crossing.value_ms if crossing else None))
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
            if self._arrival_store is not None and row.event is not None:
                previous = self._arrival_store.latest(self._arrival_scope, row.key)
                if previous is not None:
                    anchor = anchors.get(previous.get("candidate_id"))
                    row.stale = (previous["binding"] != event_binding(row.event)
                                 or anchor is None
                                 or previous.get("candidate_binding") != candidate_binding(anchor))
                    if (not row.stale and previous["action"] == "confirm" and row.event.is_active
                            and not row.item.evidence.get("configuration_stale")):
                        row.decision = previous
                        row.time_ms = previous["crossing_ms"]
                        row.reference = False
        return sorted(rows, key=lambda row: (row.time_ms if row.time_ms is not None else 2**62, row.key))

    def _row_state(self, row):
        if row.event is not None and not row.event.is_active:
            return "已作废"
        if (row.item.status == STATUS_WAITING or row.item.evidence.get("configuration_stale")
                or row.item.evidence.get("superseded")):
            return "待证据"
        if row.stale:
            return "待复核"
        if row.decision:
            simultaneous = self._simultaneous[row.time_ms] > 1
            return "已确认·同时" if simultaneous else "已确认"
        category = self._category(row.item)
        if category:
            return {"bicycle": "待认人", "held": "暂放", "not_rider": "已排除"}[category["action"]]
        return "待确认" if row.bib else "待匹配"

    def _render(self):
        if not self._quick_ready:
            return
        try:
            self._ensure_store()
            rows = self._build_rows()
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"到达记录读取失败：{error}")
            return
        self._arrival_rows = rows
        self._simultaneous = Counter(row.time_ms for row in rows if row.decision)
        self.rows = [row.item for row in rows]
        signature = (self._arrival_scope, self._selected_key,
                     tuple((row.key, row.bib, row.time_ms, row.reference, self._row_state(row),
                            candidate_binding(row.item)) for row in rows))
        if signature == self._quick_render_signature:
            self._sync_arrival_confirmation()
            return
        self._quick_render_signature = signature
        self.table.blockSignals(True)
        scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(len(rows))
        selected = -1
        for index, row in enumerate(rows):
            state = self._row_state(row)
            time = format_chip_time(row.time_ms) if row.time_ms is not None else "—"
            for column, text in enumerate((str(index + 1), row.bib or "—", time, state)):
                from PyQt5.QtWidgets import QTableWidgetItem
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row.key)
                cell.setToolTip("点击回看保存原帧" if row.decision else
                                "点击回看对应时段；已确认有自行车时单击只选择运动员，双击回看；不会自动确认过线")
                self.table.setItem(index, column, cell)
            if row.key == self._selected_key:
                selected = index
        if selected >= 0:
            self.table.setCurrentCell(selected, 0)
        elif rows:
            self.table.setCurrentCell(0, 0)
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)
        confirmed = sum(row.decision is not None for row in rows)
        self.quick_title.setText(f"到达记录 {len(rows)} 条 · 已确认 {confirmed} 人")
        self.summary.setText(self.quick_title.text())
        self.summary_changed.emit(self.quick_title.text())
        if self._active_item is None and rows:
            self._open_row(0)
        elif self._active_item is not None:
            fresh = next((row.item for row in rows if row.item.item_id == self._active_item.item_id), None)
            if fresh is None:
                self._active_item = None
                if rows:
                    self._open_row(0)
                else:
                    self.current_changed.emit(None)
            else:
                changed = candidate_binding(fresh) != candidate_binding(self._active_item)
                self._active_item = fresh
                if changed:
                    self.current_changed.emit(fresh)
        self.bicycle_button.setEnabled(self.current_item() is not None)
        self._sync_arrival_confirmation()

    def _open_row(self, index):
        if not 0 <= index < len(self._arrival_rows):
            return
        row = self._arrival_rows[index]
        self._selected_key = row.key
        self._active_item = row.item
        self._shown_id = row.item.item_id
        self.table.setCurrentCell(index, 0)
        self._sync_timeline()
        self.current_changed.emit(row.item)
        self._sync_arrival_confirmation()

    def _sync_timeline(self):
        if not self._quick_ready:
            return super()._sync_timeline()
        previous = self._big_view
        self._big_view = True
        try:
            super()._sync_timeline()
        finally:
            self._big_view = previous

    def select_item(self, item_id):
        index = next((i for i, row in enumerate(self._arrival_rows) if row.item.item_id == item_id), None)
        if index is not None:
            self._open_row(index)

    def _arrival_clicked(self, index, _column=0):
        if not 0 <= index < len(self._arrival_rows):
            return
        row = self._arrival_rows[index]
        if row.decision:
            self._open_row(index)
            self.jump_requested.emit(dict(row.decision["frame"]))
            return
        # Preserve a deliberately classified picture while assigning identities,
        # but do not strand a newly received chip on an unrelated/empty picture.
        category = self._category()
        frame = self._frame_provider()
        if not (category and category.get("action") == "bicycle" and frame
                and frame.get("frame_index", -1) >= 0):
            self._open_row(index)
            return
        self._selected_key = row.key
        self.table.setCurrentCell(index, 0)
        self._sync_arrival_confirmation()
        self.hint.setText("已选中运动员。请停在前轮压终点线的一帧，再点“确认过线”。")

    def _selected_arrival(self):
        index = self.table.currentRow()
        return self._arrival_rows[index] if 0 <= index < len(self._arrival_rows) else None

    def _sync_arrival_confirmation(self):
        row = self._selected_arrival()
        item = self._candidate_item()
        category = self._category()
        frame = self._frame_provider()
        valid_frame = bool(frame and frame.get("frame_index", -1) >= 0
                           and frame.get("position_ms", -1) >= 0
                           and frame.get("segment_id") and frame.get("video_path")
                           and frame.get("recorder_ms") is not None)
        if row is None or row.event is None or not row.event.is_active or not row.bib:
            reason = "请先选择有有效芯片记录的运动员"
        elif row.decision is not None:
            reason = "该运动员已确认；修改前请先撤销"
        elif not valid_frame:
            reason = "请从胶卷打开有效原帧"
        elif item is None or not item.evidence.get("frame"):
            reason = "请先按 1 确认当前画面有自行车"
        elif (item.status == STATUS_WAITING or item.evidence.get("configuration_stale")
              or item.evidence.get("superseded")):
            reason = "当前候选证据已失效，请重新打开原帧"
        elif category is None or category.get("action") != "bicycle":
            reason = "请先按 1 确认当前画面有自行车"
        elif row.item.evidence.get("configuration_stale"):
            reason = "所选记录配置已变化，请重新分析"
        else:
            reason = ""
        self.confirm_arrival_button.setEnabled(not reason)
        self.confirm_arrival_button.setToolTip(reason or "保存所选运动员与当前显示的压线原帧；不会修改正式芯片时间")
        self.confirmation_status.setText(reason or "已就绪：停在前轮压线帧后点“确认过线”")

    def set_finish_line_status(self, configured):
        self.finish_line_status.setText("绿色实线：实际终点线" if configured else "终点线未设置：请先在实际画面上画线")
        self.finish_line_status.setStyleSheet("color:#17675b;" if configured else "color:#9a5b12;")

    def _configure_sampling(self):
        super()._configure_sampling()
        self.sampling_configured.emit()

    def _confirm_selected_arrival(self):
        row = self._selected_arrival()
        if row is None:
            self.hint.setText("请先从左侧选择要确认的运动员。")
            return
        self._confirm_arrival(row)

    def _usable(self, item=None):
        item = item or self._candidate_item()
        if item is None or self._arrival_store is None:
            return False
        if item.status == STATUS_WAITING or item.evidence.get("configuration_stale") or item.evidence.get("superseded"):
            self.hint.setText("证据已变化，等待重新分析后再确认。")
            return False
        return True

    def _candidate_item(self):
        """Return the video candidate, not a synthetic chip-only table row."""
        item = self.current_item()
        if item is not None and item.evidence.get("frame"):
            return item
        return next((row.item for row in self._arrival_rows if row.item.evidence.get("frame")), item)

    def _confirm_bicycle(self):
        self._classify("bicycle")

    def _classify(self, action):
        if action != "held":
            self.candidate_preparing.emit()
        if not self._usable():
            return
        item = self._candidate_item()
        frame = self._frame_provider()
        # A classification belongs to the candidate currently on screen.  Do not
        # let a stale/partial provider result create an audit entry that cannot be
        # reopened later (``held`` is the sole action that may be frame-less).
        if action != "held" and (not frame or frame.get("frame_index", -1) < 0
                                  or frame.get("position_ms", -1) < 0
                                  or not frame.get("segment_id")
                                  or not frame.get("video_path")):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return
        if action == "not_rider" and any(row.decision and row.decision.get("candidate_id") == item.item_id
                                         for row in self._arrival_rows):
            self.hint.setText("本处已有选手确认，请先撤销误确认。")
            return
        try:
            frame = dict(frame or {})
            frame.setdefault("calibration_version", item.calibration_version)
            self._arrival_store.save(self._arrival_scope, "candidate:" + item.item_id,
                                     candidate_binding(item), action, candidate_id=item.item_id,
                                     frame=frame,
                                     analysis_signature=item.evidence.get("analysis_signature", ""),
                                     calibration_version=item.calibration_version)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return
        self._render()
        self.hint.setText("已确认有自行车；选择运动员，定位压线帧后点“确认过线”（F 可放大）。" if action == "bicycle"
                          else "已暂放。" if action == "held" else "已排除此画面候选。")
        if action in ("held", "not_rider"):
            self._step(1)

    def _confirm_arrival(self, row):
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
        crossing = recorder_crossing_time(frame.get("recorder_ms"), frame, self.workspace_dir)
        try:
            self._arrival_store.save(
                self._arrival_scope, row.key, event_binding(event), "confirm", event_id=event.event_id,
                bib=event.bib, candidate_id=item.item_id, candidate_binding=candidate_binding(item),
                frame=frame, crossing_ms=crossing.value_ms if crossing else None,
                calibration_version=item.calibration_version,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"过线未保存：{error}")
            return
        self._selected_key = row.key
        self._render()
        self.hint.setText(f"已确认 {event.bib} 过线；继续看下一位。")

    def _undo_arrival(self):
        if self._arrival_store is None:
            return
        try:
            previous = self._arrival_store.undo(self._arrival_scope)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤销未保存：{error}")
            return
        self._render()
        self.hint.setText("已撤销上一操作。" if previous else "没有可撤销的操作。")

    def _step(self, delta):
        items = list(dict.fromkeys(row.item.item_id for row in self._arrival_rows))
        current = self.current_item()
        index = items.index(current.item_id) if current is not None and current.item_id in items else -1
        if 0 <= index + delta < len(items):
            self.select_item(items[index + delta])

    def _queue_key(self, event):
        if event.isAutoRepeat():
            return event.key() in (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_Backspace)
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_Z:
            self._undo_arrival()
            return True
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        # F must work from any workbench child; Esc symmetrically restores the rail.
        if event.key() == Qt.Key_F:
            self.toggle_big_view()
            return True
        if event.key() == Qt.Key_Escape and self._big_view:
            self.toggle_big_view()
            return True
        actions = {Qt.Key_1: self._confirm_bicycle, Qt.Key_2: lambda: self._classify("not_rider"),
                   Qt.Key_3: lambda: self._classify("held"), Qt.Key_Backspace: self._undo_arrival,
                   Qt.Key_Return: self._confirm_selected_arrival, Qt.Key_Enter: self._confirm_selected_arrival,
                   Qt.Key_Up: lambda: self._step(-1), Qt.Key_Down: lambda: self._step(1),
                   Qt.Key_Left: lambda: self.frame_step_requested.emit(-1),
                   Qt.Key_Right: lambda: self.frame_step_requested.emit(1)}
        if event.key() in actions:
            actions[event.key()]()
            return True
        return False

    def set_browsing(self, browsing):
        self._browsing = bool(browsing)

    def toggle_big_view(self):
        """Expand the evidence area while keeping the arrival list and actions visible."""

        self._big_view = not self._big_view
        self._sync_step_buttons()
        self._sync_arrival_confirmation()
        self.count_mode_changed.emit(self._big_view)
        if self._big_view:
            self.hint.setText("画面已放大：选择运动员，停在前轮压线的一帧，再点“确认过线”。F / Esc 返回。")
            self.setFocus()
        else:
            self.hint.setText("已返回胶卷视图；名单点击只选人，定位压线帧后点“确认过线”（F 可放大）。")
            self.table.setFocus()
        self.big_view_changed.emit(self._big_view)

    def export_arrivals(self):
        if self.problem_dir is None:
            return None
        self.refresh()
        path = self.problem_dir / "exports" / "人工过线记录.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["顺序", "号码", "过线时间", "事件", "机位", "录像", "帧号", "校时版本"])
            for index, row in enumerate(row for row in self._arrival_rows if row.decision):
                frame = row.decision["frame"]
                writer.writerow([index + 1, row.bib, format_chip_time(row.time_ms), row.event.event_id,
                                 frame.get("camera_index", ""), frame.get("video_path", ""),
                                 frame.get("frame_index", ""),
                                 row.decision["calibration_version"]])
        self.hint.setText(f"已导出：{path}")
        return path

    def _open_legacy(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("复杂异常处理")
        dialog.resize(1100, 750)
        layout = QVBoxLayout(dialog)
        panel = ProblemListPanel(dialog)
        panel.set_events_provider(self._events_provider)
        panel.set_workspace(self.workspace_dir)
        panel.set_scope(*self._scope)
        panel.jump_requested.connect(self.jump_requested.emit)
        layout.addWidget(panel)
        dialog.exec_()
        panel.stop_analysis()
        panel.stop_chip_checks()
        panel.save_drafts()
        self.refresh()
