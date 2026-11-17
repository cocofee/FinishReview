"""Replace a file so readers see either the old or the complete new content.

Every save goes to a temporary file next to the target and is then moved
over it with ``os.replace``.  The temporary name carries the process and
thread id, so two writers of the same file (the console and an analysis
process, or two threads) never share one temporary file.  When anything
fails the temporary file is removed and the target keeps its old content.

``durable=True`` also forces the bytes to disk before the move, so a power
cut cannot leave an empty or half-written file behind.  Use it for operator
work, settings and exports; caches that are rebuilt anyway may skip it.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from typing import IO, Iterator

# Windows briefly refuses the move while an indexer or virus scanner holds
# the target open.  Retry for at most ~0.1 s; a file held open for longer
# (for example a CSV opened in Excel) still fails as before.
_REPLACE_ATTEMPTS = 3
_REPLACE_RETRY_S = 0.05


def temporary_path_for(path: str | Path) -> Path:
    target = Path(path)
    return target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")


def _replace(source: Path, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_S)


@contextmanager
def atomic_open(
    path: str | Path,
    mode: str = "w",
    *,
    encoding: str | None = None,
    newline: str | None = None,
    durable: bool = True,
) -> Iterator[IO]:
    """Open a temporary file for writing; move it over ``path`` on success."""

    if mode not in ("w", "wb"):
        raise ValueError(f"atomic_open only writes whole files, not mode {mode!r}")
    if mode == "w" and encoding is None:
        raise ValueError("atomic_open needs an explicit encoding for text")
    target = Path(path)
    temporary = temporary_path_for(target)
    try:
        with open(temporary, mode, encoding=encoding, newline=newline) as handle:
            yield handle
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        _replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    durable: bool = True,
) -> None:
    """Like ``Path.write_text`` but all-or-nothing (same newline handling)."""

    with atomic_open(path, "w", encoding=encoding, newline=newline, durable=durable) as handle:
        handle.write(text)


def atomic_write_bytes(path: str | Path, data: bytes, *, durable: bool = True) -> None:
    """Like ``Path.write_bytes`` but all-or-nothing."""

    with atomic_open(path, "wb", durable=durable) as handle:
        handle.write(data)


__all__ = ["atomic_open", "atomic_write_bytes", "atomic_write_text", "temporary_path_for"]
