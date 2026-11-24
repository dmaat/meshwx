"""Broadcast scheduling: when is the next scheduled weather broadcast due?

A :class:`ScheduleSpec` (see the ``broadcast`` section of ``config.example.yaml``)
selects one of three modes:

* ``times`` set - fixed local wall-clock times every day ("07:00", "18:00");
* ``align_to_clock`` and an interval that divides a day - multiples of the
  interval after local midnight (180 -> 00:00, 03:00, 06:00, ...);
* otherwise - every ``interval_minutes`` of real time counted from an anchor
  (the service start, then each previous slot).

Slots inside the quiet hours ``[quiet_start, quiet_end)`` are skipped, not
moved.  With a long interval that does not divide a day, every slot can fall
inside the quiet hours (e.g. a weekly slot that DST pushed from 21:29 to 22:29,
or a 2-day interval started at night): when no slot is left within about
8 days, :class:`BroadcastScheduler` looks again every hour, counting the
interval from that moment, so the cadence moves out of the quiet hours
instead of stopping for good.

DST: wall-clock slots are built in the datetime's own zone (``ZoneInfo``).  A
slot that does not exist (spring-forward gap) moves forward by the gap
(02:30 -> 03:30 EDT, the standard ``fold=0`` reading); a slot that exists twice
(fall-back overlap) is used once, at its first occurrence.  Comparisons and
anchored arithmetic use absolute (UTC) time, so results are always strictly
later than ``after``.
"""

from __future__ import annotations

import copy
import functools
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from . import timeutil
from .config import BroadcastConfig, parse_hhmm

log = logging.getLogger(__name__)

_DAY_MINUTES = 24 * 60
_SEARCH_DAYS = 8  # next_run() gives up (None) when no slot exists this far ahead
_RETRY_SEARCH = timedelta(hours=1)  # BroadcastScheduler looks again this often while next_run() finds none
#: A backward clock step up to this size only delays the pending slot; a larger one reschedules.
_SET_BACK_TOLERANCE = timedelta(minutes=5)

#: quiet hours as (start, end) minute of day; None = no quiet hours
QuietWindow = Optional[Tuple[int, int]]


@dataclass
class ScheduleSpec:
    """The broadcast schedule settings :func:`next_run` works from.

    ``times`` ("HH:MM" local) replaces the interval when non-empty;
    ``interval_minutes == 0`` without times means no scheduled broadcasts.
    """

    interval_minutes: int
    align_to_clock: bool = True
    times: List[str] = field(default_factory=list)
    quiet_start: str = ""
    quiet_end: str = ""

    @classmethod
    def from_config(cls, b: BroadcastConfig, interval_minutes: int) -> "ScheduleSpec":
        """Spec from the ``broadcast`` config section and the effective (runtime) interval."""
        return cls(
            interval_minutes=interval_minutes,
            align_to_clock=b.align_to_clock,
            times=list(b.times),
            quiet_start=b.quiet_start,
            quiet_end=b.quiet_end,
        )


def in_quiet_hours(spec: ScheduleSpec, t: datetime) -> bool:
    """True if ``t``'s wall-clock time is inside ``[quiet_start, quiet_end)``.

    The window may wrap midnight ("22:00"-"06:00"); unset or equal ends mean no quiet hours.
    """
    return _in_window(_quiet_window(spec), t)


def next_run(spec: ScheduleSpec, after: datetime, *, anchor: Optional[datetime] = None) -> Optional[datetime]:
    """First scheduled slot strictly after ``after`` (aware), in ``after``'s timezone.

    * ``spec.times`` set: the next listed wall-clock time (today or a later day);
    * interval 0: None (scheduled broadcasts off);
    * ``align_to_clock`` and the interval divides 24 h: the next multiple of the
      interval after local midnight;
    * otherwise ``anchor + k * interval`` in real time (``anchor`` defaults to
      ``after``, so the first slot is ``after + interval``).

    Slots inside the quiet hours are skipped.  Returns None when no slot is
    left within about 8 days.
    """
    _require_aware(after, "after")
    window = _quiet_window(spec)
    if spec.times:
        minutes = sorted({_minute_of_day(t) for t in spec.times})
        return _next_wall_slot(after, minutes, window)
    if spec.interval_minutes <= 0:
        return None
    if _clock_aligned(spec):
        return _next_wall_slot(after, range(0, _DAY_MINUTES, spec.interval_minutes), window)
    if anchor is None:
        anchor = after
    _require_aware(anchor, "anchor")
    return _next_anchored_slot(after, anchor, spec.interval_minutes, window)


