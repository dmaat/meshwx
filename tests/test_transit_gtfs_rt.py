"""Tests for meshwx.transit.gtfs_rt (real MBTA TripUpdates capture + static GTFS subset)."""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pytest

pb = pytest.importorskip("google.transit.gtfs_realtime_pb2")

from meshwx.errors import TransitError  # noqa: E402
from meshwx.httpclient import HttpBytes  # noqa: E402
from meshwx.models import Arrival  # noqa: E402
from meshwx.timeutil import from_epoch  # noqa: E402
from meshwx.transit.gtfs_rt import (  # noqa: E402
    BINDINGS_MISSING,
    LOADING_MESSAGE,
    GtfsRtProvider,
    encode_varint,
    extract_events,
    iter_length_delimited,
    prefilter_feed,
    stop_id_needle,
)
from meshwx.transit.gtfs_static import GtfsStaticIndex  # noqa: E402

from conftest import FakeHttp, load_fixture, make_config  # noqa: E402

FEED_URL = "https://rt.test/realtime/TripUpdates.pb"
STATIC_URL = "https://rt.test/MBTA_GTFS.zip"
FEED_TS = 1794573523  # header.timestamp of the capture = the frozen "now" of CAPTURE_NOTES_transit.md
NOW = from_epoch(FEED_TS)
PARK = {"id": "place-pktrm", "label": "Park St"}
FENWOOD = {"id": "1317", "label": "Fenwood"}

# CAPTURE_NOTES "parent station place-pktrm = {70075, 70076} merged": (stop, headsign, epoch, direction)
EXPECTED_PARK = [
    ("70075", "Ashmont", 1794573631, 0),
    ("70076", "Alewife", 1794573827, 1),
    ("70075", "Ashmont", 1794573862, 0),
    ("70076", "Alewife", 1794573997, 1),
    ("70076", "Alewife", 1794574161, 1),
    ("70075", "Braintree", 1794574236, 0),
    ("70076", "Alewife", 1794574302, 1),
    ("70075", "Ashmont", 1794574445, 0),
    ("70075", "Ashmont", 1794574704, 0),  # same second as the next row: feed order kept
    ("70076", "Alewife", 1794574704, 1),
]
# CAPTURE_NOTES "stop 1317": (route, headsign, epoch); row 2 is the ADDED trip (headsign = last stop's name)
EXPECTED_1317 = [
    ("39", "Back Bay", 1794573858),
    ("39", "Back Bay", 1794574095),
    ("66", "Nubian via Allston", 1794574170),
    ("39", "Back Bay", 1794574656),
    ("39", "Back Bay", 1794574855),
    ("66", "Nubian via Allston", 1794574896),
    ("39", "Back Bay", 1794575187),
    ("66", "Nubian via Allston", 1794575322),
]


