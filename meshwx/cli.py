"""The ``meshwx`` command: run | preview | send | ports | set | status | setup | example-config.

Exit codes: 0 success, 1 failure, 2 invalid usage or configuration, 130 interrupted.
``-c/--config`` and ``--log-level`` work before or after the command name.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

from . import __version__, setup_wizard, timeutil
from .app import App
from .config import (ENV_CONFIG, LOG_LEVELS, RECOMMENDED_MIN_INTERVAL, Config, load_config, normalize_node_id,
                     normalize_zip)
from .errors import ConfigError, GeocodeError, MeshWxError
from .formatting import Formatter, utf8_len
from .geocode import ZipGeocoder
from .httpclient import HttpClient, default_user_agent
from .logsetup import configure_logging
from .mesh import ConsoleLink, MeshLink, PacedSender, create_link, list_serial_ports
from .models import BROADCAST, OutgoingMessage, StopArrivals, WeatherReport
from .scheduler import ScheduleSpec, next_run
from .state import KEY_INTERVAL, KEY_ZIP, RuntimeSettings, validate_interval
from .transit import create_transit_provider
from .transit.service import TransitService
from .weather import create_weather_provider
from .weather.service import GEOCODE_CACHE_FILE, WeatherService

SEND_CONNECT_TIMEOUT = 90.0
SEND_TIMEOUT_PER_PACKET = 30.0  # on top of meshtastic.message_delay_seconds
PREVIEW_ITEMS = ("current", "forecast", "alerts", "transit", "broadcast")
NEXT_BROADCASTS = 3
SERVICE_USER_HINT = ("On the Pi, run it as the service user so the running service sees the change: "
                     "the installed 'meshwx' command does that; otherwise use sudo -u meshwx ...")


def make_http(cfg: Optional[Config] = None) -> HttpClient:
    """The HTTP client of the CLI commands (tests replace this to stay offline)."""
    return HttpClient(user_agent=default_user_agent(cfg.weather.contact if cfg is not None else ""))


# =========================================================================== entry point


def main(argv: Optional[List[str]] = None) -> int:
    """Run the ``meshwx`` command line; returns the exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version and usage errors (already printed)
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    configure_logging(args.log_level or args.default_log_level)
    try:
        return args.func(args)
    except ConfigError as exc:
        _err("config error: %s" % exc)
        return 2
    except MeshWxError as exc:
        _err("error: %s" % exc)
        return 1
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


