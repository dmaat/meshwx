"""Adversarial review of the WEATHER area (geocode.py, weather/*, Formatter weather rendering).

Regression tests from the adversarial code review: each test first reproduced one
verified defect (finding id in the comments); all of them are fixed now.
"""

from __future__ import annotations

import copy
import dataclasses
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from conftest import FakeHttp, load_fixture, make_config  # noqa: E402

from meshwx.app import select_new_alerts  # noqa: E402
from meshwx.config import DisplayConfig  # noqa: E402
from meshwx.errors import WeatherError  # noqa: E402
from meshwx.formatting import Formatter  # noqa: E402
from meshwx.geocode import ZipGeocoder  # noqa: E402
from meshwx.models import CurrentConditions, Location, WeatherAlert, WeatherReport  # noqa: E402
from meshwx.state import RuntimeSettings  # noqa: E402
from meshwx.timeutil import get_tz  # noqa: E402
from meshwx.weather import NwsProvider, OpenMeteoProvider, WeatherProvider, WeatherService  # noqa: E402
from meshwx.weather.nws import map_observation  # noqa: E402

NWS = "https://api.weather.gov"
OM = "https://api.open-meteo.com/v1/forecast"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
ZIPPO = "https://api.zippopotam.us/"
BOSTON_NOW = datetime(2026, 11, 13, 12, 38, 18, tzinfo=timezone.utc)  # capture time of the 02108 fixtures


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _service(tmp_path: Any, http: FakeHttp, now: datetime, clock: Optional[Clock] = None,
             overrides: Optional[Dict[str, Any]] = None) -> WeatherService:
    """WeatherService with the real NWS + Open-Meteo providers frozen at ``now`` (real ZipGeocoder)."""
    cfg = make_config(dict(overrides or {}, state_dir=str(tmp_path)))
    providers = {"nws": NwsProvider(http, cfg, now_fn=lambda: now),
                 "openmeteo": OpenMeteoProvider(http, cfg, now_fn=lambda: now)}
    return WeatherService(cfg, RuntimeSettings(cfg, tmp_path), http, providers=providers,
                          clock=clock or Clock())


def _boston_routes(http: FakeHttp, alerts: Any = "nws_alerts_point_02108.json") -> FakeHttp:
    """The 02108 captures of tests/fixtures, after any routes already on ``http`` (earlier routes win)."""
    return (http.add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
            .add_json(NWS + "/points/42.3576,-71.0684", "nws_points_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/forecast/hourly", "nws_forecast_hourly_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/forecast", "nws_forecast_02108.json")
            .add_json(NWS + "/gridpoints/BOX/71,101/stations", "nws_stations_02108.json")
            .add_json(NWS + "/stations/KBOS/observations/latest", "nws_observation_latest_02108.json")
            .add_json(NWS + "/alerts/active?point=42.3576,-71.0684", alerts)
            .add_json(OM, "openmeteo_forecast_02108.json"))


def _fmt(**display: Any) -> Formatter:
    return Formatter(DisplayConfig(**display), 200, default_tz=get_tz("America/New_York"))


# =========================================================================== WEATHER-1
# `!alerts` / `!alerts N` / the `!wx` alert count come from the cached report (weather.cache_minutes),
# while the alert watcher fetches alerts itself and never updates that cache.


def test_alerts_command_agrees_with_alert_watcher(tmp_path):
    state = {"alerts": load_fixture("nws_alerts_point_02108.json")}  # nothing active yet
    clock = Clock()
    http = _boston_routes(FakeHttp(), alerts=lambda url: state["alerts"])
    svc = _service(tmp_path, http, BOSTON_NOW, clock)

    assert svc.get_report().alerts == []  # e.g. a scheduled broadcast or a `!wx` at t=0

    state["alerts"] = load_fixture("nws_alerts_sample.json")  # NWS issues alerts for the point
    clock.t += 60
    watcher_alerts, _ = svc.get_alerts()  # App._do_alert_check: these are broadcast now ("⚠ Freeze Warning ...")
    assert len(watcher_alerts) == 3

    clock.t += 30  # a mesh user answers the broadcast with `!alerts` / `!alerts 1`
    report = svc.get_report()
    text = _fmt().alerts(report)
    assert [a.id for a in report.alerts] == [a.id for a in watcher_alerts], text  # today: "No alerts for Boston, MA"


