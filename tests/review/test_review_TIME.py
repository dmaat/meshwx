"""Adversarial review, area TIME: scheduler, alert watch, state.json, timeutil.

Regression tests from the adversarial code review: each test first reproduced one
verified defect (finding id in the comments); all of them are fixed now.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import pytest
from zoneinfo import ZoneInfo

import meshwx.state as state_module
from meshwx.app import App, select_new_alerts
from meshwx.errors import MeshLinkError
from meshwx.formatting import Formatter
from meshwx.mesh import PacedSender
from meshwx.mesh.base import MeshLink
from meshwx.models import Location, OutgoingMessage, WeatherAlert
from meshwx.scheduler import BroadcastScheduler, ScheduleSpec
from meshwx.state import RuntimeSettings
from meshwx.weather.nws import NwsProvider

from conftest import FakeHttp, make_config

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
API = "https://api.weather.gov"
TULSA = Location(36.1540, -95.9928, "Tulsa, OK", zip_code="74103", timezone="America/Chicago")
MOBILE = Location(31.5000, -87.6500, "Monroeville, AL", zip_code="36460", timezone="America/Chicago")


def ids(alerts: Sequence[WeatherAlert]) -> List[str]:
    return [a.id for a in alerts]


def collection(*features: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": [copy.deepcopy(f) for f in features],
            "title": "Current watches, warnings, and advisories"}


def feature(urn: str, **props: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "@id": API + "/alerts/" + urn, "id": urn, "status": "Actual", "category": "Met",
        "certainty": "Likely", "urgency": "Expected", "references": [], "parameters": {},
    }
    base.update(props)
    return {"id": API + "/alerts/" + urn, "type": "Feature", "geometry": None, "properties": base}


class AlertFeed:
    """FakeHttp responder for /alerts/active: serves whatever ``body`` is set to."""

    def __init__(self) -> None:
        self.body: Dict[str, Any] = collection()

    def __call__(self, url: str) -> Dict[str, Any]:
        return self.body


def nws_feed(tmp_path: Any, now_box: List[datetime]) -> Any:
    feed = AlertFeed()
    http = FakeHttp().add_json(API + "/alerts/active", feed)
    provider = NwsProvider(http, make_config({"state_dir": str(tmp_path)}), now_fn=lambda: now_box[0])
    return feed, provider


# ============================================================================ TIME-1
# Real lifecycle (NWS Tulsa, Freeze Warning FZ.W.0002, 2026-11-13/14):
#   10:29 CST  messageType Alert, VTEC NEW, expires 22:30 CST, ends 09:00 CST next day
#   22:59 CST  VTEC CON (same event, "REMAINS IN EFFECT"), sent after the first message expired.
#              api.weather.gov then lists it with messageType "Alert", references [] and the
#              predecessor only in parameters.expiredReferences.
# 128 of 473 active alerts nationwide had "expires" in the past (ends in the future) at capture
# time; every follow-up of such an alert is published this way.

KTSA_NEW = feature(
    "urn:oid:2.49.0.1.840.0.ae42ba47fcaf1510c352217a28d9cec7b70dc6e2.001.1",
    areaDesc="Washington; Pawnee; Tulsa; Rogers; Mayes; Creek; Okmulgee; Wagoner",
    sent="2026-11-13T10:29:00-06:00", effective="2026-11-13T10:29:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-13T22:30:00-06:00", ends="2026-11-14T09:00:00-06:00",
    messageType="Alert", severity="Severe", event="Freeze Warning", senderName="NWS Tulsa OK",
    headline="Freeze Warning issued November 13 at 10:29AM CST until November 14 at 9:00AM CST by NWS Tulsa OK",
    description="* WHAT...Sub-freezing temperatures as low as 28 expected.",
    instruction="Take steps now to protect tender plants from the cold.",
    parameters={"NWSheadline": ["FREEZE WARNING IN EFFECT FROM MIDNIGHT TONIGHT TO 9 AM CST SATURDAY"],
                "VTEC": ["/O.NEW.KTSA.FZ.W.0002.261114T0600Z-261114T1500Z/"]},
)
KTSA_CON = feature(
    "urn:oid:2.49.0.1.840.0.e75bb0e9d5c9909c0de33fc1b82a674691561d13.001.1",
    areaDesc="Washington; Pawnee; Tulsa; Rogers; Mayes; Creek; Okmulgee; Wagoner",
    sent="2026-11-13T22:59:00-06:00", effective="2026-11-13T22:59:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-14T09:00:00-06:00", ends="2026-11-14T09:00:00-06:00",
    messageType="Alert", severity="Severe", event="Freeze Warning", senderName="NWS Tulsa OK",
    headline="Freeze Warning issued November 13 at 10:59PM CST until November 14 at 9:00AM CST by NWS Tulsa OK",
    description="* WHAT...Sub-freezing temperatures as low as 28 expected.",
    instruction="Take steps now to protect tender plants from the cold.",
    references=[],
    parameters={"NWSheadline": ["FREEZE WARNING REMAINS IN EFFECT UNTIL 9 AM CST SATURDAY"],
                "VTEC": ["/O.CON.KTSA.FZ.W.0002.261114T0600Z-261114T1500Z/"],
                "expiredReferences": ["w-nws.webmaster@noaa.gov,urn:oid:2.49.0.1.840.0."
                                      "ae42ba47fcaf1510c352217a28d9cec7b70dc6e2.001.1,2026-11-13T10:29:00-06:00"]},
)

# Real upgrade (NWS Mobile, 2026-11-12/13): Freeze Watch FZ.A.0001 issued 11-12 11:53 (NEW) and
# continued 19:16; at 11-13 09:29 it was upgraded.  The watch's UPG message ("The Freeze Watch has
# been replaced") is listed in /alerts/active until 11-14 08:00 as messageType "Alert" with no
# references (its predecessors only in expiredReferences), next to the new Freeze Warning.
KMOB_WATCH_NEW = feature(
    "urn:oid:2.49.0.1.840.0.5fe072bc9fae4638acf29b75e33faeaf744cbc09.001.1",
    areaDesc="Choctaw; Washington; Clarke; Wilcox; Monroe; Conecuh; Butler",
    sent="2026-11-12T11:53:00-06:00", effective="2026-11-12T11:53:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-12T20:00:00-06:00", ends="2026-11-14T08:00:00-06:00",
    messageType="Alert", severity="Severe", event="Freeze Watch", senderName="NWS Mobile AL",
    headline="Freeze Watch issued November 12 at 11:53AM CST until November 14 at 8:00AM CST by NWS Mobile AL",
    parameters={"VTEC": ["/O.NEW.KMOB.FZ.A.0001.261114T0600Z-261114T1400Z/"]},
)
KMOB_WATCH_CON = feature(
    "urn:oid:2.49.0.1.840.0.1533fa6493dcc73284e810b552e337c43c301d75.001.1",
    areaDesc="Choctaw; Washington; Clarke; Wilcox; Monroe; Conecuh; Butler",
    sent="2026-11-12T19:16:00-06:00", effective="2026-11-12T19:16:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-13T04:00:00-06:00", ends="2026-11-14T08:00:00-06:00",
    messageType="Update", severity="Severe", event="Freeze Watch", senderName="NWS Mobile AL",
    references=[{"@id": API + "/alerts/urn:oid:2.49.0.1.840.0.5fe072bc9fae4638acf29b75e33faeaf744cbc09.001.1",
                 "identifier": "urn:oid:2.49.0.1.840.0.5fe072bc9fae4638acf29b75e33faeaf744cbc09.001.1",
                 "sender": "w-nws.webmaster@noaa.gov", "sent": "2026-11-12T11:53:00-06:00"}],
    parameters={"VTEC": ["/O.CON.KMOB.FZ.A.0001.261114T0600Z-261114T1400Z/"]},
)
KMOB_WATCH_UPG = feature(
    "urn:oid:2.49.0.1.840.0.701e05731d128000b330f70fee77e2784cfa530b.001.1",
    areaDesc="Choctaw; Washington; Clarke; Wilcox; Monroe; Conecuh; Butler",
    sent="2026-11-13T09:29:00-06:00", effective="2026-11-13T09:29:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-13T09:44:57-06:00", ends="2026-11-14T08:00:00-06:00",
    messageType="Alert", severity="Severe", event="Freeze Watch", senderName="NWS Mobile AL",
    headline="The Freeze Watch has been replaced. Please see the latest information from NWS Mobile AL "
             "on this developing situation.",
    references=[],
    parameters={"NWSheadline": ["FREEZE WARNING IN EFFECT FROM MIDNIGHT TONIGHT TO 8 AM CST SATURDAY"],
                "VTEC": ["/O.UPG.KMOB.FZ.A.0001.261114T0600Z-261114T1400Z/"],
                "expiredReferences": [
                    "w-nws.webmaster@noaa.gov,urn:oid:2.49.0.1.840.0.1533fa6493dcc73284e810b552e337c43c301d75"
                    ".001.1,2026-11-12T19:16:00-06:00 w-nws.webmaster@noaa.gov,urn:oid:2.49.0.1.840.0."
                    "5fe072bc9fae4638acf29b75e33faeaf744cbc09.001.1,2026-11-12T11:53:00-06:00"]},
)
KMOB_WARNING_NEW = feature(
    "urn:oid:2.49.0.1.840.0.701e05731d128000b330f70fee77e2784cfa530b.001.2",
    areaDesc="Choctaw; Washington; Clarke; Wilcox; Monroe; Conecuh; Butler",
    sent="2026-11-13T09:29:00-06:00", effective="2026-11-13T09:29:00-06:00",
    onset="2026-11-14T00:00:00-06:00", expires="2026-11-13T17:30:00-06:00", ends="2026-11-14T08:00:00-06:00",
    messageType="Alert", severity="Severe", event="Freeze Warning", senderName="NWS Mobile AL",
    headline="Freeze Warning issued November 13 at 9:29AM CST until November 14 at 8:00AM CST by NWS Mobile AL",
    parameters={"VTEC": ["/O.NEW.KMOB.FZ.W.0003.261114T0600Z-261114T1400Z/"]},
)


def test_continuation_of_an_announced_alert_is_not_broadcast_again(tmp_path):
    now = [datetime(2026, 11, 13, 16, 35, tzinfo=UTC)]  # 10:35 CST: the warning was just issued
    feed, nws = nws_feed(tmp_path, now)
    feed.body = collection(KTSA_NEW)
    send, seen = select_new_alerts(nws.fetch_alerts(TULSA), {}, "Moderate", now[0])
    assert [a.event for a in send] == ["Freeze Warning"]  # announced once - fine

    now[0] = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)  # 23:00 CST: NWS re-issued the SAME warning (CON)
    feed.body = collection(KTSA_CON)
    send, seen = select_new_alerts(nws.fetch_alerts(TULSA), seen, "Moderate", now[0])
    assert send == [], "the continuing Freeze Warning was broadcast a second time, at 23:00"


def test_replaced_watch_is_not_announced_again_when_it_is_upgraded(tmp_path):
    now = [datetime(2026, 11, 12, 18, 0, tzinfo=UTC)]
    feed, nws = nws_feed(tmp_path, now)
    feed.body = collection(KMOB_WATCH_NEW)
    send, seen = select_new_alerts(nws.fetch_alerts(MOBILE), {}, "Moderate", now[0])
    assert [a.event for a in send] == ["Freeze Watch"]
    now[0] = datetime(2026, 11, 13, 1, 20, tzinfo=UTC)
    feed.body = collection(KMOB_WATCH_CON)
    send, seen = select_new_alerts(nws.fetch_alerts(MOBILE), seen, "Moderate", now[0])
    assert send == []  # an ordinary Update is handled correctly

    now[0] = datetime(2026, 11, 13, 15, 31, tzinfo=UTC)  # 09:31 CST: watch upgraded to a warning
    feed.body = collection(KMOB_WATCH_UPG, KMOB_WARNING_NEW)
    send, seen = select_new_alerts(nws.fetch_alerts(MOBILE), seen, "Moderate", now[0])
    assert [a.event for a in send] == ["Freeze Warning"], (
        "the replaced Freeze Watch was announced again: %s" % [(a.event, a.headline) for a in send])


# ============================================================================ TIME-2


class FlakyLink(MeshLink):
    """A radio link whose availability the test switches with ``up``."""

    def __init__(self) -> None:
        super().__init__()
        self.up = True
        self.sent: List[OutgoingMessage] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_connected(self) -> bool:
        return self.up

    def wait_connected(self, timeout: float) -> bool:
        if not self.up:
            time.sleep(min(timeout, 0.02))
        return self.up

    def send(self, msg: OutgoingMessage) -> None:
        if not self.up:
            raise MeshLinkError("radio disconnected")
        self.sent.append(msg)

    def my_node_num(self) -> Optional[int]:
        return 0x12345678


class AlertWeather:
    """Just enough WeatherService for the alert watch."""

    def __init__(self, alerts: List[WeatherAlert]) -> None:
        self.alerts = alerts

    def alerts_supported(self) -> bool:
        return True

    def get_alerts(self):
        return list(self.alerts), "America/New_York"

    def home_location(self) -> Location:
        return Location(42.3576, -71.0684, "Boston, MA", "02108", timezone="America/New_York")

    def invalidate(self) -> None:
        pass


class IdleScheduler:
    next_run_at = None

    def poll(self) -> bool:
        return False

    def reschedule(self) -> None:
        pass


def tornado_warning(alert_id: str = "urn:oid:tor-1") -> WeatherAlert:
    return WeatherAlert(id=alert_id, event="Tornado Warning", severity="Extreme", message_type="Alert",
                        headline="Tornado Warning issued for Suffolk County", instruction="Take cover now.",
                        ends=datetime.now(UTC) + timedelta(hours=1))


def test_alert_queued_just_before_a_radio_outage_is_still_delivered(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path), "broadcast": {"channel_index": 1}})
    link = FlakyLink()
    # max_age_seconds stands in for the default 900 s: the outage simply lasts longer than that.
    sender = PacedSender(link, 0.01, max_age_seconds=0.3, max_bytes=200)
    app = App(cfg, settings=RuntimeSettings(cfg, tmp_path), weather=AlertWeather([tornado_warning()]),
              formatter=Formatter(cfg.display, 200, default_tz=UTC), link=link, sender=sender,
              scheduler=IdleScheduler())
    try:
        app._do_alert_check()  # radio up: the check runs and queues the warning
        link.up = False  # ...and the USB link drops before the sender got to it
        sender.start()
        deadline = time.monotonic() + 5
        while sender.pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sender.pending() == 0  # the outage outlived max_age: the packets were shed
        link.up = True  # radio back; the main loop runs the next alert check
        app._do_alert_check()
        assert sender.wait_idle(5)
        assert any(m.kind == "alert" for m in link.sent), \
            "the Tornado Warning was never transmitted, but state.json says it was"
    finally:
        sender.stop()


def test_alert_still_queued_when_the_service_restarts_is_sent_after_the_restart(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path), "broadcast": {"channel_index": 1}})
    weather = AlertWeather([tornado_warning("urn:oid:tor-1"), tornado_warning("urn:oid:tor-2")])

    def start_service():
        link = FlakyLink()
        sender = PacedSender(link, 8.0, max_bytes=200)  # default pacing: one packet every 8 s
        app = App(cfg, settings=RuntimeSettings(cfg, tmp_path), weather=weather,
                  formatter=Formatter(cfg.display, 200, default_tz=UTC), link=link, sender=sender,
                  scheduler=IdleScheduler())
        return app, link, sender

    app, link, sender = start_service()
    app._do_alert_check()  # two warnings at once: their packets wait for the 8 s pacing
    assert sender.pending() >= 2
    app._shutdown()  # storm knocks the power out / systemd restarts the service: queue discarded
    assert link.sent == []

    app2, link2, sender2 = start_service()  # back up; the warnings are still in effect
    app2._do_alert_check()
    assert sender2.pending() > 0, "the Tornado Warnings are never sent: state.json says they already were"
    sender2.stop()


# ============================================================================ TIME-3


def bump_mtime(path: Any, seconds: float = 2.0) -> None:
    """Make an external write visible regardless of the filesystem's mtime granularity."""
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + seconds))


