import csv
import json
from dataclasses import replace

import cv2
import numpy as np
import pytest
from PyQt5.QtCore import QEventLoop, QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from realtime.bunch_dialog import BunchDecoder, BunchReviewDialog, CountView
from realtime.bunch_review import (
    BunchChip, BunchReviewStore, align_counts, completion_issue, load_context,
    add_viewed, uncovered_intervals, review_candidates, group_member_batch,
)
from realtime.clock_sync import ClockModel, ClockModelStore
from realtime.passage_receiver import PassageEvent
from realtime.problem_items import CATEGORY_VIDEO, KIND_BUNCH, ProblemItem
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
    assert not completion_issue(cc, counts, result, True)


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
    assert '前后' in completion_issue(cc, [mark(500, '319'), *counts], result, True)


def test_cannot_complete_empty_unviewed_missing_or_duplicate_counts():
    cc = chips(1000, 2000)
    counts = [mark(1300, '101')]
    assert '完整' in completion_issue(cc, counts, align_counts(cc, counts), False)
    assert '没有对应' in completion_issue(cc, counts, align_counts(cc, counts), True)
    assert '先' in completion_issue(cc, [], align_counts(cc, []), True)
    duplicate = [mark(1300, '101'), mark(2300, '101')]
    assert '同一号码' in completion_issue(cc, duplicate, align_counts(cc, duplicate), True)


def test_excluded_people_do_not_match_chips_or_change_bias_and_keep_original_indices():
    cc = chips(1000, 2000, 3000)
    counts = [mark(1420, '101'), dict(mark(1450, '101'), exclusion_reason='行人'),
              mark(2420, '102'), dict(mark(2450), exclusion_reason='工作人员'), mark(3420, '103')]
    result = align_counts(cc, counts)
    assert result['pairs'] == {0: 0, 2: 1, 4: 2}
    assert result['bias_ms'] == 120 and result['lone_count'] == 3
    assert result['extras'] == [] and result['missing'] == []
    assert completion_issue(cc, counts, result, True) == ''
    counts[2]['exclusion_reason'] = '其他无关人员'
    result = align_counts(cc, counts)
    assert result['missing'] == [1] and result['extras'] == []
    assert '没有对应' in completion_issue(cc, counts, result, True)
    excluded_only = [dict(mark(1300), exclusion_reason='行人')]
    assert completion_issue(cc, excluded_only, align_counts(cc, excluded_only), True)


def test_excluded_mark_cannot_supply_interpolation_anchor():
    cc = chips(1000, 3000)
    counts = [dict(mark(1300, '101'), exclusion_reason='工作人员'), mark(2300, '319'), mark(3300, '102')]
    result = align_counts(cc, counts)
    assert result['pairs'] == {2: 1}
    assert result['missing'] == [0]
    assert result['extras'] == [{'mark': 1, 'bib': '319', 'chip_time_ms': None, 'bracket_ms': None}]


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
    store.save(context, counts, 18.2, completed=True, fully_viewed=True)
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
    assert panel.bunch_button.isHidden() is False
    assert not panel.action_buttons
    context = load_context(root, item)
    store = BunchReviewStore(directory)
    store.save(context, [mark(2300, '101')], 10)
    panel.refresh()
    assert panel.state(item) == '待处理'
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    store.save(context, counts, 20, completed=True, fully_viewed=True)
    panel.refresh()
    assert panel.state(item).startswith('已处理')
    store.save(context, counts[:-1], 23)
    panel.refresh()
    assert panel.state(item) == '待处理'
    app.processEvents()


def test_exclusion_reason_and_evidence_roundtrip_without_exporting_cached_extras(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms - T, c.bib) for c in context.chips]
    counts += [dict(mark(2800, '888'), exclusion_reason='行人'), mark(4000, '550')]
    store = BunchReviewStore(root / 'finish_problems')
    record = store.save(context, counts, 18, completed=True, fully_viewed=True)
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
    with pytest.raises(ValueError, match='没有对应'):
        store.save(context, counts, 20, completed=True, fully_viewed=True)
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
                                            align_counts(context.chips, dialog.marks), True)
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


