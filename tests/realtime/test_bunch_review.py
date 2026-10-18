import csv
import json
from dataclasses import replace

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QEventLoop, QPoint, Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from realtime.bunch_dialog import BunchDecoder, BunchReviewDialog, CountView
from realtime.bunch_review import (
    BunchChip, BunchReviewStore, align_counts, completion_issue, detected_crossings, load_context,
    add_viewed, uncovered_intervals, review_candidates, group_member_batch,
    mark_on_frame, unresolved_crossing_conflicts,
)
from realtime.clock_sync import ClockModel, ClockModelStore
from realtime.passage_receiver import PassageEvent
from realtime.problem_items import (
    ACTION_KEEP, ACTION_SIDE_BY_SIDE, CATEGORY_VIDEO, KIND_BUNCH, KIND_CHIP_NO_CROSSING, KIND_NEAR_CROSSING,
    KIND_NO_CHIP_CROSSING, KIND_TWO_CHIPS_ONE_CROSSING, ProblemItem,
)
from realtime.problem_panel import ProblemListPanel
from realtime.problem_resolutions import ProblemResolutionStore, export_lists
from realtime.video_timeline import VideoTimelineStore


T = 1_789_784_000_000


def chips(*times):
    return [BunchChip(f'e-{i}', str(101 + i), T + t, T + t + 300) for i, t in enumerate(times)]


def mark(t, bib='', x=.3):
    return {'recorder_ms': T + t, 'bib': bib, 'x': x, 'y': .5,
            'frame': {'video_path': 'synthetic.avi', 'segment_id': 's', 'frame_index': t // 50,
                      'position_ms': t, 'camera_index': 1}}


def test_typed_plate_has_priority_and_two_missing_riders_get_individual_times():
    cc = chips(1000, 2000, 3000)
    counts = [mark(1420, '101'), mark(1920, '319'), mark(2420, '102'),
              mark(2920, '550'), mark(3420, '103')]
    result = align_counts(cc, counts, 120)
    assert result['pairs'] == {0: 0, 2: 1, 4: 2}
    assert [(e['bib'], e['chip_time_ms']) for e in result['extras']] == [('319', T + 1500), ('550', T + 2500)]
    assert not result['missing']
    assert not completion_issue(cc, counts, result)


def test_side_by_side_plate_without_chip_is_never_assigned_to_nearby_chip():
    cc = [BunchChip('a', '348', T + 1000, T + 1300), BunchChip('b', '685', T + 1100, T + 1400),
          BunchChip('c', '999', T + 2000, T + 2300)]
    counts = [mark(1420, '348'), mark(1420, '319', .6), mark(1520, '685'), mark(2420, '999')]
    result = align_counts(cc, counts, 120)
    assert result['extras'] == [{'mark': 1, 'bib': '319', 'chip_time_ms': T + 1000, 'bracket_ms': 100}]
    assert result['pairs'][0] == 0 and result['pairs'][2] == 1
    untyped = align_counts(cc, [mark(1420), mark(1520), mark(2420)], 120)
    assert set(untyped['ambiguous']) == {0, 1}


def test_bias_uses_three_isolated_riders_and_no_extrapolation_at_bunch_edge():
    cc = chips(1000, 2000, 3000)
    counts = [mark(t + 420) for t in (1000, 2000, 3000)]
    result = align_counts(cc, counts, 0)
    assert result['bias_ms'] == 120 and result['lone_count'] == 3
    result = align_counts(cc, [mark(500, '319'), *counts], 0)
    assert result['extras'][0]['chip_time_ms'] is None
    assert '前后' in completion_issue(cc, [mark(500, '319'), *counts], result)


def test_cannot_complete_empty_missing_or_duplicate_counts():
    cc = chips(1000, 2000)
    counts = [mark(1300, '101')]
    assert '没有对应' in completion_issue(cc, counts, align_counts(cc, counts))
    assert '先' in completion_issue(cc, [], align_counts(cc, []))
    duplicate = [mark(1300, '101'), mark(2300, '101')]
    assert '同一号码' in completion_issue(cc, duplicate, align_counts(cc, duplicate))


def test_ten_riders_one_missed_and_one_double_chip_cannot_cancel_each_other():
    # Eight ordinary riders, a ninth carrying two chips, a tenth with no read.
    cc = chips(*range(1000, 9000, 1000), 9000, 9050)
    counts = [mark(t + 300, str(101 + i)) for i, t in enumerate(range(1000, 11000, 1000))]
    # Identity 110 belongs to the unread tenth rider, not the extra carried chip.
    cc[-1] = replace(cc[-1], bib='999')
    result = align_counts(cc, counts)
    assert len(counts) == len(cc) == 10
    assert result['missing'] == [9]
    assert [extra['bib'] for extra in result['extras']] == ['110']
    assert '没有对应' in completion_issue(cc, counts, result)

    # Even a perfect time-only permutation is not evidence of ten identities.
    unread = [mark(chip.recorder_ms - T) for chip in cc]
    result = align_counts(cc, unread)
    assert len(result['pairs']) == 10
    assert '未认号' in completion_issue(cc, unread, result)


def test_legacy_completed_review_reopens_without_erasing_marks_or_audit(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(chip.recorder_ms - T, chip.bib) for chip in context.chips] + [mark(2800, '319')]
    store = BunchReviewStore(root / 'finish_problems')
    record = store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    record.pop('completion_policy_version')
    store.path.write_text(json.dumps(record) + '\n')
    original = store.path.read_bytes()
    write_children(root, item, [])
    restored = BunchReviewStore(store.path.parent)
    checked = restored.get(context)
    assert not checked['completed'] and '旧核对' in checked['completion_issue']
    assert checked['marks'] == counts and restored.latest_for(item)['completed']
    assert restored.manual_entries([item], root) == []
    panel = ProblemListPanel()
    try:
        panel.set_workspace(root)
        assert panel.state(item) == '待处理'
        assert panel.current_marks() == counts
        assert store.path.read_bytes() == original
    finally:
        panel.close()
        app.processEvents()


def test_excluded_people_do_not_match_chips_or_change_bias_and_keep_original_indices():
    cc = chips(1000, 2000, 3000)
    counts = [mark(1420, '101'), dict(mark(1450, '101'), exclusion_reason='行人'),
              mark(2420, '102'), dict(mark(2450), exclusion_reason='工作人员'), mark(3420, '103')]
    result = align_counts(cc, counts)
    assert result['pairs'] == {0: 0, 2: 1, 4: 2}
    assert result['bias_ms'] == 120 and result['lone_count'] == 3
    assert result['extras'] == [] and result['missing'] == []
    assert completion_issue(cc, counts, result) == ''
    counts[2]['exclusion_reason'] = '其他无关人员'
    result = align_counts(cc, counts)
    assert result['missing'] == [1] and result['extras'] == []
    assert '没有对应' in completion_issue(cc, counts, result)
    excluded_only = [dict(mark(1300), exclusion_reason='行人')]
    assert completion_issue(cc, excluded_only, align_counts(cc, excluded_only))


