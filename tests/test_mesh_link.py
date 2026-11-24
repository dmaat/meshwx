"""Tests for the Meshtastic link (with fake library interfaces) and the mesh package factory."""

from __future__ import annotations

import errno
import gc
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

pub = pytest.importorskip("pubsub.pub")

from meshwx.config import MeshtasticConfig  # noqa: E402
from meshwx.errors import MeshLinkError  # noqa: E402
from meshwx.mesh import ConsoleLink, MeshtasticLink, create_link, list_serial_ports  # noqa: E402
from meshwx.mesh import meshtastic_link as link_mod  # noqa: E402
from meshwx.mesh.meshtastic_link import default_interface_factory, parse_destination  # noqa: E402
from meshwx.models import IncomingMessage, OutgoingMessage  # noqa: E402

from conftest import ROOT, make_config  # noqa: E402

TEXT = "meshtastic.receive.text"
LOST = "meshtastic.connection.lost"
MY_NUM = 0xA1B2C3D4
BOB = 0x0000B0B0
BROADCAST_NUM = 0xFFFFFFFF
LOGGER = "meshwx.mesh.meshtastic_link"
FAST = dict(serial_port="/dev/ttyFAKE0", reconnect_min_seconds=0.01, reconnect_max_seconds=0.04)


# ============================================================================ fakes


class FakeInterface:
    """The parts of meshtastic's MeshInterface the link uses (2.7 sendText signature)."""

    def __init__(self, my_num: int = MY_NUM, nodes: Optional[Dict[int, Any]] = None,
                 fail_send: Optional[BaseException] = None) -> None:
        self.myInfo = SimpleNamespace(my_node_num=my_num)
        self.localNode = SimpleNamespace(nodeNum=my_num)
        self.nodesByNum: Dict[int, Any] = {
            my_num: {"num": my_num, "user": {"id": "!%08x" % my_num, "longName": "WX Bot", "shortName": "WX"}},
        }
        self.nodesByNum.update(nodes or {})
        self.fail_send = fail_send
        self.sent: List[Dict[str, Any]] = []
        self.closed = threading.Event()

    def sendText(self, text, destinationId="^all", wantAck=False, wantResponse=False, onResponse=None,
                 channelIndex=0, portNum=1, replyId=None, hopLimit=None):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(dict(text=text, destinationId=destinationId, wantAck=wantAck,
                              channelIndex=channelIndex, replyId=replyId, hopLimit=hopLimit))
        return SimpleNamespace(id=len(self.sent))

    def close(self) -> None:
        self.closed.set()


class OldFakeInterface(FakeInterface):
    """An older library whose sendText has neither replyId nor hopLimit."""

    def sendText(self, text, destinationId="^all", wantAck=False, wantResponse=False, onResponse=None,
                 channelIndex=0):
        self.sent.append(dict(text=text, destinationId=destinationId, wantAck=wantAck, channelIndex=channelIndex))


class HungInterface(FakeInterface):
    """sendText spins like the library waiting for TX-queue space a hung node never reports."""

    def __init__(self) -> None:
        super().__init__()
        self.queueStatus: Any = SimpleNamespace(free=0)

    def sendText(self, text, **kwargs):
        while self.queueStatus is not None:  # the link's teardown resets it
            time.sleep(0.005)
        # ...then the library "writes" to its already-closed port: nothing sent, no error


class DyingInterface(FakeInterface):
    """The port dies while a packet is being written; sendText itself returns normally."""

    def sendText(self, text, **kwargs):
        pub.sendMessage(LOST, interface=self)


class FakeFactory:
    """interface_factory stand-in: returns (or raises) the given outcomes in order; the last one repeats."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, kind: str, **kwargs: Any) -> Any:
        self.calls.append((kind, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def wait_for(pred: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def packet(text: Optional[str], frm: int = BOB, to: int = BROADCAST_NUM, **extra: Any) -> Dict[str, Any]:
    """A packet dict shaped like the library's (protobuf JSON names, zero values omitted)."""
    decoded: Dict[str, Any] = {"portnum": "TEXT_MESSAGE_APP", "payload": (text or "").encode("utf-8")}
    if text is not None:
        decoded["text"] = text
    pkt: Dict[str, Any] = {"from": frm, "to": to, "decoded": decoded, "id": 1234, "fromId": None, "toId": "^all"}
    pkt.update(extra)
    return pkt