def build_parser() -> argparse.ArgumentParser:
    """The argument parser; each command stores its handler in ``func``."""
    config_help =("config file (default: $%s, ./config.yaml, ~/.config/meshwx/config.yaml, "
                   "/etc/meshwx/config.yaml)" % ENV_CONFIG)
    level_help = "log level: %s" % ", ".join(LOG_LEVELS)

    parser = argparse.ArgumentParser(
        prog="meshwx",
        description="Weather, severe-weather alerts and transit arrivals over a Meshtastic mesh.")
    parser.add_argument("--version", action="version", version="meshwx %s" % __version__)
    parser.add_argument("-c", "--config", metavar="PATH", help=config_help)
    parser.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, help=level_help)
    # The same options after the command name; SUPPRESS keeps a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="PATH", default=argparse.SUPPRESS, help=config_help)
    common.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, default=argparse.SUPPRESS,
                        help=level_help)
    sub = parser.add_subparsers(title="commands", metavar="COMMAND")

    def command(name: str, func: Any, help_text: str, log_level: str = "WARNING", **kwargs: Any
                ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text, **kwargs)
        p.set_defaults(func=func, default_log_level=log_level)
        return p

    p = command("run", _cmd_run, "run the service: broadcasts, alert watch, mesh commands", log_level="INFO")
    p.add_argument("--zip", help="home ZIP code for this run (instead of config.yaml / saved settings)")
    p.add_argument("--interval", type=_interval_arg, metavar="MIN|Nh|off",
                   help="broadcast interval for this run, e.g. 60, 2h or off")
    p.add_argument("--port", metavar="DEV", help="serial device of the node, e.g. /dev/ttyUSB0")
    p.add_argument("--dry-run", action="store_true",
                   help="no radio: print packets, read test messages from stdin (\"#1 text\" = channel 1)")

    p = command("preview", _cmd_preview, "print the messages meshwx would send (no radio needed)")
    p.add_argument("--zip", help="preview another ZIP code")
    p.add_argument("--what", type=_what_arg, default=["all"], metavar="ITEMS",
                   help="comma-separated: %s, all (default: all)" % ", ".join(PREVIEW_ITEMS))

    p = command("send", _cmd_send, "send a text message through the radio")
    p.add_argument("--port", metavar="DEV", help="serial device of the node, e.g. /dev/ttyUSB0")
    p.add_argument("--dry-run", action="store_true", help="print the packets instead of sending them")
    p.add_argument("--channel", type=_channel_arg, metavar="N",
                   help="channel index 0-7 (default: broadcast.channel_index, or 0 with --to)")
    # Not "--to !a1b2c3d4" in the example: bash expands "!..." (also in double quotes).
    p.add_argument("--to", type=_node_arg, metavar="NODEID",
                   help="send a direct message, e.g. --to a1b2c3d4 (the node id without its !, "
                        "or in single quotes)")
    p.add_argument("text", nargs="+", metavar="TEXT", help="the message")

    command("ports", _cmd_ports, "list serial ports and which look like a Meshtastic node")

    p = command("set", _cmd_set, "change the home ZIP code or broadcast interval of the service",
                epilog=SERVICE_USER_HINT)
    p.add_argument("setting", choices=("zip", "interval"), help="what to change")
    p.add_argument("value", help="a ZIP code, or an interval: minutes, Nh or off")

    command("status", _cmd_status, "show the effective settings and the next broadcasts")

    p = sub.add_parser("setup", help="create a config file interactively",
                       description="Create a config file interactively.")
    p.add_argument("-c", "--config", metavar="PATH", default=argparse.SUPPRESS,
                   help="file to write (default: $%s, else ./config.yaml)" % ENV_CONFIG)
    p.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, default=argparse.SUPPRESS,
                   help=level_help)
    p.set_defaults(func=_cmd_setup, default_log_level="ERROR")  # the wizard prints its own notes

    p = sub.add_parser("example-config", help="print the commented example config",
                       description="Print the commented example config (redirect it to a file).")
    p.set_defaults(func=_cmd_example_config, default_log_level="WARNING")
    return parser


# =========================================================================== commands


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.log_level is None:
        configure_logging(cfg.log_level)
    app = App.build(cfg, dry_run=args.dry_run, port=args.port, cli_zip=args.zip, cli_interval=args.interval)
    return app.run()


def _cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    items = []
    for word in args.what:
        if word == "all":
            items.extend(i for i in PREVIEW_ITEMS if i != "transit" or cfg.transit.enabled)
        else:
            items.append(word)
    http = make_http(cfg)
    try:
        return _preview(cfg, http, args.zip, list(dict.fromkeys(items)))
    finally:
        http.close()


def _preview(cfg: Config, http: HttpClient, zip_code: Optional[str], items: Sequence[str]) -> int:
    """Fetch like the service does and print every packet with its size; 1 when something failed."""
    settings = RuntimeSettings(cfg, cfg.state_path(), cli_zip=zip_code)
    where = "ZIP %s" % settings.zip_code if settings.zip_code else "the configured coordinates"
    formatter = _formatter(cfg)
    now = timeutil.utcnow()
    failed = False
    report: Optional[WeatherReport] = None
    if any(item != "transit" for item in items):
        try:
            report = WeatherService(cfg, settings, http).get_report()
        except MeshWxError as exc:
            _err("weather for %s failed: %s" % (where, exc))
            failed = True
        else:
            notes = [report.provider] + (["stale"] if report.stale else []) + report.notes
            _print("Weather for %s: %s (%s)" % (where, report.location.name, ", ".join(notes)))
    arrivals: Optional[List[StopArrivals]] = None
    broadcast_transit = "broadcast" in items and "transit" in cfg.broadcast.content
    if cfg.transit.enabled and ("transit" in items or broadcast_transit):
        transit = TransitService(cfg.transit, create_transit_provider(cfg.transit, http, cfg.cache_path()))
        if cfg.transit.provider == "gtfs_rt" and cfg.transit.gtfs_rt.static_gtfs_url:
            _print("Loading the static GTFS timetable (the first run downloads and indexes it; "
                   "this can take minutes on a Pi Zero)...")
        transit.warm_up()  # one-shot command: wait for the index instead of answering "data loading"
        try:
            arrivals = transit.get_arrivals(now=timeutil.utcnow())
        except MeshWxError as exc:
            _err("transit arrivals failed: %s" % exc)
            failed = True
    _print("Packets are limited to %d bytes." % formatter.max_bytes)
    for item in items:
        if item == "transit":
            if not cfg.transit.enabled:
                _err("transit is not enabled in the config (transit.enabled: false)")
                failed = True
            elif arrivals is not None:
                _show_packets("transit", formatter.transit(arrivals, now, cfg.transit.max_arrivals))
        elif item == "broadcast":
            _show_packets("broadcast on channel %d" % cfg.broadcast.channel_index,
                          formatter.broadcast(report, cfg.broadcast.content, arrivals, now,
                                              cfg.transit.max_arrivals))
        elif report is not None:
            render = {"current": formatter.current, "forecast": formatter.forecast, "alerts": formatter.alerts}
            _show_packets(item, render[item](report))
    return 1 if failed else 0