# =========================================================================== WEATHER-2
# An upgraded ("replaced") watch stays in /alerts/active with urgency "Past", messageType "Alert",
# VTEC action UPG, `expires` = issue time + 15 min but `ends` in the future; meshwx treats it as active.

MONROEVILLE = Location(31.5153, -87.3410, "Monroeville, AL", zip_code="36460")
MONROEVILLE_NOW = datetime(2026, 11, 14, 8, 49, 25, tzinfo=timezone.utc)
_AREA = "Choctaw; Washington; Clarke; Wilcox; Monroe; Conecuh; Butler; Crenshaw; Wayne"


def _feature(oid: str, **props: Any) -> Dict[str, Any]:
    base = {"id": "urn:oid:" + oid, "areaDesc": _AREA, "references": [], "status": "Actual",
            "senderName": "NWS Mobile AL", "instruction": "Take steps now to protect tender plants from the cold."}
    base.update(props)
    return {"id": NWS + "/alerts/urn:oid:" + oid, "type": "Feature", "geometry": None, "properties": base}


MONROEVILLE_ALERTS = {  # GET /alerts/active?point=31.5153,-87.3410 at 2026-11-14T08:49:25Z (3 features)
    "type": "FeatureCollection",
    "title": "Current watches, warnings, and advisories for 31.5153 N, 87.341 W",
    "updated": "2026-11-14T08:49:25+00:00",
    "features": [
        _feature("2.49.0.1.840.0.197269d8a817e8a742da1ab42c2c6764819c945d.001.1",
                 sent="2026-11-13T19:18:00-06:00", effective="2026-11-13T19:18:00-06:00",
                 onset="2026-11-14T00:00:00-06:00", expires="2026-11-14T08:00:00-06:00",
                 ends="2026-11-14T08:00:00-06:00", messageType="Alert", severity="Severe", certainty="Likely",
                 urgency="Expected", event="Freeze Warning",
                 headline="Freeze Warning issued November 13 at 7:18PM CST until November 14 at 8:00AM CST "
                          "by NWS Mobile AL",
                 description="* WHAT...Temperatures as low as 29 to 32 expected.",
                 parameters={"NWSheadline": ["FREEZE WARNING REMAINS IN EFFECT FROM MIDNIGHT TONIGHT TO 8 AM CST "
                                             "SATURDAY... ...FREEZE WARNING REMAINS IN EFFECT FROM MIDNIGHT SATURDAY "
                                             "NIGHT TO 8 AM CST SUNDAY"],
                             "VTEC": ["/O.CON.KMOB.FZ.W.0003.261114T0600Z-261114T1400Z/"]}),
        _feature("2.49.0.1.840.0.197269d8a817e8a742da1ab42c2c6764819c945d.001.2",
                 sent="2026-11-13T19:18:00-06:00", effective="2026-11-13T19:18:00-06:00",
                 onset="2026-11-15T00:00:00-06:00", expires="2026-11-14T08:00:00-06:00",
                 ends="2026-11-15T08:00:00-06:00", messageType="Alert", severity="Severe", certainty="Likely",
                 urgency="Expected", event="Freeze Warning",
                 headline="Freeze Warning issued November 13 at 7:18PM CST until November 15 at 8:00AM CST "
                          "by NWS Mobile AL",
                 description="* WHAT...Temperatures as low as 29 to 32 expected.",
                 parameters={"VTEC": ["/O.CON.KMOB.FZ.W.0004.261115T0600Z-261115T1400Z/"]}),
        _feature("2.49.0.1.840.0.701e05731d128000b330f70fee77e2784cfa530b.001.1",
                 sent="2026-11-13T09:29:00-06:00", effective="2026-11-13T09:29:00-06:00",
                 onset="2026-11-14T00:00:00-06:00", expires="2026-11-13T09:44:57-06:00",
                 ends="2026-11-14T08:00:00-06:00", messageType="Alert", severity="Severe", certainty="Observed",
                 urgency="Past", event="Freeze Watch",
                 headline="The Freeze Watch has been replaced. Please see the latest information from NWS "
                          "Mobile AL on this developing situation.",
                 description="The Freeze Watch has been replaced. Please see the latest information from NWS "
                             "Mobile AL on this developing situation.",
                 parameters={"NWSheadline": ["FREEZE WARNING IN EFFECT FROM MIDNIGHT TONIGHT TO 8 AM CST SATURDAY... "
                                             "...FREEZE WARNING IN EFFECT FROM MIDNIGHT SATURDAY NIGHT TO 8 AM CST "
                                             "SUNDAY"],
                             "VTEC": ["/O.UPG.KMOB.FZ.A.0001.261114T0600Z-261114T1400Z/"]}),
    ],
}


