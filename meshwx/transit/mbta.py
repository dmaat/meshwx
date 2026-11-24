"""MBTA V3 API provider (Boston): ``/predictions`` for one stop per request.

Request: ``GET {base_url}/predictions?filter[stop]=<id>&include=route,trip&sort=departure_time&page[limit]=500``
with sparse fieldsets (``fields[prediction]``, ``fields[route]``, ``fields[trip]``:
only what is shown, about 0.8 KB per row; relationships are always sent).  An
API key, if configured, goes in the ``x-api-key`` header (20 -> 1000 requests/min).
One request per stop and fetch, also for a stop with a route filter.

* Parent stations ("place-pktrm") include their child platforms natively.
* The API sorts rows without a departure (vehicles ending their trip at the
  stop, drop-off only) first: at terminals and busways more than 100 of them
  come before the first departure (Forest Hills, Ruggles at rush hour), so a
  small page would hold no departure at all.  The 500-row page (only a cap
  on the response size) holds every prediction the API has for a stop, and
  the rows are sorted again locally by ``time``.
* Route filter: ``filter[route]`` matches exact MBTA route ids and silently
  ignores other entries, while a configured route may also be a short name or
  differ in case ("B" for Green-B, "red").  So the first request for a stop
  with a route filter goes without it; the included route records then
  translate the configured routes to ids, and once every configured route is
  known, later requests send ``filter[route]`` (plus ``filter[direction_id]``,
  which the API only honours together with a route filter).  The stop's route
  and direction filters are always applied locally as well.
* Hidden: CANCELLED (double L) / SKIPPED / NO_DATA rows, rows without a
  departure time (drop-off only, e.g. the last stop) and vehicles that have
  already departed.  ``time`` = arrival, else departure (``now`` for a
  vehicle standing at the stop, or its departure when it waits longer, see
  :func:`~meshwx.transit.visit_time`).
* Headsign: the prediction's ``trip_headsign`` override, else the included
  trip's ``headsign``, else the route's ``direction_destinations``.
* ``status`` ("Boarding", "Stopped 2 stops away", ...) is passed through.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from ..config import TransitConfig
from ..errors import TransitError
from ..httpclient import HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from . import (RATE_LIMIT_MESSAGE, UNREADABLE_MESSAGE, arrivals_per_stop, http_failure, parse_direction, parse_time,
               route_display_name, visit_time)
from .base import TransitProvider

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api-v3.mbta.com"
#: A cap on the response size only: every prediction for a stop (192 rows at Forest Hills at rush hour).
PAGE_LIMIT = 500
#: Sparse fieldsets: the attributes that are shown (the relationships come anyway).
FIELDS = {
    "fields[prediction]": "arrival_time,departure_time,direction_id,schedule_relationship,status,trip_headsign",
    "fields[route]": "short_name,long_name,direction_destinations",
    "fields[trip]": "headsign",
}
#: ``schedule_relationship`` values that hide a prediction.
HIDDEN_RELATIONSHIPS = frozenset({"CANCELLED", "CANCELED", "SKIPPED", "NO_DATA"})

_RouteKey = Tuple[str, FrozenSet[str]]  # (stop id, configured routes, lower case)


class MbtaProvider(TransitProvider):
    """Predictions from the MBTA V3 API (see the module docstring)."""

    name = "mbta"

    def __init__(self, cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> None:
        self._url = (cfg.mbta.base_url or DEFAULT_BASE_URL).rstrip("/") + "/predictions"
        self._headers = {"x-api-key": cfg.mbta.api_key} if cfg.mbta.api_key else None
        self._http = http
        self._route_ids: Dict[_RouteKey, str] = {}  # learned "filter[route]" value per stop and route filter
        self._capped: Set[str] = set()  # stops already warned about a full page

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime,
                     horizon_minutes: int) -> List[StopArrivals]:
        return arrivals_per_stop(stops, lambda stop: self._stop_arrivals(stop, now), now, horizon_minutes)

    def _stop_arrivals(self, stop: TransitStop, now: datetime) -> List[Arrival]:
        wanted = frozenset(r.strip().lower() for r in stop.routes if r.strip())
        key: _RouteKey = (stop.id, wanted)
        route_ids = self._route_ids.get(key) if wanted else None
        doc = self._predictions(stop, route_ids)
        if wanted and route_ids is None:
            learned = resolve_route_ids(doc, wanted)
            if learned:
                log.debug("MBTA: routes %s at %s are route ids %s", ",".join(sorted(wanted)), stop.id, learned)
                self._route_ids[key] = learned
        return parse_predictions(doc, stop, now)

    def _predictions(self, stop: TransitStop, route_ids: Optional[str]) -> Dict[str, Any]:
        params = {"filter[stop]": stop.id, "include": "route,trip", "sort": "departure_time",
                  "page[limit]": str(PAGE_LIMIT)}
        params.update(FIELDS)
        if route_ids:
            params["filter[route]"] = route_ids
            if stop.direction_id is not None:
                params["filter[direction_id]"] = str(stop.direction_id)
        try:
            doc = self._http.get_json(self._url, params=params, headers=self._headers)
        except HttpError as exc:
            raise http_failure(exc, "MBTA", RATE_LIMIT_MESSAGE) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
            raise TransitError("MBTA: unexpected response (no data list)", user_message=UNREADABLE_MESSAGE)
        if len(doc["data"]) >= PAGE_LIMIT and stop.id not in self._capped:
            self._capped.add(stop.id)
            log.warning("MBTA: %d predictions at %s fill a page; later departures may be missing",
                        len(doc["data"]), stop.id)
        return doc


def resolve_route_ids(doc: Dict[str, Any], wanted: FrozenSet[str]) -> Optional[str]:
    """The ``filter[route]`` value for configured routes (lower case: route ids or shown names), or None.

    Uses the route records included in a ``/predictions`` response, so a route
    is known once it had a prediction at the stop.  None until every
    configured route is known: the request then goes without a route filter.
    """
    ids_by_name: Dict[str, Set[str]] = {}
    for item in doc.get("included") or []:
        if not isinstance(item, dict) or item.get("type") != "route" or not item.get("id"):
            continue
        route_id = str(item["id"])
        attrs = item.get("attributes") or {}
        shown = route_display_name(attrs.get("short_name"), attrs.get("long_name"), route_id)
        for name in (route_id, shown):  # what select_arrivals matches
            ids_by_name.setdefault(name.strip().lower(), set()).add(route_id)
    ids: Set[str] = set()
    for name in wanted:
        found = ids_by_name.get(name)
        if not found:
            return None
        ids |= found
    return ",".join(sorted(ids))


def _related_id(prediction: Dict[str, Any], name: str) -> str:
    """``relationships.<name>.data.id`` or ""."""
    related = ((prediction.get("relationships") or {}).get(name) or {}).get("data")
    return str(related.get("id") or "") if isinstance(related, dict) else ""


def _destination(route: Dict[str, Any], direction: Optional[int]) -> Optional[str]:
    destinations = route.get("direction_destinations")
    if direction is None or not isinstance(destinations, list) or direction >= len(destinations):
        return None
    return destinations[direction] or None


def parse_predictions(doc: Dict[str, Any], stop: TransitStop, now: datetime) -> List[Arrival]:
    """Shown arrivals of one ``/predictions`` response (unsorted, not yet filtered by time window)."""
    included: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in doc.get("included") or []:
        if isinstance(item, dict):
            included[(str(item.get("type")), str(item.get("id")))] = item.get("attributes") or {}
    arrivals: List[Arrival] = []
    for prediction in doc["data"]:
        attrs = prediction.get("attributes") or {}
        if str(attrs.get("schedule_relationship") or "").upper() in HIDDEN_RELATIONSHIPS:
            continue
        departure = parse_time(attrs.get("departure_time"))
        if departure is None or departure < now:
            continue  # drop-off only, or already departed
        arrival = parse_time(attrs.get("arrival_time"))
        route_id = _related_id(prediction, "route")
        route = included.get(("route", route_id), {})
        trip = included.get(("trip", _related_id(prediction, "trip")), {})
        direction = parse_direction(attrs.get("direction_id"))
        arrivals.append(Arrival(
            stop_id=_related_id(prediction, "stop") or stop.id,
            route_id=route_id,
            route_name=route_display_name(route.get("short_name"), route.get("long_name"), route_id),
            headsign=attrs.get("trip_headsign") or trip.get("headsign") or _destination(route, direction),
            time=visit_time(arrival, departure, now),  # type: ignore[arg-type] - departure is set
            realtime=True,
            direction_id=direction,
            status=attrs.get("status") or None,
        ))
    return arrivals
