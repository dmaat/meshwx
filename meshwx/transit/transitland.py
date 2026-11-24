"""Transitland v2 REST provider: departures for one stop per request (API key required).

Request: ``GET {base_url}/stops/{stop_key}/departures?next=<horizon s>&limit=30&use_service_window=false``
with the key in the ``apikey`` header.  ``stop_key`` is the configured stop id:
a Transitland integer id, a Onestop ID, or ``<feed_onestop_id>:<gtfs stop_id>``
(e.g. ``f-drt-mbta:70075``).  ``use_service_window=false`` stops Transitland
from mapping an expired feed onto a "representative" week (wrong dates).

* Time: ``departure.estimated_utc`` (real time) -> ``departure.scheduled_utc``
  (schedule only) -> the same two of ``arrival``.  The ``*_local`` ISO values
  are accepted when ``*_utc`` is missing; the bare service-day ``HH:MM:SS``
  fields (``scheduled`` / ``estimated``, possibly >= 24:00) are never used.
* Hidden: CANCELED trips, SKIPPED stops, ``pickup_type`` 1 (no pickup:
  drop-off only) and real-time departures that have already left.  A NO_DATA
  row has no estimate, so it shows its scheduled time like any schedule-only
  (``STATIC``) row.
* Headsign: ``trip.trip_headsign``, else the stop's ``stop_headsign``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from ..config import TransitConfig
from ..errors import TransitError
from ..httpclient import HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from . import (RATE_LIMIT_MESSAGE, UNREADABLE_MESSAGE, arrivals_per_stop, http_failure, parse_direction, parse_time,
               route_display_name)
from .base import TransitProvider

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://transit.land/api/v2/rest"
LIMIT = 30
_HIDDEN = frozenset({"CANCELED", "CANCELLED", "SKIPPED"})
_NO_PICKUP = "1"  # GTFS pickup_type: no pickup available


class TransitlandProvider(TransitProvider):
    """Departures from the Transitland v2 REST API (see the module docstring)."""

    name = "transitland"

    def __init__(self, cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> None:
        self._base = (cfg.transitland.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._headers = {"apikey": cfg.transitland.api_key} if cfg.transitland.api_key else None
        self._http = http

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime,
                     horizon_minutes: int) -> List[StopArrivals]:
        return arrivals_per_stop(stops, lambda stop: self._stop_arrivals(stop, now, horizon_minutes),
                                 now, horizon_minutes)

    def _stop_arrivals(self, stop: TransitStop, now: datetime, horizon_minutes: int) -> List[Arrival]:
        url = "%s/stops/%s/departures" % (self._base, quote(stop.id, safe=":"))
        params = {"next": str(horizon_minutes * 60), "limit": str(LIMIT), "use_service_window": "false"}
        try:
            doc = self._http.get_json(url, params=params, headers=self._headers)
        except HttpError as exc:
            raise http_failure(exc, "Transitland", RATE_LIMIT_MESSAGE) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("stops"), list):
            raise TransitError("Transitland: unexpected response (no stops list)", user_message=UNREADABLE_MESSAGE)
        return parse_departures(doc, stop, now)


def _event_time(event: Any) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(estimated, scheduled) absolute times of an ``arrival`` / ``departure`` object."""
    if not isinstance(event, dict):
        return None, None
    estimated = parse_time(event.get("estimated_utc")) or parse_time(event.get("estimated_local"))
    scheduled = parse_time(event.get("scheduled_utc")) or parse_time(event.get("scheduled_local"))
    return estimated, scheduled


def _pick_time(departure: Dict[str, Any]) -> Tuple[Optional[datetime], bool, bool]:
    """(time, realtime, is_departure) by the order in the module docstring."""
    for key in ("departure", "arrival"):
        estimated, scheduled = _event_time(departure.get(key))
        if estimated is not None:
            return estimated, True, key == "departure"
        if scheduled is not None:
            return scheduled, False, key == "departure"
    return None, False, False


def parse_departures(doc: Dict[str, Any], stop: TransitStop, now: datetime) -> List[Arrival]:
    """Shown arrivals of one departures response (unsorted, not yet filtered by time window)."""
    arrivals: List[Arrival] = []
    for stop_doc in doc["stops"]:
        if not isinstance(stop_doc, dict):
            continue
        for departure in stop_doc.get("departures") or []:
            if not isinstance(departure, dict):
                continue
            trip = departure.get("trip") or {}
            relationships = {str(departure.get("schedule_relationship") or "").upper(),
                             str(trip.get("schedule_relationship") or "").upper()}
            if relationships & _HIDDEN or str(departure.get("pickup_type")) == _NO_PICKUP:
                continue
            when, realtime, is_departure = _pick_time(departure)
            if when is None or (realtime and is_departure and when < now):
                continue  # no usable time, or already departed
            route = trip.get("route") or {}
            route_id = str(route.get("route_id") or "")
            arrivals.append(Arrival(
                stop_id=str(stop_doc.get("stop_id") or stop.id),
                route_id=route_id,
                route_name=route_display_name(route.get("route_short_name"), route.get("route_long_name"), route_id),
                headsign=trip.get("trip_headsign") or departure.get("stop_headsign") or None,
                time=when,
                realtime=realtime,
                direction_id=parse_direction(trip.get("direction_id")),
            ))
    return arrivals