def test_replaced_watch_is_not_an_active_alert(tmp_path):
    http = FakeHttp().add_json(NWS + "/alerts/active?point=31.5153,-87.3410", MONROEVILLE_ALERTS)
    prov = NwsProvider(http, make_config({"state_dir": str(tmp_path)}), now_fn=lambda: MONROEVILLE_NOW)
    alerts = prov.fetch_alerts(MONROEVILLE)
    report = WeatherReport(location=MONROEVILLE, provider="nws", fetched_at=MONROEVILLE_NOW,
                           timezone="America/Chicago", alerts=alerts, alerts_supported=True)
    text = "\n".join(_fmt().alerts(report))
    # today: "Monroeville, AL: 3 alerts / 1) Freeze Warning til 8a / 2) Freeze Watch til 8a / 3) Freeze Warning ..."
    assert [a.event for a in alerts] == ["Freeze Warning", "Freeze Warning"], text
    assert "Freeze Watch" not in text


def test_replaced_watch_is_not_broadcast_as_a_new_alert(tmp_path):
    http = FakeHttp().add_json(NWS + "/alerts/active?point=31.5153,-87.3410", MONROEVILLE_ALERTS)
    prov = NwsProvider(http, make_config({"state_dir": str(tmp_path)}), now_fn=lambda: MONROEVILLE_NOW)
    alerts = prov.fetch_alerts(MONROEVILLE)
    # The Freeze Warnings were announced when issued.  The UPG notice is messageType "Alert" with no
    # references (as in the live feed), so the watcher takes it for a brand-new Severe alert; live, it
    # goes out next to the new warning at the moment of the upgrade.
    seen = {a.id: {"event": a.event, "ends": a.ends.isoformat() if a.ends else None,
                   "seen_at": "2026-11-14T01:18:00+00:00"} for a in alerts if a.event == "Freeze Warning"}
    to_broadcast, _ = select_new_alerts(alerts, seen, "Moderate", MONROEVILLE_NOW)
    texts = [t for a in to_broadcast for t in _fmt().new_alert(a, "Monroeville, AL", "America/Chicago")]
    # today: ["⚠ Freeze Watch for Monroeville, AL til ... 8a: FREEZE WARNING IN EFFECT FROM MIDNIGHT TONIGHT ..."]
    assert to_broadcast == [], texts


# =========================================================================== WEATHER-3
# Territory ZIPs: zippopotam's duplicate/garbage entries are taken at face value.

GU_96932 = {"country": "Guam", "country abbreviation": "GU", "post code": "96932", "places": [
    {"place name": "Hagatna", "longitude": "144.7863", "latitude": "13.4443", "state": "Gu",
     "state abbreviation": "66"},
    {"place name": "Hagatna", "longitude": "-144.7484", "latitude": "13.4751", "state": "Guam",
     "state abbreviation": "GU"}]}
