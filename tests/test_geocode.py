"""Tests for meshwx.geocode.ZipGeocoder (real zippopotam.us / Open-Meteo captures, FakeHttp)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List

import pytest

from meshwx import geocode
from meshwx.errors import GeocodeError
from meshwx.geocode import ZipGeocoder, territory_for_zip
from meshwx.models import Location

from conftest import FakeHttp

ZIPPO = "https://api.zippopotam.us/"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
BOSTON = Location(42.3576, -71.0684, "Boston, MA", zip_code="02108", country="US")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _om_result(name: str, postcodes: List[str], cc: str = "US", admin1: str = "Massachusetts",
               lat: float = 42.0, lon: float = -71.0) -> Dict[str, Any]:
    return {"name": name, "latitude": lat, "longitude": lon, "country_code": cc, "admin1": admin1,
            "timezone": "America/New_York", "postcodes": postcodes}


def _synthetic_zippo(url: str) -> Dict[str, Any]:
    code = url.rsplit("/", 1)[1]
    return {"post code": code, "places": [{"place name": "Town" + code, "latitude": "40.1",
                                           "longitude": "-75.2", "state abbreviation": "PA"}]}


# --------------------------------------------------------------------------- zippopotam.us


def test_zippopotam_hit():
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    assert ZipGeocoder(http).lookup("02108") == BOSTON
    assert http.calls_to("zippopotam") == [ZIPPO + "us/02108"]


def test_anchorage():
    http = FakeHttp().add_json(ZIPPO + "us/99501", "zippopotam_99501.json")
    loc = ZipGeocoder(http).lookup("99501", "US")
    assert (loc.name, loc.latitude, loc.longitude) == ("Anchorage, AK", 61.2116, -149.8761)


@pytest.mark.parametrize("raw", ["02108-1234", " 02108 ", "021081234"])
def test_zip_is_normalised_before_lookup(raw):
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    assert ZipGeocoder(http).lookup(raw).zip_code == "02108"
    assert http.calls_to("zippopotam") == [ZIPPO + "us/02108"]


@pytest.mark.parametrize("bad", ["2108", "ABCDE", "", "123456"])
def test_invalid_zip_rejected_without_network(bad):
    http = FakeHttp()
    with pytest.raises(GeocodeError) as ei:
        ZipGeocoder(http).lookup(bad)
    assert ei.value.user_message == "ZIP must be 5 digits"
    assert http.calls == []


def test_puerto_rico_resolves_under_territory_code():
    http = (FakeHttp().add_json(ZIPPO + "us/00901", {}, status=404)  # real 404 body is "{}"
            .add_json(ZIPPO + "pr/00901", "zippopotam_pr_00901.json"))
    loc = ZipGeocoder(http).lookup("00901")
    assert loc == Location(18.4659, -66.1036, "San Juan, PR", zip_code="00901", country="US",
                           timezone="America/Puerto_Rico")
    assert http.calls_to("zippopotam") == [ZIPPO + "us/00901", ZIPPO + "pr/00901"]


def _place(name: str, lat: str, lon: str, abbr: str) -> dict:
    return {"place name": name, "latitude": lat, "longitude": lon, "state": "", "state abbreviation": abbr}


def test_territory_place_outside_the_territory_is_skipped():
    # live /gu/96932 (2026-11-23): the two-letter "GU" entry has the wrong longitude sign
    body = {"places": [_place("Hagatna", "13.4443", "144.7863", "66"), _place("Hagatna", "13.4751", "-144.7484", "GU")]}
    http = FakeHttp().add_json(ZIPPO + "us/96932", {}, status=404).add_json(ZIPPO + "gu/96932", body)
    loc = ZipGeocoder(http).lookup("96932")
    assert loc == Location(13.4443, 144.7863, "Hagatna, GU", zip_code="96932", country="US", timezone="Pacific/Guam")


def test_territory_place_with_flipped_longitude_sign_is_repaired():
    body = {"places": [_place("Barrigada", "13.4616", "-144.7947", "GU")]}
    http = FakeHttp().add_json(ZIPPO + "us/96921", {}, status=404).add_json(ZIPPO + "gu/96921", body)
    loc = ZipGeocoder(http).lookup("96921")
    assert (loc.latitude, loc.longitude, loc.name) == (13.4616, 144.7947, "Barrigada, GU")


def test_us_answer_outside_the_territory_tries_the_territory_path():
    # live /us/96951 (Rota, MP) answers 200 with a point in Texas
    http = (FakeHttp().add_json(ZIPPO + "us/96951", {"places": [_place("Rota", "31.8243", "-101.0372", "MP ")]})
            .add_json(ZIPPO + "mp/96951", {"places": [_place("Rota", "15.1901", "145.7408", "69")]}))
    loc = ZipGeocoder(http).lookup("96951")
    assert loc == Location(15.1901, 145.7408, "Rota, MP", zip_code="96951", country="US", timezone="Pacific/Saipan")
    assert http.calls_to("zippopotam") == [ZIPPO + "us/96951", ZIPPO + "mp/96951"]


def test_territory_with_no_plausible_place_falls_back_to_openmeteo():
    body = {"places": [_place("Nowhere", "31.8243", "-101.0372", "MP")]}
    om = {"results": [_om_result("Rota", ["96951"], cc="MP", admin1="Rota", lat=14.15, lon=145.2)]}
    http = (FakeHttp().add_json(ZIPPO + "us/96951", body).add_json(ZIPPO + "mp/96951", body)
            .add_json(OM_GEO, om))
    loc = ZipGeocoder(http).lookup("96951")
    assert (loc.latitude, loc.name, loc.timezone) == (14.15, "Rota, MP", "Pacific/Saipan")


@pytest.mark.parametrize("zip5, cc, lat, lon, zone", [
    ("00802", "vi", "18.3223", "-64.9637", "America/St_Thomas"),
    ("96799", "as", "-14.2766", "-170.7046", "Pacific/Pago_Pago"),
])
def test_territory_locations_carry_their_time_zone(zip5, cc, lat, lon, zone):
    http = (FakeHttp().add_json(ZIPPO + "us/" + zip5, {}, status=404)
            .add_json(ZIPPO + cc + "/" + zip5, {"places": [_place("Town", lat, lon, "78")]}))
    loc = ZipGeocoder(http).lookup(zip5)
    assert loc.timezone == zone and loc.name == "Town, " + cc.upper()


@pytest.mark.parametrize("zip5, expected", [
    ("00601", "pr"), ("00731", "pr"), ("00901", "pr"), ("00802", "vi"),
    ("96910", "gu"), ("96932", "gu"), ("96939", "gu"), ("96940", None),
    ("96799", "as"), ("96798", None), ("96950", "mp"), ("96952", "mp"), ("96960", None),
    ("02108", None), ("99501", None), ("00501", None),
])
def test_territory_for_zip(zip5, expected):
    assert territory_for_zip(zip5) == expected


def test_place_prefers_two_letter_state_and_strips_text():
    body = {"post code": "00680", "places": [
        {"place name": "Mayaguez", "longitude": "-67.5081", "latitude": "18.2011", "state": "Pr",
         "state abbreviation": "72"},
        {"place name": " Mayaguez ", "longitude": "-67.0988", "latitude": "18.2108", "state": "Puerto Rico ",
         "state abbreviation": "PR "},
    ]}
    http = FakeHttp().add_json(ZIPPO + "us/00680", {}, status=404).add_json(ZIPPO + "pr/00680", body)
    loc = ZipGeocoder(http).lookup("00680")
    assert (loc.name, loc.latitude, loc.longitude) == ("Mayaguez, PR", 18.2108, -67.0988)


def test_place_without_valid_state_abbreviation_uses_first_place():
    body = {"places": [{"place name": "Somewhere", "latitude": "40", "longitude": "-75", "state abbreviation": "7"},
                       {"place name": "Elsewhere", "latitude": "41", "longitude": "-76", "state abbreviation": ""}]}
    http = FakeHttp().add_json(ZIPPO + "us/19999", body)
    loc = ZipGeocoder(http).lookup("19999")
    assert (loc.name, loc.latitude) == ("Somewhere", 40.0)


# --------------------------------------------------------------------------- Open-Meteo fallback


def test_openmeteo_fallback_uses_exact_postcode_and_timezone(caplog):
    http = (FakeHttp().add_json(ZIPPO + "us/02108", {}, status=404)
            .add_json(OM_GEO, "openmeteo_geocode_02108.json"))
    with caplog.at_level(logging.INFO, logger="meshwx.geocode"):
        loc = ZipGeocoder(http).lookup("02108")
    assert loc == Location(42.35843, -71.05977, "Boston, MA", zip_code="02108", country="US",
                           timezone="America/New_York")
    url = http.calls_to("open-meteo")[0]
    assert "name=02108" in url and "countryCode=US" in url
    assert "city-centre" in caplog.text


def test_openmeteo_prefix_matches_are_rejected():
    # what "name=2108"-style prefix matching returns: places whose postcodes merely start alike
    body = {"results": [_om_result("Joppa", ["21085"], admin1="Maryland"),
                        _om_result("Kingsville", ["21087"], admin1="Maryland")], "generationtime_ms": 0.1}
    http = FakeHttp().add_json(ZIPPO + "us/21080", {}, status=404).add_json(OM_GEO, body)
    with pytest.raises(GeocodeError) as ei:
        ZipGeocoder(http).lookup("21080")
    assert ei.value.user_message == "ZIP 21080 not found"


def test_openmeteo_picks_exact_match_not_first_result():
    body = {"results": [
        _om_result("Saint-Quentin", ["02108 CEDEX", "02108"], cc="FR", admin1="Hauts-de-France"),
        _om_result("Cambridge", ["02138", "02139"]),
        _om_result("Boston", ["02107", "02108"], lat=42.35843, lon=-71.05977),
    ]}
    http = FakeHttp().add_error(ZIPPO, status=503).add_json(OM_GEO, body)
    loc = ZipGeocoder(http).lookup("02108")
    assert (loc.name, loc.latitude, loc.timezone) == ("Boston, MA", 42.35843, "America/New_York")


def test_openmeteo_for_territory_queries_territory_country():
    body = {"results": [_om_result("Mayaguez", ["00680"], cc="PR", admin1="Mayaguez", lat=18.20, lon=-67.14)]}
    http = (FakeHttp().add_json(ZIPPO + "us/00680", {}, status=404)
            .add_json(ZIPPO + "pr/00680", {}, status=404).add_json(OM_GEO, body))
    loc = ZipGeocoder(http).lookup("00680")
    assert loc.name == "Mayaguez, PR" and "countryCode=PR" in http.calls_to("open-meteo")[0]


# --------------------------------------------------------------------------- errors and negative cache


def test_not_found_is_negative_cached_for_a_day():
    clock = Clock()
    http = (FakeHttp().add_json(ZIPPO + "us/00000", {}, status=404)
            .add_json(OM_GEO, "openmeteo_geocode_00000.json"))  # 200 without a "results" key
    geo = ZipGeocoder(http, clock=clock)
    for _ in range(2):
        with pytest.raises(GeocodeError) as ei:
            geo.lookup("00000")
        assert ei.value.user_message == "ZIP 00000 not found"
    assert len(http.calls) == 2  # the second lookup was answered from the negative cache
    clock.t += 24 * 3600 + 1
    with pytest.raises(GeocodeError):
        geo.lookup("00000")
    assert len(http.calls) == 4


def test_network_failure_is_reported_and_not_cached():
    http = FakeHttp().add_error(ZIPPO, status=503).add_error(OM_GEO, status=None)
    geo = ZipGeocoder(http)
    for _ in range(2):
        with pytest.raises(GeocodeError) as ei:
            geo.lookup("02108")
        assert ei.value.user_message == "ZIP lookup failed, try later"
    assert len(http.calls) == 4


def test_not_found_on_one_source_but_other_unreachable_is_a_failure():
    http = FakeHttp().add_json(ZIPPO + "us/02108", {}, status=404).add_error(OM_GEO, status=503)
    with pytest.raises(GeocodeError) as ei:
        ZipGeocoder(http).lookup("02108")
    assert ei.value.user_message == "ZIP lookup failed, try later"


def test_malformed_zippopotam_body_falls_through_to_openmeteo():
    http = (FakeHttp().add_json(ZIPPO + "us/02108", {"places": [{"place name": "X", "latitude": "abc"}]})
            .add_json(OM_GEO, "openmeteo_geocode_02108.json"))
    assert ZipGeocoder(http).lookup("02108").latitude == 42.35843


# --------------------------------------------------------------------------- caching and persistence


def test_memory_cache_hit_needs_no_network():
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    geo = ZipGeocoder(http)
    assert geo.lookup("02108") == geo.lookup("02108-0001") == BOSTON
    assert len(http.calls) == 1


def test_found_locations_persist_across_instances(tmp_path):
    path = tmp_path / "cache" / "geocode.json"
    http = (FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
            .add_json(ZIPPO + "us/00000", {}, status=404).add_json(OM_GEO, "openmeteo_geocode_00000.json"))
    geo = ZipGeocoder(http, path)
    first = geo.lookup("02108")
    with pytest.raises(GeocodeError):
        geo.lookup("00000")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert [e["key"] for e in doc["locations"]] == ["us:02108"]  # "not found" is never written
    offline = FakeHttp()
    assert ZipGeocoder(offline, path).lookup("02108") == first
    assert offline.calls == []


def test_corrupt_cache_file_is_ignored_and_rewritten(tmp_path):
    path = tmp_path / "geocode.json"
    path.write_text("{oops", encoding="utf-8")
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    assert ZipGeocoder(http, path).lookup("02108") == BOSTON
    assert json.loads(path.read_text(encoding="utf-8"))["locations"][0]["key"] == "us:02108"


def test_bad_cache_entries_are_dropped(tmp_path):
    path = tmp_path / "geocode.json"
    good = dict(key="us:99501", latitude=61.2116, longitude=-149.8761, name="Anchorage, AK",
                zip_code="99501", country="US", timezone=None)
    path.write_text(json.dumps({"version": geocode.CACHE_VERSION,
                                "locations": [{"key": "us:02108", "name": ""}, "junk", good]}), encoding="utf-8")
    http = FakeHttp()
    assert ZipGeocoder(http, path).lookup("99501").name == "Anchorage, AK"
    assert http.calls == []


def test_old_cache_version_drops_only_territory_entries(tmp_path):
    path = tmp_path / "geocode.json"
    boston = dict(key="us:02108", latitude=42.3576, longitude=-71.0684, name="Boston, MA", zip_code="02108",
                  country="US", timezone=None)
    rota = dict(key="us:96951", latitude=31.8243, longitude=-101.0372, name="Rota, MP", zip_code="96951",
                country="US", timezone=None)  # what version 2 cached for Rota: a point in Texas
    path.write_text(json.dumps({"version": 2, "locations": [boston, rota]}), encoding="utf-8")
    http = (FakeHttp().add_json(ZIPPO + "us/96951", {}, status=404)
            .add_json(ZIPPO + "mp/96951", {"places": [_place("Rota", "15.1901", "145.7408", "69")]}))
    geo = ZipGeocoder(http, path)
    assert geo.lookup("02108") == BOSTON and http.calls == []
    assert geo.lookup("96951").latitude == 15.1901  # looked up again
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == geocode.CACHE_VERSION
    assert [e["key"] for e in doc["locations"]] == ["us:02108", "us:96951"]


@pytest.mark.parametrize("version", [None, 99, "3"])
def test_cache_of_unknown_version_is_ignored(tmp_path, version):
    path = tmp_path / "geocode.json"
    entry = dict(key="us:02108", latitude=1.0, longitude=2.0, name="Elsewhere", zip_code="02108", country="US")
    path.write_text(json.dumps({"version": version, "locations": [entry]}), encoding="utf-8")
    http = FakeHttp().add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    assert ZipGeocoder(http, path).lookup("02108") == BOSTON and len(http.calls) == 1


def test_cache_is_bounded_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(geocode, "MAX_FOUND", 2)
    path = tmp_path / "geocode.json"
    http = FakeHttp().add_json(ZIPPO + "us/", _synthetic_zippo)
    geo = ZipGeocoder(http, path)
    for code in ("10001", "10002", "10001", "10003"):  # 10001 used again -> 10002 is the oldest
        geo.lookup(code)
    keys = [e["key"] for e in json.loads(path.read_text(encoding="utf-8"))["locations"]]
    assert keys == ["us:10001", "us:10003"]
    reloaded = ZipGeocoder(FakeHttp(), path)  # order survives a restart
    assert reloaded.lookup("10003").name == "Town10003, PA"


def test_concurrent_lookups_are_safe(tmp_path):
    http = FakeHttp().add_json(ZIPPO + "us/", _synthetic_zippo)
    geo = ZipGeocoder(http, tmp_path / "geocode.json")
    results: List[Location] = []
    errors: List[BaseException] = []

    def worker(i: int) -> None:
        try:
            results.append(geo.lookup("%05d" % (20000 + i % 5)))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 20
    assert len(json.loads((tmp_path / "geocode.json").read_text(encoding="utf-8"))["locations"]) == 5