def test_run_interval_flag_survives_an_unrelated_meshwx_set(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})  # config.yaml: interval 180
    RuntimeSettings(cfg, tmp_path).set_interval(120, set_by="cli")  # weeks ago: `meshwx set interval 120`
    service = RuntimeSettings(cfg, tmp_path, cli_interval=60)  # today: `meshwx run --interval 60`
    assert (service.interval_minutes, service.source("interval_minutes")) == (60, "command line")

    RuntimeSettings(cfg, tmp_path).set_zip("90210", set_by="cli")  # `meshwx set zip 90210`
    bump_mtime(service.path)
    service.refresh()  # the main loop's 30 s poll
    assert service.zip_code == "90210"
    assert service.interval_minutes == 60, (
        "a ZIP change silently switched the interval to the stale saved override (%s, source %r)"
        % (service.interval_minutes, service.source("interval_minutes")))


def test_run_zip_flag_survives_an_unrelated_meshwx_set(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})  # config.yaml: zip_code 02108
    RuntimeSettings(cfg, tmp_path).set_zip("33101", set_by="cli")  # months ago: `meshwx set zip 33101`
    service = RuntimeSettings(cfg, tmp_path, cli_zip="10001")  # today: `meshwx run --zip 10001`
    assert (service.zip_code, service.source("zip_code")) == ("10001", "command line")

    RuntimeSettings(cfg, tmp_path).set_interval(60, set_by="cli")  # `meshwx set interval 60`
    bump_mtime(service.path)
    service.refresh()
    assert service.interval_minutes == 60
    assert service.zip_code == "10001", (
        "an interval change silently moved weather and alerts to the stale saved ZIP %s" % service.zip_code)


