"""Static GTFS index for the GTFS-Realtime provider: route names, headsigns, stop names.

GTFS-Realtime feeds carry ids only.  This module turns the agency's static
GTFS zip into small lookup tables:

* ``routes.txt``     - route_id -> (short name, long name), all routes;
* ``stops.txt``      - stop_id -> name, and parent station -> child stops
  (``location_type`` 0 or empty), all stops;
* ``directions.txt`` - optional MBTA extension: (route, direction) -> destination;
* ``trips.txt``      - trip_id -> (route, direction, headsign), but only for
  the *routes of interest*: the configured stops' route filters plus the
  routes seen at the configured stops in real-time data (persisted, so a
  later re-index keeps them).  Feeds whose trip updates carry no route id
  (BART) report the trip ids instead (:meth:`GtfsStaticIndex.note_trips`):
  the next index looks their routes up in ``trips.txt``;
* ``stop_times.txt`` - streamed once per index, keeping only rows of the
  trips of interest: each trip's last stop (where nobody boards) and its
  other ``pickup_type`` 1 (drop-off only) stops.

``shapes.txt`` is never read.

Pi Zero budget:

* the zip is streamed to ``<cache_dir>/gtfs_static.zip``, never held in RAM;
  its ETag / Last-Modified live in ``gtfs_static_meta.json``, so revalidation
  is a cheap conditional GET - daily when the server sends validators, else a
  full download every ``static_refresh_hours``.  A server that answers the
  conditional GET with the same feed in full (it ignores the validators) is
  only asked every ``static_refresh_hours``;
* the zip is re-indexed only when its content changed (CRC-32 and size of the
  indexed members, read from the zip's central directory - a re-download of
  the same feed costs no re-index), or at most once a day when new routes of
  interest appeared; the compact index is persisted as
  ``gtfs_static_index.json`` and loaded on start;
* CSV members are streamed row by row (``csv.reader``, ``utf-8-sig``);
* downloading and indexing run in :meth:`GtfsStaticIndex.warm_up` or in a
  background thread started by :meth:`GtfsStaticIndex.maintain`; lookups
  never wait for them and return None until an index exists, so the provider
  falls back to ids.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
import zipfile
import zlib
from pathlib import Path
from typing import (Any, Callable, Collection, Dict, FrozenSet, Iterable, Iterator, List, NamedTuple, Optional,
                    Sequence, Set, Tuple)

from ..cache import atomic_write_json, read_json
from ..httpclient import HttpClient, HttpError
from . import parse_direction

log = logging.getLogger(__name__)

ZIP_NAME = "gtfs_static.zip"
META_NAME = "gtfs_static_meta.json"
INDEX_NAME = "gtfs_static_index.json"
PARTIAL_MAX_AGE_SECONDS = 3600  # leftover "<zip>.*.part" files older than this are deleted
INDEX_FORMAT = 2
#: The members that are indexed (and make up the zip's content signature).
INDEXED_FILES = ("routes.txt", "stops.txt", "directions.txt", "trips.txt", "stop_times.txt")
#: Conditional revalidation interval when the server sends ETag / Last-Modified.
REVALIDATE_SECONDS = 24 * 3600.0
#: Minimum gap between re-indexes caused by newly seen routes (or trips without a route id).
ROUTE_REINDEX_SECONDS = 24 * 3600.0
#: Bounds of the in-memory sets of trips seen without a route id: still to look up / not in trips.txt.
MAX_UNKNOWN_TRIPS = 500
MAX_UNRESOLVABLE_TRIPS = 5000
#: Wait after a failed download before trying again.
RETRY_SECONDS = 1800.0
MAX_ZIP_BYTES = 256 * 1024 * 1024
#: Leftover ``.part`` files older than this come from an interrupted download.
STALE_PART_SECONDS = 3600.0

#: Starts ``job`` asynchronously (default: a daemon thread).
Spawner = Callable[[Callable[[], None]], None]


class TripInfo(NamedTuple):
    route_id: str
    direction_id: Optional[int]
    headsign: str
    last_stop: str = ""  # stop_id of the trip's last stop (stop_times.txt); "" when unknown
    last_seq: Optional[int] = None  # its stop_sequence
    loop: bool = False  # the trip ends where it starts: its last stop id alone is ambiguous
    no_pickup: Tuple[str, ...] = ()  # the trip's other stops with pickup_type 1 (drop-off only)


class StaticData:
    """Lookup tables built from one static GTFS zip; never mutated after construction."""

    def __init__(
        self,
        *,
        source: str,
        routes: Dict[str, Tuple[str, str]],
        stop_names: Dict[str, str],
        children: Dict[str, List[str]],
        directions: Dict[str, Dict[int, str]],
        trips: Dict[str, TripInfo],
        routes_indexed: FrozenSet[str],
    ) -> None:
        self.source = source  # content signature of the zip this index was built from
        self.routes = routes  # route_id -> (short_name, long_name)
        self.stop_names = stop_names  # stop_id -> stop_name ("" when stops.txt has none), every stop
        self.children = children  # parent station -> boardable child stop ids
        self.directions = directions  # route_id -> {direction_id: destination}
        self.trips = trips  # trip_id -> TripInfo (with its last stop), for routes_indexed only
        self.routes_indexed = routes_indexed  # routes whose trips were read

    def to_json(self, url: str) -> Dict[str, Any]:
        """Compact JSON form: trips are grouped by their :class:`TripInfo` (route, direction, headsign, stops)."""
        groups: Dict[TripInfo, List[str]] = {}
        for trip_id, info in self.trips.items():
            groups.setdefault(info, []).append(trip_id)
        return {
            "format": INDEX_FORMAT,
            "source": {"url": url, "signature": self.source},
            "routes_indexed": sorted(self.routes_indexed),
            "routes": {rid: list(names) for rid, names in self.routes.items()},
            "stops": self.stop_names,
            "children": self.children,
            "directions": {rid: {str(d): dest for d, dest in by_dir.items()} for rid, by_dir in self.directions.items()},
            "trips": [[info.route_id, info.direction_id, info.headsign, info.last_stop, info.last_seq, info.loop,
                       list(info.no_pickup), ids] for info, ids in groups.items()],
        }

    @classmethod
    def from_json(cls, doc: Any, url: str) -> Optional["StaticData"]:
        """Inverse of :meth:`to_json`; None for another feed, another format or a damaged file."""
        try:
            source = doc["source"]
            if doc.get("format") != INDEX_FORMAT or source.get("url") != url:
                return None
            trips: Dict[str, TripInfo] = {}
            for route_id, direction, headsign, last_stop, last_seq, loop, no_pickup, trip_ids in doc["trips"]:
                info = TripInfo(str(route_id), parse_direction(direction), str(headsign), str(last_stop),
                                int(last_seq) if last_seq is not None else None, bool(loop),
                                tuple(str(s) for s in no_pickup))
                for trip_id in trip_ids:
                    trips[str(trip_id)] = info
            return cls(
                source=str(source["signature"]),
                routes={str(rid): (str(names[0]), str(names[1])) for rid, names in doc["routes"].items()},
                stop_names={str(sid): str(name) for sid, name in doc["stops"].items()},
                children={str(p): [str(c) for c in kids] for p, kids in doc["children"].items()},
                directions={str(rid): {int(d): str(dest) for d, dest in by_dir.items()}
                            for rid, by_dir in doc["directions"].items()},
                trips=trips,
                routes_indexed=frozenset(str(r) for r in doc["routes_indexed"]),
            )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- building


def _members(zf: zipfile.ZipFile) -> Dict[str, str]:
    """Lower-cased base name -> member name; tolerates zips that nest files in a folder."""
    found: Dict[str, str] = {}
    for name in zf.namelist():
        base = name.rsplit("/", 1)[-1].lower()
        if base and (base not in found or name.count("/") < found[base].count("/")):
            found[base] = name
    return found


def zip_signature(zip_path: Path) -> str:
    """Content signature of a GTFS zip: CRC-32 and size of each indexed member.

    Read from the zip's central directory, so it costs a few kilobytes of I/O
    however big the feed is.  Raises ``zipfile.BadZipFile`` / OSError.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = _members(zf)
        parts = []
        for filename in INDEXED_FILES:
            member = members.get(filename)
            if member is not None:
                info = zf.getinfo(member)
                parts.append("%s:%08x:%d" % (filename, info.CRC, info.file_size))
    return ";".join(parts)


