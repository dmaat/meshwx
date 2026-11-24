"""Generic GTFS-Realtime TripUpdates provider (MBTA, NYC subway, SEPTA, KCM, BART, CTA, ...).

Per request:

1. configured parent stations are expanded to their boardable child stops
   via the static GTFS index - real-time feeds reference platforms only.
   Until the index is ready (and the feed has been read for the expanded
   stops), such a stop gets the error "Stop data loading, try later" rather
   than a misleading empty list;
2. the feed is fetched with the configured headers / query params, at most
   once per 30 s and as a conditional GET (ETag / Last-Modified).  All
   configured stops are extracted from every download, so requests for
   different stops share it.  The feed must decode and carry
   ``header.gtfs_realtime_version`` (CTA answers HTTP 200 with an XML error
   page), and ``header.timestamp`` must be newer than ``max_feed_age_seconds``;
3. a byte-level prefilter splits the FeedMessage into entities without
   decoding them and parses only those containing one of the wanted
   ``stop_id`` values (StopTimeUpdate field 4).  A 1 MB feed then costs
   ~2 MB of RAM and tens of milliseconds on a Pi Zero, even with the
   pure-Python protobuf backend;
4. matching stop-time updates become small :class:`StopEvent` tuples (the
   protobuf objects are dropped at once) and then arrivals, named via the
   static index.

Dropped: CANCELED/DELETED trips, SKIPPED/NO_DATA stops, delay-only updates
(no absolute time), drop-off-only stops and vehicles whose departure time has
passed.  Drop-off only is: ``pickup_type`` NONE in the feed; the trip's last
stop per the static ``stop_times.txt`` (matched by ``stop_sequence``, else by
stop id, and only on the trip's last listed update, so feeds that publish just
the next few stops keep their arrivals) or a ``pickup_type`` 1 stop there;
and, for trips the static index does not know, the trip's last update when it
lacks a departure while other updates have one (the MBTA convention).

Once the static index is ready, a configured stop that is not in
``stops.txt`` (nor a parent station there) and never had real-time data gets
the error "Stop not found" and one warning in the log: typically a typo, the
``stop_code`` from the sign instead of the ``stop_id``, or a comma list.

Headsign: static ``trips.txt`` -> real-time ``trip_properties.trip_headsign``
-> name of the trip's last stop -> ``directions.txt`` destination -> None.

Clock: ``header.timestamp`` says when the feed was built, so it is no clock;
the HTTP ``Date`` header is used for :class:`~meshwx.transit.ClockSkew` when
the HTTP client exposes it (as ``HttpBytes.date``).
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Collection, FrozenSet, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple

from ..config import TransitConfig
from ..errors import TransitError
from ..httpclient import HttpBytes, HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from ..timeutil import from_epoch, utcnow
from . import (RATE_LIMIT_MESSAGE, UNREADABLE_MESSAGE, ClockSkew, http_failure, route_display_name, select_arrivals,
               visit_time)
from .base import TransitProvider
from .gtfs_static import GtfsStaticIndex, TripInfo

log = logging.getLogger(__name__)

MIN_FETCH_INTERVAL_SECONDS = 30.0
#: Wire-format key of StopTimeUpdate.stop_id: field 4, wire type 2 (length-delimited).
STOP_ID_KEY = b"\x22"
#: TripDescriptor.ScheduleRelationship values that hide a trip: CANCELED, DELETED.
_TRIP_HIDDEN = frozenset({3, 7})
#: StopTimeUpdate.ScheduleRelationship values that hide a stop: SKIPPED, NO_DATA.
_STOP_HIDDEN = frozenset({1, 2})
#: StopTimeProperties.DropOffPickupType NONE.
_NO_PICKUP = 1

BINDINGS_MISSING = "GTFS-RT needs: pip install gtfs-realtime-bindings"
STALE_MESSAGE = "Transit feed is stale"
LOADING_MESSAGE = "Stop data loading, try later"
NOT_FOUND_MESSAGE = "Stop not found"


def load_bindings() -> Any:
    """Import ``gtfs_realtime_pb2`` on first use (protobuf is heavy and optional)."""
    try:
        from google.transit import gtfs_realtime_pb2
    except Exception as exc:  # ImportError, or protobuf too old/new for the generated code
        raise TransitError("%s (%r)" % (BINDINGS_MISSING, exc), user_message=BINDINGS_MISSING) from exc
    return gtfs_realtime_pb2


# --------------------------------------------------------------------------- wire-format prefilter


def encode_varint(value: int) -> bytes:
    """Protobuf base-128 varint of a non-negative integer."""
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if not value:
            out.append(low)
            return bytes(out)
        out.append(low | 0x80)


def stop_id_needle(stop_id: str) -> bytes:
    """The encoded bytes of ``StopTimeUpdate.stop_id = stop_id`` (key, varint length, UTF-8)."""
    raw = stop_id.encode("utf-8")
    return STOP_ID_KEY + encode_varint(len(raw)) + raw


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]  # IndexError when truncated
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def iter_length_delimited(buf: bytes) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(field_number, start, end)`` of each length-delimited top-level field of a message.

    Other fields are skipped.  Raises ValueError / IndexError on malformed input.
    """
    pos, size = 0, len(buf)
    while pos < size:
        key, pos = _read_varint(buf, pos)
        wire_type = key & 7
        if wire_type == 2:
            length, pos = _read_varint(buf, pos)
            end = pos + length
            if end > size:
                raise ValueError("truncated field")
            yield key >> 3, pos, end
            pos = end
        elif wire_type == 0:
            _, pos = _read_varint(buf, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError("unsupported wire type %d" % wire_type)
    if pos != size:
        raise ValueError("truncated message")


def prefilter_feed(pb: Any, buf: bytes, stop_ids: Iterable[str]) -> Tuple[Any, List[Any]]:
    """Parse the FeedHeader and only the FeedEntity messages whose bytes contain a wanted stop_id.

    A match may be a false positive (it is filtered like any entity), never a
    false negative.  Returns ``(header or None, entities)``; raises DecodeError /
    ValueError / IndexError for malformed input.
    """
    needles = [stop_id_needle(s) for s in set(stop_ids)]
    header = None
    entities: List[Any] = []
    for field, start, end in iter_length_delimited(buf):
        if field == 1:
            header = pb.FeedHeader.FromString(buf[start:end])
        elif field == 2 and any(buf.find(needle, start, end) != -1 for needle in needles):
            entities.append(pb.FeedEntity.FromString(buf[start:end]))
    return header, entities


# --------------------------------------------------------------------------- extraction


class StopEvent(NamedTuple):
    """A stop-time update at a wanted stop, reduced to plain values (epochs on the server clock)."""

    stop_id: str
    trip_id: str
    route_id: str
    direction_id: Optional[int]
    arrival: Optional[int]
    departure: Optional[int]
    trip_headsign: str  # real-time trip_properties.trip_headsign, "" when absent
    last_stop_id: str  # stop of the trip's last stop-time update
    stop_sequence: Optional[int] = None  # StopTimeUpdate.stop_sequence, when the feed sets it
    last_update: bool = False  # the trip's last listed stop-time update
    only_update: bool = False  # the trip's only stop-time update


def _time(event: Any) -> Optional[int]:
    """Absolute time of a StopTimeEvent (unset or delay-only -> None)."""
    return event.time if event.time > 0 else None


def _rt_trip_headsign(update: Any) -> str:
    props = getattr(update, "trip_properties", None)  # absent in old bindings
    return getattr(props, "trip_headsign", "") or ""


def _no_pickup(stu: Any) -> bool:
    props = getattr(stu, "stop_time_properties", None)
    return getattr(props, "pickup_type", 0) == _NO_PICKUP


def extract_events(entities: Iterable[Any], stop_ids: Collection[str]) -> List[StopEvent]:
    """Boardable stop-time updates at ``stop_ids``, in feed order (see the module docstring)."""
    events: List[StopEvent] = []
    for entity in entities:
        if entity.is_deleted or not entity.HasField("trip_update"):
            continue
        update = entity.trip_update
        trip = update.trip
        stus = update.stop_time_update
        if trip.schedule_relationship in _TRIP_HIDDEN or not stus:
            continue
        last = len(stus) - 1
        direction = trip.direction_id if trip.HasField("direction_id") else None
        headsign = _rt_trip_headsign(update)
        trip_has_departures: Optional[bool] = None
        for i, stu in enumerate(stus):
            if stu.stop_id not in stop_ids or stu.schedule_relationship in _STOP_HIDDEN or _no_pickup(stu):
                continue
            arrival, departure = _time(stu.arrival), _time(stu.departure)
            if arrival is None and departure is None:
                continue  # delay-only: would need stop_times.txt
            if departure is None and i == last:
                if trip_has_departures is None:
                    trip_has_departures = any(_time(u.departure) is not None for u in stus)
                if trip_has_departures:
                    continue  # terminal arrival: drop-off only
            events.append(StopEvent(stu.stop_id, trip.trip_id, trip.route_id, direction, arrival, departure,
                                    headsign, stus[last].stop_id,
                                    stu.stop_sequence if stu.HasField("stop_sequence") else None,
                                    i == last, last == 0))
    return events


def scheduled_dropoff_only(event: StopEvent, trip: TripInfo) -> bool:
    """True when static ``stop_times.txt`` says nobody boards at this visit: the trip's last stop, or pickup_type 1.

    The last stop counts only on the trip's last listed update (a trip
    continuing past it is not ending there).  It is matched by
    ``stop_sequence`` when the feed has one (platform changes at a terminal
    keep matching), else by stop id - but not on a trip's only update when the
    trip ends where it starts (a loop: that update may be its first stop).
    """
    if trip.last_stop and event.last_update:
        if event.stop_sequence is not None and trip.last_seq is not None:
            if event.stop_sequence == trip.last_seq:
                return True
        elif event.stop_id == trip.last_stop and not (event.only_update and trip.loop):
            return True
    return event.stop_id in trip.no_pickup


def _server_date(res: HttpBytes) -> Optional[datetime]:
    """Server clock from the HTTP ``Date`` header, when the HTTP client exposes it as ``res.date``."""
    value = getattr(res, "date", None)
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- provider


class _Snapshot(NamedTuple):
    targets: FrozenSet[str]  # stop ids the events were extracted for
    events: Tuple[StopEvent, ...]
    feed_time: Optional[datetime]  # header.timestamp (server clock)
    offset: timedelta  # ClockSkew correction for this response
    etag: Optional[str]
    last_modified: Optional[str]


class GtfsRtProvider(TransitProvider):
    """Arrivals from a GTFS-Realtime TripUpdates feed (see the module docstring).

    ``static`` and ``monotonic`` are test hooks.
    """

    name = "gtfs_rt"

    def __init__(
        self,
        cfg: TransitConfig,
        http: HttpClient,
        cache_dir: Path,
        *,
        static: Optional[GtfsStaticIndex] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rt = cfg.gtfs_rt
        self._stops = list(cfg.stops)
        self._http = http
        self._monotonic = monotonic
        self._static = static if static is not None else GtfsStaticIndex(
            cfg.gtfs_rt.static_gtfs_url, http, Path(cache_dir), cfg.gtfs_rt.static_refresh_hours,
            route_filters=[r for stop in cfg.stops for r in stop.routes])
        self._skew = ClockSkew("GTFS-RT feed", monotonic=monotonic)
        self._lock = threading.Lock()
        self._snapshot: Optional[_Snapshot] = None
        self._last_attempt: Optional[float] = None
        self._last_failure: Optional[TransitError] = None
        self._seen_stops: Set[str] = set()  # configured stop ids that have had real-time data
        self._reported: Set[str] = set()  # configured stop ids already reported as not in stops.txt

    def warm_up(self) -> None:
        """Build the static index; one feed fetch in between shows which routes serve the stops."""
        self._static.warm_up()
        if not self._stops:
            return
        try:
            self._fetch(load_bindings(), self._targets(self._stops), utcnow())
        except TransitError as exc:
            log.info("GTFS-RT warm-up fetch failed: %s", exc)
        self._static.warm_up()  # indexes trips of the routes just seen (no-op if none are new)

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime,
                     horizon_minutes: int) -> List[StopArrivals]:
        if not stops:
            return []
        pb = load_bindings()
        self._static.maintain()
        static_ready = self._static.ready  # read before expanding: parent stations expand only when ready
        expanded = {stop.id: self._expand(stop.id) for stop in stops}
        snapshot = self._fetch(pb, self._targets(stops), now)
        self._check_fresh(snapshot, now)
        results = []
        for stop in stops:
            ids = expanded[stop.id]
            events = [e for e in snapshot.events if e.stop_id in ids]
            if not ids <= snapshot.targets or (not events and not static_ready):
                # platforms not extracted yet, or maybe a parent station that cannot be expanded yet
                results.append(StopArrivals(stop=stop, error=LOADING_MESSAGE))
                continue
            if events:
                self._seen_stops.add(stop.id)
            elif static_ready and self._unknown_stop(stop.id, ids):
                results.append(StopArrivals(stop=stop, error=NOT_FOUND_MESSAGE))
                continue
            arrivals = [a for a in (self._arrival(e, snapshot.offset, now) for e in events) if a is not None]
            results.append(StopArrivals(stop=stop, arrivals=select_arrivals(stop, arrivals, now, horizon_minutes)))
        return results

    def _unknown_stop(self, stop_id: str, ids: FrozenSet[str]) -> bool:
        """True when stops.txt lists neither the stop nor its platforms and the feed never had data for it.

        Logs one warning per stop, naming the stop_id when ``stop_id`` is
        some stop's ``stop_code``.  Never true without static stop data, nor
        for a stop seen in the feed (the static zip may lag behind it).
        """
        if stop_id in self._seen_stops or any(self._static.has_stop(i) is not False for i in ids):
            return False
        if stop_id not in self._reported:
            self._reported.add(stop_id)
            code_of = self._static.stop_for_code(stop_id)
            hint = (" (it is the stop_code of stop_id %s: use that)" % code_of if code_of
                    else " (a typo, the stop_code from the sign, or a list? one stop_id per stop)")
            log.warning("GTFS-RT stop %r is not in the static GTFS stops.txt%s", stop_id, hint)
        return True

    # ------------------------------------------------------------------ feed
    def _expand(self, stop_id: str) -> FrozenSet[str]:
        return frozenset([stop_id] + self._static.child_stops(stop_id))

    def _targets(self, stops: Sequence[TransitStop]) -> FrozenSet[str]:
        """Stop ids to extract: the requested plus all configured stops, parents expanded."""
        ids: Set[str] = set()
        for stop in list(self._stops) + list(stops):
            ids |= self._expand(stop.id)
        return frozenset(ids)

    def _fetch(self, pb: Any, targets: FrozenSet[str], now: datetime) -> _Snapshot:
        """The current feed snapshot: reused for 30 s, then revalidated with a conditional GET.

        Within 30 s of the last network attempt nothing is fetched: the
        previous snapshot is returned - also when it was extracted for fewer
        stops because the static index has just expanded a parent station
        (the caller reports those stops as loading) - or the last failure is
        raised again.  When a refresh fails, a previous snapshot that covers
        ``targets`` is returned (the caller's freshness check still applies).
        """
        with self._lock:
            previous = self._snapshot
            covers = previous is not None and targets <= previous.targets
            mono = self._monotonic()
            if self._last_attempt is not None and mono - self._last_attempt < MIN_FETCH_INTERVAL_SECONDS:
                if previous is not None and (covers or self._last_failure is None):
                    return previous
                if self._last_failure is not None:
                    raise TransitError(str(self._last_failure), user_message=self._last_failure.user_message)
            self._last_attempt = mono
            try:
                snapshot = self._download(pb, targets, now, previous if covers else None)
            except TransitError as exc:
                self._last_failure = exc
                if not covers:
                    raise
                log.warning("GTFS-RT update failed (%s); using the previous feed", exc)
                return previous  # type: ignore[return-value]
            self._last_failure = None
            self._snapshot = snapshot
            return snapshot

    def _download(self, pb: Any, targets: FrozenSet[str], now: datetime,
                  previous: Optional[_Snapshot]) -> _Snapshot:
        try:
            res = self._http.get_bytes(
                self._rt.trip_updates_url,
                params=self._rt.params or None,
                headers=self._rt.headers or None,
                etag=previous.etag if previous else None,
                last_modified=previous.last_modified if previous else None,
            )
        except HttpError as exc:
            raise http_failure(exc, "GTFS-RT", RATE_LIMIT_MESSAGE, per_stop=False) from exc
        offset = self._skew.offset(now, _server_date(res))
        if res.not_modified:
            if previous is None:
                raise TransitError("GTFS-RT feed answered 304 to an unconditional GET",
                                   user_message=UNREADABLE_MESSAGE)
            return previous._replace(offset=offset)
        snapshot = self._parse(pb, res.content, targets, offset, res.etag, res.last_modified)
        self._static.note_routes(e.route_id for e in snapshot.events)
        self._static.note_trips(e.trip_id for e in snapshot.events if not e.route_id)  # BART: no route ids
        return snapshot

    @staticmethod
    def _parse(pb: Any, content: bytes, targets: FrozenSet[str], offset: timedelta,
               etag: Optional[str], last_modified: Optional[str]) -> _Snapshot:
        from google.protobuf.message import DecodeError

        try:
            header, entities = prefilter_feed(pb, content, targets)
        except (DecodeError, ValueError, IndexError) as exc:
            raise TransitError("GTFS-RT feed does not decode: %s" % exc, user_message=UNREADABLE_MESSAGE) from exc
        if header is None or not header.gtfs_realtime_version:
            raise TransitError("GTFS-RT response has no FeedHeader (an error page?)", user_message=UNREADABLE_MESSAGE)
        events = tuple(extract_events(entities, targets))
        log.debug("GTFS-RT feed: %d bytes, %d matching entities, %d stop events",
                  len(content), len(entities), len(events))
        feed_time = from_epoch(header.timestamp) if header.timestamp else None
        return _Snapshot(targets, events, feed_time, offset, etag, last_modified)

    def _check_fresh(self, snapshot: _Snapshot, now: datetime) -> None:
        if snapshot.feed_time is None:
            return
        age = (now - snapshot.offset) - snapshot.feed_time
        if age > timedelta(seconds=self._rt.max_feed_age_seconds):
            raise TransitError("GTFS-RT feed is %d s old" % age.total_seconds(), user_message=STALE_MESSAGE)

    # ------------------------------------------------------------------ arrivals
    def _arrival(self, event: StopEvent, offset: timedelta, now: datetime) -> Optional[Arrival]:
        arrival = from_epoch(event.arrival) + offset if event.arrival is not None else None
        departure = from_epoch(event.departure) + offset if event.departure is not None else None
        if departure is not None and departure < now:
            return None  # already left
        trip = self._static.trip(event.trip_id)
        if trip is not None and scheduled_dropoff_only(event, trip):
            return None  # the trip ends here (or does not pick up here): nobody can board
        route_id = event.route_id or (trip.route_id if trip is not None else "")
        short, long_name = self._static.route_names(route_id) or ("", "")
        direction = event.direction_id if event.direction_id is not None else (
            trip.direction_id if trip is not None else None)
        return Arrival(
            stop_id=event.stop_id,
            route_id=route_id,
            route_name=route_display_name(short, long_name, route_id),
            headsign=self._headsign(event, trip, route_id, direction),
            time=visit_time(arrival, departure, now),  # type: ignore[arg-type] - one of them is set
            realtime=True,
            direction_id=direction,
        )

    def _headsign(self, event: StopEvent, trip: Optional[TripInfo], route_id: str,
                  direction: Optional[int]) -> Optional[str]:
        if trip is not None and trip.headsign:
            return trip.headsign
        if event.trip_headsign:
            return event.trip_headsign
        if event.last_stop_id and event.last_stop_id != event.stop_id:
            name = self._static.stop_name(event.last_stop_id)
            if name:
                return name
        return self._static.direction_destination(route_id, direction)