def test_meshwx_set_back_to_the_config_value_reaches_a_service_started_with_a_flag(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path)})  # config.yaml: interval 180
    service = RuntimeSettings(cfg, tmp_path, cli_interval=60)  # `meshwx run --interval 60`
    service.set_data("seen_alerts", {})  # the service has written state.json at some point
    changed: List[Any] = []
    service.add_listener(lambda key, value: changed.append((key, value)))

    RuntimeSettings(cfg, tmp_path).set_interval(180, set_by="cli")  # `meshwx set interval 180`
    bump_mtime(service.path)
    service.refresh()  # "A running meshwx service applies this within ~30 s."
    assert service.interval_minutes == 180, "the running service ignored the change (still %d, %r)" % (
        service.interval_minutes, service.source("interval_minutes"))
    assert ("interval_minutes", 180) in changed


# ============================================================================ TIME-4


def state_doc(path: Any) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_change_written_while_the_service_reloads_is_not_lost(tmp_path, monkeypatch):
    cfg = make_config({"state_dir": str(tmp_path)})
    service = RuntimeSettings(cfg, tmp_path)
    service.set_data("seen_alerts", {})
    RuntimeSettings(cfg, tmp_path).set_zip("90210", set_by="cli")  # first `meshwx set`
    bump_mtime(service.path)

    real_read = state_module.read_json
    raced: List[bool] = []

    def read_then_other_process_writes(path: Any, default: Any = None) -> Any:
        doc = real_read(path, default)
        if not raced:  # a second `meshwx set` replaces the file right after the service read it
            raced.append(True)
            RuntimeSettings(cfg, tmp_path).set_interval(60, set_by="cli")
            bump_mtime(path, 4.0)
        return doc

    monkeypatch.setattr(state_module, "read_json", read_then_other_process_writes)
    service.refresh()  # picks up the ZIP; the interval write lands between read_json and os.stat
    monkeypatch.setattr(state_module, "read_json", real_read)
    assert service.zip_code == "90210"
    service.refresh()  # 30 s later
    service.set_data("seen_alerts", {"x": {"event": "Flood Warning", "ends": None, "seen_at": "2026-11-14T00:00:00"}})
    assert service.interval_minutes == 60, "the service never saw `meshwx set interval 60`"
    assert state_doc(service.path)["overrides"].get("interval_minutes", {}).get("value") == 60, \
        "the service's next write erased the CLI's saved interval"


