"""Judgments and undo on the quick review panel: confirming, rulings, chip exclusions, bibs and keys."""

from __future__ import annotations

import uuid

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QInputDialog

from .arrival_review import candidate_binding, event_binding
from .chip_history import format_chip_time
from .problem_items import STATUS_WAITING
from .quick_review_common import (
    BIB_CHIP_REACH_MS,
    EXCLUSION_RUN_GAP_MS,
    EXTRA_CHIP,
    LOCAL_CHIP_EXCLUSION,
    NUMBER_KEYS,
    PASSAGE_VIEW_MARGIN_MS,
    RECORD_BIB,
    ROW_KEYS,
)


class QuickReviewJudgmentsMixin:
    """Confirm arrivals, classify and save rulings, exclude chips, extra chips, bibs, undo and number keys."""

    def _confirm_selected_arrival(self):
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            self.hint.setText("这条芯片已在本机排除，按 4 撤回后再处理。")
            return
        if row is not None and row.key in self._auto_labels:
            self._accept_auto_wave(row)
            return
        plan = self._wave_plan()
        if plan is not None and plan.marks:
            # Everyone counted is saved already; Enter (or 1) goes on to the next wave.
            if self._save_wave(plan):
                self._next_wave(plan.item)
            return
        row = self._selected_arrival()
        if row is None:
            self.hint.setText("请先从左侧选择要确认的运动员。")
            return
        if row.event is None and self._row_state(row) in ("疑似漏读", "补录"):
            self._enter_bib(row)
            return
        self._confirm_arrival(row)

    def _enter_key(self):
        """Enter does what the selected row needs, never the old confirm.

        A 疑似漏读 / 补录 row: its bib.  A wave counted on its picture: saved in
        chip order and on to the next wave; a wave whose chips changed is saved
        again and stays, so the new bibs can be looked at.  Else the next row.
        """
        row = self._selected_arrival()
        if row is not None and row.event is None and self._row_state(row) in ("疑似漏读", "补录"):
            self._enter_bib(row)
            return
        if row is not None and row.key in self._auto_labels:
            self._accept_auto_wave(row)
            return
        plan = self._wave_plan()
        if plan is not None and (plan.marks or plan.split):
            outdated = self._wave_outdated(plan)
            if self._save_wave(plan):
                if outdated:
                    self.hint.setText("已按新的芯片顺序重存这一波，看一下号码；再按回车到下一波。")
                else:
                    self._next_wave(plan.item)
            return
        self._step(1)

    def _usable(self, item=None, *, action=""):
        item = item or self._candidate_item()
        if item is None or self._arrival_store is None:
            return False
        waiting_exclusion = action == "not_rider" and self._unverified_picture(item)
        if ((item.status == STATUS_WAITING and not waiting_exclusion)
                or item.evidence.get("configuration_stale") or item.evidence.get("superseded")):
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
        plan = self._wave_plan()
        if plan is not None and plan.marks:
            # Counted on the picture, so saved already.  A 1 out of habit must not
            # jump to the next wave before the rest of this one is counted.
            if self._save_wave(plan):
                self.hint.setText("点人时已自动保存；看完这一波按回车到下一波。")
            return
        row = self._selected_arrival()
        if row is not None and row.decision:
            self.hint.setText(f"{row.bib} 已确认；修改前请先撤销。")
            return
        self._shortcut_operation = uuid.uuid4().hex
        try:
            self._classify("bicycle")
            if self.confirm_arrival_button.isEnabled():
                self._confirm_selected_arrival()
            else:
                self.hint.setText("已标记运动员；请在左边选对应的芯片记录。")
        finally:
            self._shortcut_operation = None

    @staticmethod
    def _frame_usable(frame):
        """A frame of a recording a ruling can be saved on and reopened from."""
        index = frame.get("frame_index") if frame else None
        return bool(frame and index is not None and index >= 0 and frame.get("position_ms", -1) >= 0
                    and frame.get("segment_id") and frame.get("video_path"))

    def _row_frame(self, row):
        """The frame a ruling on ROW is saved on: the one shown if it is this row's, else the row's own.

        This row's: from where it is looked at from (a second at least) to a
        second after its last piece.
        """
        frame = self._frame_provider()
        last = (row.fragments[-1] if row.fragments else row).time_ms
        if (not self._frame_usable(frame) or self._shown_ms is None or row.time_ms is None
                or not row.time_ms - max(1000, self._passage_lead(row)) <= self._shown_ms <= last + 1000):
            frame = row.item.evidence.get("frame")
        return frame

    def _classify(self, action, row=None):
        """Rule on the candidate on screen; with ROW (1, its right click), on that row of the table.

        On screen, a frame browsed to becomes a candidate of its own first (the
        window's candidate_preparing: Enter on a moment dragged to).  A row is
        ruled on as it is, wherever the picture is: 10-29 a row opened before
        its own frame made a new 人工发现 row of every 1 pressed on it.
        """
        if action != "held" and row is None:
            self.candidate_preparing.emit()
        item = row.item if row is not None else self._candidate_item()
        if not self._usable(item, action=action):
            return
        frame = self._row_frame(row) if row is not None else self._frame_provider()
        # A classification belongs to the candidate currently on screen.  Do not
        # let a stale/partial provider result create an audit entry that cannot be
        # reopened later (``held`` is the sole action that may be frame-less).
        if action != "held" and not self._frame_usable(frame):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return
        if action == "not_rider" and any(row.decision and row.decision.get("candidate_id") == item.item_id
                                         for row in self._arrival_rows):
            self.hint.setText("本处已有选手确认，请先撤销误确认。")
            return
        row = row if row is not None else self._selected_arrival()
        members = [row.item, *(piece.item for piece in row.fragments)] if row is not None and row.fragments else []
        pieces = [member for member in members if member.item_id != item.item_id] if any(
            member.item_id == item.item_id for member in members) else []
        unseen = self._fragment_unseen(row) if pieces else []
        if unseen and action == "not_rider" and self._excludable(row):
            # A folded row is ruled on as a whole, so all of it is looked at
            # first: 3 plays the rest and excludes it when it is seen.
            self._start_auto_review([row.key])
            return
        if unseen and action != "held":
            # A folded row is ruled on as a whole, so all of it is looked at first.
            self.hint.setText(f"这一行 {len(pieces) + 1} 段画面还差 "
                              f"{sum(right - left for left, right in unseen) / 1000:.1f} 秒没看；"
                              f"看完首段前 {self._passage_lead(row) / 1000:g} 秒到末段后 "
                              f"{PASSAGE_VIEW_MARGIN_MS / 1000:g} 秒再判。")
            return
        operation = getattr(self, "_shortcut_operation", None) or (uuid.uuid4().hex if pieces else None)
        if not self._save_ruling(item, pieces, action, frame, operation):
            return
        self._render()
        self.hint.setText("已标为运动员；在画面上点压线的人即保存。" if action == "bicycle"
                          else "已暂放。" if action == "held" else "已排除此画面候选。")
        if action in ("held", "not_rider"):
            self._step(1)
        self._last_saved_key = "candidate:" + item.item_id

    def _save_ruling(self, item, pieces, action, frame, operation, **extra):
        try:
            frame = dict(frame or {})
            frame.setdefault("calibration_version", item.calibration_version)
            # One ruling for the row is saved on every piece of it, under one
            # operation, so Backspace takes the whole row back.
            for target in (item, *pieces):
                self._arrival_store.save(self._arrival_scope, "candidate:" + target.item_id,
                                         candidate_binding(target), action, candidate_id=target.item_id,
                                         frame=frame,
                                         analysis_signature=target.evidence.get("analysis_signature", ""),
                                         operation_id=operation,
                                         calibration_version=target.calibration_version, **extra,
                                         **({"fragment_of": item.item_id} if target is not item else {}))
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return False
        return True

    def _excludable(self, row):
        """Whether an undecided picture without a chip can be excluded with 1."""
        item = row.item
        state = self._row_state(row)
        status_allows = state == "画面待核" or item.status != STATUS_WAITING
        time_allows = state == "画面待核" or row.time_ms is not None
        return (row.event is None and row.mark is None and not row.decision
                and time_allows and state in ("待定", "画面待核") and bool(item.evidence.get("frame"))
                and status_allows and not item.evidence.get("configuration_stale")
                and not item.evidence.get("superseded")
                and not any(other.decision and other.decision.get("candidate_id") == item.item_id
                            for other in self._arrival_rows))

    def _exclusion_run(self):
        """The selected undecided picture and those after it, up to a chip or a long gap."""
        index = self.table.currentRow()
        if not 0 <= index < len(self._arrival_rows) or not self._excludable(self._arrival_rows[index]):
            return []
        run, last = [], None
        for row in self._arrival_rows[index:]:
            if row.event is not None or row.mark is not None:
                break  # a chip, or someone counted: a rider passed here
            if row.time_ms is None:
                continue
            if last is not None and row.time_ms - last > EXCLUSION_RUN_GAP_MS:
                break
            if self._excludable(row):
                run.append(row)
            last = (row.fragments[-1] if row.fragments else row).time_ms
        return run

    def _exclude_run(self):
        rows = self._exclusion_run()
        if not rows:
            self.hint.setText("连续排除从选中的待定画面行开始，排除后面没有芯片的待定行。")
            return
        self._start_auto_review([row.key for row in rows])

    def _take_back_no_chip(self, row):
        """撤回疑似漏读 on a picture of its own: back to 待定, its bib with it."""
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key="bib:" + row.item.item_id)
            self._arrival_store.undo(self._arrival_scope, key="candidate:" + row.item.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self.hint.setText("已撤回疑似漏读，这一行回到待定。")

    def _row_by_key(self, key):
        return next((row for row in self._arrival_rows if row.key == key), None)

    def _carrier_text(self, row):
        """What a 双芯片 row says: whose second chip it is."""
        return (f"{row.bib} 号芯片由 {row.carrier} 号携带（一人双芯片）" if row.carrier
                else f"{row.bib} 号记为双芯片（多余芯片）")

    def _exclude_local_chip(self, key):
        """Exclude just this chip locally, without inventing a video crossing."""
        row = self._row_by_key(key)
        if (row is None or row.event is None or not row.event.is_active or self._arrival_store is None
                or row.decision is not None or self._counted_person(row) is not None or row.note == "多余芯片"):
            self.hint.setText("已有点人或确认记录，请先按 4 撤销，再做本机排除。")
            return
        scope, binding = self._arrival_scope, event_binding(row.event)
        store = self._arrival_store
        if self._render() is False:
            return
        row = self._row_by_key(key)
        if (self._arrival_scope != scope or self._arrival_store is not store or row is None or row.event is None
                or not row.event.is_active or event_binding(row.event) != binding or row.decision is not None
                or self._counted_person(row) is not None or row.note == "多余芯片"):
            self.hint.setText("这条芯片或处理状态已变化，未排除，请重新核对。")
            return
        try:
            store.save(scope, key, binding, "held", reason=LOCAL_CHIP_EXCLUSION,
                       exclusion_reason="非运动员（本机排除）", event_id=row.event.event_id, bib=row.bib,
                       operation_id=uuid.uuid4().hex)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"本机排除未保存：{error}")
            return
        self._quick_render_signature = None
        if self._render() is False:
            self._last_saved_key = key
            return
        if self._row_by_key(key) is not None and self._row_by_key(key).note == "已排除":
            position = next(i for i, value in enumerate(self._arrival_rows) if value.key == key)
            self._select_next_visible(position)
            self._last_saved_key = key
            self.hint.setText(f"{row.bib} 号已在本机排除 · 原始记录保留 · 4 撤销")

    def _ask_carrier(self, bib, current):
        text, ok = QInputDialog.getText(self, "双芯片", f"{bib} 号芯片是谁带的？核对后填携带人的号码：",
                                        text=current)
        return text if ok else None

    def _mark_extra_chip(self, key):
        """双芯片 (right click on a chip row): the chip is a second chip of the rider with the bib given.

        Asked from the row alone, without the picture (10-29: "直接点 8 号右键
        双芯片"); the carrier's bib is kept with it and exported.  The wave's
        people move on to the chips left.  Asked again it changes the carrier.
        """
        row = self._row_by_key(key)
        if row is None or row.event is None or not row.event.is_active or self._arrival_store is None:
            self.hint.setText("双芯片用在左边的芯片行：选中那条多出来的芯片按 2（或右键）。")
            return
        plan = self._counted_plan(row)
        if row.decision is not None and not (plan is not None and self._counted_here(row, plan.item)):
            self.hint.setText(f"{row.bib} 号已在别处确认；要改先撤销这一行。")
            return
        editing = row.note == "多余芯片"
        carrier = self._ask_carrier(row.bib, row.carrier if editing else "")
        if carrier is None:
            return
        carrier = carrier.strip()
        if not carrier:
            self.hint.setText("没有填携带人，未改动。")
            return
        if carrier == row.bib:
            self.hint.setText(f"携带人不能是 {row.bib} 号自己：填带着这条芯片的那个人的号码。")
            return
        scope = self._arrival_scope
        # The frame of the carrier where he was counted, if he was: the evidence of who wore it.
        holder = next((other for other in self._arrival_rows if other.event is not None and other.bib == carrier
                       and other.decision and (other.decision.get("frame") or {}).get("video_path")), None)
        frame = dict(holder.decision["frame"]) if holder is not None else (
            dict(plan.item.evidence.get("frame") or {}) if plan is not None else {})
        try:
            if self._arrival_store.latest(scope, key) is not None:
                self._arrival_store.undo(scope, key=key)  # its person, or the old carrier, gives way
            self._arrival_store.save(scope, key, event_binding(row.event), "held", reason=EXTRA_CHIP,
                                     event_id=row.event.event_id, bib=row.bib, carrier_bib=carrier,
                                     candidate_id=plan.item.item_id if plan is not None else "",
                                     frame=frame, calibration_version=plan.item.calibration_version
                                     if plan is not None else "")
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"未保存：{error}")
            return
        bib = row.bib
        self._after_chip_change(key)
        wave = self._row_wave(self._row_by_key(key)) or ()
        text = f"{bib} 号芯片记为 {carrier} 号带的（一人双芯片），导出到建议删除清单；按 2 改携带人，4 撤回。"
        if carrier not in {chip.bib for chip in wave}:
            text += f"注意：{carrier} 号不在这一波的芯片里，请核对号码。"
        self.hint.setText(text)

    def _take_back_extra_chip(self, key):
        row = self._row_by_key(key)
        if row is None or row.note != "多余芯片" or self._arrival_store is None:
            return
        try:
            self._arrival_store.undo(self._arrival_scope, key=key)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        bib = row.bib
        self._after_chip_change(key, saved=False)
        self.hint.setText(f"已撤回：{bib} 号不再记为双芯片。")

    def _after_chip_change(self, key, saved=True):
        """A chip of a wave turned 双芯片 or back: the people counted line up with the chips left."""
        self._quick_render_signature = None
        self._render()
        plan = self._counted_plan(self._row_by_key(key))
        if plan is not None:
            self._save_wave(plan)
        self._select_key(key)
        self._last_saved_key = key if saved else None

    def _select_key(self, key):
        position = next((i for i, other in enumerate(self._arrival_rows) if other.key == key), None)
        if position is not None:
            self._selected_key = key
            self.table.setCurrentCell(position, 0)
            self._sync_arrival_confirmation()

    def _enter_bib(self, row):
        """Enter on a 疑似漏读 row: the bib read off the frame; it goes to the 补录 list."""
        if row is None or row.event is not None or self._row_state(row) not in ("疑似漏读", "补录"):
            self.hint.setText("补录号码用在左边的疑似漏读行：选中按 3（或右键）。")
            return
        bib = self._ask_bib(row.bib if row.entered is not None else "")
        if bib is None:
            return
        bib = bib.strip()
        if bib:
            self._save_bib(row, bib)
        elif row.entered is not None:
            self._undo_bib(row)

    def _ask_bib(self, current):
        text, ok = QInputDialog.getText(self, "填写号码", "核对原帧上的号码后填写（导出到补录清单）：", text=current)
        return text if ok else None

    def _save_bib(self, row, bib):
        if row.time_ms is not None:
            chip = next((other for other in self._arrival_rows if other.event is not None and other.event.is_active
                         and other.bib == bib and other.note not in ("多余芯片", "已排除")
                         and abs(int(other.event.timeline_timestamp_ms) - int(row.time_ms)) <= BIB_CHIP_REACH_MS),
                        None)
            if chip is not None:
                self.hint.setText(f"{bib} 号在 {format_chip_time(chip.event.timeline_timestamp_ms)} 有芯片，不是漏读："
                                  "号码看错就重填；确实是他，就右键这一行「撤回疑似漏读」，让他占这个芯片。")
                return
        twice = next((other for other in self._arrival_rows
                      if other is not row and other.entered is not None and other.bib == bib), None)
        if twice is not None:
            self.hint.setText(f"{bib} 号已在 {format_chip_time(twice.time_ms)} 补录过：同一人只补录一次；"
                              "要改先在那一行按 4 撤回。")
            return
        key = "bib:" + row.item.item_id
        frame = dict(row.item.evidence.get("frame") or {})
        frame.setdefault("calibration_version", row.item.calibration_version)
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key=key)
            self._arrival_store.save(self._arrival_scope, key, candidate_binding(row.item), "held",
                                     reason=RECORD_BIB, bib=bib, candidate_id=row.item.item_id,
                                     crossing_ms=row.time_ms, frame=frame,
                                     calibration_version=row.item.calibration_version)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"号码未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self._select_key(row.key)
        self._last_saved_key = key
        self.hint.setText(f"已记 {bib} 号补录（导出到补录清单）；按 3 改号码，4 撤回。")

    def _undo_bib(self, row):
        try:
            self._arrival_store.undo(self._arrival_scope, key="bib:" + row.item.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        self._quick_render_signature = None
        self._render()
        self._select_key(row.key)
        self.hint.setText("已撤回号码，这一行回到疑似漏读。")

    def _undo_arrival(self):
        # Automatic advance may land on a previously counted rider. Undo the
        # exclusion just made before considering that rider's own saved marks.
        last = (self._arrival_store.latest(self._arrival_scope, self._last_saved_key)
                if self._arrival_store is not None and self._last_saved_key is not None else None)
        if last is not None and (last["action"] == "not_rider" or last.get("reason") == LOCAL_CHIP_EXCLUSION):
            self._undo_key(self._last_saved_key)
            return
        plan = self._wave_plan()
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            self._undo_key(row.key)
            return
        if row is not None and row.key in self._auto_labels:
            self._withdraw_auto_wave(row)
            return
        if row is not None and row.entered is not None:
            self._undo_bib(row)
            return
        if row is not None and row.note == "多余芯片":
            self._take_back_extra_chip(row.key)
            return
        if (row is not None and row.event is None and row.item.evidence.get("wave_parent")
                and self._row_state(row) == "疑似漏读"):
            self._return_to_wave(row)
            return
        if plan is not None and plan.marks:
            # Backspace takes back one person: the one just clicked (or whose row was just opened).
            index = (self._editing_mark if 0 <= self._editing_mark < len(plan.marks) else
                     next((i for i, mark in enumerate(plan.marks)
                           if row is not None and self._counted_here(row, plan.item)
                           and self._decision_holds(row.decision, mark)), -1))
            if index >= 0:
                self._drop_mark(plan.item.item_id, plan.marks[index])
                return
        if (row is None or not row.decision) and self.cancel_picture_mark():
            return
        if self._arrival_store is None:
            return
        # Undo once reached the newest ruling anywhere, so a press on an
        # undecided row reopened a rider confirmed long before.  Now it takes
        # the ruling just made, else the one the selected row shows.
        self._undo_key(self._last_saved_key or self._row_ruling_key(self._selected_arrival()))

    def _row_ruling_key(self, row):
        """The saved ruling a row shows: its own confirmation, else its picture's category."""
        if row is None:
            return None
        return next((key for key in (row.key, "candidate:" + row.item.item_id)
                     if self._arrival_store.latest(self._arrival_scope, key) is not None), None)

    def _undo_key(self, target_key):
        if self._arrival_store is None:
            return
        self._last_saved_key = None
        try:
            before = len(self._arrival_store.active(self._arrival_scope))
            latest = self._arrival_store.latest(self._arrival_scope, target_key) if target_key is not None else None
            operation = (latest.get("operation_id") if latest is not None
                         and latest.get("reason") == LOCAL_CHIP_EXCLUSION else None)
            if operation:
                previous = self._arrival_store.undo(self._arrival_scope, operation_id=operation)
            else:
                previous = (self._arrival_store.undo(self._arrival_scope, key=target_key)
                            if target_key is not None else None)
            # A picture category that other confirmations still rely on stays.
            if previous is not None and len(self._arrival_store.active(self._arrival_scope)) == before:
                previous = None
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤销未保存：{error}")
            return
        self._render()
        if previous:
            candidate_id = previous.get("candidate_id")
            candidate = next((item for item in self.items if item.item_id == candidate_id), None)
            index = next((i for i, row in enumerate(self._arrival_rows)
                          if row.key == previous.get("key")), None)
            if index is None:
                index = next((i for i, row in enumerate(self._arrival_rows)
                              if row.item.item_id == candidate_id), None)
            if index is not None:
                row = self._arrival_rows[index]
                self._selected_key = row.key
                self._active_item = candidate or row.item
                self._shown_id = self._active_item.item_id
                self.table.setCurrentCell(index, 0)
            if previous.get("frame"):
                current = self._frame_provider()
                saved = previous["frame"]
                from .bunch_review import mark_on_frame
                self._editing_mark = next((i for i, mark in enumerate(self.current_marks())
                                           if saved.get("marker_x_normalized") is not None
                                           and saved.get("marker_y_normalized") is not None
                                           and abs(mark["x"] - saved["marker_x_normalized"]) <= .001
                                           and abs(mark["y"] - saved["marker_y_normalized"]) <= .001
                                           and mark_on_frame(mark, saved, saved.get("recorder_ms"))), -1)
                same_frame = bool(current and current.get("camera_index") == saved.get("camera_index")
                                  and current.get("segment_id") == saved.get("segment_id")
                                  and current.get("frame_index") == saved.get("frame_index"))
                if not same_frame:
                    self.jump_requested.emit(dict(saved))
            self._sync_arrival_confirmation()
            self.marks_changed.emit(tuple(self.current_marks()))
        undone = (None if previous is None
                  else f" {previous.get('bib')} 号确认" if previous["action"] == "confirm"
                  else "本机排除" if previous.get("reason") == LOCAL_CHIP_EXCLUSION
                  else {"not_rider": "排除", "held": "暂放"}.get(previous["action"], "运动员标记"))
        self.hint.setText(f"已撤销{undone}；可重新点人或按 1/2/3 重判。" if undone
                          else "这一行没有可撤销的判定。")

    def _step(self, delta):
        # One step per picture, from the selected row (not the open picture,
        # which may be a wave's counting picture without a row of its own).
        # A 多出 row is opened by clicking it, never stepped onto.
        first = {}
        for position, row in enumerate(self._arrival_rows):
            if row.mark is None and not self.table.isRowHidden(position):
                first.setdefault(row.item.item_id, position)
        items = list(first)
        selected = self._selected_arrival()
        if selected is None or selected.mark is None and selected.item.item_id in first:
            index = (items.index(selected.item.item_id) if selected is not None else -1) + delta
        else:  # from a 多出 row: the picture either side of it
            before = sum(position < self.table.currentRow() for position in first.values())
            index = before if delta > 0 else before - 1
        if 0 <= index < len(items):
            self._arrival_clicked(first[items[index]])
        elif selected is not None and self._row_state(selected) == "已排除":
            self._select_next_visible(self.table.currentRow())

    def _queue_key(self, event):
        if event.isAutoRepeat():
            return event.key() in NUMBER_KEYS + (Qt.Key_Backspace,)
        if self._auto_review is not None and event.key() not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt):
            self._stop_auto_review("已停止自动补看，这一行未排除。")
            return True
        if event.key() not in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            self.stop_run()  # the key then does what it always does, on the frame held
        if event.key() in NUMBER_KEYS + (Qt.Key_NumberSign,) and not event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            # 1 / 2 / 3 act on the selected row, 4 undoes (10-29); 5 and # are no keys any more.
            if event.key() in ROW_KEYS:
                self._row_key(event.key())
            elif event.key() == Qt.Key_4:
                self._undo_arrival()
            else:
                self.hint.setText("这个键不用了：1 不是运动员、2 双芯片、3 补录号码、4 撤销。")
            return True
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_Z:
            self._undo_arrival()
            return True
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        # The picture is already as large as the workbench allows: F does nothing.
        # Esc still leaves an enlarged view, however it was entered.
        if event.key() == Qt.Key_Escape and self._big_view:
            self.toggle_big_view()
            return True
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            step = 5 if event.modifiers() & Qt.ShiftModifier else 1
            self.frame_step_requested.emit(-step if event.key() == Qt.Key_Left else step)
            return True
        # The picture counts, 1 / 2 / 3 or the row's right click settles; undo is 4, Backspace or Ctrl+Z.
        actions = {Qt.Key_Backspace: self._undo_arrival,
                   Qt.Key_Return: self._enter_key, Qt.Key_Enter: self._enter_key,
                   Qt.Key_Up: lambda: self._step(-1), Qt.Key_Down: lambda: self._step(1)}
        if event.key() in actions:
            actions[event.key()]()
            return True
        return False