class BroadcastScheduler:
    """Tells the main loop when a scheduled broadcast is due (see :meth:`poll`).

    The pending slot is computed lazily, from "now", and recomputed when the
    spec returned by ``spec_fn`` changes (it is re-read on every poll, so a new
    interval applies at once), on :meth:`reschedule`, when the wall clock went
    back by more than a few minutes since the previous look at it, and - while
    no slot outside the quiet hours was found - every hour.  A slot overdue by
    more than ``grace_minutes`` (the clock jumped forward, e.g. the first NTP
    sync of a Pi without RTC) is skipped with a warning rather than sent late.
    Nothing is scheduled while the clock is not sane (pre-NTP 1970 time).

    ``now_fn`` defaults to the wall clock in ``tz``, else in the host timezone
    (see :func:`_default_now_fn`).  Thread-safe: :attr:`next_run_at` and
    :meth:`reschedule` may be used from other threads.
    """

    def __init__(
        self,
        spec_fn: Callable[[], ScheduleSpec],
        *,
        tz: Optional[tzinfo] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        grace_minutes: int = 15,
    ) -> None:
        self._spec_fn = spec_fn
        self._now_fn = now_fn if now_fn is not None else _default_now_fn(tz)
        self._grace = timedelta(minutes=grace_minutes)
        self._lock = threading.Lock()
        self._spec: Optional[ScheduleSpec] = None  # spec behind ``_next``; None = recompute
        self._next: Optional[datetime] = None
        self._last_now: Optional[datetime] = None  # UTC wall clock at the previous look (set-back check)
        self._retry_at: Optional[datetime] = None  # UTC; set while no slot was found: look again then

    @property
    def next_run_at(self) -> Optional[datetime]:
        """The pending slot (computed on first use); None when nothing is scheduled."""
        now = self._now_fn()
        with self._lock:
            if timeutil.clock_is_sane(now):
                self._refresh(now)
            return self._next

    def poll(self) -> bool:
        """True exactly once per due slot.  Cheap; call it on every main-loop tick."""
        now = self._now_fn()
        if not timeutil.clock_is_sane(now):
            return False
        with self._lock:
            spec = self._refresh(now)
            slot = self._next
            if slot is None or _utc(now) < _utc(slot):
                return False
            # Anchored mode counts on from the slot, not from "now", so intervals don't drift.
            upcoming = next_run(spec, now, anchor=slot if _anchored(spec) else None)
            self._next = upcoming
            if upcoming is None:
                self._retry_at = _utc(now) + _RETRY_SEARCH
        if upcoming is None:
            log.warning("no broadcast slot outside quiet hours within %d days after %s; looking again hourly",
                        _SEARCH_DAYS, _fmt(slot))
        late = _utc(now) - _utc(slot)
        if late > self._grace:
            log.warning("missed broadcast slot %s by %d min (clock jump?); next %s",
                        _fmt(slot), late // timedelta(minutes=1), _fmt(upcoming))
            return False
        log.debug("broadcast slot %s due; next %s", _fmt(slot), _fmt(upcoming))
        return True

    def reschedule(self) -> None:
        """Recompute the pending slot from now (call after the interval changed)."""
        now = self._now_fn()
        with self._lock:
            self._spec = None
            if timeutil.clock_is_sane(now):
                self._refresh(now)

    def _refresh(self, now: datetime) -> ScheduleSpec:
        """Recompute ``_next`` from ``now`` when needed (lock held); return the current spec."""
        spec = self._spec_fn()
        now_utc, last = _utc(now), self._last_now
        self._last_now = now_utc
        retrying = False
        if spec == self._spec:
            if last is not None and now_utc < last - _SET_BACK_TOLERANCE:
                log.warning("clock set back from %s to %s; rescheduling",
                            _fmt(last.astimezone(now.tzinfo)), _fmt(now))
            elif self._next is not None or self._retry_at is None or now_utc < self._retry_at:
                return spec
            else:
                retrying = True  # no slot found last time: look again, counting from now
        self._spec = copy.deepcopy(spec)  # a caller mutating its spec in place is still noticed
        self._next = next_run(spec, now)
        self._retry_at = None
        if self._next is not None:
            log.info("next scheduled broadcast: %s", _fmt(self._next))
        elif spec.times or spec.interval_minutes > 0:
            self._retry_at = now_utc + _RETRY_SEARCH
            if retrying:
                log.debug("still no broadcast slot outside quiet hours within %d days", _SEARCH_DAYS)
            else:
                log.warning("no scheduled broadcast slot outside quiet hours within %d days; looking again hourly",
                            _SEARCH_DAYS)
        else:
            log.info("scheduled broadcasts are off (interval 0)")
        return spec


# ---------------------------------------------------------------------------- helpers


def _default_now_fn(tz: Optional[tzinfo]) -> Callable[[], datetime]:
    """Wall clock in ``tz``, else in the host zone, looked up once (the lookup reads files).

    A host zone without an IANA name is only known as today's fixed UTC
    offset, which goes stale at the next DST change, so in that case the OS's
    current local offset is asked for on every call instead.
    """
    zone = tz if tz is not None else timeutil.local_tz()
    if tz is None and not isinstance(zone, ZoneInfo):
        return lambda: datetime.now().astimezone()
    return functools.partial(timeutil.local_now, zone)


def _require_aware(dt: datetime, name: str) -> None:
    if dt.utcoffset() is None:
        raise ValueError("%s must be a timezone-aware datetime, got %r" % (name, dt))


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _fmt(dt: Optional[datetime]) -> str:
    return "none" if dt is None else dt.strftime("%Y-%m-%d %H:%M %Z")


def _minute_of_day(hhmm: str) -> int:
    hour, minute = parse_hhmm(hhmm)
    return hour * 60 + minute


def _quiet_window(spec: ScheduleSpec) -> QuietWindow:
    if not (spec.quiet_start and spec.quiet_end):
        return None
    start, end = _minute_of_day(spec.quiet_start), _minute_of_day(spec.quiet_end)
    return None if start == end else (start, end)


def _in_window(window: QuietWindow, t: datetime) -> bool:
    if window is None:
        return False
    start, end = window
    minute = t.hour * 60 + t.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def _clock_aligned(spec: ScheduleSpec) -> bool:
    return spec.align_to_clock and _DAY_MINUTES % spec.interval_minutes == 0


def _anchored(spec: ScheduleSpec) -> bool:
    """True in anchored-interval mode (slots follow the previous slot, not the wall clock)."""
    return not spec.times and spec.interval_minutes > 0 and not _clock_aligned(spec)


def _wall_slot(day: date, minute: int, tz: Optional[tzinfo]) -> datetime:
    """``minute`` past local midnight of ``day`` as a real instant in ``tz``.

    The UTC round trip moves a nonexistent time forward by the DST gap and
    keeps the first occurrence (``fold=0``) of an ambiguous one.
    """
    wall = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=tz)
    return wall.astimezone(timezone.utc).astimezone(tz)


