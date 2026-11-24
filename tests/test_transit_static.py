"""Tests for meshwx.transit.gtfs_static (static GTFS download, indexing, persistence)."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from meshwx.transit import gtfs_static
from meshwx.transit.gtfs_static import (
    INDEX_NAME,
    META_NAME,
    ZIP_NAME,
    GtfsStaticIndex,
    StaticData,
    TripInfo,
    build_static_data,
    zip_signature,
)

from conftest import FakeHttp, fixture_path

URL = "https://static.test/MBTA_GTFS.zip"
DAY = 24 * 3600.0


class Clock:
    def __init__(self, t: float = 1.8e9) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Jobs:
    """Spawner that queues background jobs so a test decides when they run."""

    def __init__(self) -> None:
        self.queue: List[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        self.queue.append(job)

    def run_all(self) -> int:
        count = len(self.queue)
        while self.queue:
            self.queue.pop(0)()
        return count


def sample_bytes() -> bytes:
    return fixture_path("gtfs_static_sample.zip").read_bytes()


def rewrite_zip(source: bytes, edit: Callable[[str, str], Optional[str]], extra: Optional[Dict[str, str]] = None,
                *, prefix: str = "") -> bytes:
    """Copy of a zip with each member's text passed through ``edit(name, text)`` (None drops the member)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            text = edit(name, src.read(name).decode("utf-8"))
            if text is not None:
                dst.writestr(prefix + name, text.encode("utf-8"))
        for name, text in (extra or {}).items():
            dst.writestr(prefix + name, text.encode("utf-8"))
    return out.getvalue()


def make_index(tmp_path: Path, http: FakeHttp, *, url: str = URL, clock: Optional[Clock] = None,
               spawn: Optional[Callable[..., None]] = None, refresh_hours: int = 168,
               route_filters: tuple = ()) -> GtfsStaticIndex:
    return GtfsStaticIndex(url, http, tmp_path, refresh_hours, route_filters=route_filters,
                           clock=clock or Clock(), spawn=spawn or (lambda job: job()))


@pytest.fixture
def count_builds(monkeypatch) -> List[int]:
    """Counts calls of build_static_data (one per (re)index)."""
    calls: List[int] = []
    real = gtfs_static.build_static_data

    def counting(*args: Any, **kwargs: Any) -> StaticData:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(gtfs_static, "build_static_data", counting)
    return calls


# ============================================================================ building


def test_build_from_sample_zip():
    path = fixture_path("gtfs_static_sample.zip")
    data = build_static_data(path, "sig", lambda routes: {"Red", "39", "66"})
    assert data.routes == {"Red": ("", "Red Line"), "39": ("39", "Forest Hills Station - Back Bay Station"),
                           "66": ("66", "Harvard Square - Nubian Station")}
    assert len(data.stop_names) == 119
    assert data.stop_names["70075"] == "Park Street" and data.stop_names["place-pktrm"] == "Park Street"
    assert data.stop_names["1317"] == "Huntington Ave opp Fenwood Rd"
    assert data.stop_names["23391"] == "Back Bay" and data.stop_names["64000"] == "Nubian"
    assert data.children["place-pktrm"] == ["70075", "70076"]
    assert "1317" not in data.children  # no parent
    assert len(data.trips) == 19
    assert data.trips["77885786"] == TripInfo("Red", 0, "Ashmont")
    assert data.trips["78174862"] == TripInfo("66", 1, "Nubian via Allston")
    assert "78612398-OL1" not in data.trips
    assert data.directions == {}  # the sample has no directions.txt
    assert data.trips["77885786"] is data.trips["77885728"]  # identical rows share one tuple


def test_trips_filtered_to_wanted_routes():
    data = build_static_data(fixture_path("gtfs_static_sample.zip"), "sig", lambda routes: {"39"})
    assert sorted(data.trips) == ["78611546", "78611569", "78612208", "78612401"]
    assert data.routes_indexed == frozenset({"39"})
    none = build_static_data(fixture_path("gtfs_static_sample.zip"), "sig", lambda routes: set())
    assert none.trips == {} and len(none.routes) == 3


