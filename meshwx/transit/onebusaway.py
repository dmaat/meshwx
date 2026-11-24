"""OneBusAway REST provider (Puget Sound, Tampa, San Diego, DC bus, ...).

Request: ``GET {base_url}/api/where/arrivals-and-departures-for-stop/{id}.json``
with ``key``, ``minutesBefore=0`` (the default 5 lists buses that already left)
and ``minutesAfter=<horizon>``.

* The body has its own status: ``code`` must be 200 (the shared ``TEST`` key
  answers 429 when throttled).
* Times are epoch **milliseconds**.  A prediction is used when ``predicted``
  is true and ``predictedArrivalTime`` > 0 (``realtime=True``); otherwise the
  scheduled time (``realtime=False``) - OBA reports 0, not null, for "no
  prediction".  Predicted departures in the past are dropped; a bus standing
  at the stop counts as arriving now, or at its departure when it waits
  longer (:func:`~meshwx.transit.visit_time`).
* ``arrivalEnabled`` false (the first stop of a trip, where the "arrival" is
  when the bus pulled in for its layover): the departure time is used, and
  ``predictedDepartureTime`` decides whether there is a prediction.
* ``departureEnabled`` false (drop-off only) and trips whose ``tripStatus``
  says CANCELED are skipped.
* Headsign: the arrival's ``tripHeadsign`` (it overrides the trip's), else the
  referenced trip's.  Route name: ``routeShortName``, else the referenced
  route's short name.  Direction: the referenced trip's ``directionId``.
* ``currentTime`` is the server clock for :class:`~meshwx.transit.ClockSkew`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

from ..config import TransitConfig
from ..errors import TransitError
from ..httpclient import HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from ..timeutil import from_epoch
from . import (RATE_LIMIT_MESSAGE, UNREADABLE_MESSAGE, ClockSkew, arrivals_per_stop, http_failure, parse_direction,
               route_display_name, status_failure, visit_time)
from .base import TransitProvider

log = logging.getLogger(__name__)

_CANCELLED = frozenset({"CANCELED", "CANCELLED"})


class OneBusAwayProvider(TransitProvider):
    """Arrivals from a OneBusAway server (see the module docstring)."""

    name = "onebusaway"

    def __init__(self, cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> None:
        self._base = cfg.onebusaway.base_url.strip().rstrip("/")
        self._key = cfg.onebusaway.api_key
        self._http = http
        self._skew = ClockSkew("OneBusAway")

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime,
                     horizon_minutes: int) -> List[StopArrivals]:
        return arrivals_per_stop(stops, lambda stop: self._stop_arrivals(stop, now, horizon_minutes),
                                 now, horizon_minutes)

    def _stop_arrivals(self, stop: TransitStop, now: datetime, horizon_minutes: int) -> List[Arrival]:
        url = "%s/api/where/arrivals-and-departures-for-stop/%s.json" % (self._base, quote(stop.id, safe=""))
        params = {"key": self._key, "minutesBefore": "0", "minutesAfter": str(horizon_minutes)}
        try:
            doc = self._http.get_json(url, params=params)
        except HttpError as exc:
            raise http_failure(exc, "OneBusAway", RATE_LIMIT_MESSAGE) from exc
        return parse_arrivals(doc, stop, now, self._skew)


def _ms_time(value: Any, offset: timedelta = timedelta(0)) -> Optional[datetime]:
    """Epoch milliseconds (+ ``offset``) -> aware UTC datetime; 0 / missing -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return from_epoch(value / 1000.0) + offset


def _by_id(items: Any) -> Dict[str, Dict[str, Any]]:
    return {str(item["id"]): item for item in items or [] if isinstance(item, dict) and "id" in item}


def parse_arrivals(doc: Any, stop: TransitStop, now: datetime, skew: ClockSkew) -> List[Arrival]:
    """Shown arrivals of one OBA response, shifted onto the local clock (unsorted, unfiltered window)."""
    if not isinstance(doc, dict) or "code" not in doc:
        raise TransitError("OneBusAway: unexpected response", user_message=UNREADABLE_MESSAGE)
    code = doc.get("code")
    if code != 200:
        status = code if isinstance(code, int) else None
        raise status_failure(status, "OneBusAway", RATE_LIMIT_MESSAGE,
                             detail="code %s: %s" % (code, str(doc.get("text") or "")[:80]))
    data = doc.get("data") or {}
    entry = data.get("entry") or {}
    refs = data.get("references") or {}
    routes = _by_id(refs.get("routes"))
    trips = _by_id(refs.get("trips"))
    offset: timedelta = skew.offset(now, _ms_time(doc.get("currentTime")))
    arrivals: List[Arrival] = []
    for item in entry.get("arrivalsAndDepartures") or []:
        if not isinstance(item, dict) or item.get("departureEnabled") is False:
            continue  # drop-off only
        if str((item.get("tripStatus") or {}).get("status") or "").upper() in _CANCELLED:
            continue
        boards_only = item.get("arrivalEnabled") is False  # first stop: the "arrival" began the layover
        key = "DepartureTime" if boards_only else "ArrivalTime"
        realtime = bool(item.get("predicted")) and _ms_time(item.get("predicted" + key)) is not None
        kind = "predicted" if realtime else "scheduled"
        arrival = None if boards_only else _ms_time(item.get(kind + "ArrivalTime"), offset)
        departure = _ms_time(item.get(kind + "DepartureTime"), offset)
        if realtime and departure is not None and departure < now:
            continue  # already departed
        when = visit_time(arrival, departure, now)
        if when is None:
            continue
        trip = trips.get(str(item.get("tripId")), {})
        route_id = str(item.get("routeId") or trip.get("routeId") or "")
        route = routes.get(route_id, {})
        arrivals.append(Arrival(
            stop_id=str(item.get("stopId") or stop.id),
            route_id=route_id,
            route_name=route_display_name(item.get("routeShortName") or route.get("shortName"),
                                          item.get("routeLongName") or route.get("longName"), route_id),
            headsign=item.get("tripHeadsign") or trip.get("tripHeadsign") or None,
            time=when,
            realtime=realtime,
            direction_id=parse_direction(trip.get("directionId")),
        ))
    return arrivals