def test_excluded_mark_cannot_supply_interpolation_anchor():
    cc = chips(1000, 3000)
    counts = [dict(mark(1300, '101'), exclusion_reason='工作人员'), mark(2300, '319'), mark(3300, '102')]
    result = align_counts(cc, counts)
    assert result['pairs'] == {2: 1}
    assert result['missing'] == [0]
    assert result['extras'] == [{'mark': 1, 'bib': '319', 'chip_time_ms': None, 'bracket_ms': None}]


def _detections(root, camera, crossings):
    directory = root / 'finish_problems'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'crossings_camera{camera}.json').write_text(
        json.dumps({'schema_version': 1, 'camera_index': camera, 'crossings': crossings}), encoding='utf-8')


def test_the_group_keeps_only_the_people_detected_inside_it(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    middle = (context.start_ms + context.end_ms) // 2
    _detections(root, 1, [
        {'recorder_ms': context.start_ms - 1, 'frame_index': 1},
        {'recorder_ms': context.start_ms, 'frame_index': 2, 'in_obstruction': True},
        {'recorder_ms': middle, 'frame_index': 3},
        {'recorder_ms': context.end_ms, 'frame_index': 4},
        {'recorder_ms': context.end_ms + 1, 'frame_index': 5},
        {'frame_index': 6},          # 缺时刻的记录不能猜一个时间塞进来
        'not-a-record',
    ])

    # 检测出的就是画面里的每一个人，不区分有没有芯片；本团之外的不算。
    assert context.detected == [{'recorder_ms': context.start_ms, 'in_obstruction': True},
                                {'recorder_ms': middle, 'in_obstruction': False},
                                {'recorder_ms': context.end_ms, 'in_obstruction': False}]


def test_a_missing_or_broken_detection_file_is_not_read_as_nobody_crossed(synthetic_bunch):
    root, item, _events = synthetic_bunch
    assert detected_crossings(root, 1) is None
    assert load_context(root, item).detected == []
    _detections(root, 2, [{'recorder_ms': T + 1000}])
    assert detected_crossings(root, 1) is None       # 别的机位有结果，不等于机位 1 有

    (root / 'finish_problems' / 'crossings_camera1.json').write_text('{', encoding='utf-8')
    assert detected_crossings(root, 1) is None
    inside = load_context(root, item).start_ms + 10
    _detections(root, 1, [{'recorder_ms': inside}])
    assert [entry['recorder_ms'] for entry in load_context(root, item).detected] == [inside]


@pytest.fixture
def synthetic_bunch(tmp_path):
    videos = tmp_path / 'videos'
    videos.mkdir()
    timeline = VideoTimelineStore(tmp_path / 'video_timeline.jsonl')
    segments = []
    for index in range(2):
        video = videos / f'camera_01_20260919_100000_archive_{index:04d}.avi'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (160, 120))
        assert writer.isOpened()
        for frame_index in range(80):
            frame = np.full((120, 160, 3), 40 + index * 80 + frame_index, np.uint8)
            writer.write(frame)
        writer.release()
        segment = timeline.add_completed_segment(
            source_id='camera_01_review', camera_index=1, video_path=video,
            media_started_at_ms=T + index * 4000, media_duration_ms=4000,
            clock_source='videopipe_system_clock', timing_error_ms=10,
            end_reason='continuous_archive_fallback', race_id='synthetic',
        )
        segments.append(segment)
    events = [PassageEvent(event_id=f'e-{i}', race_id='synthetic', stage_id='stage-1', group_id='1',
                          sequence=i + 1, bib=str(101 + i), passage_time_ms=t,
                          passage_timestamp_ms=T + t, received_at_ms=T + t + 100)
              for i, t in enumerate((2000, 3000, 4200, 5000))]
    (tmp_path / 'cyclerace_passage_events.jsonl').write_text(
        ''.join(json.dumps(e.to_payload()) + '\n' for e in events), encoding='utf-8')
    clock = ClockModelStore(tmp_path / 'video_clock_models.jsonl').publish(ClockModel(
        1, 300, 0, T, T, T + 8000, 10, 10, 0, 300, 10))
    (tmp_path / 'finish_lines.json').write_text(json.dumps({'finish_lines': [
        {'camera_index': 1, 'x1': .1, 'y1': .5, 'x2': .9, 'y2': .5}]}), encoding='utf-8')
    item = ProblemItem('bunch:synthetic', KIND_BUNCH, CATEGORY_VIDEO, 'synthetic bunch',
                       chip_time_ms=T + 2000, bibs=tuple(e.bib for e in events),
                       event_ids=tuple(e.event_id for e in events), calibration_version=clock.version,
                       evidence={'frame': {'camera_index': 1, 'segment_id': segments[0].segment_id,
                                          'video_path': str(videos / 'camera_01_20260919_100000_archive_0000.avi'),
                                          'position_ms': 2300, 'frame_index': 46}})
    return tmp_path, item, events


def test_context_and_decoder_span_recording_files_using_actual_frame_times(synthetic_bunch):
    root, item, _events = synthetic_bunch
    inputs = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
    context = load_context(root, item)
    assert len(context.segments) == 2
    decoder = BunchDecoder()
    try:
        first = decoder.read(context, T + 3950)
        following = decoder.read(context, first.recorder_ms, 1)
        previous = decoder.read(context, following.recorder_ms, -1)
        assert first.recorder_ms == T + 3950
        assert following.recorder_ms == T + 4000
        assert first.ref['segment_id'] != following.ref['segment_id']
        assert previous.recorder_ms == first.recorder_ms
        assert first.ref['frame_index'] == 79 and following.ref['frame_index'] == 0
        assert decoder.read(context, T - 10000) is None
    finally:
        decoder.close()
    assert all(p.read_bytes() == value for p, value in inputs.items())