class Mono:
    """Fake monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def sample_http(feed: Any = "gtfs_rt_tripupdates_sample.pb", **feed_kw: Any) -> FakeHttp:
    return (FakeHttp().add_bytes(FEED_URL, feed, **feed_kw)
            .add_bytes(STATIC_URL, "gtfs_static_sample.zip"))


def make_provider(tmp_path: Path, http: FakeHttp, stops: Sequence[Dict[str, Any]] = (PARK, FENWOOD), *,
                  mono: Optional[Mono] = None, static_url: str = STATIC_URL,
                  spawn: Optional[Callable[[Callable[[], None]], None]] = None, **gtfs_rt: Any) -> GtfsRtProvider:
    """A provider whose static index runs its background work synchronously (unless ``spawn`` is given)."""
    rt = {"trip_updates_url": FEED_URL, "static_gtfs_url": static_url}
    rt.update(gtfs_rt)
    cfg = make_config({"state_dir": str(tmp_path), "transit": {
        "enabled": True, "provider": "gtfs_rt", "stops": list(stops), "gtfs_rt": rt}}).transit
    static = GtfsStaticIndex(static_url, http, tmp_path, cfg.gtfs_rt.static_refresh_hours,
                             route_filters=[r for s in cfg.stops for r in s.routes],
                             spawn=spawn or (lambda job: job()))
    return GtfsRtProvider(cfg, http, tmp_path, static=static, monotonic=mono or Mono())


def warm_provider(tmp_path: Path, stops: Sequence[Dict[str, Any]] = (PARK, FENWOOD), **kw: Any) -> GtfsRtProvider:
    provider = make_provider(tmp_path, sample_http(), stops, **kw)
    provider.warm_up()
    return provider


def rows(arrivals: Sequence[Arrival]) -> List[tuple]:
    return [(a.stop_id, a.headsign, int(a.time.timestamp()), a.direction_id) for a in arrivals]


def labels(arrivals: Sequence[Arrival], now=NOW) -> List[str]:
    """CONTRACTS countdown rule, as in the "label" column of CAPTURE_NOTES_transit.md."""
    secs = [int((a.time - now).total_seconds()) for a in arrivals]
    return ["Now" if s <= 30 else "%dm" % ((s + 30) // 60) for s in secs]


# ============================================================================ real fixtures


def test_parent_station_expands_to_platforms(tmp_path):
    provider = warm_provider(tmp_path)
    [park] = provider.get_arrivals([provider._stops[0]], NOW, 90)
    assert park.error is None
    assert rows(park.arrivals) == EXPECTED_PARK
    assert labels(park.arrivals) == ["2m", "5m", "6m", "8m", "11m", "12m", "13m", "15m", "20m", "20m"]
    assert {(a.route_id, a.route_name, a.realtime) for a in park.arrivals} == {("Red", "Red", True)}
    assert all(a.time.utcoffset() == timedelta(0) for a in park.arrivals)


def test_bus_stop_with_added_trip_and_last_stop_headsign(tmp_path):
    provider = warm_provider(tmp_path)
    [stop] = provider.get_arrivals([provider._stops[1]], NOW, 90)
    assert [(a.route_name, a.headsign, int(a.time.timestamp())) for a in stop.arrivals] == EXPECTED_1317
    assert labels(stop.arrivals) == ["6m", "10m", "11m", "19m", "22m", "23m", "28m", "30m"]
    assert {a.direction_id for a in stop.arrivals} == {1}
    assert provider._static.trip("78612398-OL1") is None  # ADDED trip: headsign came from its last stop
    assert provider._static.trip("78611546").headsign == "Back Bay"


def test_parent_station_while_the_static_index_loads(tmp_path):
    mono = Mono()
    jobs: List[Callable[[], None]] = []
    http = sample_http()
    provider = make_provider(tmp_path, http, mono=mono, spawn=jobs.append)
    park, fenwood = provider.get_arrivals(provider._stops, NOW, 90)
    assert park.error == LOADING_MESSAGE == "Stop data loading, try later" and park.arrivals == []
    # a platform-level stop is answered at once, with fallbacks (route ids, no destinations yet)
    assert fenwood.error is None and len(fenwood.arrivals) == 8
    assert [(a.route_name, a.headsign) for a in fenwood.arrivals[:2]] == [("39", None), ("39", None)]
    assert len(jobs) == 1  # the static download + index runs in the background
    jobs.pop()()
    park, fenwood = provider.get_arrivals(provider._stops, NOW, 90)
    assert park.error == LOADING_MESSAGE  # Park Street's platforms were not in the last download ...
    assert fenwood.arrivals[0].headsign == "Back Bay"  # (now named from trips.txt)
    assert len(http.calls_to(FEED_URL)) == 1  # ... which is not repeated within 30 s
    mono.t += 31
    park, _ = provider.get_arrivals(provider._stops, NOW, 90)
    assert rows(park.arrivals) == EXPECTED_PARK
    assert len(http.calls_to(FEED_URL)) == 2 and len(http.calls_to(STATIC_URL)) == 1


def test_empty_request_does_no_io(tmp_path):
    http = sample_http()
    assert make_provider(tmp_path, http).get_arrivals([], NOW, 90) == []
    assert http.calls == []


def test_single_platform_drops_cancelled_trip(tmp_path):
    provider = warm_provider(tmp_path, [{"id": "70075", "label": "Park SB"}])
    [stop] = provider.get_arrivals(provider._stops, NOW, 90)
    assert rows(stop.arrivals) == [r for r in EXPECTED_PARK if r[0] == "70075"]
    # the CANCELED Braintree trip 77886087 (all stops SKIPPED) is not shown; 77886088 is
    assert [a.headsign for a in stop.arrivals].count("Braintree") == 1


def test_all_stops_one_feed_download(tmp_path):
    http = sample_http()
    provider = make_provider(tmp_path, http)
    provider.warm_up()
    results = provider.get_arrivals(provider._stops, NOW, 90)
    assert [r.stop.label for r in results] == ["Park St", "Fenwood"]
    assert len(results[0].arrivals) == 10 and len(results[1].arrivals) == 8
    assert len(http.calls_to(FEED_URL)) == 1 and len(http.calls_to(STATIC_URL)) == 1


def test_warm_up_indexes_trips_of_observed_routes(tmp_path):
    provider = warm_provider(tmp_path)
    assert provider._static.trip("77885786").headsign == "Ashmont"
    meta = (tmp_path / "gtfs_static_meta.json").read_text(encoding="utf-8")
    assert all('"%s"' % r in meta for r in ("Red", "39", "66"))


@pytest.mark.parametrize("stop, routes, direction, expected", [
    (FENWOOD, ["66"], None, [r for r in EXPECTED_1317 if r[0] == "66"]),
    (FENWOOD, ["39", "nope"], None, [r for r in EXPECTED_1317 if r[0] == "39"]),
    (PARK, ["red"], None, None),  # route id, case-insensitive
    (PARK, [], 1, [r for r in EXPECTED_PARK if r[3] == 1]),
    (PARK, ["Green-B"], None, []),
])
def test_route_and_direction_filters(tmp_path, stop, routes, direction, expected):
    provider = warm_provider(tmp_path, [dict(stop, routes=routes, direction_id=direction)])
    [result] = provider.get_arrivals(provider._stops, NOW, 90)
    if stop is FENWOOD:
        assert [(a.route_name, a.headsign, int(a.time.timestamp())) for a in result.arrivals] == expected
    else:
        assert rows(result.arrivals) == (EXPECTED_PARK if expected is None else expected)


def test_horizon(tmp_path):
    provider = warm_provider(tmp_path)
    [stop] = provider.get_arrivals([provider._stops[1]], NOW, 10)
    assert [int((a.time - NOW).total_seconds()) for a in stop.arrivals] == [335, 572]


# ============================================================================ prefilter


def _full_feed() -> Any:
    feed = pb.FeedMessage()
    feed.ParseFromString(load_fixture("gtfs_rt_tripupdates_sample.pb"))
    return feed


@pytest.mark.parametrize("targets, matched", [
    ({"70075", "70076", "1317"}, 20),
    ({"70075"}, 6),
    ({"1317"}, 9),
    ({"place-pktrm"}, 0),
])
def test_prefilter_equals_full_parse(targets, matched):
    raw = load_fixture("gtfs_rt_tripupdates_sample.pb")
    full = _full_feed()
    header, entities = prefilter_feed(pb, raw, targets)
    assert header.timestamp == FEED_TS and header.gtfs_realtime_version == "2.0"
    assert len(entities) == matched
    assert extract_events(entities, targets) == extract_events(full.entity, targets)


def test_prefilter_equals_full_parse_for_every_stop():
    raw = load_fixture("gtfs_rt_tripupdates_sample.pb")
    full = _full_feed()
    every = {u.stop_id for e in full.entity for u in e.trip_update.stop_time_update}
    assert len(every) > 50
    _, entities = prefilter_feed(pb, raw, every)
    assert extract_events(entities, every) == extract_events(full.entity, every)
    assert [f for f, _, _ in iter_length_delimited(raw)] == [1] + [2] * 20


def test_varint_needles_for_long_stop_ids():
    assert encode_varint(0) == b"\x00" and encode_varint(127) == b"\x7f"
    assert encode_varint(128) == b"\x80\x01" and encode_varint(300) == b"\xac\x02"
    long_id = "stop-" + "x" * 195  # 200 bytes: the length needs a 2-byte varint
    assert stop_id_needle(long_id) == b"\x22\xc8\x01" + long_id.encode()
    msg = new_feed()
    add_trip(msg, "long", [{"stop": long_id, "arr": FEED_TS + 60, "dep": FEED_TS + 90}, {"stop": "Z", "arr": FEED_TS + 99}])
    add_trip(msg, "other", [{"stop": "A", "arr": FEED_TS + 60, "dep": FEED_TS + 90}])
    _, entities = prefilter_feed(pb, msg.SerializeToString(), [long_id])
    assert [e.id for e in entities] == ["long"]
    assert [e.departure for e in extract_events(entities, {long_id})] == [FEED_TS + 90]


# ============================================================================ synthetic feeds


def new_feed(ts: int = FEED_TS) -> Any:
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = ts
    return msg


def add_trip(msg: Any, trip_id: str, stus: List[Dict[str, Any]], *, route: str = "R1", direction: int = 0,
             relationship: Optional[int] = None, headsign: str = "", deleted: bool = False,
             sequence: bool = True, first_seq: int = 1) -> None:
    entity = msg.entity.add()
    entity.id = trip_id
    if deleted:
        entity.is_deleted = True
    update = entity.trip_update
    update.trip.trip_id = trip_id
    update.trip.route_id = route
    update.trip.direction_id = direction
    if relationship is not None:
        update.trip.schedule_relationship = relationship
    if headsign:
        update.trip_properties.trip_headsign = headsign
    for seq, spec in enumerate(stus, first_seq):
        stu = update.stop_time_update.add()
        if sequence:  # NYC subway feeds leave stop_sequence out
            stu.stop_sequence = seq
        stu.stop_id = spec["stop"]
        if spec.get("arr") is not None:
            stu.arrival.time = spec["arr"]
        if spec.get("dep") is not None:
            stu.departure.time = spec["dep"]
        if spec.get("delay") is not None:
            stu.arrival.delay = spec["delay"]
        if spec.get("rel") is not None:
            stu.schedule_relationship = spec["rel"]
        if spec.get("pickup") is not None:
            stu.stop_time_properties.pickup_type = spec["pickup"]


def t(offset: int) -> int:
    return FEED_TS + offset


SKIPPED, NO_DATA = 1, 2
CANCELED, DELETED = 3, 7


def edge_case_feed() -> bytes:
    msg = new_feed()
    add_trip(msg, "ok", [
        {"stop": "S1", "arr": t(100), "dep": t(130)},
        {"stop": "S2", "rel": SKIPPED},
        {"stop": "S3", "rel": NO_DATA},
        {"stop": "S4", "delay": 30},  # delay-only: no absolute time
        {"stop": "S5", "arr": t(300), "dep": t(330), "pickup": 1},  # no pickup
        {"stop": "S6", "arr": t(400)},  # terminal arrival: drop-off only
    ])
    add_trip(msg, "cancelled", [{"stop": "S1", "arr": t(200), "dep": t(220)}], relationship=CANCELED)
    add_trip(msg, "deleted", [{"stop": "S1", "arr": t(210), "dep": t(230)}], relationship=DELETED)
    add_trip(msg, "entity-deleted", [{"stop": "S1", "arr": t(250), "dep": t(260)}], deleted=True)
    add_trip(msg, "arrivals-only", [{"stop": "S0", "arr": t(50)}, {"stop": "S1", "arr": t(500)}], route="R2")
    add_trip(msg, "departed", [{"stop": "S1", "arr": t(-120), "dep": t(-10)}, {"stop": "S9", "arr": t(300)}])
    add_trip(msg, "boarding", [{"stop": "S1", "arr": t(-20), "dep": t(40)}, {"stop": "S9", "arr": t(300)}])
    add_trip(msg, "old", [{"stop": "S1", "arr": t(-200)}, {"stop": "S7", "arr": t(-100), "dep": t(-90)},
                          {"stop": "S9", "arr": t(300)}])
    add_trip(msg, "rt-headsign", [{"stop": "S1", "arr": t(600), "dep": t(600)}, {"stop": "S8", "arr": t(900)}],
             route="R3", headsign="Downtown")
    add_trip(msg, "loop", [{"stop": "S1", "arr": t(700), "dep": t(710)}, {"stop": "S9", "arr": t(750), "dep": t(760)},
                           {"stop": "S1", "arr": t(800), "dep": t(810)}, {"stop": "S10", "arr": t(900)}])
    return msg.SerializeToString()


def test_cancelled_skipped_dropoff_and_departed_are_hidden(tmp_path):
    stops = [{"id": s, "label": s} for s in ("S1", "S2", "S3", "S4", "S5", "S6")]
    provider = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, edge_case_feed()), stops, static_url="")
    results = provider.get_arrivals(provider._stops, NOW, 90)
    s1 = [(a.route_name, a.headsign, int((a.time - NOW).total_seconds())) for a in results[0].arrivals]
    assert s1 == [
        ("R1", None, -20),  # boarding: arrived 20 s ago, leaves in 40 s
        ("R1", None, 100),
        ("R2", None, 500),  # trip without any departure: its last stop is kept
        ("R3", "Downtown", 600),  # real-time trip_properties.trip_headsign
        ("R1", None, 700),  # a loop visits S1 twice: both kept
        ("R1", None, 800),
    ]
    assert [r.arrivals for r in results[1:]] == [[], [], [], [], []]
    assert all(r.error is None for r in results)


def test_vehicle_standing_at_the_stop_counts_as_arriving_now(tmp_path):
    msg = new_feed()
    add_trip(msg, "held", [{"stop": "S1", "arr": t(-150), "dep": t(45)}, {"stop": "S9", "arr": t(300)}])
    add_trip(msg, "left", [{"stop": "S1", "arr": t(-150), "dep": t(-5)}, {"stop": "S9", "arr": t(300)}])
    provider = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, msg.SerializeToString()),
                             [{"id": "S1", "label": "S1"}], static_url="")
    [stop] = provider.get_arrivals(provider._stops, NOW, 90)
    assert [a.time for a in stop.arrivals] == [NOW]  # arrived 150 s ago, still boarding: not "past"


def test_missing_static_url_uses_ids(tmp_path):
    http = FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [PARK, {"id": "70075", "label": "SB"}], static_url="")
    provider.warm_up()
    park, platform = provider.get_arrivals(provider._stops, NOW, 90)
    assert park.arrivals == [] and park.error is None  # a parent station cannot be expanded without stops.txt
    assert [(a.route_name, a.headsign) for a in platform.arrivals] == [("Red", None)] * 5
    assert [u for u, _ in http.calls] == [FEED_URL]
    assert not (tmp_path / "gtfs_static.zip").exists()


# ============================================================================ fetching


def test_feed_request_uses_configured_auth(tmp_path):
    http = FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="", params={"key": "K1"},
                             headers={"x-api-key": "H1"})
    provider.get_arrivals(provider._stops, NOW, 90)
    [(url, headers)] = http.calls
    assert url == FEED_URL + "?key=K1" and headers == {"x-api-key": "H1"}


def test_conditional_get_and_minimum_interval(tmp_path):
    mono = Mono()
    http = FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb", headers={"ETag": '"v1"'})
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="", mono=mono)
    first = provider.get_arrivals(provider._stops, NOW, 90)
    assert len(first[0].arrivals) == 8

    mono.t += 29  # within 30 s: no network at all
    assert provider.get_arrivals(provider._stops, NOW, 90) == first
    assert len(http.calls) == 1

    mono.t += 2  # revalidate: 304 reuses the extracted events
    http.routes.clear()
    http.add_bytes(FEED_URL, b"", status=304)
    assert provider.get_arrivals(provider._stops, NOW, 90) == first
    assert len(http.calls) == 2 and http.calls[1][1]["If-None-Match"] == '"v1"'

    mono.t += 31  # changed feed: parsed again (an empty but valid feed)
    http.routes.clear()
    http.add_bytes(FEED_URL, new_feed().SerializeToString(), headers={"ETag": '"v2"'})
    assert provider.get_arrivals(provider._stops, NOW, 90)[0].arrivals == []
    assert http.calls[2][1]["If-None-Match"] == '"v1"'


def test_concurrent_requests_share_one_download(tmp_path):
    http = FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="")
    results: List[Any] = []
    threads = [threading.Thread(target=lambda: results.append(provider.get_arrivals(provider._stops, NOW, 90)))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 6 and all(r == results[0] for r in results) and len(results[0][0].arrivals) == 8
    assert len(http.calls) == 1


def test_refresh_failure_serves_previous_feed_until_stale(tmp_path, caplog):
    mono = Mono()
    http = FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="", mono=mono)
    first = provider.get_arrivals(provider._stops, NOW, 90)
    http.routes.clear()
    http.add_error(FEED_URL, 503)
    mono.t += 60
    with caplog.at_level(logging.WARNING, logger="meshwx.transit.gtfs_rt"):
        assert provider.get_arrivals(provider._stops, NOW + timedelta(seconds=60), 90)[0].arrivals == \
            first[0].arrivals[:]
    assert "using the previous feed" in caplog.text
    mono.t += 60
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW + timedelta(seconds=400), 90)
    assert ei.value.user_message == "Transit feed is stale"


def test_failure_without_previous_feed_is_not_retried_within_30s(tmp_path):
    mono = Mono()
    http = FakeHttp().add_error(FEED_URL, 503)
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="", mono=mono)
    for _ in range(2):
        with pytest.raises(TransitError) as ei:
            provider.get_arrivals(provider._stops, NOW, 90)
        assert ei.value.user_message == "Transit unavailable, try later"
    assert len(http.calls) == 1
    mono.t += 31
    with pytest.raises(TransitError):
        provider.get_arrivals(provider._stops, NOW, 90)
    assert len(http.calls) == 2


@pytest.mark.parametrize("status, message", [
    (401, "GTFS-RT API key rejected"),
    (403, "GTFS-RT API key rejected"),
    (404, "Transit unavailable, try later"),  # the feed URL is wrong - not "Stop not found"
    (429, "Rate limited, try later"),
])
def test_http_errors(tmp_path, status, message):
    provider = make_provider(tmp_path, FakeHttp().add_error(FEED_URL, status), [FENWOOD], static_url="")
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW, 90)
    assert ei.value.user_message == message


def test_stale_feed(tmp_path):
    provider = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb"),
                             [FENWOOD], static_url="")
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW + timedelta(seconds=301), 90)
    assert ei.value.user_message == "Transit feed is stale"
    relaxed = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb"),
                            [FENWOOD], static_url="", max_feed_age_seconds=600)
    assert relaxed.get_arrivals(relaxed._stops, NOW + timedelta(seconds=301), 90)[0].error is None


@pytest.mark.parametrize("body", [
    '<?xml version="1.0" encoding="utf-16"?><error><message>Invalid key</message></error>'.encode("utf-16"),
    b"<html><body>Service unavailable</body></html>",
    b'{"error": "unauthorized"}',
    b"",
    b"\x12\x00",  # one empty entity and no FeedHeader
    b"\x12\x05abc",  # truncated entity
    b"\xff\xff\xff",
])
def test_unreadable_feed(tmp_path, body):
    provider = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, body), [FENWOOD], static_url="")
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW, 90)
    assert ei.value.user_message == "Transit feed unreadable"


def test_header_without_version_is_unreadable(tmp_path):
    msg = new_feed()
    msg.header.ClearField("gtfs_realtime_version")
    msg.header.timestamp = FEED_TS
    provider = make_provider(tmp_path, FakeHttp().add_bytes(FEED_URL, msg.SerializePartialToString()), [FENWOOD],
                             static_url="")
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW, 90)
    assert ei.value.user_message == "Transit feed unreadable"


def test_missing_bindings(tmp_path, monkeypatch):
    import google.transit

    monkeypatch.delattr(google.transit, "gtfs_realtime_pb2", raising=False)
    monkeypatch.setitem(sys.modules, "google.transit.gtfs_realtime_pb2", None)
    provider = make_provider(tmp_path, FakeHttp(), [FENWOOD], static_url="")
    with pytest.raises(TransitError) as ei:
        provider.get_arrivals(provider._stops, NOW, 90)
    assert ei.value.user_message == BINDINGS_MISSING == "GTFS-RT needs: pip install gtfs-realtime-bindings"


# ============================================================================ clock skew


@dataclass
class DatedBytes(HttpBytes):
    date: Optional[str] = None


class DatedHttp(FakeHttp):
    """FakeHttp whose byte responses carry the HTTP Date header (as a future HttpBytes.date)."""

    def __init__(self, date: str) -> None:
        super().__init__()
        self.date = date

    def get_bytes(self, url: str, **kwargs: Any) -> HttpBytes:  # type: ignore[override]
        res = super().get_bytes(url, **kwargs)
        return DatedBytes(res.content, res.status, res.etag, res.last_modified, res.not_modified, date=self.date)


def test_http_date_corrects_a_wrong_local_clock(tmp_path, caplog):
    mono = Mono()
    http = DatedHttp("Fri, 13 Nov 2026 12:38:45 GMT")  # server clock = capture time (feed built 2 s earlier)
    http.add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="", mono=mono)
    server_now = from_epoch(FEED_TS + 2)
    local_now = server_now + timedelta(hours=1)  # Pi clock one hour fast: the feed would look stale
    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        [stop] = provider.get_arrivals(provider._stops, local_now, 90)
        mono.t += 31
        provider.get_arrivals(provider._stops, local_now, 90)
    assert [int((a.time - local_now).total_seconds()) for a in stop.arrivals][:2] == [333, 570]
    assert stop.arrivals[0].time == from_epoch(1794573858 + 3600)  # shifted onto the local clock
    assert caplog.text.count("local clock is") == 1  # warned once per hour, not per request


def test_small_clock_difference_is_not_corrected(tmp_path):
    http = DatedHttp("Fri, 13 Nov 2026 12:38:45 GMT")
    http.add_bytes(FEED_URL, "gtfs_rt_tripupdates_sample.pb")
    provider = make_provider(tmp_path, http, [FENWOOD], static_url="")
    [stop] = provider.get_arrivals(provider._stops, NOW + timedelta(seconds=100), 90)
    assert stop.arrivals[0].time == from_epoch(1794573858)


# ============================================================================ static schedule


def schedule_zip() -> bytes:
    """Route R1 through stops A, B, C and T (stop_code 4711), with the trips of :func:`schedule_feed`."""
    import io
    import zipfile

    patterns = {"t-end": "A B T", "t-partial": "A T C", "t-noseq": "A B T", "t-loop": "T A T",
                "t-dropoff": "A T* C", "t-lone": "A T"}  # "*": pickup_type 1
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type"]
    for trip_id, pattern in patterns.items():
        for seq, stop in enumerate(pattern.split(), 1):
            stop_times.append("%s,08:%02d:00,08:%02d:00,%s,%d,%d" % (trip_id, seq, seq, stop.rstrip("*"), seq,
                                                                     1 if stop.endswith("*") else 0))
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\nR1,1,,3\n",
        "stops.txt": "stop_id,stop_code,stop_name\nA,,Alpha\nB,,Bravo\nC,,Charlie\nT,4711,Terminal\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                     + "".join("R1,S,%s,Somewhere,0\n" % trip_id for trip_id in patterns),
        "stop_times.txt": "\n".join(stop_times) + "\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def schedule_feed() -> bytes:
    msg = new_feed()
    add_trip(msg, "t-end", [{"stop": "A", "arr": t(100), "dep": t(100)}, {"stop": "B", "arr": t(200), "dep": t(200)},
                            {"stop": "T", "arr": t(300), "dep": t(300)}])  # ends at T: departure set, as at KCM
    # only the next two stops are published: T is the last listed update, not the trip's last stop
    add_trip(msg, "t-partial", [{"stop": "A", "arr": t(400), "dep": t(400)},
                                {"stop": "T", "arr": t(500), "dep": t(500)}])
    add_trip(msg, "t-noseq", [{"stop": "B", "arr": t(550), "dep": t(550)}, {"stop": "T", "arr": t(600), "dep": t(600)}],
             sequence=False)  # NYC style: matched by stop id on the last update
    add_trip(msg, "t-loop", [{"stop": "T", "arr": t(700), "dep": t(700)}], sequence=False)  # first stop of a loop
    add_trip(msg, "t-dropoff", [{"stop": "A", "arr": t(750), "dep": t(750)},
                                {"stop": "T", "arr": t(800), "dep": t(800)},
                                {"stop": "C", "arr": t(900), "dep": t(900)}])  # pickup_type 1 at T
    add_trip(msg, "t-lone", [{"stop": "T", "arr": t(950), "dep": t(950)}], sequence=False)  # its last stop only
    add_trip(msg, "t-new", [{"stop": "NEW", "arr": t(1000), "dep": t(1000)}, {"stop": "A", "arr": t(1100)}])
    return msg.SerializeToString()


def test_trips_ending_at_the_stop_are_hidden_by_the_schedule(tmp_path):
    http = FakeHttp().add_bytes(FEED_URL, schedule_feed()).add_bytes(STATIC_URL, schedule_zip())
    provider = make_provider(tmp_path, http, [{"id": "T", "label": "T"}])
    provider.warm_up()
    [stop] = provider.get_arrivals(provider._stops, NOW, 90)
    assert [int((a.time - NOW).total_seconds()) for a in stop.arrivals] == [500, 700]
    # without the static zip nothing tells those trips apart: all are shown, as before
    bare = make_provider(tmp_path / "bare", FakeHttp().add_bytes(FEED_URL, schedule_feed()),
                         [{"id": "T", "label": "T"}], static_url="")
    [stop] = bare.get_arrivals(bare._stops, NOW, 90)
    assert [int((a.time - NOW).total_seconds()) for a in stop.arrivals] == [300, 500, 600, 700, 800, 950]


def test_stop_missing_from_stops_txt_is_reported_once(tmp_path, caplog):
    http = FakeHttp().add_bytes(FEED_URL, schedule_feed()).add_bytes(STATIC_URL, schedule_zip())
    stops = [{"id": "4711", "label": "Code"}, {"id": "Typo", "label": "Typo"}, {"id": "NEW", "label": "New"}]
    provider = make_provider(tmp_path, http, stops)
    provider.warm_up()
    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        for _ in range(2):
            code, typo, new = provider.get_arrivals(provider._stops, NOW, 90)
            assert (code.error, typo.error) == ("Stop not found", "Stop not found")
            assert new.error is None and len(new.arrivals) == 1  # not in stops.txt, but in the feed: shown
    assert caplog.text.count("not in the static GTFS stops.txt") == 2  # once per stop
    assert "'4711' is not in the static GTFS stops.txt (it is the stop_code of stop_id T" in caplog.text
