# Module contracts

Public APIs that modules use to talk to each other. Change a signature here
only together with its callers and tests. Types refer to
`meshwx.models`, `meshwx.config`, `meshwx.state`, `meshwx.httpclient`.

---------------------------------------------------------------------------

## geocode.py

```python
class ZipGeocoder:
    def __init__(self, http: HttpClient, cache_file: Optional[Path] = None) -> None
    def lookup(self, zip_code: str, country: str = "us") -> Location
```
* Normalise with `config.normalize_zip` (a `ConfigError` becomes `GeocodeError` keeping its `user_message`).
* Cache: in-memory + persistent JSON (`cache.read_json` / `atomic_write_json`) keyed `"us:02108"`; thread-safe.
* Sources: zippopotam.us first, Open-Meteo geocoding (postal code search) as fallback.
* Unknown ZIP -> `GeocodeError(user_message="ZIP 12345 not found")`.
  Network failure with no cache -> `GeocodeError(user_message="ZIP lookup failed, try later")`.
* `Location.name` = `"City, ST"` for US; `timezone` filled when a source provides it.

## weather/

```python
# weather/__init__.py
def create_weather_provider(name: str, http: HttpClient, cfg: Config) -> WeatherProvider  # "nws" | "openmeteo"

# weather/nws.py         class NwsProvider(WeatherProvider):       name="nws", supports_alerts=True;  __init__(self, http, cfg)
# weather/openmeteo.py   class OpenMeteoProvider(WeatherProvider): name="openmeteo";                  __init__(self, http, cfg)
# weather/wmo.py         WMO_CODES: Dict[int, str];  def describe(code: Optional[int]) -> Optional[str]

# weather/service.py
class WeatherService:
    def __init__(self, cfg: Config, settings: RuntimeSettings, http: HttpClient, *,
                 geocoder: Optional[ZipGeocoder] = None,
                 providers: Optional[Dict[str, WeatherProvider]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None
    def home_location(self) -> Location
    def location_for_zip(self, zip_code: str) -> Location
    def get_report(self, zip_code: Optional[str] = None, *, force: bool = False) -> WeatherReport
    def get_alerts(self) -> Tuple[List[WeatherAlert], Optional[str]]   # (alerts, IANA tz) for the home location
    def alerts_supported(self) -> bool
    def invalidate(self) -> None
```
* `home_location`: config lat/lon win **unless** `settings.zip_is_override`; otherwise geocode `settings.zip_code`.
  `cfg.location.name` overrides the display name only while the ZIP is not overridden.
* `get_report`: cache per location (lat/lon rounded to 3 dp) for `weather.cache_minutes`; primary provider, then
  fallback provider (add a note like `"via Open-Meteo"`); if both fail serve a cached report up to 3 h old with
  `stale=True`; else raise `WeatherError`. Uses `weather.forecast_periods` and `weather.include_alerts`.

## httpclient.py additions

`HttpClient.download(url, dest, *, params, headers, timeout, etag, last_modified, max_bytes, chunk_size) -> HttpBytes`
streams a large body to `dest` atomically (temp file + rename; 304 leaves `dest` untouched, `not_modified=True`;
`content` is always `b""`). Use it for static GTFS zips - never hold them in RAM. `FakeHttp.download` mirrors it.

## transit/

Providers (one module each): `gtfs_rt.py` `GtfsRtProvider` (+ `gtfs_static.py` `GtfsStaticIndex`),
`mbta.py` `MbtaProvider`, `onebusaway.py` `OneBusAwayProvider`, `transitland.py` `TransitlandProvider`,
`siri.py` `SiriProvider`. Each: `__init__(self, cfg: TransitConfig, http: HttpClient, cache_dir: Path)`.

