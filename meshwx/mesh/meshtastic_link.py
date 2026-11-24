"""Link to a real Meshtastic node over USB serial (or TCP), with auto-reconnect.

The ``meshtastic`` library (and ``pubsub``) are imported lazily so that
importing this module - and ``meshwx preview`` or the unit tests - works
without them.

Library quirks handled here (meshtastic 2.7.x):

* ``SerialInterface(devPath=None)`` auto-detects badly: with no device it
  returns a half-built object, with several it calls ``sys.exit``.  The port is
  therefore always resolved here and passed explicitly.
* The constructor blocks until the node's config download completes.
* pypubsub holds listeners by *weak* reference, so only bound methods of this
  long-lived object are subscribed (never lambdas or closures).
* The reader thread publishes ``meshtastic.connection.lost`` for every dead
  interface, including ones closed on purpose; events from an interface other
  than the current one are ignored.
* ``sendText`` busy-waits, without a time limit, for TX-queue space the node
  reports.  A hung node would block the sender thread for good, so every send
  runs under a watchdog that takes the link down.
* Some library errors call ``sys.exit()``; ``SystemExit`` is caught on our
  threads so it cannot silently end them.
"""

from __future__ import annotations

import errno
import inspect
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional, Tuple, Union

from ..errors import MeshLinkError, NoDeviceError
from ..models import BROADCAST, IncomingMessage, OutgoingMessage, node_id_from_num
from ..timeutil import from_epoch
from .base import MeshLink

if TYPE_CHECKING:
    from ..config import MeshtasticConfig

log = logging.getLogger(__name__)

TOPIC_RECEIVE_TEXT = "meshtastic.receive.text"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
BROADCAST_NUM = 0xFFFFFFFF

#: A link that dies sooner than this after connecting counts as flapping: the
#: pause before the next reconnect keeps doubling instead of starting over.
STABLE_LINK_SECONDS = 60.0
#: A send still blocked after this long (the library waits forever for TX-queue
#: space a hung node never reports) takes the link down.
SEND_TIMEOUT_SECONDS = 120.0
#: Longest wait for the library's ``close()`` before the interface is abandoned.
CLOSE_TIMEOUT_SECONDS = 5.0
#: Longest wait for the supervisor thread in :meth:`MeshtasticLink.stop`.
STOP_JOIN_SECONDS = 5.0

_NODE_ID = re.compile(r"!?[0-9a-fA-F]{8}")

InterfaceFactory = Callable[..., Any]


def default_interface_factory(kind: str, **kwargs: Any) -> Any:
    """Create a real meshtastic interface; ``kind`` is ``"serial"`` or ``"tcp"``.

    Keyword arguments the installed library does not accept (``noNodes`` on old
    versions) are dropped with a warning instead of failing the connection.
    """
    if kind == "serial":
        if not kwargs.get("devPath"):
            raise ValueError("a serial interface needs an explicit devPath")  # None = the library's auto-detect
        from meshtastic.serial_interface import SerialInterface as interface_cls
    elif kind == "tcp":
        from meshtastic.tcp_interface import TCPInterface as interface_cls
    else:
        raise ValueError("unknown interface kind %r" % kind)
    accepted = _accepted_kwargs(interface_cls)
    if accepted is not None:
        unsupported = sorted(k for k in kwargs if k not in accepted)
        if unsupported:
            log.warning("this meshtastic version does not support %s; ignoring", ", ".join(unsupported))
            kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    return interface_cls(**kwargs)


def parse_destination(dest: Union[int, str]) -> Union[int, str]:
    """Validate an ``OutgoingMessage.destination``: a node number, ``"!a1b2c3d4"`` or ``"^all"``.

    Returns the node number (ids are converted, so the library never takes its
    ``sys.exit`` path for names missing from its node DB) or ``"^all"``.
    Raises ``ValueError`` for anything else.
    """
    if isinstance(dest, int) and not isinstance(dest, bool) and 0 < dest <= BROADCAST_NUM:
        return dest
    if dest == BROADCAST:
        return BROADCAST
    if isinstance(dest, str) and _NODE_ID.fullmatch(dest.strip()):
        return int(dest.strip().lstrip("!"), 16)
    raise ValueError("invalid destination %r" % (dest,))


