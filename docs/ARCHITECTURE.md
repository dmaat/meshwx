# meshwx architecture

meshwx is a small, long-running Python service for a Raspberry Pi Zero that
talks to a Meshtastic node over USB serial. It broadcasts weather for a
configurable ZIP code on a configurable interval, answers text commands, and
reports real-time transit arrivals.

```
                    +---------------------------- App (app.py) ----------------------------+
 config.yaml ---->  |  main thread: scheduler.poll(), alert-watch timer, settings.refresh()  |
 state.json  <--->  |  worker thread: one work queue -> CommandBot / broadcast / alert check |
                    |  sender thread: PacedSender (one packet every N seconds)               |
                    +-------^------------------------------|---------------------------------+
                            | IncomingMessage              | OutgoingMessage (<= max bytes)
                    +-------+------------------------------v---------------------------------+
                    |  MeshLink: MeshtasticLink (serial/tcp, auto-reconnect) | ConsoleLink   |
                    +-----------------------------------------------------------------------+
 WeatherService -> ZipGeocoder (zippopotam.us, Open-Meteo geocoding; cached on disk)
                -> WeatherProvider: NWS (api.weather.gov, alerts) | Open-Meteo (fallback)
 TransitService -> TransitProvider: gtfs_rt | mbta | onebusaway | transitland | siri
 Formatter      -> compact text, UTF-8-byte-aware chunking ("1/3" numbering)
```

## Package layout and ownership

| Module | Responsibility |
|---|---|
| `models.py` | Shared dataclasses (Location, WeatherReport, Arrival, IncomingMessage, OutgoingMessage, ...). **Contract - do not change field names.** |
| `errors.py` | Exception hierarchy; every error has a radio-safe `user_message`. |
| `config.py` | Config dataclasses, YAML loading (leading-zero-safe), validation. |
| `state.py` | `RuntimeSettings`: live ZIP code + broadcast interval, persisted overrides, misc app data. |
| `timeutil.py` | `parse_iso` (use it for ALL ISO timestamps), tz helpers, `clock_is_sane`. |
| `httpclient.py` | `HttpClient` (pooled requests session, retries, UA). Providers only use `get_json` / `get_bytes`. |
| `cache.py` | `TTLCache` (with stale reads), `read_json`, `atomic_write_json`. |
| `geocode.py` | `ZipGeocoder.lookup(zip, country) -> Location` with persistent cache. |
| `weather/` | `base.py` (interface), `nws.py`, `openmeteo.py`, `wmo.py`, `service.py` (`WeatherService`), `__init__.py` (factory). |
| `transit/` | `base.py` (interface), `gtfs_rt.py`, `gtfs_static.py`, `mbta.py`, `onebusaway.py`, `transitland.py`, `siri.py`, `service.py` (`TransitService`), `__init__.py` (factory). |
| `formatting.py` | `Formatter` + chunking helpers + unit conversion. The ONLY place that converts units for display. |
| `mesh/` | `base.py` (interface), `meshtastic_link.py`, `console_link.py` (dry run), `sender.py` (`PacedSender`), `__init__.py` (factory, port listing). |
| `scheduler.py` | `ScheduleSpec`, `next_run`, `BroadcastScheduler`. |
| `bot.py` | `CommandBot.handle(IncomingMessage) -> List[OutgoingMessage]`. |
| `app.py` | Wiring, threads, alert watcher, graceful shutdown. |
| `cli.py`, `__main__.py` | `meshwx run|preview|send|ports|set|status|setup|example-config`. |
| `setup_wizard.py` | Interactive config writer (fills the template). |
| `data/config.example.yaml` | Canonical config template (root `config.example.yaml` is an identical copy; a test enforces it). |

## Conventions (all code)

* Python 3.9+ compatible: `from __future__ import annotations` in every module;
  no `match`, no `X | Y` outside annotations, no `datetime.fromisoformat` on raw API strings (use `timeutil.parse_iso`).
