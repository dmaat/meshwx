"""Tests for PacedSender: priority, capacity, pacing, expiry, retries and shutdown."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Callable, List, Optional, Tuple

import pytest

from meshwx.errors import MeshLinkError
from meshwx.mesh import sender as sender_mod
from meshwx.mesh.base import MeshLink
from meshwx.mesh.sender import PacedSender
from meshwx.models import OutgoingMessage
from meshwx.timeutil import utcnow


class FakeLink(MeshLink):
    """Records sends; ``failures`` are raised by the next send() calls, in order.

    A MeshLinkError takes the link down; it comes back after ``reconnect_after`` seconds.
    """

    def __init__(self, connected: bool = True, reconnect_after: float = 0.03) -> None:
        super().__init__()
        self.up = threading.Event()
        if connected:
            self.up.set()
        self.reconnect_after = reconnect_after
        self.failures: List[BaseException] = []
        self.attempts: List[Tuple[float, str]] = []
        self.sent: List[Tuple[float, OutgoingMessage]] = []
        self.gate: Optional[threading.Event] = None  # when set, send() blocks until it opens

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_connected(self) -> bool:
        return self.up.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self.up.wait(timeout)

    def my_node_num(self) -> Optional[int]:
        return 1

    def send(self, msg: OutgoingMessage) -> None:
        self.attempts.append((time.monotonic(), msg.text))
        if self.gate is not None:
            self.gate.wait(2)
        if self.failures:
            exc = self.failures.pop(0)
            if isinstance(exc, MeshLinkError):
                self.up.clear()
                reconnect = threading.Timer(self.reconnect_after, self.up.set)
                reconnect.daemon = True
                reconnect.start()
            raise exc
        self.sent.append((time.monotonic(), msg))


def msg(text: str, kind: str = "reply") -> OutgoingMessage:
    return OutgoingMessage(text, kind=kind)


def texts(link: FakeLink) -> List[str]:
    return [m.text for _, m in link.sent]


def wait_for(pred: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


@pytest.fixture
def make_sender():
    senders: List[PacedSender] = []

    def make(link: MeshLink, delay: float = 0.0, **kwargs) -> PacedSender:
        s = PacedSender(link, delay, **kwargs)
        senders.append(s)
        return s

    yield make
    for s in senders:
        s.stop(timeout=1.0)


@pytest.fixture(autouse=True)
def _fast_link_polling(monkeypatch):
    monkeypatch.setattr(sender_mod, "LINK_POLL_SECONDS", 0.02)


# ============================================================================ queueing


def test_priority_order_fifo_within_kind(make_sender):
    link = FakeLink(connected=False)
    s = make_sender(link)
    assert s.enqueue([msg("b1", "broadcast"), msg("r1")])
    assert s.enqueue([msg("a1", "alert"), msg("r2"), msg("b2", "broadcast"), msg("a2", "alert")])
    s.start()
    time.sleep(0.03)  # the worker waits for the link without taking anything
    assert link.attempts == [] and s.pending() == 6
    link.up.set()
    assert s.wait_idle(1.0)
    assert texts(link) == ["a1", "a2", "r1", "r2", "b1", "b2"]


def test_enqueue_is_all_or_nothing(make_sender, caplog):
    s = make_sender(FakeLink(connected=False), max_queue=5)
    assert s.enqueue([msg("1"), msg("2"), msg("3")])
    assert not s.enqueue([msg("4"), msg("5"), msg("6")])
    assert s.pending() == 3
    assert "send queue full" in caplog.text
    assert s.enqueue([msg("4"), msg("5")])
    assert s.pending() == 5
    assert not s.enqueue([msg("6")])
    assert s.enqueue([])


def test_created_at_set_and_oversize_dropped(make_sender, caplog):
    s = make_sender(FakeLink(connected=False), max_bytes=10)
    ok, too_big = msg("0123456789"), msg("é" * 6)  # 12 UTF-8 bytes
    assert s.enqueue([ok, too_big])
    assert s.pending() == 1
    assert ok.created_at is not None and ok.created_at.utcoffset() == timedelta(0)
    assert "dropping reply packet of 12 bytes (limit 10)" in caplog.text


def test_pending_counts_in_flight_and_wait_idle(make_sender):
    link = FakeLink()
    link.gate = threading.Event()
    s = make_sender(link)
    assert s.wait_idle(0)  # nothing queued
    s.enqueue([msg("slow")])
    s.start()
    assert wait_for(lambda: len(link.attempts) == 1)
    assert s.pending() == 1  # in flight, no longer queued
    assert not s.wait_idle(0.05)
    link.gate.set()
    assert s.wait_idle(1.0) and s.pending() == 0


# ============================================================================ expiry


def test_old_messages_are_dropped(make_sender, caplog):
    link = FakeLink(connected=False)
    s = make_sender(link, max_age_seconds=900)
    old = msg("old")
    old.created_at = utcnow() - timedelta(seconds=1000)
    assert s.enqueue([old, msg("new")])
    s.start()
    link.up.set()
    assert s.wait_idle(1.0)
    assert texts(link) == ["new"]
    assert "dropping reply packet older than 900s" in caplog.text


def test_messages_expire_while_the_link_is_down(make_sender):
    link = FakeLink(connected=False)
    s = make_sender(link, max_age_seconds=0.05)
    s.enqueue([msg("too late")])
    s.start()
    time.sleep(0.08)
    link.up.set()
    assert s.wait_idle(1.0)
    assert link.sent == [] and link.attempts == []


def test_expired_messages_free_queue_space(make_sender):
    s = make_sender(FakeLink(connected=False), max_queue=2, max_age_seconds=0.05)
    assert s.enqueue([msg("a"), msg("b")])
    assert not s.enqueue([msg("c")])
    time.sleep(0.08)
    assert s.enqueue([msg("c")])
    assert s.pending() == 1


def test_stale_packets_are_shed_while_the_link_stays_down(make_sender, caplog):
    link = FakeLink(connected=False)
    s = make_sender(link, max_age_seconds=0.05)
    s.enqueue([msg("a"), msg("b")])
    s.start()
    assert s.wait_idle(1.0)  # emptied although the link never came up
    assert s.pending() == 0 and link.attempts == []
    assert caplog.text.count("dropping reply packet older than 0.05s") == 2


# ============================================================================ errors / retries


def test_retry_waits_for_reconnect(make_sender):
    link = FakeLink(reconnect_after=0.05)
    link.failures = [MeshLinkError("radio not connected")]
    s = make_sender(link, delay=0.01, retries=2)
    s.enqueue([msg("hi")])
    s.start()
    assert s.wait_idle(1.0)
    assert texts(link) == ["hi"] and len(link.attempts) == 2
    first_try, second_try = link.attempts[0][0], link.attempts[1][0]
    assert second_try - first_try >= 0.045  # retried only once the link was back


def test_failed_packet_waits_in_the_queue(make_sender, caplog):
    link = FakeLink(reconnect_after=5.0)
    link.failures = [MeshLinkError("radio not connected")]
    s = make_sender(link, retries=2)
    s.enqueue([msg("hi")])
    s.start()
    assert wait_for(lambda: len(link.attempts) == 1 and not link.up.is_set())
    assert s.pending() == 1 and not s.wait_idle(0.05)
    s.stop()
    assert "discarded 1 unsent packet(s)" in caplog.text


def test_alert_queued_during_an_outage_goes_before_the_retry(make_sender):
    link = FakeLink(reconnect_after=0.1)
    link.failures = [MeshLinkError("radio not connected")]
    s = make_sender(link, retries=2)
    s.enqueue([msg("r1"), msg("r2")])
    s.start()
    assert wait_for(lambda: not link.up.is_set())  # r1 failed: the radio is down
    s.enqueue([msg("a1", "alert")])
    assert s.wait_idle(1.0)
    assert texts(link) == ["a1", "r1", "r2"]


def test_gives_up_after_retries(make_sender, caplog):
    link = FakeLink(reconnect_after=0.01)
    link.failures = [MeshLinkError("x"), MeshLinkError("x"), MeshLinkError("x")]
    s = make_sender(link, delay=0.01, retries=2)
    s.enqueue([msg("lost"), msg("next")])
    s.start()
    assert s.wait_idle(1.0)
    assert [t for _, t in link.attempts] == ["lost", "lost", "lost", "next"]
    assert texts(link) == ["next"]
    assert "giving up on reply packet to ^all after 3 attempt(s)" in caplog.text


def test_value_error_drops_without_retry(make_sender):
    link = FakeLink()
    link.failures = [ValueError("too long")]
    s = make_sender(link, retries=2)
    s.enqueue([msg("bad"), msg("good")])
    s.start()
    assert s.wait_idle(1.0)
    assert [t for _, t in link.attempts] == ["bad", "good"] and texts(link) == ["good"]


def test_unexpected_error_does_not_kill_the_worker(make_sender, caplog):
    link = FakeLink()
    link.failures = [RuntimeError("link bug")]
    s = make_sender(link)
    s.enqueue([msg("boom"), msg("fine")])
    s.start()
    assert s.wait_idle(1.0)
    assert texts(link) == ["fine"]
    assert "unexpected error while sending" in caplog.text


# ============================================================================ pacing / shutdown


def test_paces_between_sends(make_sender):
    link = FakeLink()
    s = make_sender(link, delay=0.05)
    s.enqueue([msg("1"), msg("2"), msg("3")])
    s.start()
    assert wait_for(lambda: len(link.sent) == 3)
    times = [t for t, _ in link.sent]
    assert all(b - a >= 0.045 for a, b in zip(times, times[1:]))


def test_wait_idle_does_not_wait_for_the_pacing_delay(make_sender):
    link = FakeLink()
    s = make_sender(link, delay=30)
    s.enqueue([msg("only")])
    s.start()
    t0 = time.monotonic()
    assert s.wait_idle(1.0)
    assert time.monotonic() - t0 < 0.5 and texts(link) == ["only"]


def test_stop_is_prompt_mid_delay_and_discards(make_sender, caplog):
    link = FakeLink()
    s = make_sender(link, delay=30)
    s.enqueue([msg("1"), msg("2"), msg("3")])
    s.start()
    assert wait_for(lambda: len(link.sent) == 1)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 0.5
    assert texts(link) == ["1"] and s.pending() == 0
    assert "discarded 2 unsent packet(s)" in caplog.text
    assert not s.enqueue([msg("late")])


def test_stop_while_waiting_for_the_link(make_sender):
    link = FakeLink(connected=False)
    s = make_sender(link)
    s.enqueue([msg("never")])
    s.start()
    time.sleep(0.03)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 0.5
    link.up.set()
    time.sleep(0.03)
    assert link.attempts == []


# ============================================================================ delivery reports (on_done)


class Reports:
    """on_done callback that records ``(text, outcome, thread name)``."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, m: OutgoingMessage, outcome: str) -> None:
        with self.lock:
            self.calls.append((m.text, outcome, threading.current_thread().name))

    def outcomes(self) -> List[Tuple[str, str]]:
        with self.lock:
            return sorted((text, outcome) for text, outcome, _ in self.calls)


