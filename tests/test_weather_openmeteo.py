"""Tests for meshwx.weather.openmeteo and meshwx.weather.wmo (real Open-Meteo captures, FakeHttp)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.errors import WeatherError
from meshwx.models import Location
from meshwx.weather.openmeteo import CURRENT_FIELDS, DAILY_FIELDS, OpenMeteoProvider
from meshwx.weather.wmo import WMO_CODES, compass_degrees, compass_point, describe

from conftest import FakeHttp, load_fixture, make_config

OM = "https://api.open-meteo.com/v1/forecast"
BOS = Location(42.3576, -71.0684, "Boston, MA", zip_code="02108")
CAPTURE = datetime(2026, 11, 13, 12, 38, 34, tzinfo=timezone.utc)  # 07:38 EST, Friday
MPH = 1.609344


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def provider(http: FakeHttp, now: datetime = CAPTURE) -> OpenMeteoProvider:
    return OpenMeteoProvider(http, make_config(), now_fn=lambda: now)


def metric_body() -> Dict[str, Any]:
    """The 02108 capture relabelled as the metric units the provider asks for."""
    body = load_fixture("openmeteo_forecast_02108.json")
    metric = {"°F": "°C", "mp/h": "km/h", "inch": "mm"}
    for block in ("current_units", "daily_units"):
        body[block] = {k: metric.get(v, v) for k, v in body[block].items()}
    return body


# --------------------------------------------------------------------------- request


def test_request_asks_for_metric_units_and_local_time():
    http = FakeHttp().add_json(OM, "openmeteo_forecast_02108.json")
    provider(http).fetch(BOS, periods=4, include_alerts=True)
    (url, _), = http.calls
    query = parse_qs(urlsplit(url).query)
    assert query["latitude"] == ["42.3576"] and query["longitude"] == ["-71.0684"]
    assert query["temperature_unit"] == ["celsius"] and query["wind_speed_unit"] == ["kmh"]
    assert query["precipitation_unit"] == ["mm"] and query["timezone"] == ["auto"]
    assert query["forecast_days"] == ["4"]
    assert query["current"] == [",".join(CURRENT_FIELDS)] and query["daily"] == [",".join(DAILY_FIELDS)]


@pytest.mark.parametrize("periods, days, expected", [(1, "1", 1), (20, "16", 16), (0, "1", 0)])
def test_forecast_days_follow_periods(periods, days, expected):
    body = metric_body()
    start = datetime(2026, 11, 13)
    body["daily"] = {"time": [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(16)],
                     "weather_code": [0] * 16}
    http = FakeHttp().add_json(OM, body)
    report = provider(http).fetch(BOS, periods=periods, include_alerts=False)
    assert parse_qs(urlsplit(http.calls[0][0]).query)["forecast_days"] == [days]
    assert len(report.forecast) == expected


# --------------------------------------------------------------------------- mapping


def test_captured_imperial_response_is_converted():
    report = provider(FakeHttp().add_json(OM, "openmeteo_forecast_02108.json")).fetch(
        BOS, periods=4, include_alerts=True)
    assert report.provider == "openmeteo" and report.location is BOS and report.fetched_at == CAPTURE
    assert report.timezone == "America/New_York"
    assert report.alerts == [] and report.alerts_supported is False and report.notes == []

    cur = report.current
    assert (cur.station, cur.description) == ("Open-Meteo", "Moderate snow")  # weather_code 73
    assert cur.temperature_c == pytest.approx(f_to_c(35.3))
    assert cur.feels_like_c == pytest.approx(f_to_c(26.8))  # apparent_temperature
    assert (cur.humidity_pct, cur.pressure_hpa, cur.wind_dir_deg) == (92, 1001.8, 3)  # pressure_msl
    assert cur.wind_speed_kmh == pytest.approx(11.3 * MPH) and cur.wind_gust_kmh == pytest.approx(25.7 * MPH)
    assert cur.observed_at == datetime(2026, 11, 13, 12, 30, tzinfo=timezone.utc)  # "07:30" EST
    assert cur.dewpoint_c is None and cur.visibility_km is None

    assert [p.name for p in report.forecast] == ["Today", "Sat", "Sun", "Mon"]
    today, saturday = report.forecast[:2]
    assert today.temp_high_c == pytest.approx(f_to_c(45.9)) and today.temp_low_c == pytest.approx(f_to_c(32.9))
    assert today.precip_mm == pytest.approx(0.264 * 25.4) and today.precip_chance_pct == 97
    assert today.wind_speed_kmh == pytest.approx(13.3 * MPH) and today.wind_gust_kmh == pytest.approx(30.4 * MPH)
    assert (today.wind_dir, today.description, today.is_daytime, today.detailed) == ("N", "Moderate snow", None, None)
    assert today.start == datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)
    assert today.end == datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)
    assert (saturday.wind_dir, saturday.description) == ("WNW", "Overcast")  # 302 deg, code 3
    assert report.forecast[3].description == "Clear sky"


def test_metric_response_maps_one_to_one():
    report = provider(FakeHttp().add_json(OM, metric_body())).fetch(BOS, periods=2, include_alerts=False)
    assert report.current.temperature_c == 35.3 and report.current.wind_speed_kmh == 11.3
    assert report.forecast[0].precip_mm == 0.264 and report.forecast[0].temp_low_c == 32.9
    assert len(report.forecast) == 2


def test_missing_or_unknown_units():
    body = metric_body()
    del body["current_units"]  # no labels: the requested metric units are assumed
    body["daily_units"]["temperature_2m_max"] = "K"  # unknown label: never guess
    body["daily_units"]["temperature_2m_min"] = "km/h"  # label of another kind
    report = provider(FakeHttp().add_json(OM, body)).fetch(BOS, periods=1, include_alerts=False)
    assert report.current.temperature_c == 35.3
    assert report.forecast[0].temp_high_c is None and report.forecast[0].temp_low_c is None
    assert report.forecast[0].precip_mm == 0.264


@pytest.mark.parametrize("now, names", [
    (datetime(2026, 11, 14, 4, 59, tzinfo=timezone.utc), ["Today", "Sat", "Sun", "Mon"]),  # 23:59 EST Fri
    (datetime(2026, 11, 14, 5, 30, tzinfo=timezone.utc), ["Today", "Sun", "Mon"]),  # 00:30 EST Sat
])
def test_today_is_the_local_date_and_past_days_are_dropped(now, names):
    report = provider(FakeHttp().add_json(OM, metric_body()), now=now).fetch(BOS, periods=4, include_alerts=False)
    assert [p.name for p in report.forecast] == names


def test_times_are_localized_with_zoneinfo_across_dst():
    body = {  # US DST ended Sunday 2026-11-01; utc_offset_seconds is EDT for the whole response
        "timezone": "America/New_York", "utc_offset_seconds": -14400,
        "current": {"time": "2026-10-31T12:00", "temperature_2m": 10.0, "weather_code": 97},
        "daily": {"time": ["2026-10-31", "2026-11-01", "2026-11-02"], "weather_code": [0, 1, 2],
                  "temperature_2m_max": [12.0, 13.0, 14.0], "temperature_2m_min": [5.0, 6.0, None]},
    }
    now = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    report = provider(FakeHttp().add_json(OM, body), now=now).fetch(BOS, periods=3, include_alerts=False)
    assert report.current.observed_at == datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    assert report.current.description == "Heavy thunderstorm"
    saturday, sunday, monday = report.forecast
    assert [p.name for p in report.forecast] == ["Today", "Sun", "Mon"]
    assert saturday.start.utcoffset() == timedelta(hours=-4) and monday.start.utcoffset() == timedelta(hours=-5)
    utc = timezone.utc  # same-tzinfo subtraction would ignore the offset change
    assert sunday.end.astimezone(utc) - sunday.start.astimezone(utc) == timedelta(hours=25)
    assert monday.temp_low_c is None and monday.description == "Partly cloudy"
    assert saturday.precip_mm is None and saturday.wind_dir is None  # fields not in the response


def test_unknown_timezone_falls_back_to_the_fixed_offset():
    body = {"timezone": "Nowhere/Special", "utc_offset_seconds": 3600,
            "current": {"time": "2026-11-13T07:30", "temperature_2m": 1.0}}
    report = provider(FakeHttp().add_json(OM, body)).fetch(BOS, periods=1, include_alerts=False)
    assert report.current.observed_at == datetime(2026, 11, 13, 6, 30, tzinfo=timezone.utc)
    assert report.current.description is None and report.forecast == []


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize("fixture, status", [("openmeteo_error_400.json", 400), ("openmeteo_error_503.json", 503)])
def test_http_errors_raise_weather_error(fixture, status):
    http = FakeHttp().add_json(OM, fixture, status=status)
    with pytest.raises(WeatherError) as ei:
        provider(http).fetch(BOS, periods=2, include_alerts=False)
    assert ei.value.user_message == "Weather unavailable, try later"


@pytest.mark.parametrize("raw", [b"", b"<html>overloaded</html>"])
def test_invalid_json_raises_weather_error(raw):
    with pytest.raises(WeatherError):
        provider(FakeHttp().add_bytes(OM, raw)).fetch(BOS, periods=2, include_alerts=False)


@pytest.mark.parametrize("body", [
    {"error": True, "reason": "The service is overloaded"},
    {"generationtime_ms": 0.1},  # neither current nor daily
    {"current": {"time": "2026-11-13T07:30"}, "daily": {"time": []}},
    ["not", "an", "object"],
])
def test_unusable_bodies_raise_weather_error(body):
    with pytest.raises(WeatherError):
        provider(FakeHttp().add_json(OM, body)).fetch(BOS, periods=2, include_alerts=False)


# --------------------------------------------------------------------------- wmo


@pytest.mark.parametrize("code, text", [
    (0, "Clear sky"), (2, "Partly cloudy"), (3, "Overcast"), (45, "Fog"), (61, "Slight rain"),
    (73, "Moderate snow"), (73.0, "Moderate snow"), (95, "Thunderstorm"), (97, "Heavy thunderstorm"),
    (99, "Thunderstorm with heavy hail"), (4, "?"), (100, "?"), (-1, "?"), (73.5, "?"), (True, "?"),
    ("x", "?"), (None, None),
])
def test_wmo_describe(code, text):
    assert describe(code) == text


def test_wmo_table_covers_the_documented_codes():
    documented = {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77,
                  80, 81, 82, 85, 86, 95, 96, 97, 99}
    assert set(WMO_CODES) == documented
    assert all(text and text.isascii() and len(text) <= 30 for text in WMO_CODES.values())


@pytest.mark.parametrize("deg, point", [
    (0, "N"), (11.24, "N"), (11.25, "NNE"), (90, "E"), (302, "WNW"), (348.74, "NNW"), (350, "N"),
    (360, "N"), (-90, "W"), (None, None), (float("nan"), None), (float("inf"), None),
])
def test_compass_point(deg, point):
    assert compass_point(deg) == point


@pytest.mark.parametrize("point, deg", [("N", 0.0), ("nw", 315.0), (" SSE ", 157.5), ("", None),
                                        (None, None), ("VRB", None)])
def test_compass_degrees(point, deg):
    assert compass_degrees(point) == deg
