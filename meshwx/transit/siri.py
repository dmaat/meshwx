"""SIRI StopMonitoring provider (511.org Bay Area, MTA Bus Time NYC, ...), JSON flavour.

Request: ``GET <siri.url>`` with ``{key_param: api_key, stop_param: stop id, **params}``,
e.g. 511: ``?api_key=K&stopcode=15553&agency=SF&format=json`` (511 answers XML
without ``format=json``); MTA Bus Time:
``https://bustime.mta.info/api/siri/stop-monitoring.json?key=K&MonitoringRef=308209&version=2``.

Parsed defensively, because the deployments differ:

* MTA wraps the document in ``{"Siri": {...}}``, 511 does not;
* ``StopMonitoringDelivery`` and ``MonitoredStopVisit`` are an object or a list;
* ``PublishedLineName`` / ``DestinationName`` are a string, a list of strings
  (SIRI 2.0, MTA ``version=2``) or ``{"value": ...}`` objects.

Per ``MonitoredStopVisit.MonitoredVehicleJourney``:

* time: ``MonitoredCall.ExpectedArrivalTime`` or ``ExpectedDepartureTime``
  (``realtime=True``), else ``AimedArrivalTime`` / ``AimedDepartureTime``
  (``realtime=False``); expected departures in the past are dropped, a
  vehicle standing at the stop counts as arriving now, or at its departure
  when it waits longer (:func:`~meshwx.transit.visit_time`);
* hidden: ``Cancellation`` journeys, calls whose arrival/departure status is
  ``cancelled``, and calls without boarding (``DepartureBoardingActivity``
  ``noBoarding`` / ``passThru``);
* ``route_id`` = ``LineRef``; route name = ``LineRef`` when it is a short code
  (511: "14", "N"), else ``PublishedLineName`` (MTA: "MTA NYCT_M15" -> "M15");
* headsign: ``MonitoredCall.DestinationDisplay``, else ``DestinationName``;
* ``ServiceDelivery.ResponseTimestamp`` is the server clock for
  :class:`~meshwx.transit.ClockSkew`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import TransitConfig
from ..errors import TransitError
from ..httpclient import HttpClient, HttpError
from ..models import Arrival, StopArrivals, TransitStop
from . import (RATE_LIMIT_MESSAGE, UNREADABLE_MESSAGE, ClockSkew, arrivals_per_stop, http_failure, parse_direction,
               parse_time, route_display_name, visit_time)
from .base import TransitProvider

log = logging.getLogger(__name__)

_NO_BOARDING = frozenset({"noboarding", "passthru"})


class SiriProvider(TransitProvider):
    """Arrivals from a SIRI StopMonitoring JSON endpoint (see the module docstring)."""

    name = "siri"

    def __init__(self, cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> None:
        self._cfg = cfg.siri
        self._http = http
        self._skew = ClockSkew("SIRI")

    def get_arrivals(self, stops: Sequence[TransitStop], now: datetime,
                     horizon_minutes: int) -> List[StopArrivals]:
        return arrivals_per_stop(stops, lambda stop: self._stop_arrivals(stop, now), now, horizon_minutes)

    def _stop_arrivals(self, stop: TransitStop, now: datetime) -> List[Arrival]:
        params = {self._cfg.key_param: self._cfg.api_key, self._cfg.stop_param: stop.id}
        params.update(self._cfg.params)
        try:
            doc = self._http.get_json(self._cfg.url, params=params)
        except HttpError as exc:
            if exc.status is not None and 200 <= exc.status < 300:
                raise TransitError("SIRI response is not JSON (511.org needs transit.siri.params format: json)",
                                   user_message=UNREADABLE_MESSAGE) from exc
            raise http_failure(exc, "SIRI", RATE_LIMIT_MESSAGE) from exc
        return parse_stop_monitoring(doc, stop, now, self._skew)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str:
    """A SIRI text value (string, list of strings, ``{"value": ...}``) -> its first non-empty string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        return _text(value.get("value"))
    for item in value if isinstance(value, list) else ():
        text = _text(item)
        if text:
            return text
    return ""


