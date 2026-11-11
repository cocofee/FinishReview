from realtime.chip_checks import ChipCheckContext, chip_check_items, lap_targets, next_recheck_ms
from realtime.chip_history import ChipRead
from realtime.problem_items import (
    KIND_BATCH_READS,
    KIND_BEFORE_GUN,
    KIND_DUPLICATE_MANUAL,
    KIND_DUPLICATE_READ,
    KIND_EARLY_READ,
    KIND_LAP_MISSING,
    KIND_LAP_OVER,
    KIND_LAP_REGRESS,
    KIND_LAP_REPEAT,
    KIND_LAP_TIME,
    KIND_NOT_ARRIVED,
    KIND_PAIRED_CHIPS,
    KIND_REPEATED_PASSAGE,
    KIND_SYNC,
    KIND_TEST_CHIP,
    KIND_UNKNOWN_BIB,
    STATUS_CLOSED_UPSTREAM,
    STATUS_INFO,
    STATUS_OPEN,
    ProblemItem,
)
from realtime.sync_check import UpstreamPassage, check_sync


GUN = 1_788_052_020_931  # 09:27:00.931 on 2026-08-30
MINUTE = 60_000


def _read(event, bib, chip_ms, *, group="女子公路公开组", active=True, manual=False, excluded="", lap=0, revision=1):
    return ChipRead(
        event_id=f"race-stage-1-passage-{event}", bib=bib, chip_id=f"c{bib}", group_id="4",
        group_name=group, athlete_name="", chip_ms=chip_ms, passage_time_ms=chip_ms % 86_400_000,
        lap=lap, received_at_ms=chip_ms + (600_000 if manual else -500), revision=revision,
        latest_revision=revision, active_now=active, manual_entry=manual, excluded=excluded,
        latest_bib=bib, latest_chip_ms=chip_ms,
    )


def _kinds(items):
    return sorted((item.kind, item.bibs[0] if item.bibs else "", item.status) for item in items)


def test_duplicate_manual_entries_and_repeated_passages():
    reads = [
        _read(247, "550", GUN + 96 * MINUTE, manual=True),
        _read(366, "550", GUN + 96 * MINUTE + 652, manual=True),
        _read(10, "780", GUN + 130 * MINUTE),
        _read(11, "780", GUN + 132 * MINUTE),
        _read(12, "100", GUN + 110 * MINUTE),
        _read(13, "100", GUN + 110 * MINUTE + 3_000, active=False),
    ]

    items = chip_check_items(reads)

    assert _kinds(items) == [
        (KIND_DUPLICATE_MANUAL, "550", STATUS_OPEN),
        (KIND_DUPLICATE_READ, "100", STATUS_CLOSED_UPSTREAM),
        (KIND_REPEATED_PASSAGE, "780", STATUS_OPEN),
    ]
    manual = next(item for item in items if item.kind == KIND_DUPLICATE_MANUAL)
    assert "0.7 秒内" in manual.detail


def test_test_spare_and_unlisted_chips():
    reads = [
        _read(1, "901", GUN - 50 * MINUTE, group="测试组", excluded="test_group"),
        _read(2, "443", GUN - 20 * MINUTE, group="备用组", excluded="spare_group", active=False),
        _read(3, "999", GUN + 100 * MINUTE),
        _read(4, "700", GUN + 101 * MINUTE),
    ]
    context = ChipCheckContext(roster={"700": "女子公路公开组", "701": "女子公路公开组"})

    items = chip_check_items(reads, context)

    kinds = _kinds(items)
    assert (KIND_TEST_CHIP, "901", STATUS_OPEN) in kinds
    assert (KIND_TEST_CHIP, "443", STATUS_CLOSED_UPSTREAM) in kinds
    assert (KIND_UNKNOWN_BIB, "999", STATUS_OPEN) in kinds
    not_arrived = next(item for item in items if item.kind == KIND_NOT_ARRIVED)
    assert not_arrived.status == STATUS_INFO and not_arrived.bibs == ("701",)


