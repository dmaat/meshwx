"""Mesh text commands: parse, rate-limit, run, and address the replies.

:meth:`CommandBot.handle` turns one :class:`~meshwx.models.IncomingMessage` into
zero or more :class:`~meshwx.models.OutgoingMessage` packets - one per chunk
returned by the formatter, so byte limits and ASCII folding always apply.  A
failing command never raises: it becomes a one-packet error reply carrying
the exception's ``user_message``.

Rules (docs/ARCHITECTURE.md, "Mesh commands"):

* channel messages need ``commands.prefix`` (default ``!``) and must arrive on
  a channel listed in ``commands.channels``; DMs work with or without it;
* command words and aliases are case-insensitive, and trailing punctuation
  from phone keyboards ("Wx.", "help?") is ignored; an unknown word gets a
  hint in a DM and is ignored on a channel (people chat there), as are
  emoji-only DMs (Meshtastic sends tapback reactions as text);
* each node may run one command per ``commands.cooldown_seconds`` (admins are
  exempt).  A throttled DM gets a single "Please wait Ns"; a throttled channel
  message is ignored.  Commands that fail validation still count;
* a DM is answered by DM; a channel command on the same channel, or by DM
  when ``commands.reply_on_channel`` is false.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import timeutil
from .config import Config, normalize_zip
from .errors import MeshWxError
from .models import BROADCAST, IncomingMessage, OutgoingMessage, TransitStop, WeatherReport, node_id_from_num
from .state import RuntimeSettings

if TYPE_CHECKING:  # annotations only: the bot just calls the methods in docs/CONTRACTS.md
    from .formatting import Formatter
    from .transit.service import TransitService
    from .weather.service import WeatherService

log = logging.getLogger(__name__)

_COMMAND_ALIASES: Dict[str, Tuple[str, ...]] = {
    "help": ("?", "h", "cmds"),
    "wx": ("weather", "w", "now"),
    "fc": ("forecast", "f"),
    "alerts": ("alert", "warn"),
    "bus": ("transit", "next", "t"),
    "ping": ("test",),
    "status": (),
    "setzip": ("zip",),
    "setinterval": ("interval",),
    "broadcast": ("bcast",),
}
#: lower-case command word or alias -> canonical command name
ALIASES: Dict[str, str] = {
    word: name for name, aliases in _COMMAND_ALIASES.items() for word in (name,) + aliases
}
ADMIN_COMMANDS = frozenset({"setzip", "setinterval", "broadcast"})

_PRUNE_AT = 64  # forget expired cooldowns once this many nodes are tracked
_HINT_MAX_CHARS = 80  # keep the "Unknown stop" hint to one short packet
_TRAILING_PUNCT = ".,;:!?"  # stripped from words: "wx 90210." (iOS adds "." after a double space)


@dataclass
class BotContext:
    """What the bot needs from the app."""

    cfg: Config
    settings: RuntimeSettings
    weather: WeatherService
    transit: Optional[TransitService]
    formatter: Formatter
    status_fn: Callable[[], Dict[str, Any]]
    broadcast_fn: Callable[[], None]


@dataclass
class _Request:
    msg: IncomingMessage
    args: List[str]
    node_id: str  # canonical "!a1b2c3d4" of the sender
    is_admin: bool


@dataclass
class _Cooldown:
    until: float  # clock() value from which the node may run a command again
    warned: bool = False  # "Please wait" already sent during this cooldown


class CommandBot:
    """Answers mesh text commands; see the module docstring for the rules."""

    def __init__(self, ctx: BotContext, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ctx = ctx
        self._clock = clock
        self._lock = threading.Lock()
        self._cooldowns: Dict[int, _Cooldown] = {}
        self._handlers: Dict[str, Callable[[_Request], List[str]]] = {
            "help": self._cmd_help,
            "wx": self._cmd_wx,
            "fc": self._cmd_fc,
            "alerts": self._cmd_alerts,
            "bus": self._cmd_bus,
            "ping": self._cmd_ping,
            "status": self._cmd_status,
            "setzip": self._cmd_setzip,
            "setinterval": self._cmd_setinterval,
            "broadcast": self._cmd_broadcast,
            "unknown": self._cmd_unknown,
        }

    # ------------------------------------------------------------------ public API
    def parse(self, msg: IncomingMessage) -> Optional[Tuple[str, List[str]]]:
        """``(command, args)`` when ``msg`` is a command for the bot, else None.

        An unknown first word gives ``("unknown", [word])`` in a DM and None on
        a channel; an emoji-only DM (a tapback reaction) gives None too.
        """
        commands = self._ctx.cfg.commands
        if not commands.enabled:
            return None
        text = (msg.text or "").strip()
        prefix = commands.prefix
        has_prefix = bool(prefix) and text[:len(prefix)].lower() == prefix.lower()
        if not msg.is_dm and (msg.channel_index not in commands.channels or (prefix and not has_prefix)):
            return None
        words = (text[len(prefix):] if has_prefix else text).split()
        if not words:
            return None
        word = words[0].lower()
        command = ALIASES.get(word) or ALIASES.get(word.rstrip(_TRAILING_PUNCT))
        if command is None:
            return ("unknown", [words[0]]) if msg.is_dm and not _is_emoji_only(word) else None
        return command, [a for a in (w.rstrip(_TRAILING_PUNCT) for w in words[1:]) if a]

    def handle(self, msg: IncomingMessage) -> List[OutgoingMessage]:
        """Reply packets for ``msg``; empty when it is not a command or must be ignored."""
        parsed = self.parse(msg)
        if parsed is None:
            return []
        command, args = parsed
        node_id = _node_id(msg)
        is_admin = node_id in self._ctx.cfg.commands.admins
        if not is_admin:
            wait = self._throttle(msg)
            if wait is not None:
                log.debug("rate limited: %s from %s", command, node_id)
                return self._address(msg, self._fmt.error("Please wait %ds" % wait)) if wait else []
        log.info("command from %s: %s %s", node_id, command, args)
        return self._address(msg, self._run(command, _Request(msg, args, node_id, is_admin)))

    # ------------------------------------------------------------------ plumbing
    @property
    def _fmt(self) -> Formatter:
        return self._ctx.formatter

    def _throttle(self, msg: IncomingMessage) -> Optional[int]:
        """None when the sender may run a command now (this starts their cooldown).

        Otherwise the seconds left, to announce once in a DM, or 0 to stay
        silent (channel message, or the sender was already told).
        """
        cooldown = self._ctx.cfg.commands.cooldown_seconds
        if cooldown <= 0:
            return None
        now = self._clock()
        with self._lock:
            entry = self._cooldowns.get(msg.from_num)
            if entry is None or now >= entry.until:
                if len(self._cooldowns) >= _PRUNE_AT:
                    self._cooldowns = {k: v for k, v in self._cooldowns.items() if v.until > now}
                self._cooldowns[msg.from_num] = _Cooldown(until=now + cooldown)
                return None
            if not msg.is_dm or entry.warned:
                return 0
            entry.warned = True
            return max(1, math.ceil(entry.until - now))

    def _run(self, command: str, req: _Request) -> List[str]:
        """Execute ``command``; any failure becomes one error packet."""
        if command in ADMIN_COMMANDS and not req.is_admin:
            return self._fmt.error("Admin only")
        try:
            return self._handlers[command](req)
        except MeshWxError as exc:
            log.info("%s from %s failed: %s", command, req.node_id, exc)
            return self._fmt.error(exc.user_message)
        except Exception:  # noqa: BLE001 - one bad command must not kill the worker thread
            log.exception("%s from %s crashed", command, req.node_id)
            return self._fmt.error("Internal error")

    def _address(self, msg: IncomingMessage, texts: Sequence[str]) -> List[OutgoingMessage]:
        """One reply packet per chunk: by DM to the sender, or to the channel it came from."""
        cfg = self._ctx.cfg
        if msg.is_dm or not cfg.commands.reply_on_channel:
            destination, want_ack = msg.from_num, cfg.meshtastic.dm_want_ack
        else:
            destination, want_ack = BROADCAST, False
        return [
            OutgoingMessage(text=text, destination=destination, channel_index=msg.channel_index,
                            reply_id=msg.packet_id, want_ack=want_ack, kind="reply")
            for text in texts
        ]

    def _usage(self, syntax: str) -> List[str]:
        return self._fmt.error("Usage: %s%s" % (self._ctx.cfg.commands.prefix, syntax))

    def _report_for(self, args: List[str]) -> WeatherReport:
        """Weather for the optional ZIP argument; the home location when absent (or the home ZIP)."""
        zip_code = None
        if args:
            zip_code = normalize_zip(args[0], self._ctx.cfg.location.country)
            if zip_code == self._ctx.settings.zip_code:
                zip_code = None
            elif not self._ctx.cfg.weather.allow_other_zips:
                raise MeshWxError("other ZIPs disabled", user_message="Other ZIPs disabled")
        return self._ctx.weather.get_report(zip_code)

    # ------------------------------------------------------------------ commands
    def _cmd_help(self, req: _Request) -> List[str]:
        return self._fmt.help(transit_enabled=self._ctx.transit is not None, is_admin=req.is_admin)

    def _cmd_wx(self, req: _Request) -> List[str]:
        return self._fmt.current(self._report_for(req.args))

    def _cmd_fc(self, req: _Request) -> List[str]:
        return self._fmt.forecast(self._report_for(req.args))

    def _cmd_alerts(self, req: _Request) -> List[str]:
        if not req.args:
            return self._fmt.alerts(self._ctx.weather.get_report())
        try:
            number = int(req.args[0].lstrip("#"))
        except ValueError:
            return self._usage("alerts [number]")
        report = self._ctx.weather.get_report()
        if not 1 <= number <= len(report.alerts):
            return self._fmt.error("No alert #%d" % number)
        return self._fmt.alert_detail(report.alerts[number - 1], number, report.timezone)

    def _cmd_bus(self, req: _Request) -> List[str]:
        transit = self._ctx.transit
        if transit is None:
            return self._fmt.error("Transit not configured")
        stops = None
        if req.args:
            stop = transit.find_stop(" ".join(req.args))
            if stop is None:
                return self._fmt.error(_unknown_stop_hint(transit.stops))
            stops = [stop]
        now = timeutil.utcnow()
        results = transit.get_arrivals(stops, now=now)
        return self._fmt.transit(results, now, max_arrivals=self._ctx.cfg.transit.max_arrivals)

    def _cmd_ping(self, req: _Request) -> List[str]:
        text = "pong"
        hops = req.msg.hops_away
        if hops == 0:
            text += " (direct)"
        elif hops is not None:
            text += " (%d hop%s)" % (hops, "" if hops == 1 else "s")
        if req.msg.via_mqtt:
            text += " via MQTT"
        return self._fmt.text(text)

    def _cmd_status(self, req: _Request) -> List[str]:
        return self._fmt.status(self._ctx.status_fn())

    def _cmd_setzip(self, req: _Request) -> List[str]:
        if not req.args:
            return self._usage("setzip <zip>")
        zip_code = normalize_zip(req.args[0], self._ctx.cfg.location.country)
        location = self._ctx.weather.location_for_zip(zip_code)  # unknown ZIPs fail here, before saving
        self._ctx.settings.set_zip(zip_code, set_by=req.node_id)
        return self._fmt.text("Home ZIP now %s (%s)" % (zip_code, location.name))

    def _cmd_setinterval(self, req: _Request) -> List[str]:
        if not req.args:
            return self._usage("setinterval <min|Nh|off>")
        minutes = self._ctx.settings.set_interval(req.args[0], set_by=req.node_id)
        broadcast = self._ctx.cfg.broadcast
        if broadcast.enabled and not broadcast.times:
            return self._fmt.text("Broadcast every %d min" % minutes if minutes else "Scheduled broadcasts off")
        # Saved, but the config makes the interval moot: don't claim a schedule that won't happen.
        reason = "fixed times in config are used" if broadcast.enabled else "broadcasts are off in config"
        return self._fmt.text("Interval %s saved, but %s" % ("%d min" % minutes if minutes else "off", reason))

    def _cmd_broadcast(self, req: _Request) -> List[str]:
        self._ctx.broadcast_fn()
        return self._fmt.text("Broadcast queued")

    def _cmd_unknown(self, req: _Request) -> List[str]:
        return self._fmt.error("Unknown command. Send help")


def _node_id(msg: IncomingMessage) -> str:
    """Sender as canonical "!a1b2c3d4" (``from_id`` may be missing or oddly cased)."""
    raw = (msg.from_id or "").strip().lower()
    if not raw:
        return node_id_from_num(msg.from_num)
    return raw if raw.startswith("!") else "!" + raw


def _is_emoji_only(word: str) -> bool:
    """True for a word like a tapback emoji: no ASCII characters and no letters or digits."""
    return all(not ch.isascii() and not ch.isalnum() for ch in word)


def _unknown_stop_hint(stops: Sequence[TransitStop]) -> str:
    """'Unknown stop. Try: Park St, Harvard' with as many labels as fit in a short packet."""
    head = "Unknown stop. Try: "
    labels: List[str] = []
    for stop in stops:
        if labels and len(head + ", ".join(labels + [stop.label])) > _HINT_MAX_CHARS:
            break
        labels.append(stop.label)
    return head + ", ".join(labels)
