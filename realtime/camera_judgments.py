"""Persistent, chronological navigation for a camera's manual judgments."""

from dataclasses import dataclass

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QLabel, QListView, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget,
)


@dataclass(frozen=True)
class CameraJudgment:
    key: str
    event_id: str
    segment_id: str
    position_ms: int
    recorder_time_ms: int | None
    label: str
    time_label: str
    clock_offset_ms: int = 0
    unknown: bool = False


class CameraJudgmentTrack(QWidget):
    judgment_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cameraJudgmentTrack")
        self.setFixedHeight(88)
        self._records: tuple[CameraJudgment, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.summary = QLabel("判读记录 · 尚无标记", self)
        layout.addWidget(self.summary)
        self.list = QListWidget(self)
        self.list.setObjectName("cameraJudgmentList")
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Static)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setStyleSheet(
            "QListWidget { background: #f4f8fb; border: 1px solid #cfd7df; }"
            "QListWidget::item { padding: 3px; }"
            "QListWidget::item:selected { background: #1976c9; color: white; }"
        )
        self.list.itemClicked.connect(self._request_item)
        self.list.itemActivated.connect(self._request_item)
        layout.addWidget(self.list)

    def _request_item(self, item: QListWidgetItem) -> None:
        self.judgment_requested.emit(str(item.data(Qt.UserRole)))

    def set_records(
        self, records: tuple[CameraJudgment, ...], selected_event_id: str
    ) -> None:
        # Identity changes only highlight an item. Preserve scroll and item
        # geometry, including two riders confirmed a fraction of a second apart.
        scrollbar = self.list.horizontalScrollBar()
        scroll = scrollbar.value()
        if records != self._records:
            same_order = tuple(record.key for record in records) == tuple(
                record.key for record in self._records
            )
            self._records = records
            if not same_order:
                self.list.clear()
            for index, record in enumerate(records):
                status = "待补录" if record.unknown else "已确认"
                item = self.list.item(index) if same_order else QListWidgetItem()
                item.setText(f"{record.label} · {status}\n{record.time_label}")
                item.setData(Qt.UserRole, record.key)
                item.setSizeHint(QSize(150, 52))
                item.setToolTip(f"{record.label} · {status} · {record.time_label}\n点击回看保存的判读位置")
                if not same_order:
                    self.list.addItem(item)
            # Restore the viewport after Qt recalculates a changed item count.
            # Metadata-only edits retain the existing items and keyboard focus.
            self.list.doItemsLayout()
        self.list.clearSelection()
        for index, record in enumerate(records):
            self.list.item(index).setSelected(
                bool(selected_event_id)
                and not record.unknown
                and record.event_id == selected_event_id
            )
        scrollbar.setValue(scroll)
        confirmed = sum(not record.unknown for record in records)
        unknown = len(records) - confirmed
        self.summary.setText(
            f"判读记录 · 已确认 {confirmed} · 待补录 {unknown} · 点击回看"
            if records else "判读记录 · 尚无标记"
        )
