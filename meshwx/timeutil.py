"""Time helpers shared by providers, scheduler and formatter.

Python 3.9/3.10's ``datetime.fromisoformat`` rejects a trailing "Z" and some
fractional-second widths, so all ISO-8601 parsing must go through
:func:`parse_iso`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - 3.8 is not supported, keep import errors explicit
    raise

log = logging.getLogger(__name__)

_FRACTION_RE = re.compile(r"(\.\d+)")


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware datetime.

    Accepts "Z" suffixes, any number of fractional digits, and offsets with or
    without a colon.  Naive timestamps are assumed to be UTC.  Returns None for
    None/empty/unparseable input (and logs at debug level).
    """
    if not value:
        return None
    s = str(value).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"

    # Normalise fractional seconds to exactly 6 digits (3.9/3.10 are strict).
    def _fix_fraction(m: "re.Match[str]") -> str:
        digits = m.group(1)[1:]
        return "." + (digits + "000000")[:6]

    s = _FRACTION_RE.sub(_fix_fraction, s, count=1)
    # "+0500" -> "+05:00"
    m = re.search(r"([+-])(\d{2})(\d{2})$", s)
    if m and ":" not in s[m.start():]:
        s = s[: m.start()] + "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        log.debug("unparseable ISO timestamp: %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def from_epoch(seconds: float) -> datetime:
    """POSIX seconds -> aware UTC datetime."""
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc)


def get_tz(name: Optional[str]) -> Optional[tzinfo]:
    """IANA name -> tzinfo, or None if the name is empty/unknown."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("unknown timezone %r", name)
        return None


_TZ_CACHE_SECONDS = 3600.0
_tz_cache: Dict[str, Any] = {"at": None, "name": None}
_tz_lock = threading.Lock()


def clear_tz_cache() -> None:
    """Forget the cached host timezone name (tests; or after changing the Pi's timezone)."""
    with _tz_lock:
        _tz_cache["at"] = None


def system_tz_name() -> Optional[str]:
    """Best-effort IANA name of the host's local timezone (Linux/Pi focused).

    Cached for an hour: resolving it reads files, and a bad ``TZ`` value would
    otherwise log a warning on every call from the main loop.
    """
    with _tz_lock:
        at = _tz_cache["at"]
        if at is not None and time.monotonic() - at < _TZ_CACHE_SECONDS:
            return _tz_cache["name"]
    name = _resolve_system_tz_name()
    with _tz_lock:
        _tz_cache["at"], _tz_cache["name"] = time.monotonic(), name
    return name


def _resolve_system_tz_name() -> Optional[str]:
    env = os.environ.get("TZ", "").lstrip(":")
    if env and get_tz(env) is not None:
        return env
    try:
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if text and get_tz(text) is not None:
            return text
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo" + os.sep
        if marker in target:
            name = target.split(marker, 1)[1]
            if get_tz(name) is not None:
                return name
    except OSError:
        pass
    return None


def local_tz() -> tzinfo:
    """Host local timezone as a tzinfo (zoneinfo when resolvable, else fixed offset)."""
    name = system_tz_name()
    tz = get_tz(name) if name else None
    if tz is not None:
        return tz
    fallback = datetime.now().astimezone().tzinfo
    return fallback if fallback is not None else timezone.utc


def local_now(tz: Optional[tzinfo] = None) -> datetime:
    """Current wall-clock time as an aware datetime in ``tz`` (default host tz)."""
    return datetime.now(tz or local_tz())


def clock_is_sane(now: Optional[datetime] = None) -> bool:
    """False while a Pi without RTC still has a pre-NTP clock (year < 2024)."""
    now = now or utcnow()
    return now.year >= 2024