def _rows(zf: zipfile.ZipFile, members: Dict[str, str], filename: str,
          columns: Sequence[str]) -> Iterator[Tuple[str, ...]]:
    """Stream ``columns`` of a CSV member (missing columns read as ""); nothing for a missing member."""
    member = members.get(filename)
    if member is None:
        log.log(logging.DEBUG if filename == "directions.txt" else logging.WARNING,
                "static GTFS has no %s", filename)
        return
    with zf.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline=""))
        header = next(reader, None)
        if not header:
            return
        index = {name.strip().lower(): i for i, name in enumerate(header)}
        positions = [index.get(column) for column in columns]
        for row in reader:
            yield tuple(row[p].strip() if p is not None and p < len(row) else "" for p in positions)


def _routes_of_trips(zf: zipfile.ZipFile, members: Dict[str, str], trip_ids: Collection[str]) -> Set[str]:
    """The routes of ``trip_ids`` (trips seen in real time without a route id): one pass over trips.txt."""
    if "trips.txt" not in members:
        return set()
    return {route_id for route_id, trip_id in _rows(zf, members, "trips.txt", ("route_id", "trip_id"))
            if route_id and trip_id in trip_ids}


def _trip_ends(zf: zipfile.ZipFile, members: Dict[str, str],
               trips: Dict[str, TripInfo]) -> Dict[str, Tuple[int, str, bool, Tuple[str, ...]]]:
    """trip_id -> (last stop_sequence, last stop, loop, other pickup_type 1 stops) for the trips in ``trips``.

    One streamed pass over stop_times.txt, the biggest member of a feed (tens
    of MB, a minute or two on a Pi Zero); only rows of ``trips`` are kept, and
    the rows may come in any order.
    """
    member = members.get("stop_times.txt")
    if member is None or not trips:
        if member is None and trips:
            log.info("static GTFS has no stop_times.txt; trips ending at a stop are shown there")
        return {}
    bounds: Dict[str, List[Any]] = {}  # trip_id -> [first seq, first stop, last seq, last stop]
    no_pickup: Dict[str, List[Tuple[int, str]]] = {}
    with zf.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline=""))
        header = next(reader, None) or []
        index = {name.strip().lower(): i for i, name in enumerate(header)}
        trip_col, stop_col, seq_col = index.get("trip_id"), index.get("stop_id"), index.get("stop_sequence")
        pickup_col = index.get("pickup_type")
        if trip_col is None or stop_col is None or seq_col is None:
            log.warning("static GTFS stop_times.txt lacks trip_id, stop_id or stop_sequence")
            return {}
        width = max(trip_col, stop_col, seq_col) + 1
        for row in reader:  # hot loop: millions of rows in a big feed
            if len(row) < width:
                continue
            trip_id = row[trip_col].strip()
            if trip_id not in trips:
                continue
            try:
                seq = int(row[seq_col])
            except ValueError:
                continue
            stop_id = row[stop_col].strip()
            known = bounds.get(trip_id)
            if known is None:
                bounds[trip_id] = [seq, stop_id, seq, stop_id]
            elif seq < known[0]:
                known[0], known[1] = seq, stop_id
            elif seq > known[2]:
                known[2], known[3] = seq, stop_id
            if pickup_col is not None and pickup_col < len(row) and row[pickup_col].strip() == "1":
                no_pickup.setdefault(trip_id, []).append((seq, stop_id))
    ends: Dict[str, Tuple[int, str, bool, Tuple[str, ...]]] = {}
    for trip_id, (first_seq, first_stop, last_seq, last_stop) in bounds.items():
        others = tuple(sorted({stop for seq, stop in no_pickup.get(trip_id, ()) if seq != last_seq}))
        ends[trip_id] = (last_seq, last_stop, first_stop == last_stop and first_seq != last_seq, others)
    return ends