def test_context_uses_recording_session_clock_offset_once(synthetic_bunch):
    root, item, _events = synthetic_bunch
    original = load_context(root, item)
    clock = replace(original.model, version=2, session_offsets=((original.segments[0].session_id, 50),))
    with (root / 'video_clock_models.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(clock.to_payload()) + '\n')
    context = load_context(root, replace(item, calibration_version=2))
    assert context.chips[0].recorder_ms == T + 2000 + 300 + 50


def test_drafts_roundtrip_and_completed_export_has_multiple_individual_frames(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    counts += [mark(2800, '319'), mark(4000, '550')]
    store = BunchReviewStore(root / 'finish_problems')
    store.save(context, counts, 12.5)
    assert BunchReviewStore(root / 'finish_problems').get(context)['marks'] == counts
    assert store.manual_entries([item], root) == []
    store.save(context, counts, 18.2, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert len(store.manual_entries([item], root)) == 2
    resolutions = ProblemResolutionStore(root / 'finish_problems' / 'problem_resolutions.jsonl')
    result = export_lists([item], resolutions, root / 'exports')
    assert result['补录清单.csv'] == 2
    rows = list(csv.DictReader((root / 'exports' / '补录清单.csv').open(encoding='utf-8-sig')))
    assert {r['号码'] for r in rows} == {'319', '550'}
    # Two missed riders never share one card or one time.
    assert len({r['证据帧'] for r in rows}) == 2
    assert len({r['芯片域时间'] for r in rows}) == 2
    assert all('插值估算' in r['说明'] for r in rows)
    assert all(r['时间来源'].startswith('集团内前后过线芯片之间插值') and '前后参考相隔' in r['时间来源'] for r in rows)
    # A change in the item or clock must never export an old result.
    changed_item = replace(item, bibs=('999', *item.bibs[1:]))
    assert store.manual_entries([changed_item], root) == []
    changed = replace(context, model=replace(context.model, intercept_ms=900))
    assert store.get(changed) is None
    # Different camera drafts must survive a switch back.
    other_camera = replace(context, camera=2)
    store.save(other_camera, [], 2)
    restored = BunchReviewStore(root / 'finish_problems')
    assert restored.get(context)['marks'] == counts
    assert restored.get(other_camera)['marks'] == []
    assert restored.manual_entries([item], root) == []  # latest review is unfinished


def test_incomplete_group_stays_pending_and_group_has_no_one_click_checked(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({
        'items': [item.to_payload()], 'summary': {}, 'auto_passed': []}), encoding='utf-8')
    panel = ProblemListPanel()
    panel.set_workspace(root)
    panel.table.setCurrentCell(0, 0)
    assert not panel.action_buttons       # 集团按人数判，不按那一列现成的理由
    context = load_context(root, item)
    # 判读栏自己也算一遍：点出来的人、芯片、检测到的过线并排放着比。
    _detections(root, 1, [{'recorder_ms': context.start_ms + 900, 'in_obstruction': False},
                          {'recorder_ms': context.start_ms + 4200, 'in_obstruction': False}])
    panel._sync_sidebar()
    assert panel.side_notice.text() == '画面 0 人 · 芯片 4 条 · 还差 4 人'
    store = BunchReviewStore(directory)
    store.save(context, [mark(2300, '101')], 10)
    panel.refresh()
    assert panel.state(item) == '待处理'
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    store.save(context, counts, 20, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    panel.refresh()
    assert panel.state(item).startswith('已处理')
    store.save(context, counts[:-1], 23)
    panel.refresh()
    assert panel.state(item) == '待处理'
    app.processEvents()


def test_the_group_is_counted_and_closed_on_the_problem_itself(synthetic_bunch):
    """Pointing at the riders on the frame is the whole group tool, kept inline."""

    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({
        'items': [item.to_payload()], 'summary': {}, 'auto_passed': []}), encoding='utf-8')
    panel = ProblemListPanel()
    panel.set_workspace(root)
    panel.table.setCurrentCell(0, 0)
    context = load_context(root, item)

    # Three of the four chips counted: the count is kept, the group stays open.
    for timestamp in range(context.start_ms, context.end_ms + 1, 50):
        panel.record_viewed_frame(item.evidence['frame'], timestamp)
    for chip in context.chips[:3]:
        panel.add_mark(0.3, 0.6, chip.recorder_ms, frame_at(context, chip.recorder_ms))
    panel._confirm_pending()
    record = BunchReviewStore(directory).latest_for(item)
    assert record['counted_riders'] == 3 and not record['completed']
    assert '仍有芯片没有对应的计数' in panel.hint.text()
    panel.refresh()
    assert panel.state(item) == '待处理'
    # The work the operator did is on the row, so the queue never looks untouched.
    row = next(index for index, entry in enumerate(panel.rows) if entry.item_id == item.item_id)
    assert panel.table.item(row, 2).text() == '3 人 / 4 条'
    assert panel.table.item(row, 2).foreground().color().name() == '#8a8f98'

    # Equal totals cannot close the group, even when every time reference fits.
    panel.add_mark(0.7, 0.6, context.chips[3].recorder_ms, frame_at(context, context.chips[3].recorder_ms))
    panel._confirm_pending()
    record = BunchReviewStore(directory).latest_for(item)
    assert record['counted_riders'] == 4 and not record['completed']
    assert '未认号' in panel.hint.text()
    for entry, chip in zip(panel.current_marks(), context.chips):
        entry['bib'] = chip.bib
    panel._confirm_pending()
    assert BunchReviewStore(directory).latest_for(item)['completed']
    assert panel.state(item).startswith('已处理：集团人工核对')
    assert not panel.current_marks()
    app.processEvents()


def test_reopening_a_completed_group_keeps_crossing_order_and_original_marks(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [dict(mark(chip.recorder_ms - T, chip.bib), frame=frame_at(context, chip.recorder_ms))
              for chip in reversed(context.chips)]
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}), encoding='utf-8')
    BunchReviewStore(directory).save(context, counts, 12, completed=True, viewed_ranges=full_view(context))
    panel = ProblemListPanel()
    try:
        panel.set_workspace(root)
        assert not panel.rows
        panel.queue_filter.setCurrentIndex(3)
        panel.toggle_big_view()
        assert panel.state(item).startswith('已处理')
        assert [panel.count_table.item(row, 0).text() for row in range(4)] == ['1', '2', '3', '4']
        panel.reopen_button.click()
        assert panel.state(item) == '待处理'
        assert panel.big_view
        assert panel.current_marks() == counts
        assert not BunchReviewStore(directory).latest_for(item)['completed']
        panel.queue_filter.setCurrentIndex(0)
        assert panel.current_item().item_id == item.item_id
        assert panel.current_marks() == counts  # 切回待处理也不会立刻再次自动关闭。
        panel.count_table.setCurrentCell(0, 0)
        assert panel._editing_mark == 3
        panel.exclude_button.click()
        assert panel.state(item) == '待处理'
        panel.restore_button.click()
        record = BunchReviewStore(directory).latest_for(item)
        assert not record['completed']
        for stamp in range(context.start_ms, context.end_ms + 1, 50):
            panel.record_viewed_frame(item.evidence['frame'], stamp)
        panel._confirm_pending()
        record = BunchReviewStore(directory).latest_for(item)
        assert record['completed']
        assert [value['recorder_ms'] for value in record['marks']] == [value['recorder_ms'] for value in counts]
    finally:
        panel.close()
        app.processEvents()


def test_equal_counts_stay_pending_when_time_reference_cannot_match_every_rider(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}), encoding='utf-8')
    panel = ProblemListPanel()
    panel.set_workspace(root)
    context = load_context(root, item)
    # Equal totals cannot replace actual associations.
    stamps = [context.start_ms + 100 + i * 100 for i in range(4)]
    for stamp in reversed(stamps):
        panel.add_mark(.3, .5, stamp, frame_at(context, stamp))
    panel._confirm_pending()
    record = BunchReviewStore(directory).latest_for(item)
    assert not record['completed'] and record['counted_riders'] == 4
    assert [mark['recorder_ms'] for mark in record['marks']] == list(reversed(stamps))
    assert BunchReviewStore(directory).manual_entries([item], root) == []
    panel.close()
    app.processEvents()


