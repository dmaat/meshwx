"""US National Weather Service provider (api.weather.gov, US only, with alerts).

Pipeline for one :meth:`NwsProvider.fetch`:

1. ``/points/{lat},{lon}`` (4 decimals) gives the forecast, hourly-forecast
   and station-list URLs plus the IANA time zone.  Cached 24 h in memory and
   on disk; a stale copy is reused when a refresh fails.  A null ``forecast``
   (marine point) or HTTP 404 (outside coverage) means "no NWS data here".
   Only URLs returned by the API are used: for Anchorage the grid id is "AER"
   while ``cwa`` is "AFC".  For a marine point the zone is the forecast
   office's (St Thomas harbour says "America/New_York"), so a zone the
   geocoder knows (US territories) wins there.
2. 12-hour forecast.  The CDN can serve copies about an hour old, so periods
   that already ended are dropped.
3. Current conditions: the first of the 3 nearest stations whose latest
   observation has a temperature and is at most 90 minutes old.  Puerto Rico,
   Hawaii, USVI and Guam stations often report every quality-controlled value
   as null while ``rawMessage`` still holds the METAR; temperature, dew point,
   wind and altimeter are then decoded from it.  Failing all that, the current
   hour of the hourly forecast, which is fetched only in that case (it has no
   observation time).
4. Active "Actual" alerts for the point.  ``expires`` is often earlier than
   the end of the event, so ``ends`` is preferred; alerts already over are
   dropped, and so are products that end their event (VTEC CAN/EXP/UPG,
   messageType Cancel; without VTEC: urgency Past once expired) - a replaced
   watch stays listed until its original end.  Predecessors named only in
   ``parameters.expiredReferences`` count as references.

A failed forecast request leaves ``report.forecast`` empty (the service can
fill it from its fallback provider); only when neither a forecast nor current
conditions are available does :meth:`NwsProvider.fetch` raise.

Every request sends ``Accept: application/geo+json``; the shared HttpClient
sets the User-Agent that NWS requires.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..cache import atomic_write_json, read_json
from ..config import Config
from ..errors import WeatherError
from ..httpclient import HttpClient, HttpError
from ..models import CurrentConditions, ForecastPeriod, Location, WeatherAlert, WeatherReport
from ..timeutil import parse_iso, utcnow
from .base import WeatherProvider
from .wmo import compass_degrees

log = logging.getLogger(__name__)

API = "https://api.weather.gov"
GEOJSON_HEADERS = {"Accept": "application/geo+json"}
NO_DATA_MESSAGE = "No NWS data here"
ALERTS_FAILED_MESSAGE = "Alerts unavailable, try later"

POINTS_TTL_SECONDS = 24 * 3600
STATIONS_TTL_SECONDS = 24 * 3600
OBS_MAX_AGE = timedelta(minutes=90)
MAX_STATIONS = 3
POINTS_CACHE_FILE = "nws_points.json"
MAX_CACHED_POINTS = 50
HOURLY_STATION_NAME = "NWS forecast"
MPH_TO_KMH = 1.609344
INHG_TO_HPA = 33.8638866667

#: Sort order for alerts (most severe first); anything else ranks last.
SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}

#: VTEC actions that end an event: cancelled, expired, upgraded/replaced.
END_OF_EVENT_ACTIONS = frozenset(("CAN", "EXP", "UPG"))
_VTEC_ACTION_RE = re.compile(r"^/?[OTEX]\.([A-Z]{3})\.")

# METAR groups (body before "RMK", then the remarks' tenths-of-a-degree T group)
_METAR_WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS|KMH)$")
_METAR_TEMP_RE = re.compile(r"^(M?\d{2})/(M?\d{2})?$")
_METAR_PRESSURE_RE = re.compile(r"^([AQ])(\d{4})$")
_METAR_TENTHS_RE = re.compile(r"^T([01])(\d{3})(?:([01])(\d{3}))?$")
_METAR_SPEED_KMH = {"KT": 1.852, "MPS": 3.6, "KMH": 1.0}

_POINT_FIELDS = ("forecast", "forecastHourly", "observationStations", "timeZone",
                 "type", "gridId", "gridX", "gridY")

#: wmoUnit code (namespace stripped) -> (kind, converter to the unit used in meshwx.models)
_UNITS: Dict[str, Tuple[str, Callable[[float], float]]] = {
    "degC": ("temperature", lambda v: v),
    "degF": ("temperature", lambda v: (v - 32.0) * 5.0 / 9.0),
    "K": ("temperature", lambda v: v - 273.15),
    "km_h-1": ("speed", lambda v: v),
    "m_s-1": ("speed", lambda v: v * 3.6),
    "kt": ("speed", lambda v: v * 1.852),
    "Pa": ("pressure", lambda v: v / 100.0),
    "hPa": ("pressure", lambda v: v),
    "m": ("distance", lambda v: v / 1000.0),
    "km": ("distance", lambda v: v),
    "percent": ("percent", lambda v: v),
    "degree_(angle)": ("angle", lambda v: v),
}

_WIND_RE = re.compile(r"^(\d+)(?: to (\d+))? (mph|km/h)$")
_GUST_RE = re.compile(r"gusts as high as (\d+) (mph|km/h)", re.IGNORECASE)
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


class NwsProvider(WeatherProvider):
    """api.weather.gov provider.  ``now_fn`` is injectable so tests can freeze time."""

    name = "nws"
    supports_alerts = True

    def __init__(self, http: HttpClient, cfg: Config, *,
                 now_fn: Callable[[], datetime] = utcnow) -> None:
        self._http = http
        self._now = now_fn
        self._points_file = cfg.cache_path() / POINTS_CACHE_FILE
        self._lock = threading.Lock()
        # point key -> {"t": epoch seconds fetched, "data": subset of /points properties}; read lazily
        self._points: Optional[Dict[str, Dict[str, Any]]] = None
        # station-list URL -> (epoch seconds fetched, station URLs nearest first)
        self._stations: Dict[str, Tuple[float, List[str]]] = {}

    # ------------------------------------------------------------------ WeatherProvider API
    def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
        now = self._now()
        points = self._get_points(location, now)
        if not points.get("forecast"):
            raise WeatherError("NWS has no forecast for %s (point type %r)"
                               % (_point_key(location), points.get("type")), user_message=NO_DATA_MESSAGE)
        report = WeatherReport(location=location, provider=self.name, fetched_at=now,
                               timezone=_zone_name(points, location))
        report.forecast = self._forecast(location, points, now, periods)
        report.current = self._latest_observation(points, now)
        if report.current is None:
            report.current = self._current_from_hourly(points, now)
            if report.current is not None:
                report.notes.append("no recent obs")
        if report.current is None and not report.forecast:
            raise WeatherError("NWS returned neither observations nor a forecast for %s" % location.name)
        if include_alerts:
            try:
                report.alerts = self.fetch_alerts(location)
                report.alerts_supported = True
            except WeatherError as exc:  # keep the weather; the alert list is just unknown
                log.warning("%s", exc)
                report.notes.append("alerts unavailable")
                report.alerts_error = exc.user_message
        return report

    def fetch_alerts(self, location: Location) -> List[WeatherAlert]:
        """Active "Actual" alerts for the point that have not ended, most severe first, then by onset."""
        url = "%s/alerts/active?point=%.4f,%.4f" % (API, location.latitude, location.longitude)
        try:
            body = self._get(url)
        except HttpError as exc:
            raise WeatherError("NWS alerts request failed: %s" % exc, user_message=ALERTS_FAILED_MESSAGE) from exc
        now = self._now()
        alerts: List[WeatherAlert] = []
        for feature in body.get("features") or []:
            alert = map_alert(feature)
            if alert is None or (alert.ends is not None and alert.ends <= now) or ends_its_event(feature, now):
                continue
            alerts.append(alert)
        alerts.sort(key=_alert_order)
        return alerts

    def timezone_for(self, location: Location) -> Optional[str]:
        """IANA zone of ``location`` from the (24 h cached) ``/points`` entry, without a full fetch.

        Falls back to ``location.timezone`` (None when unknown) when ``/points``
        cannot be read.
        """
        try:
            points = self._get_points(location, self._now())
        except WeatherError as exc:
            log.info("time zone of %s unknown: %s", location.name, exc)
            return location.timezone
        return _zone_name(points, location)

    # ------------------------------------------------------------------ HTTP
    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET a GeoJSON object; every failure surfaces as :class:`HttpError`."""
        try:
            body = self._http.get_json(url, params=params, headers=GEOJSON_HEADERS)
        except ValueError as exc:  # undecodable body
            raise HttpError("invalid JSON from %s: %s" % (url, exc), url) from exc
        if not isinstance(body, dict):
            raise HttpError("unexpected JSON from %s" % url, url)
        return body

    # ------------------------------------------------------------------ /points (memory + disk)
    def _get_points(self, location: Location, now: datetime) -> Dict[str, Any]:
        key = _point_key(location)
        stamp = now.timestamp()
        with self._lock:
            entry = self._load_points().get(key)
        if entry is not None and 0 <= stamp - entry["t"] < POINTS_TTL_SECONDS:
            return entry["data"]
        url = "%s/points/%s" % (API, key)
        try:
            props = self._get(url).get("properties")
            if not isinstance(props, dict):
                raise HttpError("no properties in /points response", url)
            data = {f: props.get(f) for f in _POINT_FIELDS}
        except HttpError as exc:
            if exc.status != 404:
                if entry is not None:
                    log.warning("NWS /points refresh failed (%s); reusing %.0f h old copy",
                                exc, (stamp - entry["t"]) / 3600.0)
                    return entry["data"]
                raise WeatherError("NWS /points request failed: %s" % exc) from exc
            log.info("NWS has no data for point %s (HTTP 404)", key)
            data = dict.fromkeys(_POINT_FIELDS)  # remembered like a marine point: no forecast
        with self._lock:
            points = self._load_points()
            points[key] = {"t": stamp, "data": data}
            if len(points) > MAX_CACHED_POINTS:
                for old in sorted(points, key=lambda k: points[k]["t"])[:len(points) - MAX_CACHED_POINTS]:
                    del points[old]
            self._save_points()
        return data

    def _drop_points(self, location: Location) -> None:
        """Forget a point whose grid no longer exists so the next fetch resolves it again."""
        with self._lock:
            if self._load_points().pop(_point_key(location), None) is not None:
                self._save_points()

    def _load_points(self) -> Dict[str, Dict[str, Any]]:
        """The points cache, read from disk on first use (lock held)."""
        if self._points is None:
            doc = read_json(self._points_file, default=None)
            raw = doc.get("points") if isinstance(doc, dict) else None
            self._points = {}
            for key, entry in (raw.items() if isinstance(raw, dict) else ()):
                if (isinstance(entry, dict) and isinstance(entry.get("data"), dict)
                        and isinstance(entry.get("t"), (int, float))):
                    self._points[str(key)] = {"t": float(entry["t"]), "data": entry["data"]}
        return self._points

    def _save_points(self) -> None:
        """Persist the points cache (lock held)."""
        try:
            atomic_write_json(self._points_file, {"version": 1, "points": self._points})
        except OSError as exc:
            log.warning("cannot write NWS points cache %s: %s", self._points_file, exc)

    # ------------------------------------------------------------------ forecast
    def _forecast(self, location: Location, points: Dict[str, Any], now: datetime,
                  periods: int) -> List[ForecastPeriod]:
        if periods <= 0:
            return []
        try:
            body = self._get(points["forecast"])
        except HttpError as exc:
            if exc.status == 404:  # "Gridpoint Does Not Exist": the grid moved
                self._drop_points(location)
            log.warning("NWS forecast unavailable: %s", exc)
            return []
        result: List[ForecastPeriod] = []
        for period in _periods(body):
            end = parse_iso(period.get("endTime"))
            if end is not None and end <= now:
                continue  # stale CDN copy: this period is already over
            result.append(map_forecast_period(period))
            if len(result) >= periods:
                break
        return result

    # ------------------------------------------------------------------ current conditions
    def _station_urls(self, points: Dict[str, Any], now: datetime) -> List[str]:
        url = points.get("observationStations")
        if not url:
            return []
        stamp = now.timestamp()
        with self._lock:
            cached = self._stations.get(url)
        if cached is not None and 0 <= stamp - cached[0] < STATIONS_TTL_SECONDS:
            return cached[1]
        try:
            body = self._get(url, params={"limit": MAX_STATIONS})
        except HttpError as exc:
            log.warning("NWS station list unavailable: %s", exc)
            return cached[1] if cached is not None else []
        stations = [f["id"] for f in body.get("features") or [] if isinstance(f, dict) and f.get("id")]
        if not stations:
            stations = [u for u in body.get("observationStations") or [] if isinstance(u, str)]
        stations = stations[:MAX_STATIONS]
        if not stations:
            return []  # never pin an empty (possibly glitched) list for a whole day
        with self._lock:
            self._stations[url] = (stamp, stations)
            if len(self._stations) > MAX_CACHED_POINTS:
                del self._stations[min(self._stations, key=lambda k: self._stations[k][0])]
        return stations

    def _latest_observation(self, points: Dict[str, Any], now: datetime) -> Optional[CurrentConditions]:
        for station_url in self._station_urls(points, now):
            url = station_url.rstrip("/") + "/observations/latest"
            try:
                body = self._get(url)
            except HttpError as exc:
                log.info("NWS observation unavailable: %s", exc)
                continue
            current = map_observation(body)
            if current.temperature_c is None:
                log.debug("NWS station %s: no temperature in latest observation", current.station)
            elif current.observed_at is None or now - current.observed_at > OBS_MAX_AGE:
                log.debug("NWS station %s: latest observation too old (%s)", current.station, current.observed_at)
            else:
                return current
        return None

    def _current_from_hourly(self, points: Dict[str, Any], now: datetime) -> Optional[CurrentConditions]:
        """Approximate current conditions from the hourly-forecast period covering ``now``."""
        url = points.get("forecastHourly")
        if not url:
            return None
        try:
            body = self._get(url)
        except HttpError as exc:
            log.warning("NWS hourly forecast unavailable: %s", exc)
            return None
        for period in _periods(body):
            end = parse_iso(period.get("endTime"))
            if end is None or end <= now:
                continue
            return CurrentConditions(
                description=_text(period.get("shortForecast")),
                temperature_c=_period_temperature(period),
                humidity_pct=quantity(period.get("relativeHumidity"), "percent"),
                dewpoint_c=quantity(period.get("dewpoint"), "temperature"),
                wind_speed_kmh=parse_wind_speed(period.get("windSpeed")),
                wind_gust_kmh=quantity(period.get("windGust"), "speed"),
                wind_dir_deg=compass_degrees(period.get("windDirection")),
                station=HOURLY_STATION_NAME,
            )
        return None