```python
# transit/__init__.py
def create_transit_provider(cfg: TransitConfig, http: HttpClient, cache_dir: Path) -> TransitProvider

# transit/service.py
class TransitService:
    def __init__(self, cfg: TransitConfig, provider: TransitProvider, *,
                 clock: Callable[[], float] = time.monotonic) -> None
    @property
    def stops(self) -> List[TransitStop]
    def find_stop(self, query: str) -> Optional[TransitStop]   # "2" = 2nd stop; else label prefix (case-insens.), else exact id
    def get_arrivals(self, stops: Optional[Sequence[TransitStop]] = None,
                     now: Optional[datetime] = None) -> List[StopArrivals]
    def warm_up(self) -> None   # optional pre-load (e.g. GTFS static index) - safe to call from a background thread
```
* `get_arrivals` caches for `transit.cache_seconds` (key = tuple of `TransitStop` objects, so the same id
  with different route filters is kept apart), asks the provider for `horizon_minutes + ceil(cache_seconds/60)`
  and re-trims to the horizon on every read, hides arrivals sooner than `min_minutes`, keeps at most 20 per
  stop. Grouping/`max_arrivals` is the formatter's job.
* A per-stop `StopArrivals.error` is a short, ASCII, user-facing text ("Stop data loading, try later",
  "Stop not found", "Rate limited, try later", ...) that the formatter shows as `"<label>: <error>"`.

## formatting.py

```python
def utf8_len(s: str) -> int
def ascii_fold(s: str) -> str
def split_text(text: str, max_bytes: int) -> List[str]          # word-aware, never splits a code point, no empty chunks
def pack_segments(segments: Sequence[str], max_bytes: int, *, sep: str = "\n",
                  number_parts: bool = True) -> List[str]       # greedy; oversize segment -> split_text; " 1/3" suffix
def compass(deg: Optional[float]) -> Optional[str]              # 16-point
def c_to_f(c: float) -> float
def kmh_to_mph(v: float) -> float
def hpa_to_inhg(v: float) -> float
def mm_to_in(v: float) -> float
def km_to_mi(v: float) -> float

class Formatter:
    def __init__(self, display: DisplayConfig, max_bytes: int, *,
                 default_tz: Optional[tzinfo] = None, command_prefix: str = "!") -> None
    max_bytes: int
    def current(self, report: WeatherReport) -> List[str]
    def forecast(self, report: WeatherReport, periods: Optional[int] = None) -> List[str]
    def alerts(self, report: WeatherReport) -> List[str]
    def alert_detail(self, alert: WeatherAlert, index: int, tz_name: Optional[str], max_parts: int = 3) -> List[str]
    def new_alert(self, alert: WeatherAlert, location_name: str, tz_name: Optional[str]) -> List[str]
    def broadcast(self, report: Optional[WeatherReport], content: Sequence[str],
                  transit: Optional[Sequence[StopArrivals]] = None, now: Optional[datetime] = None,
                  max_arrivals: int = 3) -> List[str]
    def transit(self, results: Sequence[StopArrivals], now: datetime, max_arrivals: int = 3) -> List[str]
    def text(self, text: str) -> List[str]
    def help(self, transit_enabled: bool, is_admin: bool) -> List[str]
    def status(self, info: Mapping[str, Any]) -> List[str]       # info = App.status()
    def error(self, user_message: str) -> List[str]              # exactly one packet
```
Every returned string is `<= max_bytes` UTF-8 bytes **including** part numbering, and is ASCII-folded when
`display.ascii_only`. Times are shown in the report/location timezone (fallback `default_tz`).

Transit countdown rule (all providers, per the MBTA real-time display guidelines): `secs = arrival.time - now`;
show `now` when `secs <= 30` or when `arrival.status` is a short "at the stop" status (Boarding / Arriving /
Approaching, case-insensitive); otherwise `(secs + 30) // 60` minutes (round half up).

## mesh/

