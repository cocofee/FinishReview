"""Background playback-assist scheduling for the review workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal

from .thread_lifecycle import retire_qthread, track_qthread
from .time_domain import MediaPositionMs, MediaWindow
from .video_activity import ActivityTimelineWidget, VideoActivityWorker


class PlaybackCoordinator(QObject):
    """Keep advisory decoding behind operator-controlled playback.

    The primary player never depends on activity analysis. Activity work is
    delayed, paused while the operator manipulates video, cancelled when the
    media window changes, and cached independently of the review dialog.
    """

    filmstrip_update_requested = pyqtSignal()

    def __init__(
        self,
        activity_timeline: ActivityTimelineWidget,
        parent: QObject | None = None,
        *,
        activity_worker_factory: Callable[..., VideoActivityWorker] = VideoActivityWorker,
        activity_start_delay_ms: int = 1_200,
        activity_cache_limit: int = 3,
        filmstrip_update_delay_ms: int = 330,
    ) -> None:
        super().__init__(parent)
        self.activity_timeline = activity_timeline
        self._activity_worker_factory = activity_worker_factory
        self._activity_cache_limit = max(1, int(activity_cache_limit))
        self._activity_worker: VideoActivityWorker | None = None
        self._activity_context: tuple[Path, MediaWindow] | None = None
        self._activity_points: list[tuple[int, float]] = []
        self._activity_progress = 0
        self._activity_cache: dict[
            tuple[Path, MediaWindow], tuple[tuple[int, float], ...]
        ] = {}
        self._activity_start_timer = QTimer(self)
        self._activity_start_timer.setSingleShot(True)
        self._activity_start_timer.setInterval(max(0, int(activity_start_delay_ms)))
        self._activity_start_timer.timeout.connect(self._start_activity_analysis)
        self._filmstrip_update_timer = QTimer(self)
        self._filmstrip_update_timer.setSingleShot(True)
        self._filmstrip_update_timer.setInterval(
            max(0, int(filmstrip_update_delay_ms))
        )
        self._filmstrip_update_timer.timeout.connect(
            self.filmstrip_update_requested.emit
        )

    @property
    def activity_context(self) -> tuple[Path, MediaWindow] | None:
        return self._activity_context

    @property
    def activity_progress(self) -> int:
        return self._activity_progress

    def request_filmstrip_update(self, *, deferred: bool) -> None:
        """Schedule thumbnails after the primary player gets decoder priority."""

        self._filmstrip_update_timer.stop()
        if deferred:
            self._filmstrip_update_timer.start()
        else:
            self.filmstrip_update_requested.emit()

    def cancel_filmstrip_update(self) -> None:
        self._filmstrip_update_timer.stop()

    def schedule_activity(
        self,
        video_path: Path,
        start_ms: MediaPositionMs | int,
        end_ms: MediaPositionMs | int,
    ) -> None:
        window = MediaWindow.from_milliseconds(start_ms, end_ms)
        context = (Path(video_path), window)
        if context == self._activity_context:
            return
        self.stop_activity()
        self._activity_context = context
        self._activity_points = []
        self._activity_progress = 0
        timeline = self.activity_timeline
        timeline.set_range(int(window.start), int(window.end))
        timeline.set_analysis_progress(0)
        timeline.set_analysis_state("等待分析")
        cached = self._activity_cache.get(context)
        if cached is not None:
            timeline.append_points(cached)
            timeline.set_analysis_progress(100)
            timeline.set_analysis_state("分析完成")
            timeline.show()
            return
        timeline.hide()
        self._activity_start_timer.start()

    def clear_activity(self) -> None:
        self.stop_activity()
        self._activity_context = None
        self._activity_points = []
        self._activity_progress = 0

    def stop_activity(self) -> None:
        self._activity_start_timer.stop()
        worker = self._activity_worker
        self._activity_worker = None
        if worker is None:
            return
        worker.request_stop()
        for signal in (
            worker.points_ready,
            worker.progress_ready,
            worker.completed,
            worker.finished,
        ):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        if worker.isRunning():
            retire_qthread(worker)
        else:
            worker.deleteLater()

    def set_operator_busy(self, busy: bool) -> None:
        worker = self._activity_worker
        if worker is None:
            return
        worker.set_paused(bool(busy))
        self.activity_timeline.set_analysis_state(
            "暂停分析（正在操作视频）"
            if busy
            else f"正在分析 {self._activity_progress}%"
        )

    def shutdown(self) -> None:
        self.cancel_filmstrip_update()
        self.clear_activity()

    def _start_activity_analysis(self) -> None:
        context = self._activity_context
        if context is None or self._activity_worker is not None:
            return
        video_path, window = context
        worker = self._activity_worker_factory(
            video_path,
            int(window.start),
            int(window.end),
            self,
        )
        worker.points_ready.connect(self._on_activity_points)
        worker.progress_ready.connect(self._on_activity_progress)
        worker.completed.connect(self._on_activity_completed)
        worker.finished.connect(self._on_activity_worker_finished)
        self._activity_worker = worker
        track_qthread(worker)
        self.activity_timeline.set_analysis_state("正在分析 0%")
        worker.start(QThread.LowestPriority)

    def _on_activity_progress(self, progress: int) -> None:
        if self.sender() is not self._activity_worker:
            return
        self._activity_progress = max(0, min(100, int(progress)))
        self.activity_timeline.set_analysis_progress(self._activity_progress)
        self.activity_timeline.set_analysis_state(
            f"正在分析 {self._activity_progress}%"
        )

    def _on_activity_points(self, points) -> None:
        if self.sender() is not self._activity_worker:
            return
        values = tuple((int(position), float(score)) for position, score in points)
        self._activity_points.extend(values)
        self.activity_timeline.append_points(values)
        self.activity_timeline.show()

    def _on_activity_completed(self) -> None:
        context = self._activity_context
        if self.sender() is not self._activity_worker or context is None:
            return
        self._activity_cache[context] = tuple(self._activity_points)
        self._activity_progress = 100
        self.activity_timeline.set_analysis_progress(100)
        self.activity_timeline.set_analysis_state("分析完成")
        while len(self._activity_cache) > self._activity_cache_limit:
            self._activity_cache.pop(next(iter(self._activity_cache)))

    def _on_activity_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._activity_worker:
            self._activity_worker = None
            worker.deleteLater()
