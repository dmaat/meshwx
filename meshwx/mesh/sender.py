"""Outgoing packet queue that paces transmissions onto the mesh.

LoRa airtime is scarce, so :class:`PacedSender` sends one packet at a time,
waits ``delay_seconds`` after each one, sends the most urgent kind first
(alert, then reply, then broadcast; FIFO within a kind) and drops packets that
waited longer than ``max_age_seconds`` - a reply that is 20 minutes late is
just noise.

Delivery reports: every packet that :meth:`PacedSender.enqueue` accepted gets
exactly one call of its :attr:`~meshwx.models.OutgoingMessage.on_done`
callback (when set): ``"sent"`` once it was handed to the radio, ``"dropped"``
when it was too old, too big, unsendable or out of retries, ``"discarded"``
when :meth:`PacedSender.stop` found it still queued.  Callbacks run on the
sender thread (or on the thread calling ``enqueue``/``stop``), never while the
queue lock is held; one that raises is logged and ignored.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Union

from ..errors import MeshLinkError
from ..models import (
    BROADCAST,
    KIND_PRIORITY,
    OUTCOME_DISCARDED,
    OUTCOME_DROPPED,
    OUTCOME_SENT,
    OutgoingMessage,
    node_id_from_num,
)
from ..timeutil import utcnow
from .base import MeshLink
from .meshtastic_link import BROADCAST_NUM

log = logging.getLogger(__name__)

#: Slice for ``link.wait_connected`` so a stop is noticed while the radio is down.
LINK_POLL_SECONDS = 1.0
#: Upper bound of the pause before retrying a failed send.
MAX_RETRY_PAUSE_SECONDS = 1.0

_LOWEST_PRIORITY = max(KIND_PRIORITY.values())

# Outcomes of one send attempt (the first two double as delivery reports).
_SENT, _DROPPED, _RETRY = OUTCOME_SENT, OUTCOME_DROPPED, "retry"


@dataclass(order=True)
class _Entry:
    """Heap item, ordered by priority and then enqueue order (FIFO within a kind)."""

    priority: int
    seq: int
    deadline: float = field(compare=False)  # time.monotonic() after which the packet is stale
    msg: OutgoingMessage = field(compare=False)
    attempts: int = field(default=0, compare=False)


class PacedSender:
    """Priority queue plus a worker thread that feeds a :class:`MeshLink`.

    Message age is tracked on the monotonic clock, so a Pi's boot-time NTP
    clock jump cannot expire queued packets.  A packet whose send fails with
    :class:`MeshLinkError` goes back into the queue - keeping its place - and
    is tried again once the link is up, at most ``retries`` more times.
    """

    def __init__(
        self,
        link: MeshLink,
        delay_seconds: float,
        *,
        max_queue: int = 40,
        max_age_seconds: float = 900,
        max_bytes: int = 228,
        retries: int = 2,
    ) -> None:
        self._link = link
        self._delay = max(0.0, float(delay_seconds))
        self._max_queue = max_queue
        self._max_age = float(max_age_seconds)
        self._max_bytes = max_bytes
        self._retries = max(0, int(retries))
        self._cond = threading.Condition()  # guards _heap, _in_flight and _reporting
        self._heap: List[_Entry] = []
        self._seq = itertools.count()
        self._in_flight = 0  # popped packet: being sent, or its outcome is being reported
        self._reporting = 0  # packets taken out of the queue whose on_done report is still running
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="meshwx-sender", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop sending; queued packets are discarded (reported as "discarded"), not drained."""
        self._stop.set()
        with self._cond:
            discarded = [e.msg for e in sorted(self._heap)]
            self._heap.clear()
            self._reporting += len(discarded)
            self._cond.notify_all()
        if discarded:
            log.warning("sender stopped: discarded %d unsent packet(s)", len(discarded))
        self._finish(discarded, OUTCOME_DISCARDED)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def enqueue(self, msgs: Sequence[OutgoingMessage]) -> bool:
        """Queue all of ``msgs`` or none of them; False when the queue is full (or stopped).

        Packets over ``max_bytes`` are a formatting bug: they are logged and
        dropped here (reported as "dropped" when the rest was queued), the rest
        is still queued.  ``created_at`` is set when None.  Refused packets get
        no delivery report: the False return value is their report.
        """
        if self._stop.is_set():
            log.debug("sender stopped; refusing %d packet(s)", len(msgs))
            return False
        now_wall, now = utcnow(), time.monotonic()
        accepted: List[OutgoingMessage] = []
        too_big: List[OutgoingMessage] = []
        for msg in msgs:
            size = len(msg.text.encode("utf-8"))
            if size > self._max_bytes:
                log.error("dropping %s packet of %d bytes (limit %d): %r",
                          msg.kind, size, self._max_bytes, msg.text[:40])
                too_big.append(msg)
                continue
            accepted.append(msg)
        with self._cond:
            if self._stop.is_set():  # stop() ran meanwhile: it would never report these
                log.debug("sender stopped; refusing %d packet(s)", len(msgs))
                return False
            stale = self._take_expired(now)
            pending = len(self._heap) + self._in_flight
            queued = pending + len(accepted) <= self._max_queue
            if queued:
                for msg in accepted:
                    deadline = now + self._max_age - _age_seconds(msg, now_wall)
                    priority = KIND_PRIORITY.get(msg.kind, _LOWEST_PRIORITY)
                    heapq.heappush(self._heap, _Entry(priority, next(self._seq), deadline, msg))
                self._cond.notify_all()
        self._finish(stale, OUTCOME_DROPPED)
        if not queued:
            log.warning("send queue full (%d pending, max %d): refusing %d packet(s)",
                        pending, self._max_queue, len(accepted))
            return False
        _report(too_big, OUTCOME_DROPPED)
        return True

    def pending(self) -> int:
        """Packets queued or being sent right now (including outcome reports still running)."""
        with self._cond:
            return len(self._heap) + self._in_flight + self._reporting

    def wait_idle(self, timeout: float) -> bool:
        """Block until every queued packet was sent or dropped, and reported; False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._heap or self._in_flight or self._reporting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # ------------------------------------------------------------------ worker thread
    def _run(self) -> None:
        while self._wait_for_work():
            if not self._link.wait_connected(LINK_POLL_SECONDS):
                with self._cond:
                    stale = self._take_expired(time.monotonic())  # nothing can go out; shed stale packets
                self._finish(stale, OUTCOME_DROPPED)
                continue
            entry = self._pop()
            if entry is None:
                continue
            outcome = self._attempt(entry)
            if outcome == _RETRY and self._requeue(entry):
                pause = min(self._delay, MAX_RETRY_PAUSE_SECONDS)
            else:
                if outcome == _RETRY:
                    outcome = OUTCOME_DISCARDED  # stop() ran during the attempt
                _report([entry.msg], outcome)
                with self._cond:
                    self._in_flight = 0  # only now: wait_idle()/pending() include the report
                    self._cond.notify_all()
                if outcome != _SENT:
                    continue
                pause = self._delay
            if self._stop.wait(pause):
                break

    def _wait_for_work(self) -> bool:
        """Block until something is queued; False when stopping."""
        with self._cond:
            while not self._heap and not self._stop.is_set():
                self._cond.wait()
            return not self._stop.is_set()

    def _pop(self) -> Optional[_Entry]:
        """Take the most urgent packet - chosen only once the link is up."""
        with self._cond:
            if self._stop.is_set() or not self._heap:
                return None
            self._in_flight = 1
            return heapq.heappop(self._heap)

    def _requeue(self, entry: _Entry) -> bool:
        """Put a packet whose send failed back in its place; False when stopping (it is then discarded)."""
        with self._cond:
            if self._stop.is_set():
                return False
            heapq.heappush(self._heap, entry)
            self._in_flight = 0
            self._cond.notify_all()
            return True

    def _attempt(self, entry: _Entry) -> str:
        """Hand ``entry`` to the link once; return the outcome (_SENT, _RETRY or _DROPPED)."""
        msg = entry.msg
        if time.monotonic() > entry.deadline:
            self._log_stale(msg)
            return _DROPPED
        entry.attempts += 1
        try:
            self._link.send(msg)
        except ValueError as exc:
            log.error("dropping unsendable %s packet: %s", msg.kind, exc)
            return _DROPPED
        except MeshLinkError as exc:
            if entry.attempts > self._retries:
                log.warning("giving up on %s packet to %s after %d attempt(s): %s",
                            msg.kind, _dest_label(msg.destination), entry.attempts, exc)
                return _DROPPED
            log.info("send failed (%s); retry %d/%d when the radio is back", exc, entry.attempts, self._retries)
            return _RETRY
        except Exception:  # noqa: BLE001 - a buggy link must not kill the sender
            log.exception("unexpected error while sending; packet dropped")
            return _DROPPED
        log.info("sent %s packet to %s on ch%d (%dB)", msg.kind, _dest_label(msg.destination),
                 msg.channel_index, len(msg.text.encode("utf-8")))
        return _SENT

    def _take_expired(self, now: float) -> List[OutgoingMessage]:
        """Remove packets past their deadline (lock held) so they cannot fill the queue.

        Returns them for :meth:`_finish`, which the caller must run once the lock is released.
        """
        expired = sorted(e for e in self._heap if e.deadline < now)
        if not expired:
            return []
        self._heap = [e for e in self._heap if e.deadline >= now]
        heapq.heapify(self._heap)
        for entry in expired:
            self._log_stale(entry.msg)
        self._reporting += len(expired)
        self._cond.notify_all()
        return [e.msg for e in expired]

    def _finish(self, msgs: List[OutgoingMessage], outcome: str) -> None:
        """Report packets taken out of the queue (lock NOT held), then stop counting them as pending."""
        if not msgs:
            return
        try:
            _report(msgs, outcome)
        finally:
            with self._cond:
                self._reporting -= len(msgs)
                self._cond.notify_all()

    def _log_stale(self, msg: OutgoingMessage) -> None:
        log.warning("dropping %s packet older than %gs: %r", msg.kind, self._max_age, msg.text[:40])


def _report(msgs: Sequence[OutgoingMessage], outcome: str) -> None:
    """Call each packet's ``on_done(msg, outcome)``; a failing callback is logged, never raised."""
    for msg in msgs:
        if msg.on_done is None:
            continue
        try:
            msg.on_done(msg, outcome)
        except Exception:  # noqa: BLE001 - a buggy callback must not kill the sender
            log.exception("delivery callback of a %s packet failed (%s)", msg.kind, outcome)


def _age_seconds(msg: OutgoingMessage, now: datetime) -> float:
    """Seconds since ``msg.created_at``; stamps ``created_at`` (aware UTC) when it is None."""
    if msg.created_at is None:
        msg.created_at = now
        return 0.0
    created = msg.created_at
    if created.tzinfo is None:  # models promise aware datetimes; be lenient anyway
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds())


def _dest_label(dest: Union[int, str]) -> str:
    """Destination for log lines: ``"^all"``, ``"!a1b2c3d4"`` or whatever the caller passed."""
    if isinstance(dest, int) and not isinstance(dest, bool):
        return BROADCAST if dest == BROADCAST_NUM else node_id_from_num(dest)
    return str(dest)
