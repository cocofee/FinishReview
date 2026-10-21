"""One recorder per race folder.

Two FinishReview windows recording into one folder (a build switch while the
first still records, 古浪 2026-10-18) report every rider twice and leave one
session without a finish line. The lock is an open file handle, so the system
releases it when the owning process exits or crashes; it never goes stale.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt5.QtCore import QLockFile

from .stream_recorder import RecordingError

LOCK_FILENAME = "recording.lock"


class FolderInUseError(RecordingError):
    """Another live FinishReview records into the folder."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        super().__init__(
            f"另一个 FinishReview{owner}正在向这个赛事文件夹录像。两个程序同时录会把每位选手算两次："
            "请先在那个窗口停止录像或关闭它，再开始录像。"
        )


class RecordingFolderLock:
    def __init__(self) -> None:
        self._lock: QLockFile | None = None
        self._folder: Path | None = None

    @property
    def held(self) -> bool:
        return self._lock is not None

    def acquire(self, folder: Path) -> None:
        folder = Path(folder)
        if self._lock is not None:
            if self._folder == folder:
                return
            self.release()
        folder.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(folder / LOCK_FILENAME))
        # Only a live owner holds the lock; a dead one is detected, not timed out.
        lock.setStaleLockTime(0)
        if not lock.tryLock(0) and not _took_over_leftover(lock):
            found, pid, _host, _app = lock.getLockInfo()
            raise FolderInUseError(f"（进程 {pid}）" if found and pid else "")
        self._lock, self._folder = lock, folder

    def release(self) -> None:
        lock, self._lock, self._folder = self._lock, None, None
        if lock is not None:
            lock.unlock()


def _took_over_leftover(lock: QLockFile) -> bool:
    """Take the lock a crashed owner left behind under a reused process id.

    The file names a PID, and Windows hands PIDs out again, sometimes to the
    restarted FinishReview itself; Qt then sees a live owner and would refuse
    to record forever.  Windows cannot delete a file a live process holds
    open, so a file that can be removed has no owner left.
    """

    return os.name == "nt" and lock.removeStaleLockFile() and lock.tryLock(0)
