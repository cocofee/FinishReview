import subprocess
import sys

import pytest

from realtime.recording_lock import LOCK_FILENAME, FolderInUseError, RecordingFolderLock

HOLDER = """
import sys, time
from PyQt5.QtCore import QLockFile
lock = QLockFile(sys.argv[1])
lock.setStaleLockTime(0)
print(lock.tryLock(0), flush=True)
time.sleep(120)
"""


def test_a_second_recorder_is_refused_until_the_first_lets_go(tmp_path):
    first, second = RecordingFolderLock(), RecordingFolderLock()
    first.acquire(tmp_path)
    first.acquire(tmp_path)
    assert (tmp_path / LOCK_FILENAME).is_file()

    with pytest.raises(FolderInUseError, match="另一个 FinishReview"):
        second.acquire(tmp_path)
    assert not second.held

    first.release()
    assert not (tmp_path / LOCK_FILENAME).exists()
    second.acquire(tmp_path)
    assert second.held
    second.release()


def test_another_process_holds_the_folder_only_while_it_lives(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path / LOCK_FILENAME)],
                              stdout=subprocess.PIPE, text=True)
    lock = RecordingFolderLock()
    try:
        assert holder.stdout.readline().strip() == "True"
        with pytest.raises(FolderInUseError) as refused:
            lock.acquire(tmp_path)
        assert f"进程 {holder.pid}" in refused.value.owner
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
    # A killed owner leaves its file behind; that must not block the next race.
    lock.acquire(tmp_path)
    assert lock.held
    lock.release()


def test_a_leftover_naming_a_reused_process_id_does_not_block_recording(tmp_path):
    # The crashed owner's PID now belongs to a live FinishReview (here: ours).
    lock = RecordingFolderLock()
    lock.acquire(tmp_path)
    leftover = (tmp_path / LOCK_FILENAME).read_bytes()
    lock.release()
    (tmp_path / LOCK_FILENAME).write_bytes(leftover)

    lock.acquire(tmp_path)
    assert lock.held
    lock.release()
    assert not (tmp_path / LOCK_FILENAME).exists()


def test_recording_into_another_folder_moves_the_lock(tmp_path):
    first_folder, second_folder = tmp_path / "第一场", tmp_path / "第二场"
    lock, other = RecordingFolderLock(), RecordingFolderLock()
    lock.acquire(first_folder)
    lock.acquire(second_folder)
    assert not (first_folder / LOCK_FILENAME).exists()
    other.acquire(first_folder)
    with pytest.raises(FolderInUseError):
        other.acquire(second_folder)
    other.release()
    lock.release()