def test_early_reads_need_gun_times_and_close_when_voided():
    reads = [_read(3, "746", GUN + int(11.6 * MINUTE), active=False)]
    reads += [_read(100 + index, str(600 + index), GUN + (108 + index) * MINUTE) for index in range(8)]
    reads.append(_read(9, "808", GUN - 60 * MINUTE))

    without_guns = chip_check_items(reads)
    assert not any(item.kind in (KIND_EARLY_READ, KIND_BEFORE_GUN) for item in without_guns)

    context = ChipCheckContext(gun_starts={"女子公路公开组": GUN}, distance_km=61.0)
    items = chip_check_items(reads, context)

    early = next(item for item in items if item.kind == KIND_EARLY_READ)
    assert early.bibs == ("746",) and early.status == STATUS_CLOSED_UPSTREAM
    assert "11.6 分钟" in early.detail and "下限 61 分钟" in early.detail
    assert any(item.kind == KIND_BEFORE_GUN and item.bibs == ("808",) for item in items)
    assert not any(item.kind == KIND_EARLY_READ and item.bibs[0].startswith("60") for item in items)
    assert "各组发枪时间（过早读数检查）" in ChipCheckContext().missing_inputs()


def test_relative_early_rule_works_without_distance():
    reads = [_read(100 + index, str(600 + index), GUN + (100 + index) * MINUTE) for index in range(10)]
    reads.append(_read(1, "650", GUN + 30 * MINUTE))

    items = chip_check_items(reads, ChipCheckContext(gun_starts={"女子公路公开组": GUN}))

    assert [item.bibs for item in items if item.kind == KIND_EARLY_READ] == [("650",)]


def test_bunch_sprints_are_not_batch_reads_but_chip_boxes_are():
    sprint = [_read(index, str(100 + index), GUN + 100 * MINUTE + index * 110) for index in range(9)]
    assert not any(item.kind == KIND_BATCH_READS for item in chip_check_items(sprint))

    box = [_read(50 + index, str(200 + index), GUN + 200 * MINUTE + index * 20) for index in range(6)]
    with_test = [
        _read(60, "300", GUN + 300 * MINUTE),
        _read(61, "902", GUN + 300 * MINUTE + 300, group="测试组", excluded="test_group"),
        _read(62, "301", GUN + 300 * MINUTE + 600),
    ]

    items = chip_check_items(sprint + box + with_test)

    batches = [item for item in items if item.kind == KIND_BATCH_READS]
    assert len(batches) == 2
    assert len(batches[0].bibs) == 6


def test_multi_lap_anomalies():
    lap = 20 * MINUTE
    reads = []
    for rider in range(6):
        for number in range(1, 4):
            reads.append(_read(rider * 10 + number, str(rider), GUN + number * lap + rider * 1_000, lap=number))
    reads += [
        _read(90, "7", GUN + lap, lap=1),
        _read(91, "7", GUN + 2 * lap, lap=1),          # lap repeated
        _read(92, "8", GUN + lap, lap=2),
        _read(93, "8", GUN + 2 * lap, lap=1),          # lap regressed
        _read(94, "9", GUN + lap, lap=1),
        _read(95, "9", GUN + lap + 4 * MINUTE, lap=2),  # impossible lap time
        _read(96, "10", GUN + lap, lap=1),
        _read(97, "10", GUN + 2 * lap, lap=2),          # stops after lap 2
    ]

    items = chip_check_items(reads, ChipCheckContext(statuses={"11": "DNF"}))
    kinds = {(item.kind, item.bibs[0]) for item in items}

    assert (KIND_LAP_REPEAT, "7") in kinds
    assert (KIND_LAP_REGRESS, "8") in kinds
    assert (KIND_LAP_TIME, "9") in kinds
    assert (KIND_LAP_MISSING, "10") in kinds
    assert not any(kind == KIND_REPEATED_PASSAGE for kind, _bib in kinds)


def test_sync_issues_become_items_and_items_round_trip():
    upstream = [UpstreamPassage(504, "319", 39_490_466, True, True)]
    issues = check_sync(upstream, [])

    items = chip_check_items([], ChipCheckContext(sync_issues=issues), race_date_ms=1_788_019_200_000)

    item, = items
    assert item.kind == KIND_SYNC and item.bibs == ("319",)
    assert item.chip_time_ms == 1_788_019_200_000 + 39_490_466
    assert ProblemItem.from_payload(item.to_payload()) == item
    assert item.title == "两边记录不一致"


LAP = 10 * MINUTE


def _laps(bib, laps, *, offset_ms=0, base=0):
    return [_read(base + number, bib, GUN + number * LAP + offset_ms, lap=number) for number in range(1, laps + 1)]