def test_bom_crlf_nested_folder_reordered_columns_and_directions(tmp_path):
    def edit(name: str, text: str) -> Optional[str]:
        if name == "trips.txt":  # reorder columns, BOM, CRLF
            lines = text.splitlines()
            header = lines[0].split(",")
            order = [header.index(c) for c in ("trip_id", "direction_id", "trip_headsign", "route_id")]
            out = [",".join(line.split(",")[i] for i in order) for line in lines]
            return "﻿" + "\r\n".join(out) + "\r\n"
        return text

    directions = ("route_id,direction_id,direction,direction_destination\n"
                  "Red,0,South,Ashmont/Braintree\nRed,1,North,Alewife\n")
    blob = rewrite_zip(sample_bytes(), edit, {"directions.txt": directions}, prefix="gtfs/")
    path = tmp_path / "nested.zip"
    path.write_bytes(blob)
    data = build_static_data(path, "sig", lambda routes: {"Red"})
    assert data.trips["77885757"] == TripInfo("Red", 1, "Alewife")
    assert data.directions == {"Red": {0: "Ashmont/Braintree", 1: "Alewife"}}
    assert data.children["place-pktrm"] == ["70075", "70076"]


def test_missing_members_are_tolerated(tmp_path, caplog):
    path = tmp_path / "routes_only.zip"
    path.write_bytes(rewrite_zip(sample_bytes(), lambda name, text: text if name == "routes.txt" else None))
    with caplog.at_level(logging.WARNING, logger="meshwx.transit.gtfs_static"):
        data = build_static_data(path, "sig", lambda routes: {"Red"})
    assert len(data.routes) == 3 and data.stop_names == {} and data.trips == {}
    assert "no stops.txt" in caplog.text and "no trips.txt" in caplog.text
    assert "directions.txt" not in caplog.text  # optional: debug only


def test_json_roundtrip():
    data = build_static_data(fixture_path("gtfs_static_sample.zip"), "sig-1", lambda routes: {"Red", "66"})
    doc = json.loads(json.dumps(data.to_json(URL)))
    again = StaticData.from_json(doc, URL)
    assert again is not None
    assert (again.source, again.routes, again.stop_names, again.children, again.trips, again.routes_indexed) == \
        (data.source, data.routes, data.stop_names, data.children, data.trips, data.routes_indexed)
    assert len(doc["trips"]) < len(data.trips)  # grouped by (route, direction, headsign)
    assert StaticData.from_json(doc, "https://other.test/gtfs.zip") is None
    assert StaticData.from_json({"format": 1}, URL) is None
    assert StaticData.from_json(None, URL) is None


STOP_TIMES = (  # rows out of order on purpose; GTFS does not promise any order
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
    "77885786,08:10:00,08:10:00,70093,12,1,0\n"  # Ashmont: last stop (pickup_type 1 too)
    "77885786,08:00:00,08:00:00,70061,1,0,1\n"
    "77885786,08:04:00,08:04:00,70075,5,0,0\n"
    "78611546,09:00:00,09:00:00,1317,3,,\n"
    "78611546,08:50:00,08:50:00,10642,1,0,0\n"
    "78611546,09:20:00,09:20:00,23391,7,1,0\n"  # a drop-off-only stop before the end
    "78611546,09:30:00,09:30:00,10642,9,1,0\n"  # back where it started: a loop
    "78611546,09:10:00,09:10:00,1317,5,0,0\n"
    "99999999,09:00:00,09:00:00,1317,1,0,0\n"  # a trip of a route that is not of interest
)


def test_stop_times_give_each_trip_its_last_stop_and_dropoff_only_stops(tmp_path):
    path = tmp_path / "with_stop_times.zip"
    path.write_bytes(rewrite_zip(sample_bytes(), lambda name, text: text, {"stop_times.txt": STOP_TIMES}))
    data = build_static_data(path, "sig", lambda routes: {"Red", "39"})
    assert data.trips["77885786"] == TripInfo("Red", 0, "Ashmont", "70093", 12)
    assert data.trips["78611546"] == TripInfo("39", 1, "Back Bay", "10642", 9, True, ("23391",))
    assert "99999999" not in data.trips
    assert data.trips["77885728"].last_stop == ""  # no rows in stop_times.txt: unknown
    again = StaticData.from_json(json.loads(json.dumps(data.to_json(URL))), URL)
    assert again is not None and again.trips == data.trips
    assert "stop_times.txt:" in zip_signature(path)


