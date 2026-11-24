"""Tests for meshwx.transit.service.TransitService, the provider factory and the shared transit rules."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import pytest

from meshwx.config import TransitConfig
from meshwx.errors import ConfigError, TransitError
from meshwx.httpclient import HttpError
from meshwx.models import Arrival, StopArrivals, TransitStop
from meshwx.timeutil import from_epoch
from meshwx.transit import (
    ClockSkew,
    RateLimited,
    arrivals_per_stop,
    create_transit_provider,
    http_failure,
    parse_direction,
    parse_time,
    route_display_name,
    select_arrivals,
    status_failure,
    visit_time,
)
from meshwx.transit.base import TransitProvider
from meshwx.transit.gtfs_rt import GtfsRtProvider
from meshwx.transit.mbta import MbtaProvider
from meshwx.transit.onebusaway import OneBusAwayProvider
from meshwx.transit.service import TransitService
from meshwx.transit.siri import SiriProvider
from meshwx.transit.transitland import TransitlandProvider

from conftest import FakeHttp, make_config

NOW = datetime(2026, 11, 13, 12, 45, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def arrival(minutes: float, route: str = "39", *, direction: Optional[int] = 1, name: Optional[str] = None,
            stop_id: str = "1317") -> Arrival:
    return Arrival(stop_id=stop_id, route_id=route, route_name=name or route, headsign="Back Bay",
                   time=NOW + timedelta(minutes=minutes), direction_id=direction)


class StubProvider(TransitProvider):
    """Returns canned arrivals per stop id and records its calls; set ``error`` to make it raise."""

    name = "stub"

    def __init__(self, arrivals: Optional[Dict[str, List[Arrival]]] = None) -> None:
        self.arrivals = arrivals or {}
        self.calls: List[List[str]] = []
        self.error: Optional[BaseException] = None
        self.stop_errors: Dict[str, str] = {}

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime, horizon_minutes: int) -> List[StopArrivals]:
        self.calls.append([s.id for s in stops])
        if self.error is not None:
            raise self.error
        return [StopArrivals(stop=s, arrivals=list(self.arrivals.get(s.id, [])), error=self.stop_errors.get(s.id))
                for s in stops]


class WarmProvider(StubProvider):
    def __init__(self, error: Optional[BaseException] = None) -> None:
        super().__init__()
        self.warmed = 0
        self.warm_error = error

    def warm_up(self) -> None:
        self.warmed += 1
        if self.warm_error is not None:
            raise self.warm_error


STOPS = [{"id": "place-pktrm", "label": "Park St"}, {"id": "1317", "label": "Fenwood Rd"},
         {"id": "70075", "label": "Park SB", "routes": ["Red"]}]


def transit_cfg(**overrides: object) -> TransitConfig:
    section = {"enabled": True, "provider": "mbta", "stops": STOPS}
    section.update(overrides)
    return make_config({"transit": section}).transit


# ============================================================================ TransitService


def test_answers_are_cached_per_stop_selection():
    clock = Clock()
    provider = StubProvider({"1317": [arrival(5)]})
    svc = TransitService(transit_cfg(), provider, clock=clock)
    everything = svc.get_arrivals(now=NOW)
    assert [r.stop.label for r in everything] == ["Park St", "Fenwood Rd", "Park SB"]
    assert svc.get_arrivals(now=NOW) == everything
    assert provider.calls == [["place-pktrm", "1317", "70075"]]
    [one] = svc.get_arrivals([svc.stops[1]], now=NOW)
    assert [a.time for a in one.arrivals] == [NOW + timedelta(minutes=5)]
    assert provider.calls[-1] == ["1317"]
    clock.t += 59
    svc.get_arrivals(now=NOW)
    assert len(provider.calls) == 2
    clock.t += 2  # > cache_seconds (60)
    svc.get_arrivals(now=NOW)
    assert len(provider.calls) == 3


def test_cached_answer_is_refiltered_against_now():
    clock = Clock()
    provider = StubProvider({"1317": [arrival(0.25), arrival(2), arrival(5)]})
    svc = TransitService(transit_cfg(), provider, clock=clock)
    [first] = svc.get_arrivals([svc.stops[1]], now=NOW)
    clock.t += 50
    [later] = svc.get_arrivals([svc.stops[1]], now=NOW + timedelta(seconds=90))
    assert len(provider.calls) == 1
    assert len(first.arrivals) == 3
    assert [a.time for a in later.arrivals] == [NOW + timedelta(minutes=2), NOW + timedelta(minutes=5)]


def test_min_minutes_hides_arrivals_that_are_too_soon():
    provider = StubProvider({"1317": [arrival(1), arrival(2.9), arrival(3), arrival(10)]})
    svc = TransitService(transit_cfg(min_minutes=3), provider)
    [result] = svc.get_arrivals([svc.stops[1]], now=NOW)
    assert [a.time - NOW for a in result.arrivals] == [timedelta(minutes=3), timedelta(minutes=10)]


def test_past_arrivals_dropped_and_list_capped():
    provider = StubProvider({"1317": [arrival(-2), arrival(-0.5)] + [arrival(m) for m in range(1, 26)]})
    svc = TransitService(transit_cfg(), provider)
    [result] = svc.get_arrivals([svc.stops[1]], now=NOW)
    assert len(result.arrivals) == 20
    assert result.arrivals[0].time == NOW - timedelta(seconds=30)  # within the 60 s grace: "now"


@pytest.mark.parametrize("cache_seconds, asked", [(10, 91), (60, 91), (90, 92), (3600, 150)])
def test_provider_is_asked_for_the_horizon_plus_the_cache_lifetime(cache_seconds, asked):
    horizons: List[int] = []

    class Recording(StubProvider):
        def get_arrivals(self, stops: Sequence[TransitStop], now: datetime, horizon_minutes: int) -> List[StopArrivals]:
            horizons.append(horizon_minutes)
            return super().get_arrivals(stops, now, horizon_minutes)

    clock = Clock()
    provider = Recording({"1317": [arrival(5), arrival(90), arrival(91.5)]})
    svc = TransitService(transit_cfg(cache_seconds=cache_seconds), provider, clock=clock)  # horizon: 90 min
    [result] = svc.get_arrivals([svc.stops[1]], now=NOW)
    assert horizons == [asked]
    # the extra minutes keep a cached answer complete; what is shown still ends at the horizon
    assert [a.time - NOW for a in result.arrivals] == [timedelta(minutes=5), timedelta(minutes=90)]
    if cache_seconds >= 90:
        clock.t += 60  # still cached; 90 s later the 91.5-minute arrival is within the horizon
        [later] = svc.get_arrivals([svc.stops[1]], now=NOW + timedelta(seconds=90))
        assert len(horizons) == 1 and later.arrivals[-1].time == NOW + timedelta(minutes=91.5)


def test_concurrent_requests_share_one_fetch():
    started, release = threading.Event(), threading.Event()

    class SlowProvider(StubProvider):
        def get_arrivals(self, stops: Sequence[TransitStop], now: datetime, horizon_minutes: int) -> List[StopArrivals]:
            started.set()
            release.wait(5)
            return super().get_arrivals(stops, now, horizon_minutes)

    provider = SlowProvider({"1317": [arrival(5)]})
    svc = TransitService(transit_cfg(), provider)
    results: List[List[StopArrivals]] = []
    threads = [threading.Thread(target=lambda: results.append(svc.get_arrivals(now=NOW))) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(results) == 4 and all(r == results[0] for r in results)
    assert len(provider.calls) == 1


def test_empty_selection_asks_nobody():
    provider = StubProvider()
    assert TransitService(transit_cfg(), provider).get_arrivals([], now=NOW) == []
    assert TransitService(transit_cfg(stops=[], enabled=False), provider).get_arrivals(now=NOW) == []
    assert provider.calls == []


def test_per_stop_errors_are_returned_and_cached():
    provider = StubProvider({"1317": [arrival(4)]})
    provider.stop_errors["place-pktrm"] = "Stop not found"
    svc = TransitService(transit_cfg(), provider)
    first = svc.get_arrivals(now=NOW)
    assert [r.error for r in first] == ["Stop not found", None, None]
    assert svc.get_arrivals(now=NOW) == first and len(provider.calls) == 1


@pytest.mark.parametrize("error, message", [
    (TransitError("feed old", user_message="Transit feed is stale"), "Transit feed is stale"),
    (HttpError("boom", "https://x.test", 503), "Network error, try later"),
    (KeyError("arrivalTime"), "Transit unavailable, try later"),
])
def test_failures_become_transit_errors_and_are_not_cached(error, message, caplog):
    provider = StubProvider()
    provider.error = error
    svc = TransitService(transit_cfg(), provider)
    with caplog.at_level(logging.ERROR, logger="meshwx.transit.service"):
        for _ in range(2):
            with pytest.raises(TransitError) as ei:
                svc.get_arrivals(now=NOW)
            assert ei.value.user_message == message
    assert len(provider.calls) == 2
    assert ("crashed" in caplog.text) == isinstance(error, KeyError)


def test_same_stop_id_with_different_filters_is_not_confused():
    stops = [{"id": "place-pktrm", "label": "Red", "routes": ["Red"]},
             {"id": "place-pktrm", "label": "Green", "routes": ["Green-B"]}]
    provider = StubProvider({"place-pktrm": [arrival(3, "Red")]})
    svc = TransitService(transit_cfg(stops=stops), provider)
    svc.get_arrivals([svc.find_stop("red")], now=NOW)
    svc.get_arrivals([svc.find_stop("green")], now=NOW)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("query, label", [
    ("2", "Fenwood Rd"),
    ("3", "Park SB"),
    ("park", "Park St"),  # first label with that prefix
    ("PARK SB", "Park SB"),
    ("  fenwood   rd ", "Fenwood Rd"),
    ("70075", "Park SB"),  # exact id
    ("PLACE-PKTRM", "Park St"),  # id, case-insensitive
    ("1317", "Fenwood Rd"),  # a number beyond the stop count: tried as label, then id
    ("4", None),
    ("0", None),
    ("", None),
    ("xyz", None),
    ("²", None),  # superscript two: a digit, but not a number
])
def test_find_stop(query, label):
    found = TransitService(transit_cfg(), StubProvider()).find_stop(query)
    assert (found.label if found else None) == label


def test_stops_property_is_a_copy():
    svc = TransitService(transit_cfg(), StubProvider())
    svc.stops.clear()
    assert [s.id for s in svc.stops] == ["place-pktrm", "1317", "70075"]


def test_warm_up(caplog):
    warm = WarmProvider()
    TransitService(transit_cfg(), warm).warm_up()
    assert warm.warmed == 1
    TransitService(transit_cfg(), StubProvider()).warm_up()  # no warm_up method: nothing to do
    with caplog.at_level(logging.WARNING, logger="meshwx.transit.service"):
        TransitService(transit_cfg(), WarmProvider(TransitError("offline"))).warm_up()
        TransitService(transit_cfg(), WarmProvider(RuntimeError("bug"))).warm_up()
    assert "transit warm-up failed: offline" in caplog.text and "transit warm-up crashed" in caplog.text


# ============================================================================ factory + end to end


@pytest.mark.parametrize("name, cls", [
    ("gtfs_rt", GtfsRtProvider),
    ("mbta", MbtaProvider),
    ("onebusaway", OneBusAwayProvider),
    ("transitland", TransitlandProvider),
    ("siri", SiriProvider),
])
def test_factory(tmp_path, name, cls):
    cfg = TransitConfig(provider=name, stops=[TransitStop("1", "One")])
    provider = create_transit_provider(cfg, FakeHttp(), tmp_path)
    assert isinstance(provider, cls) and provider.name == name
    assert list(tmp_path.iterdir()) == []  # constructing a provider does no I/O


def test_factory_unknown_provider(tmp_path):
    with pytest.raises(ConfigError):
        create_transit_provider(TransitConfig(provider="nextbus"), FakeHttp(), tmp_path)


def test_service_with_gtfs_rt_end_to_end(tmp_path):
    pytest.importorskip("google.transit.gtfs_realtime_pb2")
    feed, static = "https://rt.test/TripUpdates.pb", "https://rt.test/gtfs.zip"
    cfg = make_config({"state_dir": str(tmp_path), "transit": {
        "enabled": True, "provider": "gtfs_rt", "stops": [{"id": "place-pktrm", "label": "Park St"}],
        "gtfs_rt": {"trip_updates_url": feed, "static_gtfs_url": static}}}).transit
    http = FakeHttp().add_bytes(feed, "gtfs_rt_tripupdates_sample.pb").add_bytes(static, "gtfs_static_sample.zip")
    svc = TransitService(cfg, create_transit_provider(cfg, http, tmp_path))
    svc.warm_up()  # downloads + indexes the static feed, synchronously
    now = from_epoch(1794573523)
    [park] = svc.get_arrivals(now=now)
    assert [a.headsign for a in park.arrivals][:3] == ["Ashmont", "Alewife", "Ashmont"]
    assert len(park.arrivals) == 10
    assert len(http.calls_to(feed)) == 1 and len(http.calls_to(static)) == 1


# ============================================================================ shared rules (transit/__init__.py)


@pytest.mark.parametrize("short, long_name, route_id, expected", [
    ("39", "Forest Hills - Back Bay", "39", "39"),
    ("", "Red Line", "Red", "Red"),
    ("", "Framingham/Worcester Line", "CR-Worcester", "Framingham/Worcester"),
    ("  ", "Mattapan Trolley", "Mattapan", "Mattapan Trolley"),
    ("", "", "CR-Worcester", "CR-Worcester"),
    (None, None, None, "?"),
])
def test_route_display_name(short, long_name, route_id, expected):
    assert route_display_name(short, long_name, route_id) == expected


@pytest.mark.parametrize("value, expected", [
    (0, 0), (1, 1), ("0", 0), (" 1 ", 1), (2, None), ("IB", None), (None, None), (True, None), ("", None),
])
def test_parse_direction(value, expected):
    assert parse_direction(value) == expected


def test_parse_time():
    assert parse_time("2026-11-13T07:44:18-05:00") == datetime(2026, 11, 13, 12, 44, 18, tzinfo=timezone.utc)
    assert parse_time("2026-11-13T07:44:18-05:00").tzinfo == timezone.utc
    assert parse_time("2026-11-13T12:44:18.5Z").microsecond == 500000
    for bad in (None, "", "07:44:18", 1794573858):
        assert parse_time(bad) is None


@pytest.mark.parametrize("arrival, departure, expected", [
    (5, 6, 5),  # arrival preferred
    (None, 6, 6),  # first stop of a trip: departure only
    (5, None, 5),  # arrival only
    (-0.5, 1, -0.5),  # boarding, within the 60 s grace: unchanged
    (-2.5, 1, 0),  # long dwell: standing at the stop, so "now" rather than "past"
    (-2.5, 1.5, 0),  # leaves within 90 s: still "now"
    (-13.6, 6.5, 6.5),  # layover at the first stop (pulled in 13.6 min ago): its departure, not "now"
    (-0.5, 5, 5),  # just pulled in for a 5-minute hold: its departure
    (2, 12, 2),  # not there yet: the arrival, even with a hold ahead
    (-2.5, None, -2.5),  # no departure known: left to the time window
    (None, None, None),
])
def test_visit_time(arrival, departure, expected):
    def at(minutes):
        return None if minutes is None else NOW + timedelta(minutes=minutes)

    assert visit_time(at(arrival), at(departure), NOW) == at(expected)


def test_select_arrivals_window_filters_and_order():
    arrivals = [
        arrival(10, "39"), arrival(-1.5, "39"), arrival(-0.9, "39"), arrival(91, "39"), arrival(90, "39"),
        arrival(5, "Red", direction=0), arrival(5, "66"), arrival(7, "1_102768", name="72", direction=None),
    ]
    every = select_arrivals(TransitStop("s", "S"), arrivals, NOW, 90)
    assert [(a.route_name, (a.time - NOW).total_seconds() / 60) for a in every] == [
        ("39", -0.9), ("66", 5), ("Red", 5), ("72", 7), ("39", 10), ("39", 90)]
    by_route = select_arrivals(TransitStop("s", "S", ("red", "72")), arrivals, NOW, 90)
    assert [a.route_name for a in by_route] == ["Red", "72"]  # route id or displayed short name
    by_direction = select_arrivals(TransitStop("s", "S", (), 1), arrivals, NOW, 90)
    assert "Red" not in [a.route_name for a in by_direction]
    assert "72" in [a.route_name for a in by_direction]  # unknown direction: kept


def test_arrivals_per_stop_rate_limit_and_partial_failure():
    stops = [TransitStop(str(i), "S%d" % i) for i in range(3)]
    fetched: List[str] = []

    def rate_limited(stop: TransitStop) -> List[Arrival]:
        fetched.append(stop.id)
        if stop.id == "1":
            raise RateLimited("429", user_message="Slow down")
        return [arrival(3)]

    results = arrivals_per_stop(stops, rate_limited, NOW, 90)
    assert fetched == ["0", "1"]  # stop 2 is not tried after the rate limit
    assert [(len(r.arrivals), r.error) for r in results] == [(1, None), (0, "Slow down"), (0, "Slow down")]

    def failing(stop: TransitStop) -> List[Arrival]:
        raise TransitError("down", user_message="Stop not found")

    with pytest.raises(TransitError) as ei:
        arrivals_per_stop(stops, failing, NOW, 90)
    assert ei.value.user_message == "Stop not found"
    assert arrivals_per_stop([], failing, NOW, 90) == []


def test_arrivals_per_stop_turns_parser_errors_into_stop_errors(caplog):
    stops = [TransitStop(str(i), "S%d" % i) for i in range(2)]

    def malformed_first(stop: TransitStop) -> List[Arrival]:
        if stop.id == "0":
            return {"data": "junk"}["data"].get("entry")  # type: ignore[attr-defined] - AttributeError
        return [arrival(3)]

    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        bad, good = arrivals_per_stop(stops, malformed_first, NOW, 90)
    assert (bad.error, len(good.arrivals)) == ("Transit feed unreadable", 1)
    assert "unexpected response" in caplog.text
    with pytest.raises(TransitError) as ei:
        arrivals_per_stop(stops[:1], malformed_first, NOW, 90)
    assert ei.value.user_message == "Transit feed unreadable"


@pytest.mark.parametrize("status, per_stop, message, cls", [
    (429, True, "Busy", RateLimited),
    (401, True, "X API key rejected", TransitError),
    (403, False, "X API key rejected", TransitError),
    (404, True, "Stop not found", TransitError),
    (404, False, "Transit unavailable, try later", TransitError),
    (200, True, "Transit feed unreadable", TransitError),
    (500, True, "Transit unavailable, try later", TransitError),
    (None, True, "Transit unavailable, try later", TransitError),
])
def test_status_failure(status, per_stop, message, cls):
    exc = status_failure(status, "X", "Busy", per_stop=per_stop)
    assert type(exc) is cls and exc.user_message == message
    assert "X request failed" in str(exc) and "http" not in str(exc).replace("HTTP", "")


def test_http_failure_never_logs_the_url():
    class ConnectTimeout(Exception):
        pass

    try:
        try:
            raise ConnectTimeout("timed out")
        except ConnectTimeout as cause:
            raise HttpError("GET https://x.test/feed?key=SECRET failed: timed out", "https://x.test/feed?key=SECRET") \
                from cause
    except HttpError as exc:
        error = http_failure(exc, "X", "Busy")
    assert str(error) == "X request failed: network error (ConnectTimeout)"
    assert error.user_message == "Transit unavailable, try later"
    assert "SECRET" not in str(http_failure(HttpError("GET https://x.test/?key=SECRET", "u", 500), "X", "Busy"))


def test_clock_skew(caplog):
    mono = Clock()
    skew = ClockSkew("Test", monotonic=mono)
    assert skew.offset(NOW, None) == timedelta(0)
    assert skew.offset(NOW, NOW - timedelta(seconds=120)) == timedelta(0)  # tolerance is inclusive
    assert skew.offset(NOW, NOW + timedelta(seconds=100)) == timedelta(0)
    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        assert skew.offset(NOW, NOW - timedelta(seconds=121)) == timedelta(seconds=121)
        assert skew.offset(NOW, NOW + timedelta(hours=2)) == timedelta(hours=-2)
        mono.t += 3599
        skew.offset(NOW, NOW + timedelta(hours=2))
        assert caplog.text.count("local clock is") == 1  # at most one warning per hour
        mono.t += 1
        skew.offset(NOW, NOW + timedelta(hours=2))
        assert caplog.text.count("local clock is") == 2
    assert "-7200 s off the Test server clock" in caplog.text