def tracked(reports: Reports, text: str, kind: str = "reply") -> OutgoingMessage:
    return OutgoingMessage(text, kind=kind, on_done=reports)


def test_every_accepted_packet_is_reported_exactly_once(make_sender):
    reports = Reports()
    link = FakeLink(reconnect_after=0.01)
    link.failures = [MeshLinkError("x"), MeshLinkError("x"), MeshLinkError("x"), ValueError("unsendable")]
    s = make_sender(link, retries=2, max_bytes=10)
    old = tracked(reports, "old")
    old.created_at = utcnow() - timedelta(seconds=1000)
    assert s.enqueue([tracked(reports, "lost"), tracked(reports, "bad"), tracked(reports, "toolongpacket"),
                      old, tracked(reports, "ok")])
    assert reports.outcomes() == [("toolongpacket", "dropped")]  # reported at once, by enqueue
    s.start()
    assert s.wait_idle(1.0)
    assert reports.outcomes() == [("bad", "dropped"), ("lost", "dropped"), ("ok", "sent"),
                                  ("old", "dropped"), ("toolongpacket", "dropped")]
    assert texts(link) == ["ok"]


def test_refused_packets_get_no_report(make_sender):
    reports = Reports()
    s = make_sender(FakeLink(connected=False), max_queue=1)
    assert not s.enqueue([tracked(reports, "a"), tracked(reports, "b"), tracked(reports, "c" * 300)])
    s.stop()
    assert not s.enqueue([tracked(reports, "late")])
    assert reports.calls == []