def test_zip_signature_depends_on_indexed_content_only(tmp_path):
    a, b, c = tmp_path / "a.zip", tmp_path / "b.zip", tmp_path / "c.zip"
    a.write_bytes(sample_bytes())
    b.write_bytes(rewrite_zip(sample_bytes(), lambda name, text: text, {"shapes.txt": "shape_id\nx\n"}))
    c.write_bytes(rewrite_zip(sample_bytes(), lambda name, text: text.replace("Back Bay", "Back Bay Sta")
                              if name == "trips.txt" else text))
    assert zip_signature(a) == zip_signature(b) != zip_signature(c)
    assert "trips.txt:" in zip_signature(a)


# ============================================================================ GtfsStaticIndex


def test_download_index_and_lookups(tmp_path):
    http = FakeHttp().add_bytes(URL, "gtfs_static_sample.zip", headers={"ETag": '"e1"'})
    index = make_index(tmp_path, http, route_filters=("red",))
    assert not index.ready
    index.warm_up()
    assert index.ready
    assert index.route_names("Red") == ("", "Red Line") and index.route_names("nope") is None
    assert index.stop_name("70093") == "Ashmont" and index.stop_name("nope") is None
    assert index.child_stops("place-pktrm") == ["70075", "70076"] and index.child_stops("70075") == []
    assert index.trip("77885786") == TripInfo("Red", 0, "Ashmont")
    assert index.trip("78611546") is None  # route 39 is neither filtered nor observed
    assert index.direction_destination("Red", 0) is None
    meta = json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))
    assert meta["etag"] == '"e1"' and meta["url"] == URL
    assert (tmp_path / ZIP_NAME).read_bytes() == sample_bytes()
    assert (tmp_path / INDEX_NAME).exists()


def test_saved_index_is_used_without_network(tmp_path, count_builds):
    clock = Clock()
    make_index(tmp_path, FakeHttp().add_bytes(URL, "gtfs_static_sample.zip", headers={"ETag": '"e1"'}),
               clock=clock, route_filters=("Red",)).warm_up()
    assert len(count_builds) == 1
    offline = FakeHttp()
    clock.t += 3600
    index = make_index(tmp_path, offline, clock=clock, route_filters=("Red",))
    index.maintain()
    assert index.trip("77885786").headsign == "Ashmont"
    assert offline.calls == [] and len(count_builds) == 1


def test_reindex_only_when_content_changes(tmp_path, count_builds):
    clock = Clock()
    http = FakeHttp().add_bytes(URL, "gtfs_static_sample.zip", headers={"ETag": '"e1"'})
    index = make_index(tmp_path, http, clock=clock, route_filters=("39",))
    index.warm_up()
    assert len(count_builds) == 1

    clock.t += 2 * DAY  # unchanged on the server: 304
    http.routes.clear()
    http.add_bytes(URL, b"", status=304)
    index.warm_up()
    assert http.calls[-1][1]["If-None-Match"] == '"e1"'
    assert len(count_builds) == 1

    clock.t += 2 * DAY  # same bytes again (server ignores conditional requests): no re-index
    http.routes.clear()
    http.add_bytes(URL, sample_bytes(), headers={"ETag": '"e2"'})
    index.warm_up()
    assert len(count_builds) == 1 and index.trip("78611546").headsign == "Back Bay"

    clock.t += 7 * DAY + 1  # new feed version (after refresh_hours: this server ignores validators): re-indexed
    changed = rewrite_zip(sample_bytes(), lambda name, text: text.replace("Back Bay", "Back Bay Station")
                          if name == "trips.txt" else text)
    http.routes.clear()
    http.add_bytes(URL, changed, headers={"ETag": '"e3"'})
    index.warm_up()
    assert len(count_builds) == 2 and index.trip("78611546").headsign == "Back Bay Station"
    assert json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))["etag"] == '"e3"'


