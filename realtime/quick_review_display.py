"""Display and row actions of the quick review panel: rendering, opening rows, hints, menus and filters."""

from __future__ import annotations

from collections import Counter

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QLabel, QMenu

from .arrival_review import candidate_binding
from .bunch_review import is_excluded
from .chip_history import format_chip_time
from .crossing_time import manual_calibrations, workspace_clocks
from .race_strip import AUTO_LABELLED
from .quick_review_common import (
    AUTO_GO_ON,
    AUTO_TEXT,
    KEEP,
    KEY_NAMES,
    NEAR_COUNTED_TEXT,
    PASSAGE_VIEW_MARGIN_MS,
    ROW_KEYS,
    row_moment,
)
from .ui_parts import UiPart


class QuickReviewDisplayPart(UiPart):
    """Render the table, open rows and pictures, row hints/menus/actions, status and view toggles, filters."""

    def _request_delete(self):
        row = next((row for row in self._host._arrival_rows if row.key == self._host._selected_key), None)
        if row is None or row.event is None:
            self._host.hint.setText("先在左表选中要删除的芯片记录")
            return
        self._host.delete_requested.emit(row.event.event_id)

    def _render(self):
        if not self._host._quick_ready:
            return False
        self._host.refresh_calibration_state()
        try:
            self._host._ensure_store()
            rows = self._host._build_rows()
        except (OSError, RuntimeError, ValueError) as error:
            self._host.hint.setText(f"到达记录读取失败：{error}")
            return False
        excluded = frozenset(row.key for row in rows if row.note == "已排除")
        if excluded != getattr(self._host, "_excluded_chip_keys", frozenset()):
            self._host._earlier.clear()
        self._host._excluded_chip_keys = excluded
        self._host._arrival_rows = rows
        self._host._index_waves()
        plans = self._host._wave_plans()
        self._host._auto_label_waves(plans)
        self._host.race_strip.set_chips([(int(row.event.timeline_timestamp_ms), row.bib,
                                    AUTO_LABELLED if row.key in self._host._auto_labels or row.decision and row.decision.get("auto_label")
                                    else row.decision is not None)
                                   for row in rows if row.event is not None and row.event.is_active and row.note != "已排除"])
        positions = {}
        for row in rows:
            if row.event is None or not row.event.is_active:
                continue
            person = self._host._auto_labels.get(row.key)
            saved_frame = (row.decision or {}).get("frame") or {}
            if person is not None:
                stamp = person.chip_ms
            elif saved_frame.get("marker_x_normalized") is not None and row.time_ms is not None:
                # _build_rows already resolves legacy recorder times versus
                # saved calibrated crossing times. Do not recalibrate twice.
                stamp = row.time_ms
            else:
                continue
            positions[(int(row.event.timeline_timestamp_ms), row.bib)] = stamp
        self._host.race_strip.view.set_line_positions(positions)
        outdated = set()
        for index, plan in plans.items():
            if plan.marks or plan.split:
                if self._host._wave_outdated(plan):
                    outdated.add(index)
                if not self._host._unseen(plan):
                    # Looked through and counted, yet nobody for these chips: one
                    # rider with two chips, a chip without a rider, or someone missed.
                    for chip in plan.unmatched:
                        if chip.decision is None and not chip.note:
                            chip.note = "疑似双芯片"
        rows = self._host._arrival_rows = self._host._with_extra_rows(rows, plans)
        self._host._simultaneous = Counter(row.time_ms for row in rows if row.decision)
        self._host.rows = [row.item for row in rows]
        groups = [self._host._row_group(row) for row in rows]
        counted = {index: plan.matches and index not in outdated
                   for index, plan in plans.items() if plan.marks or plan.split}
        busy = self._host._earlier_shown = frozenset(key for key, arrival in self._host._earlier.items() if arrival is not None)
        signature = (self._host._arrival_scope, self._host._selected_key, tuple(sorted(counted.items())), self._host._attention_only,
                     self._host._show_excluded,
                     busy, self._host._near_counted_items, tuple(sorted(self._host._target_laps.items())),
                     self._host._show_unverified, self._host._unverified_items, self._host._short_bibs,
                     tuple(sorted((index, reasons) for index, reasons in self._host._auto_reasons.items())),
                     tuple(sorted((key, person.key) for key, person in self._host._auto_labels.items())),
                     tuple((row.key, row.bib, row.time_ms, row.reference, self._host._row_state(row), self._host._row_category(row),
                            candidate_binding(row.item), self._host._wave_of.get(row.key), group, row.carrier,
                            row.event.lap if row.event is not None else None,
                            tuple(piece.key for piece in row.fragments))
                           for row, group in zip(rows, groups)))
        if self._host._quick_ready and self._host.race_strip.view.pieces and not self._host._earlier_timer.isActive():
            self._host._earlier_timer.start()  # 可能多人 of crossings not read yet
        if signature == self._host._quick_render_signature:
            self._host._sync_arrival_confirmation()
            return True
        self._host._quick_render_signature = signature
        self._host._table_builds += 1
        # Who carries which second chip: shown on the carrier's row too.
        carried = {}
        for row in rows:
            if row.carrier and self._host._row_state(row) == "多余芯片":
                carried.setdefault(row.carrier, []).append(row.bib)
        several_groups = len({group for group in groups if group}) > 1
        self._host.table.setColumnHidden(4, not several_groups)
        show_laps = any(row.event is not None and self._host._target_laps.get(row.event.group_id, 0) > 1 for row in rows)
        self._host.table.setColumnHidden(5, not show_laps)
        # Keep the lap label visible without horizontal scrolling on laptop screens.
        self._host.table.setMinimumWidth((450 if several_groups else 340) + (50 if show_laps else 0))
        for widget in (self._host.table, self._host.table_column):
            widget.setMaximumWidth(540 if several_groups else 440)
        self._host.table.blockSignals(True)
        scroll = self._host.table.verticalScrollBar().value()
        self._host.table.setRowCount(len(rows))
        selected = -1
        chip_number = 0
        for index, row in enumerate(rows):
            state = self._host._row_state(row)
            # A chip row shows its chip time, so the column reads in the table's
            # order; the frame its rider was clicked on is in the tooltip.
            moment = row_moment(row)
            time = format_chip_time(moment) if moment is not None and state != "画面待核" else "—"
            category = self._host._row_category(row)
            number = ""
            if row.event is not None:
                chip_number += 1
                if category != "非运动员":
                    number = str(chip_number)
            shown_state = f"{state}·{len(row.fragments) + 1}段" if row.fragments else state
            arrival = self._host._earlier_arrival(row, read=False)
            if arrival is not None:
                shown_state = "可能多人"
            near_counted = state == "待定" and row.item.item_id in self._host._near_counted_items
            if near_counted:
                shown_state = "已点附近"
            if state == "多余芯片":
                shown_state = f"{row.carrier}号带的" if row.carrier else "双芯片"
            auto = state == "待定" and row.key in self._host._auto_labels
            if auto:
                shown_state = "自动对上"
            target = self._host._target_laps.get(row.event.group_id, 0) if row.event is not None else 0
            laps = f"{row.event.lap}/{target}" if target > 1 else ""
            for column, text in enumerate((number, row.bib or "—", time, shown_state, groups[index], laps)):
                from PyQt5.QtWidgets import QTableWidgetItem
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row.key)
                if column == 3:
                    foreground, background = {"自动对上": ("#3b5b7a", "#e6edf5"),
                                              "已确认": ("#126044", "#e5f4ec"),
                                              "已点": ("#126044", "#e5f4ec"),
                                              "已接受对应": ("#3b5b7a", "#e6edf5"),
                                              "已排除": ("#4b5563", "#f1f3f5"),
                                              "待定": ("#9a3412", "#ffedd5"),
                                              "画面待核": ("#4b5563", "#f1f3f5"),
                                              "多出": ("#9a3412", "#ffedd5"),
                                              "疑似漏读": ("#9a3412", "#ffedd5"),
                                              "疑似双芯片": ("#9a3412", "#ffedd5"),
                                              "补录": ("#9a3412", "#ffedd5"),
                                              "多余芯片": ("#9a3412", "#ffedd5")}["自动对上" if auto else state]
                    cell.setForeground(QColor(foreground))
                    cell.setBackground(QColor(background))
                    cell.setTextAlignment(Qt.AlignCenter)
                tooltip = (AUTO_TEXT if auto else
                           ("点击回看保存原帧" if row.decision else
                            f"点击回看这一刻（{'从过线那一刻看起' if not self._host._passage_lead(row) else f'从过线前 {self._host._passage_lead(row) / 1000:g} 秒看起'}）；"
                            "选中按 1/2/3/4 或右键处理这一行"))
                if row.note == "已排除":
                    tooltip = "本机排除（测试／非参赛芯片）；原始记录保留，不导出建议删除；按 4 撤回。"
                if row.mark is not None:
                    tooltip = "比芯片多点出的人：点这一行回到点他的那一帧；按 3 补录号码或 1 不是运动员，点错在画面上右键去掉"
                tooltip = {"疑似漏读": "画面上有人却没有芯片：核对原帧号码后按 3 补录号码，导出到补录清单；"
                                       "不是运动员按 1",
                           "补录": "已填号码，导出到补录清单；按 3 改号码，4 撤回",
                           "疑似双芯片": "本波点完、看完，这条芯片没有对上的人：漏点了就回画面补点；"
                                         "是别人带的第二个芯片就按 2 填携带人",
                           "多余芯片": f"{self._host._carrier_text(row)}，导出到建议删除清单（CycleRace 人工处理）；"
                                       "按 2 改携带人，4 撤回",
                           }.get(state, tooltip)
                if self._host._unlike_athlete(row):
                    tooltip = self._host._unlike_athlete(row) + chr(10) + tooltip
                if row.bib and row.bib in carried and row.event is not None:
                    tooltip = f"{row.bib} 号另带 {'、'.join(carried[row.bib])} 号芯片（双芯片）\n" + tooltip
                if arrival is not None:
                    tooltip = (f"可能多人：过线前 {(row.time_ms - arrival) / 1000:.1f} 秒线上还有人到，"
                               "看到几个人就点几个\n" + tooltip)
                if near_counted:
                    tooltip = NEAR_COUNTED_TEXT + "\n" + tooltip
                if auto:
                    tooltip = AUTO_TEXT
                elif state == "待定" and self._host._auto_reasons.get(self._host._wave_of.get(row.key)):
                    tooltip = ("未自动对上：" + "；".join(self._host._auto_reasons[self._host._wave_of[row.key]])
                               + "。请逐个点\n" + tooltip)
                if row.fragments:
                    tooltip = (f"同一次过线被拆成 {len(row.fragments) + 1} 段画面，合成这一行"
                               f"（{(row.fragments[-1].time_ms - row.time_ms) / 1000:.1f} 秒）\n"
                               f"看完首段前 {self._host._passage_lead(row) / 1000:g} 秒到末段后 "
                               f"{PASSAGE_VIEW_MARGIN_MS / 1000:g} 秒，画面上看到几个人就点几个；"
                               "按 1（不是运动员）自动补看没看的部分再排除整行，4 或退格撤回整行\n"
                               + tooltip)
                wave = self._host._row_wave(row) if row.key in self._host._wave_of else None
                if column == 0 and wave is not None and len(wave) > 1:
                    # Neighbouring waves alternate shades, so where one ends shows.
                    cell.setBackground(QColor("#e3ebfb" if self._host._wave_of[row.key] % 2 else "#eee5fb"))
                    tooltip = f"本波 {len(wave)} 个芯片：{wave[0].bib} → {wave[-1].bib}\n" + tooltip
                if column == 0 and self._host._wave_of.get(row.key) in counted:
                    # The wave's count against its chips: green when they agree, red when not.
                    matches = counted[self._host._wave_of[row.key]]
                    cell.setForeground(QColor("#126044" if matches else "#b42318"))
                    font = cell.font()
                    font.setBold(True)
                    cell.setFont(font)
                    tooltip = ("本波人数与芯片数一致\n" if matches else
                               "芯片有变化：打开这一波按回车，按新顺序重存\n" if self._host._wave_of[row.key] in outdated
                               else "本波人数与芯片数不一致（多人或少人）\n") + tooltip
                if column == 2 and row.time_ms is not None and (
                        moment != row.time_ms or row.recorder_ms is not None and int(row.recorder_ms) != moment):
                    tooltip = (f"判读时间（校时后）：{format_chip_time(row.time_ms)}\n"
                               + (f"录像原时刻：{format_chip_time(row.recorder_ms)}\n"
                                  if row.recorder_ms is not None else "") + tooltip)
                if state == "画面待核":
                    tooltip = ("仅检测到画面变化，尚未确认目标穿越；不计到达，不生成过线时间。\n"
                               + (f"录像变化时刻：{format_chip_time(row.recorder_ms)}\n"
                                  if row.recorder_ms is not None else "")
                               + "点击回看；确实有人时在原帧点人，疑点和原始证据均保留。")
                cell.setToolTip(tooltip)
                self._host.table.setItem(index, column, cell)
            if row.key == self._host._selected_key or any(piece.key == self._host._selected_key for piece in row.fragments):
                selected = index
        attention = [self._host._attention(row, outdated, counted) for row in rows]
        for index, need in enumerate(attention):
            self._host.table.setRowHidden(index, (self._host._row_state(rows[index]) == "已排除" and not self._host._show_excluded)
                                    or (self._host._attention_only and need is None
                                        and not (self._host._show_excluded and self._host._row_state(rows[index]) == "已排除")))
        self._host._sync_attention_button(attention)
        if selected >= 0:
            self._host._selected_key = rows[selected].key
            self._host.table.setCurrentCell(selected, 0)
        elif rows:
            self._host.table.setCurrentCell(0, 0)
        self._host.table.verticalScrollBar().setValue(scroll)
        self._host.table.blockSignals(False)
        confirmed = sum(row.decision is not None for row in rows)
        excluded_count = sum(self._host._row_state(row) == "已排除" for row in rows)
        unverified_count = sum(self._host._row_state(row) == "画面待核" for row in rows)
        arrivals = len(rows) - excluded_count - unverified_count
        self._host.quick_title.setText(f"到达 {arrivals} · 已对上 {confirmed}"
                                 + (f" · 圈数不足 {len(self._host._short_bibs)} 人" if self._host._short_bibs else ""))
        details = [f"到达记录 {arrivals} · 人工已点 / 确认 {confirmed} · 自动对上 {len(self._host._auto_labels)}",
                   "灰蓝虚线：自动对应，非人工确认；绿色实线：人工已点 / 确认"]
        if excluded_count:
            details.append(f"已排除 {excluded_count} 条（仅隐藏显示，更多 → 显示已排除）")
        if self._host._target_laps:
            details.append("显示全部圈次；圈数 k/N 仅作提示，不隐藏芯片记录")
        if self._host._unverified_items:
            details.append(f"待核画面 {self._host._unverified_groups} 组、{len(self._host._unverified_items)} 段不计到达；更多 → 显示待核画面")
        if self._host._short_bibs:
            details.append("圈数不足（只提示，不判定；可能未完赛或漏读一圈）：" + "、".join(self._host._short_bibs) + " 号")
        self._host.quick_title.setToolTip("\n".join(details))
        unknown_laps = any(row.event is not None and row.event.is_active and self._host._row_state(row) != "已排除"
                           and self._host._target_laps.get(row.event.group_id, 0) <= 0 for row in rows)
        self._host.lap_status.setText("圈数未知 · 显示全部圈次" if unknown_laps else "")
        self._host.lap_status.setToolTip("未收到当前组别的有效目标圈数；所有圈次仍正常显示，请检查 CycleRace 赛事同步。"
                                   if unknown_laps else "")
        count = self._host._unverified_groups
        self._host.show_unverified_action.setText(f"显示待核画面（{count} 组/{len(self._host._unverified_items)} 段，不计到达）")
        self._host.quick_title.setText(self._host.quick_title.text() + (f" · 待核画面 {count}组" if count else ""))
        self._host.refresh_status_visibility()
        self._host.summary.setText(self._host.quick_title.text())
        self._host.summary_changed.emit(self._host.quick_title.text())
        first_visible = next((i for i in range(len(rows)) if not self._host.table.isRowHidden(i)), None)
        if self._host._active_item is None:
            if first_visible is not None:
                self._host._open_row(first_visible)
            else:
                self._host.table.setCurrentCell(-1, -1)
                self._host.table.clearSelection()
                self._host._selected_key = None
        elif self._host._active_item is not None:
            fresh = next((row.item for row in rows if row.item.item_id == self._host._active_item.item_id), None)
            if fresh is None:
                # A piece just folded into the row of its passage: the row stays open, on this piece.
                fresh = next((piece.item for row in rows for piece in row.fragments
                              if piece.item.item_id == self._host._active_item.item_id), None)
            if fresh is None and self._host._active_item.item_id in self._host._candidate_wave:
                # A wave's counting picture has no row of its own; it stays open.
                fresh = next((item for item in self._host.items if item.item_id == self._host._active_item.item_id), None)
            if fresh is None:
                self._host._active_item = None
                if first_visible is not None:
                    self._host._open_row(first_visible)
                else:
                    self._host.current_changed.emit(None)
            else:
                changed = candidate_binding(fresh) != candidate_binding(self._host._active_item)
                self._host._active_item = fresh
                if changed:
                    self._host.current_changed.emit(fresh)
        self._host._sync_arrival_confirmation()
        return True

    def _open_row(self, index, keep=True):
        if not 0 <= index < len(self._host._arrival_rows):
            return
        row = self._host._arrival_rows[index]
        self._host._selected_key = row.key
        self._host._active_item = row.item
        self._host._shown_id = row.item.item_id
        self._host.table.setCurrentCell(index, 0)
        self._host._sync_timeline()
        self._host._present(row.item, self._host._landing(row, keep))
        self._host._sync_arrival_confirmation()
        self._host.marks_changed.emit(tuple(self._host.current_marks()))

    def _strip_ready(self):
        """The strip was still being read when the row was opened (at start): land it now."""
        key, self._host._landing_pending = self._host._landing_pending, None
        self._host.refresh_calibration_state()
        self._host._earlier_timer.start()  # 可能多人 of the crossings, read off the strip
        row = self._host._selected_arrival()
        if key is not None and row is not None and row.key == key:
            landing = self._host._landing(row, keep=False)
            if isinstance(landing, dict):
                self._host.jump_requested.emit(landing)

    def _present(self, item, landing):
        self._host.holds_picture = landing is not None
        try:
            self._host.current_changed.emit(item)
        finally:
            self._host.holds_picture = False
        if isinstance(landing, dict):
            self._host.jump_requested.emit(landing)

    def open_wave_start(self):
        """回到团首: the selected row's wave, from its first rider at the line."""
        row = self._host._selected_arrival()
        landing = self._host._landing(row, keep=False) if row is not None and not row.decision else None
        if isinstance(landing, dict):
            self._host.jump_requested.emit(landing)
            return True
        return False

    def _seek_race_strip(self, frame):
        if frame is None:
            self._host.hint.setText("这一刻没有可用的过线图原录像；灰色空档不代表无人过线。")
            return
        # Browsing the whole race is not confirmation of the row still
        # selected on the left; neither its identity nor its marks change.
        self._host.jump_requested.emit(dict(frame, reference_only=True))
        self._host.race_strip.view.setFocus(Qt.MouseFocusReason)
        self._host.hint.setText("已跳到这一刻（只看，不确认）")

    def open_picture(self, item):
        """Make a wave's counting picture the one clicks go onto, without moving the frame shown."""
        if self._host._active_item is not None and self._host._active_item.item_id == item.item_id:
            return
        self._host._active_item = item
        self._host._shown_id = item.item_id
        self._host._present(item, KEEP)
        self._host._sync_arrival_confirmation()

    def select_item(self, item_id):
        # A wave's counting picture is shared by its confirmed rows: selecting the
        # picture must not reopen one of them and pull the frame back to where that
        # person was counted (古浪 261).
        counting = item_id in self._host._candidate_wave
        index = next((i for i, row in enumerate(self._host._arrival_rows)
                      if row.item.item_id == item_id and not (counting and (row.decision or row.mark))), None)
        if index is None:  # a piece of a folded passage opens its row
            index = next((i for i, row in enumerate(self._host._arrival_rows)
                          if any(piece.item.item_id == item_id for piece in row.fragments)), None)
        if index is not None:
            self._host._arrival_clicked(index)

    def _arrival_clicked(self, index, _column=0):
        if not 0 <= index < len(self._host._arrival_rows):
            return
        if self._host._auto_review is not None:
            self._host._stop_auto_review("换了一行，自动补看已停止；这一行未排除。")
        self._host.stop_run()
        self._host._last_saved_key = None
        row = self._host._arrival_rows[index]
        if row.decision:
            self._host._open_row(index)
            # Backspace now takes back this row's person, not the last one clicked.
            self._host._editing_mark = next((i for i, mark in enumerate(self._host.current_marks())
                                       if self._host._decision_holds(row.decision, mark)), -1)
            return
        candidate = self._host._wave_candidate(self._host._row_wave(row))
        if candidate is not None:
            # A wave is judged as a whole on its one picture.
            self._host._selected_key = row.key
            self._host.table.setCurrentCell(index, 0)
            landing = self._host._landing(row)
            if self._host._active_item is None or self._host._active_item.item_id != candidate.item_id:
                self._host._active_item = candidate
                self._host._shown_id = candidate.item_id
                self._host._present(candidate, landing)
            elif isinstance(landing, dict):
                self._host.jump_requested.emit(landing)
            if row.mark is not None:
                self._host._editing_mark = next((i for i, mark in enumerate(self._host.current_marks()) if mark is row.mark), -1)
                self._host.hint.setText(self._host.picture_mark_hint())
            else:
                self._host._show_row_hint(row)
            self._host._sync_arrival_confirmation()
            self._host.marks_changed.emit(tuple(self._host.current_marks()))
            return
        # Preserve a deliberately classified picture while assigning identities,
        # but not once everyone on it has a row: the next chip is someone else.
        # Only a chip can be assigned so: a row without one opens its own picture.
        item = self._host.current_item()
        people = max(1, sum(not is_excluded(mark) for mark in self._host.current_marks()))
        if (row.event is not None and item is not None and item.item_id not in self._host._candidate_wave
                and len(self._host._decided.get(item.item_id, ())) < people):
            category = self._host._category()
            frame = self._host._frame_provider()
            circle = item.evidence.get("frame", {}).get("marker_x_normalized") is not None
            if ((circle or category and category.get("action") == "bicycle") and frame
                    and frame.get("frame_index", -1) >= 0):
                self._host._selected_key = row.key
                self._host.table.setCurrentCell(index, 0)
                self._host._sync_arrival_confirmation()
                self._host.marks_changed.emit(tuple(self._host.current_marks()))
                self._host.hint.setText("已选中；在画面上点压线的人即保存。")
                return
        self._host._open_row(index)
        self._host._show_row_hint(row)

    def _show_row_hint(self, row):
        """What settles a row still to handle at the end (see 只看要处理的)."""
        if row.note == "已排除":
            self._host.hint.setText("本机已排除 · 原始记录保留 · 不导出 · 4 撤销")
            return
        text = {"画面待核": "仅有画面变化，不计到达：看完整段后按 1 排除，4 撤销；确实有人就在原帧点人。",
                "疑似漏读": "疑似漏读：核对原帧上的号码，按 3 补录号码（导出到补录清单）；不是运动员按 1。",
                "补录": f"补录 {row.bib} 号：按 3 改号码，4 撤回。",
                "疑似双芯片": f"{row.bib} 号这一波看完了却没对上人：漏点了就在画面上补点；"
                              "是别人带的第二个芯片，就按 2 填携带人。",
                "多余芯片": f"{self._host._carrier_text(row)}（建议删除）；按 2 改携带人，4 撤回。"}.get(self._host._row_state(row))
        arrival = self._host._earlier_arrival(row, read=False)
        if arrival is not None:
            text = (f"可能多人：过线前 {(row.time_ms - arrival) / 1000:.1f} 秒线上还有人到，"
                    "已从那时放起；看到几个人就点几个。")
        if self._host._unlike_athlete(row):
            text = self._host._unlike_athlete(row) + (text or "")
        if self._host._row_state(row) == "待定" and row.item.item_id in self._host._near_counted_items:
            text = NEAR_COUNTED_TEXT
        if self._host._row_state(row) == "待定" and row.key in self._host._auto_labels:
            text = AUTO_GO_ON
        if text:
            self._host.hint.setText(text)

    def _unlike_athlete(self, row):
        """11-12: the analysis thinks this one is not an athlete, but the operator decides."""
        reason = row.item.evidence.get("unlike_athlete") if row is not None and row.event is None else None
        if not reason or self._host._row_state(row) not in ("待定", "疑似漏读"):
            return ""
        return f"机器看着不像选手（{reason}），由你判断：工作人员按 1，是选手按 3 补录号码。"

    def _row_menu(self, pos):
        """Right click on a row of the left table: what that row still needs, and nothing else."""
        index = self._host.table.rowAt(pos.y())
        if not 0 <= index < len(self._host._arrival_rows):
            return
        if self._host._arrival_rows[index].key != self._host._selected_key or self._host.table.currentRow() != index:
            self._host._arrival_clicked(index)
        row = self._host._selected_arrival()
        actions = self._host._row_actions(row) if row is not None else []
        if not actions:
            self._host.hint.setText("这一行没有要处理的。")
            return
        menu = QMenu(self._host.table)
        for label, action in actions:
            key = next((key for key, labels in ROW_KEYS.items() if label in labels), None)
            menu.addAction(label + (f"\t{KEY_NAMES[key][0]}" if key is not None else ""), action)
        menu.exec_(self._host.table.viewport().mapToGlobal(pos))

    def _row_key(self, key):
        """1 / 2 / 3 on the selected row: what its right click offers under that name, or why not."""
        row = self._host._selected_arrival()
        if row is None:
            self._host.hint.setText("先在左边选一行（或在画面上点中这个人），再按 " + KEY_NAMES[key])
            return
        if row.note == "已排除":
            self._host.hint.setText("本机已排除 · 4 撤销")
            return
        actions = self._host._row_actions(row)
        action = next((action for label, action in actions if label in ROW_KEYS[key]), None)
        if action is not None:
            action()
            return
        offered = [name for other, name in KEY_NAMES.items()
                   if any(label in ROW_KEYS[other] for label, _ in actions)]
        offered += ["4 撤销"] if any(label.startswith("撤") for label, _ in actions) else []
        if (key != Qt.Key_2 and row.event is not None and row.event.is_active
                and self._host._row_state(row) != "多余芯片" and self._host._counted_person(row) is None):
            text = f"{row.bib} 号还没点人：先在画面上点中这个人，再按 {KEY_NAMES[key][0]}。"
        elif key == Qt.Key_2 and row.event is None:
            text = "双芯片用在芯片行：选中那条多出来的芯片再按 2。"
        else:
            text = f"这一行不能按 {KEY_NAMES[key][0]}。"
        self._host.hint.setText(text + (f"这一行可以按：{'、'.join(offered)}。" if offered else ""))

    def _row_actions(self, row):
        """(label, action) a right click on ROW offers, from the row's own state only (10-29).

        The picture only counts people; whatever a row still needs (a bib for
        someone without a chip, whose second chip this is, 不是运动员) is said here.
        """
        key, state = row.key, self._host._row_state(row)
        if row.event is not None:
            if not row.event.is_active:
                return []
            if row.note == "已排除":
                return [("撤销本机排除", lambda: self._host._undo_key(key))]
            if state == "多余芯片":
                return [("改携带人…", lambda: self._host._mark_extra_chip(key)),
                        ("撤回双芯片", lambda: self._host._take_back_extra_chip(key))]
            counted = self._host._counted_person(row) is not None
            actions = [("这人没芯片：补录号码…", lambda: self._host._person_no_chip(key)),
                       ("这人不是运动员", lambda: self._host._person_flag(key, "exclusion_reason"))] if counted else []
            if not counted and row.decision is None:
                actions.append(("非运动员（本机排除）", lambda: self._host._exclude_local_chip(key)))
            actions.append(("双芯片：这条芯片是谁带的…", lambda: self._host._mark_extra_chip(key)))
            if counted:
                actions.append(("撤销：去掉这个人", lambda: self._host._drop_person(key)))
            elif row.decision is not None:
                actions.append(("撤销这一行", lambda: self._host._undo_key(key)))
            return actions
        if row.mark is not None:  # 多出: counted beyond the wave's chips
            return [("补录号码…", lambda: self._host._person_no_chip(key)),
                    ("不是运动员", lambda: self._host._person_flag(key, "exclusion_reason")),
                    ("撤销：去掉这个人", lambda: self._host._drop_person(key))]
        if state == "补录":
            return [("改号码…", lambda: self._host._enter_bib(self._host._row_by_key(key))),
                    ("撤回号码", lambda: self._host._undo_bib(self._host._row_by_key(key)))]
        if state == "疑似漏读":
            return [("补录号码…", lambda: self._host._enter_bib(self._host._row_by_key(key))),
                    ("不是运动员", lambda: self._host._miss_not_rider(key)),
                    ("撤回疑似漏读", lambda: self._host._take_back_miss(key))]
        if state == "已排除":
            return [("撤销排除", lambda: self._host._undo_key("candidate:" + row.item.item_id))]
        if state == "画面待核":
            return [("不是运动员", lambda: self._host._candidate_not_rider(key))] if self._host._excludable(row) else []
        if self._host._excludable(row):
            actions = [("补录号码…", lambda: self._host._record_candidate(key)),
                       ("不是运动员", lambda: self._host._candidate_not_rider(key))]
            if len(self._host._exclusion_run()) > 1:
                actions.append(("连续排除到下一个芯片", self._host._exclude_run))
            return actions
        return []

    def _selected_arrival(self):
        index = self._host.table.currentRow()
        return self._host._arrival_rows[index] if 0 <= index < len(self._host._arrival_rows) else None

    def _mark_target_row(self, key, kind="next"):
        """The row of the person the operator is on, bold: yellow to click, green once clicked."""
        state = (self._host._table_builds, key, self._host._selected_key, kind)
        if state == self._host._target_row_state:
            return
        if self._host._target_row_state is not None and self._host._target_row_state[0] == self._host._table_builds:
            for cell, brush, font, pen in self._host._target_cells:
                cell.setBackground(brush)
                cell.setForeground(pen)
                cell.setFont(font)
        self._host._target_row_state, self._host._target_cells = state, []
        index = next((i for i, row in enumerate(self._host._arrival_rows) if row.key == key), None) if key else None
        if index is not None:
            for column in range(self._host.table.columnCount()):
                cell = self._host.table.item(index, column)
                if cell is None:
                    continue
                self._host._target_cells.append((cell, cell.background(), cell.font(), cell.foreground()))
                font = cell.font()
                font.setBold(True)
                cell.setFont(font)
                cell.setBackground(QColor("#7fdca4" if kind == "done" else "#ffd84d"))
        selected = self._host._selected_arrival()
        style = ((self._host.DONE_ROW_STYLE if kind == "done" else self._host.TARGET_ROW_STYLE)
                 if key and selected is not None and selected.key == key
                 else self._host.PLAIN_ROW_STYLE)
        if style != self._host.table.styleSheet():
            self._host.table.setStyleSheet(style)

    def refresh_status_visibility(self):
        labels = [label for label in self._host.status_widget.findChildren(QLabel)
                  if label is not self._host.finish_line_status and label is not self._host.confirmation_status]
        self._host.confirmation_status.setVisible(False)  # said beside 本波 when it matters (11-14)
        for label in labels:
            label.setVisible(bool(label.text()))
        self._host.status_widget.setVisible(any(label.text() for label in labels))

    def refresh_calibration_state(self):
        """Always say whether the current recording is calibrated, from whom and by how much.

        11-18 "目前我不知道是否校时了": red 未校时 until the latest recording
        has a clock (the next left click on the person at the line then
        calibrates from that person's chip); green 已校时 with the bib and the
        delay after.  Clicking it arms 重新校时 for the next picture click.
        The latest strip piece is the current recording: an earlier, short
        session left uncalibrated only keeps its grey on the strip.  Without a
        strip yet, any saved first-rider offset or fitted clock counts.
        """
        pieces = self._host.race_strip.view.pieces
        workspace = self._host.workspace_dir
        camera, offset = None, None
        viewed = getattr(self._host, "_viewed_clock", None)
        if viewed is not None:
            camera, aligned, shown_offset = viewed
            needed, offset = not aligned, (shown_offset if aligned else None)
        elif pieces:
            latest = max(pieces, key=lambda piece: (piece.start_ms, piece.archive))
            needed = not latest.aligned
            camera, offset = latest.camera, (latest.offset_ms if latest.aligned else None)
        else:
            saved = manual_calibrations(workspace) if workspace is not None else {}
            needed = workspace is None or not (saved or workspace_clocks(workspace))
            if saved:
                newest = max(saved.values(), key=lambda click: click.calibrated_at_ms)
                offset = newest.offset_ms
                camera = next(key[0] for key, click in saved.items() if click is newest)
        bib = ""
        if offset is not None and workspace is not None:
            clicks = [saved for (saved_camera, _session), saved in manual_calibrations(workspace).items()
                      if saved_camera == camera and abs(saved.offset_ms - offset) <= 1]
            bib = max(clicks, key=lambda saved: saved.calibrated_at_ms).bib if clicks else ""
        armed = bool(getattr(self._host, "_recalibrate_armed", False))
        # Compared at the 0.1 s shown: a fitted clock's drift must not restyle it every frame.
        state = (needed, None if offset is None else round(offset / 100), bib, armed)
        if state == getattr(self._host, "_calibration_state", None):
            return
        self._host._calibration_state = state
        self._host._calibration_needed, self._host._calibration_offset = needed, offset
        button = self._host.calibrate_button
        how = ("左键点下一位压线的人，就按他的芯片时间校时（芯片时间不变，只让画面对上芯片）。"
               "\n也可在「更多」里按列表选人校时。")
        if armed:
            button.setText("重新校时：点压线的人")
            button.setStyleSheet(
                "QPushButton{color:white; background:#b45309; font-weight:700; padding:2px 8px;"
                " border:1px solid #92400e; border-radius:3px;}")
            button.setToolTip("下一次左键点人会重新校时。再点这里或按 Esc 取消。\n" + how)
        elif needed:
            button.setText("未校时：点下一位压线的人")
            button.setStyleSheet(
                "QPushButton{color:white; background:#c2410c; font-weight:700; padding:2px 8px;"
                " border:1px solid #9a3412; border-radius:3px;}")
            button.setToolTip("本次录像还没校时：芯片标记和画面可能差几秒。\n" + how)
        else:
            words = ["已校时"]
            if bib:
                words.append(f"{bib} 号")
            if offset is not None:
                words.append(f"录像{'晚' if offset >= 0 else '早'} {abs(offset) / 1000:.1f} 秒")
            button.setText(" · ".join(words))
            button.setStyleSheet("color:#17675b; font-weight:600;")
            button.setToolTip("当前录像已校时，时间已扣除。不对就点这里，再左键点压线的人重新校时。\n" + how)

    def toggle_recalibration(self):
        """The calibrate button arms (or disarms) 重新校时 for the next picture click."""

        self._host._recalibrate_armed = not getattr(self._host, "_recalibrate_armed", False)
        self._host.refresh_calibration_state()
        self._host.hint.setText(
            "重新校时：把画面停在下一位压线的那一帧，左键点他。再点按钮或按 Esc 取消。"
            if self._host._recalibrate_armed else "已取消重新校时，校时不变。")

    def calibration_done(self):
        """A click just calibrated: disarm 重新校时 and show the new state at once."""

        self._host._recalibrate_armed = False
        self._host._quick_render_signature = None
        self._host.race_strip.load(self._host.workspace_dir, self._host.race_strip.camera)
        self._host.refresh_calibration_state()

    def set_finish_line_status(self, configured):
        self._host.finish_line_status.setText("" if configured else "未设终点线")
        self._host.finish_line_button.setVisible(not configured)
        self._host.refresh_status_visibility()

    def set_browsing(self, browsing):
        self._host._browsing = bool(browsing)

    def toggle_big_view(self):
        """Expand the evidence area while keeping the arrival list and actions visible."""

        self._host._big_view = not self._host._big_view
        self._host._sync_step_buttons()
        self._host._sync_arrival_confirmation()
        self._host.count_mode_changed.emit(self._host._big_view)
        if self._host._big_view:
            self._host.hint.setText("左键点压线的人即保存，右键去掉；1 不是运动员、2 双芯片、3 补录、4 撤销。Esc 返回。")
            self._host.setFocus()
        else:
            self._host.hint.setText("已返回胶卷视图；左键点人即保存；1 不是运动员、2 双芯片、3 补录、4 撤销。")
            self._host.table.setFocus()
        self._host.big_view_changed.emit(self._host._big_view)

    def _attention(self, row, outdated, counted):
        """What is left at the end: pending, done (to export), or nothing to do (None)."""
        if row.note == "已排除":
            return None
        state = self._host._row_state(row)
        if row.note == "已排除":
            return None
        if state in ("补录", "多余芯片"):
            return "done"
        wave = self._host._wave_of.get(row.key) if row.event is not None else None
        if wave is not None and wave in outdated:
            return "芯片有变化"  # saved before a chip came in: Enter on the wave saves it in chip order
        if (state in ("疑似漏读", "多出", "疑似双芯片", "画面待核") or row.stale
                or state == "待定" and (row.event is None or wave in counted)):
            return state
        return None

    def _sync_attention_button(self, attention):
        pending = Counter(need for need in attention if need not in (None, "done"))
        done = Counter(self._host._row_state(row) for row, need in zip(self._host._arrival_rows, attention) if need == "done")
        total = sum(pending.values())
        self._host.attention_button.setText(f"只看要处理的 {total}" if total else "只看要处理的")
        parts = [f"{state} {count}" for state, count in pending.items()]
        tooltip = ("要处理：" + "、".join(parts) if parts else "没有要处理的行") + (
            "\n已处理（会导出）：" + "、".join(f"{state} {count}" for state, count in done.items()) if done else "")
        idle = sum(row.event is not None and row.event.is_active and row.decision is None and need is None
                   and self._host._row_state(row) == "待定" for row, need in zip(self._host._arrival_rows, attention))
        if idle:
            tooltip += f"\n还没点人的波里有 {idle} 个芯片没对上人（不算在内）"
        self._host.attention_button.setToolTip(
            tooltip + "\n选中左边这一行按键处理（右键同样）：疑似漏读按 3 补录号码，疑似双芯片按 2 填是谁带的，多出按 3 补录或 1 不是运动员")

    def _select_next_visible(self, position):
        following = next((i for i in range(position + 1, len(self._host._arrival_rows))
                          if not self._host.table.isRowHidden(i)), None)
        if following is None:
            following = next((i for i in range(position - 1, -1, -1)
                              if not self._host.table.isRowHidden(i)), None)
        if following is not None:
            self._host._arrival_clicked(following)
            self._host.table.scrollToItem(self._host.table.item(following, 0))
        else:
            self._host.table.setCurrentCell(-1, -1)
            self._host.table.clearSelection()
            self._host._selected_key = None
            self._host._active_item = None
            self._host.current_changed.emit(None)
            self._host._sync_arrival_confirmation()

    def _set_show_excluded(self, on):
        self._host._show_excluded = bool(on)
        saved = self._host._last_saved_key
        self._host._quick_render_signature = None
        if self._host._render() is not False and self._host.table.isRowHidden(self._host.table.currentRow()):
            self._host._select_next_visible(self._host.table.currentRow())
        self._host._last_saved_key = saved  # Toggling display never consumes the immediate undo.

    def _set_show_unverified(self, on):
        self._host._show_unverified = bool(on)
        saved = self._host._last_saved_key
        self._host._quick_render_signature = None
        self._host._render()
        self._host._last_saved_key = saved

    def _set_attention_only(self, on):
        self._host._attention_only = bool(on)
        self._host._quick_render_signature = None
        self._host._render()
        row = self._host._selected_arrival()
        if on and (row is None or self._host.table.isRowHidden(self._host.table.currentRow())):
            position = next((i for i in range(len(self._host._arrival_rows)) if not self._host.table.isRowHidden(i)), None)
            if position is not None:
                self._host._arrival_clicked(position)
            else:
                self._host.hint.setText("没有要处理的了：可以在「更多」里导出给计时系统。")