GU_96921 = {"country": "Guam", "country abbreviation": "GU", "post code": "96921", "places": [
    {"place name": "Barrigada", "longitude": "144.7863", "latitude": "13.4443", "state": "Gu",
     "state abbreviation": "66"},
    {"place name": "Barrigada", "longitude": "-144.7947", "latitude": "13.4616", "state": "Guam",
     "state abbreviation": "GU"}]}
US_96951 = {"country": "United States", "country abbreviation": "US", "post code": "96951", "places": [
    {"place name": "Rota", "longitude": "-101.0372", "latitude": "31.8243", "state": "",
     "state abbreviation": "MP"}]}
MP_96951 = {"country": "Northern Mariana Islands", "country abbreviation": "MP", "post code": "96951", "places": [
    {"place name": "Rota", "longitude": "145.7408", "latitude": "15.1901", "state": "Mp",
     "state abbreviation": "69"}]}

#: (lat_min, lat_max, lon_min, lon_max) of the islands
GUAM_BOX = (13.2, 13.7, 144.6, 145.0)
MARIANAS_BOX = (14.0, 20.6, 144.8, 146.1)


@pytest.mark.parametrize("zip_code, routes, box", [
    ("96932", [("us/96932", {}, 404), ("gu/96932", GU_96932, 200)], GUAM_BOX),       # Hagatna, capital of Guam
    ("96921", [("us/96921", {}, 404), ("gu/96921", GU_96921, 200)], GUAM_BOX),       # Barrigada, GU
    ("96951", [("us/96951", US_96951, 200), ("mp/96951", MP_96951, 200)], MARIANAS_BOX),  # Rota, MP
])
def test_territory_zip_lands_in_the_territory(zip_code, routes, box):
    http = FakeHttp()
    for path, body, status in routes:
        http.add_json(ZIPPO + path, body, status=status)
    http.add_json(OM_GEO, {"generationtime_ms": 0.19})  # Open-Meteo has none of these ZIPs (verified live)
    loc = ZipGeocoder(http).lookup(zip_code)
    lat_min, lat_max, lon_min, lon_max = box
    # today: 96932 -> (13.4751, -144.7484) and 96921 -> (13.4616, -144.7947), mid-Pacific (NWS 404 ->
    # Open-Meteo ocean point, tz Etc/GMT+10, "Alerts: US/NWS only"); 96951 -> (31.8243, -101.0372) in Texas.
    assert lat_min <= loc.latitude <= lat_max and lon_min <= loc.longitude <= lon_max, loc


# =========================================================================== WEATHER-4
# Puerto Rico / Hawaii / USVI / Guam: every station's latest observation is all-null (QC "Z"), so the
# "current conditions" are the NWS hourly *forecast*, shown with the fetch time as if observed.

SJU = NWS + "/gridpoints/SJU/162,132"
SAN_JUAN_NOW = datetime(2026, 11, 14, 8, 47, 26, tzinfo=timezone.utc)
SAN_JUAN_POINTS = {"properties": {
    "forecast": SJU + "/forecast", "forecastHourly": SJU + "/forecast/hourly",
    "observationStations": SJU + "/stations", "timeZone": "America/Puerto_Rico", "type": "land",
    "gridId": "SJU", "gridX": 162, "gridY": 132}}


def _sju_period(number: int, name: str, start: str, end: str, day: bool, temp: int, short: str,
                wind: str = "10 mph", pop: int = 10, **extra: Any) -> Dict[str, Any]:
    p = {"number": number, "name": name, "startTime": start, "endTime": end, "isDaytime": day,
         "temperature": temp, "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
         "windSpeed": wind, "windDirection": "SSE", "shortForecast": short, "detailedForecast": ""}
    p.update(extra)
    return p


SAN_JUAN_FORECAST = {"properties": {"periods": [
    _sju_period(1, "Overnight", "2026-11-14T04:00:00-04:00", "2026-11-14T06:00:00-04:00", False, 81,
                "Isolated Rain Showers"),
    _sju_period(2, "Saturday", "2026-11-14T06:00:00-04:00", "2026-11-14T18:00:00-04:00", True, 89, "Sunny",
                wind="10 to 16 mph", pop=3)]}}