@pytest.fixture
def make_link():
    links: List[MeshtasticLink] = []

    def make(factory: Any, port_override: Optional[str] = None, **cfg: Any) -> MeshtasticLink:
        link = MeshtasticLink(MeshtasticConfig(**{**FAST, **cfg}), port_override=port_override,
                              interface_factory=factory)
        links.append(link)
        return link

    yield make
    for link in links:
        link.stop()


# ============================================================================ connect / describe


def test_connects_and_describes(make_link):
    iface = FakeInterface()
    factory = FakeFactory(iface)
    link = make_link(factory)
    events: List[bool] = []
    link.on_connection = events.append
    assert link.describe() == "serial /dev/ttyFAKE0" and link.my_node_num() is None
    link.start()
    assert link.wait_connected(1.0) and link.is_connected()
    assert factory.calls == [("serial", {"devPath": "/dev/ttyFAKE0"})]
    assert wait_for(lambda: events == [True])
    assert link.my_node_num() == MY_NUM
    assert link.describe() == 'serial /dev/ttyFAKE0 !a1b2c3d4 "WX Bot"'
    topic = pub.getDefaultTopicMgr().getTopic(TEXT)
    assert topic.hasListener(link._on_receive)
    link.stop()
    assert iface.closed.is_set() and not link.is_connected()
    assert events == [True]  # an intentional stop is not reported as a link loss
    assert not topic.hasListener(link._on_receive)
    link.stop()  # idempotent


def test_target_resolution(make_link):
    factory = FakeFactory(FakeInterface())
    link = make_link(factory, port_override="/dev/ttyOVR", serial_port="/dev/ttyCFG", skip_node_db=True)
    link.start()
    assert link.wait_connected(1.0)
    assert factory.calls == [("serial", {"devPath": "/dev/ttyOVR", "noNodes": True})]

    factory = FakeFactory(FakeInterface())
    link = make_link(factory, connection="tcp", tcp_host="10.0.0.5", tcp_port=4404)
    link.start()
    assert link.wait_connected(1.0)
    assert factory.calls == [("tcp", {"hostname": "10.0.0.5", "portNumber": 4404})]
    assert link.describe().startswith("tcp 10.0.0.5:4404 !a1b2c3d4")


def test_no_device_found_never_passes_devpath_none(make_link, monkeypatch, caplog):
    util = pytest.importorskip("meshtastic.util")
    found: List[str] = []
    monkeypatch.setattr(util, "findPorts", lambda *a, **k: list(found))
    caplog.set_level(logging.WARNING, logger=LOGGER)
    factory = FakeFactory(FakeInterface())
    link = make_link(factory, serial_port="")
    link.start()
    assert wait_for(lambda: "no Meshtastic device found; is it plugged in? retrying in 0.01s" in caplog.text)
    assert factory.calls == []
    assert not any(r.exc_info for r in caplog.records)  # tracebacks only at DEBUG
    found.extend(["/dev/ttyACM0", "/dev/ttyUSB0"])
    assert link.wait_connected(1.0)
    assert factory.calls == [("serial", {"devPath": "/dev/ttyACM0"})]
    assert "several serial ports found (/dev/ttyACM0, /dev/ttyUSB0); using /dev/ttyACM0" in caplog.text


def _serial_error(code: int, text: str) -> BaseException:
    serial = pytest.importorskip("serial")
    return serial.SerialException(code, text)


