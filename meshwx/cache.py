"""Small thread-safe TTL cache plus JSON-file persistence helpers."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

log = logging.getLogger(__name__)


class TTLCache:
    """Thread-safe in-memory cache that remembers when each value was stored.

    ``get`` returns only fresh values; ``get_stale`` lets callers serve an
    older value (stale-if-error) when a refresh fails.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = 64) -> None:
        self._clock = clock
        self._max = max_entries
        self._data: Dict[Hashable, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if key not in self._data and len(self._data) >= self._max:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (self._clock(), value)

    def get(self, key: Hashable, max_age: float) -> Optional[Any]:
        hit = self.get_stale(key, max_age)
        return hit[0] if hit else None

    def get_stale(self, key: Hashable, max_age: float) -> Optional[Tuple[Any, float]]:
        """Return ``(value, age_seconds)`` if stored less than ``max_age`` seconds ago."""
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return None
        stored, value = item
        age = self._clock() - stored
        if age > max_age:
            return None
        return value, age

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file; return ``default`` if missing or corrupt (corruption is logged)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable JSON file %s: %s", path, exc)
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + fsync + rename) to survive power loss."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
