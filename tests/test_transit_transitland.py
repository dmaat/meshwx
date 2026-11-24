"""Tests for meshwx.transit.transitland.

No live capture exists (an API key is required), so the responses below are
synthetic, built from the documented ``/stops/{key}/departures`` shape: each
departure carries ``arrival`` / ``departure`` objects with ``scheduled`` and
``estimated`` service-day ``HH:MM:SS`` values plus ``*_utc`` / ``*_local``
ISO timestamps, a ``schedule_relationship`` and a ``trip`` with its ``route``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.errors import TransitError
from meshwx.models import TransitStop
from meshwx.transit.transitland import TransitlandProvider

from conftest import FakeHttp, make_config

API = "https://transit.land/api/v2/rest"
STOP = TransitStop("f-drt-mbta:1317", "Fenwood")
URL = API + "/stops/f-drt-mbta:1317/departures"
NOW = datetime(2026, 11, 13, 12, 44, 0, tzinfo=timezone.utc)


def utc(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).astimezone(timezone(timedelta(hours=-5))).isoformat()


def event(scheduled: Optional[str] = None, estimated: Optional[str] = None, *,
          scheduled_local: Optional[str] = None, estimated_local: Optional[str] = None) -> Dict[str, Any]:
    return {
        "scheduled": "25:10:00", "estimated": "26:00:00",  # service-day local times: never used for arithmetic
        "scheduled_utc": scheduled, "estimated_utc": estimated,
        "scheduled_local": scheduled_local, "estimated_local": estimated_local,
        "estimated_delay": None, "delay": None, "uncertainty": None,
    }


def departure(trip_id: str, *, dep: Optional[Dict[str, Any]] = None, arr: Optional[Dict[str, Any]] = None,
              rel: str = "SCHEDULED", trip_rel: Optional[str] = None,
              route: Tuple[str, str, str] = ("39", "39", "Forest Hills Station - Back Bay Station"),
              headsign: str = "Back Bay", stop_headsign: Optional[str] = None, pickup_type: Optional[int] = 0,
              direction: int = 1) -> Dict[str, Any]:
    return {
        "arrival": arr or event(), "departure": dep or event(),
        "schedule_relationship": rel, "stop_headsign": stop_headsign, "stop_sequence": 15,
        "service_date": "2026-11-13", "pickup_type": pickup_type, "drop_off_type": 0,
        "trip": {"trip_id": trip_id, "trip_headsign": headsign, "direction_id": direction,
                 "schedule_relationship": trip_rel,
                 "route": {"route_id": route[0], "route_short_name": route[1], "route_long_name": route[2],
                           "route_type": 3, "agency": {"agency_name": "MBTA", "onestop_id": "o-drt-mbta"}}},
    }


def response(*departures: Dict[str, Any], stop_id: str = "1317") -> Dict[str, Any]:
    return {"stops": [{"id": 4242, "onestop_id": "s-drt0mbta-huntingtonave~fenwoodrd", "stop_id": stop_id,
                       "stop_name": "Huntington Ave opp Fenwood Rd", "stop_timezone": "America/New_York",
                       "departures": list(departures)}],
            "meta": {"after": 1, "next": URL + "?after=1"}}


def provider(http: FakeHttp, **transitland: Any) -> TransitlandProvider:
    tl = {"api_key": "TLKEY"}
    tl.update(transitland)
    cfg = make_config({"transit": {"enabled": True, "provider": "transitland", "stops": [STOP.id],
                                   "transitland": tl}})
    return TransitlandProvider(cfg.transit, http, Path())


def arrivals_of(doc: Any, horizon: int = 90):
    [result] = provider(FakeHttp().add_json(URL, doc)).get_arrivals([STOP], NOW, horizon)
    return result.arrivals


def summary(arrivals) -> list:
    return [(a.route_name, a.headsign, round((a.time - NOW).total_seconds() / 60, 1), a.realtime) for a in arrivals]


def test_time_selection_order():
    doc = response(
        departure("rt", dep=event(utc(6), utc(7)), arr=event(utc(5), utc(4))),  # departure estimate wins
        departure("static", rel="STATIC", dep=event(utc(10))),
        departure("arrival-estimate", dep=event(), arr=event(utc(12), utc(13))),
        departure("arrival-schedule", dep=event(), arr=event(utc(14))),
        departure("nodata", rel="NO_DATA", dep=event(utc(16))),  # no prediction: shown as scheduled
        departure("local-only", dep=event(scheduled_local=local(18))),
        departure("no-times"),
    )
    assert summary(arrivals_of(doc)) == [
        ("39", "Back Bay", 7.0, True),
        ("39", "Back Bay", 10.0, False),
        ("39", "Back Bay", 13.0, True),
        ("39", "Back Bay", 14.0, False),
        ("39", "Back Bay", 16.0, False),
        ("39", "Back Bay", 18.0, False),
    ]


def test_hidden_departures():
    doc = response(
        departure("cancelled", rel="CANCELED", dep=event(utc(5), utc(5))),
        departure("trip-cancelled", trip_rel="CANCELED", dep=event(utc(6))),
        departure("skipped", rel="SKIPPED", dep=event(utc(7), utc(7))),
        departure("dropoff-only", pickup_type=1, dep=event(utc(8), utc(8))),
        departure("departed", dep=event(utc(-3), utc(-1)), arr=event(utc(-4), utc(-2))),
        departure("ok", dep=event(utc(9), utc(9)), pickup_type=None),
    )
    assert summary(arrivals_of(doc)) == [("39", "Back Bay", 9.0, True)]


def test_names_headsigns_and_directions():
    doc = response(
        departure("a", dep=event(utc(3), utc(3)), route=("Red", "", "Red Line"), headsign="Ashmont", direction=0),
        departure("b", dep=event(utc(4), utc(4)), route=("CR-Worcester", "", "Framingham/Worcester Line"),
                  headsign="", stop_headsign="South Station"),
        departure("c", dep=event(utc(5), utc(5)), headsign="", stop_headsign=None),
    )
    arrivals = arrivals_of(doc)
    assert [(a.route_id, a.route_name, a.headsign, a.direction_id) for a in arrivals] == [
        ("Red", "Red", "Ashmont", 0),
        ("CR-Worcester", "Framingham/Worcester", "South Station", 1),
        ("39", "39", None, 1),
    ]
    assert {a.stop_id for a in arrivals} == {"1317"}
    assert all(a.time.utcoffset() == timedelta(0) for a in arrivals)


def test_request():
    http = FakeHttp().add_json(URL, response())
    [result] = provider(http).get_arrivals([STOP], NOW, 45)
    assert result.arrivals == [] and result.error is None
    [(url, headers)] = http.calls
    assert url.startswith(URL + "?") and headers == {"apikey": "TLKEY"}
    assert parse_qs(urlsplit(url).query) == {"next": ["2700"], "limit": ["30"], "use_service_window": ["false"]}


def test_horizon_and_route_filter():
    doc = response(departure("a", dep=event(utc(5), utc(5))),
                   departure("b", dep=event(utc(50), utc(50)), route=("66", "66", "Harvard - Nubian")),
                   departure("c", dep=event(utc(80), utc(80))))
    assert [a.route_name for a in arrivals_of(doc, horizon=60)] == ["39", "66"]
    filtered = provider(FakeHttp().add_json(URL, doc)).get_arrivals(
        [TransitStop(STOP.id, "Fenwood", ("66",))], NOW, 90)
    assert [a.route_name for a in filtered[0].arrivals] == ["66"]


@pytest.mark.parametrize("status, message", [
    (429, "Rate limited, try later"),
    (401, "Transitland API key rejected"),
    (404, "Stop not found"),
    (503, "Transit unavailable, try later"),
])
def test_http_errors(status, message):
    with pytest.raises(TransitError) as ei:
        provider(FakeHttp().add_error(URL, status)).get_arrivals([STOP], NOW, 90)
    assert ei.value.user_message == message


def test_unexpected_body():
    with pytest.raises(TransitError) as ei:
        arrivals_of({"error": "unknown stop"})
    assert ei.value.user_message == "Transit feed unreadable"
