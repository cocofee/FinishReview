"""Row building helpers of the quick review panel: events, laps, folding and row state."""

from __future__ import annotations

from .arrival_review import (
    ARRIVAL_REVIEWS_FILENAME,
    ArrivalReviewStore,
    candidate_binding,
    event_binding,
)
from .bunch_review import mark_on_frame, rider_count, uncovered_intervals
from .lap_wave import Passage, short_riders
from .problem_items import KIND_MANUAL_REVIEW, KIND_NO_CHIP_CROSSING, STATUS_WAITING
from .quick_review_common import (
    COUNTED_REACH_MS,
    EARLIER_BATCH,
    FRAGMENT_REACH_MS,
    LOCAL_CHIP_EXCLUSION,
    PASSAGE_VIEW_MARGIN_MS,
    UNVERIFIED_SPAN_MS,
    WAVE_VIEW_MARGIN_MS,
)
from .ui_parts import UiPart


class QuickReviewRowsPart(UiPart):
    """Store and events, laps, repeats, folded unverified rows and fragments, row state and lookup."""

    def _ensure_store(self):
        if self._host.problem_dir is None:
            return
        path = self._host.problem_dir / ARRIVAL_REVIEWS_FILENAME
        if self._host._arrival_store is None or self._host._arrival_store.path != path:
            self._host._arrival_store = ArrivalReviewStore(path)
        else:
            self._host._arrival_store.refresh()

    def _locally_excluded(self, event):
        entry = (self._host._arrival_store.latest(self._host._arrival_scope, "event:" + event.event_id,
                                            event_binding(event)) if self._host._arrival_store is not None else None)
        return bool(entry and entry["action"] == "held" and entry.get("reason") == LOCAL_CHIP_EXCLUSION)

    def _events(self):
        """Every chip passage of this race; lap k/N is a label, never a visibility filter."""
        events = tuple(self._host._events_provider())
        scopes = {(event.race_id, event.stage_id) for event in events}
        self._host._arrival_scope = self._host._scope if self._host._scope[0] else next(iter(scopes)) if len(scopes) == 1 else self._host._scope
        scoped = [event for event in events if (event.race_id, event.stage_id) == self._host._arrival_scope]
        metadata = self._host._metadata_provider()
        self._host._target_laps = ({group.group_id: group.target_laps for group in metadata.groups}
                             if metadata is not None and (metadata.race_id, metadata.stage_id) == self._host._arrival_scope
                             else {})
        self._host._short_bibs = self._host._short_of_laps([event for event in scoped if not self._host._locally_excluded(event)])
        return {event.event_id: event for event in scoped}

    def _short_of_laps(self, events):
        """Bibs whose last read is short of the group's target laps, in groups someone has finished.

        Before anyone finishes, every rider is short: nothing is said then.  A
        hint only (圈数异常只提示): the rider may not have finished, or a lap
        may have gone unread.
        """
        laps = self._host._target_laps
        active = [event for event in events if event.is_active and laps.get(event.group_id, 0) > 1]
        finished = {event.group_id for event in active if event.lap >= laps[event.group_id]}
        passages = [Passage(event.event_id, event.athlete_id or event.bib, int(event.timeline_timestamp_ms),
                            event.lap, laps[event.group_id])
                    for event in active if event.group_id in finished]
        short = short_riders(passages)
        bibs = {event.athlete_id or event.bib: event.bib for event in active}
        return tuple(sorted((bibs[rider] or rider for rider in short), key=lambda bib: (len(bib), bib)))

    def _category(self, item=None):
        item = item or self._host._candidate_item()
        return (self._host._arrival_store.latest(self._host._arrival_scope, "candidate:" + item.item_id,
                                           candidate_binding(item))
                if self._host._arrival_store is not None and item is not None else None)

    def _without_repeats(self, rows):
        """One row per person without a chip: the same person flagged twice on a wave is listed once.

        Older builds let a 无芯片 person be clicked and flagged again (古浪 10-28,
        two 疑似漏读 rows 0.02 apart on one frame), which exported them twice.
        Both stay drawn on the wave's picture, where a right click cancels one.
        """
        kept, seen = [], []
        for row in rows:
            marks = self._host._marks.get(row.item.item_id, ()) if row.event is None else ()
            parent = row.item.evidence.get("wave_parent")
            if parent and len(marks) == 1:
                mark = marks[0]
                if any(owner == parent and mark_on_frame(mark, other.get("frame") or {}, other["recorder_ms"])
                       and (mark["x"] - other["x"]) ** 2 + (mark["y"] - other["y"]) ** 2 < .02 ** 2
                       for owner, other in seen):
                    continue
                seen.append((parent, mark))
            kept.append(row)
        return kept

    def _fold_unverified(self, rows, assigned, counting):
        """Display-only buckets of uncertain motion, never a merged person count.

        Anchor on the first change, never chain. Keep each source item and its
        binding. Do not span a chip/confirmed person, recording source, model
        result, stale state or an independently saved manual ruling.
        """
        folded, leads = set(), {}
        for row in sorted(rows, key=lambda row: (row.time_ms if row.time_ms is not None else 2**62, row.key)):
            item = row.item
            category = self._host._category(item) or {}
            check = item.evidence.get("crossing_check") or {}
            is_unverified = self._host._unverified_picture(item)
            if row.event is not None or not is_unverified:
                leads.clear()  # A chip or known person separates review clips.
                continue
            frame = item.evidence.get("frame") or {}
            if (row.time_ms is None or row.mark is not None or row.decision is not None or row.entered
                    or item.item_id in counting or assigned[item.item_id] or self._host._marks.get(item.item_id)
                    or not frame.get("video_path") or not frame.get("segment_id")):
                leads.clear()
                continue
            state = (int(frame.get("camera_index") or 1), frame.get("session_id"), frame.get("video_path"),
                     item.calibration_version, item.evidence.get("analysis_signature"),
                     bool(item.evidence.get("configuration_stale")), bool(item.evidence.get("superseded")),
                     item.status == STATUS_WAITING, check.get("reason"), category.get("action"))
            lead = leads.get(state)
            if lead is not None and row.time_ms - lead.time_ms <= UNVERIFIED_SPAN_MS:
                lead.fragments += (row,)
                folded.add(row.key)
            else:
                leads[state] = row
        return [row for row in rows if row.key not in folded]

    def _fold_fragments(self, rows, assigned):
        """Pieces of one passage without a chip, within a second of the first, share its row.

        Display only: every piece keeps its own record, and a ruling on the row
        is saved on each. Only untouched pieces of a no-chip crossing fold, and
        only with pieces in the same state, so nothing judged or counted hides.
        """
        def foldable(row):
            item = row.item
            return (row.event is None and row.mark is None and row.decision is None and row.time_ms is not None
                    and item.kind == KIND_NO_CHIP_CROSSING and not item.evidence.get("wave_parent")
                    and ((item.evidence.get("crossing_check") or {}).get("state") == "confirmed"
                         or (self._host._category(item) or {}).get("action") == "bicycle")
                    and not self._host._marks.get(item.item_id) and not row.fragments and not assigned[item.item_id])
        folded, leads = set(), {}
        for row in sorted((row for row in rows if foldable(row)), key=lambda row: (row.time_ms, row.key)):
            state = (self._host._row_state(row), (self._host._category(row.item) or {}).get("action"),
                     int((row.item.evidence.get("frame") or {}).get("camera_index") or 1))
            lead = leads.get(state)
            if lead is not None and row.time_ms - lead.time_ms <= FRAGMENT_REACH_MS:
                lead.fragments += (row,)
                folded.add(row.key)
            else:
                leads[state] = row
        return [row for row in rows if row.key not in folded]

    def _fragment_unseen(self, row, whole=False):
        """What of a row, from its browsing lead to its last piece + 0.15 s, nobody has looked at.

        Only a folded row has to be seen whole before it is ruled on, or any row
        of a Shift+3 run (WHOLE).
        """
        if row is None or row.time_ms is None or not (row.fragments or whole):
            return []
        camera = str(int((row.item.evidence.get("frame") or {}).get("camera_index") or 1))
        seen = self._host._arrival_drafts.get("seen", {}).get(camera, [])
        last = (row.fragments[-1] if row.fragments else row).time_ms
        return uncovered_intervals(row.time_ms - self._host._passage_lead(row), last + PASSAGE_VIEW_MARGIN_MS,
                                   seen, tolerance=100)

    def _passage_lead(self, row):
        """Browsing lead: 0.15 s without a chip, no padding with one.

        可能多人: from the moment the other one came onto the line (15:50,
        10-29: the first two of 4 came on 1.9 s before the row's moment).  2 s
        before every crossing, and half a second more on 可能多人, was too far
        back: the rider was still far off.
        """
        if row is None or row.event is not None:
            return WAVE_VIEW_MARGIN_MS
        arrival = self._host._earlier_arrival(row, read=False)
        if arrival is None:
            return PASSAGE_VIEW_MARGIN_MS
        return max(PASSAGE_VIEW_MARGIN_MS, -(-int(row.time_ms - arrival) // 100) * 100)

    def _read_earlier(self):
        """A few crossings at a time; the table shows 可能多人 once they are read."""
        read = 0
        for row in self._host._arrival_rows:
            if read == EARLIER_BATCH:
                break
            key = (row.item.item_id, int(row.time_ms)) if row.time_ms is not None else None
            if row.event is None and key is not None and key not in self._host._earlier:
                self._host._earlier_arrival(row)
                read += key in self._host._earlier
        if {key for key, arrival in self._host._earlier.items() if arrival is not None} != self._host._earlier_shown:
            self._host._render()  # and on to the next few
        elif read == EARLIER_BATCH:
            self._host._earlier_timer.start()

    def _unverified_picture(self, item):
        """Line motion is evidence to inspect, not a person or an arrival.

        Also handles cached pre-fix no-chip items. Manual counts and explicit
        operator classifications remain usable even if the model lost the track.
        """
        if item.kind == KIND_MANUAL_REVIEW:
            return False
        if rider_count(self._host._marks.get(item.item_id, ())):
            return False
        category = self._host._category(item)
        if category and category["action"] in ("bicycle", "not_rider"):
            return False
        return (item.evidence.get("crossing_check") or {}).get("state") != "confirmed"

    def _candidate_shown(self, item, assigned, counting):
        """A picture is a row while it shows someone who is not yet a chip's crossing.

        The picture a wave is counted on is never a row of its own: its people
        are the wave's chip rows, and whoever is left over is split onto a row of
        their own when the wave is confirmed.  A split-off person is a row while
        they stand on it; undoing the wave takes them back into it.
        """
        if item.item_id in counting:
            return False
        if self._host._unverified_picture(item) and not self._host._show_unverified:
            return False
        marks = self._host._marks.get(item.item_id, ())
        if item.evidence.get("wave_parent"):
            return assigned[item.item_id] < rider_count(marks)
        return assigned[item.item_id] < max(1, len(marks))

    def _near_counted_moment(self, item, assigned, moments):
        """A detected crossing nobody ruled on, at the moment someone was counted on a wave's picture.

        Most likely that person, maybe someone nobody counted: a row, 已点附近
        (COUNTED_REACH_MS).
        """
        if not moments or self._host._marks.get(item.item_id) or assigned[item.item_id] or self._host._category(item) is not None:
            return False
        frame = item.evidence.get("frame") or {}
        stamp = item.evidence.get("recorder_ms", frame.get("recorder_ms"))
        near = moments.get(int(frame.get("camera_index") or 1), ())
        return stamp is not None and any(abs(int(stamp) - moment) <= COUNTED_REACH_MS for moment in near)

    def _counted_moments(self, counting):
        """Camera -> recorder moments of everyone counted on a wave's picture, those without a chip too."""
        moments = {}
        for item in self._host.items:
            if item.item_id in counting or item.evidence.get("wave_parent") in counting:
                for mark in self._host._marks.get(item.item_id, ()):
                    camera = int((mark.get("frame") or {}).get("camera_index") or 1)
                    moments.setdefault(camera, []).append(int(mark["recorder_ms"]))
        return moments

    def _row_state(self, row):
        if row.event is not None and not row.event.is_active:
            return "已排除"
        if row.event is None and self._host._unverified_picture(row.item):
            return "画面待核"
        if row.stale:
            return "待定"
        if row.decision:
            # A click on someone only counts them; their bib is the chip's turn in
            # the wave, so it is 已点, not a confirmed identity (10-29).
            if row.decision.get("auto_label"):
                return "已接受对应"
            return "已点" if (row.decision.get("frame") or {}).get("marker_x_normalized") is not None else "已确认"
        if row.note:
            return row.note
        category = self._host._category(row.item)
        if category and category["action"] == "not_rider":
            return "已排除"
        if (row.item.status == STATUS_WAITING or row.item.evidence.get("configuration_stale")
                or row.item.evidence.get("superseded")):
            return "待定"
        if category and category["action"] == "bicycle" and (row.item.evidence.get("wave_parent")
                                                             or category.get("no_chip")):
            return "补录" if row.entered else "疑似漏读"
        return "待定"

    @staticmethod
    def _row_group(row):
        event = row.event
        if event is None:
            return ""
        return (getattr(event, "group_name", "") or "").strip() or str(getattr(event, "group_id", "") or "")

    def _find(self):
        """Jump to the next row whose bib is the text, or whose bib or name holds it."""
        text = self._host.search.text().strip()
        if not text:
            return
        rows = self._host._arrival_rows
        exact = [i for i, row in enumerate(rows) if row.bib == text]
        found = exact or [i for i, row in enumerate(rows)
                          if text in row.bib or text in (getattr(row.event, "athlete_name", "") or "")]
        if not found:
            self._host.hint.setText(f"没有找到 {text}")
            return
        if any(self._host._row_state(rows[i]) == "已排除" for i in found):
            self._host.show_excluded_action.setChecked(True)
        current = self._host.table.currentRow()
        index = next((i for i in found if i > current), found[0])
        self._host._arrival_clicked(index)
        self._host.table.scrollToItem(self._host.table.item(index, 0))
        # Back on the table, so Enter / Backspace work at once.
        self._host.table.setFocus()

    def _row_category(self, row):
        if row.note == "已排除":
            return "非运动员"
        if row.decision:
            return "运动员"
        category = self._host._category(row.item)
        return ({"bicycle": "运动员", "not_rider": "非运动员"}.get(category["action"], "待判定")
                if category and not row.stale else "待判定")
