import os
import struct
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

import realtime.auyat_rgb as auyat_rgb
from realtime.auyat_rgb import (
    AUYAT_CLOCK_SOURCE,
    CAPTURE_END_FLAG,
    CAPTURE_START_FLAG,
    HEADER_SIZE,
    RECORD_SIZE,
    TICKS_PER_DAY,
    AuyatRgbCatalog,
    AuyatRgbPlaybackWorker,
    AuyatRgbScanWorker,
    AuyatScanCancelled,
    is_network_share,
    read_capture,
    read_channel_order,
    scan_rgb_file,
)
from realtime.passage_evidence import HIGH_SPEED_SOURCE
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.passage_review import PassageReviewDialog
from realtime.video_timeline import VideoTimelineStore


BEIJING_TIMEZONE = timezone(timedelta(hours=8))


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _write_rgb(
    path: Path,
    *,
    capture_date: date,
    ticks: tuple[int, ...],
    channel_order: str = "rgb",
    closed: bool = True,
) -> None:
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<H", header, 24, 1024)
    struct.pack_into("<H", header, 36, capture_date.year)
    header[38] = capture_date.month
    header[39] = capture_date.day
    records = []
    for index, tick in enumerate(ticks):
        record = bytearray(RECORD_SIZE)
        flag = 0
        if index == 0:
            flag |= CAPTURE_START_FLAG
        if closed and index == len(ticks) - 1:
            flag |= CAPTURE_END_FLAG
        struct.pack_into("<II", record, 0, tick, flag)
        rgb = bytes((10 + index, 20 + index, 30 + index))
        if channel_order == "bgr":
            rgb = rgb[::-1]
        record[8:] = rgb * 1024
        records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + b"".join(records))


def _append_rgb_records(
    path: Path,
    records: tuple[tuple[int, int], ...],
) -> None:
    payload = []
    for index, (tick, flag) in enumerate(records):
        record = bytearray(RECORD_SIZE)
        struct.pack_into("<II", record, 0, tick, flag)
        record[8:] = bytes((40 + index, 50 + index, 60 + index)) * 1024
        payload.append(record)
    with path.open("ab") as output:
        output.write(b"".join(payload))


