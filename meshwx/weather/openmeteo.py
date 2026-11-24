"""Open-Meteo provider (api.open-meteo.com: worldwide model data, no key, no alerts).

One ``/v1/forecast`` request returns current conditions plus daily forecasts.
Metric units are requested so values map 1:1 onto :mod:`meshwx.models`; the
response's ``current_units`` / ``daily_units`` are still honoured, so an
answer in other units (Fahrenheit, "mp/h", "inch") is converted, never misread.

Times come back as local wall-clock strings without an offset
("2026-11-13T07:30", dates "2026-11-13").  They are localized with
``ZoneInfo`` of the response's IANA ``timezone``: ``utc_offset_seconds`` is a
single value that ignores DST changes inside the forecast range.

The weather code is model output (it said "snow" while Boston's airport
reported light rain), so the service prefers NWS observations when it has them.
Open-Meteo is free for non-commercial use (CC BY 4.0).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..config import Config
from ..errors import WeatherError
from ..httpclient import HttpClient, HttpError
from ..models import CurrentConditions, ForecastPeriod, Location, WeatherReport
from ..timeutil import get_tz, parse_iso, utcnow
from .base import WeatherProvider
from .wmo import compass_point, describe

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
STATION_NAME = "Open-Meteo"
MAX_FORECAST_DAYS = 16
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # fixed: strftime("%a") follows the locale

# Only variables that map onto the models (the request cost grows with the variable count).
CURRENT_FIELDS = ("temperature_2m", "relative_humidity_2m", "apparent_temperature", "weather_code",
                  "pressure_msl", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")
DAILY_FIELDS = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max",
                "precipitation_sum", "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant")

#: Open-Meteo unit label -> (kind, converter to the unit used in meshwx.models)
_UNITS: Dict[str, Tuple[str, Callable[[float], float]]] = {
    "°C": ("temperature", lambda v: v),
    "°F": ("temperature", lambda v: (v - 32.0) * 5.0 / 9.0),
    "km/h": ("speed", lambda v: v),
    "mp/h": ("speed", lambda v: v * 1.609344),
    "m/s": ("speed", lambda v: v * 3.6),
    "kn": ("speed", lambda v: v * 1.852),
    "mm": ("precipitation", lambda v: v),
    "inch": ("precipitation", lambda v: v * 25.4),
    "hPa": ("pressure", lambda v: v),
    "%": ("percent", lambda v: v),
    "°": ("angle", lambda v: v),
}


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo forecast API.  ``now_fn`` is injectable so tests can freeze "today"."""

    name = "openmeteo"
    supports_alerts = False

    def __init__(self, http: HttpClient, cfg: Config, *,
                 now_fn: Callable[[], datetime] = utcnow) -> None:
        self._http = http
        self._now = now_fn

    def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
        """Current conditions + ``periods`` daily forecasts ("Today", "Sat", ...); never alerts."""
        now = self._now()
        body = self._request(location, periods)
        tz_name = _text(body.get("timezone")) or location.timezone
        zone = _zone(tz_name, body.get("utc_offset_seconds"))
        current = map_current(body.get("current"), body.get("current_units"), zone)
        forecast = map_daily(body.get("daily"), body.get("daily_units"), zone, now.astimezone(zone).date())
        forecast = forecast[:max(periods, 0)]
        if current is None and not forecast:
            raise WeatherError("Open-Meteo returned no usable data for %s" % location.name)
        return WeatherReport(location=location, provider=self.name, fetched_at=now, timezone=tz_name,
                             current=current, forecast=forecast)

    def _request(self, location: Location, periods: int) -> Dict[str, Any]:
        params = {
            "latitude": "%.4f" % location.latitude,
            "longitude": "%.4f" % location.longitude,
            "current": ",".join(CURRENT_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto",
            "forecast_days": min(max(periods, 1), MAX_FORECAST_DAYS),
        }
        try:
            body = self._http.get_json(FORECAST_URL, params=params)
        except (HttpError, ValueError) as exc:  # 4xx/5xx ("overloaded" 503s happen), network, non-JSON body
            raise WeatherError("Open-Meteo request failed: %s" % exc) from exc
        if not isinstance(body, dict):
            raise WeatherError("Open-Meteo: unexpected %s response" % type(body).__name__)
        if body.get("error"):
            raise WeatherError("Open-Meteo error: %s" % body.get("reason"))
        return body


# ---------------------------------------------------------------------- mapping helpers


def map_current(current: Any, units: Any, zone: tzinfo) -> Optional[CurrentConditions]:
    """``current`` block -> :class:`CurrentConditions`; None without temperature and weather code."""
    if not isinstance(current, dict):
        return None
    temperature = _value(current, units, "temperature_2m", "temperature")
    description = describe(current.get("weather_code"))
    if temperature is None and description is None:
        return None
    return CurrentConditions(
        description=description,
        temperature_c=temperature,
        feels_like_c=_value(current, units, "apparent_temperature", "temperature"),
        humidity_pct=_value(current, units, "relative_humidity_2m", "percent"),
        wind_speed_kmh=_value(current, units, "wind_speed_10m", "speed"),
        wind_gust_kmh=_value(current, units, "wind_gusts_10m", "speed"),
        wind_dir_deg=_value(current, units, "wind_direction_10m", "angle"),
        pressure_hpa=_value(current, units, "pressure_msl", "pressure"),
        observed_at=_localize(current.get("time"), zone),
        station=STATION_NAME,
    )


def map_daily(daily: Any, units: Any, zone: tzinfo, today: date) -> List[ForecastPeriod]:
    """``daily`` parallel arrays -> one :class:`ForecastPeriod` per local day from ``today`` on.

    Days before ``today`` (a response cached across midnight) are dropped.
    """
    if not isinstance(daily, dict):
        return []
    result: List[ForecastPeriod] = []
    for i, text in enumerate(daily.get("time") or []):
        start = _localize(text, zone)
        if start is None or start.date() < today:
            continue
        day = start.date()
        nxt = day + timedelta(days=1)
        result.append(ForecastPeriod(
            name="Today" if day == today else WEEKDAYS[day.weekday()],
            start=start,
            end=datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone),
            temp_high_c=_value(daily, units, "temperature_2m_max", "temperature", i),
            temp_low_c=_value(daily, units, "temperature_2m_min", "temperature", i),
            description=describe(_item(daily, "weather_code", i)) or "",
            precip_chance_pct=_value(daily, units, "precipitation_probability_max", "percent", i),
            precip_mm=_value(daily, units, "precipitation_sum", "precipitation", i),
            wind_speed_kmh=_value(daily, units, "wind_speed_10m_max", "speed", i),
            wind_gust_kmh=_value(daily, units, "wind_gusts_10m_max", "speed", i),
            wind_dir=compass_point(_value(daily, units, "wind_direction_10m_dominant", "angle", i)),
        ))
    return result