def test_meshwx_set_during_a_slow_service_write_is_not_lost(tmp_path, monkeypatch):
    cfg = make_config({"state_dir": str(tmp_path)})
    service = RuntimeSettings(cfg, tmp_path)
    service.set_data("seen_alerts", {})
    real_write = state_module.atomic_write_json
    writing = threading.Event()

    def slow_sd_card_write(path: Any, data: Any) -> None:
        if threading.current_thread().name == "service-worker":
            writing.set()
            time.sleep(0.5)  # fsync on a busy SD card
        real_write(path, data)

    monkeypatch.setattr(state_module, "atomic_write_json", slow_sd_card_write)
    seen = {"urn:oid:tor-1": {"event": "Tornado Warning", "ends": "2026-11-14T20:00:00+00:00",
                              "seen_at": "2026-11-14T19:00:00+00:00"}}
    worker = threading.Thread(target=service.set_data, args=("seen_alerts", seen), name="service-worker")
    worker.start()
    assert writing.wait(5)
    RuntimeSettings(cfg, tmp_path).set_interval(60, set_by="cli")  # `meshwx set interval 60` meanwhile
    worker.join(10)
    doc = state_doc(service.path)
    assert "urn:oid:tor-1" in doc["data"]["seen_alerts"]
    assert doc["overrides"].get("interval_minutes", {}).get("value") == 60, \
        "`meshwx set interval 60` reported success but the service's write erased it"


