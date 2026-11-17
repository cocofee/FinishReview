"""Waves (一波) of the quick review panel: indexing, auto-labelled waves, wave status and slots."""

from __future__ import annotations

import json
import uuid

from .arrival_review import candidate_binding
from .bunch_review import is_excluded, mark_on_frame, uncovered_intervals
from .problem_items import (
    CATEGORY_VIDEO,
    KIND_MANUAL_REVIEW,
    STATUS_WAITING,
    ProblemItem,
    stable_item_id,
)
from .wave_slots import DONE, ELSEWHERE, EXTRA, NEXT, NO_CHIP, TODO, Slot
from .quick_review_common import (
    AUTO_PICTURE_REASON,
    RUN_NEAR_MS,
    WAVE_ALERT_WORDS,
    WAVE_GAP_MS,
    WAVE_VIEW_MARGIN_MS,
)
from .atomic_file import atomic_write_text


class QuickReviewWavesMixin:
    """Index waves, accept/withdraw auto-labelled waves, wave status and confirmation, wave slots and counts."""

    def _wave_candidates(self):
        """Chip event id -> the picture its wave is counted on (kept with the drafts)."""
        return self._arrival_drafts.setdefault("waves", {})

    def _index_waves(self):
        """Chain the chips into waves by their official time; rows keep table order."""
        chips = sorted((row for row in self._arrival_rows if row.event is not None and row.event.is_active
                        and row.note != "已排除"),
                       key=lambda row: (int(row.event.timeline_timestamp_ms), row.key))
        waves, previous = [], None
        for row in chips:
            stamp = int(row.event.timeline_timestamp_ms)
            if previous is None or stamp - previous >= WAVE_GAP_MS:
                waves.append([])
            waves[-1].append(row)
            previous = stamp
        order = {row.key: index for index, row in enumerate(self._arrival_rows)}
        self._waves = [sorted(wave, key=lambda row: order[row.key]) for wave in waves]
        self._wave_of = {row.key: index for index, wave in enumerate(self._waves) for row in wave}
        mapping = self._wave_candidates()
        self._candidate_wave = {}
        for index, wave in enumerate(self._waves):
            for row in wave:
                if mapping.get(row.event.event_id):
                    self._candidate_wave.setdefault(mapping[row.event.event_id], index)
        # The picture is redrawn on every decoded frame; look decisions up by picture.
        self._decided = {}
        for row in self._arrival_rows:
            if row.decision:
                self._decided.setdefault(row.decision.get("candidate_id"), []).append(row)
        self._plan_cache = None

    def _wave_plans(self):
        """Wave index -> its plan, for the waves that have a picture to count on."""
        plans = {}
        for index, wave in enumerate(self._waves):
            item = self._wave_candidate(wave)
            plan = self._wave_plan(item) if item is not None else None
            if plan is not None:
                plans[index] = plan
        self._plan_cache = None
        return plans

    @staticmethod
    def _auto_key(wave):
        """A wave as its chips: the key 4 withdraws its 自动对上 under (a new chip makes it a new wave)."""
        return "|".join(sorted(row.event.event_id for row in wave))

    def _accept_auto_wave(self, row):
        """Accept the existing pairs, preserving their original frames; True once on the next wave.

        Acceptance is an explicit operator action, not proof of viewing a fixed
        span or a manual count. Existing undo and durable rollback still apply.
        """
        wave = self._row_wave(row) or ()
        pairs = [(chip, self._auto_labels[chip.key]) for chip in wave if chip.key in self._auto_labels]
        if not pairs:
            return False
        if len(pairs) != len(wave):
            self.hint.setText("自动对应不完整，请重新核对本波。")
            return False
        frames = [dict(person.frame, marker_x_normalized=person.x, marker_y_normalized=person.y)
                  for _chip, person in pairs]
        self.holds_picture = True  # the frame on screen stays where the operator looked
        item = None
        created = False
        previous_marks = []
        try:
            item, refusal = self.picture_candidate(frames[0])
            if refusal:
                self.hint.setText(refusal)
                return False
            if item is not None:
                previous_marks = list(self._marks.get(item.item_id, ()))
                if previous_marks:
                    self.hint.setText("这一波已有人工标记，请核对画面，不再自动添加。")
                    return False
            counted = self.picture_marks()
            if any(mark_on_frame(mark, frame, person.recorder_ms)
                   and (mark["x"] - person.x) ** 2 + (mark["y"] - person.y) ** 2 < .02 ** 2
                   for (_chip, person), frame in zip(pairs, frames) for _owner, mark in counted):
                self.hint.setText("自动对上未保存：有人已点过，请核对原有标记，勿重复点人。")
                return False
            if item is None:
                item = self.add_manual_problem(frames[0], reason=AUTO_PICTURE_REASON,
                                               recorder_ms=frames[0]["recorder_ms"])
                if item is None:
                    return False
                created = True
                self.adopt_picture_candidate(item, row.key)
            self.open_picture(item)
            for (_chip, person), frame in zip(pairs, frames):
                before = len(self._marks.get(item.item_id, ()))
                self.add_mark(person.x, person.y, person.recorder_ms, frame, save=False)
                marks = self._marks.get(item.item_id, [])
                if len(marks) > before:
                    marks[-1]["auto_label"] = True  # system pairing explicitly accepted, not a manual click
            operation_id = uuid.uuid4().hex
            plan = self._wave_plan(item)
            if plan is None or len(plan.marks) != len(pairs):
                self._drop_auto_marks(item, previous_marks,
                                      "自动对上未保存：人数标记未能完整添加，请核对原有标记。", created=created)
                return False
            if not self._save_wave(plan, operation_id=operation_id):
                self._drop_auto_marks(item, previous_marks, self.hint.text(), created=created)
                return False
        except (OSError, RuntimeError, ValueError) as error:
            self._drop_auto_marks(item, previous_marks, f"自动对上未保存：{error}", created=created)
            return False
        finally:
            self.holds_picture = False
        self.save_drafts()
        plan = self._wave_plan(item)
        if plan is not None and len(plan.marks) == len(pairs) and plan.marks and all(
                row.decision is not None for row in plan.chips):
            self._next_wave(item)
            return True
        self.hint.setText("自动对上没有全部存上：请在画面上核对这一波。")
        return False

    def _drop_auto_marks(self, item, previous_marks, message, *, created=False):
        """只清理本次临时标记；撤回失败时保留已落盘判定及对应画面。"""
        if item is not None:
            try:
                self._arrival_store.refresh()
            except (OSError, RuntimeError, ValueError) as error:
                self.hint.setText(f"{message}；无法核对已存状态：{error}，请勿重复点人。")
                return
            saved = any(entry.get("action") == "confirm" and entry.get("candidate_id") == item.item_id
                        for entry in self._arrival_store.active(self._arrival_scope))
            if saved:
                message += "；已有部分保存，请核对已点标记，勿重复点人。"
            else:
                if created:
                    path = self.problem_dir / "manual_problems.json"
                    try:
                        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                        atomic_write_text(path, json.dumps([value for value in values
                                                            if value.get("item_id") != item.item_id], ensure_ascii=False),
                                          encoding="utf-8")
                    except (OSError, ValueError, TypeError):
                        message += "；自动创建的问题条目未能清理，请稍后手工删除。"
                    self.items = [value for value in self.items if value.item_id != item.item_id]
                    self._marks.pop(item.item_id, None)
                    self._candidate_wave.pop(item.item_id, None)
                    mapping = self._wave_candidates()
                    for event_id in [key for key, value in mapping.items() if value == item.item_id]:
                        del mapping[event_id]
                    if self.current_item() is not None and self.current_item().item_id == item.item_id:
                        self._active_item = None
                elif previous_marks:
                    self._marks[item.item_id] = previous_marks
                else:
                    self._marks.pop(item.item_id, None)
                self._editing_mark = -1
            self._plan_cache = None
            self.save_drafts()
            self._quick_render_signature = None
            self._render()
            self._sync_marks()
        self.hint.setText(message)

    def _withdraw_auto_wave(self, row):
        """4 on a 自动对上 wave: its pairs are taken back for good, and the wave is counted by hand."""
        wave = self._row_wave(row)
        if not wave:
            return
        withdrawn = self._arrival_drafts.setdefault("auto_withdrawn", [])
        if self._auto_key(wave) not in withdrawn:
            withdrawn.append(self._auto_key(wave))
        self.save_drafts()
        self._quick_render_signature = None
        self._render()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已撤回这一波的自动对上：请在画面上逐个点人。")

    def _row_wave(self, row):
        if row is None or row.note == "已排除":
            return None
        index = self._wave_of.get(row.key, self._candidate_wave.get(row.item.item_id))
        return self._waves[index] if index is not None else None

    def _wave_candidate(self, wave):
        """The picture this wave's people are counted on, if one is still usable."""
        mapping = self._wave_candidates()
        wanted = [mapping[row.event.event_id] for row in wave or () if mapping.get(row.event.event_id)]
        for item_id in dict.fromkeys(wanted):
            item = next((item for item in self.items if item.item_id == item_id), None)
            if (item is not None and item.kind == KIND_MANUAL_REVIEW and item.status != STATUS_WAITING
                    and not item.evidence.get("superseded") and not item.evidence.get("configuration_stale")):
                return item
        return None

    @staticmethod
    def _plan_index(plan, mark):
        if plan is None:
            return None
        return next((i for i, value in enumerate(plan.marks) if value is mark),
                    next((i for i, value in enumerate(plan.marks) if value == mark), None))

    def _decided_row(self, mark, item):
        """The chip row a counted person was confirmed as, by the frame and spot saved with it."""
        if item is None:
            return None
        for row in self._decided.get(item.item_id, ()):
            decision = row.decision
            if not mark_on_frame(mark, decision["frame"], decision["frame"].get("recorder_ms")):
                continue
            frame = decision["frame"]
            if frame.get("marker_x_normalized") is None and len(self._marks.get(item.item_id, ())) > 1:
                continue
            if (all(frame.get(key) is not None for key in ("marker_x_normalized", "marker_y_normalized"))
                    and (abs(float(frame["marker_x_normalized"]) - mark["x"]) > .001
                         or abs(float(frame["marker_y_normalized"]) - mark["y"]) > .001)):
                continue
            return row
        return None

    @staticmethod
    def _counted_here(row, item):
        """A chip confirmed by a person clicked on this picture (its spot saved with it)."""
        decision = row.decision
        return bool(decision and item is not None and decision.get("candidate_id") == item.item_id
                    and (decision.get("frame") or {}).get("marker_x_normalized") is not None)

    def _sync_arrival_confirmation(self):
        row = self._selected_arrival()
        self.race_strip.view.set_selected_chip(
            (int(row.event.timeline_timestamp_ms), row.bib) if row is not None and row.event is not None else None)
        item = self._candidate_item()
        category = self._category()
        frame = self._frame_provider()
        valid_frame = bool(frame and frame.get("frame_index", -1) >= 0
                           and frame.get("position_ms", -1) >= 0
                           and frame.get("segment_id") and frame.get("video_path")
                           and frame.get("recorder_ms") is not None)
        if row is not None and row.note == "已排除":
            reason = "这条芯片已在本机排除，按 4 撤回后再处理"
        elif row is None or row.event is None or not row.event.is_active or not row.bib:
            reason = "可先在画面点圈；确认过线前请选择对应芯片记录"
        elif row.decision is not None:
            reason = "该运动员已确认；修改前请先撤销"
        elif not valid_frame:
            reason = "请从胶卷打开有效原帧"
        elif item is None or not item.evidence.get("frame"):
            reason = "请先在画面上左键点中压线的人"
        elif (item.status == STATUS_WAITING or item.evidence.get("configuration_stale")
              or item.evidence.get("superseded")):
            reason = "当前候选证据已失效，请重新打开原帧"
        elif category is None or category.get("action") != "bicycle":
            reason = "请先在画面上左键点中压线的人"
        elif row.item.evidence.get("configuration_stale"):
            reason = "所选记录配置已变化，请重新分析"
        else:
            reason = ""
        self.confirm_arrival_button.setEnabled(not reason)
        self.confirm_arrival_button.setToolTip(reason or "保存所选运动员与当前显示的压线原帧；不会修改正式芯片时间")
        # The old confirm path's reasons only explain its hidden button; the
        # status says what the picture and the table do now.
        count = sum(not mark.get("exclusion_reason") for mark in self.current_marks())
        selected = f"{row.bib} 号" if row is not None and row.bib else "无芯片记录" if row is not None else "未选中"
        status = selected + (f" · 已点 {count}" if count else "")
        plan = self._wave_plan()
        wave = plan.chips if plan is not None else self._row_wave(row)
        color = ""
        if row is not None and row.note == "已排除":
            status, color = f"{row.bib} 号 · 本机已排除", "#4b5563"
        elif plan is not None and (plan.marks or plan.split):
            status, color = self._wave_status(plan)
        elif row is not None and row.key in self._auto_labels and wave is not None:
            status = f"自动对上 · 芯片 {len(wave)} · 对就按空格往下走，不对按 4"
            color = "#3b5b7a"
        elif row is not None and wave is None and self._row_state(row) == "待定" and row.time_ms is not None:
            # A crossing without a chip may be several people: it is looked at
            # from a quarter second before (可能多人: from when the first came on)
            # to a quarter second after (15:50, 10-29: 4 people on one row).
            unseen = self._fragment_unseen(row, whole=True)
            span = (f"{len(row.fragments) + 1} 段 · {(row.fragments[-1].time_ms - row.time_ms) / 1000:.1f} 秒 · "
                    if row.fragments else "")
            if self._earlier_arrival(row, read=False) is not None:
                span = "可能多人 · " + span
            status = (span + (f"还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看" if unseen
                              else "已看完"))
            color = "#9a5b12" if unseen else "#126044"
        elif wave is not None and len(wave) > 1 and any(chip.decision is None for chip in wave):
            status = f"本波 {wave[0].bib}→{wave[-1].bib} · {len(wave)} 芯片"
        if plan is not None and plan.unmatched:
            status += f" · 下一位 {plan.unmatched[0].bib} 号"
        self.confirmation_status.setText(status)
        self.confirmation_status.setToolTip(status)
        self.confirmation_status.setStyleSheet(f"color:{color}; font-weight:600;" if color else "")
        # 11-14: above the strip this line mostly repeated 本波 n/m.  What only it
        # said goes beside 本波, over the picture, and only while there is
        # something to do about it; the line itself is no longer shown.
        self._wave_alert = " · ".join(part for part in status.split(" · ")
                                      if any(word in part for word in WAVE_ALERT_WORDS))
        self.confirmation_status.setVisible(False)

    def _wave_status(self, plan):
        """The count of a wave against its chips, and its color: green 已核, red 多/少, amber not all seen."""
        chips, people = len(plan.chips) - len(plan.extra_chips), plan.people
        automatic = sum(bool(mark.get("auto_label")) for mark in plan.marks)
        status = (f"芯片 {chips} · 已接受对应 {automatic} · 人工点数 {people - automatic}" if automatic
                  else f"芯片 {chips} · 已点 {people}")
        if plan.extra_chips:
            status += f" · 双芯片 {len(plan.extra_chips)}"
        if plan.extra:
            status += f" · 多 {len(plan.extra)} 人"
            for i, near in plan.near.items():
                names = "、".join(plan.labels[c] if plan.labels[c].startswith("多") else f"{plan.labels[c]} 号"
                                 for c in near)
                status += f" · {plan.labels[i]} 分不清：{names} 挨得太近，请裁判看录像"
        if plan.unmatched:
            status += f" · 少 {len(plan.unmatched)} 人"
        missing = len(plan.no_chip) + plan.split
        if missing:
            status += f" · 无芯片 {missing}"
        accepted = bool(automatic and automatic == len(plan.marks) and plan.matches)
        unseen = [] if accepted else self._unseen(plan)
        if unseen:
            status += f" · 还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒没看"
        if self._wave_outdated(plan):
            # A chip came in (or changed) after the wave was counted: the bibs
            # saved no longer follow chip order.  Saved again only when asked.
            return status + " · 芯片有变化：按回车按新顺序重存", "#b42318"
        if not plan.matches:
            return status, "#b42318"
        if unseen:
            return status, "#9a5b12"
        return status + (" · 不算人工确认" if accepted else " · 已核"), "#3b5b7a" if accepted else "#126044"

    def _wave_outdated(self, plan):
        """What is saved for a wave is not what its picture now counts (see _save_wave)."""
        wanted = {row.key: plan.marks[index] for index, row in plan.assigned}
        for row in plan.chips:
            if row.decision is not None and not self._counted_here(row, plan.item):
                continue
            mark = wanted.get(row.key)
            if (mark is None) != (row.decision is None) or (
                    mark is not None and not self._decision_holds(row.decision, mark)):
                return True
        return False

    def _counted_plan(self, row):
        """The plan of the wave this chip row is in, once someone is counted on its picture."""
        item = self._wave_candidate(self._row_wave(row)) if row is not None and row.event is not None else None
        plan = self._wave_plan(item) if item is not None else None
        return plan if plan is not None and (plan.marks or plan.split) else None

    def _unseen(self, plan):
        """The parts of a wave's span, in chip time, that no decoded frame has shown yet.

        A lone chip too: one crossing may be several people (15:50, 10-29).
        """
        if not plan.chips:
            return []
        return self._span_unseen(plan.chips, int((plan.item.evidence.get("frame") or {}).get("camera_index") or 1))

    def _span_unseen(self, chips, camera=None, margin=WAVE_VIEW_MARGIN_MS):
        """The parts of these chips' span that no decoded frame has shown yet."""
        stamps = [int(row.event.timeline_timestamp_ms) for row in chips]
        camera = str(int(camera if camera is not None else self.race_strip.camera or 1))
        seen = self._arrival_drafts.get("seen", {}).get(camera, [])
        start, end = min(stamps) - margin, max(stamps) + margin
        if start == end:
            shown = any(left - 100 <= start <= right + 100 for left, right in seen)
            return [] if shown else [[start, start + 100]]
        return uncovered_intervals(start, end, seen, tolerance=100)

    def wave_slot_list(self, plan=None):
        """One cell an arrival, the whole race in table order; the wave on the picture is marked current."""
        plan = plan or self._run_plan()  # before anyone is clicked on it: its chips alone
        # Asked for on every frame shown.  Before anyone is clicked on a wave its
        # plan is made anew each time (_run_plan): the cells are kept by what the
        # plan is made of, never by the plan object (11-14: 250 rows a frame at 4x).
        picture = plan.item.item_id if plan is not None and plan.item is not None else None
        key = (self._table_builds, len(self._arrival_rows), picture,
               self._plan_cache[0] if picture is not None and self._plan_cache is not None else None,
               tuple(row.key for row in plan.chips) if plan is not None else None,
               plan.unmatched[0].key if plan is not None and plan.unmatched else None,
               plan.people if plan is not None else None)
        target = self.current_target(plan)[0]
        key += (target,)
        if self._slot_cache is not None and self._slot_cache[0] == key:
            self.slot_cache_hits += 1
            return self._slot_cache[1]
        holder, chips, following, marks = {}, set(), None, []
        if plan is not None:
            chips = {row.key for row in plan.chips}
            holder = {row.key: i for i, row in plan.assigned}
            marks = plan.marks
            for i, mark in enumerate(marks):
                if i in plan.labels and i not in plan.no_chip and i not in plan.extra and not is_excluded(mark):
                    row = self._decided_row(mark, plan.item)
                    if row is not None:
                        holder.setdefault(row.key, i)
            following = plan.unmatched[0].key if plan.unmatched else None
        slots = []
        for row in self._arrival_rows:
            state = self._row_state(row)
            if state in ("已排除", "画面待核") or row.note == "多余芯片":
                continue
            if row.event is None:  # somebody without a chip record: 多出 or 疑似漏读
                index = next((i for i, mark in enumerate(marks) if mark is row.mark), None)
                if index is not None and index in plan.extra:
                    slots.append(Slot(plan.labels[index], EXTRA, index, True))
                else:
                    slots.append(Slot(str(row.bib) if row.entered and row.bib else "无芯片", NO_CHIP,
                                      index, index is not None))
            elif row.key in chips:
                index = holder.get(row.key)
                if index is not None:
                    slots.append(Slot(str(row.bib), DONE, index, True, row.key == target))
                elif row.decision is not None and not self._counted_here(row, plan.item):
                    slots.append(Slot(str(row.bib), ELSEWHERE, None, True, row.key == target))
                else:
                    slots.append(Slot(str(row.bib), NEXT if row.key == following else TODO, None, True,
                                      row.key == target))
            else:
                slots.append(Slot(str(row.bib), DONE if row.decision else TODO))
        summary = ""
        if plan is not None:
            count = len(plan.chips) - len(plan.extra_chips)
            summary = f"本波 {plan.people}/{count}" + (f" 多 {len(plan.extra)} 人" if plan.extra else "")
        self._slot_cache = (key, (slots, summary))
        return slots, summary

    def refresh_slots(self, passed=""):
        """PASSED: 已过 N 人 up to the frame shown, said in front of the cells."""
        if not self._quick_ready:
            return
        slots, summary = self.wave_slot_list()
        target = self.current_target()
        self._mark_target_row(target[0], target[2])
        self.wave_slots.set_slots(slots, "　".join(part for part in (passed, summary) if part),
                                  getattr(self, "_wave_alert", ""))

    def _slot_clicked(self, index):
        """A filled cell: back to the frame that person was clicked on, to look again; nothing is changed."""
        plan = self._wave_plan()
        if plan is None or not 0 <= index < len(plan.marks):
            return
        frame = plan.marks[index].get("frame") or {}
        self.stop_run()
        self.show_counted(index)
        if frame.get("recorder_ms") is None:
            self.hint.setText("这个人被点的那一帧没有记下来，回不去。")
            return
        self.jump_requested.emit(dict(frame, reference_only=True))
        self.hint.setText(f"回到点 {plan.labels.get(index, '这个人')} 的那一帧（只看，不改动）；"
                          "要撤销按 4。")

    def _run_count(self, plan):
        """How far the wave is counted, said every time the recording moves or holds (11-11:
        once it moves on, the ring of whoever was just clicked is gone)."""
        chips = len(plan.chips) - len(plan.extra_chips)
        last = plan.labels.get(len(plan.marks) - 1, "") if plan.marks else ""
        return f"已点 {plan.people}/{chips} 人" + (f"（刚点的记为 {last} 号）" if last else "")

    def passed_count_text(self, frame):
        """已过 N 人 · X 号: everyone counted on this camera up to the frame shown.

        Like the timing software's running count, so the two can be read side
        by side (11-12).  One person is one spot on the picture: a rider with
        two chips counts once; 多出 people count, people set aside do not.
        """
        if frame is None:
            return ""
        current, camera = int(frame["recorder_ms"]), int(frame.get("camera_index") or 1)
        spots = {}
        for row in self._arrival_rows:
            saved = (row.decision or {}).get("frame") or {}
            if saved.get("marker_x_normalized") is not None and saved.get("recorder_ms") is not None:
                if self._row_state(row) == "已排除":
                    continue
                spot = (int(saved.get("camera_index") or 1), int(saved["recorder_ms"]),
                        round(float(saved["marker_x_normalized"]), 4), round(float(saved["marker_y_normalized"]), 4))
                spots.setdefault(spot, row.bib)
            elif row.mark is not None and not is_excluded(row.mark):
                spot = (int((row.mark.get("frame") or {}).get("camera_index") or camera), int(row.mark["recorder_ms"]),
                        round(float(row.mark["x"]), 4), round(float(row.mark["y"]), 4))
                spots.setdefault(spot, "")
        passed = sorted((spot[1], bib) for spot, bib in spots.items() if spot[0] == camera and spot[1] <= current)
        if not passed:
            return "已过 0 人"
        bib = passed[-1][1]
        return f"已过 {len(passed)} 人 · " + (f"{bib} 号" if bib else "无芯片")

    def _wave_even(self, plan, now):
        """This wave is counted even and looked through to its last chip."""
        last = max((int(row.event.timeline_timestamp_ms) for row in plan.chips), default=None)
        return not (last is None or now < last - RUN_NEAR_MS or not plan.marks or not plan.matches
                    or self._wave_outdated(plan) or self._unseen(plan))

    def _wave_counted(self, plan, now):
        """A wave counted even and looked through to its last chip: on to the next one by itself."""
        if not self._wave_even(plan, now):
            return False
        self._sweep_between(now, plan.item)
        return True

    def _following_row(self, item):
        """The row _next_wave opens after ITEM's wave, or None."""
        plan = self._wave_plan(item)
        keys = {chip.key for chip in plan.chips} if plan is not None else set()
        positions = [i for i, row in enumerate(self._arrival_rows) if row.key in keys]
        if not positions:
            return None
        return next((self._arrival_rows[i] for i in range(max(positions) + 1, len(self._arrival_rows))
                     if not self.table.isRowHidden(i)
                     and self._arrival_rows[i].item.item_id != item.item_id
                     and self._arrival_rows[i].item.evidence.get("wave_parent") != item.item_id), None)

    def show_counted(self, index):
        """Pick out someone counted on a wave's picture: their row is selected on the left.

        The picture stays where it is; 1 / 2 / 3, 4 and Backspace then act on this person.
        """
        plan = self._wave_plan()
        if plan is None or not 0 <= index < len(plan.marks):
            return
        self._editing_mark = index
        row = next((row for i, row in plan.assigned if i == index), None)
        row = row or self._decided_row(plan.marks[index], plan.item)
        mark = plan.marks[index]
        position = next((i for i, other in enumerate(self._arrival_rows)
                         if (other.key == row.key if row is not None else other.mark is mark)), None)
        if position is not None:  # 已排除 has no row; 多出 has its own
            self._selected_key = self._arrival_rows[position].key
            self.table.setCurrentCell(position, 0)
        self._sync_arrival_confirmation()

    @staticmethod
    def _decision_holds(decision, mark):
        """A saved confirmation is this very click: same frame, same spot."""
        frame = decision.get("frame") or {}
        return (frame.get("marker_x_normalized") is not None and frame.get("marker_y_normalized") is not None
                and mark_on_frame(mark, frame, frame.get("recorder_ms"))
                and abs(float(frame["marker_x_normalized"]) - mark["x"]) <= .001
                and abs(float(frame["marker_y_normalized"]) - mark["y"]) <= .001)

    def _rollback_wave_operation(self, scope, operation_id):
        """精确撤回本次操作；不碰去重返回的旧记录或后来写入的其他判定。"""
        try:
            self._arrival_store.undo(scope, operation_id=operation_id)
        except (OSError, RuntimeError, ValueError) as error:
            return f"；本次操作撤回失败：{error}，请核对已点记录，勿重复点人"
        return ""

    def _next_wave(self, item):
        """On to the next wave from its start, even if the end of this one was looked at nearly up to it.

        Never onto this wave's own 多出 / 无芯片 rows (Enter went round on its own
        多出 row, 10-29), nor onto a row 只看要处理的 hides.
        """
        plan = self._wave_plan(item)
        status = self._wave_status(plan)[0] if plan is not None else ""
        keys = {chip.key for chip in plan.chips} if plan is not None else set()
        positions = [i for i, row in enumerate(self._arrival_rows) if row.key in keys]
        following = next((i for i in range(max(positions) + 1, len(self._arrival_rows))
                          if not self.table.isRowHidden(i)
                          and self._arrival_rows[i].item.item_id != item.item_id
                          and self._arrival_rows[i].item.evidence.get("wave_parent") != item.item_id),
                         None) if positions else None
        if following is not None:
            self._open_row(following, keep=False)
        self.hint.setText((f"上一波：{status}。" if status else "已保存。")
                          + ("" if following is not None else "后面没有要看的了。"))

    def _split_wave_people(self, item, people, wave):
        """Give each counted person without a chip a picture, and so a row, of their own."""
        path = self.problem_dir / "manual_problems.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        known = {value["item_id"] for value in existing}
        children = []
        for mark in people:
            frame = dict(mark.get("frame") or {}, marker_x_normalized=mark["x"], marker_y_normalized=mark["y"])
            child = ProblemItem(stable_item_id("manual", item.item_id, mark["recorder_ms"], mark["x"], mark["y"]),
                                KIND_MANUAL_REVIEW, CATEGORY_VIDEO, "本波点出的人没有对应芯片，疑似漏读；芯片状态待核实",
                                evidence={"frame": frame, "recorder_ms": int(mark["recorder_ms"]),
                                          "operator_scope": self._scope, "wave_parent": item.item_id})
            if child.item_id not in known:
                existing.append(child.to_payload())
                known.add(child.item_id)
            children.append((child.item_id, mark))
        self.problem_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        owned = self._marks.setdefault(item.item_id, [])
        for child_id, mark in children:
            owned[:] = [value for value in owned if value is not mark]
            self._marks[child_id] = [dict(mark, wave_operation=wave)]
        self.save_drafts()
        self.refresh()
        for child_id, _mark in children:
            child = next((value for value in self.items if value.item_id == child_id), None)
            if child is not None:
                self._arrival_store.save(self._arrival_scope, "candidate:" + child_id, candidate_binding(child),
                                         "bicycle", candidate_id=child_id, frame=dict(child.evidence["frame"]),
                                         wave=wave, calibration_version=child.calibration_version)
        return children

    def _return_to_wave(self, row, exclude=False):
        """4 again (or Backspace) on someone split off a wave: they count for its chips again.

        With EXCLUDE (3) they go back as 非运动员 instead, still drawn on the wave.
        """
        child = row.item
        parent = next((item for item in self.items if item.item_id == child.evidence.get("wave_parent")), None)
        try:
            if row.entered is not None:
                self._arrival_store.undo(self._arrival_scope, key="bib:" + child.item_id)
            self._arrival_store.undo(self._arrival_scope, key="candidate:" + child.item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"撤回未保存：{error}")
            return
        moved = []
        if parent is not None:
            owned = self._marks.setdefault(parent.item_id, [])
            for mark in self._marks.pop(child.item_id, []):
                mark = {key: value for key, value in mark.items() if key not in ("no_chip", "wave_operation")}
                if exclude:
                    mark["exclusion_reason"] = "非运动员"
                owned.append(mark)
                moved.append(mark)
        self.save_drafts()
        plan = self._wave_plan(parent) if parent is not None else None
        if plan is None or not self._save_wave(plan):
            self._quick_render_signature = None
            self._render()
        # Pick them out again where they now are: their chip's row, or their 多出 row.
        position = next((i for i, other in enumerate(self._arrival_rows) for mark in moved
                         if other.mark is mark or other.decision and self._counted_here(other, parent)
                         and self._decision_holds(other.decision, mark)), None)
        if position is not None:
            self._arrival_clicked(position)
        self._sync_arrival_confirmation()
        self.marks_changed.emit(tuple(self.current_marks()))
        self.hint.setText("已排除这一位（非运动员）。" if exclude else
                          "已撤回无芯片：他回到这一波，按芯片顺序占号。" if moved else
                          "已撤回疑似漏读；这一波的画面已失效，请重新打开这一波。")