def _cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    mcfg = cfg.meshtastic
    chunks = _formatter(cfg).text(" ".join(args.text))
    if not chunks:
        _err("nothing to send")
        return 2
    destination = args.to or BROADCAST
    channel = args.channel if args.channel is not None else (0 if args.to else cfg.broadcast.channel_index)
    msgs = [OutgoingMessage(text=c, destination=destination, channel_index=channel,
                            want_ack=bool(args.to) and mcfg.dm_want_ack, kind="reply" if args.to else "broadcast")
            for c in chunks]
    if args.dry_run:
        link: MeshLink = ConsoleLink(mcfg.max_message_bytes, read_input=False)
    else:
        link = create_link(cfg, port_override=args.port)
    counter = _SentCounter(link)
    sender = PacedSender(counter, mcfg.message_delay_seconds, max_queue=mcfg.max_queue,
                         max_bytes=mcfg.max_message_bytes)
    counter.start()
    sender.start()
    try:
        if not counter.wait_connected(SEND_CONNECT_TIMEOUT):
            _err("no radio connection within %d s (%s). Is the node plugged in, or is the meshwx service "
                 "using it? (sudo systemctl stop meshwx)" % (SEND_CONNECT_TIMEOUT, link.describe()))
            return 1
        sender.enqueue(msgs)
        sender.wait_idle(len(msgs) * (mcfg.message_delay_seconds + SEND_TIMEOUT_PER_PACKET))
    finally:
        sender.stop()
        counter.stop()
    if counter.sent < len(msgs):
        _err("sent %d of %d packet(s)" % (counter.sent, len(msgs)))
        return 1
    _print("Sent %d packet(s) to %s on channel %d." % (len(msgs), destination, channel))
    return 0


def _cmd_ports(args: argparse.Namespace) -> int:
    ports = list_serial_ports()
    if not ports:
        _print("No serial ports found. Is the node plugged in with a data (not charge-only) USB cable?")
        return 1
    width = max(len("DEVICE"), max(len(device) for device, _, _ in ports))
    _print("%-*s  %-10s  %s" % (width, "DEVICE", "MESHTASTIC", "DESCRIPTION"))
    for device, description, likely in ports:
        _print("%-*s  %-10s  %s" % (width, device, "likely" if likely else "-", description))
    if any(likely for _, _, likely in ports):
        _print("\nAuto-detect (meshtastic.serial_port: \"\") uses the first likely port; "
               "set serial_port to pick another.")
    else:
        _print("\nNo port looks like a Meshtastic node: set meshtastic.serial_port if yours is listed.")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    settings = RuntimeSettings(cfg, cfg.state_path())
    try:
        lines = _set_zip(cfg, settings, args.value) if args.setting == "zip" else \
            _set_interval(cfg, settings, args.value)
    except ConfigError as exc:  # invalid value
        _err("meshwx set %s: %s (got %r)" % (args.setting, exc.user_message, args.value))
        return 2
    except GeocodeError as exc:
        _err("meshwx set zip: %s; nothing changed" % exc.user_message)
        return 1
    # RuntimeSettings only logs a failed write: read the file back to be sure it was saved.
    if RuntimeSettings(cfg, cfg.state_path()).describe() != settings.describe():
        _err("could not save %s (permission denied?). %s" % (settings.path, SERVICE_USER_HINT))
        return 1
    for line in lines:
        _print(line)
    _print("A running meshwx service applies this within ~30 s.")
    return 0