def test_seeking_first_and_last_frames_cannot_cover_unseen_middle(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    ranges = add_viewed([], context.start_ms, context.start_ms, context.end_ms)
    ranges = add_viewed(ranges, context.end_ms, context.start_ms, context.end_ms)
    assert uncovered_intervals(context.start_ms, context.end_ms, ranges)
    counts = [mark(c.recorder_ms-T) for c in context.chips]
    result = align_counts(context.chips, counts)
    assert not result['missing'] and not result['extras']
    store = BunchReviewStore(root / 'finish_problems')
    with pytest.raises(ValueError, match='未查看'):
        store.save(context, counts, 10, completed=True, fully_viewed=True, viewed_ranges=ranges)
    store.save(context, counts, 10, viewed_ranges=ranges, visited_candidates=['count_difference'])
    restored = store.get(context)
    assert restored['viewed_ranges'] == ranges
    assert restored['visited_candidates'] == ['count_difference']
    assert not restored['completed']


def test_normal_complete_count_does_not_require_names(synthetic_bunch):
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    counts = [mark(c.recorder_ms-T) for c in context.chips]
    ranges = []
    for timestamp in range(context.start_ms, context.end_ms+50, 50):
        ranges = add_viewed(ranges, timestamp, context.start_ms, context.end_ms)
    store = BunchReviewStore(root / 'finish_problems')
    saved = store.save(context, counts, 20, fully_viewed=True, completed=True, viewed_ranges=ranges)
    assert saved['counted_riders'] == 4 and saved['identified_riders'] == 0
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
        store.save(context, counts, 10, completed=True, fully_viewed=True)
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
    counts = [mark(c.recorder_ms-T) for c in context.chips]
    with pytest.raises(ValueError, match='成员'):
        BunchReviewStore(root / 'finish_problems').save(context, counts, 30, completed=True, fully_viewed=True)


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


@pytest.mark.parametrize('running,paused', [(True, False), (True, True), (False, False), (False, True)])
def test_group_entry_preserves_analysis_schedule_and_chip_receiver(synthetic_bunch, monkeypatch, running, paused):
    app = QApplication.instance() or QApplication([])
    root, item, _events = synthetic_bunch
    directory = root / 'finish_problems'
    directory.mkdir()
    (directory / 'problem_list.json').write_text(json.dumps({'items': [item.to_payload()]}))
    panel = ProblemListPanel()
    panel.set_workspace(root)
    panel.table.setCurrentCell(0, 0)
    receiver = object()
    panel._chip_process = receiver
    panel._process = object() if running else None
    panel._live_paused = paused
    calls = []

    def stop():
        assert panel._live_paused
        panel._process = None
        calls.append('stop')

    class Dialog:
        saved_completed = False

        def __init__(self, *args):
            pass

        def exec_(self):
            assert panel._process is None and panel._live_paused
            assert panel._chip_process is receiver
            calls.append('dialog')

    monkeypatch.setattr('realtime.bunch_dialog.BunchReviewDialog', Dialog)
    monkeypatch.setattr(panel, 'stop_analysis', stop)
    monkeypatch.setattr(panel, '_start_analysis', lambda: calls.append('start'))
    try:
        panel._open_bunch()
        assert calls == ['stop', 'dialog'] + (['start'] if running and not paused else [])
        assert panel._live_paused == paused and panel._chip_process is receiver
    finally:
        panel._chip_process = None
        monkeypatch.setattr(panel, 'stop_analysis', lambda: None)
        panel.close()
        app.processEvents()


def test_cross_segment_strip_rejects_stale_line_and_keeps_cache_gap(synthetic_bunch):
    from realtime.bunch_dialog import group_strip
    from realtime.line_sampling import LineGeometry, LineStrip, line_config_digest, save_strip
    root, item, _events = synthetic_bunch
    context = load_context(root, item)
    directory = root / 'finish_problems/strips/camera_01'
    directory.mkdir(parents=True)
    for segment in context.segments:
        line = context.lines[segment.segment.segment_id]
        geometry = LineGeometry.from_config(line, 160, 120, samples=10)
        strip = LineStrip(np.zeros((10, 80, 3), np.uint8), np.arange(80)*50, geometry, str(segment.path), 20)
        save_strip(directory / (segment.path.stem+'.npz'), strip,
                   {'segment_id': segment.segment.segment_id, 'line_digest': line_config_digest(line)})
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
    counts = [mark(c.recorder_ms-T) for c in context.chips] + [mark(2800, '319')]
    store = BunchReviewStore(root / 'finish_problems')
    store.save(context, counts, 20, completed=True, fully_viewed=True)
    assert len(store.manual_entries([item], root)) == 1
    BatchDecisionStore(root / 'finish_problems').save(
        group_member_batch(context), [context.chips[0].event_id], 'hold', 'needs another camera')
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
        store.save(context, counts, 20, completed=True, fully_viewed=True)
    assert store.manual_entries([context.item], root) == []
