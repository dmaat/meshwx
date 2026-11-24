"""Tests for meshwx.weather.service.WeatherService and the weather provider factory."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from meshwx.errors import ConfigError, GeocodeError, WeatherError
from meshwx.geocode import ZipGeocoder
from meshwx.httpclient import HttpError
from meshwx.models import CurrentConditions, ForecastPeriod, Location, WeatherAlert, WeatherReport
from meshwx.state import RuntimeSettings
from meshwx.weather import NwsProvider, OpenMeteoProvider, WeatherService, create_weather_provider
from meshwx.weather.base import WeatherProvider

from conftest import FakeHttp, make_config

ZIPPO = "https://api.zippopotam.us/"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
NWS = "https://api.weather.gov"
OM = "https://api.open-meteo.com/v1/forecast"
NOW = datetime(2026, 11, 13, 12, 38, 18, tzinfo=timezone.utc)  # capture time of the 02108 fixtures


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class StubProvider(WeatherProvider):
    """Deterministic provider that records its calls; set ``error`` to make ``fetch`` raise."""

    def __init__(self, name: str, *, supports_alerts: bool = False, current: bool = True,
                 forecast: int = 9) -> None:
        self.name = name
        self.supports_alerts = supports_alerts
        self.has_current = current
        self.max_periods = forecast
        self.error: Optional[BaseException] = None
        self.alert_error: Optional[BaseException] = None
        self.alerts: List[WeatherAlert] = []
        self.calls: List[Tuple[Location, int, bool]] = []
        self.alert_calls: List[Location] = []

    def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
        self.calls.append((location, periods, include_alerts))
        if self.error is not None:
            raise self.error
        with_alerts = include_alerts and self.supports_alerts
        return WeatherReport(
            location=location, provider=self.name, fetched_at=NOW, timezone="America/New_York",
            current=CurrentConditions(temperature_c=1.0, station=self.name) if self.has_current else None,
            forecast=[ForecastPeriod(name="%s %d" % (self.name, i)) for i in range(min(periods, self.max_periods))],
            alerts=list(self.alerts) if with_alerts else [], alerts_supported=with_alerts)

    def fetch_alerts(self, location: Location) -> List[WeatherAlert]:
        self.alert_calls.append(location)
        if self.alert_error is not None:
            raise self.alert_error
        return list(self.alerts)


def geo_http() -> FakeHttp:
    return (FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
            .add_json(ZIPPO + "us/99501", "zippopotam_99501.json"))


class Env:
    """A WeatherService wired to stub providers, the real ZipGeocoder on FakeHttp and a fake clock."""

    def __init__(self, tmp_path: Any, overrides: Optional[Dict[str, Any]] = None, *,
                 primary: Optional[StubProvider] = None, http: Optional[FakeHttp] = None) -> None:
        self.cfg = make_config(dict(overrides or {}, state_dir=str(tmp_path)))
        self.settings = RuntimeSettings(self.cfg, tmp_path)
        self.http = http if http is not None else geo_http()
        self.clock = Clock()
        wcfg = self.cfg.weather
        self.primary = primary or StubProvider(wcfg.provider, supports_alerts=wcfg.provider == "nws")
        self.fallback = StubProvider(wcfg.fallback_provider or "openmeteo")
        providers = {p.name: p for p in (self.fallback, self.primary)}
        self.svc = WeatherService(self.cfg, self.settings, self.http, geocoder=ZipGeocoder(self.http),
                                  providers=providers, clock=self.clock)


# --------------------------------------------------------------------------- locations


def test_home_location_geocodes_the_zip(tmp_path):
    env = Env(tmp_path)
    assert env.svc.home_location() == Location(42.3576, -71.0684, "Boston, MA", zip_code="02108", country="US")


def test_config_name_applies_only_until_the_zip_is_overridden(tmp_path):
    env = Env(tmp_path, {"location": {"name": "Home"}})
    home = env.svc.home_location()
    assert (home.name, home.latitude) == ("Home", 42.3576)
    env.settings.set_zip("99501", "!a1b2c3d4")
    home = env.svc.home_location()
    assert (home.name, home.zip_code) == ("Anchorage, AK", "99501")


def test_config_latlon_wins_unless_the_zip_is_overridden(tmp_path):
    env = Env(tmp_path, {"location": {"latitude": 42.36, "longitude": -71.06}})
    home = env.svc.home_location()
    assert (home.latitude, home.longitude, home.name, home.zip_code) == (42.36, -71.06, "Boston, MA", "02108")
    env.settings.set_zip("99501", "test")
    assert env.svc.home_location().latitude == 61.2116
    env.settings.set_zip("02108", "test")  # back to the config value: no longer an override
    assert env.svc.home_location().latitude == 42.36


@pytest.mark.parametrize("location, name", [
    ({"zip_code": "", "latitude": 42.36, "longitude": -71.06, "name": "Home"}, "Home"),
    ({"zip_code": "", "latitude": 42.36, "longitude": -71.06}, "42.360,-71.060"),
    ({"latitude": 42.36, "longitude": -71.06, "name": "Shack"}, "Shack"),
])
def test_latlon_home_named_without_geocoding(tmp_path, location, name):
    env = Env(tmp_path, {"location": location})
    home = env.svc.home_location()
    assert home.name == name and (home.latitude, home.longitude) == (42.36, -71.06)
    assert env.http.calls == []


def test_latlon_home_name_retried_after_geocoding_failure(tmp_path):
    http = FakeHttp().add_error(ZIPPO, status=503).add_error(OM_GEO, status=503)
    env = Env(tmp_path, {"location": {"latitude": 42.36, "longitude": -71.06}}, http=http)
    assert env.svc.home_location().name == "42.360,-71.060"
    calls = len(http.calls)
    env.svc.home_location()
    assert len(http.calls) == calls  # placeholder kept for a while: no retry storm
    http.routes = geo_http().routes  # network is back
    env.clock.t += 15 * 60
    assert env.svc.home_location().name == "Boston, MA"


def test_location_for_zip(tmp_path):
    env = Env(tmp_path, {"location": {"name": "Home"}})
    assert env.svc.location_for_zip("02108-1234").name == "Home"  # the home ZIP is the home location
    assert env.svc.location_for_zip("99501").name == "Anchorage, AK"
    with pytest.raises(GeocodeError) as ei:
        env.svc.location_for_zip("123")
    assert ei.value.user_message == "ZIP must be 5 digits"


def test_unknown_zip_raises_geocode_error(tmp_path):
    http = geo_http().add_json(ZIPPO, {}, status=404).add_json(OM_GEO, {"generationtime_ms": 0.1})
    env = Env(tmp_path, http=http)
    with pytest.raises(GeocodeError) as ei:
        env.svc.get_report("12345")
    assert ei.value.user_message == "ZIP 12345 not found"
    assert env.primary.calls == []


# --------------------------------------------------------------------------- reports and caching


def test_report_is_cached_for_cache_minutes(tmp_path):
    env = Env(tmp_path, {"weather": {"cache_minutes": 10}})
    first = env.svc.get_report()
    assert env.svc.get_report() == first and len(env.primary.calls) == 1
    env.clock.t += 10 * 60 + 1
    env.svc.get_report()
    assert len(env.primary.calls) == 2
    env.svc.get_report(force=True)
    assert len(env.primary.calls) == 3


def test_uses_configured_periods_and_alert_setting(tmp_path):
    env = Env(tmp_path, {"weather": {"forecast_periods": 6, "include_alerts": False}})
    report = env.svc.get_report()
    assert env.primary.calls[0][1:] == (6, False) and len(report.forecast) == 6
    assert report.provider == "nws" and report.notes == [] and report.stale is False
    assert env.fallback.calls == []  # a complete primary report needs no help


def test_home_zip_shares_the_home_cache_entry(tmp_path):
    env = Env(tmp_path)
    env.svc.get_report()
    env.svc.get_report("02108")
    assert len(env.primary.calls) == 1
    env.svc.get_report("99501")
    assert len(env.primary.calls) == 2


def test_cache_key_is_location_rounded_to_3_decimals(tmp_path):
    env = Env(tmp_path, {"location": {"latitude": 42.35764, "longitude": -71.06836}})
    env.svc.get_report()
    env.settings.set_zip("99501", "test")  # home moves away; 02108 becomes an ordinary ZIP ...
    report = env.svc.get_report("02108")  # ... at 42.3576,-71.0684: same 3-decimal cell as the old home
    assert len(env.primary.calls) == 1
    assert report.location.name == "Boston, MA"  # served from cache, labelled with the asked location


def test_fallback_when_the_primary_fails(tmp_path):
    env = Env(tmp_path)
    env.primary.error = WeatherError("NWS down")
    report = env.svc.get_report()
    assert report.provider == "openmeteo" and report.notes == ["via Open-Meteo"]
    env.svc.get_report()  # the fallback report is cached like any other
    assert len(env.primary.calls) == 1 and len(env.fallback.calls) == 1


@pytest.mark.parametrize("error", [HttpError("boom", NWS, 500), KeyError("properties")])
def test_any_primary_failure_uses_the_fallback(tmp_path, error):
    env = Env(tmp_path)
    env.primary.error = error
    assert env.svc.get_report().provider == "openmeteo"


def test_no_fallback_configured(tmp_path):
    env = Env(tmp_path, {"weather": {"fallback_provider": ""}})
    env.primary.error = WeatherError("marine point", user_message="No NWS data here")
    with pytest.raises(WeatherError) as ei:
        env.svc.get_report()
    assert ei.value.user_message == "No NWS data here"
    assert env.fallback.calls == []


def test_everything_fails_without_a_cached_report(tmp_path):
    env = Env(tmp_path)
    env.primary.error = WeatherError("marine point", user_message="No NWS data here")
    env.fallback.error = HttpError("down", OM, 503)
    with pytest.raises(WeatherError) as ei:
        env.svc.get_report()
    assert ei.value.user_message == "Weather unavailable, try later"


def test_stale_report_served_for_up_to_3_hours(tmp_path):
    env = Env(tmp_path)
    fresh = env.svc.get_report()
    env.primary.error = env.fallback.error = WeatherError("down")
    env.clock.t += 30 * 60
    stale = env.svc.get_report()
    assert stale.stale is True and stale.notes == ["cached 30m"]
    assert stale.current == fresh.current and stale.fetched_at == fresh.fetched_at
    assert fresh.stale is False and fresh.notes == []  # the cached report itself is untouched
    env.clock.t += 150 * 60  # exactly 3 h after the fetch
    assert env.svc.get_report().notes == ["cached 180m"]
    env.clock.t += 1
    with pytest.raises(WeatherError):
        env.svc.get_report()


def test_missing_current_is_filled_from_the_fallback(tmp_path):
    env = Env(tmp_path, primary=StubProvider("nws", supports_alerts=True, current=False))
    report = env.svc.get_report()
    assert report.provider == "nws" and report.current.station == "openmeteo"
    assert report.notes == ["obs via Open-Meteo"]
    assert report.forecast[0].name == "nws 0" and report.alerts_supported
    assert env.fallback.calls[0][1:] == (0, False)  # only current conditions are needed; never alerts


def test_missing_forecast_is_filled_from_the_fallback(tmp_path):
    env = Env(tmp_path, primary=StubProvider("nws", supports_alerts=True, forecast=0))
    report = env.svc.get_report()
    assert report.current.station == "nws" and report.forecast[0].name == "openmeteo 0"
    assert report.notes == ["fc via Open-Meteo"]


def test_failed_fill_keeps_the_primary_report(tmp_path):
    env = Env(tmp_path, primary=StubProvider("nws", supports_alerts=True, current=False))
    env.fallback.error = WeatherError("down")
    report = env.svc.get_report()
    assert report.provider == "nws" and report.current is None and report.notes == []


def test_invalidate_clears_the_caches(tmp_path):
    env = Env(tmp_path)
    env.svc.get_report()
    env.svc.invalidate()
    env.svc.get_report()
    assert len(env.primary.calls) == 2
    env.primary.error = env.fallback.error = WeatherError("down")
    env.svc.invalidate()
    with pytest.raises(WeatherError):  # no stale copy left either
        env.svc.get_report()


def test_concurrent_requests_share_one_fetch(tmp_path):
    gate = threading.Event()

    class SlowProvider(StubProvider):
        def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
            gate.wait(5)
            return super().fetch(location, periods=periods, include_alerts=include_alerts)

    env = Env(tmp_path, primary=SlowProvider("nws", supports_alerts=True))
    env.svc.home_location()
    results: List[WeatherReport] = []
    threads = [threading.Thread(target=lambda: results.append(env.svc.get_report())) for _ in range(5)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 5 and len(env.primary.calls) == 1


# --------------------------------------------------------------------------- alerts


def test_alerts_supported_follows_the_primary_provider(tmp_path):
    assert Env(tmp_path).svc.alerts_supported() is True
    env = Env(tmp_path, {"weather": {"provider": "openmeteo", "fallback_provider": "nws"}})
    assert env.svc.alerts_supported() is False


def test_get_alerts_uses_the_primary_and_reports_the_timezone(tmp_path):
    env = Env(tmp_path)
    alert = WeatherAlert(id="a1", event="Wind Advisory", severity="Moderate")
    env.primary.alerts = [alert]
    assert env.svc.get_alerts() == ([alert], None)  # time zone unknown before the first report
    env.svc.get_report()
    assert env.svc.get_alerts() == ([alert], "America/New_York")
    assert env.primary.alert_calls == [env.svc.home_location()] * 2


@pytest.mark.parametrize("error", [WeatherError("x", user_message="Alerts unavailable, try later"),
                                   HttpError("x", NWS, 500), KeyError("features")])
def test_get_alerts_failure_raises_weather_error(tmp_path, error):
    env = Env(tmp_path)
    env.primary.alert_error = error
    with pytest.raises(WeatherError) as ei:
        env.svc.get_alerts()
    assert ei.value.user_message == "Alerts unavailable, try later"


def test_get_alerts_without_an_alert_feed(tmp_path):
    env = Env(tmp_path, {"weather": {"provider": "openmeteo"}})
    assert env.svc.get_alerts() == ([], None)
    assert env.primary.alert_calls == []


def test_get_alerts_when_home_cannot_be_geocoded(tmp_path):
    env = Env(tmp_path, http=FakeHttp().add_error(ZIPPO, status=503).add_error(OM_GEO, status=503))
    with pytest.raises(WeatherError) as ei:
        env.svc.get_alerts()
    assert ei.value.user_message == "ZIP lookup failed, try later"


class ZonedProvider(StubProvider):
    """A primary that knows the zone without a report (like NwsProvider.timezone_for)."""

    def __init__(self, *args: Any, zone: Any = "America/Chicago", **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.zone = zone
        self.zone_calls = 0

    def timezone_for(self, location: Location) -> Optional[str]:
        self.zone_calls += 1
        if isinstance(self.zone, BaseException):
            raise self.zone
        return self.zone


def test_get_alerts_zone_known_before_the_first_report(tmp_path):
    env = Env(tmp_path, primary=ZonedProvider("nws", supports_alerts=True))
    assert env.svc.get_alerts() == ([], "America/Chicago")  # from the provider (NWS: cached /points)
    env.svc.get_report()
    assert env.svc.get_alerts() == ([], "America/New_York")  # the report's zone
    assert env.primary.zone_calls == 1
    env.primary.alert_error = WeatherError("down", user_message="Alerts unavailable, try later")
    env.svc.invalidate()
    with pytest.raises(WeatherError):
        env.svc.get_alerts()
    assert env.primary.zone_calls == 1  # no zone lookup for a failed alert check


def test_get_alerts_zone_lookup_failure_is_not_fatal(tmp_path):
    env = Env(tmp_path, primary=ZonedProvider("nws", supports_alerts=True, zone=KeyError("timeZone")))
    alert = WeatherAlert(id="a1", event="Wind Advisory")
    env.primary.alerts = [alert]
    assert env.svc.get_alerts() == ([alert], None)


# --------------------------------------------------------------------------- one alert list for every reader

BOSTON_KEY = (42.358, -71.068)


def test_newer_alert_check_replaces_the_cached_alert_list(tmp_path):
    env = Env(tmp_path)
    assert env.svc.get_report().alerts == []  # e.g. a scheduled broadcast caches a report without alerts
    warning = WeatherAlert(id="w1", event="Tornado Warning", severity="Extreme")
    env.primary.alerts = [warning]
    env.clock.t += 60
    assert env.svc.get_alerts()[0] == [warning]  # the watcher broadcasts it
    env.clock.t += 30
    report = env.svc.get_report()  # `!alerts` answers from the cache ...
    assert report.alerts == [warning] and report.alerts_supported and report.alerts_error is None
    assert env.svc.get_report("02108").alerts == [warning]  # the home ZIP is the home location
    assert len(env.primary.calls) == 1  # ... without a new fetch
    env.primary.alerts = []
    assert env.svc.get_report("99501").alerts == []  # another place keeps its own alerts
    assert env.svc._reports.get_stale(BOSTON_KEY, float("inf"))[0].alerts == []  # cached report untouched


def test_older_alert_check_does_not_replace_a_newer_report(tmp_path):
    env = Env(tmp_path)
    old = WeatherAlert(id="old", event="Wind Advisory")
    env.primary.alerts = [old]
    env.svc.get_alerts()
    env.clock.t += 30
    env.primary.alerts = []  # the advisory was cancelled before the report was fetched
    assert env.svc.get_report().alerts == []


def test_alert_overlay_needs_alerts_in_reports(tmp_path):
    env = Env(tmp_path, {"weather": {"include_alerts": False}})
    env.primary.alerts = [WeatherAlert(id="w1", event="Tornado Warning")]
    env.svc.get_alerts()
    env.clock.t += 1
    report = env.svc.get_report()
    assert report.alerts == [] and not report.alerts_supported


def test_alerts_that_have_ended_are_not_served_from_the_cache(tmp_path):
    env = Env(tmp_path)
    ending = WeatherAlert(id="a1", event="Severe Thunderstorm Warning", ends=NOW + timedelta(minutes=5))
    lasting = WeatherAlert(id="a2", event="Flood Watch")  # no end known
    env.primary.alerts = [ending, lasting]
    assert env.svc.get_report().alerts == [ending, lasting]
    env.clock.t += 5 * 60  # cached report, fetched_at + 5 min: the warning has just ended
    assert env.svc.get_report().alerts == [lasting]
    env.svc.get_alerts()  # newer alert list (both again) - the ended one is still dropped
    env.clock.t += 1
    assert [a.id for a in env.svc.get_report().alerts] == ["a2"]
    env.primary.error = env.fallback.error = WeatherError("down")
    env.clock.t += 60 * 60
    stale = env.svc.get_report()
    assert stale.stale and stale.alerts == [lasting]


# --------------------------------------------------------------------------- failure backoff


def test_failed_refresh_is_not_retried_during_the_backoff(tmp_path):
    env = Env(tmp_path)
    env.svc.get_report()
    env.primary.error = env.fallback.error = WeatherError("down")
    env.clock.t += 11 * 60
    assert env.svc.get_report().notes == ["cached 11m"]
    calls = (len(env.primary.calls), len(env.fallback.calls))
    assert calls == (2, 1)
    env.clock.t += 119
    stale = env.svc.get_report()  # within 2 min: no network I/O at all
    assert stale.stale and stale.notes == ["cached 12m"]
    assert (len(env.primary.calls), len(env.fallback.calls)) == calls
    assert env.svc.get_alerts() == ([], "America/New_York")  # the alert watcher is not held back
    env.clock.t += 1
    env.svc.get_report()  # retried after 2 min; failing again doubles the backoff to 4 min
    assert len(env.primary.calls) == 3
    env.clock.t += 239
    env.svc.get_report()
    assert len(env.primary.calls) == 3
    env.svc.get_report(force=True)  # force is never held back
    assert len(env.primary.calls) == 4
    env.primary.error = env.fallback.error = None
    env.clock.t += 8 * 60
    assert not env.svc.get_report().stale  # success resets the backoff
    env.primary.error = env.fallback.error = WeatherError("down")
    env.clock.t += 11 * 60
    env.svc.get_report()
    env.clock.t += 121
    env.svc.get_report()
    assert len(env.primary.calls) == 7  # back to a 2 min backoff


def test_backoff_without_a_stale_copy_repeats_the_error(tmp_path):
    env = Env(tmp_path)
    env.primary.error = WeatherError("marine point", user_message="No NWS data here")
    env.fallback.error = HttpError("down", OM, 503)
    for _ in range(3):
        with pytest.raises(WeatherError) as ei:
            env.svc.get_report()
        assert ei.value.user_message == "Weather unavailable, try later"
        env.clock.t += 30
    assert (len(env.primary.calls), len(env.fallback.calls)) == (1, 1)
    env.svc.invalidate()  # e.g. a new home ZIP: try at once
    with pytest.raises(WeatherError):
        env.svc.get_report()
    assert len(env.primary.calls) == 2


def test_backoff_is_per_location(tmp_path):
    env = Env(tmp_path)
    env.primary.error = env.fallback.error = WeatherError("down")
    with pytest.raises(WeatherError):
        env.svc.get_report("99501")
    env.primary.error = env.fallback.error = None
    assert env.svc.get_report().provider == "nws"  # another place is fetched normally
    with pytest.raises(WeatherError):
        env.svc.get_report("99501")
    assert len(env.primary.calls) == 2


# --------------------------------------------------------------------------- fallback reports keep the alert feed


def test_fallback_report_gets_the_primary_alert_feed(tmp_path):
    env = Env(tmp_path)
    warning = WeatherAlert(id="w1", event="Freeze Warning", severity="Severe")
    env.primary.alerts = [warning]
    env.primary.error = WeatherError("NWS returned neither observations nor a forecast")
    report = env.svc.get_report()
    assert report.provider == "openmeteo" and report.notes == ["via Open-Meteo"]
    assert report.alerts == [warning] and report.alerts_supported and report.alerts_error is None
    assert env.fallback.calls[0][2] is True and len(env.primary.alert_calls) == 1


def test_fallback_report_says_alerts_unavailable_when_the_feed_fails_too(tmp_path):
    env = Env(tmp_path)
    env.primary.error = WeatherError("NWS down")
    env.primary.alert_error = HttpError("boom", NWS, 503)
    report = env.svc.get_report()
    assert report.provider == "openmeteo" and report.alerts == []
    assert report.alerts_error == "Alerts unavailable, try later" and not report.alerts_supported


def test_us_point_without_nws_grid_still_gets_alerts(tmp_path):
    # American Samoa: /points has no gridded forecast, but NWS Pago Pago issues alerts
    env = Env(tmp_path)
    env.primary.error = WeatherError("no grid", user_message="No NWS data here")
    env.primary.alerts = [WeatherAlert(id="hu", event="Hurricane Warning", severity="Extreme")]
    report = env.svc.get_report()
    assert [a.id for a in report.alerts] == ["hu"] and report.alerts_supported


def test_point_outside_the_us_keeps_the_us_only_answer(tmp_path):
    env = Env(tmp_path, {"location": {"zip_code": "", "latitude": 43.6532, "longitude": -79.3832,
                                      "name": "Toronto", "country": "ca"}})
    env.primary.error = WeatherError("outside coverage", user_message="No NWS data here")
    report = env.svc.get_report()
    assert report.provider == "openmeteo" and not report.alerts_supported and report.alerts_error is None
    assert env.primary.alert_calls == []


# --------------------------------------------------------------------------- wiring with real providers


def test_default_wiring_falls_back_to_openmeteo(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})
    http = geo_http().add_error(NWS + "/", status=503).add_json(OM, "openmeteo_forecast_02108.json")
    svc = WeatherService(cfg, RuntimeSettings(cfg, tmp_path), http)
    assert svc.alerts_supported() is True  # NWS is the primary provider
    report = svc.get_report()
    assert report.provider == "openmeteo" and report.notes == ["via Open-Meteo"]
    assert report.current.temperature_c == pytest.approx((35.3 - 32) * 5 / 9)
    assert (cfg.cache_path() / "geocode.json").exists()  # default geocoder cache file


def real_service(tmp_path: Any, http: FakeHttp, clock: Optional[Clock] = None) -> WeatherService:
    """WeatherService with the real NWS + Open-Meteo providers frozen at the 02108 capture time."""
    cfg = make_config({"state_dir": str(tmp_path)})
    providers = {"nws": NwsProvider(http, cfg, now_fn=lambda: NOW),
                 "openmeteo": OpenMeteoProvider(http, cfg, now_fn=lambda: NOW)}
    return WeatherService(cfg, RuntimeSettings(cfg, tmp_path), http, providers=providers, clock=clock or Clock())


def add_weather_routes(http: FakeHttp) -> FakeHttp:
    """The 02108 NWS and Open-Meteo captures, after any routes already on ``http`` (earlier routes win)."""
    return (http.add_json(NWS + "/points/42.3576,-71.0684", "nws_points_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/forecast/hourly", "nws_forecast_hourly_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/forecast", "nws_forecast_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/stations", "nws_stations_02108.json")
            .add_json(NWS + "/stations/KBOS/observations/latest", "nws_observation_latest_02108.json")
            .add_json(NWS + "/alerts/active", "nws_alerts_point_02108.json")
            .add_json(OM, "openmeteo_forecast_02108.json"))


def test_nws_without_observations_gets_current_from_openmeteo(tmp_path):
    http = add_weather_routes(geo_http()
                              .add_json(NWS + "/stations/", "nws_observation_latest_TJSJ_nulls.json")
                              .add_error(NWS + "/gridpoints/BOX/71,101/forecast/hourly", status=500))
    svc = real_service(tmp_path, http)
    report = svc.get_report()
    assert report.provider == "nws" and report.notes == ["obs via Open-Meteo"]
    assert report.current.station == "Open-Meteo"
    assert [p.name for p in report.forecast] == ["Today", "Tonight", "Saturday", "Saturday Night"]
    assert report.alerts_supported and report.timezone == "America/New_York"
    assert svc.get_alerts() == ([], "America/New_York")


def test_nws_forecast_outage_gets_the_forecast_from_openmeteo(tmp_path):
    http = add_weather_routes(geo_http().add_error(re.compile(r"/gridpoints/BOX/71,101/forecast$"), status=503))
    report = real_service(tmp_path, http).get_report()
    assert report.provider == "nws" and report.notes == ["fc via Open-Meteo"]
    assert report.current.station == "KBOS" and report.alerts_supported
    assert [p.name for p in report.forecast] == ["Today", "Sat", "Sun", "Mon"]


def test_real_providers_outage_serves_the_stale_report(tmp_path):
    clock = Clock()
    http = add_weather_routes(geo_http())
    svc = real_service(tmp_path, http, clock)
    fresh = svc.get_report()
    assert fresh.provider == "nws" and fresh.current.station == "KBOS" and fresh.notes == []
    before = len(http.calls)
    http.routes = FakeHttp().add_error(NWS + "/", status=503).add_error(OM, status=None).routes
    clock.t += 11 * 60
    stale = svc.get_report()
    assert stale.stale is True and stale.notes == ["cached 11m"]
    assert stale.current == fresh.current and stale.forecast == fresh.forecast
    tried = [url for url, _ in http.calls[before:]]
    assert any(u.startswith(NWS) for u in tried) and any(u.startswith(OM) for u in tried)
    with pytest.raises(WeatherError) as ei:
        svc.get_alerts()
    assert ei.value.user_message == "Alerts unavailable, try later"


def test_create_weather_provider(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})
    nws = create_weather_provider("nws", FakeHttp(), cfg)
    om = create_weather_provider(" OpenMeteo ", FakeHttp(), cfg)
    assert isinstance(nws, NwsProvider) and nws.name == "nws" and nws.supports_alerts
    assert isinstance(om, OpenMeteoProvider) and om.name == "openmeteo" and not om.supports_alerts
    with pytest.raises(ConfigError):
        create_weather_provider("darksky", FakeHttp(), cfg)


def test_missing_primary_provider_is_a_config_error(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})
    with pytest.raises(ConfigError):
        WeatherService(cfg, RuntimeSettings(cfg, tmp_path), FakeHttp(),
                       providers={"openmeteo": StubProvider("openmeteo")})