def _set_zip(cfg: Config, settings: RuntimeSettings, value: str) -> List[str]:
    """Validate (geocode) and save a new home ZIP; returns the lines to print."""
    country = cfg.location.country
    code = normalize_zip(value, country)
    http = make_http(cfg)
    try:
        place = ZipGeocoder(http, cfg.cache_path() / GEOCODE_CACHE_FILE).lookup(code, country).name
    finally:
        http.close()
    settings.set_zip(code, set_by="cli")
    if settings.source(KEY_ZIP) == "config":
        return ["Home ZIP: %s (%s), the config.yaml value." % (code, place)]
    return ["Home ZIP set to %s (%s)." % (code, place)]


def _set_interval(cfg: Config, settings: RuntimeSettings, value: str) -> List[str]:
    """Save a new broadcast interval; returns the lines to print."""
    minutes = settings.set_interval(value, set_by="cli")
    b = cfg.broadcast
    line = "Broadcast interval set to %d min." % minutes if minutes else "Scheduled broadcasts turned off."
    if settings.source(KEY_INTERVAL) == "config":
        line += " (This is the config.yaml value.)"
    lines = [line]
    if not b.enabled:
        lines.append("Note: broadcast.enabled is false in config.yaml, so nothing is broadcast.")
    elif b.times:
        lines.append("Note: config.yaml sets broadcast.times, which are used instead of the interval.")
    if 0 < minutes < RECOMMENDED_MIN_INTERVAL:
        lines.append("Note: every %d min is a lot of airtime; 60+ is kinder to the mesh." % minutes)
    return lines


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    settings = RuntimeSettings(cfg, cfg.state_path())
    b, loc, mcfg = cfg.broadcast, cfg.location, cfg.meshtastic
    rows: List[Tuple[str, Union[str, List[str]]]] = [
        ("Config file", str(cfg.source_path)),
        ("State file", "%s%s" % (settings.path, "" if settings.path.exists() else " (not created yet)")),
        ("ZIP code", "%s (%s)" % (settings.zip_code or "-",
                                  _source(settings.source(KEY_ZIP), loc.zip_code or "-"))),
    ]
    if loc.latitude is not None and not settings.zip_is_override:
        rows.append(("Coordinates", "%s, %s (config.yaml)" % (loc.latitude, loc.longitude)))
    rows.append(("Interval", "%s (%s)" % (_minutes(settings.interval_minutes),
                                          _source(settings.source(KEY_INTERVAL), _minutes(b.interval_minutes)))))
    weather = cfg.weather.provider + (", fallback %s" % cfg.weather.fallback_provider
                                      if cfg.weather.fallback_provider else "")
    rows.append(("Weather", weather))
    if b.enabled:
        spec = ScheduleSpec.from_config(b, settings.interval_minutes)
        rows += [("Broadcasts", "on channel %d: %s" % (b.channel_index, ", ".join(b.content))),
                 ("Schedule", _schedule_text(spec)),
                 ("Next broadcasts", _next_broadcasts(spec, timeutil.local_now()))]
    else:
        rows.append(("Broadcasts", "disabled (broadcast.enabled: false)"))
    rows.append(("Alert watch", _alert_watch_text(cfg)))
    c = cfg.commands
    commands = 'prefix "%s" on channel(s) %s, admins: %s' % (
        c.prefix, ", ".join(str(ch) for ch in c.channels) or "-", ", ".join(c.admins) or "none")
    rows.append(("Commands", commands if c.enabled else "off"))
    t = cfg.transit
    rows.append(("Transit", "%s: %s" % (t.provider, ", ".join(s.label for s in t.stops)) if t.enabled else "off"))
    if mcfg.connection == "tcp":
        radio = "tcp %s:%d" % (mcfg.tcp_host, mcfg.tcp_port)
    else:
        radio = "serial %s" % (mcfg.serial_port or "(auto-detect)")
    rows.append(("Radio", "%s; max %d bytes per packet, %gs between packets" % (
        radio, mcfg.max_message_bytes, mcfg.message_delay_seconds)))
    _print("meshwx %s" % __version__)
    _print_rows(rows)
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    dest = Path(os.path.expanduser(args.config or os.environ.get(ENV_CONFIG) or "config.yaml"))
    http = make_http()
    try:
        return setup_wizard.run_wizard(dest, port_lister=list_serial_ports, geocoder=ZipGeocoder(http))
    finally:
        http.close()


def _cmd_example_config(args: argparse.Namespace) -> int:
    sys.stdout.write(setup_wizard.TEMPLATE_PATH.read_text(encoding="utf-8"))
    return 0


# =========================================================================== helpers