def test_exclusion_reason_and_evidence_roundtrip_without_exporting_cached_extras(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    counts += [dict(mark(2800, '888'), exclusion_reason='行人'), mark(4000, '550')]
    store = BunchReviewStore(root / 'finish_problems')
    record = store.save(context, counts, 18, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert [e['bib'] for e in record['extras']] == ['550']
    restored = BunchReviewStore(root / 'finish_problems')
    assert restored.get(context)['marks'][4] == counts[4]
    # Cached suggestions must not override the recorded classification.
    restored.latest[(item.item_id, context.camera)]['extras'].append(
        {'mark': 4, 'bib': '888', 'chip_time_ms': T + 2500})
    rows = restored.manual_entries([item], root)
    assert len(rows) == 1 and rows[0][0] == '550'
    assert '81' in rows[0][2]  # original mark index 5, frame 4000 / 50 displayed one-based
    counts[0]['exclusion_reason'] = '工作人员'
    # Excluding one matched rider leaves a chip unanswered despite equal totals.
    with pytest.raises(ValueError, match='没有对应'):
        store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    store.save(context, counts, 20)
    assert store.manual_entries([item], root) == []
    counts[1]['exclusion_reason'] = '行人'
    with pytest.raises(ValueError, match='没有对应'):
        store.save(context, counts, 20, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    store.save(context, counts, 20)
    assert store.manual_entries([item], root) == []


def test_dialog_can_exclude_restore_and_reopen_without_changing_other_marks(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    counts.insert(1, mark(2800, '101'))
    BunchReviewStore(root / 'finish_problems').save(context, counts, 4)
    dialog = BunchReviewDialog(root, item)
    try:
        dialog.count_table.setCurrentCell(1, 0)
        dialog.exclusion_reason.setCurrentText('行人')
        dialog.exclude_button.click()
        assert len(dialog.marks) == 5 and dialog.marks[1]['exclusion_reason'] == '行人'
        assert '选手计数 4 人' in dialog.summary.text() and '已排除 1 人' in dialog.summary.text()
        assert dialog.count_table.item(1, 3).text() == '已排除：行人'
        assert not dialog.bib_input.isEnabled() and dialog.restore_button.isEnabled()
        assert dialog.marks[0] == counts[0] and dialog.marks[2:] == counts[2:]
        assert dialog.save(False)
        dialog.restore()
        dialog.count_table.setCurrentCell(1, 0)
        assert dialog.marks[1]['exclusion_reason'] == '行人'
        dialog.restore_button.click()
        assert 'exclusion_reason' not in dialog.marks[1]
        assert dialog.bib_input.isEnabled() and not dialog.restore_button.isEnabled()
        assert '同一号码' in completion_issue(context.chips, dialog.marks,
                                            align_counts(context.chips, dialog.marks))
        assert dialog.save(False)
    finally:
        dialog.close()
        dialog.worker.requestInterruption()
        assert dialog.worker.wait(3000)
        app.processEvents()
    records = [json.loads(line) for line in (root / 'finish_problems' / 'bunch_reviews.jsonl').read_text(
        encoding='utf-8').splitlines()]
    assert any(r['marks'][1].get('exclusion_reason') == '行人' for r in records)
    assert 'exclusion_reason' not in records[-1]['marks'][1]


def test_the_strip_shows_every_detected_person_beside_the_chips(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    # 两个落点都避开芯片预期时刻，画出来才知道是谁的线。
    first, later, quiet = context.start_ms + 900, context.start_ms + 4200, context.start_ms + 550
    _detections(root, 1, [
        {'recorder_ms': context.start_ms - 400, 'in_obstruction': False},   # 本团之前
        {'recorder_ms': first, 'in_obstruction': False},
        {'recorder_ms': later, 'in_obstruction': True},
        {'recorder_ms': context.end_ms + 400, 'in_obstruction': False},     # 本团之后
    ])
    dialog = BunchReviewDialog(root, item)
    try:
        timeline = dialog.timeline
        assert [entry['recorder_ms'] for entry in timeline.detected] == [first, later]
        assert '画面检测 2 处过线' in dialog.summary.text()

        # 画出来看：每个人的线落在自己的时刻上，被遮挡的那一个浅一号。
        dialog.layout().activate()
        picture = QPixmap(timeline.size())
        timeline.render(picture)
        image = picture.toImage()
        assert image.width() == timeline.width()
        of = lambda ms: int((ms - context.start_ms) / (context.end_ms - context.start_ms) * (image.width() - 1))
        band = (timeline.PEOPLE_TOP + timeline.PEOPLE_BOTTOM) // 2
        person, hidden, empty = (image.pixelColor(of(ms), band) for ms in (first, later, quiet))
        assert person.blue() > 150 and person.red() < 120      # 蓝线：画面看见了这个人
        assert hidden.blue() > 150 and hidden.red() > person.red()   # 遮挡时的过线画浅一点
        assert empty.name() == '#edf1f5'                       # 没人过线的时刻只有底色
    finally:
        dialog.close()
        dialog.worker.requestInterruption()
        assert dialog.worker.wait(3000)
        app.processEvents()


def test_dialog_reads_counts_saves_reopens_and_releases_worker(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    dialog = BunchReviewDialog(root, item)
    try:
        for _ in range(100):
            app.processEvents(QEventLoop.AllEvents, 50)
            if dialog.frame is not None:
                break
            QTest.qWait(10)
        assert dialog.frame is not None, dialog.notice.text()
        dialog.count_at(.3, .5)
        dialog.count_at(.3, .5)  # accidental repeat click is ignored
        dialog.count_at(.6, .5)  # two abreast in the same actual frame
        assert len(dialog.marks) == 2
        original_frame = dialog.frame
        dialog.frame = replace(original_frame, ref=dict(original_frame.ref, frame_index=999))
        dialog.count_at(.8, .5, False, original_frame)
        assert dialog.marks[-1]['frame']['frame_index'] == original_frame.ref['frame_index']
        dialog.marks.pop()
        dialog.frame = original_frame
        dialog.update_results()
        dialog.count_table.setCurrentCell(1, 0)
        dialog.bib_input.setText('319')
        dialog.assign_bib()
        assert dialog.marks[1]['bib'] == '319'
        assert dialog.save(False)
        assert not dialog.save(True)
        dialog.close()
        for _ in range(100):
            app.processEvents(QEventLoop.AllEvents, 50)
            if not dialog.worker.isRunning():
                break
            QTest.qWait(10)
        assert not dialog.worker.isRunning()
        record = BunchReviewStore(root / 'finish_problems').get(dialog.context)
        assert len(record['marks']) == 2 and not record['completed']
    finally:
        dialog.worker.requestInterruption()
        assert dialog.worker.wait(3000)


def test_zoom_double_click_adds_no_rider_and_rapid_clicks_keep_source_frame():
    app = QApplication.instance() or QApplication([])
    view = CountView()
    view.resize(600, 400)
    view.show()
    source = object()
    view.show_frame(np.zeros((300, 500, 3), np.uint8), {}, [], source)
    counted = []
    view.counted.connect(lambda *args: counted.append(args))
    app.processEvents()
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=QPoint(200, 200))
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=QPoint(200, 200))
    QTest.qWait(QApplication.doubleClickInterval() * 2 + 50)
    assert counted == []
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=QPoint(150, 200))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=QPoint(400, 200))
    view.show_frame(np.zeros((300, 500, 3), np.uint8), {}, [], object())
    QTest.qWait(QApplication.doubleClickInterval() * 2 + 50)
    assert len(counted) == 2
    assert all(args[3] is source for args in counted)
    view.close()


def test_legacy_group_without_frame_uses_only_unique_calibrated_camera(synthetic_bunch):
    root, item, _events = synthetic_bunch
    original = load_context(root, item)
    legacy = replace(item, evidence={})
    assert load_context(root, legacy).camera == 1
    # Another available camera makes an unlabelled legacy item ambiguous.
    timeline = VideoTimelineStore(root / 'video_timeline.jsonl')
    segment = original.segments[0]
    camera2_path = segment.path.with_name('camera_02_20260919_100000_archive_0000.avi')
    camera2_path.write_bytes(segment.path.read_bytes())
    timeline.add_completed_segment(source_id='cam2', camera_index=2, video_path=camera2_path,
                                   media_started_at_ms=T, media_duration_ms=8000, clock_source='videopipe_system_clock',
                                   timing_error_ms=0, end_reason='continuous_archive_fallback', race_id='synthetic')
    ClockModelStore(root / 'video_clock_models.jsonl').publish(replace(original.model, camera_index=2))
    with pytest.raises(ValueError, match='唯一'):
        load_context(root, legacy)
    assert load_context(root, replace(legacy, evidence={'camera_index': 2})).camera == 2


def test_viewed_ranges_block_completion_in_the_store(synthetic_bunch):
    """The inline UI and the dialog must share the same coverage gate."""

    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    ranges = add_viewed([], context.start_ms, context.start_ms, context.end_ms)
    counts = [mark(c.recorder_ms-T, c.bib) for c in context.chips]
    result = align_counts(context.chips, counts)
    assert not result['missing'] and not result['extras']
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='未查看'):
        store.save(context, counts, 10, completed=True, fully_viewed=True, viewed_ranges=ranges)
    saved = store.save(context, counts, 10, completed=False, viewed_ranges=ranges,
                       visited_candidates=['count_difference'])
    assert not saved['completed'] and saved['viewed_ranges'] == ranges
    assert saved['visited_candidates'] == ['count_difference']


def test_complete_time_alignment_still_requires_identification(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms-T) for c in context.chips]
    ranges = []
    for timestamp in range(context.start_ms, context.end_ms+50, 50):
        ranges = add_viewed(ranges, timestamp, context.start_ms, context.end_ms)
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='未认号'):
        store.save(context, counts, 20, fully_viewed=True, completed=True, viewed_ranges=ranges)
    for entry, chip in zip(counts, context.chips):
        entry['bib'] = chip.bib
    saved = store.save(context, counts, 20, completed=True, viewed_ranges=ranges)
    assert saved['counted_riders'] == 4 and saved['identified_riders'] == 4
    assert saved['completed'] and not saved['extras']


