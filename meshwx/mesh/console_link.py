"""Dry-run stand-in for the radio: prints what would be sent, reads fake messages.

Every input line becomes a DM to us from a fake node (``!0000beef``); a line
``#N text`` becomes a broadcast on channel N instead.  ``meshwx run --dry-run``
uses it to exercise the whole app without a radio.
"""

from __future__ import annotations

import itertools
import logging
import re
import sys
import threading
from typing import Optional, TextIO, Union

from ..errors import MeshLinkError
from ..models import BROADCAST, IncomingMessage, OutgoingMessage, node_id_from_num
from ..timeutil import utcnow
from .base import MeshLink
from .meshtastic_link import BROADCAST_NUM, parse_destination

log = logging.getLogger(__name__)

MY_NODE_NUM = 0x12345678
FAKE_NODE_NUM = 0x0000BEEF
FAKE_NODE_NAME = "console"

_CHANNEL_LINE = re.compile(r"#([0-7])\s+(\S.*)")


class ConsoleLink(MeshLink):
    """:class:`MeshLink` that talks to a terminal instead of a radio.

    It is "connected" from :meth:`start` until :meth:`stop`.  Outgoing packets
    are printed as ``[TX ch{idx} -> {dest}] {text}  ({n}B)`` and are validated
    like the real link does (size limit, destination), so a dry run catches the
    same mistakes.
    """

    def __init__(
        self,
        max_bytes: int,
        *,
        input_stream: Optional[TextIO] = None,
        output_stream: Optional[TextIO] = None,
        read_input: bool = True,
    ) -> None:
        super().__init__()
        self.max_bytes = max_bytes
        self._input = input_stream  # None = sys.stdin, resolved when reading starts
        self._output = output_stream  # None = sys.stdout, resolved per print
        self._read_input = read_input
        self._connected = threading.Event()
        self._stopped = threading.Event()
        self._started = False
        self._print_lock = threading.Lock()
        self._packet_ids = itertools.count(1)

    # ------------------------------------------------------------------ MeshLink API
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._connected.set()
        log.info("dry run: packets are printed instead of sent")
        handler = self.on_connection
        if handler is not None:
            try:
                handler(True)
            except Exception:  # noqa: BLE001 - same contract as the real link
                log.exception("on_connection handler failed")
        if self._read_input:
            threading.Thread(target=self._read_loop, name="meshwx-console-input", daemon=True).start()

    def stop(self) -> None:
        """Idempotent.  A reader blocked on input ends at its next line or EOF."""
        self._stopped.set()
        self._connected.clear()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def send(self, msg: OutgoingMessage) -> None:
        size = len(msg.text.encode("utf-8"))
        if size > self.max_bytes:
            raise ValueError("message is %d bytes, over the %d-byte limit" % (size, self.max_bytes))
        dest = parse_destination(msg.destination)
        if not self._connected.is_set():
            raise MeshLinkError("console link is not running")
        self._print("[TX ch%d -> %s] %s  (%dB)" % (msg.channel_index, _dest_label(dest), msg.text, size))

    def my_node_num(self) -> Optional[int]:
        return MY_NODE_NUM

    def node_name(self, num: int) -> Optional[str]:
        return FAKE_NODE_NAME if num == FAKE_NODE_NUM else None

    def describe(self) -> str:
        return "console (dry run) %s" % node_id_from_num(MY_NODE_NUM)

    # ------------------------------------------------------------------ internals
    def _read_loop(self) -> None:
        stream = self._input if self._input is not None else sys.stdin
        if stream is None:  # no stdin at all (e.g. pythonw)
            return
        try:
            for line in iter(stream.readline, ""):
                if self._stopped.is_set():
                    return
                msg = self._parse(line)
                if msg is not None:
                    self._deliver(msg)
        except (OSError, ValueError) as exc:  # closed or unreadable stream
            log.debug("console input ended: %s", exc)

    def _parse(self, line: str) -> Optional[IncomingMessage]:
        text = line.strip()
        if not text:
            return None
        channel, to_num = 0, MY_NODE_NUM
        m = _CHANNEL_LINE.fullmatch(text)
        if m:
            channel, text, to_num = int(m.group(1)), m.group(2), BROADCAST_NUM
        return IncomingMessage(
            text=text,
            from_num=FAKE_NODE_NUM,
            from_id=node_id_from_num(FAKE_NODE_NUM),
            to_num=to_num,
            channel_index=channel,
            is_dm=to_num == MY_NODE_NUM,
            packet_id=next(self._packet_ids),
            from_name=FAKE_NODE_NAME,
            rx_time=utcnow(),
            hops_away=0,
        )

    def _deliver(self, msg: IncomingMessage) -> None:
        handler = self.on_message
        if handler is None:
            log.debug("no message handler; ignoring %r", msg.text)
            return
        try:
            handler(msg)
        except Exception:  # noqa: BLE001 - keep reading input
            log.exception("message handler failed")

    def _print(self, line: str) -> None:
        out = self._output if self._output is not None else sys.stdout
        with self._print_lock:
            try:
                print(line, file=out, flush=True)
            except UnicodeEncodeError:  # e.g. a redirected Windows console without UTF-8
                encoding = getattr(out, "encoding", None) or "ascii"
                print(line.encode(encoding, "replace").decode(encoding), file=out, flush=True)


def _dest_label(dest: Union[int, str]) -> str:
    """``"^all"`` or ``"!xxxxxxxx"`` for a destination from :func:`parse_destination`."""
    if dest == BROADCAST or dest == BROADCAST_NUM:
        return BROADCAST
    return node_id_from_num(int(dest))
