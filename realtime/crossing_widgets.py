"""The compact controls and the crossing-card rail of the finish workbench.

Both widgets are presentation only: the caller owns the marks, the chips and
the stores.  ``CrossingReviewControls`` sits under the evidence picture and says
what the operator did — counted people, recognised numbers, exclusions; the
count and the chip records are read side by side so the comparison is one
glance.  ``CrossingCardRail`` is the row of one-per-person (or one-per-group)
cards that opens the original frame again, with the workbench's own controls on
the same line as its title.

One rule from the field workflow is built in here, because this is the last
cheap place to keep it: a count and a record are different numbers — the summary
reads ``已确认 N 人 · M 条有效记录`` and never adds one to the other.

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


# 排除是二选一的一按：原帧说明一切，依据随后写进记录，不再让人先挑一句话。
EXCLUSION_STANDING_REASON = "原帧不是选手过线（行人 / 车辆 / 工作人员）"

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
    """画面下那一行：点出来的人，和芯片条数的对照。

    数到多少人、芯片有几条，就摆在同一条线上；人数相等只是对照，看完整团后由操作员确认。
    ``set_review`` 是数据唯一的入口，而且不发声：只重画字段，绝不因为刷新而决定
    什么，所以两次按键之间落进来的一次刷新不会替操作员做主。
    """

    # A person on the frame was picked in the list (their index into the marks).
    mark_selected = pyqtSignal(int)
    # A number was read off the frame for that person; an empty string clears it.
    bib_assigned = pyqtSignal(int, str)
    # That person is not a rider: the reason is explicit, and "" restores them.
    exclusion_changed = pyqtSignal(int, str)
    # A mis-clicked person; the excluded ones are restored before they are removed.
    mark_removed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("crossingReviewControls")
        # The caller's data, read only; the widget holds no draft of its own.
        self._marks: tuple = ()
        self._records = 0
        self._selected_mark = -1
        self._bib_loaded_for = -1
        self._bib_loaded_key: Optional[tuple] = None

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
        self.exclude_button = QPushButton("不是选手", self.mark_group)
        self.exclude_button.setToolTip("把选中的这位记为非选手：不计入人数，不算补录；原帧和依据一起留档")
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

        self._sync_summary()
        self._fill_marks()
        self._show_mark(-1)
        self._sync_view()

    # ----- data in ----------------------------------------------------
    def set_review(self, marks, records=0, *, notice=""):
        """Show what the caller has: the counted people and how many chips recorded.

        ``marks`` are the people pointed at on the frame (``recorder_ms``,
        ``bib``, ``exclusion_reason`` and whatever the caller keeps for the
        frame); ``records`` is how many chip records this crossing has, so the
        line under the picture holds both halves of the comparison.  Both are
        read, never written.
        """

        self._marks = tuple(marks or ())
        self._records = int(records or 0)
        if not 0 <= self._selected_mark < len(self._marks):
            self._selected_mark = -1

        self._fill_marks()
        self._sync_summary()
        self._show_mark(self._selected_mark)
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

    def _sync_summary(self) -> None:
        active = sum(not _excluded(mark) for mark in self._marks)
        excluded = len(self._marks) - active
        text = f"已确认 {active} 人 · {self._records} 条有效记录"
        if excluded:
            text += f" · 已排除 {excluded} 人"
        self.summary.setText(text)

    def _sync_view(self) -> None:
        """Show only the rows this crossing actually needs."""

        self.mark_group.setVisible(bool(self._marks))
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
        elif not valid and self._bib_loaded_for != -1:
            self.bib_input.clear()
            self._bib_loaded_for = -1
            self._bib_loaded_key = None
        self._selected_mark = index if valid else -1
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        valid = 0 <= self._selected_mark < len(self._marks)
        excluded = valid and _excluded(self._marks[self._selected_mark])
        for widget in (self.bib_input, self.assign_button,
                       self.exclude_button, self.remove_button):
            widget.setEnabled(valid and not excluded)
        # 两种纠正只在各自成立时出现：是选手的人不需要「恢复」，被排除的人先恢复
        # 再谈删除。
        self.restore_button.setVisible(bool(excluded))
        self.remove_button.setVisible(valid and not excluded)

    def _set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setToolTip(text)
        self.notice.setVisible(bool(text))

    def _chosen_mark(self):
        if not 0 <= self._selected_mark < len(self._marks):
            return None, None
        return self._selected_mark, self._marks[self._selected_mark]

    # ----- what the operator does -------------------------------------
    def _mark_activated(self, index: int) -> None:
        index = int(index)
        self._show_mark(index if 0 <= index < len(self._marks) else -1, reload=True)
        if self._selected_mark >= 0:
            self.mark_selected.emit(self._selected_mark)

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
        self._set_notice("")
        self.exclusion_changed.emit(index, EXCLUSION_STANDING_REASON)

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
    "CrossingCardRail",
    "CrossingReviewControls",
    "EXCLUSION_STANDING_REASON",
    "PREVIEW_CACHE_LIMIT",
    "RAIL_ENTRY_SIZE",
    "RAIL_ICON_SIZE",
    "RAIL_LIST_HEIGHT",
    "STATUS_LABELS",
]
