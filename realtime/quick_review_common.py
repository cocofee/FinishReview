"""Constants, texts and row/wave records shared by the quick review panel."""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt5.QtCore import Qt

from .problem_items import ProblemItem


# Chips closer together than this are one wave, and the operator counts the
# people of a wave against its chips.  On real races (古浪, 定边, 可克达拉)
# the gaps cluster below 2 s and above 3 s, so the cut falls in the quiet.
WAVE_GAP_MS = 2000
KEEP = "keep"  # landing: the picture already shows this row's wave; leave it
# Restore the final field adjustment from 58f2f99: no padding on chip waves.
# Matching tolerance is independent of playback and of the operator's viewed span.
WAVE_VIEW_MARGIN_MS = 0
# Video-only passages keep a small browsing lead/tail; this is not a chip
# matching tolerance and does not make a split recording continuous by itself.
PASSAGE_VIEW_MARGIN_MS = 150
# Crossings without a chip read for 可能多人 per pass, so the table never waits on the strip.
EARLIER_BATCH = 8
# A wave is swept by dragging the picture; a rider stays in view about a second
# either side of the line, so one decoded frame covers 250 ms either side.
WAVE_VIEW_REACH_MS = 250
# One passage the detector broke into pieces (古浪, 10-28: 7 pieces in 1.1 s)
# is one row: pieces without a chip within a second of the first, never
# chained on from a later piece, so two riders a few seconds apart stay apart.
FRAGMENT_REACH_MS = 1000
# 3 on a row not yet seen whole plays the rest frame by frame (only single steps
# go on into the next recording file) and excludes it once seen; a refused 3
# looked like a dead key (10-28).  Stops if no frame comes.
REVIEW_STEP_FRAMES = 1
REVIEW_STEP_DELAY_MS = 15
REVIEW_STALL_MS = 3000
# Shift+3 excludes this row and the undecided pictures after it, up to a chip or
# a gap this long: someone walking about at the line (特奥测试, 10-28).
EXCLUSION_RUN_GAP_MS = 10_000
# 点一个人，录像自己走到下一位 (11-11): after a click on a wave's picture the
# recording plays on to the next chip nobody is counted for, or the next person
# the analysis saw, and stops there.  Someone due within RUN_NEAR_MS is on the
# picture already (side by side, or the clock a little off): it stays.  With
# nobody left it plays RUN_TAIL_MS past the last chip, so a rider without a chip
# at the back of the wave is seen, and a wave that then counts even opens the next.
RUN_NEAR_MS = 200
RUN_STOP_LEAD_MS = 80
RUN_TAIL_MS = 1000
RUN_STALE_MS = 30_000
# Space on a wave goes the same way (11-12, 古浪: in a big bunch Space played
# everyone past before one could be clicked).  With the next chip due right
# here but nobody at the line yet, Space moves RUN_STEP_MS on and stops again.
RUN_STEP_MS = 300
# Space through a stretch with nobody due (11-14: wheel and normal play are too
# slow there, a jump could miss someone): the recording plays this fast, every
# frame shown, down to RUN_SLOW_MS before where it stops, and it also stops
# where the strip shows something coming onto the line (line_arrivals).  Only
# where the strip covers the stretch, and only when it is at least this long.
RUN_FAST_SPEED = 4.0
RUN_FAST_MIN_MS = 1000
RUN_SLOW_MS = 400
# Between waves Space sweeps the same way, this far at a time (under
# RUN_STALE_MS), to the next chip or to whatever comes onto the line first.
SWEEP_REACH_MS = 25_000
# 11-15 古浪 12 号 (no chip, 0.27 s behind 57): after 57 Space ran on fast and
# the one right behind went past unseen ("大集团这个有点危险").  For this long
# after any chip, or anyone counted, Space only moves RUN_STEP_MS at a time, at
# normal speed, however the operator got there (a click, a row, the strip).
CLOSE_BEHIND_MS = 1000
# Space there plays on at normal speed and stops where the strip shows someone
# new come onto the line (line_next_entry); a lone rider's line stays empty and
# Space goes on as before, one press (11-15: "一个人的话还要点好几次").  The
# first SAME_BODY_MS after the one counted are their own back wheel.  Only
# where the strip cannot tell does Space step RUN_STEP_MS at a time.
SAME_BODY_MS = 150
def row_moment(row):
    """A chip row's chip time (not its rider's click); a row without a chip, its own time."""
    return int(row.event.timeline_timestamp_ms) if row.event is not None else row.time_ms


