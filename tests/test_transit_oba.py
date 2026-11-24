"""Tests for meshwx.transit.onebusaway (synthetic document in the OneBusAway format, stop 1_10001)."""

from __future__ import annotations

import copy
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.errors import TransitError
from meshwx.models import TransitStop
from meshwx.timeutil import from_epoch
from meshwx.transit.onebusaway import OneBusAwayProvider

from conftest import FakeHttp, load_fixture, make_config

BASE = "https://api.pugetsound.onebusaway.test"
URL = BASE + "/api/where/arrivals-and-departures-for-stop/1_10001.json"
SERVER_MS = 1794571200250  # body currentTime of the fixture
NOW = from_epoch(SERVER_MS / 1000.0)
STOP = TransitStop("1_10001", "Main St")

# CAPTURE_NOTES section 5: (route name, headsign, epoch, realtime, direction).  Predicted
# arrivals have a different scheduled time, so these epochs also prove the prediction wins.
EXPECTED = [
    ("5", "Downtown", 1794572100, True, 1),
    ("5", "Downtown", 1794573180, False, 1),  # schedule only (predicted false, time 0)
    ("8", "Hillcrest", 1794573690, True, 0),
    ("12", "Riverside Park", 1794574050, False, 0),
    ("5", "Downtown", 1794574530, True, 1),
]


def provider(http: FakeHttp, base_url: str = BASE + "/") -> OneBusAwayProvider:
    cfg = make_config({"transit": {"enabled": True, "provider": "onebusaway", "stops": ["1_10001"],
                                   "onebusaway": {"base_url": base_url, "api_key": "TEST"}}})
    return OneBusAwayProvider(cfg.transit, http, Path())


def fixture_doc() -> Dict[str, Any]:
    return load_fixture("oba_arrivals_sample.json")


def arrivals_of(doc: Any, now=NOW, horizon: int = 90) -> List[Any]:
    [result] = provider(FakeHttp().add_json(URL, doc)).get_arrivals([STOP], now, horizon)
    return result.arrivals


