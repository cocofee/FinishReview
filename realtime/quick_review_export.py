"""Arrival export of the quick review panel."""

from __future__ import annotations

import csv
import json

from .chip_history import format_chip_time
from .crossing_time import item_crossing_time, workspace_clocks
from .problem_resolutions import _frame_text, _passage_number, export_lists


class QuickReviewExportMixin:
    """Build and write the arrival exports."""

    def _arrival_exports(self):
        """补录 rows (a person without a chip) and 建议删除 rows (多余芯片) the arrival list settled."""
        clocks = workspace_clocks(self.workspace_dir) if self.workspace_dir else None
        sources = {"aligned": "校时换算（视频原帧）", "video": "未校时：录像原时刻"}
        manual, deletions, blank = [], [], 0
        for row in self._arrival_rows:
            state = self._row_state(row)
            if state in ("补录", "疑似漏读") and row.event is None:
                frame = (row.entered or {}).get("frame") or row.item.evidence.get("frame") or {}
                crossing = (item_crossing_time(row.item, self.workspace_dir, clocks=clocks)
                            if self.workspace_dir else None)
                blank += state == "疑似漏读"
                manual.append((row.bib if state == "补录" else "", format_chip_time(row.time_ms)
                               if row.time_ms is not None else "", _frame_text(frame), frame.get("camera_index", ""),
                               row.item.calibration_version,
                               "到达记录：画面有人无芯片" + ("" if state == "补录" else "；号码未填，请核对原帧"),
                               sources.get(crossing.source, "参考时间：请核对原帧") if crossing else "未换算：按原帧人工判断"))
            elif state == "多余芯片":
                entry = self._arrival_store.latest(self._arrival_scope, row.key) or {}
                frame = entry.get("frame") or {}
                event = row.event
                reason = (f"到达记录：{row.bib} 号芯片由 {row.carrier} 号携带（一人双芯片），"
                          f"{row.carrier} 号成绩不受影响" if row.carrier
                          else "到达记录：本波看完，人数比芯片少，这条芯片没有对应的人")
                deletions.append((row.bib, format_chip_time(event.timeline_timestamp_ms),
                                  _passage_number(event.event_id), "一人双芯片", reason, event.event_id,
                                  getattr(event, "revision", ""), "", _frame_text(frame),
                                  entry.get("calibration_version", ""), json.dumps(entry, ensure_ascii=False),
                                  row.carrier))
        return manual, deletions, blank

    def export(self):
        """The three lists for the timing operator, with what the arrival list settled in them too."""
        if self.store is None or self.problem_dir is None:
            return None
        try:
            self.refresh()
            manual, deletions, blank = self._arrival_exports()
            counts = export_lists(self.items, self.store, self.problem_dir / "exports",
                                  events=tuple(self._events_provider()),
                                  manual_extra=manual, deletions_extra=deletions)
        except (OSError, RuntimeError, ValueError) as error:
            self.hint.setText(f"导出失败：{error}")
            return None
        text = "已导出到 " + str(self.problem_dir / "exports") + "：" + "，".join(
            f"{name.removesuffix('.csv')} {count} 条" for name, count in counts.items())
        if blank:
            text += f"；其中 {blank} 条疑似漏读还没填号码"
        self.hint.setText(text)
        return counts

    def export_arrivals(self):
        if self.problem_dir is None:
            return None
        self.refresh()
        path = self.problem_dir / "exports" / "人工过线记录.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["顺序", "号码", "过线时间", "事件", "机位", "录像", "帧号", "校时版本"])
            for index, row in enumerate(row for row in self._arrival_rows if row.decision):
                frame = row.decision["frame"]
                writer.writerow([index + 1, row.bib, format_chip_time(row.time_ms), row.event.event_id,
                                 frame.get("camera_index", ""), frame.get("video_path", ""),
                                 frame.get("frame_index", ""),
                                 row.decision["calibration_version"]])
        self.hint.setText(f"已导出：{path}")
        return path
