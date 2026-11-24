"""Tests for meshwx.transit.mbta (real MBTA V3 /predictions captures)."""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.errors import TransitError
from meshwx.models import TransitStop
from meshwx.timeutil import from_epoch
from meshwx.transit.mbta import MbtaProvider

from conftest import FakeHttp, load_fixture, make_config

PRED = "https://api-v3.mbta.com/predictions"
PARK_NOW = from_epoch(1794573834)  # HTTP Date of the Park Street capture
BUS_NOW = from_epoch(1794573839)  # HTTP Date of the stop 1317 capture
EST = timezone(timedelta(hours=-5))

# CAPTURE_NOTES section 3 (shown rows, sorted): (stop, headsign, epoch, direction)
EXPECTED_PARK = [
    ("70075", "Ashmont", 1794573858, 0),
    ("70076", "Alewife", 1794573891, 1),
    ("70076", "Alewife", 1794573964, 1),
    ("70076", "Alewife", 1794574156, 1),
    ("70075", "Braintree", 1794574264, 0),
    ("70076", "Alewife", 1794574300, 1),
    ("70075", "Ashmont", 1794574605, 0),
]
# CAPTURE_NOTES section 4: (route, headsign, epoch); the ADDED "-OL1" trips only have a departure time
EXPECTED_BUS = [
    ("39", "Back Bay", 1794573926),
    ("39", "Back Bay", 1794574140),
    ("66", "Nubian via Allston", 1794574214),
    ("39", "Back Bay", 1794574602),
    ("39", "Back Bay", 1794574733),
    ("39", "Back Bay", 1794574740),
    ("66", "Nubian via Allston", 1794575445),
]


def stop(id_: str, label: str = "", routes: tuple = (), direction_id: Optional[int] = None) -> TransitStop:
    return TransitStop(id_, label or id_, routes, direction_id)


def provider(http: FakeHttp, **mbta: Any) -> MbtaProvider:
    cfg = make_config({"transit": {"enabled": True, "provider": "mbta", "stops": ["place-pktrm"], "mbta": mbta}})
    return MbtaProvider(cfg.transit, http, None)  # type: ignore[arg-type] - cache_dir unused


def fixture_http() -> FakeHttp:
    return (FakeHttp().add_json(PRED + "?filter%5Bstop%5D=place-pktrm", "mbta_predictions_sample.json")
            .add_json(PRED + "?filter%5Bstop%5D=1317", "mbta_predictions_bus_1317_sample.json"))


def query(url: str) -> Dict[str, List[str]]:
    return parse_qs(urlsplit(url).query)