def test_revalidation_schedule(tmp_path):
    clock = Clock()
    jobs = Jobs()
    with_validators = make_index(tmp_path / "a", FakeHttp().add_bytes(URL, "gtfs_static_sample.zip",
                                                                       headers={"ETag": '"e1"'}),
                                 clock=clock, spawn=jobs, refresh_hours=72)
    without = make_index(tmp_path / "b", FakeHttp().add_bytes(URL, "gtfs_static_sample.zip"),
                         clock=clock, spawn=jobs, refresh_hours=72)
    for index in (with_validators, without):
        index.maintain()
        assert jobs.run_all() == 1 and index.ready
    clock.t += DAY - 60
    with_validators.maintain()
    without.maintain()
    assert jobs.run_all() == 0
    clock.t += 120  # > 1 day: a cheap conditional GET when the server sends validators
    with_validators.maintain()
    without.maintain()
    assert jobs.run_all() == 1
    clock.t += 2 * DAY  # > 72 h since the last download: full refresh without validators
    without.maintain()
    assert jobs.run_all() == 1


def test_server_ignoring_validators_is_asked_every_refresh_hours_only(tmp_path):
    clock = Clock()
    jobs = Jobs()
    http = FakeHttp().add_bytes(URL, "gtfs_static_sample.zip", headers={"ETag": '"e1"'})  # always a full 200
    index = make_index(tmp_path, http, clock=clock, spawn=jobs, refresh_hours=72)

    def check_after(seconds: float) -> int:
        clock.t += seconds
        index.maintain()
        return jobs.run_all()

    def meta() -> Dict[str, Any]:
        return json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))

    assert check_after(0) == 1 and "validators_ignored" not in meta()  # first download: nothing to compare
    assert check_after(DAY + 60) == 1  # daily conditional GET ... answered with the same feed in full
    assert http.calls[-1][1]["If-None-Match"] == '"e1"' and meta()["validators_ignored"] is True
    assert check_after(DAY + 60) == 0  # no more daily revalidation
    assert check_after(2 * DAY) == 1  # 72 h after the last download
    http.routes.clear()
    http.add_bytes(URL, b"", status=304)
    assert check_after(3 * DAY + 1) == 1 and "validators_ignored" not in meta()  # 304: validators work again
    assert check_after(DAY + 1) == 1  # back to daily revalidation


def test_unindexable_zip_is_not_retried_on_every_request(tmp_path, monkeypatch, caplog):
    def unsupported(*args: Any, **kwargs: Any) -> StaticData:
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(gtfs_static, "build_static_data", unsupported)
    jobs = Jobs()
    index = make_index(tmp_path, FakeHttp().add_bytes(URL, "gtfs_static_sample.zip"), spawn=jobs)
    with caplog.at_level(logging.ERROR, logger="meshwx.transit.gtfs_static"):
        index.warm_up()
    assert "cannot index static GTFS" in caplog.text and not index.ready
    index.maintain()
    assert jobs.queue == []  # the same file is not indexed again (a new download would be)


def test_new_routes_trigger_at_most_one_reindex_per_day(tmp_path, count_builds):
    clock = Clock()
    http = FakeHttp().add_bytes(URL, "gtfs_static_sample.zip", headers={"ETag": '"e1"'})
    index = make_index(tmp_path, http, clock=clock)
    index.warm_up()
    assert index.trip("77885786") is None and len(count_builds) == 1  # no routes of interest yet

    index.note_routes(["Red"])
    index.maintain()  # background job runs synchronously here
    assert index.trip("77885786").headsign == "Ashmont" and len(count_builds) == 2

    index.note_routes(["Red", "39"])
    index.maintain()
    assert index.trip("78611546") is None and len(count_builds) == 2  # same day: wait

    clock.t += DAY + 1
    index.maintain()
    assert index.trip("78611546").headsign == "Back Bay" and len(count_builds) == 3
    assert json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))["observed_routes"] == ["39", "Red"]

    restarted = make_index(tmp_path, FakeHttp(), clock=clock)  # observed routes and index persist
    restarted.maintain()
    assert restarted.trip("78611546").headsign == "Back Bay" and len(count_builds) == 3


