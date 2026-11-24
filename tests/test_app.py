"""Tests for meshwx.app: the running service (threads, broadcasts, alert watch, shutdown).

The App runs for real with a ConsoleLink writing to a StringIO, a fast
PacedSender, the real CommandBot/Formatter/RuntimeSettings, stub weather and
transit services, a stub scheduler and a fake monotonic clock.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

import meshwx.app as app_module
from meshwx import __version__
from meshwx.app import App, select_new_alerts, severity_rank
from meshwx.errors import ConfigError, TransitError, WeatherError
from meshwx.formatting import Formatter
from meshwx.mesh import ConsoleLink, PacedSender
from meshwx.mesh.console_link import MY_NODE_NUM
from meshwx.models import (
    BROADCAST,
    Arrival,
    CurrentConditions,
    ForecastPeriod,
    IncomingMessage,
    Location,
    OutgoingMessage,
    StopArrivals,
    TransitStop,
    WeatherAlert,
    WeatherReport,
    node_id_from_num,
)
from meshwx.state import RuntimeSettings
from meshwx.timeutil import utcnow

from conftest import FakeHttp, make_config

USER = 0x11223344
ADMIN = 0x000000AA
ADMIN_ID = "!000000aa"
TZ = "America/New_York"
BOSTON = Location(42.3576, -71.0684, "Boston, MA", "02108", timezone=TZ)
STOPS = [TransitStop("place-pktrm", "Park St")]
NOW = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
BARRIER_IDS = 90000  # packet ids of the ping DMs Harness.idle() uses as a barrier

STATUS_KEYS = {"version", "zip_code", "zip_source", "location", "interval_minutes", "interval_source", "times",
               "next_broadcast", "broadcast_enabled", "channel_index", "link", "link_desc", "queue", "uptime_s",
               "alerts_active", "transit_enabled"}


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


# ============================================================================ stubs


class StubWeather:
    """WeatherService stand-in (docs/CONTRACTS.md shape) with call counters."""

    def __init__(self, *, alerts_supported: bool = True) -> None:
        self.location = BOSTON
        self.report = WeatherReport(
            location=BOSTON, provider="nws", fetched_at=utcnow(), timezone=TZ,
            current=CurrentConditions(description="Cloudy", temperature_c=10.0),
            forecast=[ForecastPeriod(name="Tonight", is_daytime=False, temp_low_c=4.0, description="Rain")],
            alerts_supported=alerts_supported)
        self.alerts: List[WeatherAlert] = []
        self.fail: Optional[BaseException] = None
        self.alert_fail: Optional[BaseException] = None
        self.block: Optional[threading.Event] = None  # get_report waits for it when set
        self.supported = alerts_supported
        self.report_calls = 0
        self.alert_calls = 0
        self.invalidated = 0

    def home_location(self) -> Location:
        return self.location

    def location_for_zip(self, zip_code: str) -> Location:
        return self.location

    def get_report(self, zip_code: Optional[str] = None, *, force: bool = False) -> WeatherReport:
        self.report_calls += 1
        if self.block is not None:
            self.block.wait(5)
        if self.fail is not None:
            raise self.fail
        return self.report

    def get_alerts(self):
        self.alert_calls += 1
        if self.alert_fail is not None:
            raise self.alert_fail
        return list(self.alerts), TZ

    def alerts_supported(self) -> bool:
        return self.supported

    def invalidate(self) -> None:
        self.invalidated += 1


class StubTransit:
    def __init__(self) -> None:
        self.fail: Optional[BaseException] = None
        self.warmed = threading.Event()

    @property
    def stops(self) -> List[TransitStop]:
        return list(STOPS)

    def find_stop(self, query: str) -> Optional[TransitStop]:
        return STOPS[0]

    def get_arrivals(self, stops: Optional[Sequence[TransitStop]] = None,
                     now: Optional[datetime] = None) -> List[StopArrivals]:
        if self.fail is not None:
            raise self.fail
        now = now or utcnow()
        return [StopArrivals(stop=s, arrivals=[Arrival(s.id, "Red", "Red", "Alewife", now + timedelta(minutes=5))])
                for s in (stops or STOPS)]

    def warm_up(self) -> None:
        self.warmed.set()


class StubScheduler:
    """BroadcastScheduler stand-in: ``due`` makes the next poll() return True once."""

    def __init__(self) -> None:
        self.due = False
        self.polls = 0
        self.reschedules = 0
        self.next_run_at: Optional[datetime] = NOW

    def poll(self) -> bool:
        self.polls += 1
        due, self.due = self.due, False
        return due

    def reschedule(self) -> None:
        self.reschedules += 1


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingLink(ConsoleLink):
    """ConsoleLink (printing to a StringIO) that also records sent packets; ``down`` fakes an outage."""

    def __init__(self, events: List[str]) -> None:
        self.out = io.StringIO()
        super().__init__(200, output_stream=self.out, read_input=False)
        self.events = events
        self.sent: List[OutgoingMessage] = []
        self.down = False
        self.sigint_handler_at_start: Any = None

    def start(self) -> None:
        self.sigint_handler_at_start = signal.getsignal(signal.SIGINT)
        super().start()

    def stop(self) -> None:
        self.events.append("link.stop")
        super().stop()

    def is_connected(self) -> bool:
        return not self.down and super().is_connected()

    def send(self, msg: OutgoingMessage) -> None:
        super().send(msg)
        self.sent.append(msg)


class RecordingSender(PacedSender):
    def __init__(self, link: RecordingLink, events: List[str]) -> None:
        super().__init__(link, 0.01, max_bytes=200)
        self._events = events
        self._rec_link = link

    def stop(self, timeout: float = 5.0) -> None:
        self._events.append("sender.stop (on_message=%s)" % self._rec_link.on_message)
        super().stop(timeout)


class StubHttp:
    def __init__(self, events: List[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("http.close")


class Harness:
    """An App on stubs; ``start()`` runs it in a thread until ``stop()``."""

    def __init__(self, tmp_path: Any, overrides: Optional[Dict[str, Any]] = None, *, transit: bool = False,
                 alerts_supported: bool = True) -> None:
        base: Dict[str, Any] = {"state_dir": str(tmp_path), "commands": {"cooldown_seconds": 0, "admins": [ADMIN_ID]},
                                "broadcast": {"channel_index": 1}}
        for key, value in (overrides or {}).items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = dict(base[key], **value)
            else:
                base[key] = value
        self.cfg = make_config(base)
        self.events: List[str] = []
        self.settings = RuntimeSettings(self.cfg, tmp_path)
        self.weather = StubWeather(alerts_supported=alerts_supported)
        self.transit = StubTransit() if transit else None
        self.link = RecordingLink(self.events)
        self.sender = RecordingSender(self.link, self.events)
        self.scheduler = StubScheduler()
        self.clock = FakeClock()
        self.formatter = Formatter(self.cfg.display, 200, default_tz=timezone.utc, command_prefix="!")
        self.app = App(self.cfg, settings=self.settings, weather=self.weather, formatter=self.formatter,
                       link=self.link, sender=self.sender, scheduler=self.scheduler, transit=self.transit,
                       http=StubHttp(self.events), clock=self.clock, tick_seconds=0.005)
        self.thread: Optional[threading.Thread] = None
        self.rc: Optional[int] = None
        self._barriers = 0

    def start(self) -> "Harness":
        self.thread = threading.Thread(target=self._run, name="test-app-main", daemon=True)
        self.thread.start()
        assert wait_for(self.link.is_connected)
        if self.cfg.broadcast.enabled:  # the main loop polls the scheduler on every tick
            assert wait_for(lambda: self.scheduler.polls > 0)
        return self

    def _run(self) -> None:
        self.rc = self.app.run()

    def stop(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.app.request_stop()
            self.thread.join(10)
            assert not self.thread.is_alive()

    def ticks(self, n: int = 3) -> None:
        """Let the main loop run ``n`` more ticks."""
        start = self.scheduler.polls
        assert wait_for(lambda: self.scheduler.polls >= start + n)

    def idle(self) -> None:
        """Wait until earlier work items are done and every packet is sent (a ping DM is the FIFO barrier)."""
        self._barriers += 1
        packet_id = BARRIER_IDS + self._barriers
        self.link.on_message(dm("ping", packet_id=packet_id))
        assert wait_for(lambda: any(m.reply_id == packet_id for m in self.link.sent))
        assert self.sender.wait_idle(5)

    def sent(self, kind: Optional[str] = None) -> List[OutgoingMessage]:
        """Packets sent so far (barrier replies excluded), optionally of one kind."""
        return [m for m in self.link.sent if (kind is None or m.kind == kind)
                and (m.reply_id or 0) < BARRIER_IDS]


@pytest.fixture
def harness(tmp_path):
    made: List[Harness] = []

    def make(overrides: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Harness:
        h = Harness(tmp_path, overrides, **kwargs)
        made.append(h)
        return h

    yield make
    for h in made:
        h.stop()


def dm(text: str, from_num: int = USER, packet_id: int = 7) -> IncomingMessage:
    return IncomingMessage(text=text, from_num=from_num, from_id=node_id_from_num(from_num), to_num=MY_NODE_NUM,
                           channel_index=0, is_dm=True, packet_id=packet_id, hops_away=1)


def alert(alert_id: str, event: str = "Tornado Warning", severity: Optional[str] = "Extreme", *,
          message_type: str = "Alert", references: Sequence[str] = (),
          ends: Optional[datetime] = None) -> WeatherAlert:
    return WeatherAlert(id=alert_id, event=event, severity=severity, message_type=message_type,
                        headline="%s issued for Suffolk County" % event, instruction="Take cover now. Stay low.",
                        ends=ends if ends is not None else utcnow() + timedelta(hours=2),
                        references=tuple(references))


# ============================================================================ commands


def test_command_from_the_radio_is_answered(harness):
    h = harness().start()
    h.link.on_message(dm("ping"))
    assert wait_for(lambda: h.sent("reply"))
    reply = h.sent("reply")[0]
    assert reply.text == "pong (1 hop)"
    assert reply.destination == USER and reply.reply_id == 7
    assert "[TX ch0 -> !11223344] pong (1 hop)" in h.link.out.getvalue()


def test_status_command_uses_app_status(harness):
    h = harness().start()
    h.link.on_message(dm("status"))
    assert wait_for(lambda: h.sent("reply"))
    text = " ".join(m.text for m in h.sent("reply"))
    assert "ZIP 02108 Boston, MA" in text and "Link up" in text and "v%s" % __version__ in text


def test_on_message_never_blocks_while_the_worker_is_busy(harness):
    h = harness().start()
    h.weather.block = threading.Event()
    h.link.on_message(dm("wx"))
    assert wait_for(lambda: h.weather.report_calls == 1)  # the worker is now stuck in get_report
    started = time.monotonic()
    h.link.on_message(dm("ping", packet_id=8))
    assert time.monotonic() - started < 0.5
    h.weather.block.set()
    assert wait_for(lambda: len(h.sent("reply")) == 2)
    assert [m.reply_id for m in h.sent("reply")] == [7, 8]  # FIFO


def test_reply_refused_by_a_full_send_queue_is_logged(harness, caplog):
    h = harness().start()
    h.sender.enqueue = lambda msgs: False  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="meshwx.app"):
        h.link.on_message(dm("ping"))
        assert wait_for(lambda: "reply to !11223344 dropped (1 packet(s)): send queue full" in caplog.text)


def test_admin_broadcast_command_queues_a_broadcast(harness):
    h = harness().start()
    h.link.on_message(dm("broadcast", from_num=ADMIN))
    assert wait_for(lambda: h.sent("broadcast"))
    assert h.sent("reply")[0].text == "Broadcast queued"


# ============================================================================ broadcasts


def test_broadcast_now_sends_weather_on_the_broadcast_channel(harness):
    h = harness().start()
    h.app.broadcast_now("test")
    assert wait_for(lambda: h.sent("broadcast"))
    h.idle()
    packets = h.sent("broadcast")
    assert all(m.destination == BROADCAST and m.channel_index == 1 for m in packets)
    text = "\n".join(m.text for m in packets)
    assert "Boston, MA" in text and "Tonight" in text


def test_scheduled_broadcast_when_the_scheduler_says_so(harness):
    h = harness().start()
    h.ticks()
    assert not h.sent()
    h.scheduler.due = True
    assert wait_for(lambda: h.sent("broadcast"))
    assert h.weather.report_calls == 1


def test_no_scheduled_broadcasts_when_disabled(harness):
    h = harness({"broadcast": {"enabled": False}}).start()
    h.scheduler.due = True
    time.sleep(0.1)
    assert h.scheduler.polls == 0 and not h.sent()


def test_pending_broadcasts_are_coalesced(harness):
    h = harness()
    h.app.broadcast_now("one")
    h.app.broadcast_now("two")  # still queued: not added again
    h.start()
    assert wait_for(lambda: h.sent("broadcast"))
    h.idle()
    assert h.weather.report_calls == 1


def test_weather_error_skips_the_broadcast_and_the_worker_survives(harness, caplog):
    h = harness().start()
    h.weather.fail = WeatherError("NWS down", user_message="Weather unavailable, try later")
    with caplog.at_level(logging.WARNING, logger="meshwx.app"):
        h.app.broadcast_now("test")
        assert wait_for(lambda: h.weather.report_calls == 1)
        h.link.on_message(dm("ping"))
        assert wait_for(lambda: h.sent("reply"))
    assert not h.sent("broadcast")  # never an error message on the mesh
    assert "weather unavailable" in caplog.text and "skipped" in caplog.text


def test_unexpected_exception_is_logged_and_the_worker_survives(harness, caplog):
    h = harness().start()
    h.weather.fail = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="meshwx.app"):
        h.app.broadcast_now("test")
        assert wait_for(lambda: "broadcast job failed" in caplog.text)
    assert any(r.exc_info for r in caplog.records if r.getMessage() == "broadcast job failed")
    h.link.on_message(dm("ping"))
    assert wait_for(lambda: h.sent("reply"))


def test_broadcast_with_transit(harness):
    h = harness({"broadcast": {"content": ["current", "transit"]}}, transit=True).start()
    h.app.broadcast_now("test")
    assert wait_for(lambda: h.sent("broadcast"))
    h.idle()
    text = "\n".join(m.text for m in h.sent("broadcast"))
    assert "Boston, MA" in text and "Park St: Red>Alewife 5m" in text
    assert h.transit.warmed.wait(2)


def test_transit_failure_does_not_block_the_weather_part(harness):
    h = harness({"broadcast": {"content": ["current", "transit"]}}, transit=True).start()
    h.transit.fail = TransitError("feed down")
    h.app.broadcast_now("test")
    assert wait_for(lambda: h.sent("broadcast"))
    h.idle()
    text = "\n".join(m.text for m in h.sent("broadcast"))
    assert "Boston, MA" in text and "Park St" not in text


def test_weather_failure_keeps_the_transit_part(harness):
    h = harness({"broadcast": {"content": ["current", "transit"]}}, transit=True).start()
    h.weather.fail = WeatherError("down")
    h.app.broadcast_now("test")
    assert wait_for(lambda: h.sent("broadcast"))
    assert "Park St" in h.sent("broadcast")[0].text and "Boston" not in h.sent("broadcast")[0].text


def test_startup_broadcast_once_on_first_connect(harness):
    h = harness({"broadcast": {"on_startup": True}}).start()
    assert wait_for(lambda: h.sent("broadcast"))
    h.link.on_connection(True)  # e.g. a reconnect later on
    h.ticks()
    h.idle()
    assert h.weather.report_calls == 1


def test_no_startup_broadcast_by_default(harness):
    h = harness().start()
    h.ticks()
    assert h.weather.report_calls == 0


# ============================================================================ alert watch


def test_alert_watch_broadcasts_new_alerts_once(harness, tmp_path):
    h = harness({"broadcast": {"alert_channel_index": 0}}).start()
    h.weather.alerts = [alert("a1")]
    h.clock.advance(29)
    h.ticks()
    assert h.weather.alert_calls == 0  # first check about 30 s after the start
    h.clock.advance(2)
    assert wait_for(lambda: h.sent("alert"))
    h.idle()
    first = h.sent("alert")
    assert first[0].text.startswith("⚠ Tornado Warning for Boston, MA")
    assert all(m.channel_index == 0 and m.destination == BROADCAST for m in first)
    doc = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert doc["data"]["seen_alerts"]["a1"]["event"] == "Tornado Warning"
    written = (tmp_path / "state.json").stat().st_mtime_ns

    h.clock.advance(5 * 60)  # second check: nothing new
    assert wait_for(lambda: h.weather.alert_calls == 2)
    h.idle()
    assert h.sent("alert") == first
    assert (tmp_path / "state.json").stat().st_mtime_ns == written  # unchanged state is not rewritten
    assert h.app.status()["alerts_active"] == 1

    # an update of the same event is recorded silently; a new event is announced
    h.weather.alerts = [alert("a2", message_type="Update", references=["a1"]),
                        alert("a3", event="Flood Warning", severity="Severe")]
    h.clock.advance(5 * 60)
    assert wait_for(lambda: h.weather.alert_calls == 3)
    h.idle()
    new = h.sent("alert")[len(first):]
    assert new and "Flood Warning" in new[0].text
    assert not any("Tornado" in m.text for m in new)
    seen = h.settings.get_data("seen_alerts")
    assert set(seen) == {"a1", "a2", "a3"}


def test_seen_alerts_survive_a_restart(harness):
    first = harness().start()
    first.weather.alerts = [alert("a1")]
    first.clock.advance(31)
    assert wait_for(lambda: first.sent("alert"))
    first.idle()  # every packet out: only then is the alert recorded as announced (TIME-2)
    first.stop()

    second = harness().start()  # same state dir
    second.weather.alerts = [alert("a1")]
    second.clock.advance(31)
    assert wait_for(lambda: second.weather.alert_calls == 1)
    second.idle()
    assert not second.sent("alert")


def test_alerts_use_the_broadcast_channel_by_default_and_ignore_min_severity_below(harness):
    h = harness({"broadcast": {"alert_min_severity": "Severe"}}).start()
    h.weather.alerts = [alert("weak", event="Wind Advisory", severity="Moderate"), alert("strong")]
    h.clock.advance(31)
    assert wait_for(lambda: h.sent("alert"))
    h.idle()
    assert all(m.channel_index == 1 for m in h.sent("alert"))
    assert not any("Wind Advisory" in m.text for m in h.sent("alert"))


def test_alert_check_postponed_while_the_radio_is_down(harness):
    h = harness().start()
    h.weather.alerts = [alert("a1")]
    h.link.down = True
    h.clock.advance(31)
    h.ticks()
    assert h.weather.alert_calls == 0
    h.link.down = False
    assert wait_for(lambda: h.sent("alert"))


def test_no_alert_watch_without_provider_support(harness):
    h = harness(alerts_supported=False).start()
    h.clock.advance(3600)
    h.ticks()
    assert h.weather.alert_calls == 0


def test_no_alert_watch_when_disabled(harness):
    h = harness({"broadcast": {"alert_watch": False}}).start()
    h.clock.advance(3600)
    h.ticks()
    assert h.weather.alert_calls == 0


def test_failed_alert_check_is_logged_not_broadcast(harness, caplog):
    h = harness().start()
    h.weather.alert_fail = WeatherError("NWS alerts request failed")
    with caplog.at_level(logging.WARNING, logger="meshwx.app"):
        h.clock.advance(31)
        assert wait_for(lambda: "alert check failed" in caplog.text)
    h.idle()
    assert not h.sent()


def test_alert_refused_by_a_full_queue_is_retried(harness):
    h = harness().start()
    h.weather.alerts = [alert("a1")]
    real_enqueue = h.sender.enqueue
    full = [True]
    h.sender.enqueue = lambda msgs: False if full[0] and msgs[0].kind == "alert" else real_enqueue(msgs)
    h.clock.advance(31)
    assert wait_for(lambda: h.weather.alert_calls == 1)
    h.idle()
    assert not h.sent("alert") and not h.settings.get_data("seen_alerts")
    full[0] = False
    h.clock.advance(5 * 60)
    assert wait_for(lambda: h.sent("alert"))


# ---------------------------------------------------------------------------- alert delivery (TIME-2)


def reject_alert_packets(h: Harness, while_: Callable[[], bool], tried: Optional[List[int]] = None) -> None:
    """Make the radio refuse alert packets (ValueError: dropped, no retry) while ``while_()`` is true."""
    real_send = h.link.send

    def send(msg: OutgoingMessage) -> None:
        if msg.kind == "alert" and while_():
            if tried is not None:
                tried.append(h.weather.alert_calls)
            raise ValueError("packet rejected by the radio")
        real_send(msg)

    h.link.send = send  # type: ignore[assignment]


def check_alerts(h: Harness, n: int) -> None:
    """Run alert check number ``n`` and wait until its packets are sent or dropped."""
    h.clock.advance(31 if n == 1 else 5 * 60)
    assert wait_for(lambda: h.weather.alert_calls == n)
    h.idle()


def pending_marks(h: Harness) -> Dict[str, Any]:
    return {k: v.get("pending") for k, v in (h.settings.get_data("seen_alerts") or {}).items()}


def test_alert_whose_packets_are_dropped_is_announced_again(harness):
    h = harness().start()
    h.weather.alerts = [alert("a1")]
    radio_trouble = [True]
    reject_alert_packets(h, lambda: radio_trouble[0])
    check_alerts(h, 1)
    assert not h.sent("alert")
    assert pending_marks(h) == {"a1": "a1"}  # saved as "on its way", not as announced

    radio_trouble[0] = False
    check_alerts(h, 2)
    announced = h.sent("alert")
    assert announced and "Tornado Warning" in announced[0].text
    assert pending_marks(h) == {"a1": None}  # every packet out: now it counts as announced

    check_alerts(h, 3)
    assert h.sent("alert") == announced  # and is not repeated


def test_alert_discarded_at_shutdown_is_announced_after_the_restart_and_only_then(tmp_path):
    cfg = make_config({"state_dir": str(tmp_path), "broadcast": {"channel_index": 1}})
    weather = StubWeather()
    weather.alerts = [alert("a1"), alert("a2", event="Flood Warning", severity="Severe")]

    def service(started: bool) -> Tuple[App, RecordingLink, PacedSender]:
        link = RecordingLink([])
        sender = PacedSender(link, 8.0 if not started else 0.0, max_bytes=200)
        app = App(cfg, settings=RuntimeSettings(cfg, tmp_path), weather=weather,
                  formatter=Formatter(cfg.display, 200, default_tz=timezone.utc), link=link, sender=sender,
                  scheduler=StubScheduler())
        if started:
            link.start()
            sender.start()
        return app, link, sender

    app, link, sender = service(started=False)
    app._do_alert_check()  # queued; the service stops before the pacing lets them out
    assert sender.pending() >= 2
    app._shutdown()
    assert link.sent == []
    marks = RuntimeSettings(cfg, tmp_path).get_data("seen_alerts")
    assert {k: v.get("pending") for k, v in marks.items()} == {"a1": "a1", "a2": "a2"}

    app, link, sender = service(started=True)  # after the restart: announced now
    app._do_alert_check()
    assert sender.wait_idle(5)
    assert any("Tornado Warning" in m.text for m in link.sent) and any("Flood Warning" in m.text for m in link.sent)
    marks = RuntimeSettings(cfg, tmp_path).get_data("seen_alerts")
    assert {k: v.get("pending") for k, v in marks.items()} == {"a1": None, "a2": None}
    app._shutdown()

    app, link, sender = service(started=True)  # everything went out: the next restart repeats nothing
    app._do_alert_check()
    assert sender.wait_idle(5) and link.sent == []
    app._shutdown()


def test_alert_recorded_silently_because_of_a_failed_announcement_is_reconsidered(harness):
    h = harness().start()
    # Original and update both new: the update is announced, the original recorded silently with it.
    h.weather.alerts = [alert("a1"), alert("u1", message_type="Update", references=["a1"])]
    radio_trouble = [True]
    reject_alert_packets(h, lambda: radio_trouble[0])
    check_alerts(h, 1)
    assert not h.sent("alert") and pending_marks(h) == {"a1": "u1", "u1": "u1"}

    radio_trouble[0] = False
    check_alerts(h, 2)  # without the tie, u1 would now pass as a silent follow-up of a1
    assert h.sent("alert") and pending_marks(h) == {"a1": None, "u1": None}


def test_alert_that_can_never_be_sent_is_given_up_after_three_announcements(harness, caplog):
    h = harness().start()
    h.weather.alerts = [alert("a1")]
    tried: List[int] = []
    reject_alert_packets(h, lambda: True, tried)
    with caplog.at_level(logging.INFO, logger="meshwx.app"):
        for n in range(1, 6):
            check_alerts(h, n)
    assert sorted(set(tried)) == [1, 2, 3]  # announced by checks 1-3, then given up
    assert "alert a1 could not be sent 3 times; giving up on it" in caplog.text
    assert pending_marks(h) == {"a1": None}


def test_alert_still_on_its_way_is_not_queued_twice(harness):
    h = harness().start()
    h.weather.alerts = [alert("a1")]
    gate = threading.Event()
    real_send = h.link.send

    def slow_send(msg: OutgoingMessage) -> None:
        if msg.kind == "alert":
            gate.wait(5)  # a slow radio: the packets are still on their way at the next check
        real_send(msg)

    h.link.send = slow_send  # type: ignore[assignment]
    h.clock.advance(31)
    assert wait_for(lambda: h.weather.alert_calls == 1 and h.sender.pending() > 0)
    queued = h.sender.pending()
    h.app._do_alert_check()  # the next check: nothing new, nothing queued again
    assert h.sender.pending() == queued and pending_marks(h) == {"a1": "a1"}
    gate.set()
    h.idle()
    assert h.sent("alert") and pending_marks(h) == {"a1": None}


# ============================================================================ status, settings, lifecycle


def test_status_has_exactly_the_contract_keys(harness):
    h = harness(transit=True).start()
    st = h.app.status()
    assert set(st) == STATUS_KEYS
    assert st["version"] == __version__
    assert (st["zip_code"], st["zip_source"], st["location"]) == ("02108", "config", "Boston, MA")
    assert (st["interval_minutes"], st["interval_source"], st["times"]) == (180, "config", [])
    assert st["next_broadcast"] == NOW and st["broadcast_enabled"] is True and st["channel_index"] == 1
    assert st["link"] == "up" and st["link_desc"].startswith("console")
    assert st["queue"] == 0 and isinstance(st["uptime_s"], int)
    assert st["alerts_active"] is None and st["transit_enabled"] is True


def test_status_without_broadcasts_has_no_next_broadcast(harness):
    h = harness({"broadcast": {"enabled": False}})
    st = h.app.status()
    assert st["next_broadcast"] is None and st["link"] == "down" and st["uptime_s"] == 0


def test_setting_changes_reschedule_and_invalidate(harness):
    h = harness()
    h.settings.set_interval(60, "!000000aa")
    assert h.scheduler.reschedules == 1
    h.settings.set_zip("90210", "!000000aa")
    assert h.weather.invalidated == 1


def test_external_setting_change_is_picked_up_by_the_main_loop(harness, tmp_path):
    h = harness().start()
    other = RuntimeSettings(h.cfg, tmp_path)  # e.g. `meshwx set interval 60` in another process
    other.set_interval(60, "cli")
    st = (tmp_path / "state.json").stat()
    os.utime(tmp_path / "state.json", (st.st_atime, st.st_mtime + 2))
    h.clock.advance(31)
    assert wait_for(lambda: h.settings.interval_minutes == 60)
    assert wait_for(lambda: h.scheduler.reschedules == 1)


def test_startup_banner(harness, caplog):
    with caplog.at_level(logging.INFO, logger="meshwx.app"):
        h = harness({"broadcast": {"alert_channel_index": 0}}).start()
        assert wait_for(lambda: "home location: Boston, MA" in caplog.text)
    banner = next(r.getMessage() for r in caplog.records if "starting" in r.getMessage())
    assert banner == ("meshwx %s starting: ZIP 02108 (config); broadcasts every 180 min (config) on channel 1; "
                      "alert watch every 5 min, Moderate or worse, on channel 0; radio %s"
                      % (__version__, h.link.describe()))


def test_shutdown_order_and_return_code(harness):
    h = harness().start()
    worker = [t for t in threading.enumerate() if t.name == "meshwx-worker"]
    h.stop()
    assert h.rc == 0
    assert h.events == ["sender.stop (on_message=None)", "link.stop", "http.close"]
    assert not any(t.is_alive() for t in worker)


# App.run() on the main thread of a child process (signal handlers only work there), so a
# regression that deadlocks the handler fails the test (timeout) instead of hanging the suite.
_SIGNAL_SCRIPT = r'''
import io, signal, sys, threading
from datetime import timezone
sys.path.insert(0, ROOT)
from meshwx.app import App
from meshwx.config import config_from_dict
from meshwx.formatting import Formatter
from meshwx.mesh import ConsoleLink, PacedSender
from meshwx.models import Location
from meshwx.state import RuntimeSettings

class Weather:
    def alerts_supported(self): return False
    def home_location(self): return Location(42.3576, -71.0684, "Boston, MA", "02108")
    def invalidate(self): pass

class Scheduler:
    next_run_at = None
    def poll(self): return False
    def reschedule(self): pass

class Link(ConsoleLink):
    def start(self):
        self.sigint_at_start = signal.getsignal(signal.SIGINT)
        super().start()
    def stop(self):
        self.sigint_at_stop = signal.getsignal(signal.SIGINT)
        super().stop()

cfg = config_from_dict({"location": {"zip_code": "02108"}, "state_dir": STATE_DIR, "broadcast": {"enabled": False}})
for name in ("SIGINT", "SIGTERM"):
    before = signal.getsignal(signal.SIGINT)
    link = Link(200, output_stream=io.StringIO(), read_input=False)
    app = App(cfg, settings=RuntimeSettings(cfg, STATE_DIR), weather=Weather(),
              formatter=Formatter(cfg.display, 200, default_tz=timezone.utc), link=link,
              sender=PacedSender(link, 0.01), scheduler=Scheduler(), tick_seconds=0.005)
    timer = threading.Timer(0.3, signal.raise_signal, (getattr(signal, name),))
    timer.start()
    assert app.run() == 0
    timer.join()
    assert link.sigint_at_start == app._on_signal, link.sigint_at_start
    # after a Ctrl+C a second one raises KeyboardInterrupt, cutting a slow shutdown short
    expected = signal.default_int_handler if name == "SIGINT" else app._on_signal
    assert link.sigint_at_stop == expected, (name, link.sigint_at_stop)
    assert signal.getsignal(signal.SIGINT) is before
    print(name, "stopped cleanly")
'''


def test_run_on_the_main_thread_stops_on_sigint_and_sigterm(tmp_path):
    from conftest import ROOT

    script = tmp_path / "signals.py"
    script.write_text(_SIGNAL_SCRIPT.replace("ROOT", repr(str(ROOT))).replace("STATE_DIR", repr(str(tmp_path))),
                      encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2500:]
    assert proc.stdout.split() == ["SIGINT", "stopped", "cleanly", "SIGTERM", "stopped", "cleanly"]


def test_signal_handler_only_records_the_signal(harness, caplog):
    """RADIO-1: the handler runs on the main thread between two bytecodes - possibly while that
    thread holds the stop Event's internal lock (inside Event.wait) or a logging lock - so it must
    take no lock: no Event.set(), no logging.  The main loop stops and logs instead."""
    h = harness()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    with h.app._stop._cond:  # held by the main thread for a moment in every Event.wait()/set()
        handler = threading.Thread(target=h.app._on_signal, args=(signal.SIGTERM, None), daemon=True)
        handler.start()
        handler.join(2)
        blocked = handler.is_alive()
    assert not blocked, "the signal handler waits for a lock its own thread may be holding"
    assert caplog.records == []
    assert h.app._stop_requested() is True
    assert "SIGTERM received; stopping" in caplog.text


def test_signal_stops_the_main_loop_within_a_tick(harness):
    h = harness().start()
    h.app._on_signal(signal.SIGTERM, None)  # what the main thread's handler does
    h.thread.join(5)
    assert not h.thread.is_alive() and h.rc == 0
    assert h.events == ["sender.stop (on_message=None)", "link.stop", "http.close"]


def test_link_start_failure_returns_1(harness):
    from meshwx.errors import MeshLinkError

    h = harness()

    def broken_start() -> None:
        raise MeshLinkError("the meshtastic library is not installed")

    h.link.start = broken_start  # type: ignore[assignment]
    assert h.app.run() == 1
    assert "link.stop" in h.events and "http.close" in h.events


def test_build_wires_real_components(tmp_path, monkeypatch):
    http = FakeHttp().add_json("https://api.zippopotam.us/us/02108", "zippopotam_02108.json")
    agents: List[str] = []

    def fake_client(user_agent: Optional[str] = None) -> FakeHttp:
        agents.append(user_agent or "")
        return http

    monkeypatch.setattr(app_module, "HttpClient", fake_client)
    cfg = make_config({"state_dir": str(tmp_path), "weather": {"contact": "ops@example.org"},
                       "transit": {"enabled": True, "provider": "mbta", "stops": ["place-pktrm"]}})
    app = App.build(cfg, dry_run=True, cli_zip="02108", cli_interval="2h")
    assert "ops@example.org" in agents[0]
    st = app.status()
    assert set(st) == STATUS_KEYS
    assert st["location"] == "Boston, MA" and st["transit_enabled"] is True
    assert (st["interval_minutes"], st["interval_source"]) == (120, "command line")
    assert st["link_desc"].startswith("console") and st["link"] == "down"


def test_build_rejects_a_bad_cli_zip(tmp_path, monkeypatch):
    made: List[str] = []
    real_mkdtemp = app_module.tempfile.mkdtemp

    def mkdtemp(**kw: Any) -> str:
        made.append(real_mkdtemp(**kw))
        return made[-1]

    monkeypatch.setattr(app_module.tempfile, "mkdtemp", mkdtemp)
    with pytest.raises(ConfigError):
        App.build(make_config({"state_dir": str(tmp_path)}), dry_run=True, cli_zip="12")
    assert made and not os.path.exists(made[0])  # the dry run's state copy is cleaned up


def test_dry_run_works_on_a_private_copy_of_the_state(tmp_path, monkeypatch):
    """OPS-4: a dry run may share the state directory with the running service (the Pi's
    wrapper sets STATE_DIRECTORY): it must never write there."""
    service_dir = tmp_path / "service"
    cfg = make_config({"state_dir": str(service_dir), "commands": {"admins": [ADMIN_ID]}})
    service = RuntimeSettings(cfg, service_dir)
    service.set_interval(60, "cli")  # saved settings still apply to the dry run...
    service.set_data("seen_alerts", {"old": {"event": "Flood Warning", "ends": None,
                                             "seen_at": utcnow().isoformat(timespec="seconds")}})
    saved = (service_dir / "state.json").read_bytes()
    weather = StubWeather()
    weather.alerts = [alert("old", event="Flood Warning"), alert("a1")]
    monkeypatch.setattr(app_module, "HttpClient", lambda user_agent=None: StubHttp([]))
    monkeypatch.setattr(app_module, "WeatherService", lambda cfg, settings, http: weather)

    app = App.build(cfg, dry_run=True)
    copy_dir = app._settings.path.parent
    assert copy_dir != service_dir and app.status()["interval_minutes"] == 60
    app._do_alert_check()  # ...and so do the alerts the service already announced: only a1 is new
    marks = {k: v.get("pending") for k, v in app._settings.get_data("seen_alerts").items()}
    assert marks == {"old": None, "a1": "a1"} and app.status()["queue"] > 0
    app._settings.set_zip("90210", ADMIN_ID)  # an admin `setzip` sent to the dry run
    assert (service_dir / "state.json").read_bytes() == saved  # ...but nothing reaches the service
    assert "a1" in json.loads((copy_dir / "state.json").read_text(encoding="utf-8"))["data"]["seen_alerts"]
    app._shutdown()
    assert not copy_dir.exists()


# ============================================================================ select_new_alerts


def entry(event: str = "Tornado Warning", ends: Optional[datetime] = None, seen_at: datetime = NOW) -> Dict[str, Any]:
    return {"event": event, "ends": ends.isoformat() if ends else None, "seen_at": seen_at.isoformat()}


def a(alert_id: str, event: str = "Tornado Warning", severity: Optional[str] = "Severe", message_type: str = "Alert",
      references: Sequence[str] = (), ends: Optional[datetime] = NOW + timedelta(hours=3)) -> WeatherAlert:
    return WeatherAlert(id=alert_id, event=event, severity=severity, message_type=message_type, ends=ends,
                        references=tuple(references))


def ids(alerts: Sequence[WeatherAlert]) -> List[str]:
    return [x.id for x in alerts]


def test_severity_rank():
    assert [severity_rank(s) for s in ("Minor", "moderate", "SEVERE", "Extreme")] == [1, 2, 3, 4]
    assert severity_rank("Unknown") == severity_rank(None) == severity_rank("") == 2


def test_new_alert_is_broadcast_and_recorded():
    send, seen = select_new_alerts([a("x")], {}, "Moderate", NOW)
    assert ids(send) == ["x"]
    assert seen == {"x": {"event": "Tornado Warning", "ends": (NOW + timedelta(hours=3)).isoformat(),
                          "seen_at": NOW.isoformat()}}


def test_seen_alert_is_not_repeated_and_state_unchanged():
    old = {"x": entry()}
    send, seen = select_new_alerts([a("x")], old, "Moderate", NOW)
    assert send == [] and seen == old


def test_input_seen_is_not_modified():
    old = {"x": entry()}
    snapshot = json.dumps(old, sort_keys=True)
    select_new_alerts([a("y"), a("z", message_type="Cancel")], old, "Minor", NOW + timedelta(days=30))
    assert json.dumps(old, sort_keys=True) == snapshot


@pytest.mark.parametrize("severity, minimum, sent", [
    ("Minor", "Moderate", False),
    ("Moderate", "Moderate", True),
    ("Extreme", "Severe", True),
    ("Severe", "Extreme", False),
    (None, "Moderate", True),  # unknown counts as Moderate
    ("Unknown", "Severe", False),
    ("Minor", "Minor", True),
])
def test_min_severity(severity, minimum, sent):
    send, seen = select_new_alerts([a("x", severity=severity)], {}, minimum, NOW)
    assert bool(send) is sent
    assert ("x" in seen) is sent  # weaker alerts are not recorded


def test_cancel_is_recorded_silently():
    send, seen = select_new_alerts([a("c", message_type="Cancel", references=["x"])], {"x": entry()}, "Minor", NOW)
    assert send == [] and set(seen) == {"x", "c"}


def test_update_of_same_event_is_recorded_silently():
    send, seen = select_new_alerts([a("u", message_type="Update", references=["x"])], {"x": entry()},
                                   "Moderate", NOW)
    assert send == [] and "u" in seen


def test_upgrade_to_another_event_is_broadcast():
    watch = entry(event="Tornado Watch")
    send, seen = select_new_alerts([a("w", message_type="Update", references=["x"])], {"x": watch}, "Moderate", NOW)
    assert ids(send) == ["w"] and seen["w"]["event"] == "Tornado Warning"


@pytest.mark.parametrize("message_type", ["Alert", "Update", "", "update"])
def test_follow_up_of_the_same_event_is_silent_whatever_its_message_type(message_type):
    # TIME-1: a continuation issued after its predecessor expired is listed as a new "Alert" that
    # names the predecessor only in parameters.expiredReferences (the NWS provider adds them to references).
    send, seen = select_new_alerts([a("con", message_type=message_type, references=["x"])], {"x": entry()},
                                   "Moderate", NOW)
    assert send == [] and "con" in seen


def test_follow_up_typed_alert_of_another_event_is_still_news():
    watch = entry(event="Freeze Watch")
    send, _ = select_new_alerts([a("w", event="Freeze Warning", references=["x"])], {"x": watch}, "Moderate", NOW)
    assert ids(send) == ["w"]


def test_replaced_watch_is_silent_and_its_warning_is_news():
    # Real NWS upgrade: the watch's UPG message ("The Freeze Watch has been replaced") stays listed as an
    # "Alert" of event Freeze Watch, referencing the (expired) watch messages, next to the new warning.
    seen = {"watch-new": entry(event="Freeze Watch"), "watch-con": entry(event="Freeze Watch")}
    upg = a("watch-upg", event="Freeze Watch", references=["watch-con", "watch-new"])
    warning = a("warning-new", event="Freeze Warning")
    send, new_seen = select_new_alerts([upg, warning], seen, "Moderate", NOW)
    assert ids(send) == ["warning-new"] and {"watch-upg", "warning-new"} <= set(new_seen)


def test_continuation_after_a_missed_update_is_silent():
    # The service was down while an intermediate update was current: the next message references both
    # that unseen update and the announced original (all expired, so only via expiredReferences).
    send, _ = select_new_alerts([a("con2", references=["con1-never-seen", "x"])], {"x": entry()}, "Moderate", NOW)
    assert send == []


def test_follow_up_does_not_need_its_predecessor_to_be_active():
    send, seen = select_new_alerts([a("con", references=["x"])], {"x": entry(ends=NOW - timedelta(hours=1))},
                                   "Moderate", NOW)
    assert send == [] and set(seen) == {"x", "con"}


def test_extra_fields_of_seen_entries_are_kept():
    old = {"x": dict(entry(), pending="x")}
    send, seen = select_new_alerts([a("x"), a("y")], old, "Moderate", NOW)
    assert ids(send) == ["y"] and seen["x"] == old["x"]


def test_update_of_an_unknown_alert_is_broadcast():
    send, _ = select_new_alerts([a("u", message_type="Update", references=["never-seen"])], {}, "Moderate", NOW)
    assert ids(send) == ["u"]


def test_original_and_its_update_in_one_batch_broadcast_once():
    original, update = a("x"), a("u", message_type="Update", references=["x"])
    for batch in ([original, update], [update, original]):
        send, seen = select_new_alerts(batch, {}, "Moderate", NOW)
        assert ids(send) == ["u"] and set(seen) == {"x", "u"}


def test_weak_original_then_stronger_update_is_announced():
    send, seen = select_new_alerts([a("x", severity="Minor")], {}, "Severe", NOW)
    assert send == [] and seen == {}
    send, _ = select_new_alerts([a("u", severity="Extreme", message_type="Update", references=["x"])], seen,
                                "Severe", NOW)
    assert ids(send) == ["u"]


def test_pruning():
    old = {
        "ended-long-ago": entry(ends=NOW - timedelta(hours=25)),
        "ended-recently": entry(ends=NOW - timedelta(hours=23)),
        "no-end-old": entry(seen_at=NOW - timedelta(days=8)),
        "no-end-recent": entry(seen_at=NOW - timedelta(days=6)),
        "garbage": {"event": "x"},
        "not-a-dict": "oops",
    }
    _, seen = select_new_alerts([], old, "Moderate", NOW)
    assert set(seen) == {"ended-recently", "no-end-recent"}


def test_active_alerts_are_never_pruned():
    old = {"x": entry(ends=NOW - timedelta(days=3), seen_at=NOW - timedelta(days=30))}
    send, seen = select_new_alerts([a("x", ends=None)], old, "Moderate", NOW)
    assert send == [] and seen == old