@pytest.mark.parametrize(
    "make_exc, hint",
    [
        (lambda: PermissionError(errno.EACCES, "Permission denied: '/dev/ttyFAKE0'"), "dialout group"),
        (lambda: _serial_error(errno.EAGAIN, "Could not exclusively lock port /dev/ttyFAKE0"),
         "another program (meshtastic CLI? another meshwx?)"),
        (lambda: Exception("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"),
         "another program"),
        (lambda: FileNotFoundError(errno.ENOENT, "No such file or directory"), "not found (unplugged?"),
        (lambda: RuntimeError("Timed out waiting for connection completion"), "skip_node_db: true"),
        (lambda: SystemExit(1), "gave up (exit code 1)"),
    ],
)
def test_connect_failures_are_explained(make_link, caplog, make_exc, hint):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    link = make_link(FakeFactory(make_exc()))
    link.start()
    assert wait_for(lambda: hint in caplog.text), caplog.text
    assert "; retrying in 0.01s" in caplog.text
    assert not link.is_connected()


def test_timeout_hint_when_the_node_db_is_already_skipped(make_link, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    link = make_link(FakeFactory(RuntimeError("Timed out waiting for interface config")), skip_node_db=True)
    link.start()
    assert wait_for(lambda: "node did not answer (is it a Meshtastic node, and not hung?)" in caplog.text)
    assert "skip_node_db: true" not in caplog.text


def test_connect_failure_traceback_only_at_debug(make_link, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    link = make_link(FakeFactory(OSError("boom")))
    link.start()
    assert wait_for(lambda: any(r.levelno == logging.WARNING and r.exc_info for r in caplog.records))


@pytest.mark.parametrize(
    "exc, hint",
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
         "cannot reach tcp radio.lan:4403: ConnectionRefusedError"),
        (BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"), "cannot reach tcp radio.lan:4403"),
        (RuntimeError("Timed out waiting for connection completion"), "tcp radio.lan:4403: node did not answer"),
    ],
)
def test_tcp_failures_are_explained(make_link, caplog, exc, hint):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    link = make_link(FakeFactory(exc), connection="tcp", tcp_host="radio.lan")
    link.start()
    assert wait_for(lambda: hint in caplog.text), caplog.text
    assert "another program" not in caplog.text  # serial-only hints stay out of TCP errors


def test_backoff_doubles_up_to_max(make_link, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    link = make_link(FakeFactory(OSError("boom")), reconnect_min_seconds=0.01, reconnect_max_seconds=0.02)
    link.start()
    assert wait_for(lambda: caplog.text.count("retrying in 0.02s") >= 2)
    assert caplog.text.count("retrying in 0.01s") == 1


def test_backoff_resets_after_a_successful_connection(make_link, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    iface = FakeInterface()
    link = make_link(FakeFactory(OSError("boom"), OSError("boom"), iface, OSError("boom")),
                     reconnect_min_seconds=0.01, reconnect_max_seconds=1)
    link.start()
    assert link.wait_connected(1.0)
    assert re.findall(r"retrying in (\S+)", caplog.text) == ["0.01s", "0.02s"]
    pub.sendMessage(LOST, interface=iface)
    assert wait_for(lambda: len(re.findall(r"retrying in (\S+)", caplog.text)) >= 4)
    assert re.findall(r"retrying in (\S+)", caplog.text)[2:4] == ["0.01s", "0.02s"]


@pytest.mark.parametrize("stable_after, delays", [(60.0, ["0.01s", "0.02s", "0.04s"]),  # flapping: back off
                                                  (0.0, ["0.01s", "0.01s", "0.01s"])])  # links that worked
def test_reconnect_delay_after_a_lost_link(make_link, caplog, monkeypatch, stable_after, delays):
    monkeypatch.setattr(link_mod, "STABLE_LINK_SECONDS", stable_after)
    caplog.set_level(logging.INFO, logger=LOGGER)
    ifaces = [FakeInterface() for _ in range(4)]
    factory = FakeFactory(*ifaces)
    link = make_link(factory, reconnect_min_seconds=0.01, reconnect_max_seconds=0.04)
    link.start()
    for n, iface in enumerate(ifaces[:3], start=1):
        assert wait_for(lambda: len(factory.calls) == n and link.is_connected())
        pub.sendMessage(LOST, interface=iface)
    assert wait_for(lambda: len(factory.calls) == 4 and link.is_connected())
    assert re.findall(r"reconnecting to the radio in (\S+)", caplog.text) == delays


def test_stop_interrupts_backoff(make_link, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    link = make_link(FakeFactory(OSError("boom")), reconnect_min_seconds=30, reconnect_max_seconds=60)
    link.start()
    assert wait_for(lambda: "retrying in 30s" in caplog.text)
    t0 = time.monotonic()
    link.stop()
    assert time.monotonic() - t0 < 0.5


def test_stop_during_slow_connect_closes_the_late_interface(make_link):
    iface = FakeInterface()
    entered, release = threading.Event(), threading.Event()

    def slow_factory(kind: str, **kwargs: Any) -> FakeInterface:
        entered.set()
        release.wait(2)
        return iface

    link = make_link(slow_factory)
    link.start()
    assert entered.wait(1)
    threading.Timer(0.05, release.set).start()
    link.stop()  # the constructor returns while stopping: its interface must be closed
    assert iface.closed.is_set() and not link.is_connected()


# ============================================================================ receive


def test_incoming_text_dispatch(make_link):
    iface = FakeInterface(nodes={BOB: {"num": BOB, "user": {"shortName": "BOB", "longName": "Bob Base"}}})
    link = make_link(FakeFactory(iface))
    got: List[IncomingMessage] = []
    link.on_message = got.append
    link.start()
    assert link.wait_connected(1.0)
    gc.collect()  # pypubsub holds weak references: the listeners must survive a GC pass

    pub.sendMessage(TEXT, interface=iface, packet=packet(
        "wx 02108", frm=BOB, to=MY_NUM, id=42, rxTime=1700000000, hopStart=3, hopLimit=1, viaMqtt=True))
    pub.sendMessage(TEXT, interface=iface, packet=packet("!fc", frm=0x00001234, channel=2))

    assert got[0] == IncomingMessage(
        text="wx 02108", from_num=BOB, from_id="!0000b0b0", to_num=MY_NUM, channel_index=0, is_dm=True,
        packet_id=42, from_name="BOB", rx_time=datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
        via_mqtt=True, hops_away=2,
    )
    second = got[1]
    assert (second.text, second.from_id, second.to_num) == ("!fc", "!00001234", BROADCAST_NUM)
    assert second.channel_index == 2 and not second.is_dm
    assert second.from_name is None and second.rx_time is None and second.hops_away is None
    assert not second.via_mqtt
    assert link.node_name(BOB) == "BOB"


@pytest.mark.parametrize(
    "extra, hops",
    [({"hopStart": 3}, 3),  # protobuf JSON omits hopLimit == 0
     ({"hopStart": 3, "hopLimit": 3}, 0),
     ({"hopLimit": 3}, None),  # no hopStart: firmware too old to tell
     ({}, None)],
)
def test_hops_away(make_link, extra, hops):
    iface = FakeInterface()
    link = make_link(FakeFactory(iface))
    got: List[IncomingMessage] = []
    link.on_message = got.append
    link.start()
    assert link.wait_connected(1.0)
    pub.sendMessage(TEXT, interface=iface, packet=packet("ping", **extra))
    assert got[0].hops_away == hops


def test_incoming_filters(make_link, caplog):
    iface = FakeInterface()
    link = make_link(FakeFactory(iface))
    got: List[IncomingMessage] = []
    link.on_message = got.append
    link.start()
    assert link.wait_connected(1.0)

    pub.sendMessage(TEXT, interface=iface, packet=packet("my own echo", frm=MY_NUM))
    pub.sendMessage(TEXT, interface=FakeInterface(), packet=packet("from another interface"))
    pub.sendMessage(TEXT, interface=iface, packet=packet(None))  # undecodable UTF-8: no "text"
    pub.sendMessage(TEXT, interface=iface, packet=packet(""))
    pub.sendMessage(TEXT, interface=iface, packet=packet("someone else's DM", to=0x00005555))
    assert got == []

    def broken_handler(msg: IncomingMessage) -> None:
        raise RuntimeError("app bug")

    link.on_message = broken_handler
    pub.sendMessage(TEXT, interface=iface, packet=packet("hello"))  # must not raise into the library
    assert "failed to handle an incoming text packet" in caplog.text


# ============================================================================ loss / reconnect


def test_connection_lost_reconnects(make_link):
    first, second = FakeInterface(), FakeInterface()
    factory = FakeFactory(first, second)
    link = make_link(factory)
    events: List[bool] = []
    got: List[IncomingMessage] = []
    link.on_connection = events.append
    link.on_message = got.append
    link.start()
    assert link.wait_connected(1.0)

    pub.sendMessage(LOST, interface=FakeInterface())  # some other interface: ignored
    assert link.is_connected()
    pub.sendMessage(LOST, interface=first)
    assert not link.is_connected()
    assert wait_for(lambda: events == [True, False, True])
    assert first.closed.is_set() and len(factory.calls) == 2

    pub.sendMessage(LOST, interface=first)  # late event from the old interface
    pub.sendMessage(TEXT, interface=first, packet=packet("stale"))
    pub.sendMessage(TEXT, interface=second, packet=packet("fresh"))
    assert link.is_connected() and len(factory.calls) == 2
    assert [m.text for m in got] == ["fresh"]


# ============================================================================ send


def test_send_kwargs_and_limits(make_link):
    iface = FakeInterface()
    link = make_link(FakeFactory(iface), max_message_bytes=10, hop_limit=3)
    with pytest.raises(MeshLinkError):
        link.send(OutgoingMessage("early"))
    link.start()
    assert link.wait_connected(1.0)

    with pytest.raises(ValueError):
        link.send(OutgoingMessage("é" * 6))  # 12 UTF-8 bytes > 10
    with pytest.raises(ValueError):
        link.send(OutgoingMessage("hi", destination="bob"))
    link.send(OutgoingMessage("hi", destination=0xDEADBEEF, channel_index=2, reply_id=77, want_ack=True))
    link.send(OutgoingMessage("all"))
    link.send(OutgoingMessage("id", destination="!0000b0b0"))
    assert iface.sent == [
        dict(text="hi", destinationId=0xDEADBEEF, wantAck=True, channelIndex=2, replyId=77, hopLimit=3),
        dict(text="all", destinationId="^all", wantAck=False, channelIndex=0, replyId=None, hopLimit=3),
        dict(text="id", destinationId=BOB, wantAck=False, channelIndex=0, replyId=None, hopLimit=3),
    ]
    assert link.is_connected()  # rejected packets do not take the link down


def test_send_filters_kwargs_an_old_library_lacks(make_link, caplog):
    iface = OldFakeInterface()
    link = make_link(FakeFactory(iface), hop_limit=2)
    link.start()
    assert link.wait_connected(1.0)
    link.send(OutgoingMessage("one", reply_id=5))
    link.send(OutgoingMessage("two", reply_id=6))
    assert iface.sent == [
        dict(text="one", destinationId="^all", wantAck=False, channelIndex=0),
        dict(text="two", destinationId="^all", wantAck=False, channelIndex=0),
    ]
    assert caplog.text.count("cannot set a hop limit") == 1


def test_send_failure_marks_down_and_reconnects(make_link):
    first = FakeInterface(fail_send=OSError(errno.EIO, "Input/output error"))
    second = FakeInterface()
    link = make_link(FakeFactory(first, second))
    events: List[bool] = []
    link.on_connection = events.append
    link.start()
    assert link.wait_connected(1.0)
    with pytest.raises(MeshLinkError) as info:
        link.send(OutgoingMessage("lost"))
    assert isinstance(info.value.__cause__, OSError)
    assert wait_for(lambda: first.closed.is_set() and link.is_connected())
    assert events == [True, False, True]
    link.send(OutgoingMessage("again"))
    assert [s["text"] for s in second.sent] == ["again"]


def test_hung_send_times_out_and_reconnects(make_link, monkeypatch, caplog):
    monkeypatch.setattr(link_mod, "SEND_TIMEOUT_SECONDS", 0.05)
    hung, fresh = HungInterface(), FakeInterface()
    link = make_link(FakeFactory(hung, fresh))
    events: List[bool] = []
    link.on_connection = events.append
    link.start()
    assert link.wait_connected(1.0)
    t0 = time.monotonic()
    with pytest.raises(MeshLinkError, match="lost while sending"):
        link.send(OutgoingMessage("stuck"))
    assert time.monotonic() - t0 < 0.5
    assert "send still blocked after 0.05s (node hung?)" in caplog.text
    assert wait_for(lambda: link.is_connected() and events == [True, False, True])
    link.send(OutgoingMessage("again"))
    assert [s["text"] for s in fresh.sent] == ["again"] and hung.closed.is_set()


def test_send_that_races_a_link_loss_is_reported(make_link):
    dying, fresh = DyingInterface(), FakeInterface()
    link = make_link(FakeFactory(dying, fresh))
    link.start()
    assert link.wait_connected(1.0)
    with pytest.raises(MeshLinkError, match="lost while sending"):
        link.send(OutgoingMessage("maybe"))
    assert wait_for(lambda: dying.closed.is_set() and link.is_connected())


@pytest.mark.parametrize("dest, expected", [
    (0xDEADBEEF, 0xDEADBEEF), (BROADCAST_NUM, BROADCAST_NUM), ("^all", "^all"),
    ("!0000b0b0", BOB), ("0000B0B0", BOB), (" !a1b2c3d4 ", MY_NUM),
])
def test_parse_destination(dest, expected):
    assert parse_destination(dest) == expected


@pytest.mark.parametrize("dest", ["bob", "beef", "!b0b0", "!a1b2c3d4e", "", "^local", 0, -1, 0x100000000,
                                  True, None, 1.5])
def test_parse_destination_rejects(dest):
    with pytest.raises(ValueError):
        parse_destination(dest)


def test_library_sys_exit_in_send_becomes_link_error(make_link):
    link = make_link(FakeFactory(FakeInterface(fail_send=SystemExit(1)), FakeInterface()))
    link.start()
    assert link.wait_connected(1.0)
    with pytest.raises(MeshLinkError):
        link.send(OutgoingMessage("x"))


# ============================================================================ real library (no radio)


def test_real_library_packet_path_and_events(make_link):
    """End to end through meshtastic's own packet decoding, publishing thread and sendText."""
    mesh_interface = pytest.importorskip("meshtastic.mesh_interface")
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    def radio_less() -> Any:
        iface = mesh_interface.MeshInterface(noProto=True)  # never touches a port; sends are skipped
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=MY_NUM)
        iface.nodes, iface.nodesByNum = {}, {}
        return iface

    first, second = radio_less(), radio_less()
    factory = FakeFactory(first, second)
    link = make_link(factory, hop_limit=2)
    got: List[IncomingMessage] = []
    link.on_message = got.append
    link.start()
    assert link.wait_connected(1.0) and link.my_node_num() == MY_NUM

    pkt = mesh_pb2.MeshPacket(**{"from": BOB, "to": MY_NUM, "id": 42, "rx_time": 1700000000,
                                 "hop_start": 3, "hop_limit": 0})  # hop_limit 0: omitted from the dict
    pkt.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    pkt.decoded.payload = "wx 02108".encode("utf-8")
    first._handlePacketFromRadio(pkt)  # what the reader thread does for a received packet
    assert wait_for(lambda: len(got) == 1)
    msg = got[0]
    assert (msg.text, msg.from_id, msg.to_num, msg.is_dm, msg.channel_index) == ("wx 02108", "!0000b0b0", MY_NUM,
                                                                                   True, 0)
    assert (msg.packet_id, msg.hops_away, msg.via_mqtt) == (42, 3, False)
    assert msg.rx_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert msg.from_name == "b0b0"  # the library's placeholder entry for a node it has not met

    link.send(OutgoingMessage("pong", destination=BOB, reply_id=42))  # real sendText signature
    first._disconnected()  # what the reader thread does when the port dies
    assert wait_for(lambda: len(factory.calls) == 2 and link.is_connected())


# ============================================================================ default factory / package


def test_default_factory_drops_unsupported_kwargs(monkeypatch, caplog):
    serial_mod = pytest.importorskip("meshtastic.serial_interface")
    tcp_mod = pytest.importorskip("meshtastic.tcp_interface")
    created: List[Tuple[Any, ...]] = []

    class OldSerial:  # predates noNodes
        def __init__(self, devPath=None, debugOut=None, noProto=False, connectNow=True):
            created.append(("serial", devPath))

    class NewTcp:
        def __init__(self, hostname, debugOut=None, portNumber=4403, noNodes=False):
            created.append(("tcp", hostname, portNumber, noNodes))

    monkeypatch.setattr(serial_mod, "SerialInterface", OldSerial)
    monkeypatch.setattr(tcp_mod, "TCPInterface", NewTcp)
    default_interface_factory("serial", devPath="/dev/ttyX", noNodes=True)
    default_interface_factory("tcp", hostname="radio.lan", portNumber=4403, noNodes=True)
    assert created == [("serial", "/dev/ttyX"), ("tcp", "radio.lan", 4403, True)]
    assert "does not support noNodes" in caplog.text
    with pytest.raises(ValueError):
        default_interface_factory("ble")
    with pytest.raises(ValueError):  # devPath=None would make the library auto-detect (or sys.exit)
        default_interface_factory("serial", devPath=None)
    assert len(created) == 2


def test_importing_the_package_does_not_import_meshtastic():
    code = "import sys, meshwx.mesh; print(sorted(m for m in ('meshtastic', 'pubsub') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_create_link():
    cfg = make_config({"meshtastic": {"max_message_bytes": 150, "serial_port": "/dev/ttyUSB0"}})
    dry = create_link(cfg, dry_run=True)
    assert isinstance(dry, ConsoleLink) and dry.max_bytes == 150
    real = create_link(cfg, port_override="/dev/ttyACM9")
    assert isinstance(real, MeshtasticLink) and real.describe() == "serial /dev/ttyACM9"
    assert not real.is_connected()  # nothing happens before start()


def test_list_serial_ports(monkeypatch):
    list_ports = pytest.importorskip("serial.tools.list_ports")
    util = pytest.importorskip("meshtastic.util")
    rak = next(iter(util.whitelistVids))
    blocked = next(iter(util.blacklistVids))
    ports = [
        SimpleNamespace(device="/dev/ttyUSB0", description="CP2102 USB to UART", vid=0x10C4),
        SimpleNamespace(device="/dev/ttyAMA0", description="ttyAMA0", vid=None),
        SimpleNamespace(device="/dev/ttyACM1", description="J-Link", vid=blocked),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: list(ports))
    # no known Meshtastic vendor: like auto-detect, any non-blacklisted USB port is a candidate
    assert list_serial_ports() == [
        ("/dev/ttyACM1", "J-Link", False),
        ("/dev/ttyAMA0", "ttyAMA0", False),
        ("/dev/ttyUSB0", "CP2102 USB to UART", True),
    ]
    ports.append(SimpleNamespace(device="/dev/ttyACM0", description="RAK4631", vid=rak))
    assert [(d, likely) for d, _, likely in list_serial_ports()] == [
        ("/dev/ttyACM0", True), ("/dev/ttyACM1", False), ("/dev/ttyAMA0", False), ("/dev/ttyUSB0", False),
    ]


def test_list_serial_ports_without_meshtastic(monkeypatch):
    list_ports = pytest.importorskip("serial.tools.list_ports")
    monkeypatch.setitem(sys.modules, "meshtastic.util", None)  # import fails
    monkeypatch.setattr(list_ports, "comports",
                        lambda: [SimpleNamespace(device="COM3", description=None, vid=0x239A)])
    assert list_serial_ports() == [("COM3", "", False)]