class _SentCounter(MeshLink):
    """Delegates to another link and counts the packets it accepted (``meshwx send``'s exit code)."""

    def __init__(self, link: MeshLink) -> None:
        super().__init__()
        self._link = link
        self.sent = 0

    def start(self) -> None:
        self._link.start()

    def stop(self) -> None:
        self._link.stop()

    def is_connected(self) -> bool:
        return self._link.is_connected()

    def wait_connected(self, timeout: float) -> bool:
        return self._link.wait_connected(timeout)

    def send(self, msg: OutgoingMessage) -> None:
        self._link.send(msg)
        self.sent += 1

    def my_node_num(self) -> Optional[int]:
        return self._link.my_node_num()

    def describe(self) -> str:
        return self._link.describe()


def _formatter(cfg: Config) -> Formatter:
    return Formatter(cfg.display, cfg.meshtastic.max_message_bytes, default_tz=timeutil.local_tz(),
                     command_prefix=cfg.commands.prefix)


def _interval_arg(text: str) -> int:
    try:
        return validate_interval(text)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _channel_arg(text: str) -> int:
    if not (text.isdigit() and 0 <= int(text) <= 7):
        raise argparse.ArgumentTypeError("channel must be 0-7 (got %r)" % text)
    return int(text)


def _node_arg(text: str) -> str:
    try:
        return normalize_node_id(text)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _what_arg(text: str) -> List[str]:
    words = [w.strip().lower() for w in text.split(",") if w.strip()]
    unknown = [w for w in words if w not in PREVIEW_ITEMS + ("all",)]
    if unknown or not words:
        raise argparse.ArgumentTypeError("choose from %s, all (got %r)" % (", ".join(PREVIEW_ITEMS), text))
    return words


def _show_packets(title: str, packets: Sequence[str]) -> None:
    _print("")
    _print("== %s: %d packet%s ==" % (title, len(packets), "" if len(packets) == 1 else "s"))
    if not packets:
        _print("(nothing to send)")
    for i, text in enumerate(packets, 1):
        _print("-- %d/%d, %d bytes --" % (i, len(packets), utf8_len(text)))
        _print(text)


def _source(source: str, config_value: str) -> str:
    return "config.yaml" if source == "config" else "%s; config.yaml: %s" % (source, config_value)


def _minutes(value: int) -> str:
    return "%d min" % value if value else "off"


def _schedule_text(spec: ScheduleSpec) -> str:
    if spec.times:
        text = "daily at %s" % ", ".join(spec.times)
    elif not spec.interval_minutes:
        text = "no scheduled broadcasts (interval off)"
    elif spec.align_to_clock and (24 * 60) % spec.interval_minutes == 0:  # see meshwx.scheduler
        text = "every %d min, aligned to the clock" % spec.interval_minutes
    else:
        text = "every %d min, counted from the service start (times below assume a start now)" % \
               spec.interval_minutes
    if spec.quiet_start:
        text += "; quiet %s-%s" % (spec.quiet_start, spec.quiet_end)
    return text


def _next_broadcasts(spec: ScheduleSpec, now: datetime) -> List[str]:
    times: List[str] = []
    slot = next_run(spec, now)
    while slot is not None and len(times) < NEXT_BROADCASTS:
        times.append(slot.strftime("%a %Y-%m-%d %H:%M %Z"))
        slot = next_run(spec, slot, anchor=slot)
    return times or ["none"]


def _alert_watch_text(cfg: Config) -> str:
    b = cfg.broadcast
    if not b.enabled:
        return "off (broadcasts are disabled)"
    if not b.alert_watch:
        return "off (broadcast.alert_watch: false)"
    http = make_http(cfg)
    try:
        supported = create_weather_provider(cfg.weather.provider, http, cfg).supports_alerts
    finally:
        http.close()
    if not supported:
        return "off (weather provider %s has no alerts)" % cfg.weather.provider
    channel = b.alert_channel_index if b.alert_channel_index is not None else b.channel_index
    return "every %d min, %s or worse, on channel %d" % (b.alert_check_minutes, b.alert_min_severity, channel)


def _print_rows(rows: Sequence[Tuple[str, Union[str, List[str]]]]) -> None:
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        for i, line in enumerate([value] if isinstance(value, str) else value):
            _print("%-*s%s" % (width, label + ":" if i == 0 else "", line))


def _print(text: str = "") -> None:
    """print() that survives a console which cannot encode some characters (e.g. the degree sign)."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding))


def _err(text: str) -> None:
    print(text, file=sys.stderr)
