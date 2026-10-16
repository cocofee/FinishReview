"""The compact controls and the crossing-card rail of the finish workbench.

Both widgets are presentation only: the caller owns the marks, the chips and
the stores.  ``CrossingReviewControls`` sits under the evidence picture and
says what the operator did — counted people, recognised numbers, exclusions,
the keep/delete decisions of a multi-chip crossing and the evidence reasons
that settle the child doubts of the same crossing; ``CrossingCardRail`` is the
row of one-per-person (or one-per-group) cards that opens the original frame
again, with the workbench's own controls on the same line as its title.

Two rules from the field workflow are built in here, because this is the last
cheap place to keep them:

* a count and a record are different numbers — the summary reads
  ``已确认 N 人 · M 条有效记录`` and never adds one to the other;
* a chip decision needs an explicitly chosen member, and a doubt needs an
  explicitly chosen child — the first record is never selected for the
  operator, and nothing is decided by pointing at it.

Nothing here writes a journal, a draft or a chip; every change leaves the
widget as a signal, and the caller decides what to store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .chip_history import format_chip_time
from .problem_items import (
    ACTION_INSUFFICIENT,
    ACTION_KEEP,
    ACTION_LABELS,
    ACTION_NOT_RIDER,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
    ACTION_SIDE_BY_SIDE,
    CLASS_TITLES,
    STATUS_CLOSED_UPSTREAM,
    STATUS_DISPUTED,
    STATUS_EXCLUDED,
    STATUS_INFO,
    STATUS_OPEN,
    STATUS_WAITING,
    TYPE_CONFIRM,
    ProblemItem,
    problem_class,
)


# The reasons an operator excludes a person with.  The list is short on purpose,
# and the string that is emitted is exactly the one shown here, so the reason
# travels to the draft with no second vocabulary.
EXCLUSION_REASONS = ("行人", "工作人员", "车辆", "往回骑或推车", "其他非选手")

# The short state a card carries: the queue's own words, kept local so this
# module stays free of the panel's stores.
STATUS_LABELS = {
    STATUS_OPEN: "待处理",
    STATUS_DISPUTED: "有争议",
    STATUS_WAITING: "待证据",
    STATUS_EXCLUDED: "已排除",
    STATUS_CLOSED_UPSTREAM: "CycleRace 已处理",
    STATUS_INFO: "参考",
}

# The one line a member combo shows before the operator has chosen a record;
# it carries no event id, so the two decision buttons start disabled.
MEMBER_PLACEHOLDER = "选择一条芯片记录…"

# The child doubts of one crossing are settled by their evidence only.  A coarse
# chip deletion or a plate recording belongs to the row that owns the exact
# record, so only these reasons are offered here.
CONFLICT_ACTIONS = (
    ACTION_NOT_RIDER,
    ACTION_RIDE_BACK,
    ACTION_SAME_CROSSING,
    ACTION_SIDE_BY_SIDE,
    ACTION_KEEP,
    ACTION_INSUFFICIENT,
)
CONFLICT_PLACEHOLDER = "选择一条疑点…"

# What an untouched single rider looks like: matched by the analysis, counted
# without an operator.  It must not read as a missed chip or as "reference".
AUTO_PASSED_TITLE = "正常单人"
AUTO_PASSED_STATE = "已自动对应"

RAIL_ICON_SIZE = QSize(150, 95)
RAIL_ENTRY_SIZE = QSize(158, 140)
# The rail is one row of cards: the entry, its padding and the horizontal
# scrollbar.  Fixed, so a layout never squeezes it to nothing or stretches it
# over the picture it belongs to.
RAIL_LIST_HEIGHT = RAIL_ENTRY_SIZE.height() + 20
# Card pixmaps are cached by path and modification stamp: the workbench refreshes
# the rail in place, and re-reading a dozen JPEGs on every refresh would stall it.
ICON_CACHE_LIMIT = 64
# Previews are frames the workbench already decoded and handed to one card (a
# normal single rider has no backend JPEG at all).  Kept per item id, newest 64,
# so opening a card again does not ask for the same pane twice.
PREVIEW_CACHE_LIMIT = 64


def _exclusion_reason(mark: Mapping[str, Any]) -> str:
    return str(mark.get("exclusion_reason") or "").strip()


def _excluded(mark: Mapping[str, Any]) -> bool:
    """The same rule as ``bunch_review.is_excluded``: only an explicit reason excludes.

    Old drafts that merely counted riders read as riders and not as exclusions.
    """

    return bool(_exclusion_reason(mark))


def member_void(member: Mapping[str, Any]) -> bool:
    """A record the upstream already voided is shown but is no valid record.

    Its state also keeps the two decision buttons away: a voided record is not
    decided again here, exactly as the batch log refuses to.
    """

    return "作废" in str(member.get("state") or "")


def _mark_key(mark: Mapping[str, Any]) -> tuple:
    """Who a mark is: the moment and the point on the frame, never the number.

    The key keeps the selection on the same person when the marks are rebuilt,
    and it tells a rebuilt one from the one that was already on screen — while a
    number the operator is still typing cannot look like a different person.
    """

    frame = mark.get("frame")
    if frame:
        return (mark.get("recorder_ms"), tuple(sorted((str(key), str(value)) for key, value in dict(frame).items())))
    return (mark.get("recorder_ms"), mark.get("x"), mark.get("y"))


def _rail_time_ms(item: ProblemItem) -> Optional[int]:
    """When this card happened, in the recorder's own clock.

    The recorded moment is what the rail is chronological by; the frame's own
    recording stamp is the fallback, and the official chip time only the last
    resort.  It is the time a card must not hide, so it is named apart in the
    tooltip instead of standing in for when the picture was taken.
    """

    evidence = item.evidence or {}
    value = evidence.get("recorder_ms", evidence.get("expected_recorder_ms"))
    if value is None:
        value = (evidence.get("frame") or {}).get("recorder_ms")
    if value is None:
        value = item.chip_time_ms
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rail_order(item: ProblemItem) -> tuple:
    when = _rail_time_ms(item)
    return (when is None, when or 0, item.item_id)


def _item_summary(item: ProblemItem) -> str:
    """The one line a row carries: numbers, class and state, never a sentence."""

    if item.evidence.get("auto_passed"):
        title, state = AUTO_PASSED_TITLE, AUTO_PASSED_STATE
    else:
        title = CLASS_TITLES.get(problem_class(item), CLASS_TITLES[TYPE_CONFIRM])
        state = STATUS_LABELS.get(item.status, item.status)
    bibs = "、".join(item.bibs[:4]) + ("…" if len(item.bibs) > 4 else "")
    return " · ".join(part for part in (bibs, title, state) if part)


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _frame_key(frame: Any) -> tuple:
    return tuple(sorted((str(key), str(value)) for key, value in dict(frame or {}).items()))


def _file_stamp(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


class CrossingReviewControls(QFrame):
    """The group / count / chip actions that fit under the evidence picture.

    ``set_review`` is the only way data gets in, and it is silent: it repaints
    the fields and never emits.  Only the operator's own keys and clicks emit,
    so a refresh that lands between two keystrokes cannot decide anything.
    """

    # A person on the frame was picked in the list (their index into the marks).
    mark_selected = pyqtSignal(int)
    # A number was read off the frame for that person; an empty string clears it.
    bib_assigned = pyqtSignal(int, str)
    # That person is not a rider: the reason is explicit, and "" restores them.
    exclusion_changed = pyqtSignal(int, str)
    # A mis-clicked person; the excluded ones are restored before they are removed.
    mark_removed = pyqtSignal(int)
    # One chip record of this crossing is kept or suggested for deletion.
    member_decision = pyqtSignal(str, str)
    # A child doubt of this crossing is the one being looked at.
    conflict_selected = pyqtSignal(str)
    # That child doubt is settled by this evidence reason.
    conflict_decided = pyqtSignal(str, str)
    # The operator says they have seen the whole group.
    fully_viewed_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("crossingReviewControls")
        # The caller's data, read only; the widget holds no draft of its own.
        self._marks: tuple = ()
        self._members: tuple = ()
        self._conflicts: tuple = ()
        self._selected_mark = -1
        self._bib_loaded_for = -1
        self._bib_loaded_key: Optional[tuple] = None
        self._selected_member = ""
        self._selected_conflict = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.summary = QLabel("已确认 0 人 · 0 条有效记录", self)
        self.summary.setStyleSheet("font-weight: 600;")
        self.summary.setToolTip("人数是画面里点出来的人；记录数是本处的芯片记录，两者不能互相替代；"
                                "已排除的人不计入人数。")
        top.addWidget(self.summary)
        self.notice = QLabel("", self)
        self.notice.setWordWrap(False)
        # The line of counted seconds must never widen the strip under the
        # picture: it takes what is left and is read in full from its tooltip.
        self.notice.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.notice.setStyleSheet("color: #667085; font-size: 9pt;")
        self.notice.hide()
        top.addWidget(self.notice, 1)
        layout.addLayout(top)

        # The people counted on the frame: follow them by number and time,
        # recognise a number, exclude a passer-by, take a mis-click back.  Two
        # short rows, so the row still fits a laptop width beside the picture.
        self.mark_group = QWidget(self)
        mark_layout = QVBoxLayout(self.mark_group)
        mark_layout.setContentsMargins(0, 0, 0, 0)
        mark_layout.setSpacing(3)
        numbers_row = QHBoxLayout()
        numbers_row.setSpacing(4)
        self.mark_combo = QComboBox(self.mark_group)
        self.mark_combo.setMinimumWidth(150)
        self.mark_combo.setToolTip("画面里点出来的人：序号 · 录像时间 · 号码 / 已排除")
        # Only the operator's own choice announces itself; filling the combo from
        # set_review or select_mark must never look like a decision.
        self.mark_combo.activated[int].connect(self._mark_activated)
        numbers_row.addWidget(self.mark_combo, 1)
        self.bib_input = QLineEdit(self.mark_group)
        self.bib_input.setPlaceholderText("号码（可留空）")
        self.bib_input.setMaximumWidth(120)
        self.bib_input.setToolTip("认得出号码牌就填；看不清先留空，按回车或「记号码」保存")
        self.bib_input.returnPressed.connect(self._assign_bib)
        numbers_row.addWidget(self.bib_input)
        self.assign_button = QPushButton("记号码", self.mark_group)
        self.assign_button.setToolTip("把填写的号码记在选中的人身上；留空表示暂不认号")
        self.assign_button.clicked.connect(self._assign_bib)
        numbers_row.addWidget(self.assign_button)
        mark_layout.addLayout(numbers_row)
        exclusion_row = QHBoxLayout()
        exclusion_row.setSpacing(4)
        self.exclusion_combo = QComboBox(self.mark_group)
        self.exclusion_combo.addItems(EXCLUSION_REASONS)
        self.exclusion_combo.setToolTip("排除依据：排除一定要写明原因，原帧和原因都会留在记录里")
        exclusion_row.addWidget(self.exclusion_combo)
        self.exclude_button = QPushButton("排除", self.mark_group)
        self.exclude_button.setToolTip("按所选原因把这位记为非选手：不计入人数，不算补录")
        self.exclude_button.clicked.connect(self._exclude)
        exclusion_row.addWidget(self.exclude_button)
        self.restore_button = QPushButton("恢复为选手", self.mark_group)
        self.restore_button.setToolTip("去掉排除原因，把这位放回选手计数，再重新核对号码")
        self.restore_button.clicked.connect(self._restore)
        exclusion_row.addWidget(self.restore_button)
        self.remove_button = QPushButton("删除误点", self.mark_group)
        self.remove_button.setToolTip("点错的人直接去掉；已排除的人先恢复再删除")
        self.remove_button.clicked.connect(self._remove)
        exclusion_row.addWidget(self.remove_button)
        exclusion_row.addStretch(1)
        mark_layout.addLayout(exclusion_row)
        layout.addWidget(self.mark_group)

        # The crossing's own chip records: keep the valid ones, suggest the
        # deletion of the extras.  One at a time, and only one explicitly chosen.
        self.member_group = QWidget(self)
        member_row = QHBoxLayout(self.member_group)
        member_row.setContentsMargins(0, 0, 0, 0)
        member_row.setSpacing(4)
        self.member_combo = QComboBox(self.member_group)
        self.member_combo.setMinimumWidth(200)
        self.member_combo.setToolTip("本次过线的芯片记录：号码 · 芯片时间 · 状态；先在这里选一条，再决定")
        self.member_combo.activated[int].connect(self._member_activated)
        member_row.addWidget(self.member_combo, 1)
        self.keep_button = QPushButton("确认有效", self.member_group)
        self.keep_button.setToolTip("这条记录对应的是本次有效过线，保留")
        self.keep_button.clicked.connect(lambda: self._decide_member("keep"))
        member_row.addWidget(self.keep_button)
        self.delete_button = QPushButton("建议删除", self.member_group)
        self.delete_button.setToolTip("这条记录是多余读数，作为建议交给 CycleRace，不在这里改写成绩")
        self.delete_button.clicked.connect(lambda: self._decide_member("delete"))
        member_row.addWidget(self.delete_button)
        layout.addWidget(self.member_group)

        # The child doubts of this crossing: look at one, and settle it by the
        # evidence that can be seen.  Deleting chips and recording plates stay in
        # the rows that own those exact records.
        self.conflict_group = QWidget(self)
        conflict_layout = QVBoxLayout(self.conflict_group)
        conflict_layout.setContentsMargins(0, 0, 0, 0)
        conflict_layout.setSpacing(3)
        child_row = QHBoxLayout()
        child_row.setSpacing(4)
        self.conflict_combo = QComboBox(self.conflict_group)
        self.conflict_combo.setMinimumWidth(220)
        self.conflict_combo.setToolTip("本次过线相关的其他疑点：号码 · 类型 · 状态；先选一条，再查看或按依据处理")
        self.conflict_combo.activated[int].connect(self._conflict_activated)
        child_row.addWidget(self.conflict_combo, 1)
        self.conflict_view_button = QPushButton("查看疑点", self.conflict_group)
        self.conflict_view_button.setToolTip("跳到这条疑点的原帧，逐帧看它到底是什么")
        self.conflict_view_button.clicked.connect(self._view_conflict)
        child_row.addWidget(self.conflict_view_button)
        conflict_layout.addLayout(child_row)
        action_row = QHBoxLayout()
        action_row.setSpacing(4)
        self.conflict_label = QLabel("", self.conflict_group)
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet("color: #667085; font-size: 9pt;")
        action_row.addWidget(self.conflict_label, 1)
        self.conflict_action = QComboBox(self.conflict_group)
        self.conflict_action.setToolTip("这条疑点可以按原帧证据处理的原因；只提供能由证据直接确认的处理")
        action_row.addWidget(self.conflict_action)
        self.conflict_decide_button = QPushButton("确认所选处理", self.conflict_group)
        self.conflict_decide_button.setToolTip("按所选原因处理这条疑点；不改写 CycleRace 成绩，只留依据")
        self.conflict_decide_button.clicked.connect(self._decide_conflict)
        action_row.addWidget(self.conflict_decide_button)
        conflict_layout.addLayout(action_row)
        layout.addWidget(self.conflict_group)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.fully_viewed = QCheckBox("已看完整团", self)
        self.fully_viewed.setToolTip("看过完整团段且人数无遗漏或重复，才可确认；逐帧未看全就不能完成")
        self.fully_viewed.toggled.connect(self.fully_viewed_changed)
        bottom.addWidget(self.fully_viewed)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self._sync_summary()
        self._fill_marks()
        self._fill_members()
        self._fill_conflicts()
        self._show_mark(-1)
        self._sync_view()

    # ----- data in ----------------------------------------------------
    def set_review(self, marks, members=(), conflicts=(), *, can_complete=False, fully_viewed=False,
                   show_fully_viewed=True, notice=""):
        """Show what the caller has: the counted people, the chips and the doubts.

        ``marks`` are the people pointed at on the frame (``recorder_ms``,
        ``bib``, ``exclusion_reason`` and whatever the caller keeps for the
        frame); ``members`` are the chip records (``event_id``, ``bib``,
        ``chip_ms`` and an optional ``state``); ``conflicts`` are the child
        problem items of this crossing.  All of them are read, never written.
        The tick means the operator has seen the whole group, so it is shown
        only where the caller says there is a group; without ``can_complete`` it
        is disabled and cleared rather than left lying.
        """

        self._marks = tuple(marks or ())
        self._members = tuple(members or ())
        self._conflicts = tuple(conflicts or ())
        if not 0 <= self._selected_mark < len(self._marks):
            self._selected_mark = -1
        if self._selected_member and self._selected_member not in {
            str(member.get("event_id") or "") for member in self._members
        }:
            self._selected_member = ""
        if self._selected_conflict and self._selected_conflict not in {
            item.item_id for item in self._conflicts
        }:
            self._selected_conflict = ""

        self._fill_marks()
        self._fill_members()
        self._fill_conflicts()
        self._sync_summary()
        self._show_mark(self._selected_mark)
        show_complete = bool(show_fully_viewed)
        self.fully_viewed.blockSignals(True)
        self.fully_viewed.setVisible(show_complete)
        self.fully_viewed.setEnabled(show_complete and bool(can_complete))
        self.fully_viewed.setChecked(show_complete and bool(can_complete) and bool(fully_viewed))
        self.fully_viewed.blockSignals(False)
        self._set_notice(str(notice or ""))
        self._sync_view()

    def select_mark(self, index: int) -> None:
        """Point the controls at one counted person; quiet, like any refresh."""

        index = int(index)
        if not 0 <= index < len(self._marks):
            index = -1
        self.mark_combo.blockSignals(True)
        self.mark_combo.setCurrentIndex(index)
        self.mark_combo.blockSignals(False)
        self._show_mark(index, reload=True)

    def select_conflict(self, item_id: str) -> None:
        """Point the child selector at one doubt; quiet, the parent asked for it."""

        wanted = str(item_id or "")
        for index, item in enumerate(self._conflicts):
            if item.item_id == wanted:
                self.conflict_combo.blockSignals(True)
                self.conflict_combo.setCurrentIndex(index + 1)
                self.conflict_combo.blockSignals(False)
                self._selected_conflict = wanted
                self._sync_conflict_actions()
                return
        self.conflict_combo.blockSignals(True)
        self.conflict_combo.setCurrentIndex(0)
        self.conflict_combo.blockSignals(False)
        self._selected_conflict = ""
        self._sync_conflict_actions()

    # ----- the fields -------------------------------------------------
    def _fill_marks(self) -> None:
        self.mark_combo.blockSignals(True)
        self.mark_combo.clear()
        for index, mark in enumerate(self._marks):
            self.mark_combo.addItem(self._mark_label(index, mark))
        self.mark_combo.setCurrentIndex(self._selected_mark)
        self.mark_combo.blockSignals(False)

    def _mark_label(self, index: int, mark: Mapping[str, Any]) -> str:
        time_text = format_chip_time(mark.get("recorder_ms"))
        if _excluded(mark):
            return f"{index + 1} · {time_text} · 已排除：{_exclusion_reason(mark)}"
        bib = str(mark.get("bib") or "").strip()
        return f"{index + 1} · {time_text} · {bib or '未认号'}"

    def _fill_members(self) -> None:
        self.member_combo.blockSignals(True)
        self.member_combo.clear()
        self.member_combo.addItem(MEMBER_PLACEHOLDER)
        chosen = None
        for index, member in enumerate(self._members):
            self.member_combo.addItem(self._member_label(member))
            event_id = str(member.get("event_id") or "")
            self.member_combo.setItemData(index + 1, event_id, Qt.UserRole)
            if event_id and event_id == self._selected_member:
                chosen = index + 1
            if member_void(member):
                # Shown for the record, but not selectable and not decidable.
                model = self.member_combo.model()
                slot = model.item(index + 1) if hasattr(model, "item") else None
                if slot is not None:
                    slot.setEnabled(False)
        # No record is chosen for the operator: the placeholder stays until they
        # picked one themselves, so nothing is decided by the default.
        self.member_combo.setCurrentIndex(chosen if chosen is not None else 0)
        self.member_combo.blockSignals(False)

    def _member_label(self, member: Mapping[str, Any]) -> str:
        bib = str(member.get("bib") or "").strip() or "未认号"
        state = str(member.get("state") or "").strip()
        label = f"{bib} · {format_chip_time(member.get('chip_ms'))}"
        return f"{label} · {state}" if state else label

    def _fill_conflicts(self) -> None:
        self.conflict_combo.blockSignals(True)
        self.conflict_combo.clear()
        self.conflict_combo.addItem(CONFLICT_PLACEHOLDER)
        chosen = 0
        for index, item in enumerate(self._conflicts):
            self.conflict_combo.addItem(_item_summary(item))
            self.conflict_combo.setItemData(index + 1, item.item_id, Qt.UserRole)
            self.conflict_combo.setItemData(index + 1, self._conflict_tooltip(item), Qt.ToolTipRole)
            if item.item_id == self._selected_conflict:
                chosen = index + 1
        # Nothing is chosen for the operator: the placeholder stays until they
        # picked one themselves, so no doubt is decided by the default.
        self.conflict_combo.setCurrentIndex(chosen)
        self.conflict_combo.blockSignals(False)
        self._sync_conflict_actions()

    def _conflict_tooltip(self, item: ProblemItem) -> str:
        lines = [item.title, item.detail]
        if item.evidence.get("frame"):
            lines.append("按「查看疑点」打开它的原帧，看过再按依据处理")
        return "\n".join(line for line in lines if line)

    def _sync_conflict_actions(self) -> None:
        """Offer the child's own actions, minus the ones only its record may take."""

        item = self._chosen_conflict()
        self.conflict_action.blockSignals(True)
        self.conflict_action.clear()
        for action in item.actions if item is not None else ():
            if action not in CONFLICT_ACTIONS:
                continue
            self.conflict_action.addItem(ACTION_LABELS.get(action, action))
            self.conflict_action.setItemData(self.conflict_action.count() - 1, action, Qt.UserRole)
        self.conflict_action.blockSignals(False)
        if item is None:
            self.conflict_label.setText("选择一条疑点后可按依据处理")
            self.conflict_label.setToolTip("")
        else:
            self.conflict_label.setText(f"{item.title}：{item.detail}")
            self.conflict_label.setToolTip(self._conflict_tooltip(item))
        self._sync_buttons()

    def _sync_summary(self) -> None:
        active = sum(not _excluded(mark) for mark in self._marks)
        excluded = len(self._marks) - active
        records = sum(not member_void(member) for member in self._members)
        text = f"已确认 {active} 人 · {records} 条有效记录"
        if excluded:
            text += f" · 已排除 {excluded} 人"
        self.summary.setText(text)

    def _sync_view(self) -> None:
        """Show only the rows this crossing actually needs."""

        self.mark_group.setVisible(bool(self._marks))
        self.member_group.setVisible(bool(self._members))
        self.conflict_group.setVisible(bool(self._conflicts))
        self.notice.setVisible(bool(self.notice.text()))

    def _show_mark(self, index: int, *, reload: bool = False) -> None:
        """Follow the selection in the fields; never worth a signal on its own."""

        valid = 0 <= index < len(self._marks)
        mark = self._marks[index] if valid else None
        key = _mark_key(mark) if mark is not None else None
        if valid and (reload or key != self._bib_loaded_key or index != self._bib_loaded_for):
            # The person under the selection changed; the number in the field
            # follows, and a number being typed is left alone otherwise.
            self.bib_input.setText(str(mark.get("bib") or ""))
            self._bib_loaded_for = index
            self._bib_loaded_key = key
            # The reason combo shows what this person already carries, so an
            # exclusion is read before it is written again.
            recorded = self.exclusion_combo.findText(_exclusion_reason(mark))
            self.exclusion_combo.setCurrentIndex(recorded if recorded >= 0 else 0)
        elif not valid and self._bib_loaded_for != -1:
            self.bib_input.clear()
            self._bib_loaded_for = -1
            self._bib_loaded_key = None
        self._selected_mark = index if valid else -1
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        valid = 0 <= self._selected_mark < len(self._marks)
        excluded = valid and _excluded(self._marks[self._selected_mark])
        for widget in (self.bib_input, self.assign_button, self.exclusion_combo,
                       self.exclude_button, self.remove_button):
            widget.setEnabled(valid and not excluded)
        self.restore_button.setEnabled(bool(excluded))
        member = self._chosen_member()
        decidable = member is not None and not member_void(member)
        self.keep_button.setEnabled(decidable)
        self.delete_button.setEnabled(decidable)
        conflict = self._chosen_conflict()
        self.conflict_view_button.setEnabled(conflict is not None)
        self.conflict_decide_button.setEnabled(conflict is not None and self.conflict_action.count() > 0)

    def _set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setToolTip(text)
        self.notice.setVisible(bool(text))

    def _chosen_mark(self):
        if not 0 <= self._selected_mark < len(self._marks):
            return None, None
        return self._selected_mark, self._marks[self._selected_mark]

    def _chosen_member(self):
        index = self.member_combo.currentIndex()
        if index <= 0 or index > len(self._members):
            return None
        return self._members[index - 1]

    def _chosen_conflict(self) -> Optional[ProblemItem]:
        index = self.conflict_combo.currentIndex()
        if index <= 0 or index > len(self._conflicts):
            return None
        return self._conflicts[index - 1]

    # ----- what the operator does -------------------------------------
    def _mark_activated(self, index: int) -> None:
        index = int(index)
        self._show_mark(index if 0 <= index < len(self._marks) else -1, reload=True)
        if self._selected_mark >= 0:
            self.mark_selected.emit(self._selected_mark)

    def _member_activated(self, index: int) -> None:
        member = self._members[index - 1] if 1 <= index <= len(self._members) else None
        self._selected_member = str(member.get("event_id") or "") if member is not None else ""
        self._sync_buttons()

    def _conflict_activated(self, index: int) -> None:
        item = self._conflicts[index - 1] if 1 <= index <= len(self._conflicts) else None
        self._selected_conflict = item.item_id if item is not None else ""
        self._sync_conflict_actions()
        if item is not None:
            self.conflict_selected.emit(item.item_id)

    def _assign_bib(self) -> None:
        index, mark = self._chosen_mark()
        if mark is None:
            self._set_notice("请先在下拉里选择一位已点的人。")
            return
        if _excluded(mark):
            self._set_notice("已排除的人不填号码；先「恢复为选手」再认号。")
            return
        bib = self.bib_input.text().strip()
        if bib and any(
            other_index != index and not _excluded(other)
            and str(other.get("bib") or "").strip() == bib
            for other_index, other in enumerate(self._marks)
        ):
            self._set_notice("这个号码已经填在另一位过线者上，请先核对。")
            return
        self._set_notice("")
        self.bib_assigned.emit(index, bib)

    def _exclude(self) -> None:
        index, mark = self._chosen_mark()
        if mark is None:
            self._set_notice("请先选择一位已点的人，再排除。")
            return
        if _excluded(mark):
            self._set_notice("这位已经是已排除人员；如需纠正请按「恢复为选手」。")
            return
        reason = self.exclusion_combo.currentText().strip()
        if not reason:
            self._set_notice("排除要写明依据：先在旁边选择原因。")
            return
        self._set_notice("")
        self.exclusion_changed.emit(index, reason)

    def _restore(self) -> None:
        index, mark = self._chosen_mark()
        if mark is None or not _excluded(mark):
            self._set_notice("选中的这位不是已排除人员。")
            return
        self._set_notice("")
        self.exclusion_changed.emit(index, "")

    def _remove(self) -> None:
        index, mark = self._chosen_mark()
        if mark is None:
            self._set_notice("请先选择一位已点的人。")
            return
        if _excluded(mark):
            self._set_notice("已排除人员保留原帧和原因；先恢复为选手，再删除误点。")
            return
        self._set_notice("")
        self.mark_removed.emit(index)

    def _decide_member(self, action: str) -> None:
        member = self._chosen_member()
        if member is None:
            self._set_notice("先在下拉里选一条芯片记录，再决定保留或删除；不替操作员默认第一条。")
            return
        event_id = str(member.get("event_id") or "")
        if not event_id:
            self._set_notice("这条记录没有编号，不能在这里决定。")
            return
        if member_void(member):
            self._set_notice("这条记录已在上游作废，不再在这里决定。")
            return
        self._set_notice("")
        self.member_decision.emit(event_id, action)

    def _view_conflict(self) -> None:
        item = self._chosen_conflict()
        if item is None:
            self._set_notice("先在下拉里选择一条疑点，再查看。")
            return
        self._set_notice("")
        self.conflict_selected.emit(item.item_id)

    def _decide_conflict(self) -> None:
        item = self._chosen_conflict()
        if item is None:
            self._set_notice("先在下拉里选择一条疑点，再按依据处理。")
            return
        action = str(self.conflict_action.currentData(Qt.UserRole) or "")
        if not action:
            self._set_notice("这条疑点没有能由原帧证据直接确认的处理，请到它自己的入口处理。")
            return
        self._set_notice("")
        self.conflict_decided.emit(item.item_id, action)


