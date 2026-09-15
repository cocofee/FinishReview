import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtWidgets import QApplication

from realtime.camera_judgments import CameraJudgment, CameraJudgmentTrack


def test_rejudgment_updates_item_without_resetting_scrolled_track():
    app = QApplication.instance() or QApplication([])
    track = CameraJudgmentTrack()
    track.resize(480, 104)
    records = tuple(
        CameraJudgment(
            key=f"event:{index}", event_id=str(index), segment_id="camera-one",
            position_ms=index * 1_000, recorder_time_ms=index * 1_000,
            label=str(index), time_label=f"08:00:{index:02d}.000",
        )
        for index in range(30)
    )
    track.set_records(records, "20")
    track.show()
    app.processEvents()
    track.list.setCurrentRow(20)
    scrollbar = track.list.horizontalScrollBar()
    scrollbar.setValue(2_850)
    app.processEvents()
    scroll = scrollbar.value()
    visible = track.list.itemAt(QPoint(5, 5)).data(Qt.UserRole)
    updated = tuple(
        replace(record, time_label="08:00:20.100", position_ms=20_100,
                recorder_time_ms=20_100) if record.event_id == "20" else record
        for record in records
    )

    track.set_records(updated, "20")
    app.processEvents()

    assert scrollbar.value() == scroll
    assert track.list.itemAt(QPoint(5, 5)).data(Qt.UserRole) == visible
    assert track.list.currentRow() == 20
    assert track.list.currentItem().isSelected()
    assert "08:00:20.100" in track.list.currentItem().text()
    track.close()