* All datetimes are timezone-aware. Units inside models are SI (degC, km/h, hPa, mm, km).
* No module except `httpclient.py` imports `requests`. No test touches the network (use `tests/conftest.py::FakeHttp`).
* Logging via `logging.getLogger(__name__)`; never `print` except in `cli.py`, `setup_wizard.py` and `ConsoleLink`.
* Anything that can reach a mesh user must be short ASCII-safe text; use `errors.*.user_message`.
* Heavy/optional imports (`google.transit.gtfs_realtime_pb2`, `meshtastic`) are imported lazily inside the
  module that needs them so `meshwx preview` and unit tests work without a radio.
* Pi Zero budget: keep memory low (stream large files, filter early), avoid busy loops (sleep/wait on Events),
  minimise SD-card writes (state.json only when something changes).

## Threading model

* **meshtastic library threads** (serial reader + pubsub publisher) call `MeshLink.on_message`.
  The app only enqueues the message - never blocks there.
* **worker thread**: processes a FIFO of work items: `("command", IncomingMessage)`,
  `("broadcast", reason)`, `("alert_check", None)`. Network fetches happen here, one at a time.
* **sender thread** (`PacedSender`): pops `OutgoingMessage`s (priority alert < reply < broadcast, FIFO within a
  priority), waits until the link is up, sends one packet, sleeps `meshtastic.message_delay_seconds`.
* **main thread**: 1-2 s tick: stop flag, `settings.refresh()` (every ~30 s), `scheduler.poll()`,
  alert-watch timer, on-startup broadcast.
* pypubsub holds listeners by **weak reference**: subscribe bound methods of objects that stay alive
  (never lambdas/closures), and unsubscribe on stop.

## Meshtastic library facts (meshtastic 2.7.x, verified from source)

* `SerialInterface(devPath=None)` with **no** device found prints a message and returns a half-built object;
  with **several** devices it calls `sys.exit`. => Always resolve the port ourselves
  (`meshtastic.util.findPorts(True)`), pass an explicit `devPath`, raise `NoDeviceError` if none.
* Constructor blocks until config download completes (`connectNow=True`); raises on timeout.
* Reader thread exits on serial errors and publishes `meshtastic.connection.lost` (`interface=`) - also on our own
  `close()`. Ignore events whose `interface` is not the current one, and ignore "lost" during intentional shutdown.
* Receive topic `meshtastic.receive.text` with kwargs `packet`, `interface`. Packet dict keys (protobuf JSON names):
  `from` (int), `to` (int), `id`, `channel` (absent when 0), `decoded.text`, `decoded.portnum`,
  `rxTime`, `hopStart`, `hopLimit`, `viaMqtt`, `fromId` (may be None when the node DB lacks the node).
  DM = `packet["to"] == iface.myInfo.my_node_num`. Broadcast = `to == 0xFFFFFFFF`.
* `sendText(text, destinationId, wantAck, wantResponse, onResponse, channelIndex, portNum, replyId, hopLimit)`.
  `sendData` raises `MeshInterfaceError("Data payload too big")` above `DATA_PAYLOAD_LEN` = **233 bytes**.
  We cap at `meshtastic.max_message_bytes` (default 200; PKI DMs lose ~16 bytes of payload).
* `_sendToRadio` busy-waits for TX-queue space (firmware `queueStatus`), and `_writeBytes` sleeps 0.1 s.
* Library sends a heartbeat every 300 s.

## Severe-weather alert watch

When `broadcast.enabled` and `broadcast.alert_watch` are on and the provider supports alerts (NWS), the main
loop enqueues an `alert_check` every `broadcast.alert_check_minutes` (default 5). The worker fetches the home
location's active alerts and broadcasts each **new** alert whose severity is at least `alert_min_severity`:

