"""Real-time transit arrivals: provider factory plus the rules every provider shares.

:func:`create_transit_provider` builds the provider named in ``transit.provider``:

* ``gtfs_rt``     - any agency's GTFS-Realtime TripUpdates feed (+ optional static GTFS)
* ``mbta``        - MBTA V3 API (Boston)
* ``onebusaway``  - OneBusAway REST servers (Puget Sound, Tampa, San Diego, ...)
* ``transitland`` - Transitland v2 REST (API key)
* ``siri``        - SIRI StopMonitoring (511.org Bay Area, MTA Bus Time)

Shared rules, so every source looks the same on the radio:

* ``Arrival.time`` is aware UTC: the arrival time, else the departure time;
  a vehicle standing at the stop shows ``now``, or its departure time when it
  leaves more than 90 s later (a layover at the first stop, a timed hold; see
  :func:`visit_time`); shifted by :class:`ClockSkew` when the local clock
  disagrees with the server;
* providers drop cancelled trips, skipped stops, drop-off-only stops and
  vehicles whose departure time has passed;
* :func:`select_arrivals` applies the stop's route / direction filters, keeps
  ``now - 60 s <= time <= now + horizon`` and sorts by ``(time, route_name)``;
* :func:`arrivals_per_stop` turns a failure at one stop into
  ``StopArrivals.error`` and raises :class:`TransitError` only when every
  stop failed.

Provider modules are imported lazily by the factory, so importing this
package never imports protobuf.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

from ..config import TransitConfig
from ..errors import ConfigError, TransitError
from ..httpclient import HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from ..timeutil import parse_iso
from .base import TransitProvider

log = logging.getLogger(__name__)

#: Arrivals up to this long ago are still shown (as "now"): prediction noise, slow delivery.
PAST_GRACE = timedelta(seconds=60)
#: A vehicle standing at the stop that leaves within this long is shown as arriving "now"; one that
#: waits longer (a layover at its first stop, a timed hold) is shown at its departure time.
DWELL_AS_NOW = timedelta(seconds=90)
#: Local and server clocks may differ this much before times are corrected.
SKEW_TOLERANCE = timedelta(seconds=120)
SKEW_WARN_INTERVAL_SECONDS = 3600.0
#: A route id this short is a fine display name when the short name is empty ("Red").
MAX_ROUTE_ID_AS_NAME = 6
#: User message for a response that is not what the API should send (HTML/XML error page, bad JSON).
UNREADABLE_MESSAGE = "Transit feed unreadable"
#: User message when an API refuses because of its rate limit (shown per stop on the radio: short, ASCII).
RATE_LIMIT_MESSAGE = "Rate limited, try later"

__all__ = [
    "ClockSkew",
    "DWELL_AS_NOW",
    "RATE_LIMIT_MESSAGE",
    "RateLimited",
    "UNREADABLE_MESSAGE",
    "arrivals_per_stop",
    "create_transit_provider",
    "http_failure",
    "parse_direction",
    "parse_time",
    "route_display_name",
    "select_arrivals",
    "status_failure",
    "visit_time",
]


def create_transit_provider(cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> TransitProvider:
    """Build the provider named by ``cfg.provider`` (imports only that provider's module)."""
    name = (cfg.provider or "").strip().lower()
    cache_dir = Path(cache_dir)
    if name == "gtfs_rt":
        from .gtfs_rt import GtfsRtProvider

        return GtfsRtProvider(cfg, http, cache_dir)
    if name == "mbta":
        from .mbta import MbtaProvider

        return MbtaProvider(cfg, http, cache_dir)
    if name == "onebusaway":
        from .onebusaway import OneBusAwayProvider

        return OneBusAwayProvider(cfg, http, cache_dir)
    if name == "transitland":
        from .transitland import TransitlandProvider

        return TransitlandProvider(cfg, http, cache_dir)
    if name == "siri":
        from .siri import SiriProvider

        return SiriProvider(cfg, http, cache_dir)
    raise ConfigError("unknown transit provider %r" % cfg.provider)


# --------------------------------------------------------------------------- parsing helpers


def route_display_name(short_name: Optional[str], long_name: Optional[str], route_id: Optional[str]) -> str:
    """Short name; else the route id when short ("Red"); else the long name minus " Line".

    Falls back to the (long) route id, and to "?" when nothing is known.
    """
    short = (short_name or "").strip()
    if short:
        return short
    rid = (route_id or "").strip()
    if rid and len(rid) <= MAX_ROUTE_ID_AS_NAME:
        return rid
    name = (long_name or "").strip()
    if name.endswith(" Line"):
        name = name[: -len(" Line")].rstrip()
    return name or rid or "?"


def parse_direction(value: Any) -> Optional[int]:
    """GTFS direction id (0/1 as int or text) -> int; anything else -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in (0, 1) else None
    text = str(value or "").strip()
    return int(text) if text in ("0", "1") else None


def parse_time(value: Any) -> Optional[datetime]:
    """ISO-8601 text (any offset) -> aware UTC datetime; None for empty or unparseable values."""
    parsed = parse_iso(value) if isinstance(value, str) else None
    return parsed.astimezone(timezone.utc) if parsed is not None else None


def visit_time(arrival: Optional[datetime], departure: Optional[datetime], now: datetime) -> Optional[datetime]:
    """The time shown for a stop visit: the arrival, else the departure.

    A vehicle that has arrived but not left yet is standing at the stop:

    * it leaves more than :data:`DWELL_AS_NOW` from now (a bus laying over at
      the first stop of its trip, whose "arrival" is when it pulled in, or a
      timed hold): its departure time, so nobody gives up on a bus that waits;
    * else, when it arrived more than :data:`PAST_GRACE` ago (a long dwell):
      ``now`` instead of being dropped as past; within the grace the arrival
      itself, which is shown as "now" anyway.

    Callers drop vehicles whose departure has passed before calling this.
    """
    when = arrival if arrival is not None else departure
    if when is None or departure is None or when > now or departure < now:
        return when
    if departure - now > DWELL_AS_NOW:
        return departure
    return now if when < now - PAST_GRACE else when


# --------------------------------------------------------------------------- filtering


def select_arrivals(stop: TransitStop, arrivals: Iterable[Arrival], now: datetime,
                    horizon_minutes: int) -> List[Arrival]:
    """Apply the stop's filters and the time window, then sort by ``(time, route_name)``.

    ``stop.routes`` matches the route id or the displayed route name (which is
    the short name when there is one), case-insensitively.  ``stop.direction_id``
    keeps arrivals whose direction the source does not report: hiding them
    would empty the stop for sources without GTFS directions (511 says IB/OB).
    The sort is stable, so equal keys keep the source's order.
    """
    earliest = now - PAST_GRACE
    latest = now + timedelta(minutes=horizon_minutes)
    routes = {r.strip().lower() for r in stop.routes if r.strip()}
    kept = [a for a in arrivals
            if earliest <= a.time <= latest
            and (not routes or a.route_id.lower() in routes or a.route_name.lower() in routes)
            and (stop.direction_id is None or a.direction_id is None or a.direction_id == stop.direction_id)]
    kept.sort(key=lambda a: (a.time, a.route_name))
    return kept


# --------------------------------------------------------------------------- errors


class RateLimited(TransitError):
    """The API refused because of its rate limit; later stops of the same request are not tried."""


def status_failure(status: Optional[int], source: str, rate_limit_message: str, *,
                   detail: str = "", per_stop: bool = True) -> TransitError:
    """Map an HTTP status (or an API's in-body status code) to a radio-safe :class:`TransitError`.

    429 -> :class:`RateLimited` with ``rate_limit_message``; 401/403 -> "<source> API key
    rejected"; 404 -> "Stop not found" when the URL names a stop (``per_stop``);
    a 2xx status means the body was unreadable.  Log messages never contain the
    URL, which may carry an API key.
    """
    if not detail:
        if status is not None and 200 <= status < 300:
            detail = "response is not valid JSON"
        else:
            detail = "HTTP %d" % status if status else "network error"
    message = "%s request failed: %s" % (source, detail)
    if status == 429:
        return RateLimited(message, user_message=rate_limit_message)
    if status in (401, 403):
        return TransitError(message, user_message="%s API key rejected" % source)
    if status == 404 and per_stop:
        return TransitError(message, user_message="Stop not found")
    if status is not None and 200 <= status < 300:
        return TransitError(message, user_message=UNREADABLE_MESSAGE)
    return TransitError(message)


def http_failure(exc: HttpError, source: str, rate_limit_message: str, *, per_stop: bool = True) -> TransitError:
    """:func:`status_failure` for an :class:`HttpError` (``HttpClient.get_json`` reports bad JSON with a 2xx).

    A connection failure is logged with the exception type only ("network error
    (ConnectTimeout)"): the HttpError text contains the URL, which may carry an API key.
    """
    detail = ""
    if exc.status is None and exc.__cause__ is not None:
        detail = "network error (%s)" % type(exc.__cause__).__name__
    return status_failure(exc.status, source, rate_limit_message, detail=detail, per_stop=per_stop)


def arrivals_per_stop(stops: Sequence[TransitStop], fetch: Callable[[TransitStop], List[Arrival]],
                      now: datetime, horizon_minutes: int) -> List[StopArrivals]:
    """Run ``fetch(stop)`` for each stop (one API call per stop) and filter its result.

    A :class:`TransitError` from one stop becomes that stop's ``error``, and so
    does a response the parser chokes on (KeyError, TypeError, ...: "Transit
    feed unreadable"); after a :class:`RateLimited` error the remaining stops
    are not tried.  Raises the first error when every stop failed.
    """
    results: List[StopArrivals] = []
    first_error: Optional[TransitError] = None
    abort: Optional[RateLimited] = None
    for stop in stops:
        if abort is not None:
            results.append(StopArrivals(stop=stop, error=abort.user_message))
            continue
        try:
            arrivals = fetch(stop)
        except TransitError as exc:
            log.warning("transit stop %s (%s): %s", stop.label, stop.id, exc)
            error = exc
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("transit stop %s (%s): unexpected response", stop.label, stop.id, exc_info=True)
            error = TransitError("unexpected response: %r" % exc, user_message=UNREADABLE_MESSAGE)
        else:
            results.append(StopArrivals(stop=stop, arrivals=select_arrivals(stop, arrivals, now, horizon_minutes)))
            continue
        first_error = first_error or error
        if isinstance(error, RateLimited):
            abort = error
        results.append(StopArrivals(stop=stop, error=error.user_message))
    if first_error is not None and all(r.error for r in results):
        raise TransitError(str(first_error), user_message=first_error.user_message)
    return results


# --------------------------------------------------------------------------- clock skew


class ClockSkew:
    """Corrects for a wrong local clock (a Pi Zero has no RTC) using the server's clock.

    :meth:`offset` returns what to add to server-clock times so that countdowns
    computed against the local clock stay right: ``now - server_now`` when the
    clocks differ by more than :data:`SKEW_TOLERANCE`, else zero.  A warning is
    logged at most once per hour per source.  Thread-safe.
    """

    def __init__(self, source: str, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._source = source
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._warned_at: Optional[float] = None

    def offset(self, now: datetime, server_now: Optional[datetime]) -> timedelta:
        if server_now is None:
            return timedelta(0)
        skew = now - server_now
        if abs(skew) <= SKEW_TOLERANCE:
            return timedelta(0)
        with self._lock:
            mono = self._monotonic()
            warn = self._warned_at is None or mono - self._warned_at >= SKEW_WARN_INTERVAL_SECONDS
            if warn:
                self._warned_at = mono
        if warn:
            log.warning("local clock is %+d s off the %s server clock; correcting arrival times (is NTP running?)",
                        skew.total_seconds(), self._source)
        return skew
