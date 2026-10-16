"""Member-level multi-chip review; selection never implies an entire batch."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                            QTableWidget, QTableWidgetItem, QVBoxLayout)

from .batch_review import BatchDecisionStore, member_binding
from .chip_history import format_chip_time

# What the standing reason claims about the frame, per member decision; the
# operator confirms it with a second press and can edit it before that.
BATCH_ACTION_REASONS = {
    "delete": "同一车两块芯片，同一帧压线，建议作废多余记录",
    "keep": "原帧两车分别过线，两条芯片记录各自有效",
    "hold": "原帧看不清，留待现场确认",
    "reopen": "上游记录已变化，重新核对后再判",
}


class BatchReviewDialog(QDialog):
    def __init__(self, directory, batch, current, events, jump, parent=None):
        super().__init__(parent)
        self.batch, self.current, self.events = batch, current, events
        self.store = BatchDecisionStore(directory)
        self.saved = False
        self.setWindowTitle("多芯片批次 · 按记录处理")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        self.description = QLabel(batch.detail + "\n勾选具体记录再处理；未勾选及后来加入的成员不继承决定。确认有效不代表人数已核清。")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        from .problem_resolutions import ProblemResolutionStore
        legacy = ProblemResolutionStore(self.store.path.parent / "problem_resolutions.jsonl")
        old = [legacy.get(i["item_id"]) for i in batch.evidence.get("batch_sources", [])]
        old = [r for r in old if r is not None]
        if old:
            note = QLabel("旧整项决定保留供追溯，未自动应用于成员：" + "；".join(
                f"{r.label} {r.target_bib or r.bib}：{r.reason}" for r in old))
            note.setWordWrap(True)
            layout.addWidget(note)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["选择", "号码", "芯片", "通过时间", "事件ID / 修订", "状态", "名单绑定"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("本次选中成员的处理原因（必填）")
        layout.addWidget(self.reason)
        row = QHBoxLayout()
        for action, text in (("delete", "选中：建议作废"), ("keep", "选中：确认有效"),
                             ("hold", "选中：暂存"), ("reopen", "选中：重新处理")):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, a=action: self._save(a))
            row.addWidget(button)
        layout.addLayout(row)
        controls = QHBoxLayout()
        original = QPushButton("返回主界面查看原帧（未提交勾选不保存）")
        original.setEnabled(bool(batch.evidence.get("frame")))
        def open_frame():
            jump(dict(self.batch.evidence["frame"]))
            self.accept()
        original.clicked.connect(open_frame)
        controls.addWidget(original)
        refresh = QPushButton("刷新成员（清除勾选）")
        refresh.clicked.connect(self._refresh)
        controls.addWidget(refresh)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        controls.addWidget(close)
        layout.addLayout(controls)
        self.notice = QLabel("建议只写本地交接日志，不修改正式成绩；须收到对应记录明确作废后才关闭该成员。")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self._render()

    def _render(self):
        self.members = list(self.batch.evidence.get("batch_members", []))
        self.table.setRowCount(len(self.members))
        events = tuple(self.events())
        for row, member in enumerate(self.members):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            values = [member["bib"], member.get("chip_id", ""), format_chip_time(member["chip_ms"]),
                      f"记录 {member['event_id'].rsplit('-passage-', 1)[-1]} · r{member['revision']} / {member['event_id']}",
                      self.store.state(self.batch, member, events),
                      "、".join(member.get("legal_chip_ids", [])) or "未知"]
            for column, value in enumerate(values, 1):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(4, 240)

    def _refresh(self):
        batch = self.current(self.batch.item_id)
        if batch is None:
            self.notice.setText("批次已更新或不再可用；请返回问题清单核对关联项。")
            return
        self.batch = batch
        self.store.load()
        self.description.setText(batch.detail + "\n名单允许多芯片不等于独立通过记录可合并；请核查上游语义，不自动删除。")
        self._render()

    def _save(self, action):
        selected = [m for row, m in enumerate(self.members) if self.table.item(row, 0).checkState() == Qt.Checked]
        if selected and not self.reason.text().strip() and action in BATCH_ACTION_REASONS:
            # Same bargain as the problem list: look at the frame, confirm the
            # standing reason with a second press, rewrite it if you saw otherwise.
            preset = BATCH_ACTION_REASONS[action]
            self.reason.setText(preset)
            self.notice.setText(f"依据已填「{preset}」；再点一次同一个按钮就是确认，改成你看到的情形也可以。")
            return
        try:
            current = self.current(self.batch.item_id)
            if current is None:
                raise ValueError("批次已变化，请刷新")
            now = {m["event_id"]: m for m in current.evidence.get("batch_members", [])}
            if any(m["event_id"] not in now or member_binding(self.batch, m) != member_binding(current, now[m["event_id"]])
                   for m in selected):
                raise ValueError("所选成员或视频证据已变化，请刷新并重新核对")
            self.store.save(current, [m["event_id"] for m in selected], action, self.reason.text(), tuple(self.events()))
            self.batch = current
            self.saved = True
            self._render()
            self.notice.setText(f"已记录 {len(selected)} 个成员；其他成员保持原状态。")
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.setText(f"未保存：{error}")