def build_static_data(zip_path: Path, source: str,
                      wanted_routes: Callable[[Dict[str, Tuple[str, str]]], Set[str]],
                      wanted_trips: Collection[str] = ()) -> StaticData:
    """Index ``zip_path``.  ``wanted_routes(routes)`` picks the routes whose trips are read.

    The routes of ``wanted_trips`` (trip ids seen without a route id) are read too.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = _members(zf)
        routes: Dict[str, Tuple[str, str]] = {}
        for route_id, short, long_name in _rows(zf, members, "routes.txt",
                                                ("route_id", "route_short_name", "route_long_name")):
            if route_id:
                routes[route_id] = (short, long_name)

        stop_names: Dict[str, str] = {}
        children: Dict[str, List[str]] = {}
        for stop_id, name, parent, location_type in _rows(
                zf, members, "stops.txt", ("stop_id", "stop_name", "parent_station", "location_type")):
            if not stop_id:
                continue
            stop_names[stop_id] = name
            if parent and location_type in ("", "0"):
                children.setdefault(parent, []).append(stop_id)

        directions: Dict[str, Dict[int, str]] = {}
        for route_id, direction, destination in _rows(
                zf, members, "directions.txt", ("route_id", "direction_id", "direction_destination")):
            d = parse_direction(direction)
            if route_id and d is not None and destination:
                directions.setdefault(route_id, {})[d] = destination

        wanted_set = set(wanted_routes(routes))
        if wanted_trips:
            wanted_set |= _routes_of_trips(zf, members, wanted_trips)
        wanted = frozenset(wanted_set)
        trips: Dict[str, TripInfo] = {}
        if wanted:
            shared: Dict[TripInfo, TripInfo] = {}  # one tuple per distinct TripInfo
            for route_id, trip_id, headsign, direction in _rows(
                    zf, members, "trips.txt", ("route_id", "trip_id", "trip_headsign", "direction_id")):
                if trip_id and route_id in wanted:
                    info = TripInfo(route_id, parse_direction(direction), headsign)
                    trips[trip_id] = shared.setdefault(info, info)
            for trip_id, (last_seq, last_stop, loop, others) in _trip_ends(zf, members, trips).items():
                info = trips[trip_id]._replace(last_stop=last_stop, last_seq=last_seq, loop=loop, no_pickup=others)
                trips[trip_id] = shared.setdefault(info, info)
    return StaticData(source=source, routes=routes, stop_names=stop_names, children=children,
                      directions=directions, trips=trips, routes_indexed=wanted)


# --------------------------------------------------------------------------- index


def _spawn_daemon(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="meshwx-gtfs-static", daemon=True).start()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class GtfsStaticIndex:
    """Downloads, indexes and serves one static GTFS feed (see the module docstring).

    With ``url == ""`` everything is a no-op and every lookup returns None.
    Thread-safe: lookups read an immutable :class:`StaticData` snapshot that a
    re-index swaps atomically; one download/index job runs at a time.
    """

    def __init__(
        self,
        url: str,
        http: HttpClient,
        cache_dir: Path,
        refresh_hours: int,
        *,
        route_filters: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
        spawn: Optional[Spawner] = None,
    ) -> None:
        self._url = (url or "").strip()
        self._http = http
        self._dir = Path(cache_dir)
        self._zip_path = self._dir / ZIP_NAME
        self._meta_path = self._dir / META_NAME
        self._index_path = self._dir / INDEX_NAME
        self._refresh_seconds = max(1, int(refresh_hours)) * 3600.0
        self._filters = frozenset(r.strip().lower() for r in route_filters if r and r.strip())
        self._clock = clock
        self._spawn = spawn or _spawn_daemon
        self._data: Optional[StaticData] = None
        self._meta: Dict[str, Any] = {}
        self._observed: Set[str] = set()
        self._unknown_trips: Set[str] = set()  # seen without a route id, not in the index: look up their routes
        self._unresolvable: Set[str] = set()  # such trips that trips.txt does not list either (ADDED trips)
        self._loaded = False
        self._load_lock = threading.Lock()
        self._meta_lock = threading.Lock()
        self._work_lock = threading.Lock()
        self._retry_at = 0.0
        self._route_reindex_at: Optional[float] = None
        self._failed_source: Optional[str] = None
        self._signature: Optional[Tuple[Tuple[int, int], str]] = None  # ((size, mtime_ns), signature) of the zip

    # ------------------------------------------------------------------ lifecycle
    @property
    def ready(self) -> bool:
        """True once lookups use real data (always True without a static URL)."""
        return not self._url or self._data is not None

    def warm_up(self) -> None:
        """Blocking: load the saved index, revalidate/download the zip when due, (re)index when needed."""
        if not self._url:
            return
        self._ensure_loaded()
        with self._work_lock:
            self._work()

    def maintain(self) -> None:
        """Non-blocking: start :meth:`warm_up`'s work in the background when something is due."""
        if not self._url:
            return
        self._ensure_loaded()
        if not self._work_due(self._clock()) or not self._work_lock.acquire(blocking=False):
            return

        def job() -> None:
            try:
                self._work()
            except Exception:  # noqa: BLE001 - a background job must log, not die silently
                log.exception("static GTFS update failed")
            finally:
                self._work_lock.release()

        try:
            self._spawn(job)
        except RuntimeError as exc:  # thread could not be started
            self._work_lock.release()
            log.warning("cannot start the static GTFS update: %s", exc)

    def note_routes(self, route_ids: Iterable[str]) -> None:
        """Record routes seen at the configured stops so the next index keeps their trips."""
        if not self._url:
            return
        self._ensure_loaded()
        with self._meta_lock:
            new = {r for r in route_ids if r} - self._observed
            if not new:
                return
            self._observed |= new
            self._meta["observed_routes"] = sorted(self._observed)
            self._save_meta_locked()
        log.info("static GTFS: routes seen at the configured stops: %s", ", ".join(sorted(new)))

    def note_trips(self, trip_ids: Iterable[str]) -> None:
        """Record trips seen at the configured stops without a route id (BART), so the next index finds their routes.

        Trips the index already knows, or that the last index could not find
        in trips.txt, are ignored; a re-index for new trips is rate-limited like
        one for new routes.
        """
        if not self._url:
            return
        self._ensure_loaded()
        data = self._data
        known: Collection[str] = data.trips if data is not None else ()
        with self._meta_lock:
            new = {t for t in trip_ids if t and t not in known} - self._unknown_trips - self._unresolvable
            room = MAX_UNKNOWN_TRIPS - len(self._unknown_trips)
            if not new or room <= 0:
                return
            self._unknown_trips.update(sorted(new)[:room])
        log.debug("static GTFS: %d trips without a route id to look up", len(new))

    # ------------------------------------------------------------------ lookups (never block)
    def route_names(self, route_id: str) -> Optional[Tuple[str, str]]:
        """(short_name, long_name) or None."""
        data = self._data
        return data.routes.get(route_id) if data is not None else None

    def stop_name(self, stop_id: str) -> Optional[str]:
        data = self._data
        return data.stop_names.get(stop_id) if data is not None else None

    def has_stop(self, stop_id: str) -> Optional[bool]:
        """Whether stops.txt lists ``stop_id``; None while there is no index (or it has no stops)."""
        data = self._data
        if data is None or not data.stop_names:
            return None
        return stop_id in data.stop_names or stop_id in data.children

    def stop_for_code(self, code: str) -> Optional[str]:
        """The stop_id whose ``stop_code`` (the number on the sign) is ``code``; None if there is none.

        Reads stops.txt from the zip on disk: meant for a one-time hint in a warning.
        """
        if not self._url or not code:
            return None
        try:
            with zipfile.ZipFile(self._zip_path) as zf:
                for stop_id, stop_code in _rows(zf, _members(zf), "stops.txt", ("stop_id", "stop_code")):
                    if stop_code == code and stop_id:
                        return stop_id
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, csv.Error, RuntimeError, ValueError):
            return None
        return None

    def child_stops(self, stop_id: str) -> List[str]:
        """Boardable children (``location_type`` 0/empty) of a parent station; [] otherwise."""
        data = self._data
        return list(data.children.get(stop_id, ())) if data is not None else []

    def trip(self, trip_id: str) -> Optional[TripInfo]:
        data = self._data
        return data.trips.get(trip_id) if data is not None else None

    def direction_destination(self, route_id: str, direction_id: Optional[int]) -> Optional[str]:
        data = self._data
        if data is None or direction_id is None:
            return None
        return data.directions.get(route_id, {}).get(direction_id)

    # ------------------------------------------------------------------ internals
    def _ensure_loaded(self) -> None:
        """Read the meta file and the saved index once (both small)."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._remove_stale_partials()
            meta = read_json(self._meta_path, default=None)
            if not isinstance(meta, dict) or meta.get("url") != self._url:
                if isinstance(meta, dict) and meta.get("url"):
                    log.info("static GTFS url changed; discarding the cached feed")
                    _unlink(self._zip_path)
                    _unlink(self._index_path)
                meta = {"url": self._url}
            observed = meta.get("observed_routes")
            with self._meta_lock:
                self._meta = meta
                self._observed = {str(r) for r in observed} if isinstance(observed, list) else set()
            self._data = StaticData.from_json(read_json(self._index_path, default=None), self._url)
            self._loaded = True

    def _remove_stale_partials(self) -> None:
        """Delete half-downloaded zips left by a process that died mid-download.

        Only files untouched for an hour are removed, so a download still being
        written by another process sharing this cache (e.g. ``meshwx preview``
        while the service runs) is left alone.
        """
        cutoff = time.time() - PARTIAL_MAX_AGE_SECONDS
        try:
            leftovers = list(self._dir.glob(ZIP_NAME + ".*.part"))
        except OSError:
            return
        for part in leftovers:
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    log.info("removed an interrupted download: %s", part.name)
            except OSError:
                pass

    def _zip_signature(self) -> Optional[str]:
        """Content signature of the zip on disk (None without one); recomputed only when the file changes."""
        try:
            st = os.stat(self._zip_path)
        except OSError:
            return None
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._signature
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            signature = zip_signature(self._zip_path)
        except (zipfile.BadZipFile, OSError, EOFError):
            signature = "unreadable:%d:%d" % stamp  # indexing reports the damage and discards the file
        self._signature = (stamp, signature)
        return signature

    def _refresh_due(self, now: float) -> bool:
        if now < self._retry_at:
            return False
        if not self._zip_path.exists():
            return True
        with self._meta_lock:
            checked = self._meta.get("checked_at")
            daily = (bool(self._meta.get("etag") or self._meta.get("last_modified"))
                     and not self._meta.get("validators_ignored"))
        if not isinstance(checked, (int, float)):
            return True
        interval = min(REVALIDATE_SECONDS, self._refresh_seconds) if daily else self._refresh_seconds
        elapsed = now - checked
        return elapsed < 0 or elapsed >= interval  # < 0: the clock was wrong when we checked

    def _wanted_routes(self, routes: Dict[str, Tuple[str, str]]) -> Set[str]:
        """Route ids whose trips to index: observed routes plus the configured filters (id or short name)."""
        with self._meta_lock:
            wanted = {r for r in self._observed if r in routes}
        if self._filters:
            wanted.update(rid for rid, (short, _long) in routes.items()
                          if rid.lower() in self._filters or short.lower() in self._filters)
        return wanted

    def _index_outdated(self, data: StaticData) -> bool:
        """New routes of interest, or trips seen without a route id whose route the index does not know yet."""
        if self._wanted_routes(data.routes) - data.routes_indexed:
            return True
        with self._meta_lock:
            return any(t not in data.trips for t in self._unknown_trips)

    def _route_reindex_allowed(self, now: float) -> bool:
        last = self._route_reindex_at
        return last is None or not 0 <= now - last < ROUTE_REINDEX_SECONDS

    def _work_due(self, now: float) -> bool:
        if self._refresh_due(now):
            return True
        source = self._zip_signature()
        if source is None:
            return False
        data = self._data
        if data is None or data.source != source:
            return source != self._failed_source
        return self._index_outdated(data) and self._route_reindex_allowed(now)

    def _work(self) -> None:
        """Download when due, then index when needed.  Caller holds ``_work_lock``."""
        now = self._clock()
        if self._refresh_due(now):
            self._download(now)
        source = self._zip_signature()
        if source is None:
            return
        data = self._data
        if data is None or data.source != source:
            if source != self._failed_source:
                self._reindex(source, "new static feed" if data is not None else "no index yet")
        elif self._index_outdated(data) and self._route_reindex_allowed(now):
            self._route_reindex_at = now
            self._reindex(source, "new routes at the configured stops")

    def _download(self, now: float) -> None:
        with self._meta_lock:
            have_zip = self._zip_path.exists()
            etag = self._meta.get("etag") if have_zip else None
            last_modified = self._meta.get("last_modified") if have_zip else None
        conditional = bool(etag or last_modified)
        before = self._zip_signature() if conditional else None
        self._remove_stale_parts()
        try:
            res = self._http.download(self._url, self._zip_path, etag=etag, last_modified=last_modified,
                                      max_bytes=MAX_ZIP_BYTES)
        except (HttpError, OSError) as exc:
            self._retry_at = now + RETRY_SECONDS
            detail = ("HTTP %d" % exc.status if exc.status else "network error") if isinstance(exc, HttpError) \
                else str(exc)
            log.warning("static GTFS download failed (%s); next try in %d min", detail, RETRY_SECONDS // 60)
            return
        same_feed = False
        if res.not_modified:
            log.debug("static GTFS unchanged (HTTP 304)")
        else:
            self._signature = None  # don't trust (size, mtime) alone: coarse file-system clocks
            log.info("static GTFS downloaded (%d bytes)", self._zip_path.stat().st_size)
            same_feed = conditional and self._zip_signature() == before
            if same_feed:
                log.info("static GTFS server ignores conditional requests; next download in %d h",
                         self._refresh_seconds // 3600)
        with self._meta_lock:
            self._meta.update(etag=res.etag, last_modified=res.last_modified, checked_at=now)
            if res.not_modified:
                self._meta.pop("validators_ignored", None)
            elif same_feed:
                self._meta["validators_ignored"] = True
            self._save_meta_locked()

    def _reindex(self, source: str, reason: str) -> None:
        started = time.monotonic()
        previous = self._data
        with self._meta_lock:
            if previous is None or previous.source != source:
                self._unresolvable.clear()  # a new feed may list them
            tried = frozenset(self._unknown_trips)
        try:
            data = build_static_data(self._zip_path, source, self._wanted_routes, tried)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            log.error("static GTFS zip is damaged (%s); it will be downloaded again", exc)
            _unlink(self._zip_path)
            self._retry_at = self._clock() + RETRY_SECONDS
            with self._meta_lock:
                for key in ("etag", "last_modified", "checked_at"):
                    self._meta.pop(key, None)
                self._save_meta_locked()
            return
        except (OSError, csv.Error, ValueError, MemoryError, RuntimeError) as exc:
            # RuntimeError: encrypted member; NotImplementedError: unsupported compression method
            log.error("cannot index static GTFS: %r", exc)
            self._failed_source = source  # don't retry this same file on every request
            return
        self._data = data
        self._failed_source = None
        if tried:
            with self._meta_lock:
                self._unknown_trips -= tried
                missing = {t for t in tried if t not in data.trips}
                if len(self._unresolvable) + len(missing) > MAX_UNRESOLVABLE_TRIPS:
                    self._unresolvable.clear()
                self._unresolvable |= missing
            self.note_routes(data.trips[t].route_id for t in tried if t in data.trips)  # keep them next time
        try:
            atomic_write_json(self._index_path, data.to_json(self._url))
        except OSError as exc:
            log.warning("cannot save static GTFS index: %s", exc)
        log.info("static GTFS indexed (%s) in %.1f s: %d routes, %d stops, %d trips of %d routes",
                 reason, time.monotonic() - started, len(data.routes), len(data.stop_names),
                 len(data.trips), len(data.routes_indexed))

    def _remove_stale_parts(self) -> None:
        """Delete temp files of downloads that were killed (e.g. the process exited mid-download)."""
        cutoff = time.time() - STALE_PART_SECONDS
        for part in self._dir.glob(ZIP_NAME + ".*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass

    def _save_meta_locked(self) -> None:
        try:
            atomic_write_json(self._meta_path, self._meta)
        except OSError as exc:
            log.warning("cannot save static GTFS state: %s", exc)