```python
# mesh/__init__.py
def create_link(cfg: Config, *, dry_run: bool = False, port_override: Optional[str] = None) -> MeshLink
def list_serial_ports() -> List[Tuple[str, str, bool]]   # (device, description, likely_meshtastic)

# mesh/meshtastic_link.py
class MeshtasticLink(MeshLink):
    def __init__(self, cfg: MeshtasticConfig, *, port_override: Optional[str] = None,
                 interface_factory: Optional[Callable[..., Any]] = None) -> None

# mesh/console_link.py   (dry run: prints TX to stdout; each stdin line = DM from a fake node, "#N text" = channel N)
class ConsoleLink(MeshLink):
    def __init__(self, max_bytes: int, *, input_stream: Optional[TextIO] = None,
                 output_stream: Optional[TextIO] = None, read_input: bool = True) -> None

# mesh/sender.py
class PacedSender:
    def __init__(self, link: MeshLink, delay_seconds: float, *, max_queue: int = 40,
                 max_age_seconds: float = 900, max_bytes: int = 228, retries: int = 2) -> None
    def start(self) -> None
    def stop(self, timeout: float = 5.0) -> None
    def enqueue(self, msgs: Sequence[OutgoingMessage]) -> bool   # all-or-nothing; False when full
    def pending(self) -> int
    def wait_idle(self, timeout: float) -> bool
```

Every accepted message gets exactly one `msg.on_done(msg, outcome)` call (if set), outside the queue lock:
`models.OUTCOME_SENT`, `OUTCOME_DROPPED` (too old / too big / retries exhausted) or `OUTCOME_DISCARDED`
(still queued at `stop()`). A refused `enqueue` reports nothing. `pending()`/`wait_idle()` count a message
until its callback has returned.

## scheduler.py

```python
@dataclass
class ScheduleSpec:
    interval_minutes: int
    align_to_clock: bool = True
    times: List[str] = field(default_factory=list)   # "HH:MM"; non-empty overrides interval
    quiet_start: str = ""
    quiet_end: str = ""
    @classmethod
    def from_config(cls, b: BroadcastConfig, interval_minutes: int) -> "ScheduleSpec"

def in_quiet_hours(spec: ScheduleSpec, t: datetime) -> bool
def next_run(spec: ScheduleSpec, after: datetime, *, anchor: Optional[datetime] = None) -> Optional[datetime]

class BroadcastScheduler:
    def __init__(self, spec_fn: Callable[[], ScheduleSpec], *, tz: Optional[tzinfo] = None,
                 now_fn: Optional[Callable[[], datetime]] = None, grace_minutes: int = 15) -> None
    def poll(self) -> bool                 # True exactly once per due slot
    @property
    def next_run_at(self) -> Optional[datetime]
    def reschedule(self) -> None           # recompute (call after interval change)
```

## bot.py

```python
@dataclass
class BotContext:
    cfg: Config
    settings: RuntimeSettings
    weather: WeatherService
    transit: Optional[TransitService]
    formatter: Formatter
    status_fn: Callable[[], Dict[str, Any]]
    broadcast_fn: Callable[[], None]

class CommandBot:
    def __init__(self, ctx: BotContext, *, clock: Callable[[], float] = time.monotonic) -> None
    def parse(self, msg: IncomingMessage) -> Optional[Tuple[str, List[str]]]
    def handle(self, msg: IncomingMessage) -> List[OutgoingMessage]
```

## app.py / cli.py / logsetup.py / setup_wizard.py

```python
class App:
    @classmethod
    def build(cls, cfg: Config, *, dry_run: bool = False, port: Optional[str] = None,
              cli_zip: Optional[str] = None, cli_interval: Optional[Any] = None) -> "App"
    def run(self) -> int
    def request_stop(self) -> None
    def status(self) -> Dict[str, Any]
    def broadcast_now(self, reason: str = "manual") -> None

def main(argv: Optional[List[str]] = None) -> int                      # cli.py
def configure_logging(level: str = "INFO") -> None                     # logsetup.py
def run_wizard(dest: Path, *, input_fn=input, print_fn=print,
               port_lister=None, geocoder=None) -> int                 # setup_wizard.py
```

`App.status()` keys: `version`, `zip_code`, `zip_source`, `location` (display name or None),
`interval_minutes`, `interval_source`, `times` (list), `next_broadcast` (aware datetime or None),
`broadcast_enabled`, `channel_index`, `link` ("up"|"down"), `link_desc`, `queue` (int), `uptime_s` (int),
`alerts_active` (int or None), `transit_enabled` (bool).
