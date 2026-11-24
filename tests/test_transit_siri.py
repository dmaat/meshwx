"""Tests for meshwx.transit.siri.

No live capture exists (511.org and MTA Bus Time need keys), so the documents
below are synthetic but follow the field paths of both deployments:

* 511.org: ``{"ServiceDelivery": {"StopMonitoringDelivery": {...}}}`` (object),
  plain-string ``PublishedLineName`` ("MISSION") with a short ``LineRef`` ("14");
* MTA Bus Time: ``{"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [...]}}}``
  (list), SIRI 2.0 list values (``"PublishedLineName": ["M15-SBS"]``) and a long
  ``LineRef`` ("MTA NYCT_M15+"), local-offset timestamps with milliseconds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from meshwx.errors import TransitError
from meshwx.models import TransitStop
from meshwx.transit.siri import SiriProvider

from conftest import FakeHttp, make_config

URL_511 = "https://api.511.org/transit/StopMonitoring"
URL_MTA = "https://bustime.mta.info/api/siri/stop-monitoring.json"
NOW = datetime(2026, 11, 13, 15, 30, 0, tzinfo=timezone.utc)
EST = timezone(timedelta(hours=-5))


def ts(minutes: float, tz: timezone = timezone.utc) -> str:
    when = (NOW + timedelta(minutes=minutes)).astimezone(tz)
    if tz is timezone.utc:
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return when.isoformat(timespec="milliseconds")


def call(*, ea: Optional[float] = None, aa: Optional[float] = None, ed: Optional[float] = None,
         ad: Optional[float] = None, display: Optional[str] = None, stop_ref: str = "15553",
         **extra: Any) -> Dict[str, Any]:
    c = {"StopPointRef": stop_ref, "StopPointName": "Mission St & 16th St", "VehicleAtStop": "",
         "DestinationDisplay": display,
         "AimedArrivalTime": ts(aa) if aa is not None else None,
         "ExpectedArrivalTime": ts(ea) if ea is not None else None,
         "AimedDepartureTime": ts(ad) if ad is not None else None,
         "ExpectedDepartureTime": ts(ed) if ed is not None else None}
    c.update(extra)
    return c


def visit_511(line: str, published: str, destination: str, monitored_call: Dict[str, Any],
              direction: str = "IB", **journey: Any) -> Dict[str, Any]:
    mvj = {"LineRef": line, "DirectionRef": direction,
           "FramedVehicleJourneyRef": {"DataFrameRef": "2026-11-13", "DatedVehicleJourneyRef": "11640140"},
           "PublishedLineName": published, "OperatorRef": "SF", "OriginRef": "13311",
           "OriginName": "Mission St & Silver Ave", "DestinationRef": "13530", "DestinationName": destination,
           "Monitored": True, "VehicleLocation": {"Longitude": "-122.419", "Latitude": "37.765"},
           "Bearing": "45.0", "Occupancy": "seatsAvailable", "VehicleRef": "8701", "MonitoredCall": monitored_call}
    mvj.update(journey)
    return {"RecordedAtTime": ts(-1), "MonitoringRef": "15553", "MonitoredVehicleJourney": mvj}


def doc_511(visits: Any, response_ts: Optional[str] = None) -> Dict[str, Any]:
    stamp = response_ts or ts(0)
    return {"ServiceDelivery": {"ResponseTimestamp": stamp, "ProducerRef": "SF", "Status": True,
                                "StopMonitoringDelivery": {"version": "1.4", "ResponseTimestamp": stamp,
                                                           "Status": True, "MonitoredStopVisit": visits}}}


def visit_mta(minutes_arrival: Optional[float], minutes_departure: Optional[float], *,
              line: str = "MTA NYCT_M15+", published: Any = ("M15-SBS",), destination: Any = ("SOUTH FERRY",),
              direction: str = "1") -> Dict[str, Any]:
    monitored = {"ExpectedArrivalTime": ts(minutes_arrival, EST) if minutes_arrival is not None else None,
                 "ExpectedDepartureTime": ts(minutes_departure, EST) if minutes_departure is not None else None,
                 "DistanceFromStop": 1234, "NumberOfStopsAway": 3, "StopPointRef": "MTA_401906",
                 "VisitNumber": 1, "StopPointName": ["1 AV/E 14 ST"], "ArrivalProximityText": "3 stops away"}
    return {"MonitoredVehicleJourney": {
        "LineRef": line, "DirectionRef": direction, "PublishedLineName": list(published),
        "OperatorRef": "MTA NYCT", "DestinationRef": "MTA_803180", "DestinationName": list(destination),
        "SituationRef": [], "Monitored": True, "VehicleRef": "MTA NYCT_6066", "MonitoredCall": monitored,
        "OnwardCalls": {}}, "RecordedAtTime": ts(-0.5, EST)}


def doc_mta(visits: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"Siri": {"ServiceDelivery": {
        "ResponseTimestamp": ts(0, EST),
        "StopMonitoringDelivery": [{"MonitoredStopVisit": visits, "ResponseTimestamp": ts(0, EST),
                                    "ValidUntil": ts(1, EST)}],
        "SituationExchangeDelivery": []}}}


def provider(http: FakeHttp, **siri: Any) -> SiriProvider:
    cfg = {"api_key": "K511", "params": {"agency": "SF", "format": "json"}}
    cfg.update(siri)
    config = make_config({"transit": {"enabled": True, "provider": "siri", "stops": ["15553"], "siri": cfg}})
    return SiriProvider(config.transit, http, Path())


def arrivals_of(doc: Any, stop: TransitStop = TransitStop("15553", "Mission/16th"), now: datetime = NOW,
                horizon: int = 90):
    [result] = provider(FakeHttp().add_json(URL_511, doc)).get_arrivals([stop], now, horizon)
    return result.arrivals


def minutes(arrival: Any, now: datetime = NOW) -> float:
    return round((arrival.time - now).total_seconds() / 60, 2)


# ============================================================================ parsing


def test_511_style_document():
    doc = doc_511([
        visit_511("14", "MISSION", "Daly City", call(ea=4, aa=3, ed=4.5, ad=3.5)),
        visit_511("49", "VAN NESS-MISSION", "Ft Mason", call(aa=9, ad=9)),  # not monitored: schedule only
        visit_511("14R", "MISSION RAPID", "Downtown", call(ed=6, display="Salesforce Transit Ctr")),
        visit_511("N", "JUDAH", "Ocean Beach", call(ea=2), direction="OB"),
    ])
    arrivals = arrivals_of(doc)
    # route name: the short LineRef ("14"), not 511's PublishedLineName ("MISSION")
    assert [(a.route_id, a.route_name, a.headsign, minutes(a), a.realtime) for a in arrivals] == [
        ("N", "N", "Ocean Beach", 2.0, True),
        ("14", "14", "Daly City", 4.0, True),  # the expected arrival, not the departure
        ("14R", "14R", "Salesforce Transit Ctr", 6.0, True),  # DestinationDisplay beats DestinationName
        ("49", "49", "Ft Mason", 9.0, False),  # aimed times only
    ]
    assert {a.stop_id for a in arrivals} == {"15553"}
    assert {a.direction_id for a in arrivals} == {None}  # IB/OB are not GTFS direction ids
    assert all(a.time.utcoffset() == timedelta(0) for a in arrivals)


def test_mta_style_wrapped_document_with_lists():
    doc = doc_mta([visit_mta(5, 5.5), visit_mta(None, 12, line="MTA NYCT_M14A+", published=["M14A-SBS"],
                                                     destination=["LOWER EAST SIDE GRAND ST"], direction="0")])
    http = FakeHttp().add_json(URL_MTA, doc)
    p = provider(http, url=URL_MTA, key_param="key", stop_param="MonitoringRef", params={"version": "2"})
    [result] = p.get_arrivals([TransitStop("401906", "1 Av/14 St")], NOW, 90)
    assert [(a.route_id, a.route_name, a.headsign, a.direction_id, a.stop_id, minutes(a), a.realtime)
            for a in result.arrivals] == [
        ("MTA NYCT_M15+", "M15-SBS", "SOUTH FERRY", 1, "MTA_401906", 5.0, True),
        ("MTA NYCT_M14A+", "M14A-SBS", "LOWER EAST SIDE GRAND ST", 0, "MTA_401906", 12.0, True),
    ]
    assert parse_qs(urlsplit(http.calls[0][0]).query) == {"key": ["K511"], "MonitoringRef": ["401906"],
                                                          "version": ["2"]}


def test_single_visit_object_and_value_objects():
    visit = visit_511("MTA NYCT_B63", "B63", "BAY RIDGE", call(ea=7))
    visit["MonitoredVehicleJourney"]["PublishedLineName"] = [{"value": "B63", "lang": "EN"}]
    visit["MonitoredVehicleJourney"]["DestinationName"] = {"value": "BAY RIDGE"}
    [arrival] = arrivals_of(doc_511(visit))  # MonitoredStopVisit is an object, not a list
    assert (arrival.route_name, arrival.headsign, minutes(arrival)) == ("B63", "BAY RIDGE", 7.0)


def test_stop_monitoring_delivery_list_merges_visits():
    doc = doc_511([])
    single = doc["ServiceDelivery"]["StopMonitoringDelivery"]
    doc["ServiceDelivery"]["StopMonitoringDelivery"] = [
        dict(single, MonitoredStopVisit=[visit_511("14", "MISSION", "Daly City", call(ea=3))]),
        dict(single, MonitoredStopVisit=visit_511("49", "VAN NESS", "Ft Mason", call(ea=1))),
    ]
    assert [a.route_name for a in arrivals_of(doc)] == ["49", "14"]


def test_hidden_visits():
    doc = doc_511([
        visit_511("14", "MISSION", "Daly City", call(ea=3), Cancellation=True),
        visit_511("14", "MISSION", "Daly City", call(ea=4, DepartureStatus="cancelled")),
        visit_511("14", "MISSION", "Daly City", call(ea=5, ArrivalStatus="cancelled")),
        visit_511("14", "MISSION", "Daly City", call(ea=6, DepartureBoardingActivity="noBoarding")),
        visit_511("14", "MISSION", "Daly City", call(ea=7, DepartureBoardingActivity="passThru")),
        visit_511("14", "MISSION", "Daly City", call(ea=-3, ed=-1)),  # already departed
        visit_511("14", "MISSION", "Daly City", call(ea=-0.5, ed=0.5)),  # at the stop: shown
        visit_511("14", "MISSION", "Daly City", call(ea=-2.5, ed=1)),  # long dwell at the stop: shown as now
        visit_511("14", "MISSION", "Daly City", call(aa=-2)),  # scheduled 2 min ago: beyond the 60 s grace
        visit_511("14", "MISSION", "Daly City", call()),  # no times at all
        visit_511("14", "MISSION", "Daly City", call(ea=8, DepartureBoardingActivity="boarding")),
        {"MonitoredVehicleJourney": None},  # malformed visit: skipped
    ])
    assert [minutes(a) for a in arrivals_of(doc)] == [-0.5, 0.0, 8.0]


def test_route_and_direction_filters():
    doc = doc_mta([visit_mta(5, 5), visit_mta(6, 6, line="MTA NYCT_M14A+", published=["M14A-SBS"], direction="0")])
    http = FakeHttp().add_json(URL_MTA, doc)
    p = provider(http, url=URL_MTA)
    by_name = p.get_arrivals([TransitStop("401906", "x", ("m15-sbs",))], NOW, 90)[0]
    by_dir = p.get_arrivals([TransitStop("401906", "x", (), 0)], NOW, 90)[0]
    assert [a.route_name for a in by_name.arrivals] == ["M15-SBS"]
    assert [a.route_name for a in by_dir.arrivals] == ["M14A-SBS"]


# ============================================================================ request, clock, errors


def test_511_request_params():
    http = FakeHttp().add_json(URL_511, doc_511([]))
    [result] = provider(http).get_arrivals([TransitStop("15553", "Mission/16th")], NOW, 90)
    assert result.arrivals == [] and result.error is None
    [(url, headers)] = http.calls
    assert headers == {}
    assert parse_qs(urlsplit(url).query) == {"api_key": ["K511"], "stopcode": ["15553"], "agency": ["SF"],
                                             "format": ["json"]}


def test_response_timestamp_corrects_clock_skew(caplog):
    server_now = NOW - timedelta(minutes=5)  # the Pi's clock is 5 minutes fast
    doc = doc_511([visit_511("14", "MISSION", "Daly City", call(ea=4 - 5))], response_ts=ts(-5))
    with caplog.at_level(logging.WARNING, logger="meshwx.transit"):
        [arrival] = arrivals_of(doc, now=NOW)
    assert minutes(arrival) == 4.0  # 4 minutes after the server's "now"
    assert arrival.time == server_now + timedelta(minutes=4) + (NOW - server_now)
    assert "local clock is +300 s off the SIRI server clock" in caplog.text


def test_error_condition():
    doc = doc_511([])
    delivery = doc["ServiceDelivery"]["StopMonitoringDelivery"]
    delivery["ErrorCondition"] = {"OtherError": {"ErrorText": "No such stop: MTA_999"},
                                  "Description": "No such stop: MTA_999"}
    with pytest.raises(TransitError) as ei:
        arrivals_of(doc)
    assert "No such stop: MTA_999" in str(ei.value)
    assert ei.value.user_message == "Transit unavailable, try later"
    delivery["MonitoredStopVisit"] = [visit_511("14", "MISSION", "Daly City", call(ea=3))]
    assert len(arrivals_of(doc)) == 1  # visits win over a partial error


@pytest.mark.parametrize("doc", [{"Siri": {}}, {"foo": 1}, [1, 2]])
def test_no_service_delivery(doc):
    with pytest.raises(TransitError) as ei:
        arrivals_of(doc)
    assert ei.value.user_message == "Transit feed unreadable"


def test_xml_answer_hints_at_format_json():
    with pytest.raises(TransitError) as ei:
        provider(FakeHttp().add_error(URL_511, 200)).get_arrivals([TransitStop("15553", "x")], NOW, 90)
    assert ei.value.user_message == "Transit feed unreadable"
    assert "format: json" in str(ei.value)


@pytest.mark.parametrize("status, message", [
    (401, "SIRI API key rejected"),
    (429, "Rate limited, try later"),
    (500, "Transit unavailable, try later"),
])
def test_http_errors(status, message):
    with pytest.raises(TransitError) as ei:
        provider(FakeHttp().add_error(URL_511, status)).get_arrivals([TransitStop("15553", "x")], NOW, 90)
    assert ei.value.user_message == message