def _shifted(value: Any, offset: timedelta) -> Optional[datetime]:
    """ISO timestamp -> aware UTC datetime moved onto the local clock; None when absent."""
    parsed = parse_time(value)
    return parsed + offset if parsed is not None else None


def _error_text(condition: Any) -> str:
    if isinstance(condition, dict):
        other = condition.get("OtherError")
        return (_text(condition.get("Description")) or _text(other.get("ErrorText") if isinstance(other, dict) else None)
                or "unknown error")
    return _text(condition) or "unknown error"


def _hidden(journey: Dict[str, Any], call: Dict[str, Any]) -> bool:
    """Cancelled journey/call, or a call where nobody can board."""
    if str(journey.get("Cancellation")).lower() == "true":  # JSON true or "true"
        return True
    if "cancelled" in (str(call.get("ArrivalStatus") or "").lower(), str(call.get("DepartureStatus") or "").lower()):
        return True
    return str(call.get("DepartureBoardingActivity") or "").lower() in _NO_BOARDING


def parse_stop_monitoring(doc: Any, stop: TransitStop, now: datetime, skew: ClockSkew) -> List[Arrival]:
    """Shown arrivals of one StopMonitoring response, shifted onto the local clock (unsorted)."""
    root = doc.get("Siri", doc) if isinstance(doc, dict) else None
    service = root.get("ServiceDelivery") if isinstance(root, dict) else None
    if not isinstance(service, dict):
        raise TransitError("SIRI: response has no ServiceDelivery", user_message=UNREADABLE_MESSAGE)
    server_now = parse_time(service.get("ResponseTimestamp"))
    visits: List[Dict[str, Any]] = []
    errors: List[str] = []
    for delivery in _as_list(service.get("StopMonitoringDelivery")):
        if not isinstance(delivery, dict):
            continue
        server_now = server_now or parse_time(delivery.get("ResponseTimestamp"))
        visits.extend(v for v in _as_list(delivery.get("MonitoredStopVisit")) if isinstance(v, dict))
        if delivery.get("ErrorCondition"):
            errors.append(_error_text(delivery["ErrorCondition"]))
    if errors and not visits:
        raise TransitError("SIRI error: %s" % "; ".join(errors)[:200])
    offset: timedelta = skew.offset(now, server_now)
    arrivals: List[Arrival] = []
    for visit in visits:
        journey = visit.get("MonitoredVehicleJourney")
        call = journey.get("MonitoredCall") if isinstance(journey, dict) else None
        if not isinstance(call, dict) or _hidden(journey, call):  # type: ignore[arg-type]
            continue
        arrival = _shifted(call.get("ExpectedArrivalTime"), offset)
        departure = _shifted(call.get("ExpectedDepartureTime"), offset)
        realtime = arrival is not None or departure is not None
        if not realtime:
            arrival = _shifted(call.get("AimedArrivalTime"), offset)
            departure = _shifted(call.get("AimedDepartureTime"), offset)
        if realtime and departure is not None and departure < now:
            continue  # already departed
        when = visit_time(arrival, departure, now)
        if when is None:
            continue
        line_ref = _text(journey.get("LineRef"))
        published = _text(journey.get("PublishedLineName"))
        arrivals.append(Arrival(
            stop_id=_text(call.get("StopPointRef")) or _text(visit.get("MonitoringRef")) or stop.id,
            route_id=line_ref or published,
            route_name=route_display_name(None, published, line_ref),
            headsign=_text(call.get("DestinationDisplay")) or _text(journey.get("DestinationName")) or None,
            time=when,
            realtime=realtime,
            direction_id=parse_direction(_text(journey.get("DirectionRef"))),
        ))
    return arrivals