# ---------------------------------------------------------------------- mapping helpers


def quantity(obj: Any, kind: str) -> Optional[float]:
    """NWS quantity ``{"unitCode": "wmoUnit:degC", "value": 3}`` -> float in the model unit.

    ``kind`` is one of temperature (degC), speed (km/h), pressure (hPa),
    distance (km), percent or angle (degrees).  A range ``{minValue, maxValue}``
    gives its maximum and a missing unitCode means "already in the model unit".
    Null values, unknown units and units of another kind give None.
    """
    if not isinstance(obj, dict):
        return None
    value = obj.get("value")
    if value is None:
        value = obj.get("maxValue")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    unit = str(obj.get("unitCode") or "").rsplit(":", 1)[-1]
    if not unit:
        return float(value)
    conversion = _UNITS.get(unit)
    if conversion is None or conversion[0] != kind:
        log.debug("NWS: unexpected unit %r for %s", unit, kind)
        return None
    return conversion[1](float(value))


def parse_wind_speed(value: Any) -> Optional[float]:
    """NWS forecast ``windSpeed`` ("6 to 12 mph", "9 km/h", "0 mph", quantity) -> max speed in km/h."""
    if isinstance(value, dict):
        return quantity(value, "speed")
    if not isinstance(value, str):
        return None
    m = _WIND_RE.match(value.strip())
    if m is None:
        return None
    speed = max(float(m.group(1)), float(m.group(2) or 0))
    return speed * MPH_TO_KMH if m.group(3) == "mph" else speed