def _item(block: Mapping[str, Any], key: str, index: int) -> Any:
    values = block.get(key)
    return values[index] if isinstance(values, list) and index < len(values) else None


def _value(block: Mapping[str, Any], units: Any, key: str, kind: str,
           index: Optional[int] = None) -> Optional[float]:
    """Number ``block[key]`` (or its element ``index``) in the model unit of ``kind``; None if missing.

    Without a unit label the metric unit we asked for is assumed; an unknown
    label (or one of another kind) gives None rather than a wrong number.
    """
    value = block.get(key) if index is None else _item(block, key, index)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    unit = units.get(key) if isinstance(units, dict) else None
    if unit is None:
        return float(value)
    conversion = _UNITS.get(str(unit).strip())
    if conversion is None or conversion[0] != kind:
        log.debug("Open-Meteo: unexpected unit %r for %s", unit, key)
        return None
    return conversion[1](float(value))


def _localize(text: Any, zone: tzinfo) -> Optional[datetime]:
    """Local wall-clock time without offset ("2026-11-13T07:30" or "2026-11-13") -> aware in ``zone``."""
    dt = parse_iso(text)  # offset-less input comes back labelled UTC: relabel, don't convert
    return dt.replace(tzinfo=zone) if dt is not None else None


def _zone(name: Optional[str], utc_offset_seconds: Any) -> tzinfo:
    """``ZoneInfo(name)``; the response's fixed offset (or UTC) only if the name is unknown."""
    zone = get_tz(name)
    if zone is not None:
        return zone
    if isinstance(utc_offset_seconds, (int, float)) and not isinstance(utc_offset_seconds, bool):
        return timezone(timedelta(seconds=utc_offset_seconds))
    return timezone.utc


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None