def test_actual_recording_gap_blocks_completion_even_if_checkbox_set(synthetic_bunch):
    root, item, _events = synthetic_bunch
    # First segment ends before this final group time, and the second was lost.
    next((root / 'videos').glob('*0001.avi')).unlink()
    context = load_context(root, item)
    assert context.gaps
    counts = [mark(c.recorder_ms-T) for c in context.chips]
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='缺口'):
        store.save(context, counts, 10, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert any(c['id'].startswith('gap:') for c in review_candidates(context, counts, align_counts(context.chips, counts)))


def test_candidate_navigation_uses_child_evidence_and_unmatched_counts(synthetic_bunch):
    root, item, _events = synthetic_bunch
    item = replace(item, evidence=dict(item.evidence, video_count=3))
    context = load_context(root, item)
    directory = root / 'finish_problems'
    directory.mkdir()
    child = ProblemItem('child', 'no_chip_crossing', CATEGORY_VIDEO, 'candidate',
                        evidence={'group': item.item_id, 'recorder_ms': T+2800, 'frame': {'camera_index': 1}})
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload(), child.to_payload()]}))
    counts = [mark(2300), mark(2800, 'unread'), mark(3300)]
    candidates = review_candidates(context, counts, align_counts(context.chips, counts))
    assert {c['id'] for c in candidates} >= {'child', 'count_difference', 'extra:1', 'chip:e-2'}
    assert next(c for c in candidates if c['id']=='child')['start_ms'] == T+2550