def _wait_until(qapp, predicate, timeout_seconds=2.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_catalog_uses_real_header_layout_and_isolates_bad_history(tmp_path):
    root = tmp_path / "vendor"
    photo = root / "Photo"
    photo.mkdir(parents=True)
    (photo / "00_bad.RGB").write_bytes(bytes(HEADER_SIZE + RECORD_SIZE))
    ticks = (
        TICKS_PER_DAY + 9 * 60 * 60 * 20_000,
        TICKS_PER_DAY + 9 * 60 * 60 * 20_000 + 19,
        TICKS_PER_DAY + 9 * 60 * 60 * 20_000 + 43,
    )
    rgb_path = photo / "01_group.RGB"
    _write_rgb(rgb_path, capture_date=date(2026, 8, 22), ticks=ticks)
    _write_rgb(
        photo / "01_group_2026-08-22 120000.RGB",
        capture_date=date(2026, 8, 22),
        ticks=ticks,
    )

    catalog = AuyatRgbCatalog(root)
    waiting = catalog.scan()
    result = catalog.scan()

    assert waiting.status == "ready"
    assert waiting.waiting_file_count == 0
    assert len(waiting.captures) == 1
    assert "已跳过 1 个不可读文件" in waiting.message
    assert result.status == "ready"
    assert len(result.captures) == 1
    assert "已跳过 1 个不可读文件" in result.message
    capture = result.captures[0]
    assert capture.file_path.name == "01_group_2026-08-22 120000.RGB"
    expected = datetime(2026, 8, 22, 9, 0, tzinfo=BEIJING_TIMEZONE)
    assert capture.media_started_at_ms == int(expected.timestamp() * 1000)
    assert capture.media_duration_ms == 2
    assert capture.column_count == 3


def test_network_share_detection_distinguishes_formal_remote_and_local_test_paths(
    tmp_path,
):
    assert is_network_share(r"\\FINISH-RGB\AuyatData")
    assert is_network_share("//FINISH-RGB/AuyatData")
    assert not is_network_share(tmp_path / "AuyatData")


def test_catalog_reports_pending_file_even_when_older_capture_is_ready(tmp_path):
    root = tmp_path / "vendor"
    old_path = root / "Photo" / "01_old.RGB"
    _write_rgb(
        old_path,
        capture_date=date(2026, 8, 22),
        ticks=(100, 120),
    )
    old_time = time.time() - 10
    os.utime(old_path, (old_time, old_time))
    catalog = AuyatRgbCatalog(root)
    assert catalog.scan().status == "ready"

    _write_rgb(
        root / "Photo" / "02_current.RGB",
        capture_date=date(2026, 8, 22),
        ticks=(200, 220),
        closed=False,
    )
    result = catalog.scan()

    assert result.status == "ready"
    assert len(result.captures) == 1
    assert result.waiting_file_count == 1
    assert "等待 1 个高速文件封口" in result.message


def test_catalog_publishes_completed_segments_while_vendor_file_keeps_growing(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "vendor"
    rgb_path = root / "Photo" / "01_group.RGB"
    _write_rgb(
        rgb_path,
        capture_date=date(2026, 8, 22),
        ticks=(100, 120),
    )
    real_scan = auyat_rgb._scan_rgb_file
    start_records = []

    def counted_scan(path, **kwargs):
        start_records.append(int(kwargs.get("start_record", 0)))
        return real_scan(path, **kwargs)

    monkeypatch.setattr(auyat_rgb, "_scan_rgb_file", counted_scan)
    catalog = AuyatRgbCatalog(root)

    first = catalog.scan()

    assert first.status == "ready"
    assert len(first.captures) == 1
    assert first.waiting_file_count == 0

    _append_rgb_records(
        rgb_path,
        ((200, CAPTURE_START_FLAG),),
    )
    growing = catalog.scan()

    assert growing.status == "ready"
    assert len(growing.captures) == 1
    assert growing.waiting_file_count == 1

    _append_rgb_records(
        rgb_path,
        ((220, CAPTURE_END_FLAG),),
    )
    completed = catalog.scan()

    assert completed.status == "ready"
    assert len(completed.captures) == 2
    assert completed.waiting_file_count == 0
    assert completed.captures[1].start_tick == 200
    assert completed.captures[1].end_tick == 220
    assert start_records == [0, 2, 3]


def test_catalog_keeps_snapshot_history_after_vendor_rolls_active_file(tmp_path):
    root = tmp_path / "vendor"
    photo = root / "Photo"
    active_path = photo / "01_group.RGB"
    snapshot_path = photo / "01_group_2026-08-22 120000.RGB"
    _write_rgb(
        active_path,
        capture_date=date(2026, 8, 22),
        ticks=(100, 120),
    )
    catalog = AuyatRgbCatalog(root)
    assert len(catalog.scan().captures) == 1

    active_path.replace(snapshot_path)
    _write_rgb(
        active_path,
        capture_date=date(2026, 8, 22),
        ticks=(200, 220),
    )

    rolled = catalog.scan()

    assert rolled.status == "ready"
    assert len(rolled.captures) == 2
    assert [capture.start_tick for capture in rolled.captures] == [100, 200]
    assert rolled.captures[0].file_path == snapshot_path.absolute()
    assert rolled.captures[1].file_path == active_path.absolute()


def test_catalog_filters_dates_and_reuses_persisted_local_index(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "vendor"
    cache_path = tmp_path / "index.json"
    old_path = root / "Photo" / "01_old.RGB"
    current_path = root / "Photo" / "02_current.RGB"
    _write_rgb(
        old_path,
        capture_date=date(2026, 8, 1),
        ticks=(100, 120),
    )
    _write_rgb(
        current_path,
        capture_date=date(2026, 8, 22),
        ticks=(200, 220),
    )
    stable_time = time.time() - 10
    os.utime(old_path, (stable_time, stable_time))
    os.utime(current_path, (stable_time, stable_time))
    real_scan = auyat_rgb._scan_rgb_file
    scanned_paths = []

    def counted_scan(path, **kwargs):
        scanned_paths.append(Path(path))
        return real_scan(path, **kwargs)

    monkeypatch.setattr(auyat_rgb, "_scan_rgb_file", counted_scan)
    first_catalog = AuyatRgbCatalog(
        root,
        cache_path=cache_path,
        target_dates=(date(2026, 8, 22),),
    )
    first = first_catalog.scan()

    assert first.status == "ready"
    assert scanned_paths == [current_path.absolute()]
    assert cache_path.is_file()

    scanned_paths.clear()
    second_catalog = AuyatRgbCatalog(
        root,
        cache_path=cache_path,
        target_dates=(date(2026, 8, 22),),
    )
    second = second_catalog.scan()

    assert second.status == "ready"
    assert len(second.captures) == 1
    assert scanned_paths == []


def test_scan_worker_stops_a_cooperative_directory_scan(qapp):
    class SlowCatalog:
        def scan(self, *, cancel_requested=None):
            while cancel_requested is None or not cancel_requested():
                time.sleep(0.01)
            raise AuyatScanCancelled()

    worker = AuyatRgbScanWorker(SlowCatalog(), interval_seconds=0.5)
    worker.start()
    assert _wait_until(qapp, worker.isRunning)

    worker.stop()

    assert worker.wait(1_000)


def test_capture_uses_each_column_tick_and_color_order(tmp_path):
    root = tmp_path / "vendor"
    rgb_path = root / "Photo" / "01_group.RGB"
    ticks = (100, 119, 143, 180)
    _write_rgb(
        rgb_path,
        capture_date=date(2026, 8, 22),
        ticks=ticks,
        channel_order="bgr",
    )
    capture = scan_rgb_file(rgb_path, channel_order="bgr")[0]

    frame = read_capture(capture)

    assert frame.position_ms_for_column(0) == 0
    assert frame.position_ms_for_column(1) == 1
    assert frame.position_ms_for_column(2) == 2
    assert frame.position_ms_for_column(3) == 4
    assert frame.column_for_position_ms(3) == 2
    assert frame.pixels_rgb[0, 0].tolist() == [10, 20, 30]
    assert frame.pixels_rgb[0, 3].tolist() == [13, 23, 33]


def test_channel_order_is_found_when_operator_selects_photo_directory(tmp_path):
    root = tmp_path / "vendor"
    photo = root / "Photo"
    photo.mkdir(parents=True)
    (root / "PhotoTime.ini").write_text(
        "ColorBitExchange=1\n",
        encoding="ascii",
    )

    assert read_channel_order(photo) == "bgr"


def test_playback_worker_loads_once_and_seeks_by_real_tick(qapp, tmp_path):
    rgb_path = tmp_path / "Photo" / "01_group.RGB"
    _write_rgb(
        rgb_path,
        capture_date=date(2026, 8, 22),
        ticks=(200, 220, 270, 330),
    )
    capture = scan_rgb_file(rgb_path)[0]
    location = capture.to_location(
        capture.media_started_at_ms + 3,
        race_id="race-1",
        pre_roll_ms=0,
    )
    metadata = []
    frames = []
    worker = AuyatRgbPlaybackWorker(location)
    worker.metadata_ready.connect(lambda *values: metadata.append(values))
    worker.frame_ready.connect(
        lambda image, position_ms, frame_index: frames.append(
            (image.width(), image.height(), position_ms, frame_index)
        )
    )

    worker.start()
    assert _wait_until(qapp, lambda: bool(metadata))
    assert metadata[0][0] == 7
    assert metadata[0][2:] == (4, 1024, 4)

    worker.seek(4)
    assert _wait_until(qapp, lambda: bool(frames))
    assert frames[-1] == (4, 1024, 4, 2)
    assert worker.position_ms_for_x(1.0) == 7
    assert worker.x_for_position_ms(4) == pytest.approx(2 / 3)

    worker.stop()
    assert worker.wait(2_000)
    assert location.segment.clock_source == AUYAT_CLOCK_SOURCE


def test_review_reuses_one_rgb_capture_and_marks_only_selected_identity(
    qapp,
    tmp_path,
):
    root = tmp_path / "vendor"
    rgb_path = root / "Photo" / "01_group.RGB"
    base_tick = 10 * 60 * 60 * 20_000
    _write_rgb(
        rgb_path,
        capture_date=date(2026, 8, 22),
        ticks=(base_tick, base_tick + 20, base_tick + 60, base_tick + 100),
    )
    catalog = AuyatRgbCatalog(root)
    catalog.scan()
    assert catalog.scan().status == "ready"
    capture = catalog.captures()[0]
    passage_store = PassageEventStore(tmp_path / "passages.jsonl")
    for event_id, sequence, bib, delta_ms in (
        ("passage-15", 1, "15", 1),
        ("passage-16", 2, "16", 3),
    ):
        timestamp_ms = capture.media_started_at_ms + delta_ms
        passage_store.append(
            PassageEvent(
                event_id=event_id,
                race_id="race-1",
                stage_id="stage-1",
                group_id="women-open",
                sequence=sequence,
                chip_id=f"chip-{bib}",
                bib=bib,
                passage_time_ms=timestamp_ms % 86_400_000,
                passage_timestamp_ms=timestamp_ms,
                lap=1,
                emitted_at_ms=timestamp_ms + 100,
            )
        )

    def locate(event, clock_offset_ms, pre_roll_ms):
        return catalog.locate(
            event.timeline_timestamp_ms,
            race_id=event.race_id,
            clock_offset_ms=clock_offset_ms,
            pre_roll_ms=pre_roll_ms,
        )

    dialog = PassageReviewDialog(
        passage_store,
        VideoTimelineStore(tmp_path / "video_timeline.jsonl"),
        high_speed_locator=locate,
    )
    dialog.show()
    assert _wait_until(qapp, lambda: dialog.high_speed_pane.video_view.has_frame)
    first_worker = dialog.high_speed_pane._worker
    assert isinstance(first_worker, AuyatRgbPlaybackWorker)
    assert dialog.table.item(dialog.table.currentRow(), 7).text() == "可查看"
    assert dialog.table.item(dialog.table.currentRow(), 8).text() == "芯片记录"
    assert dialog.high_speed_pane.video_view._marker_simple

    dialog.high_speed_pane.video_view.set_actual_size()
    dialog.high_speed_pane.video_view.zoom_by(1.2)
    zoom_before = dialog.high_speed_pane.video_view.zoom_percent
    next_row = 1 - dialog.table.currentRow()
    dialog.table.setCurrentCell(next_row, 0)
    dialog.table.selectRow(next_row)
    assert _wait_until(
        qapp,
        lambda: dialog.high_speed_pane._current_frame_index >= 0,
    )

    assert dialog.high_speed_pane._worker is first_worker
    assert dialog.high_speed_pane.video_view.zoom_percent == zoom_before
    assert dialog.high_speed_pane.video_view._marker[2] in {"15", "16"}
    assert dialog.high_speed_pane.video_view._marker_simple
    assert not dialog.high_speed_pane.open_btn.isEnabled()

    dialog.high_speed_pane.begin_marking()
    dialog.high_speed_pane._on_marker_position_selected(0.75, 0.5)
    assert dialog._confirm_pending_marker(dialog.high_speed_pane)
    selected_event_id = dialog._selected_event_id
    assert (
        dialog.association_store.get(selected_event_id, HIGH_SPEED_SOURCE)
        is not None
    )
    assert dialog.table.item(dialog.table.currentRow(), 7).text() == "已标记"
    assert dialog.table.item(dialog.table.currentRow(), 8).text() == "高速标记"

    dialog.close()
    qapp.processEvents()