def _gust_kmh(period: Dict[str, Any]) -> Optional[float]:
    """Forecast gusts: the ``windGust`` quantity, else "gusts as high as 28 mph" in the narrative."""
    gust = quantity(period.get("windGust"), "speed")
    if gust is not None:
        return gust
    m = _GUST_RE.search(period.get("detailedForecast") or "")
    if m is None:
        return None
    speed = float(m.group(1))
    return speed * MPH_TO_KMH if m.group(2).lower() == "mph" else speed


def _period_temperature(period: Dict[str, Any]) -> Optional[float]:
    """Forecast ``temperature`` (int + ``temperatureUnit`` F/C, or a quantity) -> degC."""
    temp = period.get("temperature")
    if isinstance(temp, dict):
        return quantity(temp, "temperature")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        return None
    if str(period.get("temperatureUnit") or "F").strip().upper() == "C":
        return float(temp)
    return (temp - 32.0) * 5.0 / 9.0


def _periods(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    props = body.get("properties")
    periods = props.get("periods") if isinstance(props, dict) else None
    return [p for p in periods or [] if isinstance(p, dict)]


def _text(value: Any) -> Optional[str]:
    """Stripped text, None for null/empty."""
    if value is None:
        return None
    return str(value).strip() or None


def _point_key(location: Location) -> str:
    return "%.4f,%.4f" % (location.latitude, location.longitude)


def map_forecast_period(period: Dict[str, Any]) -> ForecastPeriod:
    """One 12-hour NWS forecast period -> :class:`ForecastPeriod` (day = high, night = low)."""
    temp = _period_temperature(period)
    is_day = period.get("isDaytime")
    night = is_day is False
    return ForecastPeriod(
        name=_text(period.get("name")) or "",
        start=parse_iso(period.get("startTime")),
        end=parse_iso(period.get("endTime")),
        is_daytime=is_day if isinstance(is_day, bool) else None,
        temp_high_c=None if night else temp,
        temp_low_c=temp if night else None,
        description=_text(period.get("shortForecast")) or "",
        precip_chance_pct=quantity(period.get("probabilityOfPrecipitation"), "percent"),
        wind_speed_kmh=parse_wind_speed(period.get("windSpeed")),
        wind_gust_kmh=_gust_kmh(period),
        wind_dir=_text(period.get("windDirection")),  # "" when calm
        detailed=_text(period.get("detailedForecast")),
    )


def map_observation(body: Dict[str, Any]) -> CurrentConditions:
    """``/stations/{id}/observations/latest`` -> :class:`CurrentConditions` (nulls stay None)."""
    props = body.get("properties")
    if not isinstance(props, dict):
        props = {}
    # barometricPressure is the altimeter setting, present in (nearly) every observation; sea-level
    # pressure is null in 5-minute ones and differs by ~0.1 inHg at elevation, so it is only a fallback.
    pressure = quantity(props.get("barometricPressure"), "pressure")
    if pressure is None:
        pressure = quantity(props.get("seaLevelPressure"), "pressure")
    feels_like = quantity(props.get("heatIndex"), "temperature")
    if feels_like is None:
        feels_like = quantity(props.get("windChill"), "temperature")
    wind_speed = quantity(props.get("windSpeed"), "speed")
    wind_dir = quantity(props.get("windDirection"), "angle")
    if wind_dir == 0 and not wind_speed:  # NWS reports calm as 0 deg (north is 360)
        wind_dir = None
    station = _text(props.get("stationId"))
    if station is None and props.get("station"):
        station = str(props["station"]).rstrip("/").rsplit("/", 1)[-1]
    current = CurrentConditions(
        description=_text(props.get("textDescription")),
        temperature_c=quantity(props.get("temperature"), "temperature"),
        feels_like_c=feels_like,
        humidity_pct=quantity(props.get("relativeHumidity"), "percent"),
        dewpoint_c=quantity(props.get("dewpoint"), "temperature"),
        wind_speed_kmh=wind_speed,
        wind_gust_kmh=quantity(props.get("windGust"), "speed"),
        wind_dir_deg=wind_dir,
        pressure_hpa=pressure,
        visibility_km=quantity(props.get("visibility"), "distance"),
        observed_at=parse_iso(props.get("timestamp")),
        station=station,
    )
    if current.temperature_c is None:  # quality control left the values null: decode the METAR itself
        _fill_from_metar(current, props.get("rawMessage"))
    return current


def parse_metar(raw: Any) -> Dict[str, float]:
    """Minimal METAR decoding: any of temperature_c, dewpoint_c, wind_speed_kmh, wind_gust_kmh,
    wind_dir_deg (None for calm/variable, so left out) and pressure_hpa found in ``raw``.

    The remarks' T group ("T02890244") gives tenths of a degree and wins over
    the whole-degree body group ("29/24", "M05/M10").
    """
    out: Dict[str, float] = {}
    if not isinstance(raw, str):
        return out
    tokens = raw.split()
    split = tokens.index("RMK") if "RMK" in tokens else len(tokens)
    for token in tokens[:split]:
        wind = _METAR_WIND_RE.match(token)
        if wind is not None and "wind_speed_kmh" not in out:
            factor = _METAR_SPEED_KMH[wind.group(4)]
            out["wind_speed_kmh"] = int(wind.group(2)) * factor
            if wind.group(3):
                out["wind_gust_kmh"] = int(wind.group(3)) * factor
            if wind.group(1) != "VRB" and int(wind.group(1)) > 0 and int(wind.group(2)) > 0:
                out["wind_dir_deg"] = float(int(wind.group(1)))
            continue
        temps = _METAR_TEMP_RE.match(token)
        if temps is not None and "temperature_c" not in out:
            out["temperature_c"] = _metar_degrees(temps.group(1))
            if temps.group(2):
                out["dewpoint_c"] = _metar_degrees(temps.group(2))
            continue
        pressure = _METAR_PRESSURE_RE.match(token)
        if pressure is not None and "pressure_hpa" not in out:
            value = int(pressure.group(2))
            out["pressure_hpa"] = value / 100.0 * INHG_TO_HPA if pressure.group(1) == "A" else float(value)
    for token in tokens[split + 1:]:
        tenths = _METAR_TENTHS_RE.match(token)
        if tenths is not None:
            out["temperature_c"] = _tenths(tenths.group(1), tenths.group(2))
            if tenths.group(3):
                out["dewpoint_c"] = _tenths(tenths.group(3), tenths.group(4))
            break
    return out


def _metar_degrees(text: str) -> float:
    """ "29" -> 29.0, "M05" -> -5.0."""
    return -float(text[1:]) if text.startswith("M") else float(text)


def _tenths(sign: str, digits: str) -> float:
    """T-group part: sign digit 1 = below zero, then tenths of a degree ("0289" -> 28.9)."""
    value = int(digits) / 10.0
    return -value if sign == "1" else value


def _fill_from_metar(current: CurrentConditions, raw: Any) -> None:
    """Fill the values that are None from the observation's raw METAR (temperature first)."""
    decoded = parse_metar(raw)
    if "temperature_c" not in decoded:
        return
    for name, value in decoded.items():
        if getattr(current, name) is None:
            setattr(current, name, value)
    if current.humidity_pct is None and current.dewpoint_c is not None:
        current.humidity_pct = _relative_humidity(current.temperature_c, current.dewpoint_c)


def _relative_humidity(temp_c: float, dewpoint_c: float) -> float:
    """Relative humidity (%) from temperature and dew point (Magnus formula)."""
    a, b = 17.625, 243.04
    return min(100.0, 100.0 * math.exp(a * dewpoint_c / (b + dewpoint_c) - a * temp_c / (b + temp_c)))


def _zone_name(points: Dict[str, Any], location: Location) -> Optional[str]:
    """IANA zone for a report: /points' zone, except at a marine point (it gives the forecast
    office's zone there) where the geocoder's zone wins."""
    zone = points.get("timeZone")
    if location.timezone and (not zone or points.get("type") not in (None, "land")):
        return location.timezone
    return zone or location.timezone


def vtec_actions(feature: Any) -> List[str]:
    """VTEC action codes ("NEW", "CON", "UPG", ...) of an ``/alerts/active`` feature."""
    props = feature.get("properties") if isinstance(feature, dict) else None
    params = props.get("parameters") if isinstance(props, dict) else None
    vtec = params.get("VTEC") if isinstance(params, dict) else None
    actions: List[str] = []
    for text in vtec if isinstance(vtec, list) else [vtec]:
        m = _VTEC_ACTION_RE.match(text.strip()) if isinstance(text, str) else None
        if m is not None:
            actions.append(m.group(1))
    return actions


def ends_its_event(feature: Any, now: datetime) -> bool:
    """True for a product announcing the end of its event rather than an active hazard.

    Every VTEC action is CAN, EXP or UPG ("The Freeze Watch has been replaced"),
    or messageType is "Cancel".  Without VTEC: urgency "Past" once ``expires``
    has passed.  NWS keeps such products in /alerts/active until the original
    end of the event, with ``ends`` in the future.
    """
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return False
    if str(props.get("messageType") or "").strip().lower() == "cancel":
        return True
    actions = vtec_actions(feature)
    if actions:
        return all(action in END_OF_EVENT_ACTIONS for action in actions)
    if str(props.get("urgency") or "").strip().lower() != "past":
        return False
    expires = parse_iso(props.get("expires"))
    return expires is not None and expires <= now


def _expired_references(params: Dict[str, Any]) -> List[str]:
    """Identifiers in ``parameters.expiredReferences``: space-separated "sender,identifier,sent"
    triples (a string or a list of such strings) naming predecessors that already expired."""
    raw = params.get("expiredReferences")
    ids: List[str] = []
    for text in raw if isinstance(raw, list) else [raw]:
        if not isinstance(text, str):
            continue
        for triple in text.split():
            fields = triple.split(",")
            if len(fields) >= 2 and fields[1].strip():
                ids.append(fields[1].strip())
    return ids


def map_alert(feature: Any) -> Optional[WeatherAlert]:
    """One ``/alerts/active`` feature -> :class:`WeatherAlert`; None unless status is "Actual".

    ``references`` lists ``properties.references`` first, then the predecessors
    that NWS moved to ``parameters.expiredReferences`` once they expired (such a
    follow-up is then typed "Alert", not "Update").
    """
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict) or props.get("status") != "Actual":
        return None
    alert_id = props.get("id") or feature.get("id")
    if not alert_id:
        return None
    params = props.get("parameters") if isinstance(props.get("parameters"), dict) else {}
    nws_headline = params.get("NWSheadline")
    if isinstance(nws_headline, list):
        nws_headline = nws_headline[0] if nws_headline else None
    direct = [str(r["identifier"]) for r in props.get("references") or []
              if isinstance(r, dict) and r.get("identifier")]
    references = tuple(dict.fromkeys(direct + _expired_references(params)))
    return WeatherAlert(
        id=str(alert_id),
        event=_text(props.get("event")) or "Alert",
        headline=_text(nws_headline) or _text(props.get("headline")),
        severity=_text(props.get("severity")),
        urgency=_text(props.get("urgency")),
        certainty=_text(props.get("certainty")),
        message_type=_text(props.get("messageType")),
        onset=parse_iso(props.get("onset")) or parse_iso(props.get("effective")),
        # "expires" is often before the event ends (and before onset): prefer "ends"
        ends=parse_iso(props.get("ends")) or parse_iso(props.get("expires")),
        description=_text(props.get("description")),
        instruction=_text(props.get("instruction")),
        area=_text(props.get("areaDesc")),
        sender=_text(props.get("senderName")) or _text(props.get("sender")),
        references=references,
    )


def _alert_order(alert: WeatherAlert) -> Tuple[int, datetime]:
    return -SEVERITY_RANK.get(alert.severity or "", 0), alert.onset or _FAR_FUTURE