def test_member_deletion_in_group_exports_one_record_and_blocks_completion(synthetic_bunch):
    from realtime.batch_review import BatchDecisionStore
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    batch = group_member_batch(context)
    store = BatchDecisionStore(root / 'finish_problems')
    store.save(batch, [context.chips[0].event_id], 'delete', 'extra chip reviewed')
    resolutions = ProblemResolutionStore(root / 'finish_problems/problem_resolutions.jsonl')
    assert export_lists([item], resolutions, root / 'exports')['建议删除清单.csv'] == 1
    rows = list(csv.DictReader((root / 'exports/建议删除清单.csv').open(encoding='utf-8-sig')))
    assert rows[0]['事件ID'] == context.chips[0].event_id
    counts = [mark(c.recorder_ms-T, c.bib) for c in context.chips]
    with pytest.raises(ValueError, match='成员待 CycleRace'):
        BunchReviewStore(root / 'finish_problems').save(
            context, counts, 30, completed=True, viewed_ranges=full_view(context))


def test_group_context_revision_changes_invalidate_draft(synthetic_bunch):
    root, item, events = synthetic_bunch
    context = load_context(root, item)
    store = BunchReviewStore(root / 'finish_problems')
    store.save(context, [], 10)
    with (root / 'cyclerace_passage_events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(replace(events[0], revision=events[0].revision+1).to_payload())+'\n')
    revised = load_context(root, item)
    assert revised.chips[0].revision != context.chips[0].revision
    assert store.get(revised) is None


def test_dialog_local_navigation_leaves_group_pending_and_tracks_frames(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    item = replace(item, evidence=dict(item.evidence, video_count=2))
    dialog = BunchReviewDialog(root, item)
    try:
        for _ in range(100):
            app.processEvents(QEventLoop.AllEvents, 50)
            if not dialog.busy:
                break
            QTest.qWait(10)
        assert dialog.frame is not None
        assert dialog.candidates and dialog.viewed_ranges
        assert not dialog.fully_viewed.isEnabled()
        assert not dialog.save(True)
        dialog.navigate_candidate(1)
        for _ in range(100):
            app.processEvents(QEventLoop.AllEvents, 50)
            if not dialog.busy:
                break
            QTest.qWait(10)
        assert 'count_difference' in dialog.visited_candidates
        assert dialog.save(False)
        assert not dialog.store.get(dialog.context)['completed']
    finally:
        dialog.close()
        dialog.worker.requestInterruption()
        assert dialog.worker.wait(3000)
        app.processEvents()


def test_the_workbench_is_the_group_tool_and_keeps_the_live_analysis_running(synthetic_bunch, monkeypatch):
    """集团判读就是判读那一屏，不再停下来开一个窗口。"""

    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}))
    panel = ProblemListPanel()
    panel.set_workspace(root)
    panel.table.setCurrentCell(0, 0)
    stops = []
    monkeypatch.setattr(panel, 'stop_analysis', lambda: stops.append('stop'))
    panel.show()
    try:
        QTest.keyClick(panel, Qt.Key_F)
        assert stops == []                     # 分析继续跑：核对不再打断接收
        assert panel.timeline.isVisibleTo(panel)
        assert not hasattr(panel, 'bunch_button')
        QTest.keyClick(panel, Qt.Key_Escape)
        assert not panel.timeline.isVisibleTo(panel)   # 回到清单，刻度让位给队列
    finally:
        panel.close()
        app.processEvents()


def test_the_group_ruler_shows_the_people_and_seeks_from_a_click(synthetic_bunch):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}))
    context = load_context(root, item)
    middle = context.start_ms + 900
    _detections(root, 1, [{'recorder_ms': middle, 'in_obstruction': False}])
    _strips(root, context)
    panel = ProblemListPanel()
    panel.set_workspace(root)
    panel.table.setCurrentCell(0, 0)
    jumps = []
    panel.jump_requested.connect(jumps.append)
    panel.resize(1400, 700)
    panel.show()
    app.processEvents()
    panel.layout().activate()
    app.processEvents()
    try:
        QTest.keyClick(panel, Qt.Key_F)
        app.processEvents()
        panel.layout().activate()
        timeline = panel.timeline
        assert timeline.isVisibleTo(panel) and timeline.context is not None
        assert [entry['recorder_ms'] for entry in timeline.detected] == [middle]
        assert len(timeline.context.chips) == 4
        # 刻度在画面上面，图片带晚一步补上：判读屏先出来，拼图不挡按键。
        assert panel.layout().indexOf(timeline) < panel.layout().indexOf(panel.splitter)
        for _ in range(60):
            app.processEvents(QEventLoop.AllEvents, 20)
            if timeline.image is not None:
                break
            QTest.qWait(20)
        assert timeline.image is not None

        # 点出来的人立刻出现在刻度上：人、芯片、检测三者同一条轴。
        panel.add_mark(.3, .5, middle, frame_at(context, middle))
        assert [mark['recorder_ms'] for mark in timeline.marks] == [middle]

        # 点刻度就跳到那一刻：给出所在分段和片内位置，画面自己定位到帧。
        app.processEvents()
        click_x = timeline.width() * 3 // 4
        QTest.mouseClick(timeline, Qt.LeftButton, pos=QPoint(click_x, 60))
        app.processEvents()
        # 点在哪一段上，就跳到哪一段的对应时刻（宽度以落点那一刻为准）。
        clicked = click_x / max(1, timeline.width() - 1)
        assert [jump['frame_index'] for jump in jumps] == [46, None]   # 先跳本团原帧，再跳点中的那一刻
        piece = next(s for s in context.segments if s.segment.segment_id == jumps[-1]['segment_id'])
        moment = piece.start_ms + jumps[-1]['position_ms']
        assert abs((moment - context.start_ms) / (context.end_ms - context.start_ms) - clicked) <= 0.02
        assert jumps[-1]['camera_index'] == context.camera
    finally:
        panel.close()
        app.processEvents()


def _strips(root, context):
    """Write a sampled strip per segment, as the sampling run would have."""

    from realtime.line_sampling import LineGeometry, LineStrip, line_config_digest, save_strip
    directory = root / 'finish_problems/strips/camera_01'
    directory.mkdir(parents=True)
    for segment in context.segments:
        line = context.lines[segment.segment.segment_id]
        geometry = LineGeometry.from_config(line, 160, 120, samples=10)
        strip = LineStrip(np.zeros((10, 80, 3), np.uint8), np.arange(80)*50, geometry, str(segment.path), 20)
        save_strip(directory / (segment.path.stem + '.npz'), strip,
                   {'segment_id': segment.segment.segment_id, 'line_digest': line_config_digest(line)})
    return directory