def test_alert_recorded_while_meshwx_set_writes_is_not_forgotten(tmp_path, monkeypatch):
    cfg = make_config({"state_dir": str(tmp_path)})
    service = RuntimeSettings(cfg, tmp_path)
    service.set_data("seen_alerts", {})
    real_write = state_module.atomic_write_json
    writing = threading.Event()

    def slow_sd_card_write(path: Any, data: Any) -> None:
        if threading.current_thread().name == "meshwx-set":
            writing.set()
            time.sleep(0.5)  # fsync on a busy SD card
        real_write(path, data)

    monkeypatch.setattr(state_module, "atomic_write_json", slow_sd_card_write)
    cli = threading.Thread(target=lambda: RuntimeSettings(cfg, tmp_path).set_interval(60, set_by="cli"),
                           name="meshwx-set")  # `meshwx set interval 60`, read-modify-write in progress
    cli.start()
    assert writing.wait(5)
    seen = {"urn:oid:tor-1": {"event": "Tornado Warning", "ends": "2026-11-14T20:00:00+00:00",
                              "seen_at": "2026-11-14T19:00:00+00:00"}}
    service.set_data("seen_alerts", seen)  # the alert check just queued the Tornado Warning
    cli.join(10)
    bump_mtime(service.path)
    service.refresh()  # the main loop's 30 s poll picks up the CLI's file
    assert service.interval_minutes == 60
    assert "urn:oid:tor-1" in (service.get_data("seen_alerts") or {}), \
        "the service forgot the Tornado Warning it announced: the next alert check broadcasts it again"