def test_trips_without_route_ids_are_looked_up_in_trips_txt(tmp_path, count_builds):
    clock = Clock()
    index = make_index(tmp_path, FakeHttp().add_bytes(URL, "gtfs_static_sample.zip"), clock=clock)
    index.warm_up()
    assert index.trip("78611546") is None and len(count_builds) == 1  # no routes of interest yet
    index.note_trips(["78611546", "added-trip", ""])  # a route 39 trip, and one trips.txt does not list
    index.maintain()
    assert index.trip("78611546").headsign == "Back Bay" and len(count_builds) == 2
    assert index.trip("78612208").route_id == "39"  # every trip of the route
    assert json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))["observed_routes"] == ["39"]
    index.note_trips(["78611546", "added-trip"])  # known, or known to be missing: nothing to look up
    clock.t += DAY + 1
    index.maintain()
    assert len(count_builds) == 2


def test_has_stop_and_stop_for_code(tmp_path):
    blob = rewrite_zip(sample_bytes(), lambda name, text: text.replace("\n1317,1317,", "\n1317,90210,")
                       if name == "stops.txt" else text)
    index = make_index(tmp_path, FakeHttp().add_bytes(URL, blob))
    assert index.has_stop("1317") is None  # no index yet
    index.warm_up()
    assert (index.has_stop("1317"), index.has_stop("place-pktrm"), index.has_stop("1371")) == (True, True, False)
    assert index.stop_for_code("90210") == "1317" and index.stop_for_code("1371") is None
    assert make_index(tmp_path / "none", FakeHttp(), url="").has_stop("1317") is None


def test_maintain_never_blocks(tmp_path):
    jobs = Jobs()
    index = make_index(tmp_path, FakeHttp().add_bytes(URL, "gtfs_static_sample.zip"), spawn=jobs)
    index.maintain()
    index.maintain()  # the first job holds the work lock: no second job
    assert len(jobs.queue) == 1
    assert index.child_stops("place-pktrm") == [] and index.stop_name("70075") is None  # fallbacks meanwhile
    jobs.run_all()
    assert index.child_stops("place-pktrm") == ["70075", "70076"]
    index.maintain()
    assert jobs.queue == []  # nothing due


def test_empty_url_is_a_noop(tmp_path):
    http = FakeHttp()
    index = make_index(tmp_path, http, url="")
    index.warm_up()
    index.maintain()
    index.note_routes(["Red"])
    assert index.ready and index.trip("x") is None and index.child_stops("place-pktrm") == []
    assert http.calls == [] and list(tmp_path.iterdir()) == []


def test_failed_download_is_retried_later(tmp_path, caplog):
    clock = Clock()
    jobs = Jobs()
    http = FakeHttp().add_error(URL, 503)
    index = make_index(tmp_path, http, clock=clock, spawn=jobs)
    with caplog.at_level(logging.WARNING, logger="meshwx.transit.gtfs_static"):
        index.warm_up()
    assert "static GTFS download failed (HTTP 503)" in caplog.text and not index.ready
    index.maintain()
    assert jobs.queue == []  # retry later, not on every request
    clock.t += 1801
    http.routes.clear()
    http.add_bytes(URL, "gtfs_static_sample.zip")
    index.maintain()
    jobs.run_all()
    assert index.ready and len(http.calls) == 2


def test_damaged_zip_is_discarded(tmp_path, caplog):
    clock = Clock()
    http = FakeHttp().add_bytes(URL, b"PK\x03\x04 this is not a zip", headers={"ETag": '"bad"'})
    index = make_index(tmp_path, http, clock=clock)
    with caplog.at_level(logging.ERROR, logger="meshwx.transit.gtfs_static"):
        index.warm_up()
    assert "damaged" in caplog.text and not index.ready
    assert not (tmp_path / ZIP_NAME).exists()
    assert "etag" not in json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))
    clock.t += 1801
    http.routes.clear()
    http.add_bytes(URL, "gtfs_static_sample.zip")
    index.warm_up()
    assert index.ready
    assert "If-None-Match" not in http.calls[-1][1]  # the damaged file's ETag was forgotten


def test_url_change_discards_the_cached_feed(tmp_path):
    make_index(tmp_path, FakeHttp().add_bytes(URL, "gtfs_static_sample.zip")).warm_up()
    other = "https://static.test/other.zip"
    http = FakeHttp().add_bytes(other, "gtfs_static_sample.zip")
    index = make_index(tmp_path, http, url=other)
    index.warm_up()
    assert [u for u, _ in http.calls] == [other] and index.ready
    assert json.loads((tmp_path / META_NAME).read_text(encoding="utf-8"))["url"] == other
