"""Cross-module behaviour added during integration (lead-owned)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from meshwx.formatting import Formatter
from meshwx.httpclient import HttpBytes
from meshwx.mesh.meshtastic_link import MeshtasticLink
from meshwx.models import Location, WeatherReport
from meshwx.timeutil import clear_tz_cache, system_tz_name
from meshwx.transit.gtfs_static import PARTIAL_MAX_AGE_SECONDS, ZIP_NAME, GtfsStaticIndex
from meshwx.weather.service import WeatherService
from meshwx.state import RuntimeSettings
from meshwx.errors import WeatherError

from conftest import FakeHttp, make_config

BOSTON = Location(42.3576, -71.0684, "Boston, MA", "02108", "US", "America/New_York")


def _report(**kw) -> WeatherReport:
    return WeatherReport(location=BOSTON, provider="nws", fetched_at=datetime.now(timezone.utc), **kw)


def test_alerts_error_shown_instead_of_us_only():
    f = Formatter(make_config().display, 200)
    assert f.alerts(_report(alerts_supported=False, alerts_error="Alerts unavailable, try later")) == [
        "Alerts unavailable, try later"]
    assert f.alerts(_report(alerts_supported=False)) == ["Alerts: US/NWS only"]
    assert f.alerts(_report(alerts_supported=True)) == ["No alerts for Boston, MA"]


class _Provider:
    def __init__(self, name, supports_alerts, exc=None, alerts_exc=None):
        self.name, self.supports_alerts, self._exc, self._alerts_exc = name, supports_alerts, exc, alerts_exc

    def fetch(self, location, *, periods, include_alerts):
        if self._exc:
            raise self._exc
        return WeatherReport(location=location, provider=self.name, fetched_at=datetime.now(timezone.utc))

    def fetch_alerts(self, location):
        if self._alerts_exc:
            raise self._alerts_exc
        return []


def _service(tmp_path, primary_exc, alerts_exc=None, country="us"):
    cfg = make_config({"location": {"zip_code": "", "latitude": 42.3576, "longitude": -71.0684, "name": "Home",
                                    "country": country},
                       "weather": {"provider": "nws"}, "state_dir": str(tmp_path)})
    providers = {"nws": _Provider("nws", True, primary_exc, alerts_exc), "openmeteo": _Provider("openmeteo", False)}
    return WeatherService(cfg, RuntimeSettings(cfg, tmp_path), FakeHttp(), providers=providers)


def test_fallback_after_nws_outage_reports_alerts_unavailable(tmp_path):
    report = _service(tmp_path, WeatherError("NWS 503"), alerts_exc=WeatherError("alerts 503")).get_report()
    assert report.provider == "openmeteo"
    assert report.alerts_error == "Alerts unavailable, try later"


def test_fallback_keeps_a_working_nws_alert_feed(tmp_path):
    report = _service(tmp_path, WeatherError("NWS gridpoint 500")).get_report()
    assert report.provider == "openmeteo"
    assert report.alerts_supported and report.alerts_error is None


def test_fallback_outside_nws_coverage_keeps_us_only_message(tmp_path):
    report = _service(tmp_path, WeatherError("no grid", user_message="No NWS data here"), country="ca").get_report()
    assert report.provider == "openmeteo" and report.alerts_error is None


def test_tapback_reactions_are_ignored():
    link = MeshtasticLink(make_config().meshtastic)
    link._my_num = 0x12345678
    base = {"from": 0xBEEF, "to": 0x12345678, "id": 7, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "wx"}}
    assert link._incoming(base, None) is not None
    reaction = dict(base, decoded={"portnum": "TEXT_MESSAGE_APP", "text": "\U0001F44D", "emoji": 1, "replyId": 5})
    assert link._incoming(reaction, None) is None


def test_stale_partial_downloads_removed(tmp_path):
    old = tmp_path / (ZIP_NAME + ".abc.part")
    fresh = tmp_path / (ZIP_NAME + ".def.part")
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    past = time.time() - PARTIAL_MAX_AGE_SECONDS - 60
    os.utime(old, (past, past))
    idx = GtfsStaticIndex("https://example.test/gtfs.zip", FakeHttp(), tmp_path, 168)
    idx._ensure_loaded()
    assert not old.exists() and fresh.exists()  # a download in progress elsewhere is kept


def test_http_bytes_carries_date():
    fake = FakeHttp().add_bytes("https://x.test/feed", b"abc", headers={"Date": "Fri, 13 Nov 2026 12:00:00 GMT"})
    res = fake.get_bytes("https://x.test/feed")
    assert isinstance(res, HttpBytes) and res.date == "Fri, 13 Nov 2026 12:00:00 GMT"


def test_system_tz_name_is_cached(monkeypatch):
    clear_tz_cache()
    monkeypatch.setenv("TZ", "America/Chicago")
    assert system_tz_name() == "America/Chicago"
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert system_tz_name() == "America/Chicago"  # cached
    clear_tz_cache()
    assert system_tz_name() == "Europe/Paris"
    clear_tz_cache()
