"""The meshwx service: wiring, threads, the severe-weather alert watch and shutdown.

Threads (docs/ARCHITECTURE.md, "Threading model"):

* **main thread** (:meth:`App.run`) ticks once a second: it re-reads
  ``state.json`` every 30 s, asks the scheduler whether a broadcast is due and
  queues the severe-weather alert checks.  It never does network I/O.
* **worker thread**: one FIFO of work items, handled one at a time:
  ``("command", IncomingMessage)``, ``("broadcast", reason)`` and
  ``("alert_check", None)``.  Every weather/transit fetch happens here.  A
  failing item is logged and never stops the worker; errors are never
  broadcast to the mesh.
* **sender thread** (:class:`~meshwx.mesh.PacedSender`) paces packets onto the radio.
* **radio threads** (the link and the meshtastic library) only enqueue
  incoming messages.

Severe-weather alerts (docs/ARCHITECTURE.md, "Severe-weather alert watch"):
:func:`select_new_alerts` decides which active alerts are news; the ids
already handled are kept in ``state.json`` (``seen_alerts``) so a restart
never repeats an alert.  An announced alert counts as handled only once every
one of its packets went out: until then its entry carries ``"pending"``, and
an entry whose packets were dropped, discarded at shutdown or lost with the
process is forgotten at the next alert check, so the alert is announced again
while it is still in effect.

Signals: the SIGINT/SIGTERM handler only sets a flag (it must not take a lock
or log: it runs on the main thread between two bytecodes, possibly while that
thread holds the very lock it would wait for); the main loop sees the flag
within one tick.
"""

from __future__ import annotations

import functools
import logging
import queue
import shutil
import signal
import tempfile
import threading
import time
import weakref
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import __version__, timeutil
from .bot import BotContext, CommandBot
from .config import Config
from .errors import MeshWxError
from .formatting import Formatter
from .httpclient import HttpClient, default_user_agent
from .mesh import MeshLink, PacedSender, create_link
from .models import (
    BROADCAST,
    OUTCOME_SENT,
    IncomingMessage,
    OutgoingMessage,
    StopArrivals,
    WeatherAlert,
    WeatherReport,
)
from .scheduler import BroadcastScheduler, ScheduleSpec
from .state import KEY_INTERVAL, KEY_ZIP, STATE_FILENAME, RuntimeSettings
from .transit import create_transit_provider
from .transit.service import TransitService
from .weather.service import WeatherService

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
SETTINGS_REFRESH_SECONDS = 30.0  # how soon a `meshwx set ...` from another process is applied
FIRST_ALERT_CHECK_SECONDS = 30.0
MAX_PENDING_WORK = 50  # queued work items; more incoming messages are dropped
WORKER_JOIN_SECONDS = 10.0

SEEN_ALERTS_KEY = "seen_alerts"
#: Field of a ``seen_alerts`` entry while the announcement it belongs to (the
#: value: id of the announced alert) is not fully sent yet.
PENDING_FIELD = "pending"
#: Failed announcements of one alert (per run) before it is recorded as handled anyway.
MAX_ALERT_DELIVERIES = 3
SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}
UNKNOWN_SEVERITY_RANK = 2  # "Unknown" or missing counts as Moderate
SEEN_AFTER_END = timedelta(hours=24)  # keep a seen alert this long after it ended...
SEEN_WITHOUT_END = timedelta(days=7)  # ...or this long after it was first seen when it has no end

_STOP = object()  # worker-queue sentinel
_COALESCED = frozenset({"broadcast", "alert_check"})  # at most one of each waiting in the queue


# =========================================================================== alert de-duplication


def severity_rank(severity: Optional[str]) -> int:
    """Minor 1, Moderate 2, Severe 3, Extreme 4; unknown or missing counts as Moderate (2)."""
    return SEVERITY_RANK.get((severity or "").strip().lower(), UNKNOWN_SEVERITY_RANK)