def _next_wall_slot(after: datetime, minutes: Sequence[int], window: QuietWindow) -> Optional[datetime]:
    """Earliest daily slot (minutes past local midnight) after ``after``, outside quiet hours."""
    tz = after.tzinfo
    floor = _utc(after)
    day = after.date()
    best: Optional[datetime] = None
    for _ in range(_SEARCH_DAYS + 1):
        next_day = day + timedelta(days=1)
        for minute in minutes:
            slot = _wall_slot(day, minute, tz)
            if _utc(slot) > floor and (best is None or _utc(slot) < _utc(best)) and not _in_window(window, slot):
                best = slot
        # A gap-shifted slot may land past the next midnight: stop only once no later day can beat it.
        if best is not None and _utc(best) < _utc(_wall_slot(next_day, 0, tz)):
            return best
        day = next_day
    return best


def _next_anchored_slot(after: datetime, anchor: datetime, interval_minutes: int,
                        window: QuietWindow) -> Optional[datetime]:
    """First ``anchor + k * interval`` (real time) after ``after``, outside quiet hours."""
    step = timedelta(minutes=interval_minutes)
    floor = _utc(after)
    slot = _utc(anchor) + ((floor - _utc(anchor)) // step + 1) * step
    limit = floor + timedelta(days=_SEARCH_DAYS)
    while slot <= limit:
        local = slot.astimezone(after.tzinfo)
        if not _in_window(window, local):
            return local
        slot += step
    return None
