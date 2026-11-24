"""Tests for meshwx.weather.nws (real api.weather.gov captures served by FakeHttp)."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from meshwx.errors import WeatherError
from meshwx.models import Location
from meshwx.timeutil import parse_iso
from meshwx.weather.nws import (
    NwsProvider,
    map_alert,
    map_forecast_period,
    map_observation,
    parse_metar,
    parse_wind_speed,
    quantity,
)

from conftest import FakeHttp, load_fixture, make_config

API = "https://api.weather.gov"
BOS = Location(42.3576, -71.0684, "Boston, MA", zip_code="02108")
CAPTURE = datetime(2026, 11, 13, 12, 38, 18, tzinfo=timezone.utc)  # when the 02108 fixtures were captured
MPH = 1.609344
FORECAST_ONLY = re.compile(r"/forecast$")


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def boston(http: Optional[FakeHttp] = None) -> FakeHttp:
    """Add the 02108 capture routes after any routes already on ``http`` (earlier routes win)."""
    http = http or FakeHttp()
    http.add_json(API + "/points/42.3576,-71.0684", "nws_points_02108.json")
    http.add_json(API + "/gridpoints/BOX/71,101/forecast/hourly", "nws_forecast_hourly_02108.json")
    http.add_json(API + "/gridpoints/BOX/71,101/forecast", "nws_forecast_02108.json")
    http.add_json(API + "/gridpoints/BOX/71,101/stations", "nws_stations_02108.json")
    http.add_json(API + "/stations/KBOS/observations/latest", "nws_observation_latest_02108.json")
    http.add_json(API + "/alerts/active?point=42.3576,-71.0684", "nws_alerts_point_02108.json")
    return http


def provider(http: FakeHttp, tmp_path, now: datetime = CAPTURE) -> NwsProvider:
    return NwsProvider(http, make_config({"state_dir": str(tmp_path)}), now_fn=lambda: now)


def observation(station: str, timestamp: str, temp: Optional[float] = 3.0) -> Dict[str, Any]:
    body = load_fixture("nws_observation_latest_02108.json")
    body["properties"].update(stationId=station, timestamp=timestamp)
    body["properties"]["temperature"]["value"] = temp
    return body


# --------------------------------------------------------------------------- full fetch


def test_fetch_boston(tmp_path):
    http = boston()
    report = provider(http, tmp_path).fetch(BOS, periods=4, include_alerts=True)
    assert report.provider == "nws" and report.location is BOS and report.fetched_at == CAPTURE
    assert report.timezone == "America/New_York"
    assert report.alerts == [] and report.alerts_supported is True and report.notes == []
    assert report.stale is False

    cur = report.current
    assert (cur.station, cur.description) == ("KBOS", "Light Rain")
    assert cur.temperature_c == 3.0 and cur.dewpoint_c == 2.0
    assert cur.humidity_pct == pytest.approx(93.1415, abs=1e-3)
    assert cur.wind_speed_kmh == pytest.approx(25.92) and cur.wind_gust_kmh is None
    assert cur.wind_dir_deg == 350
    assert cur.pressure_hpa == pytest.approx(1001.3551)  # barometricPressure = the altimeter setting
    assert cur.visibility_km == pytest.approx(16.09344)
    assert cur.feels_like_c == pytest.approx(-2.1352, abs=1e-3)  # windChill (heatIndex is null)
    assert cur.observed_at == datetime(2026, 11, 13, 12, 15, tzinfo=timezone.utc)

    assert [p.name for p in report.forecast] == ["Today", "Tonight", "Saturday", "Saturday Night"]
    today, tonight = report.forecast[:2]
    assert today.is_daytime is True and today.temp_low_c is None
    assert today.temp_high_c == pytest.approx(f_to_c(45))
    assert tonight.is_daytime is False and tonight.temp_high_c is None
    assert tonight.temp_low_c == pytest.approx(f_to_c(35))
    assert today.wind_speed_kmh == pytest.approx(12 * MPH)  # "6 to 12 mph" -> max
    assert today.wind_gust_kmh == pytest.approx(28 * MPH)  # "with gusts as high as 28 mph"
    assert tonight.wind_gust_kmh is None and tonight.wind_speed_kmh == pytest.approx(6 * MPH)
    assert (today.wind_dir, today.precip_chance_pct) == ("N", 49)
    assert today.description == "Chance Light Rain then Mostly Cloudy"
    assert today.detailed.startswith("A chance of rain before noon.")
    assert today.start == datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)
    assert today.end == datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)

    assert all(headers.get("Accept") == "application/geo+json" for _, headers in http.calls)
    assert http.calls_to("/gridpoints/BOX/71,101/stations?limit=3")
    assert not http.calls_to("/forecast/hourly")  # only needed without a usable observation


def test_periods_that_already_ended_are_dropped(tmp_path):
    late = datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)  # stale CDN copy: "Today" ended 23:00Z
    report = provider(boston(), tmp_path, now=late).fetch(BOS, periods=2, include_alerts=False)
    assert [p.name for p in report.forecast] == ["Tonight", "Saturday"]


def test_zero_periods_skips_the_forecast(tmp_path):
    http = boston()
    report = provider(http, tmp_path).fetch(BOS, periods=0, include_alerts=False)
    assert report.forecast == [] and report.current is not None
    assert not http.calls_to("/forecast")


# --------------------------------------------------------------------------- current conditions


def test_first_usable_station_wins(tmp_path):
    http = boston(FakeHttp()
                  .add_json(API + "/stations/KBOS/observations/latest", "nws_observation_latest_TJSJ_nulls.json")
                  .add_json(API + "/stations/KOWD/observations/latest",
                            observation("KOWD", "2026-11-13T11:08:00+00:00"))  # 90 min 18 s old
                  .add_json(API + "/stations/KBED/observations/latest",
                            observation("KBED", "2026-11-13T11:08:18+00:00", temp=4.0)))  # exactly 90 min
    report = provider(http, tmp_path).fetch(BOS, periods=1, include_alerts=False)
    assert (report.current.station, report.current.temperature_c) == ("KBED", 4.0)
    assert report.notes == [] and not http.calls_to("/forecast/hourly")


def test_no_usable_observation_uses_the_hourly_forecast(tmp_path):
    http = boston(FakeHttp().add_json(API + "/stations/", "nws_observation_latest_TJSJ_nulls.json"))
    report = provider(http, tmp_path).fetch(BOS, periods=1, include_alerts=False)
    cur = report.current
    assert cur.station == "NWS forecast" and report.notes == ["no recent obs"]
    assert cur.temperature_c == pytest.approx(f_to_c(41))  # hour 07:00-08:00 local
    assert cur.description == "Light Rain Likely"
    assert cur.wind_speed_kmh == pytest.approx(12 * MPH) and cur.wind_dir_deg == 0.0  # "N"
    assert cur.humidity_pct == 89 and cur.dewpoint_c == pytest.approx(3.3333, abs=1e-3)
    assert cur.observed_at is None
    assert len(http.calls_to("/observations/latest")) == 3
    assert len(http.calls_to("/forecast/hourly")) == 1


def test_null_quality_controlled_observation_is_decoded_from_its_metar(tmp_path):
    # PR/HI/VI/GU: every QC'd value is null (flag "Z") while rawMessage still holds the METAR
    body = load_fixture("nws_observation_latest_TJSJ_nulls.json")
    body["properties"].update(timestamp="2026-11-13T12:10:00+00:00",
                              rawMessage="TJSJ 131210Z 15007G17KT 10SM FEW025 29/24 A2991 RMK AO2 SLP126 T02890244 $")
    http = boston(FakeHttp().add_json(API + "/stations/", body))
    report = provider(http, tmp_path).fetch(BOS, periods=1, include_alerts=False)
    cur = report.current
    assert (cur.station, cur.description, report.notes) == ("TJSJ", "Mostly Clear", [])
    assert (cur.temperature_c, cur.dewpoint_c) == (28.9, 24.4)  # tenths from the T group
    assert cur.wind_speed_kmh == pytest.approx(7 * 1.852) and cur.wind_gust_kmh == pytest.approx(17 * 1.852)
    assert cur.wind_dir_deg == 150.0 and cur.pressure_hpa == pytest.approx(29.91 * 33.8638866667)
    assert cur.humidity_pct == pytest.approx(76.7, abs=0.1) and cur.visibility_km == pytest.approx(16.09)
    assert cur.observed_at == datetime(2026, 11, 13, 12, 10, tzinfo=timezone.utc)
    assert not http.calls_to("/forecast/hourly")


def test_metar_does_not_override_quality_controlled_values():
    body = load_fixture("nws_observation_latest_02108.json")
    body["properties"]["rawMessage"] = "KBOS 131215Z 18030KT 10SM CLR 20/10 A3050"
    cur = map_observation(body)
    assert (cur.temperature_c, cur.wind_dir_deg, cur.pressure_hpa) == (3.0, 350, pytest.approx(1001.3551))


@pytest.mark.parametrize("raw, expected", [
    ("KBKF 140658Z VRB05G18KT 10SM BKN220 M06/M11 A2978 RMK AO2A SLP042 T10641111",
     {"wind_speed_kmh": 5 * 1.852, "wind_gust_kmh": 18 * 1.852, "temperature_c": -6.4, "dewpoint_c": -11.1,
      "pressure_hpa": 29.78 * 33.8638866667}),
    ("METAR EGLL 140650Z 00000KT 9999 FEW020 07/05 Q1021",
     {"wind_speed_kmh": 0.0, "temperature_c": 7.0, "dewpoint_c": 5.0, "pressure_hpa": 1021.0}),
    ("PHNL 140653Z 06012KT 10SM FEW025 26/ A3002 RMK AO2",
     {"wind_speed_kmh": 12 * 1.852, "wind_dir_deg": 60.0, "temperature_c": 26.0, "pressure_hpa": 30.02 * 33.8638866667}),
    ("KXYZ 140653Z AUTO 27015MPS 1/2SM R04/2000FT FG M01/M02 A2992 RMK T1006",
     {"wind_speed_kmh": 54.0, "wind_dir_deg": 270.0, "temperature_c": -0.6, "dewpoint_c": -2.0,
      "pressure_hpa": 29.92 * 33.8638866667}),
    ("", {}), (None, {}), ("TJSJ 140756Z RMK AO2 29/24 A2991", {}),  # groups after RMK are remarks
])
def test_parse_metar(raw, expected):
    assert parse_metar(raw) == {k: pytest.approx(v) for k, v in expected.items()}


def test_hourly_skips_hours_that_already_ended(tmp_path):
    now = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)  # 09:30 local; KBOS obs is 2 h 15 min old
    http = boston(FakeHttp().add_error(API + "/stations/KOWD/", status=500)
                  .add_error(API + "/stations/KBED/", status=None))
    report = provider(http, tmp_path, now=now).fetch(BOS, periods=1, include_alerts=False)
    assert report.current.station == "NWS forecast"
    assert report.current.temperature_c == pytest.approx(f_to_c(36))  # hour 09:00-10:00 local
    assert report.current.humidity_pct == 100


def test_station_list_is_cached(tmp_path):
    http = boston()
    prov = provider(http, tmp_path)
    prov.fetch(BOS, periods=1, include_alerts=False)
    prov.fetch(BOS, periods=1, include_alerts=False)
    assert len(http.calls_to("/stations?limit=3")) == 1
    assert len(http.calls_to("/points/")) == 1


def test_station_list_refresh_failure_reuses_the_old_list(tmp_path):
    now = [CAPTURE]
    http = boston()
    prov = NwsProvider(http, make_config({"state_dir": str(tmp_path)}), now_fn=lambda: now[0])
    prov.fetch(BOS, periods=1, include_alerts=False)
    now[0] = CAPTURE + timedelta(days=1, minutes=1)  # the cached station list has expired
    http.routes[:0] = (FakeHttp().add_error(API + "/gridpoints/BOX/71,101/stations", status=503)
                       .add_json(API + "/stations/KBOS/observations/latest",
                                 observation("KBOS", "2026-11-14T12:30:00+00:00")).routes)
    report = prov.fetch(BOS, periods=1, include_alerts=False)
    assert len(http.calls_to("/stations?limit=3")) == 2  # a refresh was attempted ...
    assert report.current.station == "KBOS"  # ... and the old list was used


def test_empty_station_list_is_not_cached(tmp_path):
    http = boston(FakeHttp().add_json(API + "/gridpoints/BOX/71,101/stations",
                                      {"features": [], "observationStations": []}))
    prov = provider(http, tmp_path)
    for _ in range(2):
        assert prov.fetch(BOS, periods=1, include_alerts=False).current.station == "NWS forecast"
    assert len(http.calls_to("/stations?limit=3")) == 2


# --------------------------------------------------------------------------- /points


def test_marine_point_has_no_nws_data(tmp_path):
    loc = Location(-14.3333, -170.0, "Ofu, AS")
    http = FakeHttp().add_json(API + "/points/-14.3333,-170.0000", "nws_points_marine_AS.json")
    with pytest.raises(WeatherError) as ei:
        provider(http, tmp_path).fetch(loc, periods=2, include_alerts=True)
    assert ei.value.user_message == "No NWS data here"
    assert len(http.calls) == 1


def test_point_outside_coverage_404_is_remembered(tmp_path):
    loc = Location(43.6532, -79.3832, "Toronto")
    http = FakeHttp().add_json(API + "/points/", "nws_problem_points_404.json", status=404)
    prov = provider(http, tmp_path)
    for _ in range(2):
        with pytest.raises(WeatherError) as ei:
            prov.fetch(loc, periods=2, include_alerts=True)
        assert ei.value.user_message == "No NWS data here"
    assert len(http.calls) == 1  # cached for 24 h like a marine point


def test_points_failure_without_cache_raises(tmp_path):
    http = FakeHttp().add_error(API + "/points/", status=503)
    with pytest.raises(WeatherError) as ei:
        provider(http, tmp_path).fetch(BOS, periods=2, include_alerts=True)
    assert ei.value.user_message == "Weather unavailable, try later"


def test_points_are_cached_on_disk(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})
    NwsProvider(boston(), cfg, now_fn=lambda: CAPTURE).fetch(BOS, periods=1, include_alerts=False)
    doc = json.loads((cfg.cache_path() / "nws_points.json").read_text(encoding="utf-8"))
    entry = doc["points"]["42.3576,-71.0684"]
    assert entry["t"] == CAPTURE.timestamp()
    assert entry["data"]["forecast"] == API + "/gridpoints/BOX/71,101/forecast"
    assert entry["data"]["timeZone"] == "America/New_York"

    http = boston(FakeHttp().add_error(API + "/points/", status=500))  # restarted while /points is down
    report = NwsProvider(http, cfg, now_fn=lambda: CAPTURE + timedelta(hours=1)).fetch(
        BOS, periods=1, include_alerts=False)
    assert report.forecast and not http.calls_to("/points/")


def test_stale_points_copy_used_when_refresh_fails(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})
    NwsProvider(boston(), cfg, now_fn=lambda: CAPTURE).fetch(BOS, periods=1, include_alerts=False)
    later = CAPTURE + timedelta(days=2)  # Sunday 07:38 local
    http = boston(FakeHttp().add_error(API + "/points/", status=503))
    report = NwsProvider(http, cfg, now_fn=lambda: later).fetch(BOS, periods=1, include_alerts=False)
    assert len(http.calls_to("/points/")) == 1  # a refresh was attempted
    assert [p.name for p in report.forecast] == ["Sunday"]

    http = boston()
    NwsProvider(http, cfg, now_fn=lambda: later).fetch(BOS, periods=1, include_alerts=False)
    assert len(http.calls_to("/points/")) == 1
    doc = json.loads((cfg.cache_path() / "nws_points.json").read_text(encoding="utf-8"))
    assert doc["points"]["42.3576,-71.0684"]["t"] == later.timestamp()


def test_alaska_uses_urls_from_points_not_cwa(tmp_path):
    anchorage = Location(61.2116, -149.8761, "Anchorage, AK", zip_code="99501")
    now = datetime(2026, 11, 13, 12, 38, 21, tzinfo=timezone.utc)
    http = (FakeHttp()
            .add_json(API + "/points/61.2116,-149.8761", "nws_points_99501.json")
            .add_json(API + "/gridpoints/AER/143,236/forecast/hourly", {"properties": {"periods": []}})
            .add_json(API + "/gridpoints/AER/143,236/forecast", "nws_forecast_99501.json")
            .add_json(API + "/gridpoints/AER/143,236/stations", {"features": [], "observationStations": []}))
    report = provider(http, tmp_path, now=now).fetch(anchorage, periods=14, include_alerts=False)
    assert report.timezone == "America/Anchorage"
    assert not [url for url, _ in http.calls if "AFC" in url]
    names = [p.name for p in report.forecast]
    assert names[0] == "Tonight" and len(names) == 14
    tuesday = report.forecast[names.index("Tuesday")]
    assert tuesday.wind_speed_kmh == 0.0 and tuesday.wind_dir is None  # "0 mph", direction ""
    friday_night = report.forecast[names.index("Friday Night")]
    assert friday_night.wind_speed_kmh == pytest.approx(5 * MPH) and friday_night.wind_dir == "NE"
    assert report.current is None  # no station and no hourly data: the service fills it in


# --------------------------------------------------------------------------- forecast failures


def test_forecast_failure_keeps_the_observation(tmp_path):
    http = boston(FakeHttp().add_error(FORECAST_ONLY, status=503))
    report = provider(http, tmp_path).fetch(BOS, periods=2, include_alerts=False)
    assert report.forecast == [] and report.current.station == "KBOS"


def test_gridpoint_404_forces_a_new_points_lookup(tmp_path):
    http = boston(FakeHttp().add_error(FORECAST_ONLY, status=404))
    prov = provider(http, tmp_path)
    prov.fetch(BOS, periods=2, include_alerts=False)
    prov.fetch(BOS, periods=2, include_alerts=False)
    assert len(http.calls_to("/points/")) == 2


def test_no_forecast_and_no_current_raises(tmp_path):
    http = boston(FakeHttp().add_error(re.compile(r"/forecast(/hourly)?$"), status=503)
                  .add_error(API + "/stations/", status=503))
    with pytest.raises(WeatherError):
        provider(http, tmp_path).fetch(BOS, periods=2, include_alerts=False)


# --------------------------------------------------------------------------- alerts


def test_alerts_mapping_and_order(tmp_path):
    http = FakeHttp().add_json(API + "/alerts/active?point=42.3576,-71.0684", "nws_alerts_sample.json")
    alerts = provider(http, tmp_path).fetch_alerts(BOS)
    assert [a.event for a in alerts] == ["Freeze Warning", "Special Weather Statement", "Flood Advisory"]
    freeze, sps, flood = alerts

    assert (freeze.severity, freeze.urgency, freeze.certainty, freeze.message_type) == (
        "Severe", "Expected", "Likely", "Alert")
    assert freeze.id == "urn:oid:2.49.0.1.840.0.f478126a4d0887d8cd2ffbfe0a4a1efad8e4677a.001.2"
    assert freeze.headline.startswith("FREEZE WARNING IN EFFECT FROM MIDNIGHT TONIGHT")  # NWSheadline[0]
    assert freeze.onset == parse_iso("2026-11-14T00:00:00-06:00")
    assert freeze.ends == parse_iso("2026-11-14T08:00:00-06:00")  # "ends", not the earlier "expires"
    assert freeze.sender == "NWS Huntsville AL" and freeze.area.startswith("Lauderdale; Colbert")
    assert freeze.instruction == "Take steps now to protect tender plants from the cold."
    assert freeze.references == ()

    assert sps.headline.startswith("Special Weather Statement issued")  # no NWSheadline
    assert sps.ends == parse_iso("2026-11-13T09:00:00-08:00")  # ends is null -> expires
    assert sps.instruction is None and sps.severity == "Moderate"

    assert flood.headline == "FLOOD ADVISORY IN EFFECT UNTIL 615 AM PST THIS MORNING"
    assert flood.description.startswith("* WHAT...") and flood.description.endswith("...")
    assert all(headers.get("Accept") == "application/geo+json" for _, headers in http.calls)


def test_alert_status_filter_references_and_onset_tie_break(tmp_path):
    body = load_fixture("nws_alerts_sample.json")
    flood, freeze, sps = body["features"]
    test_message = copy.deepcopy(sps)
    test_message["properties"].update(id="test-1", status="Test")
    update = copy.deepcopy(freeze)
    update["properties"].update(
        id="upd-1", messageType="Update", onset="2026-11-13T20:00:00-06:00",
        references=[{"@id": API + "/alerts/" + freeze["properties"]["id"],
                     "identifier": freeze["properties"]["id"],
                     "sender": "w-nws.webmaster@noaa.gov", "sent": "2026-11-13T02:09:00-06:00"}])
    body["features"] = [flood, freeze, sps, test_message, update]
    http = FakeHttp().add_json(API + "/alerts/active", body)
    alerts = provider(http, tmp_path).fetch_alerts(BOS)
    freeze_id = freeze["properties"]["id"]
    assert [a.id for a in alerts] == ["upd-1", freeze_id, sps["properties"]["id"], flood["properties"]["id"]]
    assert alerts[0].message_type == "Update" and alerts[0].references == (freeze_id,)


def test_alerts_that_already_ended_are_dropped(tmp_path):
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)  # the Flood Advisory ended 14:15Z
    http = FakeHttp().add_json(API + "/alerts/active", "nws_alerts_sample.json")
    alerts = provider(http, tmp_path, now=now).fetch_alerts(BOS)
    assert [a.event for a in alerts] == ["Freeze Warning", "Special Weather Statement"]


def test_expired_references_count_as_references():
    freeze = load_fixture("nws_alerts_sample.json")["features"][1]
    props = freeze["properties"]
    props["references"] = [{"identifier": "urn:a", "sender": "w-nws.webmaster@noaa.gov", "sent": "x"}]
    props["parameters"]["expiredReferences"] = [  # space-separated "sender,identifier,sent" triples
        "w-nws.webmaster@noaa.gov,urn:b,2026-11-12T19:16:00-06:00 w-nws.webmaster@noaa.gov,urn:a,2026-11-12T11:53:00-06:00",
        "w-nws.webmaster@noaa.gov,urn:c,2026-11-12T08:00:00-06:00"]
    assert map_alert(freeze).references == ("urn:a", "urn:b", "urn:c")
    props["references"] = []
    props["parameters"]["expiredReferences"] = "w-nws.webmaster@noaa.gov,urn:d,2026-11-12T08:00:00-06:00"
    assert map_alert(freeze).references == ("urn:d",)


PAST = "2026-11-13T06:00:00-06:00"  # before CAPTURE (12:38Z)


@pytest.mark.parametrize("change, listed", [
    ({}, True),  # VTEC NEW
    ({"VTEC": ["/O.CON.KHUN.FZ.W.0003.261114T0600Z-261114T1400Z/"], "expires": PAST}, True),  # long-fuse follow-up
    ({"VTEC": ["/O.UPG.KHUN.FZ.A.0001.261114T0600Z-261114T1400Z/"], "urgency": "Past"}, False),  # "has been replaced"
    ({"VTEC": ["/O.CAN.KHUN.FZ.W.0003.261114T0600Z-261114T1400Z/"]}, False),
    ({"VTEC": ["/O.EXP.KHUN.FZ.W.0003.261114T0600Z-261114T1400Z/"]}, False),
    ({"VTEC": ["/O.UPG.KHUN.FZ.A.0001.261114T0600Z-261114T1400Z/",
               "/O.EXA.KHUN.FZ.W.0003.261114T0600Z-261114T1400Z/"]}, True),  # one event ends, another begins
    ({"VTEC": None, "messageType": "Cancel"}, False),
    ({"VTEC": None, "urgency": "Past", "expires": PAST}, False),
    ({"VTEC": None, "urgency": "Past"}, True),  # not expired yet
])
def test_products_ending_their_event_are_not_active_alerts(tmp_path, change, listed):
    body = load_fixture("nws_alerts_sample.json")
    props = body["features"][1]["properties"]  # Freeze Warning, ends tomorrow
    change = dict(change)
    if "VTEC" in change:
        props["parameters"]["VTEC"] = change.pop("VTEC")
    props.update(change)
    http = FakeHttp().add_json(API + "/alerts/active", body)
    alerts = provider(http, tmp_path).fetch_alerts(BOS)
    assert ("Freeze Warning" in [a.event for a in alerts]) is listed


# --------------------------------------------------------------------------- time zone


def test_timezone_for_reads_the_cached_points(tmp_path):
    http = boston()
    prov = provider(http, tmp_path)
    assert prov.timezone_for(BOS) == "America/New_York"
    assert prov.timezone_for(Location(42.3576, -71.0684, "Boston", timezone="America/Chicago")) == "America/New_York"
    prov.fetch(BOS, periods=1, include_alerts=False)
    assert len(http.calls_to("/points/")) == 1  # the alert check's lookup is reused by the report


def test_timezone_for_without_points_falls_back_to_the_location(tmp_path):
    http = FakeHttp().add_error(API + "/points/", status=503)
    prov = provider(http, tmp_path)
    assert prov.timezone_for(BOS) is None
    assert prov.timezone_for(Location(42.3576, -71.0684, "Boston", timezone="America/New_York")) == "America/New_York"


def test_marine_point_uses_the_geocoder_zone(tmp_path):
    # St Thomas harbour: /points type "marine", timeZone "America/New_York" (the office's zone)
    points = load_fixture("nws_points_02108.json")
    points["properties"]["type"] = "marine"
    http = boston(FakeHttp().add_json(API + "/points/", points))
    prov = provider(http, tmp_path)
    harbour = Location(42.3576, -71.0684, "Harbour", timezone="America/St_Thomas")
    assert prov.fetch(harbour, periods=1, include_alerts=False).timezone == "America/St_Thomas"
    assert prov.timezone_for(harbour) == "America/St_Thomas"
    assert prov.fetch(BOS, periods=1, include_alerts=False).timezone == "America/New_York"  # no zone of its own


def test_alert_failure_keeps_the_weather(tmp_path):
    http = boston(FakeHttp().add_error(API + "/alerts/", status=503))
    report = provider(http, tmp_path).fetch(BOS, periods=2, include_alerts=True)
    assert report.current is not None and len(report.forecast) == 2
    assert report.alerts_supported is False and report.notes == ["alerts unavailable"]


def test_include_alerts_false_skips_the_alert_request(tmp_path):
    http = boston()
    report = provider(http, tmp_path).fetch(BOS, periods=1, include_alerts=False)
    assert report.alerts_supported is False and not http.calls_to("/alerts/")


def test_fetch_alerts_failure_raises(tmp_path):
    http = FakeHttp().add_error(API + "/alerts/", status=500)
    with pytest.raises(WeatherError) as ei:
        provider(http, tmp_path).fetch_alerts(BOS)
    assert ei.value.user_message == "Alerts unavailable, try later"


# --------------------------------------------------------------------------- mapping helpers


@pytest.mark.parametrize("obj, kind, expected", [
    ({"unitCode": "wmoUnit:degC", "value": 3}, "temperature", 3.0),
    ({"unitCode": "wmoUnit:degF", "value": 212}, "temperature", 100.0),
    ({"unitCode": "unit:degC", "value": -2.5}, "temperature", -2.5),  # older namespace prefix
    ({"unitCode": "wmoUnit:km_h-1", "value": 25.92}, "speed", 25.92),
    ({"unitCode": "wmoUnit:m_s-1", "value": 10}, "speed", 36.0),
    ({"unitCode": "wmoUnit:Pa", "value": 100135.51}, "pressure", 1001.3551),
    ({"unitCode": "wmoUnit:m", "value": 16093.44}, "distance", 16.09344),
    ({"unitCode": "wmoUnit:percent", "value": 93.1}, "percent", 93.1),
    ({"unitCode": "wmoUnit:degree_(angle)", "value": 350}, "angle", 350.0),
    ({"unitCode": "wmoUnit:km_h-1", "minValue": 9, "maxValue": 19}, "speed", 19.0),
    ({"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "Z"}, "temperature", None),
    ({"unitCode": "wmoUnit:furlong", "value": 1}, "distance", None),
    ({"unitCode": "wmoUnit:Pa", "value": 1000}, "temperature", None),  # unit of another kind
    ({"unitCode": "wmoUnit:degC", "value": True}, "temperature", None),
    ({"value": 5}, "speed", 5.0),
    (None, "temperature", None),
])
def test_quantity(obj, kind, expected):
    got = quantity(obj, kind)
    assert got == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("text, kmh", [
    ("6 mph", 6 * MPH), ("6 to 12 mph", 12 * MPH), ("0 mph", 0.0), ("0 to 5 mph", 5 * MPH),
    ("9 km/h", 9.0), ("9 to 19 km/h", 19.0), (" 7 mph ", 7 * MPH),
    ({"unitCode": "wmoUnit:km_h-1", "minValue": 9, "maxValue": 19}, 19.0),
    ("", None), (None, None), ("calm", None), ("10 knots", None), ("5-10 mph", None),
])
def test_parse_wind_speed(text, kmh):
    got = parse_wind_speed(text)
    assert got == (pytest.approx(kmh) if kmh is not None else None)


def test_forecast_period_metric_units_and_nulls():
    period = {"name": "Tonight", "startTime": "2026-11-13T18:00:00-05:00", "endTime": "2026-11-14T06:00:00-05:00",
              "isDaytime": False, "temperature": -3, "temperatureUnit": "C",
              "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": None},
              "windSpeed": "9 to 19 km/h", "windDirection": "", "shortForecast": "Snow",
              "detailedForecast": "Snow. North wind 9 to 19 km/h, with gusts as high as 45 km/h."}
    p = map_forecast_period(period)
    assert (p.temp_low_c, p.temp_high_c, p.precip_chance_pct) == (-3.0, None, None)
    assert (p.wind_speed_kmh, p.wind_gust_kmh, p.wind_dir) == (19.0, 45.0, None)
    assert p.start == datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)


def test_observation_mapping_variants():
    body = load_fixture("nws_observation_latest_02108.json")
    props = body["properties"]
    props["textDescription"] = ""
    props["heatIndex"]["value"] = 30.0
    props["seaLevelPressure"]["value"] = 101000
    props["windSpeed"]["value"] = 0  # calm: NWS sends direction 0
    props["windDirection"]["value"] = 0
    cur = map_observation(body)
    assert cur.description is None
    assert cur.feels_like_c == 30.0
    assert cur.pressure_hpa == pytest.approx(1001.3551)  # the altimeter setting wins over sea-level pressure
    assert cur.wind_speed_kmh == 0.0 and cur.wind_dir_deg is None

    props["windSpeed"]["value"] = 18.0
    props["windDirection"]["value"] = 360  # north
    props["barometricPressure"]["value"] = None
    cur = map_observation(body)
    assert cur.wind_dir_deg == 360.0 and cur.pressure_hpa == 1010.0  # no altimeter: sea-level pressure


def test_all_null_observation_maps_to_none():
    cur = map_observation(load_fixture("nws_observation_latest_TJSJ_nulls.json"))
    assert cur.station == "TJSJ" and cur.description == "Mostly Clear"
    assert cur.temperature_c is None and cur.wind_speed_kmh is None and cur.wind_dir_deg is None
    assert cur.pressure_hpa is None and cur.feels_like_c is None
    assert cur.visibility_km == pytest.approx(16.09)