def test_cross_segment_strip_rejects_stale_line_and_keeps_cache_gap(synthetic_bunch):
    from realtime.bunch_timeline import group_strip
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    _strips(root, context)
    image, present = group_strip(context)
    assert present and image[:, :800].mean() < 30 and image[:, 1000:].mean() < 30
    last = context.segments[-1].segment.segment_id
    changed = dict(context.lines[last], y1=.7)
    context.lines[last] = changed
    image, present = group_strip(context)
    assert present and image[:, :700].mean() < 30
    assert np.all(image[:, 1000:] == 230)


def test_later_member_hold_reopens_group_and_suppresses_manual_export(synthetic_bunch):
    from realtime.batch_review import BatchDecisionStore
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms-T, c.bib) for c in context.chips] + [mark(2800, '319')]
    store = BunchReviewStore(root / 'finish_problems')
    store.save(context, counts, 20, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert len(store.manual_entries([item], root)) == 1
    BatchDecisionStore(root / 'finish_problems').save(
        group_member_batch(context), [context.chips[0].event_id], 'hold', 'needs another camera')
    # A new hold reopens the group as well as suppressing its export.
    assert store.manual_entries([item], root) == []
    (root / 'finish_problems/problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}))
    panel = ProblemListPanel()
    panel.set_workspace(root)
    assert panel.state(item) == '待处理'
    panel.close()
    app.processEvents()


def test_obstruction_and_two_unread_counts_remain_pending(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, replace(item, kind='obstruction'))
    counts = [mark(c.recorder_ms-T) for c in context.chips] + [mark(2800), mark(3900)]
    result = align_counts(context.chips, counts)
    assert len(result['extras']) == 2
    assert any(c['id'] == 'obstruction' for c in review_candidates(context, counts, result))
    store = BunchReviewStore(root / 'finish_problems')
    saved = store.save(context, counts, 20, viewed_ranges=[[context.start_ms, context.end_ms]])
    assert saved['counted_riders'] == 6 and saved['identified_riders'] == 0
    with pytest.raises(ValueError, match='号码'):
        store.save(context, counts, 20, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert store.manual_entries([context.item], root) == []


def frame_at(context, stamp, **extra):
    """The frame an operator is looking at, in the shape the panes produce."""

    segment = next(s for s in context.segments if s.start_ms <= stamp <= s.end_ms)
    position = int(stamp - segment.start_ms)
    return {'camera_index': context.camera, 'segment_id': segment.segment.segment_id,
            'video_path': str(segment.path), 'frame_index': position // 50, 'position_ms': position, **extra}


def counted(context, recorder_ms, bib='', x=.3):
    """One pointed-at person, with the extra metadata a saved mark really carries."""

    return {'recorder_ms': int(recorder_ms), 'bib': bib, 'x': x, 'y': .5, 'exclusion_reason': '',
            'frame': frame_at(context, recorder_ms, recorder_ms=int(recorder_ms))}


def crossing_child(item, kind, key, *, recorder_ms, frame=None, bibs=(), event_ids=(), chip_time_ms=None,
                   **evidence):
    payload = {'group': item.item_id, 'recorder_ms': int(recorder_ms), **evidence}
    if frame is not None:
        payload['frame'] = frame
    return ProblemItem(f'{kind}:{key}', kind, CATEGORY_VIDEO, 'group child', chip_time_ms=chip_time_ms,
                       bibs=tuple(bibs), event_ids=tuple(event_ids), evidence=payload)


def write_children(root, item, children):
    directory = root / 'finish_problems'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'problem_list.json').write_text(json.dumps({
        'items': [item.to_payload(), *(child.to_payload() for child in children)],
        'summary': {}, 'auto_passed': []}), encoding='utf-8')


def full_view(context):
    ranges = []
    for timestamp in range(context.start_ms, context.end_ms + 50, 50):
        ranges = add_viewed(ranges, timestamp, context.start_ms, context.end_ms)
    return ranges


def test_a_normal_group_requires_numbers_even_when_count_answers_the_child(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    child = crossing_child(item, KIND_NEAR_CROSSING, 'e-1', recorder_ms=context.chips[1].recorder_ms,
                           frame=frame_at(context, context.chips[1].recorder_ms, session_id='s',
                                          calibration_version=context.model.version))
    write_children(root, item, [child])
    counts = [counted(context, chip.recorder_ms) for chip in context.chips]
    result = align_counts(context.chips, counts)
    assert len(result['pairs']) == 4
    assert unresolved_crossing_conflicts(context, counts, result) == []
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='未认号'):
        store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    for entry, chip in zip(counts, context.chips):
        entry['bib'] = chip.bib
    saved = store.save(
        context, counts, 20, completed=True, fully_viewed=True, viewed_ranges=full_view(context))
    assert saved['completed'] and saved['counted_riders'] == 4 and saved['identified_riders'] == 4
    assert not saved['extras']
    # An excluded person is not a counted rider, so the child opens again.
    excluded = [dict(mark, exclusion_reason='行人') for mark in counts]
    assert [c.item_id for c in unresolved_crossing_conflicts(
        context, excluded, align_counts(context.chips, excluded))] == [child.item_id]


def test_frame_identity_tolerates_extra_metadata_and_kept_copies():
    current = {'camera_index': 1, 'segment_id': 'seg', 'video_path': 'a.avi', 'frame_index': 10,
               'position_ms': 500, 'recorder_ms': T + 500, 'session_id': 's'}
    marked = {'camera_index': 1, 'segment_id': 'seg', 'video_path': 'a.avi', 'frame_index': 10,
              'position_ms': 500, 'session_id': 's', 'calibration_version': 7, 'line_config': {'x1': .1}}
    assert mark_on_frame({'recorder_ms': T + 500, 'frame': dict(marked, recorder_ms=T + 500)}, current, T + 500)
    # The same actual frame survives a rebuilt stamp and a stale frame number...
    assert mark_on_frame({'recorder_ms': T + 620, 'frame': marked}, current, T + 500)
    assert mark_on_frame({'recorder_ms': T + 500, 'frame': dict(marked, frame_index=12)}, current, T + 500)
    # ...but another camera, another moment or another frame is another person.
    assert not mark_on_frame({'recorder_ms': T + 500, 'frame': dict(marked, camera_index=2)}, current, T + 500)
    assert not mark_on_frame({'recorder_ms': T + 580, 'frame': dict(marked, frame_index=12)}, current, T + 500)
    assert not mark_on_frame({'recorder_ms': T + 500, 'frame': dict(marked, segment_id='other', frame_index=99)},
                             dict(current, camera_index=2), T + 500)
    # A rebound archive has a new segment but the same moment; old drafts carry no frame at all.
    assert mark_on_frame({'recorder_ms': T + 500, 'frame': dict(marked, segment_id='other', frame_index=99)},
                         current, T + 500)
    assert mark_on_frame({'recorder_ms': T + 500, 'bib': '319'}, current, T + 500)


def test_a_doubt_added_later_reopens_the_group_and_suppresses_export(synthetic_bunch):
    """Fresh child evidence must be checked even if the parent signature is unchanged."""

    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    assert context.child_items == []  # the context filled its child list before any child existed
    counts = [counted(context, chip.recorder_ms, chip.bib) for chip in context.chips]
    counts.append(counted(context, context.chips[1].recorder_ms - 400, '319'))
    store = BunchReviewStore(root / 'finish_problems')
    record = store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    assert len(store.manual_entries([item], root)) == 1
    third, last = context.chips[2], context.chips[3]
    child = crossing_child(item, KIND_TWO_CHIPS_ONE_CROSSING, 'e-2-e-3', recorder_ms=third.recorder_ms,
                           frame=frame_at(context, third.recorder_ms), bibs=(third.bib, last.bib),
                           event_ids=(third.event_id, last.event_id), chip_time_ms=T + 4200)
    write_children(root, item, [child])
    marks = record['marks']
    assert not store.get(context)['completed']
    assert store.latest_for(item)['completed']  # the audit record is preserved
    with pytest.raises(ValueError, match='疑点未处理'):
        store.save(context, marks, 20, completed=True, viewed_ranges=full_view(context))
    assert store.manual_entries([item], root) == []
    # 那一行还等着操作员：留哪一块芯片，或在那一帧上认号。
    assert [c.item_id for c in unresolved_crossing_conflicts(context, marks,
                                                             align_counts(context.chips, marks))] == [child.item_id]


def test_a_child_answer_stops_counting_when_the_child_evidence_moves(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    first, second = context.chips[0], context.chips[1]
    child = crossing_child(item, KIND_TWO_CHIPS_ONE_CROSSING, 'e-0-e-1', recorder_ms=first.recorder_ms,
                           frame=frame_at(context, first.recorder_ms), bibs=(first.bib, second.bib),
                           event_ids=(first.event_id, second.event_id))
    write_children(root, item, [child])
    ProblemResolutionStore(root / 'finish_problems/problem_resolutions.jsonl').resolve(child, ACTION_SIDE_BY_SIDE)
    marks = [counted(context, chip.recorder_ms, chip.bib) for chip in context.chips]
    result = align_counts(context.chips, marks)
    assert unresolved_crossing_conflicts(context, marks, result) == []
    moved = replace(child, evidence=dict(child.evidence, frame=frame_at(context, second.recorder_ms)))
    write_children(root, item, [moved])
    assert [c.item_id for c in unresolved_crossing_conflicts(context, marks, result)] == [child.item_id]


def test_a_generic_checked_mark_never_answers_a_group_child(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    first, second = context.chips[0], context.chips[1]
    child = crossing_child(item, KIND_TWO_CHIPS_ONE_CROSSING, 'e-0-e-1', recorder_ms=first.recorder_ms,
                           frame=frame_at(context, first.recorder_ms), bibs=(first.bib, second.bib),
                           event_ids=(first.event_id, second.event_id), chip_time_ms=T + 2000)
    write_children(root, item, [child])
    journal = root / 'finish_problems/problem_resolutions.jsonl'
    journal.write_text(json.dumps({
        'schema_version': 1, 'item_id': child.item_id, 'kind': child.kind, 'action': 'checked',
        'binding': {'bibs': list(child.bibs), 'event_ids': list(child.event_ids), 'chip_time_ms': child.chip_time_ms},
        'frame': dict(child.evidence['frame']), 'calibration_version': 0, 'analysis_signature': '',
    }) + '\n', encoding='utf-8')
    marks = [counted(context, chip.recorder_ms, chip.bib) for chip in context.chips]
    result = align_counts(context.chips, marks)
    assert [c.item_id for c in unresolved_crossing_conflicts(context, marks, result)] == [child.item_id]


def test_equal_identified_counts_cannot_override_a_double_chip_conflict(synthetic_bunch):
    """Even identified marks cannot override an unanswered double-chip doubt."""

    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    first, second = context.chips[0], context.chips[1]
    child = crossing_child(item, KIND_TWO_CHIPS_ONE_CROSSING, 'e-0-e-1', recorder_ms=first.recorder_ms,
                           frame=frame_at(context, first.recorder_ms), bibs=(first.bib, second.bib),
                           event_ids=(first.event_id, second.event_id), chip_time_ms=T + 2000)
    write_children(root, item, [child])
    marks = [counted(context, chip.recorder_ms, chip.bib) for chip in context.chips]
    result = align_counts(context.chips, marks)
    assert len(result['pairs']) == 4 and not result['missing'] and not result['extras']
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='疑点未处理'):
        store.save(context, marks, 20, completed=True, viewed_ranges=full_view(context))
    assert [c.item_id for c in unresolved_crossing_conflicts(context, marks, result)] == [child.item_id]
    ProblemResolutionStore(root / 'finish_problems/problem_resolutions.jsonl').resolve(child, ACTION_SIDE_BY_SIDE)
    saved = store.save(context, marks, 20, completed=True, viewed_ranges=full_view(context))
    assert saved['completed'] and saved['counted_riders'] == 4 and saved['identified_riders'] == 4
    assert unresolved_crossing_conflicts(context, marks, result) == []


def test_a_missed_crossing_blocks_the_group_until_accounted_for(synthetic_bunch):
    """An uncounted crossing must not disappear behind equal group totals."""

    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    missed_ms = context.chips[1].recorder_ms + 400
    child = crossing_child(item, KIND_NO_CHIP_CROSSING, 'crossing', recorder_ms=missed_ms,
                           frame=frame_at(context, missed_ms), chip_time_ms=missed_ms - 300)
    write_children(root, item, [child])
    counts = [counted(context, chip.recorder_ms, chip.bib) for chip in context.chips]
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='疑点未处理'):
        store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    # 连同漏读的那位一起数进来，他就走本团的补录清单。
    counts = [*counts, counted(context, missed_ms, '319')]
    saved = store.save(context, counts, 20, completed=True, viewed_ranges=full_view(context))
    assert saved['completed'] and saved['counted_riders'] == 5 and saved['identified_riders'] == 5
    assert [row[0] for row in store.manual_entries([item], root)] == ['319']
