"""TransitService: stop lookup, caching and display rules on top of one :class:`TransitProvider`.

* :meth:`TransitService.find_stop` resolves what a mesh user typed after
  ``bus``: a 1-based number ("2"), else a label prefix (case-insensitive),
  else a stop id.
* :meth:`TransitService.get_arrivals` caches the provider's answer for
  ``transit.cache_seconds`` per stop selection, so several users asking within
  a minute cost one API call.  Arrival times are absolute, so a cached answer
  is re-filtered against the current time on every read: departed vehicles
  vanish, ``transit.min_minutes`` (walking time) hides arrivals that come too
  soon and ``transit.horizon_minutes`` hides those too far ahead.  The
  provider is asked for ``horizon_minutes`` plus the cache lifetime (whole
  minutes, at most 60 more), so arrivals that come into range while an answer
  is cached are in it.  At most 20 arrivals per stop are returned; grouping
  and ``max_arrivals`` are the formatter's job.
* :meth:`TransitService.warm_up` lets the provider pre-load (the GTFS static
  index for ``gtfs_rt``); meant for a background thread at start-up.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

from ..cache import TTLCache
from ..config import TransitConfig
from ..errors import MeshWxError, TransitError
from ..models import StopArrivals, TransitStop
from ..timeutil import utcnow
from . import PAST_GRACE
from .base import TransitProvider

log = logging.getLogger(__name__)

MAX_ARRIVALS_PER_STOP = 20
MAX_CACHED_SELECTIONS = 32
#: Upper bound of the extra minutes asked from the provider for a cached answer's lifetime.
MAX_CACHE_EXTENSION_MINUTES = 60

_Key = Tuple[TransitStop, ...]


class TransitService:
    """Thread-safe transit facade for the bot and the broadcaster."""

    def __init__(self, cfg: TransitConfig, provider: TransitProvider, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._provider = provider
        self._cache = TTLCache(clock=clock, max_entries=MAX_CACHED_SELECTIONS)
        self._fetch_lock = threading.Lock()  # one provider round-trip at a time: no duplicate fetches

    @property
    def stops(self) -> List[TransitStop]:
        """The configured stops, in config order."""
        return list(self._cfg.stops)

    def find_stop(self, query: str) -> Optional[TransitStop]:
        """``"2"`` = the 2nd configured stop; else the first label starting with ``query``
        (case-insensitive); else the stop with that id.  None when nothing matches."""
        text = " ".join((query or "").split())
        if not text:
            return None
        stops = self._cfg.stops
        if text.isdecimal() and 1 <= int(text) <= len(stops):
            return stops[int(text) - 1]
        lowered = text.lower()
        for stop in stops:
            if " ".join(stop.label.split()).lower().startswith(lowered):
                return stop
        for stop in stops:
            if stop.id == text:
                return stop
        for stop in stops:
            if stop.id.lower() == lowered:
                return stop
        return None

    def get_arrivals(self, stops: Optional[Sequence[TransitStop]] = None,
                     now: Optional[datetime] = None) -> List[StopArrivals]:
        """Arrivals for ``stops`` (default: all configured), one :class:`StopArrivals` per stop.

        Raises :class:`TransitError` when nothing could be fetched; a failure at
        a single stop is reported in that stop's ``error`` instead.  The
        returned arrivals may be shared with the cache: treat them as read-only.
        """
        chosen = list(self._cfg.stops if stops is None else stops)
        if not chosen:
            return []
        now = now or utcnow()
        # Keyed by the whole stop (frozen), not just its id: the same stop id may be
        # configured twice with different route or direction filters.
        key: _Key = tuple(chosen)
        results = self._cache.get(key, self._cfg.cache_seconds)
        if results is None:
            with self._fetch_lock:
                results = self._cache.get(key, self._cfg.cache_seconds)  # fetched while we waited?
                if results is None:
                    results = self._fetch(chosen, now)
                    self._cache.put(key, results)
        return [self._visible(result, now) for result in results]

    def warm_up(self) -> None:
        """Let the provider pre-load (GTFS static index); a no-op for providers without ``warm_up``.

        Safe to call from a background thread; never raises.
        """
        warm_up = getattr(self._provider, "warm_up", None)
        if not callable(warm_up):
            return
        try:
            warm_up()
        except MeshWxError as exc:
            log.warning("transit warm-up failed: %s", exc)
        except Exception:  # noqa: BLE001 - runs in a background thread, which must not die silently
            log.exception("transit warm-up crashed")

    # ------------------------------------------------------------------ internals
    def _fetch_horizon(self) -> int:
        """Minutes to ask the provider for: the horizon plus the cache lifetime, so a cached answer stays complete."""
        extra = math.ceil(max(0, self._cfg.cache_seconds) / 60.0)
        return self._cfg.horizon_minutes + min(extra, MAX_CACHE_EXTENSION_MINUTES)

    def _fetch(self, stops: List[TransitStop], now: datetime) -> List[StopArrivals]:
        """The provider's answer, with any unexpected exception turned into :class:`TransitError`."""
        try:
            return self._provider.get_arrivals(stops, now, self._fetch_horizon())
        except TransitError:
            raise
        except MeshWxError as exc:  # e.g. an HttpError a provider let through
            raise TransitError("%s: %s" % (self._provider.name, exc), user_message=exc.user_message) from exc
        except Exception as exc:  # noqa: BLE001 - a provider bug must surface as a normal transit error
            log.exception("transit provider %s crashed", self._provider.name)
            raise TransitError("%s crashed: %r" % (self._provider.name, exc)) from exc

    def _visible(self, result: StopArrivals, now: datetime) -> StopArrivals:
        """Keep the arrivals between ``now`` (departed / too soon) and the horizon, and cap the list."""
        earliest = now - PAST_GRACE
        if self._cfg.min_minutes > 0:
            earliest = now + timedelta(minutes=self._cfg.min_minutes)
        latest = now + timedelta(minutes=self._cfg.horizon_minutes)
        kept = [a for a in result.arrivals if earliest <= a.time <= latest][:MAX_ARRIVALS_PER_STOP]
        return StopArrivals(stop=result.stop, arrivals=kept, error=result.error)