class CrossingCardRail(QFrame):
    """One card per person or group, in time order, under the evidence picture.

    A card is never decoded here: it is the JPEG the analysis already wrote
    (``evidence.card``, resolved against the problem directory) or a frame the
    workbench already decoded and handed over with ``set_preview`` — and an item
    with neither still reads as a card through its time, class and numbers.
    Cards of the members inside a batch or a group stay out of the rail — their
    parent is the one task — while two different riders who happen to share one
    frame keep a card each.  The title shares its line with ``toolbar``, where
    the workbench adds its own controls without a second row.
    """

    item_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("crossingCardRail")
        self._items: tuple = ()
        self._signature: Optional[tuple] = None
        self._icons: dict = {}
        self._previews: dict = {}
        self._problem_dir: Optional[Path] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.toolbar = QHBoxLayout()
        self.toolbar.setContentsMargins(0, 0, 0, 0)
        self.toolbar.setSpacing(4)
        self.title = QLabel("过线卡片", self)
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setToolTip("一人或一团一张卡片；点击打开对应的原帧")
        self.toolbar.addWidget(self.title)
        # The workbench's own controls land before this stretch and stay on the
        # title's line, as the camera track's header does.
        self.toolbar.addStretch(1)
        layout.addLayout(self.toolbar)
        self.list = QListWidget(self)
        self.list.setObjectName("crossingCardList")
        self.list.setViewMode(QListView.IconMode)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Static)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setUniformItemSizes(True)
        self.list.setIconSize(RAIL_ICON_SIZE)
        self.list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setStyleSheet(
            "QListWidget { background: #f4f8fb; border: 1px solid #cfd7df; }"
            "QListWidget::item { padding: 3px; }"
            "QListWidget::item:selected { background: #1976c9; color: white; }"
        )
        # One row of cards, never squeezed to nothing and never given the room
        # of the picture above it.
        self.list.setMinimumHeight(RAIL_LIST_HEIGHT)
        self.list.setMaximumHeight(RAIL_LIST_HEIGHT)
        self.list.itemClicked.connect(self._card_chosen)
        self.list.itemActivated.connect(self._card_chosen)
        layout.addWidget(self.list)

    def set_items(self, items: Sequence[ProblemItem], problem_dir=None, selected_id: str = "") -> None:
        """Show the cards of ``items``; identical content keeps scroll and selection.

        ``problem_dir`` is where ``evidence.card`` is relative to (the
        ``finish_problems`` directory); without it the cards are text only.  An
        empty ``selected_id`` keeps whatever card the operator has already
        opened, so a refresh cannot take the selection away.
        """

        directory = Path(problem_dir) if problem_dir else None
        self._problem_dir = directory
        visible = self._visible(items)
        signature = tuple(self._entry_key(item, directory) for item in visible)
        desired = str(selected_id or self._current_id())
        scrollbar = self.list.horizontalScrollBar()
        scroll = scrollbar.value()
        if signature != self._signature:
            same_order = tuple(item.item_id for item in visible) == tuple(
                item.item_id for item in self._items
            )
            self._items = visible
            if not same_order:
                self.list.clear()
            for index, item in enumerate(visible):
                entry = self.list.item(index) if same_order else QListWidgetItem()
                entry.setText(self._card_text(item))
                entry.setData(Qt.UserRole, item.item_id)
                entry.setIcon(self._icon(item, directory))
                entry.setSizeHint(RAIL_ENTRY_SIZE)
                entry.setToolTip(self._card_tooltip(item))
                if not same_order:
                    self.list.addItem(entry)
            # Metadata-only edits keep the running scroll; a changed card count
            # is laid out again from the same place.
            self.list.doItemsLayout()
            self._signature = signature
        self._apply_selection(desired)
        scrollbar.setValue(scroll)
        self.title.setToolTip(f"共 {len(visible)} 张卡片；点击打开对应的原帧")

    def set_selected(self, item_id: str) -> None:
        """Show one card as the opened one; quiet, the click already happened."""

        self._apply_selection(str(item_id or ""))

    def set_preview(self, item_id: str, pixmap: QPixmap) -> None:
        """Hand one card a frame the workbench has already decoded for it.

        A card of a normally auto-passed rider has no JPEG on the backend at
        all, so this is what it is drawn from until one exists.  The preview is
        kept in memory (the newest 64) and the matching entry, if it is on
        screen, takes it in place — no rebuild, no signal, and no decoding or
        reading of anything here.  A null pixmap only drops the preview, and the
        card falls back to its JPEG (if any) or to its text.
        """

        key = str(item_id or "")
        if not key:
            return
        self._previews.pop(key, None)
        if pixmap is not None and not pixmap.isNull():
            self._previews[key] = pixmap.scaled(RAIL_ICON_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        while len(self._previews) > PREVIEW_CACHE_LIMIT:
            # Oldest first; a card that scrolls back into use asks again.
            self._previews.pop(next(iter(self._previews)))
        for item in self._items:
            if item.item_id == key:
                self._show_icon(item)
                break

    def _show_icon(self, item: ProblemItem) -> None:
        """Redraw one card's icon where it already is, without touching the rest."""

        for index in range(self.list.count()):
            entry = self.list.item(index)
            if str(entry.data(Qt.UserRole)) == item.item_id:
                entry.setIcon(self._icon(item, self._problem_dir))
                return

    # ----- content ----------------------------------------------------
    def _visible(self, items: Iterable[ProblemItem]) -> tuple:
        """One card per task: members stay with their parent, none without a frame."""

        ordered, seen = [], set()
        for item in items or ():
            if item.grouped or not item.evidence.get("frame"):
                continue
            if item.item_id in seen:
                # The same item delivered twice is one card; two riders sharing
                # one frame are still two, because their ids differ.
                continue
            seen.add(item.item_id)
            ordered.append(item)
        ordered.sort(key=_rail_order)
        return tuple(ordered)

    def _entry_key(self, item: ProblemItem, directory: Optional[Path]) -> tuple:
        """What a card is made of: its evidence, its icon file stamp, its state."""

        card = str(item.evidence.get("card") or "")
        stamp = _file_stamp(directory / card) if directory is not None and card else 0
        return (
            item.item_id,
            card,
            _frame_key(item.evidence.get("frame")),
            stamp,
            item.status,
            item.status_note,
            int(item.calibration_version),
            bool(item.evidence.get("auto_passed")),
            _count(item.evidence.get("video_count")),
            _count(item.evidence.get("chip_count")),
            self._card_text(item),
        )

    def _card_state(self, item: ProblemItem) -> tuple:
        """The class and the state a card reads as, auto-passed riders included."""

        if item.evidence.get("auto_passed"):
            return AUTO_PASSED_TITLE, AUTO_PASSED_STATE
        return (CLASS_TITLES.get(problem_class(item), CLASS_TITLES[TYPE_CONFIRM]),
                STATUS_LABELS.get(item.status, item.status))

    def _card_text(self, item: ProblemItem) -> str:
        when = _rail_time_ms(item)
        time_text = format_chip_time(when) if when is not None else "--"
        title, state = self._card_state(item)
        bibs = "、".join(item.bibs[:4]) + ("…" if len(item.bibs) > 4 else "")
        detail = " · ".join(part for part in (bibs, state) if part)
        return f"{time_text} · {title}\n{detail}"

    def _card_tooltip(self, item: ProblemItem) -> str:
        when = _rail_time_ms(item)
        time_text = format_chip_time(when) if when is not None else "--"
        title, state = self._card_state(item)
        lines = [item.title, item.detail, f"{time_text} · {title} · {state}"]
        if item.chip_time_ms is not None:
            # The official time of the record this card is about, named apart
            # from when the picture was taken.
            lines.append(f"官方芯片时间 {format_chip_time(item.chip_time_ms)}（仍以芯片时间为准）")
        lines.append("点击打开对应的原帧")
        return "\n".join(line for line in lines if line)

    def _icon(self, item: ProblemItem, directory: Optional[Path]) -> QIcon:
        card = str(item.evidence.get("card") or "")
        if directory is not None and card:
            path = directory / card
            stamp = _file_stamp(path)
            if stamp:
                key = (str(path), stamp)
                pixmap = self._icons.get(key)
                if pixmap is None:
                    pixmap = QPixmap(str(path))
                    if pixmap.isNull():
                        return self._preview_icon(item)
                    pixmap = pixmap.scaled(RAIL_ICON_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    while len(self._icons) >= ICON_CACHE_LIMIT:
                        self._icons.pop(next(iter(self._icons)))
                    self._icons[key] = pixmap
                return QIcon(pixmap)
        return self._preview_icon(item)

    def _preview_icon(self, item: ProblemItem) -> QIcon:
        preview = self._previews.get(item.item_id)
        return QIcon(preview) if preview is not None else QIcon()

    # ----- selection --------------------------------------------------
    def _current_id(self) -> str:
        selected = self.list.selectedItems()
        return str(selected[0].data(Qt.UserRole)) if selected else ""

    def _apply_selection(self, item_id: str) -> None:
        self.list.clearSelection()
        if not item_id:
            return
        for index in range(self.list.count()):
            entry = self.list.item(index)
            if str(entry.data(Qt.UserRole)) == item_id:
                entry.setSelected(True)
                return

    def _card_chosen(self, item: QListWidgetItem) -> None:
        self.item_selected.emit(str(item.data(Qt.UserRole)))


__all__ = [
    "AUTO_PASSED_STATE",
    "AUTO_PASSED_TITLE",
    "CONFLICT_ACTIONS",
    "CONFLICT_PLACEHOLDER",
    "CrossingCardRail",
    "CrossingReviewControls",
    "EXCLUSION_REASONS",
    "MEMBER_PLACEHOLDER",
    "PREVIEW_CACHE_LIMIT",
    "RAIL_ENTRY_SIZE",
    "RAIL_ICON_SIZE",
    "RAIL_LIST_HEIGHT",
    "STATUS_LABELS",
    "member_void",
]