def _five_lap_field(extra_bib="7"):
    reads = []
    for rider in range(1, 11):
        reads += _laps(str(rider), 5, offset_ms=rider * 20_000, base=rider * 100)
    # One extra passage after finishing: lap 6 for a single bib.
    reads.append(_read(9_999, extra_bib, GUN + 5 * LAP + 7 * 20_000 + 4 * MINUTE, lap=6))
    return reads


def test_one_extra_lap_is_lap_over_not_everyone_short():
    items = chip_check_items(_five_lap_field())

    assert not any(item.kind == KIND_LAP_MISSING for item in items)
    over, = [item for item in items if item.kind == KIND_LAP_OVER]
    assert over.bibs == ("7",) and over.event_ids == ("race-stage-1-passage-9999",)
    assert over.status == STATUS_OPEN and "同组多数人是 5 圈" in over.detail
    assert lap_targets(_five_lap_field())["女子公路公开组"].over_bibs == ("7",)


def test_live_lap_judging_waits_until_the_group_settles():
    reads = _five_lap_field()
    last = max(read.chip_ms for read in reads)
    early = ChipCheckContext(race_finished=False, now_ms=last + LAP)
    late = ChipCheckContext(race_finished=False, now_ms=last + 2 * LAP)

    assert not any(item.kind in (KIND_LAP_OVER, KIND_LAP_MISSING) for item in chip_check_items(reads, early))
    assert next_recheck_ms(reads, early) == last + int(1.5 * LAP)
    assert any(item.kind == KIND_LAP_OVER for item in chip_check_items(reads, late))
    assert next_recheck_ms(reads, late) is None
    # Without a clock a live check never settles.
    assert not any(item.kind == KIND_LAP_OVER
                   for item in chip_check_items(reads, ChipCheckContext(race_finished=False)))


def test_long_lap_hints_a_missed_read():
    reads = []
    for rider in range(1, 7):
        reads += _laps(str(rider), 4, offset_ms=rider * 1_000, base=rider * 100)
    reads = [read for read in reads if not (read.bib == "3" and read.lap == 2)]
    reads = [_read(int(read.event_id.rsplit("-", 1)[1]), read.bib, read.chip_ms,
                   lap=read.lap - 1 if read.bib == "3" and read.lap > 2 else read.lap) for read in reads]

    lap_time = [item for item in chip_check_items(reads) if item.kind == KIND_LAP_TIME and item.bibs == ("3",)]

    assert lap_time and "可能中间漏读一次" in lap_time[0].detail


def test_paired_chips_open_split_and_voided():
    together = _laps("5", 5, base=100) + _laps("8", 5, offset_ms=300, base=200) + _laps("9", 5, offset_ms=90_000, base=300)

    pair, = [item for item in chip_check_items(together) if item.kind == KIND_PAIRED_CHIPS]
    assert pair.bibs == ("5", "8") and pair.status == STATUS_OPEN and len(pair.event_ids) == 6

    split = [read if not (read.bib == "8" and read.lap >= 4) else
             _read(int(read.event_id.rsplit("-", 1)[1]), "8", read.chip_ms + 5_000, lap=read.lap)
             for read in together]
    pair, = [item for item in chip_check_items(split) if item.kind == KIND_PAIRED_CHIPS]
    assert pair.status == STATUS_INFO and "已经分开" in pair.status_note

    voided = [read if not (read.bib == "8" and read.lap == 1) else
              _read(201, "8", read.chip_ms, lap=1, active=False) for read in together]
    pair, = [item for item in chip_check_items(voided) if item.kind == KIND_PAIRED_CHIPS]
    assert pair.status == STATUS_CLOSED_UPSTREAM

    # Two laps together is pacing, not a pair.
    short = _laps("5", 2, base=100) + _laps("8", 2, offset_ms=300, base=200)
    assert not any(item.kind == KIND_PAIRED_CHIPS for item in chip_check_items(short))


def test_reidentified_read_is_not_a_pair_with_itself():
    reads = _laps("5", 4, base=100)
    renamed = [_read(100 + number, "6", read.chip_ms, lap=number, revision=2)
               for number, read in enumerate(reads, start=1)]

    assert not any(item.kind == KIND_PAIRED_CHIPS for item in chip_check_items(reads + renamed))
