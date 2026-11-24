"""Tests for ConsoleLink, the dry-run stand-in for the radio."""

from __future__ import annotations

import io
import queue
import threading
import time
from typing import Callable, List

import pytest

from meshwx.errors import MeshLinkError
from meshwx.mesh.console_link import ConsoleLink
from meshwx.models import IncomingMessage, OutgoingMessage

ME = 0x12345678
FAKE = 0x0000BEEF
BROADCAST_NUM = 0xFFFFFFFF
READER = "meshwx-console-input"


def wait_for(pred: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def start_and_get_reader(link: ConsoleLink) -> List[threading.Thread]:
    """Start ``link`` and return its input thread (empty if it already finished)."""
    before = set(threading.enumerate())
    link.start()
    return [t for t in threading.enumerate() if t not in before and t.name == READER]


class LineFeed:
    """A stdin stand-in whose readline() blocks until a test feeds it a line ("" = EOF)."""

    def __init__(self) -> None:
        self.lines: "queue.Queue[str]" = queue.Queue()

    def readline(self) -> str:
        return self.lines.get(timeout=2)


# ============================================================================ sending


def test_send_prints_packets():
    out = io.StringIO()
    link = ConsoleLink(20, output_stream=out, read_input=False)
    events: List[bool] = []
    link.on_connection = events.append
    assert not link.is_connected() and not link.wait_connected(0)
    with pytest.raises(MeshLinkError):
        link.send(OutgoingMessage("too early"))
    link.start()
    assert link.is_connected() and link.wait_connected(0) and events == [True]

    link.send(OutgoingMessage("hello"))
    link.send(OutgoingMessage("re: wx", destination=FAKE, channel_index=2, reply_id=7, want_ack=True))
    link.send(OutgoingMessage("by id", destination="!a1b2c3d4"))
    link.send(OutgoingMessage("all by num", destination=BROADCAST_NUM, channel_index=1))
    link.send(OutgoingMessage("22°F"))
    assert out.getvalue().splitlines() == [
        "[TX ch0 -> ^all] hello  (5B)",
        "[TX ch2 -> !0000beef] re: wx  (6B)",
        "[TX ch0 -> !a1b2c3d4] by id  (5B)",
        "[TX ch1 -> ^all] all by num  (10B)",
        "[TX ch0 -> ^all] 22°F  (5B)",
    ]


def test_send_validates_like_the_real_link():
    out = io.StringIO()
    link = ConsoleLink(20, output_stream=out, read_input=False)
    link.start()
    link.send(OutgoingMessage("x" * 20))
    with pytest.raises(ValueError):
        link.send(OutgoingMessage("x" * 21))
    with pytest.raises(ValueError):
        link.send(OutgoingMessage("é" * 11))  # 22 UTF-8 bytes
    with pytest.raises(ValueError):
        link.send(OutgoingMessage("hi", destination="bob"))
    assert len(out.getvalue().splitlines()) == 1


def test_stop_is_idempotent_and_disconnects():
    link = ConsoleLink(200, output_stream=io.StringIO(), read_input=False)
    events: List[bool] = []
    link.on_connection = events.append
    link.start()
    link.stop()
    link.stop()
    assert not link.is_connected() and events == [True]  # like the real link: a stop is no "link lost"
    with pytest.raises(MeshLinkError):
        link.send(OutgoingMessage("late"))


def test_default_output_is_stdout(capsys):
    link = ConsoleLink(200, read_input=False)
    link.start()
    link.send(OutgoingMessage("hi"))
    assert capsys.readouterr().out == "[TX ch0 -> ^all] hi  (2B)\n"


def test_output_survives_a_console_that_cannot_encode():
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="ascii")
    link = ConsoleLink(200, output_stream=out, read_input=False)
    link.start()
    link.send(OutgoingMessage("22°F"))
    out.flush()
    assert raw.getvalue().decode("ascii").splitlines() == ["[TX ch0 -> ^all] 22?F  (5B)"]


def test_identity():
    link = ConsoleLink(200, read_input=False)
    assert link.my_node_num() == ME
    assert link.describe() == "console (dry run) !12345678"
    assert link.node_name(FAKE) == "console" and link.node_name(1) is None


def test_connection_handler_errors_are_contained(caplog):
    link = ConsoleLink(200, output_stream=io.StringIO(), read_input=False)

    def broken(up: bool) -> None:
        raise RuntimeError("app bug")

    link.on_connection = broken
    link.start()
    assert link.is_connected() and "on_connection handler failed" in caplog.text


# ============================================================================ input


def test_input_lines_become_messages(caplog):
    inp = io.StringIO("wx 02108\n\n   \n#2 !fc\n#9 no such channel\nping")
    got: List[IncomingMessage] = []
    link = ConsoleLink(200, input_stream=inp, output_stream=io.StringIO())
    link.on_message = got.append
    readers = start_and_get_reader(link)
    assert wait_for(lambda: len(got) == 4)
    for reader in readers:  # EOF ends the reader quietly
        reader.join(1.0)
        assert not reader.is_alive()
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]

    dm, channel, odd, last = got
    assert (dm.text, dm.from_num, dm.from_id, dm.to_num) == ("wx 02108", FAKE, "!0000beef", ME)
    assert dm.is_dm and dm.channel_index == 0 and dm.from_name == "console" and dm.hops_away == 0
    assert dm.rx_time is not None and dm.rx_time.tzinfo is not None and not dm.via_mqtt
    assert (channel.text, channel.channel_index, channel.to_num, channel.is_dm) == ("!fc", 2, BROADCAST_NUM, False)
    assert odd.text == "#9 no such channel" and odd.is_dm  # channels are 0-7: a plain DM
    assert last.text == "ping"  # no trailing newline needed
    assert len({m.packet_id for m in got}) == 4


def test_handler_errors_do_not_stop_the_reader(caplog):
    seen: List[str] = []

    def handler(msg: IncomingMessage) -> None:
        seen.append(msg.text)
        if msg.text == "boom":
            raise RuntimeError("app bug")

    link = ConsoleLink(200, input_stream=io.StringIO("boom\nok\n"), output_stream=io.StringIO())
    link.on_message = handler
    link.start()
    assert wait_for(lambda: seen == ["boom", "ok"])
    assert "message handler failed" in caplog.text


def test_lines_after_stop_are_ignored():
    feed = LineFeed()
    got: List[IncomingMessage] = []
    link = ConsoleLink(200, input_stream=feed, output_stream=io.StringIO())
    link.on_message = got.append
    readers = start_and_get_reader(link)
    feed.lines.put("first\n")
    assert wait_for(lambda: [m.text for m in got] == ["first"])
    link.stop()
    feed.lines.put("second\n")  # the blocked reader wakes up, sees the stop and ends
    for reader in readers:
        reader.join(1.0)
        assert not reader.is_alive()
    assert [m.text for m in got] == ["first"]


def test_no_reader_without_read_input():
    inp = io.StringIO("wx\n")
    got: List[IncomingMessage] = []
    link = ConsoleLink(200, input_stream=inp, output_stream=io.StringIO(), read_input=False)
    link.on_message = got.append
    assert start_and_get_reader(link) == []
    time.sleep(0.03)
    assert got == [] and inp.tell() == 0
