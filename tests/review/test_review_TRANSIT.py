"""Adversarial review, area TRANSIT: meshwx/transit/*.py and the transit lines of formatting.py.

Regression tests from the adversarial code review: each test first reproduced one
verified defect (finding id in the comments); all of them are fixed now.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.config import DisplayConfig, config_from_dict, load_yaml_text
from meshwx.formatting import Formatter
from meshwx.models import StopArrivals, TransitStop
from meshwx.timeutil import from_epoch
from meshwx.transit.mbta import MbtaProvider
from meshwx.transit.onebusaway import OneBusAwayProvider

from conftest import FakeHttp, load_fixture, make_config

UTC = timezone.utc
FEED_URL = "https://rt.test/realtime/TripUpdates.pb"
STATIC_URL = "https://rt.test/gtfs.zip"
#: 2026-11-14T08:46:56Z, the moment of the live King County Metro capture.
LIVE_TS = 1794646016
LIVE_NOW = from_epoch(LIVE_TS)
#: header.timestamp of tests/fixtures/gtfs_rt_tripupdates_sample.pb (MBTA).
SAMPLE_TS = 1794573523
SAMPLE_NOW = from_epoch(SAMPLE_TS)


def fmt() -> Formatter:
    return Formatter(DisplayConfig(), 200, default_tz=UTC)


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ----------------------------------------------------------------------------- GTFS-RT helpers


@pytest.fixture
def pb() -> Any:
    return pytest.importorskip("google.transit.gtfs_realtime_pb2")


def new_feed(pb: Any, ts: int) -> Any:
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = ts
    return msg


def add_trip(msg: Any, trip_id: str, route: str, direction: Optional[int], stus: List[Dict[str, Any]]) -> None:
    entity = msg.entity.add()
    entity.id = trip_id
    trip = entity.trip_update.trip
    trip.trip_id = trip_id
    if route:
        trip.route_id = route
    if direction is not None:
        trip.direction_id = direction
    for seq, spec in enumerate(stus, 1):
        stu = entity.trip_update.stop_time_update.add()
        stu.stop_sequence = seq
        stu.stop_id = spec["stop"]
        if spec.get("arr") is not None:
            stu.arrival.time = spec["arr"]
        if spec.get("dep") is not None:
            stu.departure.time = spec["dep"]


def gtfs_rt_provider(tmp_path: Path, http: FakeHttp, stops: Sequence[Dict[str, Any]], *,
                     static_url: str = "", spawn: Optional[Callable[[Callable[[], None]], None]] = None) -> Any:
    from meshwx.transit.gtfs_rt import GtfsRtProvider
    from meshwx.transit.gtfs_static import GtfsStaticIndex

    cfg = make_config({"state_dir": str(tmp_path), "transit": {
        "enabled": True, "provider": "gtfs_rt", "stops": list(stops),
        "gtfs_rt": {"trip_updates_url": FEED_URL, "static_gtfs_url": static_url}}}).transit
    static = GtfsStaticIndex(static_url, http, tmp_path, 168, route_filters=[r for s in cfg.stops for r in s.routes],
                             spawn=spawn or (lambda job: job()))
    return GtfsRtProvider(cfg, http, tmp_path, static=static, monotonic=Mono())


def at(ts: int, offset: int) -> int:
    return ts + offset


# ============================================================================ TRANSIT-1


def test_layover_at_first_stop_shows_departure_not_now_gtfs_rt(tmp_path, pb):
    # Live feeds 2026-11-23 12:47 UTC: updates that arrived > 60 s ago but depart >= 2 min later (all of
    # them the trip's first listed stop, i.e. a terminal layover): KCM 3 (leaving in 193-613 s), MARTA 1
    # (780 s), Denver RTD 4 (303-1443 s).  visit_time() shows every one of them as "now".
    # Live KCM TripUpdates, 2026-11-14T08:46:56Z: trip 846336551 (route 10) starts at stop 11370.
    # Its first stop-time update says arrival 816 s ago (the bus reached the terminal) and departure
    # in 388 s.  OneBusAway (same data) lists it with arrivalEnabled=false, predictedDepartureTime +384 s.
    msg = new_feed(pb, LIVE_TS - 10)
    add_trip(msg, "846336551", "100002", 1, [
        {"stop": "11370", "arr": at(LIVE_TS, -816), "dep": at(LIVE_TS, 388)},
        {"stop": "11375", "arr": at(LIVE_TS, 450), "dep": at(LIVE_TS, 450)},
        {"stop": "1121", "arr": at(LIVE_TS, 1500), "dep": at(LIVE_TS, 1500)},
    ])
    http = FakeHttp().add_bytes(FEED_URL, msg.SerializeToString())
    provider = gtfs_rt_provider(tmp_path, http, [{"id": "11370", "label": "15th & Galer"}])
    [stop] = provider.get_arrivals(provider._stops, LIVE_NOW, 90)
    [bus] = stop.arrivals
    # the bus leaves in 388 s; telling riders "now" makes them run for a bus that waits 6.5 minutes
    assert abs((bus.time - (LIVE_NOW + timedelta(seconds=388))).total_seconds()) <= 60
    assert fmt().transit([stop], LIVE_NOW) == ["15th & Galer: 100002 6m"]


def test_layover_at_first_stop_shows_departure_not_now_onebusaway():
    # Live OneBusAway Puget Sound (key TEST), stop 1_11370 at 2026-11-14T08:47Z:
    #   route 10 trip 1_846336551 stopSequence 0 arrivalEnabled false departureEnabled true
    #   predictedArrivalTime -820 s, predictedDepartureTime +384 s, scheduled +384 s
    base = "https://api.pugetsound.onebusaway.test"
    doc = load_fixture("oba_arrivals_sample.json")
    server_ms = doc["currentTime"]
    now = from_epoch(server_ms / 1000.0)
    item = doc["data"]["entry"]["arrivalsAndDepartures"][0]
    item.update(stopSequence=0, arrivalEnabled=False, departureEnabled=True, predicted=True,
                predictedArrivalTime=server_ms - 820_000, predictedDepartureTime=server_ms + 384_000,
                scheduledArrivalTime=server_ms + 384_000, scheduledDepartureTime=server_ms + 384_000)
    cfg = make_config({"transit": {"enabled": True, "provider": "onebusaway", "stops": ["1_10001"],
                                   "onebusaway": {"base_url": base, "api_key": "TEST"}}})
    http = FakeHttp().add_json(base + "/api/where/arrivals-and-departures-for-stop/1_10001.json", doc)
    [stop] = OneBusAwayProvider(cfg.transit, http, Path()).get_arrivals([TransitStop("1_10001", "Stop")], now, 90)
    first = min(stop.arrivals, key=lambda a: a.time)
    assert first.route_name == "5"
    assert abs((first.time - (now + timedelta(seconds=384))).total_seconds()) <= 60  # today: exactly `now`


# ============================================================================ TRANSIT-2


def _terminal_static_zip(route: str, stop_id: str) -> bytes:
    """A static GTFS zip that agrees with the feed below: "ends-here" ends at ``stop_id``, "starts-here" starts there."""
    import io
    import zipfile

    route_id = route or "R"
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n%s,%s,,3\n" % (route_id, route_id),
        "stops.txt": "stop_id,stop_name\nprev-2,Prev 2\nprev-1,Prev 1\n%s,Terminal\nnext-1,Next 1\n" % stop_id,
        "trips.txt": ("route_id,service_id,trip_id,trip_headsign,direction_id\n"
                      "%s,S,ends-here,Terminal,0\n%s,S,starts-here,Next 1,1\n" % (route_id, route_id)),
        "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
                           "ends-here,08:00:00,08:00:00,prev-2,1,0,0\nends-here,08:03:00,08:03:00,prev-1,2,0,0\n"
                           "ends-here,08:06:00,08:06:00,%s,3,1,0\n"
                           "starts-here,08:10:00,08:10:00,%s,1,0,1\nstarts-here,08:13:00,08:13:00,next-1,2,0,0\n"
                           % (stop_id, stop_id)),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


@pytest.mark.parametrize("agency, route, stop_id, last_arr, last_dep", [
    # Live counts, 2026-11-23 12:47 UTC: the last stop-time update of a running trip carries a departure
    # time in BART 45/45, NYC subway ACE 136/136, KCM 257/261 and MARTA 273/287 trips, so the rule
    # "the trip's last update without a departure" (gtfs_rt.extract_events) never fires there.
    # KCM trip 846336541 (route 10) ends at 11370: last update arrival == departure (+2556 s).
    # OneBusAway lists the same trip at 1_11370 with departureEnabled=false.
    ("kcm", "100002", "11370", 2556, 2556),
    # NYC subway A train 012200_A..N ends at Inwood-207 St (A02N): arrival == departure.
    ("nyc", "A", "A02N", 1601, 1601),
    # BART trip 1965481 ends at Milpitas (S40-1): departure 48 s after arrival.
    ("bart", "", "S40-1", 1263, 1311),
])
def test_trip_terminating_at_the_stop_is_not_shown(tmp_path, pb, agency, route, stop_id, last_arr, last_dep):
    # The static zip (stop_times.txt: pickup_type 1 at the last stop) is there so that a fix may use either
    # the schedule or the feed alone; the feed lists every remaining stop of both trips, as these agencies do.
    msg = new_feed(pb, LIVE_TS - 10)
    add_trip(msg, "ends-here", route, 0, [
        {"stop": "prev-2", "arr": at(LIVE_TS, last_arr - 400), "dep": at(LIVE_TS, last_arr - 380)},
        {"stop": "prev-1", "arr": at(LIVE_TS, last_arr - 200), "dep": at(LIVE_TS, last_arr - 180)},
        {"stop": stop_id, "arr": at(LIVE_TS, last_arr), "dep": at(LIVE_TS, last_dep)},
    ])
    add_trip(msg, "starts-here", route, 1, [
        {"stop": stop_id, "arr": at(LIVE_TS, 300), "dep": at(LIVE_TS, 300)},
        {"stop": "next-1", "arr": at(LIVE_TS, 500), "dep": at(LIVE_TS, 500)},
    ])
    http = (FakeHttp().add_bytes(FEED_URL, msg.SerializeToString())
            .add_bytes(STATIC_URL, _terminal_static_zip(route, stop_id)))
    provider = gtfs_rt_provider(tmp_path, http, [{"id": stop_id, "label": "Terminal"}], static_url=STATIC_URL)
    provider.warm_up()
    [stop] = provider.get_arrivals(provider._stops, LIVE_NOW, 90)
    shown = [int((a.time - LIVE_NOW).total_seconds()) for a in stop.arrivals]
    assert shown == [300]  # today also [300, last_arr]: a vehicle that ends its trip here, nobody can board it


# ============================================================================ TRANSIT-3


def test_feed_without_route_ids_uses_trips_txt(tmp_path, pb):
    # BART's live TripUpdates (api.bart.gov/gtfsrt/tripupdate.aspx) set only trip_id: 16 of 16 trips had no
    # route_id, so every BART arrival rendered as "?>Milpitas now,13,36m".  Same MBTA sample, route_id removed:
    feed = pb.FeedMessage()
    feed.ParseFromString(load_fixture("gtfs_rt_tripupdates_sample.pb"))
    for entity in feed.entity:
        entity.trip_update.trip.ClearField("route_id")
        entity.trip_update.trip.ClearField("direction_id")
    http = (FakeHttp().add_bytes(FEED_URL, feed.SerializeToString())
            .add_bytes(STATIC_URL, "gtfs_static_sample.zip"))
    provider = gtfs_rt_provider(tmp_path, http, [{"id": "1317", "label": "Fenwood"},
                                                 {"id": "1317", "label": "Fenwood out", "direction_id": 0}],
                                static_url=STATIC_URL)
    provider.warm_up()
    fenwood, outbound = provider.get_arrivals(provider._stops, SAMPLE_NOW, 90)
    added = 1794574095  # ADDED trip 78612398-OL1 is not in trips.txt: its route stays unknown
    names = [a.route_name for a in fenwood.arrivals if int(a.time.timestamp()) != added]
    assert names == ["39", "66", "39", "39", "66", "39", "66"]  # today: all "?"
    # trips.txt says every trip at 1317 runs in direction 1, so the direction-0 stop shows only the unknown trip
    assert [int(a.time.timestamp()) for a in outbound.arrivals] == [added]


# ============================================================================ TRANSIT-4


def _mbta_prediction(pid: str, route: str, stop: str, trip: str, arr: int, dep: int) -> Dict[str, Any]:
    iso = lambda e: from_epoch(e).astimezone(timezone(timedelta(hours=-5))).isoformat()  # noqa: E731
    return {"id": pid, "type": "prediction",
            "attributes": {"arrival_time": iso(arr), "departure_time": iso(dep), "schedule_relationship": None,
                           "status": None, "trip_headsign": None, "direction_id": 0},
            "relationships": {"route": {"data": {"id": route, "type": "route"}},
                              "trip": {"data": {"id": trip, "type": "trip"}},
                              "stop": {"data": {"id": stop, "type": "stop"}}}}


def test_mbta_route_filter_mixing_ids_and_short_names():
    now = 1794573834
    rows = [_mbta_prediction("p1", "Red", "70075", "t1", now + 120, now + 150),
            _mbta_prediction("p2", "Green-B", "70196", "t2", now + 200, now + 230)]
    included = [{"type": "route", "id": "Red", "attributes": {"short_name": "", "long_name": "Red Line"}},
                {"type": "route", "id": "Green-B", "attributes": {"short_name": "B", "long_name": "Green Line B"}},
                {"type": "trip", "id": "t1", "attributes": {"headsign": "Ashmont"}},
                {"type": "trip", "id": "t2", "attributes": {"headsign": "Boston College"}}]

    def api(url: str) -> Dict[str, Any]:
        # what api-v3.mbta.com does (checked live: filter[route]=66,bogus returns the route 66 rows, 200 OK):
        # unknown ids in a filter[route] list are ignored, known ids filter exactly
        wanted = parse_qs(urlsplit(url).query).get("filter[route]")
        keep = rows if not wanted else [r for r in rows if r["relationships"]["route"]["data"]["id"]
                                        in wanted[0].split(",")]
        return {"data": copy.deepcopy(keep), "included": included, "jsonapi": {"version": "1.0"}}

    cfg = make_config({"transit": {"enabled": True, "provider": "mbta", "stops": ["place-pktrm"]}})
    http = FakeHttp().add_json("https://api-v3.mbta.com/predictions", api)
    stop = TransitStop("place-pktrm", "Park St", ("Red", "B"))  # README: "route id or name"
    [result] = MbtaProvider(cfg.transit, http, Path()).get_arrivals([stop], from_epoch(now), 90)
    assert [a.route_name for a in result.arrivals] == ["Red", "B"]  # today: ["Red"] - the B train is never shown


# ============================================================================ TRANSIT-5


def test_unquoted_onebusaway_ids_survive_yaml():
    text = """
