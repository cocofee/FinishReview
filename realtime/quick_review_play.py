"""Play to the next person on the quick review panel, and the automatic review stepping."""

from __future__ import annotations

import uuid

from PyQt5.QtCore import QTimer

from .bunch_review import is_excluded
from .crossing_time import workspace_clocks
from .quick_review_common import (
    REVIEW_STEP_DELAY_MS,
    REVIEW_STEP_FRAMES,
    RUN_FAST_MIN_MS,
    RUN_FAST_SPEED,
    RUN_NEAR_MS,
    RUN_STEP_MS,
    RUN_TAIL_MS,
    SAME_FRAME_MS,
    WAVE_VIEW_REACH_MS,
    WavePlan,
)


class QuickReviewPlayMixin:
    """Play on to the next person or counted mark, run offsets and targets, automatic review stepping."""

    def _run_slow(self):
        """Back to normal speed; True when the run was playing fast."""
        fast, self._run_fast = self._run_fast, False
        if fast:
            self.run_speed.emit(1.0)
        return fast

    def _forget_run(self, _frame=None):
        """A jump (a row, the strip, a counted person's frame) ends any run quietly.

        The window holds the recording on every jump; what the run was to do at
        its end must not happen later somewhere else (11-15: 63 号 → 23 号).
        """
        self._run_to = None
        self._run_between = self._run_counted = None
        self._run_slow()

    def stop_run(self):
        """Hold the recording a click set playing; True when it was."""
        self._run_from = None
        if self._run_to is None:
            return False
        self._run_to = None
        self._run_slow()
        self._run_between = self._run_counted = None
        self.run_requested.emit(False)
        return True

    def _run_offset(self, plan):
        """(ms, {mark index: crossing ms}): how far this wave's clicks run off their chips.

        Where people are clicked on the picture runs some hundred ms off their
        chips (the clock, where on the bike the line is met).  In a tight wave
        the next chip then looked already gone and the run went through the
        whole wave (11-11).  This wave's own clicks say how far off it runs.
        """
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        key = (self.workspace_dir, id(clocks), tuple(row.key for row in plan.chips),
               tuple((mark["recorder_ms"], (mark.get("frame") or {}).get("session_id", "")) for mark in plan.marks),
               tuple((index, row.key) for index, row in plan.assigned))
        if self._offset_cache is not None and self._offset_cache[0] == key:
            return self._offset_cache[1]
        result = self._compute_run_offset(plan)
        self._offset_cache = (key, result)
        return result

    def _run_plan(self):
        """The wave being looked through: its picture's plan, or before anyone is
        clicked on it (no picture yet), its chips alone."""
        plan = self._wave_plan()
        if plan is not None:
            return plan
        wave = self._row_wave(self._selected_arrival())
        if not wave:
            return None
        plan = WavePlan(None, list(wave), [])
        plan.extra_chips = [row for row in wave if row.note == "多余芯片"]
        plan.unmatched = [row for row in wave if row.decision is None and row.note != "多余芯片"]
        plan.people = sum(row.decision is not None for row in wave)
        return plan

    def play_on(self):
        """Space on a wave: play on to the next person and stop there, as after a click (11-12).

        Space used to play the recording straight on, and in a big bunch
        everyone went past before one could be clicked (古浪).  Someone due
        right here but not at the line yet: RUN_STEP_MS on, then stop again.
        False with no wave on screen or past its tail: Space then plays as always.
        """
        # Space goes on from the frame shown, whatever was being swept before (11-15).
        self._run_between = None
        self._run_counted = None
        row = self._selected_arrival()
        if row is not None and row.key in self._auto_labels and self._auto_review is None:
            if self._accept_auto_wave(row):   # looked at, right: on to the next wave (11-15)
                return True
            # Not accepted (the hint says why): Space still goes on from the frame shown.
            refused = self.hint.text()
            moved = self._play_on_from_here()
            self.hint.setText(refused + ("　" + self.hint.text() if moved else ""))
            return moved
        return self._play_on_from_here()

    def _play_on_from_here(self):
        plan = self._run_plan()
        now = self._shown_ms
        if now is None or self._auto_review is not None:
            return False
        counted = self._next_counted(now, plan)
        if self._close_behind(now, plan):
            entry = self._next_on_line(now)
            if counted is not None and (entry is None or counted[0] <= entry):
                return self._run_to_counted(counted, plan)
            if entry is None:
                return self._step_on(now, plan)
            if entry != float("inf"):
                return self._run_to_entry(entry, plan)
        if plan is None or not plan.chips:
            return self._sweep_on(now)
        offset, _crossings = self._run_offset(plan)
        end = max(int(row.event.timeline_timestamp_ms) for row in plan.chips) + offset + RUN_TAIL_MS
        if now >= end - RUN_NEAR_MS:
            if self._wave_even(plan, now) and self._following_row(plan.item) is not None:
                self._sweep_between(now, plan.item)
                return True
            return self._sweep_on(now)
        target = self._run_target(plan, now, counted)
        if target and counted is not None and target == (int(counted[0]), False):
            return self._run_to_counted(counted, plan)  # never swept past fast (11-15 review)
        step = target == ()
        if step:
            target = (min(now + RUN_STEP_MS, end), False)
        if not target:
            return False
        self._run_to, self._run_tail = target
        # A stretch the strip shows empty is played fast, not jumped: every frame is still seen.
        self._run_fast = not step and self._run_swept and target[0] - now >= RUN_FAST_MIN_MS
        self.hint.setText(self._run_count(plan) + "；"
                          + ("录像往前走一点" if step else "录像走到本波末尾" if self._run_tail else "录像走到下一位")
                          + ("（快扫，线上一有人影就停）" if self._run_fast else "")
                          + "，中间有人就按空格停下再点他。")
        self.run_speed.emit(RUN_FAST_SPEED if self._run_fast else 1.0)
        self.run_requested.emit(True)
        return True

    def _close_behind(self, now, plan):
        """NOW is within CLOSE_BEHIND_MS after a chip or after someone counted (see CLOSE_BEHIND_MS)."""
        reach = self._close_behind_ms
        if reach <= 0:
            return False
        moments = [int(row.event.timeline_timestamp_ms) for row in self._arrival_rows if row.event is not None]
        moments += [row.time_ms for row in self._arrival_rows if row.time_ms is not None]
        if self._clicked_at is not None:
            moments.append(self._clicked_at)
        if plan is not None and plan.marks:
            moments += list(self._run_offset(plan)[1].values())
        return any(moment - RUN_NEAR_MS <= now < moment + reach for moment in moments)

    def _next_counted(self, now, plan):
        """(moment, label) of the next person already clicked on this wave, ahead of NOW.

        Going back over a wave counted even, no chip is left to stop at and a
        tight bunch shows on the strip as one long shape: Space ran past many
        (11-15 古浪, from 73 号 on).  It goes from one clicked person to the next.
        """
        if plan is None or not plan.marks:
            return None
        ahead = sorted((moment, index) for index, moment in self._run_offset(plan)[1].items()
                       if moment > now + SAME_FRAME_MS and not is_excluded(plan.marks[index]))
        if not ahead:
            return None
        moment, index = ahead[0]
        return moment, plan.labels.get(index, ""), index

    def _run_to_counted(self, counted, plan):
        """On to the next person already clicked, at normal speed, and stop on them.

        The run stops RUN_STOP_LEAD_MS short; on arrival the very frame they
        were clicked on opens and their row is selected (_land_on_counted).
        """
        moment, label, index = counted
        self._run_counted = index
        self._run_to, self._run_tail, self._run_fast = int(moment), False, False
        self.hint.setText(f"{self._run_count(plan)}；到下一位已点的人"
                          + (f"（{label}）" if label else "") + "，中间有没点到的人就按空格停下再点他。")
        self.run_speed.emit(1.0)
        self.run_requested.emit(True)
        return True

    def _run_to_entry(self, entry, plan):
        """Right behind the one counted someone comes onto the line: play there at normal speed and stop."""
        self._run_to, self._run_tail, self._run_fast = int(entry), False, False
        counted = self._run_count(plan) + "；" if plan is not None and plan.chips else ""
        self.hint.setText(counted + "后面紧跟着有人上线，停在他压线的地方：是运动员就点他，不是就再按空格。")
        self.run_speed.emit(1.0)
        self.run_requested.emit(True)
        return True

    def _step_on(self, now, plan):
        """Space right behind someone: RUN_STEP_MS on at normal speed, then stop again."""
        self._run_to, self._run_tail, self._run_fast = now + RUN_STEP_MS, False, False
        counted = self._run_count(plan) + "；" if plan is not None and plan.chips else ""
        self.hint.setText(counted + "刚过一个人，录像往前走一点（0.3 秒）：紧跟在后面的人会停在画面上，有就点他。")
        self.run_speed.emit(1.0)
        self.run_requested.emit(True)
        return True

    def _run_on(self):
        """After a click on a wave's picture: say how far it is counted; the recording holds."""
        now, self._run_from = self._run_from, None
        plan = self._wave_plan()
        if now is None or plan is None or not plan.marks or self._auto_review is not None:
            return
        # The recording stays where it was clicked (11-14: in a bunch a run to
        # "the next person" took riders past before one could be pinned, "我根本
        # 就盯不住那个人").  The wheel walks a frame at a time, Space goes on.
        if self._run_target(plan, now) is None:  # nobody left and the tail already passed
            self._wave_counted(plan, now)
        elif plan.unmatched:
            self.hint.setText(f"{self._run_count(plan)}；录像停在这里：滚轮一帧一帧走，"
                              f"空格到下一位（{plan.unmatched[0].bib} 号）。")
        else:
            self.hint.setText(f"{self._run_count(plan)}；录像停在这里：后面还有人压线就点他，"
                              "没有就按空格看到本波末尾。")

    def _land_on_counted(self, index):
        """Stopped at someone already clicked: their own frame, their row, nothing to click again.

        11-15 review: the run stopped two frames short and said "机器看到有人过线：
        是运动员就点" — clicking the same rider again made the wave 多 1, and the
        next Space came back to the same person.
        """
        plan = self._wave_plan()
        if plan is None or not 0 <= index < len(plan.marks):
            return
        frame = plan.marks[index].get("frame") or {}
        self.show_counted(index)
        if frame.get("recorder_ms") is not None:
            self.jump_requested.emit(dict(frame, reference_only=True))
        label = plan.labels.get(index, "")
        self.hint.setText(f"{self._run_count(plan)}；这是已点的" + (f" {label}" if label else "")
                          + "（不用再点）：空格到下一位，中间有没点到的人就点他。")

    def _run_arrived(self, tail):
        counted, self._run_counted = self._run_counted, None
        if counted is not None:
            self._run_between = None
            self._land_on_counted(counted)
            return
        between, self._run_between = self._run_between, None
        if between is not None and self._run_to is None:
            item, arrived = between
            if arrived == "figure":
                self.hint.setText("线上有人影，停下了：是运动员就点他；不是就按空格继续扫到下一波。")
            elif self._shown_ms is not None:
                self._sweep_between(self._shown_ms, item, arrived == "there")
            return
        plan = self._run_plan()
        if plan is None or self._run_to is not None:
            return
        if tail and self._shown_ms is not None and self._wave_counted(plan, self._shown_ms):
            return
        status = self._wave_status(plan)[0] if plan.marks or plan.split else ""
        if tail:
            self.hint.setText((f"本波看到末尾：{status}。" if status else "本波看到末尾。") + (
                "回车到下一波。" if plan.matches
                else "多的人按 3 补录或 1 排除，多的芯片按 2；处理完回车到下一波。"))
        elif plan.unmatched:
            self.hint.setText(f"{self._run_count(plan)}；到下一位（{plan.unmatched[0].bib} 号）：点压线的人；"
                              "还没到线按空格往前走，← → 一帧一帧。")
        else:
            self.hint.setText("这里机器看到有人过线：是运动员就点，不是就按空格继续。")

    def current_target(self, plan=None):
        """(row key, words, kind) for the person the operator is on; kind is next, done or alert.

        Yellow means one thing only, the next person to click; clicked, it turns
        green (11-14: "这个黄色是下一个还是…变化有点怪").

        The next click's bib while the wave has chips left; once it is counted
        even, whoever is selected (just clicked).  11-14: "我现在点到哪一个人物，
        好像不能一下看出来": it is said on the left, in the cells and on the picture.
        """
        plan = plan or self._run_plan()
        if plan is None:
            return None, "", "done"
        if plan.extra:
            return None, f"多 {len(plan.extra)} 人", "alert"
        if plan.unmatched:
            return plan.unmatched[0].key, f"下一位 {plan.unmatched[0].bib} 号", "next"
        row = self._selected_arrival()
        chips = {chip.key for chip in plan.chips}
        if row is not None and row.key in chips and row.bib:
            return row.key, f"{row.bib} 号 已点", "done"
        return None, "本波点齐", "done"

    def _start_auto_review(self, keys):
        self._auto_review = {"keys": list(keys), "total": len(keys), "done": 0, "target": None,
                             "jumps": 0, "operation": uuid.uuid4().hex, "last_key": None}
        self._next_auto_review()

    def _review_row(self, key):
        index = next((i for i, row in enumerate(self._arrival_rows)
                      if row.key == key or any(piece.key == key for piece in row.fragments)), None)
        return (index, self._arrival_rows[index]) if index is not None else (None, None)

    def _next_auto_review(self):
        """Exclude the run's rows in order; a row not yet seen whole is played first."""
        review = self._auto_review
        if review is None:
            return
        while review["keys"]:
            index, row = self._review_row(review["keys"][0])
            if row is None or not self._excludable(row):
                review["keys"].pop(0)
                continue
            if self.table.currentRow() != index:
                self._open_row(index, keep=False)
            unseen = self._fragment_unseen(row, whole=True)
            if unseen:
                review["target"], review["jumps"] = row.key, 0
                self._review_seek(row, unseen)
                return
            if not self._exclude_seen(row, review["operation"]):
                self._auto_review = None
                self._review_timer.stop()
                return
            review["done"] += 1
            review["last_key"] = "candidate:" + row.item.item_id
            review["keys"].pop(0)
        self._auto_review = None
        self._review_timer.stop()
        done = review["done"]
        if done:
            self._step(1)
        self._last_saved_key = review["last_key"]  # Backspace takes back the whole run
        self.hint.setText(f"已排除 {done} 行；4 或退格一次全部撤回。" if done > 1
                          else "已排除此画面候选。" if done else "没有可排除的待定行。")

    def _review_seek(self, row, unseen):
        """Go to the first part of the row nobody has seen, else step on from here."""
        review = self._auto_review
        start, end = unseen[0]
        self.hint.setText(f"正在补看（第 {review['done'] + 1}/{review['total']} 行）："
                          f"还差 {sum(right - left for left, right in unseen) / 1000:.1f} 秒，"
                          "看完自动排除；按任意键停止。")
        self._review_timer.start()
        shown = self._shown_ms
        # Close before the part not seen yet (a jump that stopped at the end of
        # a short clip): stepping on reaches it, into the next file if need be.
        if shown is not None and (start - 300 <= shown <= end + WAVE_VIEW_REACH_MS
                                  or review["jumps"] and start - 3000 <= shown < start):
            # One step at a time: a decoded frame is reported more than once.
            if not review.get("stepping"):
                review["stepping"] = True
                QTimer.singleShot(REVIEW_STEP_DELAY_MS, self._review_step)
            return
        review["jumps"] += 1
        frame = self._review_frame(start, row) if review["jumps"] <= 6 else None
        if frame is None:
            self._stop_auto_review("这一段没有可补看的录像画面，这一行未排除；请手动看完再按 1 排除。")
            return
        self.jump_requested.emit(dict(frame, reference_only=True))

    def _review_step(self):
        review = self._auto_review
        if review is not None:
            review["stepping"] = False
            review["idle_steps"] = review.get("idle_steps", 0) + 1
            self.hint.setText(f"正在补看（第 {review['done'] + 1}/{review['total']} 行），"
                              "看完自动排除；按任意键停止。")
            self.frame_step_requested.emit(REVIEW_STEP_FRAMES)

    def _advance_auto_review(self):
        """A frame was shown while 3 plays a row: go on, or exclude once all of it is seen."""
        review = self._auto_review
        if review["target"] is None:
            return  # between rows: the next one is opened outside this callback
        row = self._selected_arrival()
        if row is None or (row.key != review["target"]
                           and not any(piece.key == review["target"] for piece in row.fragments)):
            self._stop_auto_review("换了一行，自动补看已停止；这一行未排除。")
            return
        unseen = self._fragment_unseen(row, whole=True)
        if unseen:
            if self._shown_ms != review.get("shown"):
                review["shown"], review["idle_steps"] = self._shown_ms, 0
            if review.get("idle_steps", 0) >= 8:
                # Steps that no longer move the picture on: the recording ends here.
                self._stop_auto_review("这一段录像放到头了，补看不完，这一行未排除；请手动看完再按 1 排除。")
                return
            self._review_seek(row, unseen)
            return
        self._review_timer.stop()
        review["target"] = None
        QTimer.singleShot(0, self._next_auto_review)  # rule outside the frame callback

    def _exclude_seen(self, row, operation):
        """Exclude a row seen whole, on the frame shown if it is this row's, else the row's own."""
        frame = self._row_frame(row)
        if not self._frame_usable(frame):
            self.hint.setText("请先打开当前候选的有效录像画面。")
            return False
        if not self._save_ruling(row.item, [piece.item for piece in row.fragments], "not_rider", frame, operation):
            return False
        self._render()
        return True

    def _stop_auto_review(self, message):
        review, self._auto_review = self._auto_review, None
        self._review_timer.stop()
        if review is None:
            return
        if review["done"]:
            self._last_saved_key = review["last_key"]
            message = f"{message}前面已排除 {review['done']} 行，4 或退格一次全部撤回。"
        self.hint.setText(message)
