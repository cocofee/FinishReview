"""Display the counted riders in crossing order without changing their saved indices."""

from itertools import groupby

from .bunch_review import is_excluded


def ordered_mark_indices(marks: list) -> list[int]:
    return sorted(range(len(marks)), key=lambda index: (marks[index]["recorder_ms"], index))


def mark_order_labels(marks: list) -> dict[int, str]:
    """Simultaneous riders share a number; excluded people occupy no rider position."""

    labels = {index: "排除" for index, mark in enumerate(marks) if is_excluded(mark)}
    riders = [index for index in ordered_mark_indices(marks) if not is_excluded(marks[index])]
    position = 1
    for _stamp, group in groupby(riders, key=lambda index: marks[index]["recorder_ms"]):
        simultaneous = list(group)
        label = str(position) + (" 同时" if len(simultaneous) > 1 else "")
        for index in simultaneous:
            labels[index] = label
        position += len(simultaneous)
    return labels