def test_fixture():
    arrivals = arrivals_of(fixture_doc())
    assert [(a.route_name, a.headsign, int(a.time.timestamp()), a.realtime, a.direction_id)
            for a in arrivals] == EXPECTED
    assert [int((a.time - NOW).total_seconds()) for a in arrivals] == [899, 1979, 2489, 2849, 3329]
    # CAPTURE_NOTES "label" column: countdown rule, "*" = schedule only
    secs = [int((a.time - NOW).total_seconds()) for a in arrivals]
    assert ["%dm%s" % ((s + 30) // 60, "" if a.realtime else "*") for s, a in zip(secs, arrivals)] == \
        ["15m", "33m*", "41m", "47m*", "55m"]
    assert [a.route_id for a in arrivals] == ["1_100001", "1_100001", "1_100002", "1_100003", "1_100001"]
    assert {a.stop_id for a in arrivals} == {"1_10001"} and all(a.status is None for a in arrivals)
    # the arrival-level headsign wins over references.trips[] ("Downtown Harbor Station")
    assert "Downtown Harbor Station" not in {a.headsign for a in arrivals}


def test_request():
    http = FakeHttp().add_json(URL, fixture_doc())
    provider(http).get_arrivals([STOP], NOW, 75)
    [(url, headers)] = http.calls
    assert url.startswith(URL + "?") and headers == {}
    assert parse_qs(urlsplit(url).query) == {"key": ["TEST"], "minutesBefore": ["0"], "minutesAfter": ["75"]}


def test_horizon():
    assert [int(a.time.timestamp()) for a in arrivals_of(fixture_doc(), horizon=45)] == \
        [1794572100, 1794573180, 1794573690]


def test_route_name_and_headsign_fall_back_to_references():
    doc = fixture_doc()
    first = doc["data"]["entry"]["arrivalsAndDepartures"][0]
    first["routeShortName"] = ""
    first["tripHeadsign"] = ""
    arrivals = arrivals_of(doc)
    assert (arrivals[0].route_name, arrivals[0].headsign) == ("5", "Downtown Harbor Station")


def test_dropoff_only_cancelled_and_departed_are_skipped():
    doc = fixture_doc()
    items = doc["data"]["entry"]["arrivalsAndDepartures"]
    items[0]["departureEnabled"] = False  # last stop of the trip: nobody can board
    items[2]["tripStatus"]["status"] = "CANCELED"
    departed = copy.deepcopy(items[4])
    departed.update(predictedArrivalTime=SERVER_MS - 90000, predictedDepartureTime=SERVER_MS - 30000)
    boarding = copy.deepcopy(items[4])
    boarding.update(predictedArrivalTime=SERVER_MS - 20000, predictedDepartureTime=SERVER_MS + 30000)
    items.extend([departed, boarding])
    secs = [int((a.time - NOW).total_seconds()) for a in arrivals_of(doc)]
    assert secs == [-20, 1979, 2849, 3329]


def test_bus_standing_at_the_stop_counts_as_arriving_now():
    doc = fixture_doc()
    items = doc["data"]["entry"]["arrivalsAndDepartures"]
    held = copy.deepcopy(items[0])
    held.update(predictedArrivalTime=SERVER_MS - 150000, predictedDepartureTime=SERVER_MS + 45000)
    items.append(held)
    first = arrivals_of(doc)[0]
    assert (first.time, first.realtime, first.headsign) == (NOW, True, "Downtown")


def test_predicted_without_arrival_uses_schedule():
    doc = fixture_doc()
    item = doc["data"]["entry"]["arrivalsAndDepartures"][0]
    # a scheduled arrival that differs from every departure time of the item
    item.update(predicted=True, predictedArrivalTime=0, scheduledArrivalTime=1794571980000)
    first = arrivals_of(doc)[0]
    assert (int(first.time.timestamp()), first.realtime) == (1794571980, False)


@pytest.mark.parametrize("code, text, message", [
    (404, "resource not found", "Stop not found"),
    (429, "rate limit exceeded", "Rate limited, try later"),
    (401, "permission denied", "OneBusAway API key rejected"),
    (500, "internal error", "Transit unavailable, try later"),
])
def test_body_status_codes(code, text, message):
    with pytest.raises(TransitError) as ei:
        arrivals_of({"code": code, "currentTime": SERVER_MS, "text": text, "version": 2})
    assert ei.value.user_message == message
    assert text in str(ei.value)


@pytest.mark.parametrize("status, message", [
    (429, "Rate limited, try later"),
    (404, "Stop not found"),
    (200, "Transit feed unreadable"),  # HttpClient.get_json: a 2xx body that is not JSON
])
def test_http_errors(status, message):
    http = FakeHttp().add_error(URL, status)
    with pytest.raises(TransitError) as ei:
        provider(http).get_arrivals([STOP], NOW, 90)
    assert ei.value.user_message == message


def test_unexpected_body():
    with pytest.raises(TransitError) as ei:
        arrivals_of(["not", "an", "object"])
    assert ei.value.user_message == "Transit feed unreadable"


def test_clock_skew_is_corrected_from_current_time(caplog):
    local_now = NOW + timedelta(minutes=10)  # the Pi's clock is 10 minutes fast
    oba = provider(FakeHttp().add_json(URL, fixture_doc()))
    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        [first] = oba.get_arrivals([STOP], local_now, 90)
        [second] = oba.get_arrivals([STOP], local_now, 90)
    assert [int((a.time - local_now).total_seconds()) for a in first.arrivals] == [899, 1979, 2489, 2849, 3329]
    assert first.arrivals[0].time == from_epoch(1794572100) + (local_now - NOW)
    assert second.arrivals == first.arrivals
    assert caplog.text.count("local clock is +600 s off the OneBusAway server clock") == 1


def test_small_skew_is_ignored():
    arrivals = arrivals_of(fixture_doc(), now=NOW + timedelta(seconds=100))
    assert int(arrivals[0].time.timestamp()) == 1794572100