_HUMID = {"dewpoint": {"unitCode": "wmoUnit:degC", "value": 25},
          "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 88}}
SAN_JUAN_HOURLY = {"properties": {"periods": [
    _sju_period(1, "", "2026-11-14T04:00:00-04:00", "2026-11-14T05:00:00-04:00", False, 81,
                "Isolated Rain Showers", **_HUMID),
    _sju_period(2, "", "2026-11-14T05:00:00-04:00", "2026-11-14T06:00:00-04:00", False, 81,
                "Isolated Rain Showers", **_HUMID)]}}
SAN_JUAN_STATIONS = {"features": [{"id": NWS + "/stations/" + s} for s in ("TJSJ", "TJPS", "TJBQ")]}


def _null_obs(station: str, timestamp: str, text: str, raw: str) -> Dict[str, Any]:
    q = {"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "Z"}
    props: Dict[str, Any] = {"stationId": station, "timestamp": timestamp, "textDescription": text,
                             "rawMessage": raw}
    for key in ("temperature", "dewpoint", "windDirection", "windSpeed", "windGust", "barometricPressure",
                "seaLevelPressure", "relativeHumidity", "heatIndex", "windChill"):
        props[key] = dict(q)
    props["visibility"] = {"unitCode": "wmoUnit:m", "value": 16090, "qualityControl": "C"}
    return {"properties": props}


SAN_JUAN_OBS = {
    "TJSJ": _null_obs("TJSJ", "2026-11-14T07:56:00+00:00", "Mostly Clear",
                      "TJSJ 140756Z 15007KT 10SM FEW025 29/24 A2991 RMK AO2 SLP126 T02890244 $"),
    "TJPS": _null_obs("TJPS", "2026-11-14T07:55:00+00:00", "Mostly Cloudy",
                      "TJPS 140755Z AUTO 14007KT 10SM SCT020 BKN030 BKN043 28/26 A2990 RMK AO2"),
    "TJBQ": _null_obs("TJBQ", "2026-11-14T05:50:00+00:00", "Clear", "TJBQ 140550Z 14003KT 10SM CLR 25/24 A2994"),
}


def _san_juan_http() -> FakeHttp:
    http = (FakeHttp().add_json(ZIPPO + "us/00901", {}, status=404)
            .add_json(ZIPPO + "pr/00901", "zippopotam_pr_00901.json")
            .add_json(NWS + "/points/18.4659,-66.1036", SAN_JUAN_POINTS)
            .add_json(SJU + "/forecast/hourly", SAN_JUAN_HOURLY)
            .add_json(SJU + "/forecast", SAN_JUAN_FORECAST)
            .add_json(SJU + "/stations", SAN_JUAN_STATIONS))
    for station, body in SAN_JUAN_OBS.items():
        http.add_json(NWS + "/stations/%s/observations/latest" % station, body)
    return (http.add_json(NWS + "/alerts/active?point=18.4659,-66.1036", {"features": []})
            .add_json(OM, "openmeteo_forecast_02108.json"))


def test_forecast_is_not_passed_off_as_current_conditions(tmp_path):
    svc = _service(tmp_path, _san_juan_http(), SAN_JUAN_NOW, overrides={"location": {"zip_code": "00901"}})
    report = svc.get_report()
    text = _fmt().current(report)[0]
    # today: "San Juan, PR 4:47a: 81°F Isolated Rain Shwrs. Wind SSE 10mph. RH 88%" - the 4-5 AM hourly
    # forecast, stamped with the fetch time (TJSJ actually reported 29 C = 84°F, "Mostly Clear", 7 kt).
    # The provider's own note "no recent obs" never reaches the mesh (the formatter ignores report.notes).
    marked = any(m in text.lower() for m in ("fcst", "forecast", "no recent obs", "no obs"))
    assert report.current.station != "NWS forecast" or marked, text


# =========================================================================== WEATHER-5
# Pressure source flips between seaLevelPressure (hourly METARs) and barometricPressure (= the altimeter
# setting) at the same station; at elevation the two differ by ~0.13 inHg.

KBKF_0658Z = {"properties": {  # hourly METAR: API has both SLP and altimeter
    "stationId": "KBKF", "timestamp": "2026-11-14T06:58:00+00:00", "textDescription": "Mostly Cloudy",
    "rawMessage": "KBKF 140658Z 19005KT 10SM BKN220 06/01 A2978 RMK AO2A SLP042 T00640011 401740043",
    "temperature": {"unitCode": "wmoUnit:degC", "value": 6.4},
    "dewpoint": {"unitCode": "wmoUnit:degC", "value": 1.1},
    "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 190},
    "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 9.36},
    "windGust": {"unitCode": "wmoUnit:km_h-1", "value": None},
    "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 100850},
    "seaLevelPressure": {"unitCode": "wmoUnit:Pa", "value": 100420},
    "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 68.880190945135},
    "heatIndex": {"unitCode": "wmoUnit:degC", "value": None},
    "windChill": {"unitCode": "wmoUnit:degC", "value": 4.479877769263889},
    "visibility": {"unitCode": "wmoUnit:m", "value": 16090}}}
KBKF_0558Z = copy.deepcopy(KBKF_0658Z)  # one hour earlier: same altimeter A2978, API left SLP null
KBKF_0558Z["properties"].update(
    timestamp="2026-11-14T05:58:00+00:00",
    rawMessage="KBKF 140558Z 19006KT 10SM BKN220 07/00 A2978 RMK AO2A SLP042 T00690003 10121 20067 57007")
KBKF_0558Z["properties"]["seaLevelPressure"]["value"] = None
KBKF_0558Z["properties"]["temperature"]["value"] = 6.9
DENVER = Location(39.7491, -104.9946, "Denver, CO", zip_code="80202")


def _shown_inhg(body: Dict[str, Any]) -> Optional[float]:
    cur = map_observation(body)
    report = WeatherReport(location=DENVER, provider="nws", fetched_at=cur.observed_at,
                           timezone="America/Denver", current=cur)
    m = re.search(r"(\d+\.\d+)inHg", _fmt().current(report)[0])
    return float(m.group(1)) if m else None


def test_pressure_does_not_jump_with_the_observation_type():
    shown = [p for p in (_shown_inhg(KBKF_0558Z), _shown_inhg(KBKF_0658Z)) if p is not None]
    # today: 29.78 (05:58Z, altimeter) then 29.65 (06:58Z, SLP) although both METARs say A2978 / SLP042
    assert max(shown) - min(shown) <= 0.02, shown


# =========================================================================== WEATHER-6
# When the NWS *weather* requests fail (gridpoint outage) the whole NWS report is discarded and the
# alert feed - a separate, working endpoint - is never asked: `!alerts` says "Alerts unavailable".


def test_gridpoint_outage_keeps_the_working_alert_feed(tmp_path):
    http = _boston_routes(FakeHttp().add_error(NWS + "/gridpoints/", status=503),
                          alerts="nws_alerts_sample.json")
    svc = _service(tmp_path, http, BOSTON_NOW)
    report = svc.get_report()
    watcher_alerts, _ = svc.get_alerts()  # the alert watcher still sees (and broadcasts) them
    assert len(watcher_alerts) == 3
    text = "\n".join(_fmt().alerts(report))
    # today: provider "openmeteo", alerts [], alerts_error -> "Alerts unavailable, try later"
    assert [a.id for a in report.alerts] == [a.id for a in watcher_alerts], text


# =========================================================================== WEATHER-7
# get_alerts() returns tz None until a full report was fetched (zippopotam has no time zone), so the
# first alert check after every restart renders alert times in the Pi's zone, not the location's.


def test_alert_time_zone_known_on_the_first_check(tmp_path):
    http = _boston_routes(FakeHttp(), alerts="nws_alerts_sample.json")
    svc = _service(tmp_path, http, BOSTON_NOW)
    alerts, tz_name = svc.get_alerts()  # App's first alert check, 30 s after start
    london = Formatter(DisplayConfig(), 200, default_tz=get_tz("Europe/London"))  # Raspberry Pi OS default zone
    text = london.new_alert(alerts[0], "Boston, MA", tz_name)[0]
    assert tz_name == "America/New_York", text  # NWS /points (cached on disk) knows it


# =========================================================================== WEATHER-8
# During an outage nothing remembers the failure: every `!wx`/`!fc`/`!alerts` re-runs the whole failing
# chain (NWS forecast, observation, hourly forecast, then Open-Meteo), each URL retried 3x by HttpClient.


def test_outage_does_not_refetch_on_every_command(tmp_path):
    clock = Clock()
    http = _boston_routes(FakeHttp())
    svc = _service(tmp_path, http, BOSTON_NOW, clock)
    svc.get_report()
    http.routes = FakeHttp().add_error(NWS + "/", status=503).add_error(OM, status=503).routes
    clock.t += 11 * 60
    assert svc.get_report().stale  # the first command after the outage began: tries, fails, serves stale
    before = len(http.calls)
    for _ in range(5):  # five more commands in the next minute
        clock.t += 12
        assert svc.get_report().stale
    # today: 5 x 4 = 20 more failed requests (x3 attempts each with the real HttpClient)
    assert len(http.calls) - before <= 4, [u for u, _ in http.calls[before:]]


# =========================================================================== WEATHER-9
# USVI's main ZIP 00802 geocodes to a harbour ("marine") point; NWS /points then says
# timeZone "America/New_York" (land points in the USVI say "America/St_Thomas").  USVI keeps AST
# (UTC-4) all year, so from November to March every time meshwx shows for St Thomas is one hour early.

SJU_VI = NWS + "/gridpoints/SJU/257,119"
ST_THOMAS_NOW = datetime(2026, 11, 14, 8, 47, 28, tzinfo=timezone.utc)
VI_00802 = {"country": "Virgin Islands", "country abbreviation": "VI", "post code": "00802", "places": [
    {"place name": "St Thomas", "longitude": "-64.9637", "latitude": "18.3223", "state": "Vi",
     "state abbreviation": "78"}]}
ST_THOMAS_POINTS = {"properties": {
    "forecast": SJU_VI + "/forecast", "forecastHourly": SJU_VI + "/forecast/hourly",
    "observationStations": SJU_VI + "/stations", "timeZone": "America/New_York", "type": "marine",
    "gridId": "SJU", "gridX": 257, "gridY": 119}}
ST_THOMAS_FORECAST = {"properties": {"periods": [
    _sju_period(1, "Overnight", "2026-11-14T04:00:00-04:00", "2026-11-14T06:00:00-04:00", False, 82,
                "Mostly Clear", wind="20 mph", pop=0),
    _sju_period(2, "Saturday", "2026-11-14T06:00:00-04:00", "2026-11-14T18:00:00-04:00", True, 83, "Sunny",
                wind="17 to 21 mph", pop=0)]}}
ST_THOMAS_HOURLY = {"properties": {"periods": [
    _sju_period(1, "", "2026-11-14T04:00:00-04:00", "2026-11-14T05:00:00-04:00", False, 82, "Mostly Clear",
                wind="20 mph", pop=0, **_HUMID)]}}


def test_st_thomas_times_are_atlantic_standard_time(tmp_path):
    http = (FakeHttp().add_json(ZIPPO + "us/00802", {}, status=404)
            .add_json(ZIPPO + "vi/00802", VI_00802)
            .add_json(NWS + "/points/18.3223,-64.9637", ST_THOMAS_POINTS)
            .add_json(SJU_VI + "/forecast/hourly", ST_THOMAS_HOURLY)
            .add_json(SJU_VI + "/forecast", ST_THOMAS_FORECAST)
            .add_json(SJU_VI + "/stations", {"features": [{"id": NWS + "/stations/TIST"}]})
            .add_json(NWS + "/stations/TIST/observations/latest",
                      _null_obs("TIST", "2026-11-14T07:53:00+00:00", "Clear",
                                "TIST 140753Z AUTO 15015KT 10SM CLR 29/26 A2991 RMK AO2 SLP129 T02890256 $"))
            .add_json(NWS + "/alerts/active?point=18.3223,-64.9637", {"features": []})
            .add_json(OM, "openmeteo_forecast_02108.json"))
    svc = _service(tmp_path, http, ST_THOMAS_NOW, overrides={"location": {"zip_code": "00802"}})
    report = svc.get_report()
    zone = get_tz(report.timezone or report.location.timezone)
    offset = ST_THOMAS_NOW.astimezone(zone).utcoffset() if zone else None
    # today: report.timezone == "America/New_York" (UTC-5 in November): "St Thomas, VI 3:47a ..." at 4:47 AST
    assert offset == timedelta(hours=-4), (report.timezone, _fmt().current(report))


# =========================================================================== WEATHER-10
# Alerts of a cached report are rendered without looking at `ends` (ended forecast periods *are* skipped).
# During an outage the stale report (served up to 3 h) keeps announcing a warning that is long over -
# typically an ISP outage during the very storm the warning was about.

BOSTON = Location(42.3576, -71.0684, "Boston, MA", zip_code="02108")


class _ScriptedProvider(WeatherProvider):
    """Serves ``report`` until ``down`` is set, then fails like an unreachable API."""

    supports_alerts = True

    def __init__(self, name: str, report: Optional[WeatherReport]) -> None:
        self.name = name
        self.report = report
        self.down = report is None

    def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
        if self.down or self.report is None:
            raise WeatherError("%s request failed: connection refused" % self.name)
        return dataclasses.replace(self.report, location=location)


def test_ended_alerts_are_not_served_from_a_stale_report(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fetched = now - timedelta(hours=2)
    warning = WeatherAlert(
        id="urn:oid:2.49.0.1.840.0.review.svr.001.1", event="Severe Thunderstorm Warning",
        headline="Severe Thunderstorm Warning issued by NWS Boston/Norton MA",
        severity="Severe", urgency="Immediate", certainty="Observed", message_type="Alert",
        onset=fetched - timedelta(minutes=10), ends=fetched + timedelta(minutes=45),
        description="At 1:05 PM EDT, a severe thunderstorm was located over Boston, moving east at 25 mph.",
        instruction="For your protection move to an interior room on the lowest floor of a building.")
    report = WeatherReport(location=BOSTON, provider="nws", fetched_at=fetched, timezone="America/New_York",
                           current=CurrentConditions(description="Thunderstorm", temperature_c=24.0,
                                                     observed_at=fetched - timedelta(minutes=12), station="KBOS"),
                           alerts=[warning], alerts_supported=True)
    nws = _ScriptedProvider("nws", report)
    clock = Clock()
    cfg = make_config({"state_dir": str(tmp_path)})
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    svc = WeatherService(cfg, RuntimeSettings(cfg, tmp_path), http, clock=clock,
                         providers={"nws": nws, "openmeteo": _ScriptedProvider("openmeteo", None)})
    assert svc.get_report().alerts == [warning]  # the warning is active when fetched
    nws.down = True  # both providers unreachable from here on
    clock.t += 2 * 3600  # two hours later: the warning ended 75 minutes ago
    stale = svc.get_report()
    assert stale.stale
    fmt = _fmt()
    texts = fmt.alerts(stale) + fmt.current(stale) + fmt.broadcast(stale, ["current", "forecast", "alerts"], now=now)
    # today: "Boston, MA: 1 alert (cached 2h) / 1) Severe Thunderstorm Warning til <75 min ago> ...",
    # "... 75°F Thunderstorm. 1 alert: !alerts (cached 2h)" and the broadcast "⚠ Severe Thunderstorm Warning til ..."
    assert not any("Severe Thunderstorm" in t or "1 alert" in t for t in texts), texts
