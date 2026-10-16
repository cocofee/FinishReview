"""One display row per observed rider or still-unmatched chip record."""

from .bunch_review import align_counts, is_excluded
from .crossing_order import mark_order_labels, ordered_mark_indices


def crossing_rows(marks, chips, *, time_reference=True):
    """Keep saved mark indices intact, even when an earlier rider is counted later.

    A time match is a reference for the operator, never an identified rider.
    Chip-only rows have no finishing position until a person is observed.
    """

    if time_reference:
        pairs = align_counts(chips, marks)["pairs"]
    else:
        pairs, used = {}, set()
        for index in ordered_mark_indices(marks):
            mark = marks[index]
            if is_excluded(mark) or not mark.get("bib"):
                continue
            matches = [q for q, chip in enumerate(chips) if chip.bib == mark["bib"] and q not in used]
            if matches:
                pairs[index] = matches[0]
                used.add(matches[0])
    labels = mark_order_labels(marks)
    rows = [dict(mark_index=index, chip_index=pairs.get(index), order=labels[index],
                 recorder_ms=marks[index]["recorder_ms"],
                 reference=index in pairs and not marks[index].get("bib"))
            for index in ordered_mark_indices(marks)]
    used = set(pairs.values())
    rows.extend(dict(mark_index=None, chip_index=q, order="待看", recorder_ms=chip.recorder_ms, reference=False)
                for q, chip in enumerate(chips) if q not in used)
    if time_reference:
        rows.sort(key=lambda row: row["recorder_ms"])
    return rows