def select_new_alerts(alerts: Sequence[WeatherAlert], seen: Mapping[str, Any], min_severity: str,
                      now: datetime) -> Tuple[List[WeatherAlert], Dict[str, Dict[str, Any]]]:
    """Pick the active ``alerts`` worth broadcasting; returns ``(to_broadcast, new_seen)``.

    ``seen`` maps the ids of alerts already handled to ``{"event", "ends",
    "seen_at"}`` (ISO times; the app may add other fields, which are kept); it
    is not modified.  For each alert not in it:

    * a ``Cancel`` is recorded without a broadcast;
    * a follow-up of a seen alert of the same event - any message that
      references it, whatever its ``message_type`` - is recorded without a
      broadcast.  (api.weather.gov lists a continuation issued after its
      predecessor expired as a new ``Alert`` that names the predecessor only in
      ``parameters.expiredReferences``; the NWS provider adds those ids to
      ``references``.)  A different event, e.g. Watch -> Warning, is news;
    * an alert that another active alert references (superseded within this
      batch) is recorded without a broadcast - its successor speaks for it;
    * anything else is broadcast and recorded if its :func:`severity_rank` is
      at least that of ``min_severity``.  Weaker alerts are not recorded, so an
      update that raises the severity is still announced.

    Entries of alerts that are no longer active are dropped once they ended
    more than 24 h ago or, without an end time, were first seen more than
    7 days ago.  Pure function: compare ``new_seen`` with ``seen`` to know
    whether anything must be saved.
    """
    floor = severity_rank(min_severity)
    stamp = now.isoformat(timespec="seconds")
    before: Dict[str, Dict[str, Any]] = {str(k): dict(v) for k, v in seen.items() if isinstance(v, dict)}
    new_seen = dict(before)
    superseded = {ref for alert in alerts for ref in alert.references}
    to_broadcast: List[WeatherAlert] = []
    for alert in alerts:
        if alert.id in new_seen:
            continue
        kind = (alert.message_type or "").strip().lower()
        if kind == "cancel" or alert.id in superseded or _follows_seen(alert, before):
            log.debug("alert %s (%s %s) recorded without a broadcast", alert.id, alert.event, kind or "alert")
        elif severity_rank(alert.severity) >= floor:
            to_broadcast.append(alert)
        else:
            continue
        new_seen[alert.id] = _seen_entry(alert, stamp)
    active = {alert.id for alert in alerts}
    for alert_id in [k for k, v in new_seen.items() if k not in active and _forgettable(v, now)]:
        del new_seen[alert_id]
    return to_broadcast, new_seen


def _seen_entry(alert: WeatherAlert, stamp: str) -> Dict[str, Any]:
    return {"event": alert.event, "ends": alert.ends.isoformat() if alert.ends else None, "seen_at": stamp}


