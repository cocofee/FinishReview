"""Shared status encoding for finish-strip and video correspondence lines.

The number identifies the binding; colour and stroke describe its source,
never an official result.  Text keeps the meaning available without colour.
"""

AUTO_COLOR = "#597895"
MANUAL_COLOR = "#16734b"
PENDING_COLOR = "#a34214"
CURSOR_COLOR = "#b400cc"


def marker_style(*, automatic=False, manual=False):
    """Return (colour, dashed) without changing any judgement state."""
    if automatic:
        return AUTO_COLOR, True
    if manual:
        return MANUAL_COLOR, False
    return PENDING_COLOR, True


LEGEND = ("灰蓝虚线＝自动对应（非人工确认） · 绿实线＝人工处理 · "
          "橙虚线＝待核 · 紫线＝播放位置")