# ============================================================================ TIME-5


def run_scheduler(spec: ScheduleSpec, start: datetime, end: datetime, step: timedelta) -> List[datetime]:
    """Poll a scheduler on a fake wall clock from ``start`` to ``end``; return the times it fired."""
    now = [start]
    sched = BroadcastScheduler(lambda: spec, now_fn=lambda: now[0])
    fired: List[datetime] = []
    while now[0] < end:
        if sched.poll():
            fired.append(now[0])
        now[0] += step
    return fired


def test_weekly_broadcast_comes_back_after_dst_pushed_slots_into_quiet_hours():
    # README: interval "15-10080 (one week)"; quiet hours as in the README example.  Not clock-aligned
    # (1440 % 10080 != 0), so slots are every 7 days from the service start: Saturday 21:29 EST.
    spec = ScheduleSpec(10080, quiet_start="22:00", quiet_end="06:00")
    fired = run_scheduler(spec, datetime(2026, 2, 28, 21, 29, tzinfo=NY), datetime(2026, 11, 22, tzinfo=NY),
                          timedelta(minutes=5))
    assert fired and fired[0].date().isoformat() == "2026-03-07"  # first weekly slot - fine
    # From March 8 (EDT) the slots fall at 22:29 local, inside the quiet hours, and are skipped.  From
    # November 1 (EST again) they are back at 21:29 (Nov 7, 14, 21) - but the scheduler has given up.
    november = [t for t in fired if t >= datetime(2026, 11, 1, tzinfo=NY)]
    assert november, "no scheduled broadcast after 2026-03-07: the scheduler stopped for good (%s)" % fired


def test_skipping_quiet_slots_is_not_mistaken_for_a_clock_set_back(caplog):
    spec = ScheduleSpec(1500, quiet_start="22:00", quiet_end="03:00")  # every 25 h, not clock-aligned
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        fired = run_scheduler(spec, datetime(2026, 6, 1, 20, 59, tzinfo=NY), datetime(2026, 6, 4, tzinfo=NY),
                              timedelta(seconds=10))
    assert [t.strftime("%m-%d %H:%M") for t in fired] == ["06-02 21:59"]
    bogus = [r for r in caplog.records if "implausibly far" in r.getMessage()]
    assert not bogus, ("%d 'clock set back?' reschedules (one per main-loop tick) although the clock only "
                       "moved forward" % len(bogus))