def test_packets_left_at_stop_are_reported_discarded(make_sender):
    reports = Reports()
    link = FakeLink()
    s = make_sender(link, delay=30)
    s.enqueue([tracked(reports, "1"), tracked(reports, "2", "alert"), tracked(reports, "3")])
    s.start()
    assert wait_for(lambda: len(link.sent) == 1)
    s.stop()
    assert texts(link) == ["2"]  # the alert went first
    assert reports.outcomes() == [("1", "discarded"), ("2", "sent"), ("3", "discarded")]


def test_stale_packets_shed_while_the_link_is_down_are_reported(make_sender):
    reports = Reports()
    s = make_sender(FakeLink(connected=False), max_age_seconds=0.05)
    s.enqueue([tracked(reports, "a", "alert"), tracked(reports, "b")])
    s.start()
    assert wait_for(lambda: s.pending() == 0)
    # pending() counts a packet until its report has run: once it is 0, both reports are in
    assert reports.outcomes() == [("a", "dropped"), ("b", "dropped")]


def test_packet_failing_while_the_sender_stops_is_reported_discarded(make_sender):
    reports = Reports()
    link = FakeLink(reconnect_after=5.0)
    link.gate = threading.Event()
    link.failures = [MeshLinkError("radio gone")]
    s = make_sender(link, retries=2)
    s.enqueue([tracked(reports, "hi")])
    s.start()
    assert wait_for(lambda: len(link.attempts) == 1)  # send() is blocked in the link
    stopper = threading.Thread(target=s.stop)
    stopper.start()
    assert wait_for(lambda: s._stop.is_set())
    link.gate.set()  # ...and now fails: no retry, the sender is stopping
    stopper.join(2)
    assert reports.outcomes() == [("hi", "discarded")]