# Within this of the frame shown a clicked person is the one on screen (one
# frame at 25 fps is 40 ms): Space goes on to the next one, not to them.
SAME_FRAME_MS = 20
# A chip of a confirmed wave that nobody on the picture was counted for
# (older confirmations only; a wave now shows 少 N until someone is counted).
WAVE_NO_PERSON = "wave_no_person"
# A chip the operator marked as a second chip (双芯片, right click on its row):
# the rider counted for another bib carries it too, kept as carrier_bib.
# Exported to 建议删除清单; the wave counts without it.
EXTRA_CHIP = "extra_chip"
# Local operator exclusion, never a change to official events or an export.
LOCAL_CHIP_EXCLUSION = "local_chip_exclusion"
# The number the operator read on the frame of a rider without a chip, kept
# beside their 疑似漏读 row and exported to 补录清单.
RECORD_BIB = "record_bib"
# A bib read for a rider without a chip while that bib has a chip this close:
# it is the same passage (古浪 6 号 was a late import), not a miss.
BIB_CHIP_REACH_MS = 5000
# A detected crossing this close to someone counted on a wave's picture, on the
# same camera, is most likely that person (古浪 10-28: one rider showed as a
# 疑似漏读 row and a 待定 card at 11:28:57.101).  It stays a row, shown as
# 已点附近: it may be someone else nobody counted (review 10-29, 疑点保留).
COUNTED_REACH_MS = 500
# Display-only buckets of uncertain line motion, anchored on the first change.
# Never a person count or a chained group; source files and manual rulings stay separate.
UNVERIFIED_SPAN_MS = 2_000
NEAR_COUNTED_TEXT = ("已点附近：这一刻 0.5 秒内已点过人，多半就是那个人。看一眼：是同一个人按 1 排除；"
                     "是没点到的人就在画面上补点，或按 3 补录号码。")
# People counted up to about three frames before the frame shown are drawn as a
# faded ring, so a rider still on the line is not counted a second time.  Only
# earlier ones and only this close: further off the rider has moved on and the
# ring sits on empty road (古浪).
GHOST_REACH_MS = 150
# The left hand's keys (10-29): each does on the selected row what its right
# click offers under these names; 4 is the same undo as Backspace.
ROW_KEYS = {Qt.Key_1: ("这人不是运动员", "不是运动员", "非运动员（本机排除）"),
            Qt.Key_2: ("双芯片：这条芯片是谁带的…", "改携带人…"),
            Qt.Key_3: ("这人没芯片：补录号码…", "补录号码…", "改号码…")}
KEY_NAMES = {Qt.Key_1: "1 不是运动员", Qt.Key_2: "2 双芯片", Qt.Key_3: "3 补录号码"}
NUMBER_KEYS = (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5)
AUTO_PICTURE_REASON = "自动对上：按过线顺序对应芯片，操作员看过往下走即接受"
AUTO_TEXT = "自动对应：灰蓝小标签仅表示系统对应，不是人工确认；看一眼对就按空格往下走，号码不对按 4 撤回。"
# 11-15: people are counted one by one and every click is saved; Enter no longer
# confirms anything.  An 自动对上 wave is accepted by going on from it, Space
# (or Enter); it is still saved as the system's pairing, never as a manual count.
AUTO_GO_ON = "自动对上 · 未人工确认 · 看一眼，对就按空格往下走，不对按 4 撤回"

# What the wave's status line alone told the operator (see _sync_arrival_confirmation).
WAVE_ALERT_WORDS = ("分不清", "没看", "芯片有变化", "不算人工确认", "按空格往下走", "可能多人")


@dataclass
class ArrivalRow:
    key: str
    item: ProblemItem
    bib: str = ""
    event: object = None
    time_ms: int | None = None
    recorder_ms: int | None = None
    reference: bool = True
    decision: dict | None = None
    stale: bool = False
    note: str = ""
    mark: dict | None = None  # a 多出 row: someone counted beyond the wave's chips
    fragments: tuple = ()  # later pieces of the same passage shown on this row (rows of their own before)
    entered: dict | None = None  # the bib read for a 疑似漏读 row (see RECORD_BIB)
    carrier: str = ""  # a 双芯片 row: the bib of the rider who carries this chip too


@dataclass
class WavePlan:
    """How the people counted on a wave's picture line up with its chips."""

    item: ProblemItem
    chips: list
    marks: list
    labels: dict = field(default_factory=dict)
    assigned: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    no_chip: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    extra_chips: list = field(default_factory=list)  # chips marked 多余芯片: nobody is counted for them
    people: int = 0
    split: int = 0  # people marked 无芯片 and moved onto a 疑似漏读 row of their own
    near: dict = field(default_factory=dict)  # a 多 n's index -> the clicks it may as well be (times too close)

    @property
    def matches(self):
        """Every chip has its person and nobody counted is left over."""
        return not self.extra and not self.unmatched