location: {zip_code: "98195"}
transit:
  enabled: true
  provider: onebusaway
  onebusaway: {base_url: "https://api.pugetsound.onebusaway.org", api_key: "TEST"}
  stops:
    - id: 1_75403
      label: Stevens Way
      routes: [1_102768]
"""
    stop = config_from_dict(load_yaml_text(text)).transit.stops[0]
    # YAML 1.1 reads 1_75403 as the integer 175403 (config._Loader only protects leading zeros): the
    # stop becomes "175403", which OneBusAway answers with code 404 ("Stop not found", rendered as
    # "unavailable"), and the route filter "1102768" never matches any route, so a stop configured
    # with a quoted id but an unquoted route shows "no arrivals soon" forever without any warning.
    assert (stop.id, stop.routes) == ("1_75403", ("1_102768",))


# ============================================================================ TRANSIT-6


def test_unknown_gtfs_rt_stop_is_reported(tmp_path):
    pytest.importorskip("google.transit.gtfs_realtime_pb2")
    http = (FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
            .add_bytes(STATIC_URL, "gtfs_static_sample.zip"))
    provider = gtfs_rt_provider(tmp_path, http, [{"id": "1317", "label": "Fenwood"},
                                                 {"id": "1371", "label": "Typo"},  # not in stops.txt
                                                 {"id": "70075,70076", "label": "List"}],  # works with mbta
                                static_url=STATIC_URL)
    provider.warm_up()
    fenwood, typo, listed = provider.get_arrivals(provider._stops, SAMPLE_NOW, 90)
    assert len(fenwood.arrivals) == 8
    rendered = fmt().transit([typo, listed], SAMPLE_NOW)
    # the index knows every stop_id of the feed, yet both stops read like a quiet night
    assert "no arrivals soon" not in rendered[0], rendered


# ============================================================================ TRANSIT-7


def test_per_stop_error_text_reaches_the_user(tmp_path):
    pytest.importorskip("google.transit.gtfs_realtime_pb2")
    jobs: List[Callable[[], None]] = []
    http = (FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
            .add_bytes(STATIC_URL, "gtfs_static_sample.zip"))
    provider = gtfs_rt_provider(tmp_path, http, [{"id": "place-pktrm", "label": "Park St"}],
                                static_url=STATIC_URL, spawn=jobs.append)
    [park] = provider.get_arrivals(provider._stops, SAMPLE_NOW, 90)  # "!bus park" right after start-up
    assert park.error == "Stop data loading, try later"
    out = fmt().transit([park], SAMPLE_NOW)
    assert "loading" in out[0].lower(), out  # today: ["Park St: unavailable"]


# ============================================================================ TRANSIT-8


def test_cached_answer_does_not_hide_arrivals_that_entered_the_horizon():
    from meshwx.transit import select_arrivals
    from meshwx.transit.base import TransitProvider
    from meshwx.transit.service import TransitService
    from meshwx.models import Arrival

    t0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    stop_cfg = {"id": "1317", "label": "Fenwood"}
    cfg = make_config({"transit": {"enabled": True, "provider": "mbta", "stops": [stop_cfg],
                                   "horizon_minutes": 30, "cache_seconds": 1800}}).transit  # both valid

    class HorizonProvider(TransitProvider):
        """Behaves like every real provider: drops arrivals beyond now + horizon."""

        name = "stub"

        def get_arrivals(self, stops, now, horizon_minutes):
            arrivals = [Arrival("1317", "39", "39", "Back Bay", t0 + timedelta(minutes=m)) for m in (10, 40)]
            return [StopArrivals(stop=s, arrivals=select_arrivals(s, arrivals, now, horizon_minutes)) for s in stops]

    clock = Mono()
    svc = TransitService(cfg, HorizonProvider(), clock=clock)
    assert [a.time for a in svc.get_arrivals(now=t0)[0].arrivals] == [t0 + timedelta(minutes=10)]
    clock.t += 25 * 60  # 25 min later: still within cache_seconds
    later = t0 + timedelta(minutes=25)
    [result] = svc.get_arrivals(now=later)
    # the 39 at t0+40 min is 15 min away (inside the 30-min horizon) but the cached answer never had it
    assert [a.time for a in result.arrivals] == [t0 + timedelta(minutes=40)]


# ============================================================================ TRANSIT-9


def _mbta_like_api(rows: List[Dict[str, Any]], included: List[Dict[str, Any]]) -> Callable[[str], Dict[str, Any]]:
    """/predictions as api-v3.mbta.com serves it (checked live 2026-11-23, 07:50 Boston time).

    An ascending ``sort`` puts rows whose sort key is null first (sort=departure_time at place-forhl:
    the first row with a departure was row 115 of 192); ``sort=time`` sorts by arrival, else departure;
    ``page[offset]`` / ``page[limit]`` cut the sorted list; ``filter[route]`` keeps the listed ids.
    """
    def epoch(value: str) -> float:
        return datetime.fromisoformat(value).timestamp()

    def api(url: str) -> Dict[str, Any]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        keep = list(rows)
        if "filter[route]" in q:
            wanted = q["filter[route]"].split(",")
            keep = [r for r in keep if r["relationships"]["route"]["data"]["id"] in wanted]
        sort = q.get("sort", "")
        if sort:
            field = sort.lstrip("-")

            def key(row: Dict[str, Any]) -> Any:
                attrs = row["attributes"]
                value = (attrs["arrival_time"] or attrs["departure_time"]) if field == "time" else attrs.get(field)
                return (value is not None, epoch(value) if value else 0.0)

            keep.sort(key=key, reverse=sort.startswith("-"))
        offset = int(q.get("page[offset]", "0"))
        keep = keep[offset:offset + int(q["page[limit]"])] if "page[limit]" in q else keep[offset:]
        return {"data": copy.deepcopy(keep), "included": included, "jsonapi": {"version": "1.0"}}

    return api


def test_mbta_terminal_with_many_dropoff_rows_still_shows_departures():
    # Live, 2026-11-23 ~12:50 UTC (Monday rush hour), /predictions?filter[stop]=X&sort=departure_time:
    # null-departure (terminating, drop-off only) rows come first - place-forhl 115 of 192,
    # place-rugg 77, place-asmnl 61, place-nubn 47, place-alfcl 36, place-harsq 35 - so the 30-row
    # page MbtaProvider asks for holds no departure at all, while 74 / 134 / 34 / 137 / 16 / 80
    # departures were due within 90 minutes.  Every one of those stations reads "no arrivals soon".
    now = 1794646016
    iso = lambda e: None if e is None else from_epoch(e).astimezone(timezone(timedelta(hours=-5))).isoformat()  # noqa: E731

    def row(pid: str, route: str, trip: str, arr: Optional[int], dep: Optional[int]) -> Dict[str, Any]:
        return {"id": pid, "type": "prediction",
                "attributes": {"arrival_time": iso(arr), "departure_time": iso(dep), "schedule_relationship": None,
                               "status": None, "trip_headsign": None, "direction_id": 1 if dep is None else 0},
                "relationships": {"route": {"data": {"id": route, "type": "route"}},
                                  "trip": {"data": {"id": trip, "type": "trip"}},
                                  "stop": {"data": {"id": "875", "type": "stop"}}}}

    # 40 terminating trips (Orange Line and buses ending at the busway), one every 2 minutes ...
    rows = [row("in%d" % i, ("Orange", "39", "16")[i % 3], "in%d" % i, now + 60 + 120 * i, None) for i in range(40)]
    # ... and the 39 leaving the busway every 8 minutes (first stop of its trip: no arrival time)
    departures = [3, 11, 19, 27, 35, 43]
    rows += [row("out%d" % m, "39", "out%d" % m, None, now + 60 * m) for m in departures]
    included = [{"type": "route", "id": "39", "attributes": {"short_name": "39", "long_name": "",
                                                            "direction_destinations": ["Back Bay", "Forest Hills"]}},
                {"type": "route", "id": "16", "attributes": {"short_name": "16", "long_name": ""}},
                {"type": "route", "id": "Orange", "attributes": {"short_name": "", "long_name": "Orange Line"}}]
    cfg = make_config({"transit": {"enabled": True, "provider": "mbta", "stops": ["place-forhl"]}})
    http = FakeHttp().add_json("https://api-v3.mbta.com/predictions", _mbta_like_api(rows, included))
    [result] = MbtaProvider(cfg.transit, http, Path()).get_arrivals(
        [TransitStop("place-forhl", "Forest Hills")], from_epoch(now), 90)
    shown = [(a.route_name, round((a.time - from_epoch(now)).total_seconds() / 60)) for a in result.arrivals]
    assert shown[:3] == [("39", 3), ("39", 11), ("39", 19)], shown  # today: [] -> "Forest Hills: no arrivals soon"
