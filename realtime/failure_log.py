"""Rate-limited warnings for failures that the caller already handles.

Several places keep the field console running by falling back to a safe
default when something fails (an empty device list, "still recording", an
unreadable video).  The fallback stays; this module only makes sure the
failure reaches the runtime log so it can be diagnosed after the race,
without flooding the log when a timer hits the same failure every second.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

DEFAULT_INTERVAL_S = 60.0

_lock = threading.Lock()
_last_logged: dict[tuple[str, str], float] = {}


def warn_handled_failure(
    logger: logging.Logger,
    key: str,
    message: str,
    *args: object,
    exc_info: bool = False,
    interval_s: float = DEFAULT_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Log ``message`` as a warning at most once per ``interval_s`` per key.

    Returns True when the warning was written.  Callers must pass details that
    are safe to log (for example RTSP sources through
    ``sanitize_recording_message``).
    """

    now = clock()
    slot = (logger.name, key)
    with _lock:
        last = _last_logged.get(slot)
        if last is not None and now - last < interval_s:
            return False
        _last_logged[slot] = now
    logger.warning(message, *args, exc_info=exc_info)
    return True


def reset_handled_failure_log() -> None:
    """Forget previous warnings (used by tests)."""

    with _lock:
        _last_logged.clear()


__all__ = ["DEFAULT_INTERVAL_S", "reset_handled_failure_log", "warn_handled_failure"]