class MeshtasticLink(MeshLink):
    """:class:`MeshLink` backed by the ``meshtastic`` library.

    A daemon supervisor thread connects, waits until the link is lost (library
    event, failed or stuck send), tears the interface down and reconnects:

    * a failed connection attempt is retried after ``reconnect_min_seconds``,
      doubling up to ``reconnect_max_seconds``; a successful connection resets
      that backoff;
    * after losing a link the next attempt waits ``reconnect_min_seconds``,
      doubling only while links keep dying within :data:`STABLE_LINK_SECONDS`
      (a flaky USB cable, a node in a crash loop).

    ``on_connection`` is always called from the supervisor thread, so up/down
    notifications arrive in order; an intentional :meth:`stop` reports nothing.

    ``interface_factory(kind, **kwargs)`` builds the library interface; tests
    inject fakes.  An explicit ``port_override`` (``meshwx run --port``) always
    means a serial connection on that port.
    """

    def __init__(
        self,
        cfg: MeshtasticConfig,
        *,
        port_override: Optional[str] = None,
        interface_factory: Optional[InterfaceFactory] = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._port_override = port_override or None
        self._factory: InterfaceFactory = interface_factory or default_interface_factory
        self._min_backoff = max(0.01, float(cfg.reconnect_min_seconds))  # floor: never a hot loop
        self._max_backoff = max(self._min_backoff, float(cfg.reconnect_max_seconds))

        self._lock = threading.Lock()  # guards the interface state below
        self._send_lock = threading.Lock()  # one packet at a time
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._wake = threading.Event()  # set when the link is marked down (or on stop)
        self._thread: Optional[threading.Thread] = None
        self._subscribed = False

        self._iface: Any = None
        self._lost = False
        self._send_params: Optional[FrozenSet[str]] = None
        self._my_num: Optional[int] = None  # last known identity, kept while down
        self._my_name: Optional[str] = None
        self._target = self._configured_target()
        self._warned_ports: List[str] = []
        self._warned_hop_limit = False

    # ------------------------------------------------------------------ MeshLink API
    def start(self) -> None:
        """Subscribe to the library's events and start the supervisor thread."""
        if self._thread is not None:
            return
        try:
            from pubsub import pub
        except ImportError as exc:
            raise MeshLinkError("the meshtastic library is not installed (pip install meshtastic)") from exc
        pub.subscribe(self._on_receive, TOPIC_RECEIVE_TEXT)
        pub.subscribe(self._on_lost, TOPIC_CONNECTION_LOST)
        self._subscribed = True
        self._thread = threading.Thread(target=self._supervise, name="meshwx-radio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Disconnect and stop reconnecting; returns within a bounded time."""
        self._stopping.set()
        self._wake.set()
        self._unsubscribe()
        with self._lock:
            iface, self._iface = self._iface, None
            self._connected.clear()
        if iface is not None:
            _close_interface(iface)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_SECONDS)
            if thread.is_alive():
                log.warning("radio thread still busy (connecting?); leaving it behind")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def send(self, msg: OutgoingMessage) -> None:
        """Hand one text packet to the radio (blocks while the library sends it).

        Raises ``ValueError`` for text over ``max_message_bytes`` or a malformed
        destination, and :class:`MeshLinkError` when not connected or when the
        send fails, hangs for :data:`SEND_TIMEOUT_SECONDS` or races a link loss
        - the link is then marked down and reconnected.
        """
        size = len(msg.text.encode("utf-8"))
        if size > self._cfg.max_message_bytes:
            raise ValueError("message is %d bytes, over the %d-byte limit" % (size, self._cfg.max_message_bytes))
        kwargs: Dict[str, Any] = {
            "destinationId": parse_destination(msg.destination),
            "wantAck": bool(msg.want_ack),
            "channelIndex": int(msg.channel_index),
        }
        with self._send_lock:
            with self._lock:
                iface = self._iface if self._connected.is_set() else None
                params = self._send_params
            if iface is None:
                raise MeshLinkError("radio not connected")
            kwargs.update(self._optional_send_kwargs(msg, params))
            watchdog = threading.Timer(
                SEND_TIMEOUT_SECONDS, self._mark_down,
                (iface, "send still blocked after %s (node hung?)" % _secs(SEND_TIMEOUT_SECONDS)))
            watchdog.daemon = True
            watchdog.start()
            try:
                iface.sendText(msg.text, **kwargs)
            except (Exception, SystemExit) as exc:  # SystemExit: the library calls sys.exit() on some errors
                self._mark_down(iface, "send failed: %s" % _describe_exc(exc))
                raise MeshLinkError("radio send failed: %s" % _describe_exc(exc)) from exc
            finally:
                watchdog.cancel()
            if not self._is_current(iface):
                # The library silently skips writes once its port is closed, so
                # a send that raced a link loss may never have reached the radio.
                raise MeshLinkError("radio link lost while sending")

    def my_node_num(self) -> Optional[int]:
        return self._my_num

    def node_name(self, num: int) -> Optional[str]:
        return _user_name(self._iface, num, ("shortName", "longName"))

    def describe(self) -> str:
        """E.g. ``serial /dev/ttyUSB0 !a1b2c3d4 "WX Bot"`` (last known identity while down)."""
        parts = [self._target]
        if self._my_num is not None:
            parts.append(node_id_from_num(self._my_num))
        if self._my_name:
            parts.append('"%s"' % self._my_name)
        return " ".join(parts)

    # ------------------------------------------------------------------ pubsub listeners
    def _on_receive(self, packet: Dict[str, Any], interface: Any) -> None:
        """``meshtastic.receive.text`` listener (runs on the library's publishing thread)."""
        if interface is None or interface is not self._iface:
            return
        try:
            msg = self._incoming(packet, interface)
            handler = self.on_message
            if msg is not None and handler is not None:
                handler(msg)
        except Exception:  # noqa: BLE001 - never raise into the library's thread
            log.exception("failed to handle an incoming text packet")

    def _on_lost(self, interface: Any) -> None:
        """``meshtastic.connection.lost`` listener: only flags the link; the supervisor tears it down."""
        if not self._stopping.is_set():
            self._mark_down(interface, "connection lost")

    # ------------------------------------------------------------------ supervisor thread
    def _supervise(self) -> None:
        """Connect, wait for the link to drop, tear down, back off, repeat - until stop()."""
        retry = self._min_backoff  # pause after a failed connection attempt
        relink = self._min_backoff  # pause after losing a link that was up
        while not self._stopping.is_set():
            try:
                connected = self._connect()
            except (Exception, SystemExit) as exc:  # SystemExit: library errors call sys.exit()
                if self._stopping.is_set():
                    break
                reason = self._explain(exc)
                log.warning("%s%sretrying in %s", reason, " " if reason.endswith("?") else "; ", _secs(retry),
                            exc_info=log.isEnabledFor(logging.DEBUG))
                self._stopping.wait(retry)
                retry = min(retry * 2, self._max_backoff)
                continue
            if not connected:  # stop() won the race
                break
            retry = self._min_backoff
            up_since = time.monotonic()
            self._wait_until_lost()
            self._teardown()
            if self._stopping.is_set():
                break
            if time.monotonic() - up_since >= STABLE_LINK_SECONDS:
                relink = self._min_backoff
            log.info("reconnecting to the radio in %s", _secs(relink))
            self._stopping.wait(relink)
            relink = min(relink * 2, self._max_backoff)

    def _connect(self) -> bool:
        """Open one interface (blocking); True once it is current, False if stopping."""
        kind, kwargs = self._resolve_target()
        log.info("connecting to the radio (%s)", self._target)
        iface = self._factory(kind, **kwargs)
        my_num = _my_node_num_of(iface)
        with self._lock:
            accepted = not self._stopping.is_set()
            if accepted:
                self._iface = iface
                self._lost = False
                self._my_num = my_num
                self._my_name = _user_name(iface, my_num, ("longName", "shortName"))
                self._send_params = _accepted_kwargs(getattr(iface, "sendText", None))
                self._connected.set()
        if not accepted:
            _close_interface(iface)
            return False
        if my_num is None:
            log.warning("the radio did not report its node number; DMs will not be recognised")
        log.info("radio connected: %s", self.describe())
        self._notify_connection(True)
        return True

    def _resolve_target(self) -> Tuple[str, Dict[str, Any]]:
        """Interface kind and constructor kwargs; never a serial devPath of None."""
        cfg = self._cfg
        kwargs: Dict[str, Any]
        if self._uses_tcp():
            kind, kwargs = "tcp", {"hostname": cfg.tcp_host, "portNumber": cfg.tcp_port}
        else:
            port = self._port_override or cfg.serial_port or self._detect_port()
            kind, kwargs = "serial", {"devPath": port}
        self._target = self._configured_target(kwargs.get("devPath"))
        if cfg.skip_node_db:
            kwargs["noNodes"] = True
        return kind, kwargs

    def _detect_port(self) -> str:
        """Pick the node's serial port the way the meshtastic CLI does (first candidate)."""
        from meshtastic.util import findPorts

        ports = list(findPorts(True))
        if not ports:
            raise NoDeviceError("no Meshtastic device found; is it plugged in?")
        if len(ports) > 1 and ports != self._warned_ports:
            log.warning("several serial ports found (%s); using %s - set meshtastic.serial_port to choose",
                        ", ".join(ports), ports[0])
            self._warned_ports = ports
        return ports[0]

    def _wait_until_lost(self) -> None:
        while not self._stopping.is_set():
            with self._lock:
                if self._lost:
                    return
            self._wake.wait()
            self._wake.clear()

    def _teardown(self) -> None:
        """Report the link down and close the interface (supervisor thread only, never in a pubsub callback)."""
        with self._lock:
            iface, self._iface = self._iface, None
            self._lost = False
            self._connected.clear()
        if iface is None:  # stop() already took it
            return
        if not self._stopping.is_set():
            self._notify_connection(False)
        _close_interface(iface)

    # ------------------------------------------------------------------ helpers
    def _mark_down(self, iface: Any, reason: str) -> None:
        """Flag ``iface`` as dead (if it is still current) and wake the supervisor."""
        with self._lock:
            if iface is None or iface is not self._iface or self._lost:
                return
            self._lost = True
            self._connected.clear()
        log.warning("radio link lost (%s): %s", self._target, reason)
        self._wake.set()

    def _is_current(self, iface: Any) -> bool:
        with self._lock:
            return iface is self._iface and not self._lost

    def _incoming(self, packet: Dict[str, Any], iface: Any) -> Optional[IncomingMessage]:
        """Library packet dict -> IncomingMessage, or None for packets the app must not see."""
        decoded = packet.get("decoded") or {}
        text = decoded.get("text")
        from_num = packet.get("from")
        if not isinstance(text, str) or not text or not isinstance(from_num, int):
            return None  # undecodable/empty text, or no sender to answer
        if decoded.get("emoji"):
            log.debug("ignoring a tapback reaction from %s", node_id_from_num(from_num))
            return None  # apps send emoji reactions as text packets with Data.emoji set
        my_num = self._my_num
        if from_num == my_num:
            return None
        to_num = packet.get("to", BROADCAST_NUM)
        if to_num not in (my_num, BROADCAST_NUM):
            log.debug("ignoring a text addressed to %s", node_id_from_num(to_num))
            return None
        hops = None
        hop_start = packet.get("hopStart")  # absent on old firmware
        if hop_start:
            hops = hop_start - packet.get("hopLimit", 0)  # protobuf JSON omits a hopLimit of 0
        rx_time = packet.get("rxTime")
        return IncomingMessage(
            text=text,
            from_num=from_num,
            from_id=node_id_from_num(from_num),
            to_num=to_num,
            channel_index=int(packet.get("channel", 0)),  # absent when 0
            is_dm=my_num is not None and to_num == my_num,
            packet_id=packet.get("id"),
            from_name=_user_name(iface, from_num, ("shortName", "longName")),
            rx_time=from_epoch(rx_time) if isinstance(rx_time, (int, float)) and rx_time > 0 else None,
            via_mqtt=bool(packet.get("viaMqtt")),
            hops_away=hops if hops is not None and hops >= 0 else None,
        )

    def _optional_send_kwargs(self, msg: OutgoingMessage, params: Optional[FrozenSet[str]]) -> Dict[str, Any]:
        """``replyId`` / ``hopLimit`` - only when set and supported by the installed sendText."""
        extra: Dict[str, Any] = {}
        if msg.reply_id is not None and (params is None or "replyId" in params):
            extra["replyId"] = int(msg.reply_id)
        hop_limit = self._cfg.hop_limit
        if hop_limit is not None:
            if params is None or "hopLimit" in params:
                extra["hopLimit"] = int(hop_limit)
            elif not self._warned_hop_limit:
                self._warned_hop_limit = True
                log.warning("this meshtastic version cannot set a hop limit; using the node's default")
        return extra

    def _notify_connection(self, up: bool) -> None:
        handler = self.on_connection
        if handler is None:
            return
        try:
            handler(up)
        except Exception:  # noqa: BLE001 - an app callback must not kill the supervisor
            log.exception("on_connection handler failed")

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        self._subscribed = False
        from pubsub import pub

        for listener, topic in ((self._on_receive, TOPIC_RECEIVE_TEXT), (self._on_lost, TOPIC_CONNECTION_LOST)):
            try:
                pub.unsubscribe(listener, topic)
            except Exception:  # noqa: BLE001 - best effort during shutdown
                log.debug("unsubscribing from %s failed", topic, exc_info=True)

    def _uses_tcp(self) -> bool:
        return self._cfg.connection == "tcp" and not self._port_override

    def _configured_target(self, port: Optional[str] = None) -> str:
        if self._uses_tcp():
            return "tcp %s:%d" % (self._cfg.tcp_host, self._cfg.tcp_port)
        return "serial %s" % (port or self._port_override or self._cfg.serial_port or "(auto-detect)")

    def _explain(self, exc: BaseException) -> str:
        """One actionable line for a failed connection attempt."""
        target = self._target
        if isinstance(exc, NoDeviceError):
            return str(exc)
        if isinstance(exc, SystemExit):
            return "%s: the meshtastic library gave up (exit code %s)" % (target, exc.code)
        if isinstance(exc, ImportError):
            return "the meshtastic library is not installed (pip install meshtastic): %s" % exc
        text = str(exc).lower()
        if "timed out waiting" in text:  # the config download did not finish
            if self._cfg.skip_node_db:
                return "%s: node did not answer (is it a Meshtastic node, and not hung?)" % target
            return "%s: node did not answer; if it has a large node DB set meshtastic.skip_node_db: true" % target
        if self._uses_tcp():
            if isinstance(exc, OSError):
                return "cannot reach %s: %s" % (target, _describe_exc(exc))
            return "%s: %s" % (target, _describe_exc(exc))
        code = getattr(exc, "errno", None)
        if code in (errno.EBUSY, errno.EAGAIN) or any(
                s in text for s in ("exclusively lock", "resource busy", "access is denied")):
            return "%s is busy: another program (meshtastic CLI? another meshwx?) is using the port" % target
        if code == errno.EACCES or "permission denied" in text:
            return ("%s: permission denied - add the user to the dialout group "
                    "(sudo usermod -aG dialout $USER, then log in again)" % target)
        if code == errno.ENOENT or "no such file" in text or "cannot find the file" in text:
            return "%s not found (unplugged? check meshtastic.serial_port)" % target
        return "%s: %s" % (target, _describe_exc(exc))


# ---------------------------------------------------------------------- module helpers


def _close_interface(iface: Any) -> None:
    """Close a library interface without raising or blocking for long.

    The library's ``close()`` can wait forever on a dead or hung node (it
    busy-waits for TX-queue space the radio will never report again, and
    drains the serial port), so it runs on a helper thread with a deadline.
    """

    def close() -> None:
        try:
            iface.close()
        except (Exception, SystemExit) as exc:
            log.debug("closing the radio interface failed: %s", _describe_exc(exc))

    if getattr(iface, "queueStatus", None) is not None:
        # "Unknown" TX-queue state counts as free space: releases any thread
        # (a sender, or close() itself) spinning in the library's queue wait.
        iface.queueStatus = None
    closer = threading.Thread(target=close, name="meshwx-radio-close", daemon=True)
    closer.start()
    closer.join(CLOSE_TIMEOUT_SECONDS)
    if closer.is_alive():
        log.warning("the radio interface did not close within %s; abandoning it", _secs(CLOSE_TIMEOUT_SECONDS))


def _accepted_kwargs(func: Any) -> Optional[FrozenSet[str]]:
    """Keyword names ``func`` accepts; None when it takes ``**kwargs`` or cannot be inspected."""
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return None
    keyword_kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return frozenset(p.name for p in params if p.kind in keyword_kinds)


def _my_node_num_of(iface: Any) -> Optional[int]:
    """``myInfo.my_node_num``, falling back to ``localNode.nodeNum``; None if unknown."""
    num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
    if not isinstance(num, int) or num <= 0:
        num = getattr(getattr(iface, "localNode", None), "nodeNum", None)
    return num if isinstance(num, int) and num > 0 else None


def _user_name(iface: Any, num: Optional[int], keys: Tuple[str, ...]) -> Optional[str]:
    """First non-empty ``keys`` field of ``iface.nodesByNum[num]["user"]``, if known."""
    nodes = getattr(iface, "nodesByNum", None)
    node = nodes.get(num) if isinstance(nodes, dict) and num is not None else None
    user = node.get("user") if isinstance(node, dict) else None
    if not isinstance(user, dict):
        return None
    for key in keys:
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _describe_exc(exc: BaseException) -> str:
    text = str(exc)
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


def _secs(seconds: float) -> str:
    return "%gs" % seconds
