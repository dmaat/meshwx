"""WeatherService: location resolution, provider fallback and report caching.

* Home location (:meth:`WeatherService.home_location`): configured
  latitude/longitude win unless the ZIP was overridden at runtime; otherwise
  the ZIP is geocoded.
* Reports are cached per location (lat/lon rounded to 3 decimals, ~100 m) for
  ``weather.cache_minutes``.  On a miss: the primary provider; if it fails,
  the fallback provider (note "via Open-Meteo"); if that fails too, a cached
  report up to 3 h old marked ``stale`` (note "cached 25m"); else
  :class:`~meshwx.errors.WeatherError`.
* Parts missing from a primary report are filled from the fallback provider:
  current conditions (note "obs via Open-Meteo") and the forecast (note
  "fc via Open-Meteo", e.g. during an NWS gridpoint outage).  A report built
  by the fallback provider still gets the primary's alert feed, which is a
  separate endpoint that usually keeps working.
* After a failed refresh a location is not fetched again for a while (2 min,
  doubling up to 10 min while failures continue): the stale copy, or the
  same error, comes back at once.  ``force=True`` and :meth:`get_alerts` are
  not held back.
* Every returned report goes through one finishing step, so ``!alerts`` and
  ``!alerts N`` number the same list: alerts whose ``ends`` has passed are
  dropped (the clock is the report's ``fetched_at`` plus its cache age), and
  for the home location the alert list of a newer :meth:`get_alerts` fetch
  (the alert watcher's) replaces the report's.

Returned reports are shallow copies sharing their parts with the cache: treat
them as read-only (the service itself never modifies a cached report).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ..cache import TTLCache
from ..config import Config, normalize_zip
from ..errors import ConfigError, GeocodeError, MeshWxError, WeatherError
from ..geocode import ZipGeocoder
from ..httpclient import HttpClient
from ..models import Location, WeatherAlert, WeatherReport
from ..state import RuntimeSettings
from .base import WeatherProvider
from .nws import NO_DATA_MESSAGE

log = logging.getLogger(__name__)

GEOCODE_CACHE_FILE = "geocode.json"
STALE_MAX_SECONDS = 3 * 3600
NAME_RETRY_SECONDS = 15 * 60  # retry naming a lat/lon home after its ZIP lookup failed
MAX_CACHED_REPORTS = 32
FAILURE_BACKOFF_SECONDS = 2 * 60  # after a failed refresh, serve the stale copy (or the error) this long
FAILURE_BACKOFF_MAX_SECONDS = 10 * 60  # ... doubling while the failures continue
ALERTS_FAILED_MESSAGE = "Alerts unavailable, try later"
PROVIDER_LABELS = {"nws": "NWS", "openmeteo": "Open-Meteo"}

_Key = Tuple[float, float]


class WeatherService:
    """Thread-safe weather facade for the bot, the broadcaster and the alert watcher."""

    def __init__(self, cfg: Config, settings: RuntimeSettings, http: HttpClient, *,
                 geocoder: Optional[ZipGeocoder] = None,
                 providers: Optional[Dict[str, WeatherProvider]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """``providers`` maps provider names to instances (default: built from ``cfg.weather``)."""
        self._cfg = cfg
        self._settings = settings
        self._clock = clock
        if geocoder is None:
            geocoder = ZipGeocoder(http, cfg.cache_path() / GEOCODE_CACHE_FILE)
        self._geocoder = geocoder
        wcfg = cfg.weather
        if providers is None:
            from . import create_weather_provider  # deferred: the package __init__ imports this module
            providers = {n: create_weather_provider(n, http, cfg) for n in (wcfg.provider, wcfg.fallback_provider) if n}
        if wcfg.provider not in providers:
            raise ConfigError("weather provider %r is not available" % wcfg.provider)
        self._primary = providers[wcfg.provider]
        fallback = wcfg.fallback_provider
        self._fallback = providers.get(fallback) if fallback and fallback != wcfg.provider else None
        self._reports = TTLCache(clock=clock, max_entries=MAX_CACHED_REPORTS)
        self._fetch_lock = threading.Lock()  # one provider round-trip at a time: no duplicate fetches
        self._lock = threading.Lock()  # guards _home, _failures and _home_alerts
        self._home: Optional[Tuple[Tuple[str, bool], Location, Optional[float]]] = None  # key, loc, retry_at
        # cache key -> (clock() before which no refresh is tried, backoff seconds, the failure)
        self._failures: Dict[_Key, Tuple[float, float, WeatherError]] = {}
        # last successful get_alerts(): (cache key of the home location, alerts, clock() when fetched)
        self._home_alerts: Optional[Tuple[_Key, List[WeatherAlert], float]] = None

    # ------------------------------------------------------------------ locations
    def home_location(self) -> Location:
        """The location broadcasts and a plain ``wx`` are about.

        Config latitude/longitude win unless the ZIP was overridden at runtime
        (then that ZIP is geocoded).  ``location.name`` replaces the display
        name only while the ZIP is not overridden; without it a lat/lon home is
        named after its ZIP ("City, ST").  Raises :class:`GeocodeError`.
        """
        key = (self._settings.zip_code, self._settings.zip_is_override)
        with self._lock:
            home = self._home
        if home is not None and home[0] == key and (home[2] is None or self._clock() < home[2]):
            return home[1]
        loc, complete = self._resolve_home(*key)
        with self._lock:
            self._home = (key, loc, None if complete else self._clock() + NAME_RETRY_SECONDS)
        return loc

    def location_for_zip(self, zip_code: str) -> Location:
        """Location of any ZIP (``wx 90210``); the home ZIP gives :meth:`home_location`."""
        country = self._cfg.location.country
        try:
            code = normalize_zip(zip_code, country)
        except ConfigError as exc:
            raise GeocodeError(str(exc), user_message=exc.user_message) from exc
        if code == self._settings.zip_code:
            return self.home_location()
        return self._geocoder.lookup(code, country)

    def _resolve_home(self, zip_code: str, overridden: bool) -> Tuple[Location, bool]:
        """(home location, complete); not complete = placeholder name, worth retrying later."""
        lc = self._cfg.location
        if overridden or lc.latitude is None or lc.longitude is None:
            loc = self._geocoder.lookup(zip_code, lc.country)
            if lc.name and not overridden:
                loc = dataclasses.replace(loc, name=lc.name)
            return loc, True
        name, tz, complete = lc.name, None, True
        if not name and zip_code:
            try:
                geo = self._geocoder.lookup(zip_code, lc.country)
                name, tz = geo.name, geo.timezone
            except GeocodeError as exc:
                log.warning("cannot name the home location after ZIP %s: %s", zip_code, exc)
                complete = False
        return Location(latitude=lc.latitude, longitude=lc.longitude,
                        name=name or "%.3f,%.3f" % (lc.latitude, lc.longitude),
                        zip_code=zip_code or None, country=lc.country.upper(), timezone=tz), complete

    # ------------------------------------------------------------------ reports
    def get_report(self, zip_code: Optional[str] = None, *, force: bool = False) -> WeatherReport:
        """Weather for ``zip_code`` (default: home), cached for ``weather.cache_minutes``.

        ``force`` skips the fresh cache and the failure backoff; a stale copy
        may still be served when every provider fails.  Raises
        :class:`GeocodeError` or :class:`WeatherError`.
        """
        loc = self.location_for_zip(zip_code) if zip_code else self.home_location()
        key = _cache_key(loc)
        cached = None if force else self._cached(key, loc)
        if cached is not None:
            return cached
        with self._fetch_lock:
            cached = None if force else self._cached(key, loc)  # fetched (or failed) while we waited?
            return cached if cached is not None else self._refresh(loc, key)

    def get_alerts(self) -> Tuple[List[WeatherAlert], Optional[str]]:
        """Active alerts for the home location (never cached) and its IANA time zone (None if unknown).

        Returns no alerts when the primary provider has no alert feed.  The
        zone comes from the last report, else the primary provider (NWS reads
        its cached ``/points`` entry), else the location.  A successful fetch
        also replaces the alert list of home reports older than it.  Raises
        :class:`WeatherError` when the alerts cannot be fetched.
        """
        try:
            loc = self.home_location()
        except GeocodeError as exc:
            raise WeatherError(str(exc), user_message=exc.user_message) from exc
        if not self._primary.supports_alerts:
            return [], self._zone(loc)
        alerts = self._fetch_alerts(loc)
        with self._lock:
            self._home_alerts = (_cache_key(loc), list(alerts), self._clock())
        return alerts, self._zone(loc)

    def alerts_supported(self) -> bool:
        """True when the primary provider has an alert feed (NWS)."""
        return bool(self._primary.supports_alerts)

    def invalidate(self) -> None:
        """Forget cached reports, alerts, failures and the resolved home location (e.g. after a ZIP change)."""
        self._reports.clear()
        with self._lock:
            self._home = None
            self._failures.clear()
            self._home_alerts = None

    # ------------------------------------------------------------------ internals
    def _cached(self, key: _Key, loc: Location) -> Optional[WeatherReport]:
        """The fresh cached report; while backing off after a failed refresh the stale one (or the
        failure raised again); None when a fetch is due."""
        hit = self._reports.get_stale(key, self._cfg.weather.cache_minutes * 60.0)
        if hit is not None:
            return self._finish(hit[0], loc, hit[1])
        with self._lock:
            failure = self._failures.get(key)
        if failure is None or self._clock() >= failure[0]:
            return None
        stale = self._stale(key, loc)  # backing off: no network I/O
        if stale is None:
            raise WeatherError(str(failure[2]), user_message=failure[2].user_message)
        return stale

    def _refresh(self, loc: Location, key: _Key) -> WeatherReport:
        """Fetch and cache a new report (fetch lock held); serve a stale one if every provider fails."""
        try:
            report = self._fetch(loc)
        except WeatherError as exc:
            backoff = self._remember_failure(key, exc)
            stale = self._stale(key, loc)
            if stale is None:
                raise
            log.warning("weather for %s unavailable (%s); serving the cached report, next try in %d s",
                        loc.name, exc, backoff)
            return stale
        with self._lock:
            self._failures.pop(key, None)
        self._reports.put(key, report)
        return self._finish(report, loc, 0.0)

    def _stale(self, key: _Key, loc: Location) -> Optional[WeatherReport]:
        """The cached report up to 3 h old, marked stale ("cached 25m"); None when there is none."""
        hit = self._reports.get_stale(key, STALE_MAX_SECONDS)
        if hit is None:
            return None
        cached, age = hit
        stale = dataclasses.replace(cached, stale=True, notes=cached.notes + ["cached %dm" % int(age // 60)])
        return self._finish(stale, loc, age)

    def _remember_failure(self, key: _Key, exc: WeatherError) -> float:
        """Hold refreshes of ``key`` back for a while (2 min, doubling up to 10 min); returns the backoff."""
        with self._lock:
            previous = self._failures.pop(key, None)
            backoff = float(FAILURE_BACKOFF_SECONDS)
            if previous is not None:
                backoff = min(previous[1] * 2, float(FAILURE_BACKOFF_MAX_SECONDS))
            while len(self._failures) >= MAX_CACHED_REPORTS:
                del self._failures[min(self._failures, key=lambda k: self._failures[k][0])]
            # a traceback-free copy: nothing of the failed fetch is kept alive
            self._failures[key] = (self._clock() + backoff, backoff,
                                   WeatherError(str(exc), user_message=exc.user_message))
        return backoff

    def _finish(self, report: WeatherReport, loc: Location, age: float) -> WeatherReport:
        """A copy of a (possibly cached) report for ``loc``, as every caller sees it.

        For the home location the newest :meth:`get_alerts` result replaces
        older alerts; alerts that have ended by now (``fetched_at`` + ``age``)
        are dropped.
        """
        alerts = report.alerts
        changes: Dict[str, object] = {"location": loc}
        if self._cfg.weather.include_alerts and self._primary.supports_alerts:
            with self._lock:
                home = self._home_alerts
            if home is not None and home[0] == _cache_key(loc) and home[2] > self._clock() - age:
                alerts = home[1]
                changes.update(alerts_supported=True, alerts_error=None)
        now = report.fetched_at + timedelta(seconds=max(0.0, age))
        changes["alerts"] = [a for a in alerts if a.ends is None or a.ends > now]
        return dataclasses.replace(report, **changes)

    def _zone(self, loc: Location) -> Optional[str]:
        """IANA zone of ``loc``: the last report's, else the primary provider's, else the location's."""
        hit = self._reports.get_stale(_cache_key(loc), float("inf"))  # the zone of the last report, any age
        zone = hit[0].timezone if hit is not None else None
        lookup = getattr(self._primary, "timezone_for", None)
        if not zone and lookup is not None:
            try:
                zone = lookup(loc)
            except Exception:  # noqa: BLE001 - a zone is nice to have; never fail the alert check for it
                log.exception("weather provider %s crashed looking up the time zone", self._primary.name)
        return zone or loc.timezone

    def _fetch_alerts(self, loc: Location) -> List[WeatherAlert]:
        """The primary provider's alerts for ``loc``; every failure becomes :class:`WeatherError`."""
        try:
            return self._primary.fetch_alerts(loc)
        except WeatherError:
            raise
        except Exception as exc:  # noqa: BLE001 - network errors and provider bugs alike
            if not isinstance(exc, MeshWxError):
                log.exception("weather provider %s crashed fetching alerts", self._primary.name)
            raise WeatherError("%s alerts: %s" % (self._primary.name, exc),
                               user_message=ALERTS_FAILED_MESSAGE) from exc

    def _fetch(self, loc: Location) -> WeatherReport:
        """Primary provider, else the fallback provider (whose WeatherError propagates)."""
        periods = self._cfg.weather.forecast_periods
        include_alerts = self._cfg.weather.include_alerts
        try:
            report = _call(self._primary, loc, periods, include_alerts)
        except WeatherError as exc:
            if self._fallback is None:
                raise
            log.warning("%s failed for %s (%s); using %s", self._primary.name, loc.name, exc, self._fallback.name)
            report = _call(self._fallback, loc, periods, include_alerts)
            report.notes.append("via %s" % _label(self._fallback))
            if include_alerts and self._primary.supports_alerts:
                self._add_primary_alerts(report, loc, no_data=exc.user_message == NO_DATA_MESSAGE)
            return report
        self._fill_gaps(report, loc, periods)
        return report

    def _add_primary_alerts(self, report: WeatherReport, loc: Location, no_data: bool) -> None:
        """Give a fallback report the primary's alerts: the alert feed is a separate endpoint that
        usually survives a gridpoint outage, and covers US points without a gridded forecast
        (American Samoa).  Outside the US with no primary data it stays "Alerts: US/NWS only"."""
        if no_data and (loc.country or "").upper() != "US":
            return
        try:
            alerts = self._fetch_alerts(loc)
        except WeatherError as exc:
            log.warning("%s", exc)
            report.alerts_error = ALERTS_FAILED_MESSAGE  # the feed exists for this place but is down
            return
        report.alerts, report.alerts_supported, report.alerts_error = alerts, True, None

    def _fill_gaps(self, report: WeatherReport, loc: Location, periods: int) -> None:
        """Take current conditions / forecast missing from a primary report from the fallback provider."""
        need_current = report.current is None
        need_forecast = periods > 0 and not report.forecast
        if self._fallback is None or not (need_current or need_forecast):
            return
        try:
            extra = _call(self._fallback, loc, periods if need_forecast else 0, False)
        except WeatherError as exc:
            log.warning("cannot fill in missing weather from %s: %s", self._fallback.name, exc)
            return
        label = _label(self._fallback)
        if need_current and extra.current is not None:
            report.current = extra.current
            report.notes.append("obs via %s" % label)
        if need_forecast and extra.forecast:
            report.forecast = extra.forecast
            report.notes.append("fc via %s" % label)
        report.timezone = report.timezone or extra.timezone


def _call(provider: WeatherProvider, loc: Location, periods: int, include_alerts: bool) -> WeatherReport:
    """``provider.fetch`` with every failure turned into :class:`WeatherError`."""
    try:
        return provider.fetch(loc, periods=periods, include_alerts=include_alerts)
    except WeatherError:
        raise
    except MeshWxError as exc:
        raise WeatherError("%s: %s" % (provider.name, exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a provider bug must not take the fallback down with it
        log.exception("weather provider %s crashed", provider.name)
        raise WeatherError("%s crashed: %r" % (provider.name, exc)) from exc


def _cache_key(loc: Location) -> _Key:
    return round(loc.latitude, 3), round(loc.longitude, 3)


def _label(provider: WeatherProvider) -> str:
    return PROVIDER_LABELS.get(provider.name, provider.name)