def labels(arrivals: List[Any], now) -> List[str]:
    """CONTRACTS countdown rule, as in the "label" column of CAPTURE_NOTES_transit.md."""
    secs = [int((a.time - now).total_seconds()) for a in arrivals]
    return ["Now" if s <= 30 else "%dm" % ((s + 30) // 60) for s in secs]


def iso(epoch: int) -> str:
    return from_epoch(epoch).astimezone(EST).isoformat()


def prediction(pid: str, *, arr: Optional[int] = None, dep: Optional[int] = None, rel: Optional[str] = None,
               status: Optional[str] = None, headsign: Optional[str] = None, route: str = "39", trip: str = "t1",
               direction: int = 1) -> Dict[str, Any]:
    return {
        "id": pid, "type": "prediction",
        "attributes": {"arrival_time": iso(arr) if arr else None, "departure_time": iso(dep) if dep else None,
                       "schedule_relationship": rel, "status": status, "trip_headsign": headsign,
                       "direction_id": direction},
        "relationships": {"route": {"data": {"id": route, "type": "route"}},
                          "trip": {"data": {"id": trip, "type": "trip"}},
                          "stop": {"data": {"id": "1317", "type": "stop"}},
                          "vehicle": {"data": None}},
    }


ROUTE_39 = {"type": "route", "id": "39", "attributes": {
    "short_name": "39", "long_name": "Forest Hills Station - Back Bay Station",
    "direction_destinations": ["Forest Hills Station", "Back Bay Station"]}}


# ============================================================================ real fixtures


def test_park_street_fixture():
    [result] = provider(fixture_http()).get_arrivals([stop("place-pktrm", "Park St")], PARK_NOW, 90)
    assert result.error is None
    assert [(a.stop_id, a.headsign, int(a.time.timestamp()), a.direction_id) for a in result.arrivals] == EXPECTED_PARK
    assert [int((a.time - PARK_NOW).total_seconds()) for a in result.arrivals] == [24, 57, 130, 322, 430, 466, 771]
    assert labels(result.arrivals, PARK_NOW) == ["Now", "1m", "2m", "5m", "7m", "8m", "13m"]
    assert {(a.route_id, a.route_name, a.realtime, a.status) for a in result.arrivals} == {("Red", "Red", True, None)}
    assert all(a.time.utcoffset() == timedelta(0) for a in result.arrivals)


def test_bus_stop_fixture_with_added_and_cancelled_trips():
    [result] = provider(fixture_http()).get_arrivals([stop("1317")], BUS_NOW, 90)
    assert [(a.route_name, a.headsign, int(a.time.timestamp())) for a in result.arrivals] == EXPECTED_BUS
    assert [int((a.time - BUS_NOW).total_seconds()) for a in result.arrivals] == [87, 301, 375, 763, 894, 901, 1606]
    assert labels(result.arrivals, BUS_NOW) == ["1m", "5m", "6m", "13m", "15m", "15m", "27m"]


SPARSE = {"fields[prediction]": ["arrival_time,departure_time,direction_id,schedule_relationship,status,"
                                  "trip_headsign"],
          "fields[route]": ["short_name,long_name,direction_destinations"], "fields[trip]": ["headsign"]}


def test_request_and_api_key():
    http = fixture_http()
    provider(http, api_key="K123").get_arrivals([stop("place-pktrm")], PARK_NOW, 90)
    [(url, headers)] = http.calls
    assert url.startswith(PRED + "?")
    # every prediction of the stop (the page limit only caps the size), only the attributes that are shown
    assert query(url) == dict({"filter[stop]": ["place-pktrm"], "include": ["route,trip"], "sort": ["departure_time"],
                               "page[limit]": ["500"]}, **SPARSE)
    assert headers == {"x-api-key": "K123"}
    http = fixture_http()
    provider(http, base_url="https://api-v3.mbta.com/").get_arrivals([stop("1317")], BUS_NOW, 90)
    assert http.calls[0][1] == {} and http.calls[0][0].startswith(PRED + "?")


def test_route_and_direction_filters_are_applied_then_sent_once_the_route_ids_are_known():
    http = fixture_http()
    mbta = provider(http)
    fenwood = stop("1317", routes=("66",), direction_id=1)
    for _ in range(2):
        [result] = mbta.get_arrivals([fenwood], BUS_NOW, 90)
        # the fixture ignores the filter; the provider always applies it locally
        assert [(a.route_name, int(a.time.timestamp())) for a in result.arrivals] == [("66", 1794574214),
                                                                                      ("66", 1794575445)]
    first, second = (query(url) for url, _ in http.calls)
    assert "filter[route]" not in first and "filter[direction_id]" not in first  # "66" may be a name
    assert (second["filter[route]"], second["filter[direction_id]"]) == (["66"], ["1"])  # learned: a route id
    [park] = provider(fixture_http()).get_arrivals([stop("place-pktrm", direction_id=0)], PARK_NOW, 90)
    assert [a.headsign for a in park.arrivals] == ["Ashmont", "Braintree", "Ashmont"]


def test_route_names_are_translated_to_route_ids():
    now = 1794573834
    doc = {"data": [prediction("r", arr=now + 60, dep=now + 90, route="Red"),
                    prediction("b", arr=now + 120, dep=now + 150, route="Green-B", trip="t2"),
                    prediction("c", arr=now + 180, dep=now + 210, route="Green-C", trip="t3")],
           "included": [{"type": "route", "id": "Red", "attributes": {"short_name": "", "long_name": "Red Line"}},
                        {"type": "route", "id": "Green-B", "attributes": {"short_name": "B", "long_name": "Green B"}},
                        {"type": "route", "id": "Green-C", "attributes": {"short_name": "C", "long_name": "Green C"}}]}
    http = FakeHttp().add_json(PRED, doc)
    mbta = provider(http)
    park = stop("place-pktrm", routes=("red", " B "))
    for _ in range(2):
        [result] = mbta.get_arrivals([park], from_epoch(now), 90)
        assert [a.route_name for a in result.arrivals] == ["Red", "B"]
    assert "filter[route]" not in query(http.calls[0][0])
    assert query(http.calls[1][0])["filter[route]"] == ["Green-B,Red"]
    # a route without predictions yet (night): no server filter until every configured route is known
    unknown = stop("place-pktrm", routes=("Red", "Mattapan"))
    for _ in range(2):
        mbta.get_arrivals([unknown], from_epoch(now), 90)
    assert ["filter[route]" in query(url) for url, _ in http.calls[2:]] == [False, False]


def test_hidden_predictions_and_boarding_status():
    now = 1794573834
    doc = {"data": [
        prediction("cancelled", rel="CANCELLED"),
        prediction("skipped", arr=now + 60, dep=now + 90, rel="SKIPPED"),
        prediction("nodata", rel="NO_DATA"),
        prediction("dropoff", arr=now + 120),  # last stop: no departure
        prediction("departed", arr=now - 100, dep=now - 5),
        prediction("boarding", arr=now - 20, dep=now + 40, status="Boarding", trip="t2"),
        prediction("added", dep=now + 300, rel="ADDED", trip="t3"),
        prediction("override", arr=now + 400, dep=now + 400, headsign="Ruggles", trip="t1"),
        prediction("no-trip", arr=now + 500, dep=now + 500, trip="missing", direction=0),
    ], "included": [ROUTE_39,
                    {"type": "trip", "id": "t1", "attributes": {"headsign": "Back Bay"}},
                    {"type": "trip", "id": "t2", "attributes": {"headsign": "Back Bay"}},
                    {"type": "trip", "id": "t3", "attributes": {"headsign": "Back Bay"}}]}
    http = FakeHttp().add_json(PRED, doc)
    [result] = provider(http).get_arrivals([stop("1317")], from_epoch(now), 90)
    assert [(int(a.time.timestamp()) - now, a.headsign, a.status) for a in result.arrivals] == [
        (-20, "Back Bay", "Boarding"),
        (300, "Back Bay", None),
        (400, "Ruggles", None),  # trip_headsign override wins
        (500, "Forest Hills Station", None),  # route direction_destinations[direction_id]
    ]
    assert {a.route_name for a in result.arrivals} == {"39"}


def test_vehicle_standing_at_the_stop_counts_as_arriving_now():
    now = 1794573834
    doc = {"data": [prediction("held", arr=now - 150, dep=now + 45, status="Stopped at station"),
                    prediction("left", arr=now - 150, dep=now - 5, trip="t2")],
           "included": [ROUTE_39]}
    [result] = provider(FakeHttp().add_json(PRED, doc)).get_arrivals([stop("1317")], from_epoch(now), 90)
    assert [(a.time, a.status) for a in result.arrivals] == [(from_epoch(now), "Stopped at station")]


def test_route_names():
    now = 1794573834
    doc = {"data": [prediction("a", arr=now + 60, dep=now + 60, route="CR-Worcester"),
                    prediction("b", arr=now + 70, dep=now + 70, route="Green-B"),
                    prediction("c", arr=now + 80, dep=now + 80, route="Mattapan")],
           "included": [
               {"type": "route", "id": "CR-Worcester",
                "attributes": {"short_name": "", "long_name": "Framingham/Worcester Line"}},
               {"type": "route", "id": "Green-B", "attributes": {"short_name": "B", "long_name": "Green Line B"}},
               {"type": "route", "id": "Mattapan", "attributes": {"short_name": "", "long_name": "Mattapan Trolley"}}]}
    [result] = provider(FakeHttp().add_json(PRED, doc)).get_arrivals([stop("x")], from_epoch(now), 90)
    assert [a.route_name for a in result.arrivals] == ["Framingham/Worcester", "B", "Mattapan Trolley"]


def test_horizon():
    [result] = provider(fixture_http()).get_arrivals([stop("place-pktrm")], PARK_NOW, 5)
    assert [int((a.time - PARK_NOW).total_seconds()) for a in result.arrivals] == [24, 57, 130]


def test_rate_limit_stops_further_requests():
    http = FakeHttp().add_error(PRED, 429)
    with pytest.raises(TransitError) as ei:
        provider(http).get_arrivals([stop("place-pktrm"), stop("1317")], PARK_NOW, 90)
    assert ei.value.user_message == "Rate limited, try later"
    assert len(http.calls) == 1


def test_one_failing_stop_does_not_hide_the_others():
    http = (FakeHttp().add_error(PRED + "?filter%5Bstop%5D=bad", 503)
            .add_error(PRED + "?filter%5Bstop%5D=gone", 404))
    http.routes.extend(fixture_http().routes)
    bad, ok, gone = provider(http).get_arrivals([stop("bad"), stop("1317"), stop("gone")], BUS_NOW, 90)
    assert bad.error == "Transit unavailable, try later" and bad.arrivals == []
    assert ok.error is None and len(ok.arrivals) == 7
    assert gone.error == "Stop not found"


@pytest.mark.parametrize("body", [{"errors": [{"status": "400"}]}, [], {"data": None}])
def test_unexpected_body(body):
    with pytest.raises(TransitError) as ei:
        provider(FakeHttp().add_json(PRED, body)).get_arrivals([stop("1317")], BUS_NOW, 90)
    assert ei.value.user_message == "Transit feed unreadable"