def test_callbacks_run_without_the_queue_lock(make_sender):
    seen: List[Tuple[str, bool]] = []

    def on_done(m: OutgoingMessage, outcome: str) -> None:
        # Another thread must be able to use the queue meanwhile (an app callback takes its own locks).
        probe = threading.Thread(target=s.pending)
        probe.start()
        probe.join(0.5)
        seen.append((outcome, not probe.is_alive()))

    link = FakeLink()
    s = make_sender(link, delay=30, max_age_seconds=0.2, max_bytes=10)
    stale = OutgoingMessage("stale", on_done=on_done)
    stale.created_at = utcnow() - timedelta(seconds=1)
    assert s.enqueue([OutgoingMessage("toolongpacket", on_done=on_done), stale,
                      OutgoingMessage("sent", on_done=on_done)])
    s.start()
    assert wait_for(lambda: len(link.sent) == 1)
    s.enqueue([OutgoingMessage("left", on_done=on_done)])
    s.stop()
    assert sorted(seen) == [("discarded", True), ("dropped", True), ("dropped", True), ("sent", True)]


def test_a_failing_callback_is_logged_and_the_sender_carries_on(make_sender, caplog):
    reports = Reports()

    def broken(m: OutgoingMessage, outcome: str) -> None:
        raise RuntimeError("callback bug")

    link = FakeLink()
    s = make_sender(link)
    s.enqueue([OutgoingMessage("first", on_done=broken), tracked(reports, "second")])
    s.start()
    assert s.wait_idle(1.0)
    assert texts(link) == ["first", "second"] and reports.outcomes() == [("second", "sent")]
    assert "delivery callback of a reply packet failed (sent)" in caplog.text
    s.stop()
    s.enqueue([OutgoingMessage("never", on_done=broken)])  # refused: no callback, no error


def test_wait_idle_waits_for_the_report(make_sender):
    release = threading.Event()
    done: List[str] = []

    def slow(m: OutgoingMessage, outcome: str) -> None:
        release.wait(2)
        done.append(outcome)

    link = FakeLink()
    s = make_sender(link)
    s.enqueue([OutgoingMessage("x", on_done=slow)])
    s.start()
    assert wait_for(lambda: len(link.sent) == 1)
    assert s.pending() == 1 and not s.wait_idle(0.05)  # sent, but the report is still running
    release.set()
    assert s.wait_idle(1.0) and done == ["sent"] and s.pending() == 0
