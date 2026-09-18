"""Process-wide budgets for review decoders and retained preview images.

The budgets exclude the image currently displayed by a widget, decoder-internal
buffers and temporary full-resolution frames. Acquisitions run on workers only.
"""

from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import dataclass
import threading
import time
import weakref

from .runtime_metrics import RuntimeMetrics


FOREGROUND = 0
THUMBNAIL = 1
PREFETCH = 2


@dataclass(frozen=True)
class DecodeSnapshot:
    captures: int
    background_captures: int
    waiting: int
    peak_captures: int
    cache_bytes: int
    peak_cache_bytes: int
    cache_hits: int
    cache_misses: int


class DecodeResources:
    def __init__(self, *, max_captures=6, max_background=2, max_cache_bytes=256 * 1024 * 1024):
        self.max_captures = max_captures
        self.max_background = max_background
        self.max_cache_bytes = max_cache_bytes
        self._condition = threading.Condition(threading.RLock())
        self._captures = self._background = self._peak_captures = 0
        self._waiting = []
        self._caches = weakref.WeakValueDictionary()
        self._cache_serial = 0
        self._peak_cache_bytes = self._hits = self._misses = 0
        self.metrics = RuntimeMetrics()

    def open_capture(self, path, factory, *, priority=FOREGROUND, cancelled=lambda: False, wait=True):
        started = time.perf_counter()
        ticket = (priority, object())
        with self._condition:
            self._waiting.append(ticket)
            try:
                while not cancelled():
                    first = min(self._waiting, key=lambda item: item[0])
                    if (first is ticket and self._captures < self.max_captures
                            and (priority == FOREGROUND or self._background < self.max_background)):
                        self._captures += 1
                        self._background += int(priority != FOREGROUND)
                        self._peak_captures = max(self._peak_captures, self._captures)
                        break
                    if not wait:
                        return None
                    self._condition.wait(0.05)
                else:
                    return None
            finally:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                self.metrics.observe("decode.capture_wait", (time.perf_counter() - started) * 1000)
        try:
            capture = factory(str(path))
            if capture is None:
                self._release_capture(priority)
                return None
            return _CaptureLease(capture, self, priority)
        except BaseException:
            self._release_capture(priority)
            raise

    def _release_capture(self, priority):
        with self._condition:
            self._captures -= 1
            self._background -= int(priority != FOREGROUND)
            self._condition.notify_all()

    def _cache_bytes(self):
        return sum(cache._bytes for cache in list(self._caches.values()))

    def snapshot(self):
        with self._condition:
            return DecodeSnapshot(self._captures, self._background, len(self._waiting),
                                  self._peak_captures, self._cache_bytes(), self._peak_cache_bytes,
                                  self._hits, self._misses)


class _CaptureLease:
    def __init__(self, capture, resources, priority):
        self._capture = capture
        self._resources = resources
        self._priority = priority

    def __getattr__(self, name):
        return getattr(self._capture, name)

    def release(self):
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            finally:
                self._resources._release_capture(self._priority)


class ImageCache(MutableMapping):
    """An LRU mapping whose images also count against a shared byte budget."""

    def __init__(self, *, resources=None, priority=FOREGROUND, max_bytes=64 * 1024 * 1024,
                 image_of=lambda value: value, max_items=None):
        self.resources = resources or DECODE_RESOURCES
        self.priority = priority
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.image_of = image_of
        self._entries = OrderedDict()
        self._bytes = 0
        with self.resources._condition:
            self.resources._caches[id(self)] = self

    @property
    def byte_count(self):
        with self.resources._condition:
            return self._bytes

    def __len__(self):
        with self.resources._condition:
            return len(self._entries)

    def __iter__(self):
        with self.resources._condition:
            return iter(tuple(self._entries))

    def __getitem__(self, key):
        with self.resources._condition:
            entry = self._entries.get(key)
            if entry is None:
                self.resources._misses += 1
                raise KeyError(key)
            self.resources._hits += 1
            self.move_to_end(key)
            return entry[0]

    def __setitem__(self, key, value):
        resources = self.resources
        size = self.image_of(value).sizeInBytes()
        with resources._condition:
            self.pop(key, None)
            if size > min(self.max_bytes, resources.max_cache_bytes):
                return
            resources._cache_serial += 1
            self._entries[key] = (value, size, resources._cache_serial)
            self._bytes += size
            while (self._bytes > self.max_bytes
                   or (self.max_items is not None and len(self._entries) > self.max_items)):
                self.popitem(last=False)
            total = resources._cache_bytes()
            while total > resources.max_cache_bytes:
                candidates = [cache for cache in list(resources._caches.values()) if cache._entries]
                victim = max(candidates, key=lambda cache: (
                    cache.priority, -next(iter(cache._entries.values()))[2]))
                removed_size = next(iter(victim._entries.values()))[1]
                victim.popitem(last=False)
                total -= removed_size
            resources._peak_cache_bytes = max(resources._peak_cache_bytes, total)

    def __delitem__(self, key):
        with self.resources._condition:
            self._bytes -= self._entries.pop(key)[1]

    def move_to_end(self, key):
        with self.resources._condition:
            if key not in self._entries:
                return
            value, size, _ = self._entries[key]
            self.resources._cache_serial += 1
            self._entries[key] = (value, size, self.resources._cache_serial)
            self._entries.move_to_end(key)

    def popitem(self, last=True):
        with self.resources._condition:
            key, entry = self._entries.popitem(last=last)
            self._bytes -= entry[1]
            return key, entry[0]

    def pop(self, key, *default):
        with self.resources._condition:
            if key not in self._entries:
                if default:
                    return default[0]
                raise KeyError(key)
            value, size, _ = self._entries.pop(key)
            self._bytes -= size
            return value

    def values(self):
        with self.resources._condition:
            return tuple(entry[0] for entry in self._entries.values())

    def items(self):
        with self.resources._condition:
            return tuple((key, entry[0]) for key, entry in self._entries.items())

    def clear(self):
        with self.resources._condition:
            self._entries.clear()
            self._bytes = 0


DECODE_RESOURCES = DecodeResources()