* sent as `kind="alert"` => highest send priority (jumps ahead of queued replies/broadcasts);
* sent on `broadcast.alert_channel_index` (falls back to `broadcast.channel_index`);
* ignores quiet hours;
* de-duplicated by alert id, persisted in `state.json` (`seen_alerts`) so restarts don't repeat alerts;
  `Cancel` messages are recorded silently; any non-Cancel message whose `references` (including the ids
  NWS lists only in `parameters.expiredReferences`, merged in by `nws.map_alert`) contain an announced
  alert of the same event is a silent follow-up; an upgrade (different event, e.g. Watch -> Warning) is
  broadcast. End-of-event products (VTEC CAN/EXP/UPG, `messageType` Cancel, expired `urgency` Past) are
  dropped by the NWS provider itself, so `!alerts`, the `!wx` alert count and the watcher agree;
* delivery-confirmed: each alert packet carries an `OutgoingMessage.on_done` callback; the alert's
  `seen_alerts` entry carries `"pending"` until every packet reported `sent`. A `dropped`/`discarded`
  packet (long outage, send failures, shutdown) ends the delivery and the next check (or the first check
  after a restart) re-announces the alert if it is still active; after 3 failed deliveries in one run the
  alert is given up (logged at ERROR);
* severity rank: Minor 1, Moderate 2, Severe 3, Extreme 4, Unknown/missing 2.

`WeatherService.get_alerts()` remembers its last successful home fetch; reports served for the home
location (fresh, cached or stale) are overlaid with that newer alert list, and alerts whose `ends` has
passed are filtered in the same place, so the `!alerts N` numbering always matches. After a failed weather
refresh the service backs off 2-10 minutes per location (serving the stale copy without network I/O);
`get_alerts()` is never blocked by that backoff.

## Signals and shutdown

The SIGINT/SIGTERM handler only records the signal number (no locks, no logging - an `Event.set()` inside a
handler can deadlock the main thread). The main loop sleeps with `time.sleep(tick)` and acts on the recorded
signal within one tick. A second Ctrl+C raises `KeyboardInterrupt` to cut a slow shutdown short. Worst-case
clean shutdown is ~25 s; the systemd unit allows `TimeoutStopSec=60`.

## Transit horizon and visits

`TransitService` asks providers for `horizon_minutes + ceil(cache_seconds / 60)` (at most +60) and trims to
the real horizon on every read, so a cached answer stays complete for its lifetime. `transit.visit_time()`:
a vehicle already at the stop shows `now` only if it leaves within 90 s; otherwise (layover, timed hold) its
departure time is shown. GTFS-RT uses the static index (built in the background, including one streaming pass
over `stop_times.txt` limited to the routes of interest) to hide trips that end at the stop or don't pick up
there.

## Runtime-changeable settings

ZIP code and broadcast interval: `config.yaml` -> `meshwx run --zip/--interval` -> persisted overrides in
`state.json` (from mesh admin commands `setzip`/`setinterval` or `meshwx set zip|interval`).
A running service picks up `meshwx set` changes within ~30 s. See `state.py` docstring for precedence.

## Mesh commands

Channel messages need the prefix (default `!`), DMs don't. Case-insensitive.

| Command | Aliases | Who | Reply |
|---|---|---|---|
| `help` | `?`, `h`, `cmds` | all | command list |
| `wx [zip]` | `weather`, `w`, `now` | all | current conditions (+ alert count) |
| `fc [zip]` | `forecast`, `f` | all | forecast (1-3 packets) |
| `alerts [n]` | `alert`, `warn` | all | active alert list, or details of alert *n* |
| `bus [stop]` | `transit`, `next`, `t` | all (if transit enabled) | arrivals for all/one stop (label prefix or 1-based number) |
| `ping` | `test` | all | `pong` (+hops) |
| `status` | | all | ZIP, interval, next broadcast, link/queue state |
| `setzip <zip>` | `zip` | admins | change home ZIP (persisted) |
| `setinterval <min\|Nh\|off>` | `interval` | admins | change broadcast interval (persisted) |
| `broadcast` | `bcast` | admins | broadcast now |
