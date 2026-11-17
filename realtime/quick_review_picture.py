"""Marking people on the picture in the quick review panel."""

from __future__ import annotations

import uuid

from .bunch_review import is_excluded, mark_on_frame
from .problem_items import KIND_MANUAL_REVIEW
from .quick_review_common import GHOST_REACH_MS


class QuickReviewPictureMixin:
    """Counted people and candidates on the picture, picture marks, overlays and hints, dropping marks."""

    def _counted_person(self, row):
        """(plan, index) of the person counted for ROW on its wave's picture: its chip's, or a 多出 row's."""
        if row is None:
            return None
        if row.mark is not None:
            plan = self._wave_plan(row.item)
            index = self._plan_index(plan, row.mark)
        else:
            plan = self._counted_plan(row)
            index = next((i for i, chip in plan.assigned if chip.key == row.key), None) if plan is not None else None
        return (plan, index) if index is not None else None

    def _person_picture(self, key):
        """The person of a row, with their wave's picture made the one acted on."""
        person = self._counted_person(self._row_by_key(key))
        if person is None:
            self.hint.setText("这一行没有在画面上点过的人。")
            return None
        plan = person[0]
        if self._active_item is None or self._active_item.item_id != plan.item.item_id:
            self._active_item = plan.item
            self._shown_id = plan.item.item_id
        return person

    def _person_no_chip(self, key):
        """这人没芯片：they leave the wave's chips for a 疑似漏读 row of their own, and their bib is asked."""
        person = self._person_picture(key)
        if person is None:
            return
        self._flag_mark("no_chip", index=person[1])
        row = self._selected_arrival()
        if row is not None and row.event is None and self._row_state(row) == "疑似漏读":
            self._enter_bib(row)

    def _person_flag(self, key, flag):
        person = self._person_picture(key)
        if person is not None:
            self._flag_mark(flag, index=person[1])

    def _drop_person(self, key):
        person = self._person_picture(key)
        if person is not None:
            plan, index = person
            self._drop_mark(plan.item.item_id, plan.marks[index])

    def _record_candidate(self, key):
        """补录号码 on a crossing without a chip: listed 疑似漏读, then its bib is asked."""
        row = self._row_by_key(key)
        if row is None or not self._excludable(row) or self._row_state(row) == "画面待核":
            return
        self._candidate_no_chip(row)
        row = self._row_by_key(key)
        if row is not None and self._row_state(row) == "疑似漏读":
            self._select_key(key)
            self._enter_bib(row)

    def _candidate_not_rider(self, key):
        index = next((i for i, row in enumerate(self._arrival_rows) if row.key == key), None)
        if index is None:
            return
        row = self._arrival_rows[index]
        if self.current_item() is None or self.current_item().item_id != row.item.item_id:
            self._open_row(index)
            row = self._row_by_key(key) or row
        self._classify("not_rider", row)

    def _miss_not_rider(self, key):
        """不是运动员 on a 疑似漏读 row: out of the wave's count, or the picture excluded."""
        row = self._row_by_key(key)
        if row is None:
            return
        if row.item.evidence.get("wave_parent"):
            self._return_to_wave(row, exclude=True)
            return
        if not self._frame_usable(self._row_frame(row)):
            self.hint.setText("请先打开当前候选的有效录像画面。")  # still 疑似漏读: nothing taken back
            return
        self._take_back_no_chip(row)
        self._candidate_not_rider(key)

    def _take_back_miss(self, key):
        row = self._row_by_key(key)
        if row is None:
            return
        if row.item.evidence.get("wave_parent"):
            self._return_to_wave(row)
        else:
            self._take_back_no_chip(row)

    def picture_mark_label(self, mark):
        """Use the saved identity for a circle; an unassigned circle has no count number."""
        if mark.get("exclusion_reason"):
            return "非运动员"
        plan = self._wave_plan()
        index = self._plan_index(plan, mark)
        if index is not None:
            return plan.labels.get(index, "")
        item = self.current_item()
        decided = self._decided_row(mark, item)
        if decided is not None:
            return decided.bib
        row = self._selected_arrival()
        marks = self.current_marks()
        editing = marks[self._editing_mark] if 0 <= self._editing_mark < len(marks) else None
        if ((len(marks) == 1 or mark == editing) and row is not None and row.event is not None
                and row.event.is_active and row.bib and not row.stale and row.decision is None):
            return row.bib
        return mark.get("bib") or "待选号码"

    def picture_mark_warning(self, mark):
        """A person beyond the wave's chips, or one without a chip: drawn in red."""
        plan = self._wave_plan()
        index = self._plan_index(plan, mark)
        return index is not None and (index in plan.extra or index in plan.no_chip)

    def picture_candidate(self, frame):
        """Which picture a click on the frame counts a person on.

        Returns (picture, refusal): picture None means a new one is needed.  Inside
        a wave every click goes onto the wave's one picture, however long the
        wave lasts, and only on the camera it was started on.
        """
        row = self._selected_arrival()
        if row is not None and row.note == "已排除":
            return None, "这条芯片已在本机排除，请先按 4 撤回或选择其他记录。"
        wave = self._wave_for_frame(frame)
        if wave is not None:
            item = self._wave_candidate(wave)
            camera = int((item.evidence.get("frame") or {}).get("camera_index") or 1) if item else None
            if camera is not None and camera != int(frame["camera_index"]):
                return None, f"本波在机位 {camera} 上点人；换机位会把同一个人数两次。"
            return item, ""
        if row is not None and (row.decision is not None or row.stale):
            return None, "该记录已确认或证据已变化，请先撤销或重新打开原帧。"
        item = self.current_item()
        original = item.evidence.get("frame", {}) if item else {}
        reuse = bool(item and item.kind == KIND_MANUAL_REVIEW and not item.evidence.get("superseded")
                     and not item.evidence.get("configuration_stale")
                     and original.get("camera_index") == frame["camera_index"]
                     and original.get("recorder_ms") is not None
                     and abs(original["recorder_ms"] - frame["recorder_ms"]) <= 10000)
        return (item if reuse else None), ""

    def adopt_picture_candidate(self, item, row_key):
        """A new picture counts the wave of the moment clicked (else of the row the operator had chosen)."""
        row = next((entry for entry in self._arrival_rows if entry.key == row_key), None)
        frame = dict(item.evidence.get("frame") or {}) if item is not None else {}
        if frame.get("recorder_ms") is None and item is not None:
            frame["recorder_ms"] = item.evidence.get("recorder_ms")
        wave = self._wave_for_frame(frame) or self._row_wave(row)
        if wave is None or item is None:
            return
        mapping = self._wave_candidates()
        for chip in wave:
            mapping[chip.event.event_id] = item.item_id
        self._index_waves()
        self.save_drafts()

    def picture_mark_hint(self):
        """The line shown after a person is counted on a wave's picture; None outside a wave."""
        plan = self._wave_plan()
        if plan is None:
            return None
        index = self._editing_mark if 0 <= self._editing_mark < len(plan.marks) else None
        label = plan.labels.get(index, "")
        if index in plan.extra:
            head = "多出一人 · 待处理"
        elif index in plan.no_chip:
            head = "这一位无芯片"
        elif label and not is_excluded(plan.marks[index]):
            head = f"已点：按芯片顺序记为 {label} 号"
        else:
            head = "已点这一位"
        near = self._near_counted(plan, index)
        if near:
            head += f"；{near}，若是同一人请右键取消它"
        return head

    def _near_counted(self, plan, index):
        """Someone already counted close by on a nearby frame: likely the same rider clicked twice."""
        if index is None:
            return ""
        mark = plan.marks[index]
        frame = dict(mark.get("frame") or {}, recorder_ms=mark["recorder_ms"])
        near = [i for i, other in enumerate(plan.marks)
                if i != index and not is_excluded(other)
                and not mark_on_frame(other, frame, mark["recorder_ms"])
                and abs(int(other["recorder_ms"]) - int(mark["recorder_ms"])) <= GHOST_REACH_MS
                and (other["x"] - mark["x"]) ** 2 + (other["y"] - mark["y"]) ** 2 <= .08 ** 2]
        if not near:
            return ""
        other = min(near, key=lambda i: abs(int(plan.marks[i]["recorder_ms"]) - int(mark["recorder_ms"])))
        return f"{plan.labels.get(other) or '有人'} 在{self._frame_offset(plan.marks[other], frame)}点过"

    @staticmethod
    def _frame_offset(mark, frame):
        """When a counted person was clicked, seen from the frame shown: 3 帧前, 0.4 秒后."""
        own = mark.get("frame") or {}
        if (own.get("segment_id") and own.get("segment_id") == frame.get("segment_id")
                and own.get("frame_index") is not None and frame.get("frame_index") is not None):
            steps = int(frame["frame_index"]) - int(own["frame_index"])
            return f" {abs(steps)} 帧{'前' if steps > 0 else '后'}"
        delta = int(frame["recorder_ms"]) - int(mark["recorder_ms"])
        return f" {abs(delta) / 1000:.1f} 秒{'前' if delta > 0 else '后'}"

    def picture_marks(self):
        """(owner, mark) for everyone counted on the picture shown.

        On a wave's picture this includes the people marked 无芯片, who were moved
        onto a 疑似漏读 row of their own but are still drawn, and cancelled, here.
        """
        item = self.current_item()
        if item is None:
            return []
        marks = [(item.item_id, mark) for mark in self._marks.get(item.item_id, ())]
        if item.item_id in self._candidate_wave:
            marks += [(other.item_id, mark) for other in self.items
                      if other.evidence.get("wave_parent") == item.item_id
                      for mark in self._marks.get(other.item_id, ())]
        return marks

    def picture_overlays(self, frame):
        """Person markers: (x, y, label, manual, alert, dot=False, ghost, auto).

        Bindings are visible without selecting their chip row. Nearby-frame
        ghosts are faded and unlabelled: these are positions, not tracking.
        AUTO describes the source, never an operator's identity confirmation.
        """
        if frame is None:
            return []
        current, camera = int(frame["recorder_ms"]), int(frame.get("camera_index") or 1)
        bibs = {row.key: row.bib for row in self._arrival_rows}
        # 11-16: rings only, the number while the mouse is on one (it is in the corner
        # badge and the cells above); the one selected is ringed twice.
        chosen = self._selected_key
        marks_now = self.current_marks()
        editing = marks_now[self._editing_mark] if 0 <= self._editing_mark < len(marks_now) else None
        # 自动对上: grey-blue dashed with the bib on the frame the person was seen
        # crossing on, faded on the frames just around it.
        automatic = []
        for key, person in self._auto_labels.items():
            if person.camera != camera:
                continue
            here = mark_on_frame({"recorder_ms": person.recorder_ms, "frame": person.frame}, frame, current)
            if here or abs(current - person.recorder_ms) <= GHOST_REACH_MS:
                automatic.append((person.x, person.y, bibs.get(key, ""), False, False, False, not here, True,
                                  True, key == chosen))
        # Saved bindings also remain visible when another table row is selected.
        for row in self._arrival_rows:
            decision = row.decision or {}
            saved = decision.get("frame") or {}
            if (saved.get("marker_x_normalized") is None or saved.get("marker_y_normalized") is None
                    or saved.get("recorder_ms") is None or int(saved.get("camera_index") or 1) != camera):
                continue
            if mark_on_frame({"recorder_ms": saved["recorder_ms"], "frame": saved}, frame, current):
                auto = bool(decision.get("auto_label"))
                automatic.append((float(saved["marker_x_normalized"]), float(saved["marker_y_normalized"]),
                                  row.bib, not auto, False, False, False, auto, True, row.key == chosen))
        item = self.current_item()
        if item is None:
            return automatic
        shown, ghosts = [], []
        for owner, mark in self.picture_marks():
            if owner == item.item_id:
                label = self.picture_mark_label(mark)
                reason = mark.get("exclusion_reason", "").strip() if is_excluded(mark) else ""
                label += f" 已排除：{reason}" if reason and reason != label else ""
                alert = bool(self.picture_mark_warning(mark))
            else:
                label, alert = "无芯片", True
            auto = bool(mark.get("auto_label"))
            picked = mark is editing
            if mark_on_frame(mark, frame, current):
                shown.append((float(mark["x"]), float(mark["y"]), label, not alert and not auto, alert, False, False, auto,
                              True, picked))
            elif (int((mark.get("frame") or {}).get("camera_index") or camera) == camera
                  and 0 < current - int(mark["recorder_ms"]) <= GHOST_REACH_MS):
                ghosts.append((float(mark["x"]), float(mark["y"]), label, not alert and not auto, alert, False, True, auto,
                               True, False))
        # A saved binding may also be a current picture mark; draw it only once
        # (selected when either says so).
        unique = {}
        for overlay in automatic + ghosts + shown:
            spot = (round(overlay[0], 4), round(overlay[1], 4), overlay[2], overlay[6])
            if spot in unique and unique[spot][9]:
                overlay = overlay[:9] + (True,)
            unique[spot] = overlay
        return list(unique.values())

    def _candidate_no_chip(self, row):
        """A picture without a chip of its own shows a rider: a 疑似漏读 row, kept for the operator."""
        # The frame shown only if it is this row's: a right click may come from anywhere.
        frame = self._row_frame(row)
        members = [row.item, *(piece.item for piece in row.fragments)]
        try:
            for member in members:
                # Saved as a rider picture with no chip; one already marked a
                # rider is taken back first, or the flag would not be kept.
                if (self._category(member) or {}).get("action") == "bicycle":
                    self._arrival_store.undo(self._arrival_scope, key="candidate:" + member.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"审核未保存：{error}")
            return
        if not self._save_ruling(row.item, members[1:], "bicycle", frame, uuid.uuid4().hex, no_chip=True):
            return
        self._render()
        self._last_saved_key = "candidate:" + row.item.item_id
        self.hint.setText("已列为疑似漏读（无芯片）：核对原帧号码后按 3 补录号码；4 撤回。")

    def _flag_mark(self, flag, index=None):
        """Toggle 无芯片 / 非运动员 on a counted person; saved at once.

        INDEX: the person picked by their row on the left (right click); else
        the one last clicked, only while on the frame shown.
        """
        item = self.current_item()
        marks = self._marks.get(item.item_id, []) if item is not None else []
        frame = self._frame_provider() or {}
        picked = index is not None
        index = self._editing_mark if index is None else index
        if not (0 <= index < len(marks)
                and (picked or mark_on_frame(marks[index], frame, frame.get("recorder_ms")))):
            self.hint.setText("先点中当前画面上的那个人。")
            return
        mark = marks[index]
        decided = self._decided_row(mark, item)
        if decided is not None and not self._counted_here(decided, item):
            self.hint.setText(f"这一位已在别处确认为 {decided.bib}；要改先按 4 撤销。")
            return
        self._editing_mark = index
        if flag == "no_chip":
            mark["no_chip"] = not mark.get("no_chip")
            mark["exclusion_reason"] = ""
            text = "已标为无芯片：单独列一行疑似漏读，后面的号码顺延。"
        else:
            mark["exclusion_reason"] = "" if mark.get("exclusion_reason") else "非运动员"
            mark.pop("no_chip", None)
            text = ("已排除这一位（非运动员），不计人数，后面的号码顺延。" if mark["exclusion_reason"]
                    else "已恢复计数这一位。")
        self.save_drafts()
        plan = self._wave_plan()
        if plan is not None:
            if mark.get("no_chip"):
                # Their own 疑似漏读 row at their own time; still drawn, and cancelled, here.
                children = self._split_wave_people(item, [mark], uuid.uuid4().hex)
                self._editing_mark = -1
                plan = self._wave_plan()
                if children:
                    # Selected, on the wave's picture: 4 or Backspace takes it back.
                    self._selected_key = "candidate:" + children[0][0]
                    text = "已标无芯片（疑似漏读，不占号），后面的号码顺延；按 4 撤回。"
            if plan is not None:
                self._save_wave(plan)
        if self._editing_mark < 0:
            self._select_key(self._selected_key)
        self._keep_selection()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(text)

    def _keep_selection(self):
        """A settled 多出 row is gone: select the wave's person being edited instead of row 1."""
        if all(row.key != self._selected_key for row in self._arrival_rows):
            self.show_counted(self._editing_mark)

    def _pointed_mark(self, x=None, y=None):
        """The counted person a right click points at: on this frame first, else a faded one nearby."""
        frame = self._frame_provider()
        if not frame or frame.get("recorder_ms") is None:
            return None
        marks = self.picture_marks()
        shown = [entry for entry in marks if mark_on_frame(entry[1], frame, frame["recorder_ms"])]
        if x is None:
            return shown[-1] if shown else None

        def distance(entry):
            return (entry[1]["x"] - x) ** 2 + (entry[1]["y"] - y) ** 2

        near = [entry for entry in shown if distance(entry) <= .04 ** 2]
        if not near:
            camera = int(frame.get("camera_index") or 1)
            near = [entry for entry in marks if entry not in shown and distance(entry) <= .06 ** 2
                    and int((entry[1].get("frame") or {}).get("camera_index") or camera) == camera
                    and 0 < int(frame["recorder_ms"]) - int(entry[1]["recorder_ms"]) <= GHOST_REACH_MS]
        return min(near, key=distance) if near else None

    def _drop_mark(self, owner, mark):
        """Take one counted person back; the bibs after them shift into line and are saved so."""
        item = self.current_item()
        label = self.picture_mark_label(mark) if owner == item.item_id else "无芯片的人"
        try:
            if owner != item.item_id:
                # A 无芯片 person: their 疑似漏读 row, and a bib entered for it, go with them.
                if self._arrival_store.latest(self._arrival_scope, "bib:" + owner) is not None:
                    self._arrival_store.undo(self._arrival_scope, key="bib:" + owner)
                self._arrival_store.undo(self._arrival_scope, key="candidate:" + owner)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"取消未保存：{error}")
            return
        owned = self._marks.get(owner, [])
        owned[:] = [value for value in owned if value is not mark]
        if owner == item.item_id:
            self._editing_mark = len(owned) - 1
        self.save_drafts()
        plan = self._wave_plan(item)
        if plan is None or not self._save_wave(plan):
            self._quick_render_signature = None
            self._render()
        self._keep_selection()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText(f"已取消 {label}；后面的号码已顺延。")

    def cancel_picture_mark(self, x=None, y=None):
        if self._wave_plan() is not None:
            target = self._pointed_mark(x, y)
            if target is None:
                return False
            self._drop_mark(*target)
            return True
        marks = self.current_marks()
        frame = self._frame_provider()
        candidates = [i for i, mark in enumerate(marks)
                      if frame and mark_on_frame(mark, frame, frame.get("recorder_ms"))]
        if x is not None:
            candidates = [i for i in candidates
                          if (marks[i]["x"] - x) ** 2 + (marks[i]["y"] - y) ** 2 <= .04 ** 2]
        index = (min(candidates, key=lambda i: (marks[i]["x"] - x) ** 2 + (marks[i]["y"] - y) ** 2)
                 if candidates and x is not None else candidates[-1] if candidates else None)
        if index is None:
            return False
        mark = marks[index]
        item = self.current_item()
        for row_index, row in enumerate(self._arrival_rows):
            saved = (row.decision or {}).get("frame", {})
            if (row.decision and item and row.decision.get("candidate_id") == item.item_id
                    and mark_on_frame(mark, saved, saved.get("recorder_ms"))
                    and abs(saved.get("marker_x_normalized", -1) - mark["x"]) <= .001
                    and abs(saved.get("marker_y_normalized", -1) - mark["y"]) <= .001):
                self.table.setCurrentCell(row_index, 0)
                self._undo_key(row.key)
                break
        # Undo may have re-rendered the table and moved the active item; remove
        # the circle from the item it was drawn on, not from whatever is current.
        owned = self._marks.get(item.item_id, []) if item is not None else []
        if 0 <= index < len(owned):
            owned.pop(index)
            self._editing_mark = min(index, len(owned) - 1)
            self._marks_updated()
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已取消这个待选标记；左键可重新点选，右键取消。")
        return True