def _follows_seen(alert: WeatherAlert, seen: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when ``alert`` references an already seen alert of the same event (a follow-up, not news)."""
    return any(ref in seen and seen[ref].get("event") == alert.event for ref in alert.references)


def _forgettable(entry: Mapping[str, Any], now: datetime) -> bool:
    ends = timeutil.parse_iso(entry.get("ends"))
    if ends is not None:
        return now - ends > SEEN_AFTER_END
    seen_at = timeutil.parse_iso(entry.get("seen_at"))
    return seen_at is None or now - seen_at > SEEN_WITHOUT_END


def _mark_pending(alerts: Sequence[WeatherAlert], to_broadcast: Sequence[WeatherAlert],
                  before: Mapping[str, Any], new_seen: Dict[str, Dict[str, Any]]) -> None:
    """Tie the entries of this alert check to the announcements they wait for (in place).

    Each announced alert's entry gets ``"pending": <its id>``.  An alert
    recorded silently because of a pending announcement - it follows a pending
    alert, or a pending alert supersedes it - gets the same ``"pending"``:
    should that announcement fail, both must be reconsidered, otherwise the
    next check would take one for a follow-up of the other and stay silent.
    """
    for alert in to_broadcast:
        new_seen[alert.id] = dict(new_seen[alert.id], **{PENDING_FIELD: alert.id})
    silent = [a for a in alerts if a.id in new_seen and a.id not in before
              and PENDING_FIELD not in new_seen[a.id]]
    referrers: Dict[str, List[str]] = {}
    for alert in alerts:
        for ref in alert.references:
            referrers.setdefault(ref, []).append(alert.id)
    changed = True
    while changed:  # follow chains (a silent update of a silent update of a pending alert)
        changed = False
        for alert in silent:
            if PENDING_FIELD in new_seen[alert.id]:
                continue
            for other in list(alert.references) + referrers.get(alert.id, []):
                key = (new_seen.get(other) or {}).get(PENDING_FIELD)
                if key is not None:
                    new_seen[alert.id] = dict(new_seen[alert.id], **{PENDING_FIELD: key})
                    changed = True
                    break


def _settle(seen: Dict[str, Any], key: str) -> None:
    """Drop ``"pending"`` from the entries waiting for the announcement ``key`` (in place): it is done."""
    for alert_id, entry in list(seen.items()):
        if isinstance(entry, dict) and entry.get(PENDING_FIELD) == key:
            seen[alert_id] = {f: v for f, v in entry.items() if f != PENDING_FIELD}


def _private_state_copy(state_dir: Path) -> Path:
    """A new temporary directory holding a copy of ``state_dir``'s state file (if readable)."""
    scratch = Path(tempfile.mkdtemp(prefix="meshwx-dry-run-"))
    source = state_dir / STATE_FILENAME
    try:
        shutil.copyfile(str(source), str(scratch / STATE_FILENAME))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("dry run: cannot read %s (%s); starting without saved settings", source, exc)
    log.info("dry run: using a private copy of the state in %s; nothing is saved to %s", scratch, state_dir)
    return scratch


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return "signal %d" % signum


# =========================================================================== the service


class App:
    """The running service: owns the threads and wires the components together.

    :meth:`build` creates everything from the config; the constructor takes
    ready-made components so tests can inject fakes.  ``bot`` defaults to a
    :class:`~meshwx.bot.CommandBot` over the given components; ``clock``
    (monotonic seconds) and ``tick_seconds`` drive the main loop.
    """

    def __init__(self, cfg: Config, *, settings: RuntimeSettings, weather: WeatherService,
                 formatter: Formatter, link: MeshLink, sender: PacedSender,
                 scheduler: BroadcastScheduler, transit: Optional[TransitService] = None,
                 http: Optional[HttpClient] = None, bot: Optional[CommandBot] = None,
                 clock: Callable[[], float] = time.monotonic, tick_seconds: float = TICK_SECONDS) -> None:
        self._cfg = cfg
        self._settings = settings
        self._weather = weather
        self._transit = transit
        self._formatter = formatter
        self._link = link
        self._sender = sender
        self._scheduler = scheduler
        self._http = http
        self._bot = bot if bot is not None else CommandBot(BotContext(
            cfg=cfg, settings=settings, weather=weather, transit=transit, formatter=formatter,
            status_fn=self.status, broadcast_fn=self.broadcast_now))
        self._clock = clock
        self._tick_seconds = tick_seconds

        b = cfg.broadcast
        self._alert_watch = b.enabled and b.alert_watch and weather.alerts_supported()
        self._startup_broadcast_due = b.enabled and b.on_startup
        self._stop = threading.Event()
        self._signal_received: Optional[int] = None  # set by the signal handler (no locks there)
        # seen_alerts read-modify-write (worker) vs. delivery reports (sender thread); re-entrant
        # because the sender may report a packet from inside enqueue().
        self._alerts_lock = threading.RLock()
        self._deliveries: Dict[str, int] = {}  # announced alert id -> packets not yet sent
        self._delivery_failures: Dict[str, int] = {}  # announced alert id -> failed announcements
        self._cleanup: Optional[Callable[[], Any]] = None  # removes a dry run's private state copy
        self._queue: "queue.Queue[Any]" = queue.Queue()  # unbounded: the sentinel always fits
        self._queue_lock = threading.Lock()  # guards _waiting and the MAX_PENDING_WORK check
        self._waiting: Set[str] = set()  # coalesced job kinds currently queued
        self._handlers: Dict[str, Callable[[Any], None]] = {
            "command": self._do_command,
            "broadcast": self._do_broadcast,
            "alert_check": self._do_alert_check,
        }
        self._worker: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._alerts_active: Optional[int] = None  # from the last alert check / broadcast report
        self._next_refresh = 0.0
        self._next_alert_check = 0.0
        settings.add_listener(self._on_setting_changed)

    @classmethod
    def build(cls, cfg: Config, *, dry_run: bool = False, port: Optional[str] = None,
              cli_zip: Optional[str] = None, cli_interval: Optional[Any] = None) -> "App":
        """Create every component from ``cfg``.

        ``dry_run`` uses the console instead of the radio and a private copy of
        ``state.json`` in a temporary directory: saved ZIP/interval overrides and
        seen alerts still apply, but nothing the dry run does (alerts it shows,
        ``setzip``/``setinterval``) reaches the configured state directory, which
        a running service may share.  ``port`` overrides ``meshtastic.serial_port``;
        ``cli_zip`` / ``cli_interval`` are the ``meshwx run --zip/--interval``
        overrides.  Raises :class:`ConfigError` for invalid overrides.
        """
        http = HttpClient(user_agent=default_user_agent(cfg.weather.contact))
        scratch: Optional[Path] = None
        try:
            state_dir = cfg.state_path()
            if dry_run:
                scratch = state_dir = _private_state_copy(state_dir)
            settings = RuntimeSettings(cfg, state_dir, cli_zip=cli_zip, cli_interval=cli_interval)
            weather = WeatherService(cfg, settings, http)
            transit = None
            if cfg.transit.enabled:
                transit = TransitService(cfg.transit, create_transit_provider(cfg.transit, http, cfg.cache_path()))
            tz = timeutil.local_tz()
            formatter = Formatter(cfg.display, cfg.meshtastic.max_message_bytes, default_tz=tz,
                                  command_prefix=cfg.commands.prefix)
            link = create_link(cfg, dry_run=dry_run, port_override=port)
            mcfg = cfg.meshtastic
            sender = PacedSender(link, mcfg.message_delay_seconds, max_queue=mcfg.max_queue,
                                 max_bytes=mcfg.max_message_bytes)
            scheduler = BroadcastScheduler(
                lambda: ScheduleSpec.from_config(cfg.broadcast, settings.interval_minutes), tz=tz)
        except BaseException:
            http.close()
            if scratch is not None:
                shutil.rmtree(str(scratch), ignore_errors=True)
            raise
        app = cls(cfg, settings=settings, weather=weather, transit=transit, formatter=formatter,
                  link=link, sender=sender, scheduler=scheduler, http=http)
        if scratch is not None:  # removed at shutdown, or when the app is garbage-collected / at exit
            app._cleanup = weakref.finalize(app, shutil.rmtree, str(scratch), True)
        return app

    # ------------------------------------------------------------------ public API
    def run(self) -> int:
        """Run until :meth:`request_stop`, SIGINT or SIGTERM.

        Returns 0 after a clean stop, 1 when the radio link cannot be started.
        """
        restore_signals = self._install_signal_handlers()
        self._started_at = self._clock()
        try:
            self._log_banner()
            self._link.on_message = self._on_message
            self._link.on_connection = self._on_connection
            self._worker = threading.Thread(target=self._work_loop, name="meshwx-worker", daemon=True)
            self._worker.start()
            self._sender.start()
            try:
                self._link.start()
            except MeshWxError as exc:
                log.error("cannot start the radio link: %s", exc)
                return 1
            threading.Thread(target=self._warm_up, name="meshwx-warmup", daemon=True).start()
            self._main_loop()
            return 0
        finally:
            try:
                self._shutdown()
            finally:
                restore_signals()

    def request_stop(self) -> None:
        """Make :meth:`run` return within a tick (thread-safe; NOT for signal handlers, see :meth:`_on_signal`)."""
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        """Service state for the ``status`` command (keys listed in docs/CONTRACTS.md)."""
        s = self._settings
        b = self._cfg.broadcast
        try:
            location: Optional[str] = self._weather.home_location().name
        except MeshWxError:
            location = None
        return {
            "version": __version__,
            "zip_code": s.zip_code,
            "zip_source": s.source(KEY_ZIP),
            "location": location,
            "interval_minutes": s.interval_minutes,
            "interval_source": s.source(KEY_INTERVAL),
            "times": list(b.times),
            "next_broadcast": self._scheduler.next_run_at if b.enabled else None,
            "broadcast_enabled": b.enabled,
            "channel_index": b.channel_index,
            "link": "up" if self._link.is_connected() else "down",
            "link_desc": self._link.describe(),
            "queue": self._sender.pending(),
            "uptime_s": int(self._clock() - self._started_at) if self._started_at is not None else 0,
            "alerts_active": self._alerts_active,
            "transit_enabled": self._transit is not None,
        }

    def broadcast_now(self, reason: str = "manual") -> None:
        """Queue a broadcast of the configured content (also used by the ``broadcast`` command)."""
        self._enqueue("broadcast", reason)

    # ------------------------------------------------------------------ main thread
    def _main_loop(self) -> None:
        now = self._clock()
        self._next_refresh = now + SETTINGS_REFRESH_SECONDS
        self._next_alert_check = now + FIRST_ALERT_CHECK_SECONDS
        while not self._stop_requested():
            # A plain sleep, not self._stop.wait(): the main thread then holds no lock while idle,
            # so a second Ctrl+C (KeyboardInterrupt) cannot leave one locked.  A signal handler runs
            # during the sleep, which then resumes (PEP 475): a stop is seen within one tick.
            time.sleep(self._tick_seconds)
            if self._stop_requested():
                break
            try:
                self._tick(self._clock())
            except Exception:  # noqa: BLE001 - the main loop must keep running
                log.exception("main loop error")

    def _stop_requested(self) -> bool:
        """True once :meth:`request_stop` was called or a signal arrived (main thread)."""
        signum = self._signal_received
        if signum is not None and not self._stop.is_set():
            log.info("%s received; stopping", _signal_name(signum))
            self._stop.set()
        return self._stop.is_set()

    def _tick(self, now: float) -> None:
        if now >= self._next_refresh:
            self._next_refresh = now + SETTINGS_REFRESH_SECONDS
            self._settings.refresh()
        b = self._cfg.broadcast
        if b.enabled and self._scheduler.poll():
            self._enqueue("broadcast", "schedule")
        # Postponed while the radio is down: a new alert is then announced when it
        # is back instead of expiring in the send queue.
        if self._alert_watch and now >= self._next_alert_check and self._link.is_connected():
            self._next_alert_check = now + b.alert_check_minutes * 60.0
            self._enqueue("alert_check")

    def _install_signal_handlers(self) -> Callable[[], None]:
        """SIGINT/SIGTERM -> stop (main thread only, see :meth:`_on_signal`); returns a restore function."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous: Dict[int, Any] = {}
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous[signum] = signal.signal(signum, self._on_signal)
            except (OSError, ValueError) as exc:  # e.g. not supported on this platform
                log.debug("cannot handle %s: %s", name, exc)

        def restore() -> None:
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
                except (OSError, ValueError, TypeError):
                    pass

        return restore

    def _on_signal(self, signum: int, frame: Any) -> None:
        """SIGINT/SIGTERM handler: only records the signal; the main loop stops and logs.

        It runs on the main thread between two bytecodes, possibly while that
        thread holds a lock - e.g. inside ``threading.Event.wait`` or a log
        call - so it must not take any lock: no ``Event.set()``, no logging,
        no thread start.  ``signal.signal`` takes no Python-level lock.
        """
        self._signal_received = signum
        if signum == signal.SIGINT:  # a second Ctrl+C raises KeyboardInterrupt: it cuts a slow shutdown short
            signal.signal(signal.SIGINT, signal.default_int_handler)

    def _log_banner(self) -> None:
        s, b = self._settings, self._cfg.broadcast
        if not b.enabled:
            schedule = "broadcasts disabled"
        elif b.times:
            schedule = "broadcasts at %s on channel %d" % (", ".join(b.times), b.channel_index)
        elif s.interval_minutes:
            schedule = "broadcasts every %d min (%s) on channel %d" % (
                s.interval_minutes, s.source(KEY_INTERVAL), b.channel_index)
        else:
            schedule = "scheduled broadcasts off (%s)" % s.source(KEY_INTERVAL)
        if self._alert_watch:
            alerts = "alert watch every %d min, %s or worse, on channel %d" % (
                b.alert_check_minutes, b.alert_min_severity, self._alert_channel())
        else:
            alerts = "alert watch off"
        where = "ZIP %s (%s)" % (s.zip_code, s.source(KEY_ZIP)) if s.zip_code else "coordinates from config"
        log.info("meshwx %s starting: %s; %s; %s; radio %s", __version__, where, schedule, alerts,
                 self._link.describe())

    def _shutdown(self) -> None:
        """Stop in order: no new input, no more sending, the worker, the radio, HTTP.

        Alert packets the sender discards here leave their ``seen_alerts``
        entries pending, so the next run announces those alerts again.
        """
        self._stop.set()
        self._link.on_message = None
        for what, step in (("sender", self._sender.stop), ("worker", self._stop_worker),
                           ("radio link", self._link.stop), ("HTTP client", self._close_http),
                           ("dry run's state copy", self._remove_scratch)):
            try:
                step()
            except Exception:  # noqa: BLE001 - finish the shutdown regardless
                log.exception("error stopping the %s", what)
        log.info("meshwx stopped")

    def _remove_scratch(self) -> None:
        if self._cleanup is not None:
            self._cleanup()

    def _stop_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._queue.put(_STOP)
        worker.join(WORKER_JOIN_SECONDS)
        if worker.is_alive():
            log.warning("worker still busy (slow network request?); not waiting for it")

    def _close_http(self) -> None:
        if self._http is not None:
            self._http.close()

    # ------------------------------------------------------------------ callbacks (other threads)
    def _on_message(self, msg: IncomingMessage) -> None:
        """Radio thread: just queue the message."""
        self._enqueue("command", msg)

    def _on_connection(self, up: bool) -> None:
        if not up:
            log.warning("radio link down; outgoing packets wait until it is back")
            return
        log.info("radio link up: %s", self._link.describe())
        if self._startup_broadcast_due:
            self._startup_broadcast_due = False
            self.broadcast_now("startup")

    def _on_setting_changed(self, key: str, value: Any) -> None:
        if key == KEY_INTERVAL:
            log.info("broadcast interval now %s; rescheduling", "%d min" % value if value else "off")
            self._scheduler.reschedule()
        elif key == KEY_ZIP:
            log.info("home ZIP now %s; dropping cached weather", value)
            self._alerts_active = None
            self._weather.invalidate()

    def _warm_up(self) -> None:
        """Start-up work off the main thread: resolve (and log) the home location, pre-load transit."""
        try:
            loc = self._weather.home_location()
        except MeshWxError as exc:
            log.warning("home location for ZIP %s not resolved yet: %s", self._settings.zip_code, exc)
        else:
            log.info("home location: %s (%.4f, %.4f)", loc.name, loc.latitude, loc.longitude)
        if self._transit is not None:
            self._transit.warm_up()

    # ------------------------------------------------------------------ work queue
    def _enqueue(self, kind: str, arg: Any = None) -> bool:
        """Queue a work item without ever blocking; False when it was dropped."""
        with self._queue_lock:
            if self._stop.is_set():
                return False
            if kind in _COALESCED and kind in self._waiting:
                log.debug("%s already queued", kind)
                return False
            if self._queue.qsize() >= MAX_PENDING_WORK:
                log.warning("work queue full (%d items); dropping a %s item", MAX_PENDING_WORK, kind)
                return False
            if kind in _COALESCED:
                self._waiting.add(kind)
            self._queue.put_nowait((kind, arg))
        return True

    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            kind, arg = item
            with self._queue_lock:
                self._waiting.discard(kind)
            if self._stop.is_set():
                continue  # shutting down: drop what is left
            try:
                self._handlers[kind](arg)
            except Exception:  # noqa: BLE001 - one bad item must not kill the worker
                log.exception("%s job failed", kind)

    def _send(self, msgs: List[OutgoingMessage], what: str) -> bool:
        """Hand packets to the sender; log when the queue refuses them."""
        if self._sender.enqueue(msgs):
            return True
        level = logging.DEBUG if self._stop.is_set() else logging.WARNING
        log.log(level, "%s dropped (%d packet(s)): send queue full", what, len(msgs))
        return False

    def _do_command(self, msg: IncomingMessage) -> None:
        replies = self._bot.handle(msg)
        if replies:
            self._send(replies, "reply to %s" % msg.from_id)

    def _do_broadcast(self, reason: str) -> None:
        """Broadcast the configured content; a part that cannot be fetched is left out."""
        b = self._cfg.broadcast
        now = timeutil.utcnow()
        report: Optional[WeatherReport] = None
        if any(item != "transit" for item in b.content):
            try:
                report = self._weather.get_report()
            except MeshWxError as exc:
                log.warning("%s broadcast: weather unavailable, leaving it out: %s", reason, exc)
            else:
                if report.alerts_supported and not report.stale:
                    self._alerts_active = len(report.alerts)
        arrivals: Optional[List[StopArrivals]] = None
        if "transit" in b.content and self._transit is not None:
            try:
                arrivals = self._transit.get_arrivals(now=now)
            except MeshWxError as exc:
                log.warning("%s broadcast: transit unavailable, leaving it out: %s", reason, exc)
        texts = self._formatter.broadcast(report, b.content, arrivals, now, self._cfg.transit.max_arrivals)
        if not texts:
            log.warning("%s broadcast skipped: nothing to send", reason)
            return
        msgs = [OutgoingMessage(text=t, destination=BROADCAST, channel_index=b.channel_index, kind="broadcast")
                for t in texts]
        if self._send(msgs, "%s broadcast" % reason):
            log.info("%s broadcast: %d packet(s) queued for channel %d", reason, len(msgs), b.channel_index)

    def _do_alert_check(self, _: Any = None) -> None:
        """Fetch the active alerts and broadcast the new ones (see :func:`select_new_alerts`).

        An announcement is tracked until all its packets are sent (see
        :meth:`_alert_packet_done`); meanwhile its ``seen_alerts`` entry, and
        those of alerts recorded silently because of it, carry ``"pending"``.
        Pending entries whose packets are no longer on their way (dropped,
        discarded at shutdown, or left by an earlier run) are forgotten first,
        so those alerts are announced again while they are in effect.
        """
        try:
            alerts, tz_name = self._weather.get_alerts()
        except MeshWxError as exc:
            log.warning("alert check failed: %s", exc)
            return
        self._alerts_active = len(alerts)
        place = self._location_name()  # outside the lock: it may need the network (normally cached)
        channel = self._alert_channel()
        with self._alerts_lock:  # the sender thread's delivery reports wait meanwhile
            stored = self._settings.get_data(SEEN_ALERTS_KEY)
            seen = stored if isinstance(stored, dict) else {}
            current = self._forget_undelivered(seen)
            to_broadcast, new_seen = select_new_alerts(alerts, current, self._cfg.broadcast.alert_min_severity,
                                                       timeutil.utcnow())
            _mark_pending(alerts, to_broadcast, current, new_seen)
            for alert in to_broadcast:
                report = functools.partial(self._alert_packet_done, alert.id)
                msgs = [OutgoingMessage(text=t, destination=BROADCAST, channel_index=channel, kind="alert",
                                        on_done=report)
                        for t in self._formatter.new_alert(alert, place, tz_name)]
                if not msgs:
                    log.warning("alert %s (%s): nothing to send", alert.id, alert.event)
                    _settle(new_seen, alert.id)
                    continue
                self._deliveries[alert.id] = len(msgs)  # before enqueue: it may already report a packet
                if self._send(msgs, "alert %s" % alert.id):
                    log.info("new alert broadcast on channel %d: %s (%s, %s)",
                             channel, alert.event, alert.severity or "unknown severity", alert.id)
                else:  # not announced: try again at the next check
                    self._deliveries.pop(alert.id, None)
                    for key in [k for k, v in new_seen.items() if v.get(PENDING_FIELD) == alert.id]:
                        del new_seen[key]
            for key in [k for k in self._delivery_failures if k not in new_seen and k not in self._deliveries]:
                del self._delivery_failures[key]  # that alert is over
            if new_seen != seen:
                self._settings.set_data(SEEN_ALERTS_KEY, new_seen)

    def _forget_undelivered(self, seen: Mapping[str, Any]) -> Dict[str, Any]:
        """``seen`` without the entries of announcements that are pending but no longer on their way.

        Lock held.  An alert whose announcement already failed
        :data:`MAX_ALERT_DELIVERIES` times in this run is kept as handled
        instead (e.g. a packet the radio always rejects), so it is not repeated
        every few minutes.
        """
        result: Dict[str, Any] = {}
        retry: Set[str] = set()
        give_up: Set[str] = set()
        for alert_id, entry in seen.items():
            key = entry.get(PENDING_FIELD) if isinstance(entry, dict) else None
            if key is None or key in self._deliveries:
                result[alert_id] = entry
            elif self._delivery_failures.get(key, 0) >= MAX_ALERT_DELIVERIES:
                give_up.add(key)
                result[alert_id] = {f: v for f, v in entry.items() if f != PENDING_FIELD}
            else:
                retry.add(key)
        for key in sorted(give_up):
            del self._delivery_failures[key]
            log.error("alert %s could not be sent %d times; giving up on it", key, MAX_ALERT_DELIVERIES)
        for key in sorted(retry):
            log.info("alert %s was not fully sent; announcing it again if it is still in effect", key)
        return result

    def _alert_packet_done(self, alert_id: str, msg: OutgoingMessage, outcome: str) -> None:
        """Delivery report for one packet announcing ``alert_id`` (sender thread, or whoever stops it).

        Every packet sent: the entries waiting for this announcement lose
        ``"pending"``, saved at once so a restart does not repeat the alert.
        A packet dropped or discarded: the announcement is over; its entries
        are forgotten at the next alert check - or the next run's first one -
        which announces the alert again while it is in effect.
        """
        with self._alerts_lock:
            left = self._deliveries.get(alert_id)
            if left is None:
                return  # an earlier packet already failed
            if outcome != OUTCOME_SENT:
                del self._deliveries[alert_id]
                self._delivery_failures[alert_id] = self._delivery_failures.get(alert_id, 0) + 1
                log.warning("alert %s: a packet was %s; the alert is announced again while it is in effect",
                            alert_id, outcome)
                return
            if left > 1:
                self._deliveries[alert_id] = left - 1
                return
            del self._deliveries[alert_id]
            self._delivery_failures.pop(alert_id, None)
            stored = self._settings.get_data(SEEN_ALERTS_KEY)
            if isinstance(stored, dict):
                done = dict(stored)
                _settle(done, alert_id)
                if done != stored:
                    self._settings.set_data(SEEN_ALERTS_KEY, done)
        log.debug("alert %s: every packet sent", alert_id)

    def _location_name(self) -> str:
        try:
            return self._weather.home_location().name
        except MeshWxError:
            return ""

    def _alert_channel(self) -> int:
        b = self._cfg.broadcast
        return b.alert_channel_index if b.alert_channel_index is not None else b.channel_index
