"""Configuration: dataclasses, YAML loading, validation.

The YAML file mirrors these dataclasses one-to-one (see ``config.example.yaml``).
Unknown keys are rejected with a "did you mean" hint so typos never silently
fall back to defaults.

Two settings can also be changed at runtime (mesh admin commands or
``meshwx set``): the ZIP code and the broadcast interval.  Those runtime
overrides live in :mod:`meshwx.state`, not here.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .errors import ConfigError
from .models import TransitStop

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 7 * 24 * 60
RECOMMENDED_MIN_INTERVAL = 30

WEATHER_PROVIDERS = ("nws", "openmeteo")
TRANSIT_PROVIDERS = ("gtfs_rt", "mbta", "onebusaway", "transitland", "siri")
BROADCAST_CONTENT = ("current", "forecast", "alerts", "transit")
SEVERITIES = ("Minor", "Moderate", "Severe", "Extreme")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

ENV_CONFIG = "MESHWX_CONFIG"
ENV_STATE_DIR = "MESHWX_STATE_DIR"


def default_config_paths() -> List[Path]:
    return [
        Path.cwd() / "config.yaml",
        Path.home() / ".config" / "meshwx" / "config.yaml",
        Path("/etc/meshwx/config.yaml"),
    ]


# =========================================================================== dataclasses


@dataclass
class LocationConfig:
    zip_code: str = ""
    country: str = "us"  # ISO-3166 alpha-2, lower case (zippopotam.us country code)
    latitude: Optional[float] = None  # optional manual override (skips geocoding)
    longitude: Optional[float] = None
    name: str = ""  # optional display name override, e.g. "Home"


@dataclass
class DisplayConfig:
    units: str = "imperial"  # imperial | metric
    ascii_only: bool = False  # replace degree signs etc. with ASCII (saves bytes)
    time_format: str = "12h"  # 12h | 24h
    number_parts: bool = True  # append " 1/3" to multi-packet messages


@dataclass
class WeatherConfig:
    provider: str = "nws"  # nws (US, with alerts) | openmeteo (worldwide)
    fallback_provider: str = "openmeteo"  # "" to disable
    forecast_periods: int = 4  # NWS half-day periods, or days for Open-Meteo
    include_alerts: bool = True
    cache_minutes: int = 10
    contact: str = ""  # optional contact (email/URL) for the User-Agent of every web request
    allow_other_zips: bool = True  # let mesh users ask "wx 90210"


@dataclass
class GtfsRtConfig:
    trip_updates_url: str = ""
    static_gtfs_url: str = ""  # optional: route names / headsigns / stop names
    headers: Dict[str, str] = field(default_factory=dict)  # header auth, e.g. {"x-api-key": "..."}
    params: Dict[str, str] = field(default_factory=dict)  # query auth, e.g. {"key": "..."} (CTA, TriMet appID)
    static_refresh_hours: int = 168
    max_feed_age_seconds: int = 300  # ignore a realtime feed whose header timestamp is older


@dataclass
class MbtaConfig:
    api_key: str = ""  # optional; raises rate limit from 20 to 1000 req/min
    base_url: str = "https://api-v3.mbta.com"


@dataclass
class SiriConfig:
    """SIRI StopMonitoring (511.org Bay Area, MTA Bus Time NYC, ...)."""

    url: str = "https://api.511.org/transit/StopMonitoring"
    api_key: str = ""
    key_param: str = "api_key"  # MTA Bus Time: "key"
    stop_param: str = "stopcode"  # MTA Bus Time: "MonitoringRef"
    params: Dict[str, str] = field(default_factory=dict)  # 511: {agency: SF, format: json}


@dataclass
class OneBusAwayConfig:
    base_url: str = ""  # e.g. https://api.pugetsound.onebusaway.org
    api_key: str = ""


@dataclass
class TransitlandConfig:
    api_key: str = ""
    base_url: str = "https://transit.land/api/v2/rest"


@dataclass
class TransitConfig:
    enabled: bool = False
    provider: str = "gtfs_rt"
    stops: List[TransitStop] = field(default_factory=list)
    max_arrivals: int = 3  # per route+destination group
    horizon_minutes: int = 90
    min_minutes: int = 0  # hide arrivals sooner than this (walking time)
    cache_seconds: int = 60
    gtfs_rt: GtfsRtConfig = field(default_factory=GtfsRtConfig)
    mbta: MbtaConfig = field(default_factory=MbtaConfig)
    onebusaway: OneBusAwayConfig = field(default_factory=OneBusAwayConfig)
    transitland: TransitlandConfig = field(default_factory=TransitlandConfig)
    siri: SiriConfig = field(default_factory=SiriConfig)


@dataclass
class MeshtasticConfig:
    connection: str = "serial"  # serial | tcp
    serial_port: str = ""  # "" = auto-detect; e.g. /dev/ttyUSB0, /dev/ttyACM0, /dev/meshtastic
    tcp_host: str = ""
    tcp_port: int = 4403
    reconnect_min_seconds: int = 10
    reconnect_max_seconds: int = 300
    max_message_bytes: int = 200  # per packet; hard protocol cap is 233 (less for PKI DMs)
    message_delay_seconds: float = 8.0  # pause between consecutive packets we send
    hop_limit: Optional[int] = None  # None = node's configured default
    dm_want_ack: bool = True
    skip_node_db: bool = False  # True = don't download the node DB on connect (faster on Pi Zero)
    max_queue: int = 40  # max packets waiting to be sent before new requests are refused


@dataclass
class BroadcastConfig:
    enabled: bool = True
    channel_index: int = 0  # tip: use a secondary channel (e.g. 1 named "Weather")
    interval_minutes: int = 180  # runtime-changeable; 0 disables scheduled broadcasts
    align_to_clock: bool = True  # e.g. 180 -> 00:00, 03:00, 06:00 ... local time
    times: List[str] = field(default_factory=list)  # ["07:00","12:00"]: overrides interval
    quiet_start: str = ""  # "22:00" - no scheduled broadcasts from here...
    quiet_end: str = ""  # "06:00" - ...until here (alerts still go out)
    content: List[str] = field(default_factory=lambda: ["current", "forecast", "alerts"])
    on_startup: bool = False  # broadcast once right after the radio connects
    alert_watch: bool = True  # broadcast NEW NWS alerts as soon as they appear
    alert_check_minutes: int = 5
    alert_min_severity: str = "Moderate"
    alert_channel_index: Optional[int] = None  # None = same channel as channel_index


@dataclass
class CommandsConfig:
    enabled: bool = True
    prefix: str = "!"  # required on channels, optional in DMs
    channels: List[int] = field(default_factory=lambda: [0])  # channel indexes to listen on
    reply_on_channel: bool = True  # False: answer channel commands by DM instead
    cooldown_seconds: int = 30  # per requesting node
    admins: List[str] = field(default_factory=list)  # node ids allowed to run admin commands


@dataclass
class Config:
    location: LocationConfig = field(default_factory=LocationConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    transit: TransitConfig = field(default_factory=TransitConfig)
    meshtastic: MeshtasticConfig = field(default_factory=MeshtasticConfig)
    broadcast: BroadcastConfig = field(default_factory=BroadcastConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    state_dir: str = ""  # "" = $MESHWX_STATE_DIR, $STATE_DIRECTORY (systemd) or ~/.local/state/meshwx
    log_level: str = "INFO"
    source_path: Optional[str] = field(default=None, compare=False)  # set by load_config

    # ------------------------------------------------------------------ derived paths
    def state_path(self) -> Path:
        if self.state_dir:
            return Path(os.path.expanduser(self.state_dir))
        return default_state_dir()

    def cache_path(self) -> Path:
        env = os.environ.get("CACHE_DIRECTORY", "").split(":")[0]
        if env and not self.state_dir:
            return Path(env)
        return self.state_path() / "cache"


def default_state_dir() -> Path:
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return Path(os.path.expanduser(env))
    systemd = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    if systemd:
        return Path(systemd)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "meshwx"


# =========================================================================== YAML


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps leading-zero numbers as strings.

    Plain YAML turns ``zip_code: 02134`` into the *octal* integer 1116 and
    ``01234`` into 668, silently corrupting ZIP codes and stop ids.  Here any
    integer scalar written with a leading zero stays the literal string.
    """


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Any:
    raw = str(loader.construct_scalar(node))
    # YAML 1.1 also reads "1_75403" as 175403 (OneBusAway stop ids) and "22:00" as the base-60
    # number 1320 (times).  Keep those literal too; numeric settings still accept them via _coerce.
    if "_" in raw or ":" in raw or re.fullmatch(r"[+-]?0\d+", raw):
        return raw
    return yaml.SafeLoader.construct_yaml_int(loader, node)


def _construct_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Any:
    raw = str(loader.construct_scalar(node))
    if ":" in raw:  # YAML 1.1 base-60 float, e.g. an unquoted "7:30.5"
        return raw
    return yaml.SafeLoader.construct_yaml_float(loader, node)


_Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_Loader.add_constructor("tag:yaml.org,2002:float", _construct_float)


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """One readable line ("invalid YAML at line 5, column 20: ...") instead of PyYAML's caret block."""
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is not None and problem:
        return "invalid YAML at line %d, column %d: %s (check the indentation and quoting there)" % (
            mark.line + 1, mark.column + 1, problem)
    return "invalid YAML: %s" % " ".join(str(exc).split())


def load_yaml_text(text: str) -> Dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(_yaml_error_message(exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a mapping at the top level")
    return data


# =========================================================================== dict -> dataclass


def _unknown_key_error(path: str, key: str, known: List[str]) -> ConfigError:
    hint = difflib.get_close_matches(str(key), known, n=1)
    where = "%s.%s" % (path, key) if path else str(key)
    msg = "unknown setting '%s'" % where
    if hint:
        msg += " (did you mean '%s'?)" % hint[0]
    return ConfigError(msg)


def _build(cls: Any, data: Any, path: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("'%s' must be a mapping of settings" % (path or "config"))
    hints = typing.get_type_hints(cls)
    init_fields = [f for f in fields(cls) if f.init and f.name != "source_path"]
    known = [f.name for f in init_fields]
    for key in data:
        if key not in known:
            raise _unknown_key_error(path, key, known)
    kwargs = {}
    for f in init_fields:
        if f.name in data:
            sub = "%s.%s" % (path, f.name) if path else f.name
            kwargs[f.name] = _coerce(hints[f.name], data[f.name], sub)
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError("'%s': %s" % (path or "config", exc)) from exc


def _build_stop(data: Any, path: str) -> TransitStop:
    if isinstance(data, (str, int)) and not isinstance(data, bool):
        data = {"id": str(data)}
    if not isinstance(data, dict):
        raise ConfigError("'%s' must be a mapping with at least an 'id'" % path)
    if "id" not in data or data["id"] in (None, ""):
        raise ConfigError("'%s' is missing 'id'" % path)
    data = dict(data)
    if not data.get("label"):
        data["label"] = str(data["id"])
    routes = data.get("routes")
    if routes is None:
        data["routes"] = []
    elif not isinstance(routes, list):
        data["routes"] = [routes]
    return _build(TransitStop, data, path)


def _coerce(tp: Any, value: Any, path: str) -> Any:  # noqa: C901 - a type switch
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if tp is Any:
        return value
    if origin is typing.Union:
        options = [a for a in args if a is not type(None)]
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none")):
            return None
        return _coerce(options[0], value, path)
    if tp is TransitStop:
        return _build_stop(value, path)
    if is_dataclass(tp):
        return _build(tp, value, path)
    if origin in (list, List):
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return [_coerce(args[0], v, "%s[%d]" % (path, i)) for i, v in enumerate(value)]
    if origin in (tuple, Tuple):
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            value = [value]
        return tuple(_coerce(args[0], v, "%s[%d]" % (path, i)) for i, v in enumerate(value))
    if origin in (dict, Dict):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError("'%s' must be a mapping" % path)
        return {str(k): _coerce(args[1], v, "%s.%s" % (path, k)) for k, v in value.items()}
    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
            return False
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ConfigError("'%s' must be true or false (got %r)" % (path, value))
    if tp is int:
        if isinstance(value, bool):
            raise ConfigError("'%s' must be a whole number (got %r)" % (path, value))
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d[\d_]*\s*", value):
            return int(value.strip().replace("_", ""), 10)
        raise ConfigError("'%s' must be a whole number (got %r)" % (path, value))
    if tp is float:
        if isinstance(value, bool):
            raise ConfigError("'%s' must be a number (got %r)" % (path, value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ConfigError("'%s' must be a number (got %r)" % (path, value))
    if tp is str:
        if value is None:
            return ""
        if isinstance(value, bool):
            raise ConfigError("'%s' must be text (got %r)" % (path, value))
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ConfigError("'%s' must be text (got %r)" % (path, value))
    raise ConfigError("'%s': unsupported setting type %r" % (path, tp))  # pragma: no cover


def config_from_dict(data: Dict[str, Any]) -> Config:
    """Build a validated :class:`Config` from a parsed YAML mapping.

    Raises :class:`ConfigError` for invalid input.  Non-fatal problems are
    logged as warnings (see :func:`validate`).
    """
    cfg = _build(Config, data, "")
    for warning in validate(cfg):
        log.warning("config: %s", warning)
    return cfg


# =========================================================================== normalisers


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str, path: str = "time") -> Tuple[int, int]:
    m = _HHMM.match(str(text).strip())
    if not m:
        raise ConfigError("'%s' must be a 24-hour time like \"07:30\" in quotes (got %r)" % (path, text))
    return int(m.group(1)), int(m.group(2))


def normalize_zip(text: Any, country: str = "us") -> str:
    """Return a canonical postal code or raise :class:`ConfigError`.

    US: accepts "02108", "02108-1234", " 02108 " -> "02108".
    Other countries: trimmed, upper-cased, internal whitespace collapsed.
    """
    if text is None or isinstance(text, bool):
        raise ConfigError("ZIP code is empty")
    s = str(text).strip()
    if country.lower() == "us":
        m = re.fullmatch(r"(\d{5})(?:[-\s]?\d{4})?", s)
        if not m:
            raise ConfigError(
                "ZIP code must be 5 digits, e.g. \"02108\" (got %r). In YAML, put it in quotes." % s,
                user_message="ZIP must be 5 digits",
            )
        return m.group(1)
    s = re.sub(r"\s+", " ", s).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{1,9}", s):
        raise ConfigError("invalid postal code %r" % s, user_message="Invalid postal code")
    return s


def normalize_node_id(value: Any) -> str:
    """Accept "!a1b2c3d4", "a1b2c3d4" (or a YAML int of 8 hex digits) -> "!a1b2c3d4"."""
    s = str(value).strip().lower()
    if s.startswith("!"):
        s = s[1:]
    if not re.fullmatch(r"[0-9a-f]{8}", s):
        raise ConfigError(
            "node id %r must look like \"!a1b2c3d4\" (8 hex digits; quote it in YAML)" % value
        )
    return "!" + s


# =========================================================================== validation


def validate(cfg: Config) -> List[str]:  # noqa: C901 - flat list of checks
    """Normalise ``cfg`` in place, raise :class:`ConfigError` on errors, return warnings."""
    warnings: List[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ConfigError(msg)

    # ---------------------------------------------------------------- location
    loc = cfg.location
    loc.country = (loc.country or "us").strip().lower()
    need(re.fullmatch(r"[a-z]{2}", loc.country) is not None, "location.country must be a 2-letter code like 'us'")
    has_ll = loc.latitude is not None or loc.longitude is not None
    if has_ll:
        need(loc.latitude is not None and loc.longitude is not None,
             "location.latitude and location.longitude must be set together")
        need(-90 <= loc.latitude <= 90, "location.latitude must be between -90 and 90")  # type: ignore[operator]
        need(-180 <= loc.longitude <= 180, "location.longitude must be between -180 and 180")  # type: ignore[operator]
    if loc.zip_code:
        loc.zip_code = normalize_zip(loc.zip_code, loc.country)
    need(bool(loc.zip_code) or has_ll, "set location.zip_code (e.g. \"02108\") or latitude+longitude")

    # ---------------------------------------------------------------- display
    d = cfg.display
    d.units = d.units.strip().lower()
    need(d.units in ("imperial", "metric"), "display.units must be 'imperial' or 'metric'")
    d.time_format = d.time_format.strip().lower()
    need(d.time_format in ("12h", "24h"), "display.time_format must be '12h' or '24h'")

    # ---------------------------------------------------------------- weather
    w = cfg.weather
    w.provider = w.provider.strip().lower()
    need(w.provider in WEATHER_PROVIDERS, "weather.provider must be one of %s" % ", ".join(WEATHER_PROVIDERS))
    w.fallback_provider = (w.fallback_provider or "").strip().lower()
    if w.fallback_provider in ("none", "off", "false"):
        w.fallback_provider = ""
    need(w.fallback_provider in ("",) + WEATHER_PROVIDERS,
         "weather.fallback_provider must be '' or one of %s" % ", ".join(WEATHER_PROVIDERS))
    if w.fallback_provider == w.provider:
        w.fallback_provider = ""
    need(1 <= w.forecast_periods <= 14, "weather.forecast_periods must be 1-14")
    need(1 <= w.cache_minutes <= 360, "weather.cache_minutes must be 1-360")
    if w.provider == "nws" and loc.country != "us":
        warnings.append("weather.provider 'nws' only covers the US; consider 'openmeteo'")

    # ---------------------------------------------------------------- transit
    t = cfg.transit
    t.provider = t.provider.strip().lower()
    need(t.provider in TRANSIT_PROVIDERS, "transit.provider must be one of %s" % ", ".join(TRANSIT_PROVIDERS))
    need(1 <= t.max_arrivals <= 10, "transit.max_arrivals must be 1-10")
    need(5 <= t.horizon_minutes <= 24 * 60, "transit.horizon_minutes must be 5-1440")
    need(0 <= t.min_minutes < t.horizon_minutes, "transit.min_minutes must be >= 0 and < horizon_minutes")
    need(10 <= t.cache_seconds <= 3600, "transit.cache_seconds must be 10-3600")
    labels = [s.label.lower() for s in t.stops]
    if len(set(labels)) != len(labels):
        warnings.append("transit.stops have duplicate labels; 'bus <label>' will match the first")
    if t.enabled:
        need(len(t.stops) > 0, "transit.enabled is true but transit.stops is empty")
        if t.provider == "gtfs_rt":
            need(bool(t.gtfs_rt.trip_updates_url), "transit.gtfs_rt.trip_updates_url is required for provider gtfs_rt")
            if not t.gtfs_rt.static_gtfs_url:
                warnings.append("transit.gtfs_rt.static_gtfs_url not set: routes show as ids and no destinations")
        elif t.provider == "onebusaway":
            need(bool(t.onebusaway.base_url), "transit.onebusaway.base_url is required")
            need(bool(t.onebusaway.api_key), "transit.onebusaway.api_key is required")
        elif t.provider == "transitland":
            need(bool(t.transitland.api_key), "transit.transitland.api_key is required (free at transit.land)")
        elif t.provider == "siri":
            need(bool(t.siri.url), "transit.siri.url is required")
            need(bool(t.siri.api_key), "transit.siri.api_key is required")
            need(bool(t.siri.key_param) and bool(t.siri.stop_param),
                 "transit.siri.key_param and stop_param must not be empty")
    need(30 <= t.gtfs_rt.max_feed_age_seconds <= 86400, "transit.gtfs_rt.max_feed_age_seconds must be 30-86400")
    need(1 <= t.gtfs_rt.static_refresh_hours <= 24 * 90, "transit.gtfs_rt.static_refresh_hours must be 1-2160")
    if "transit" in cfg.broadcast.content and not t.enabled:
        warnings.append("broadcast.content includes 'transit' but transit.enabled is false")

    # ---------------------------------------------------------------- meshtastic
    m = cfg.meshtastic
    m.connection = m.connection.strip().lower()
    need(m.connection in ("serial", "tcp"), "meshtastic.connection must be 'serial' or 'tcp'")
    if m.connection == "tcp":
        need(bool(m.tcp_host), "meshtastic.tcp_host is required when connection is 'tcp'")
    need(1 <= m.tcp_port <= 65535, "meshtastic.tcp_port must be 1-65535")
    need(60 <= m.max_message_bytes <= 228, "meshtastic.max_message_bytes must be 60-228")
    if m.max_message_bytes > 200:
        warnings.append("max_message_bytes > 200: some apps truncate, and PKI-encrypted DMs allow ~217")
    need(0.5 <= m.message_delay_seconds <= 300, "meshtastic.message_delay_seconds must be 0.5-300")
    if m.message_delay_seconds < 3:
        warnings.append("message_delay_seconds < 3 may flood the mesh / overflow the radio TX queue")
    if m.hop_limit is not None:
        need(0 <= m.hop_limit <= 7, "meshtastic.hop_limit must be 0-7 (or null)")
    need(1 <= m.reconnect_min_seconds <= m.reconnect_max_seconds,
         "meshtastic.reconnect_min_seconds must be >= 1 and <= reconnect_max_seconds")
    need(5 <= m.max_queue <= 500, "meshtastic.max_queue must be 5-500")

    # ---------------------------------------------------------------- broadcast
    b = cfg.broadcast
    need(0 <= b.channel_index <= 7, "broadcast.channel_index must be 0-7")
    need(b.interval_minutes == 0 or MIN_INTERVAL_MINUTES <= b.interval_minutes <= MAX_INTERVAL_MINUTES,
         "broadcast.interval_minutes must be 0 (off) or %d-%d" % (MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES))
    if 0 < b.interval_minutes < RECOMMENDED_MIN_INTERVAL:
        warnings.append("broadcast every %d min is a lot of airtime; 60+ is kinder to the mesh" % b.interval_minutes)
    norm_times = []
    for i, tm in enumerate(b.times):
        h, mi = parse_hhmm(tm, "broadcast.times[%d]" % i)
        norm_times.append("%02d:%02d" % (h, mi))
    b.times = sorted(set(norm_times))
    need(bool(b.quiet_start) == bool(b.quiet_end), "broadcast.quiet_start and quiet_end must be set together")
    if b.quiet_start:
        b.quiet_start = "%02d:%02d" % parse_hhmm(b.quiet_start, "broadcast.quiet_start")
        b.quiet_end = "%02d:%02d" % parse_hhmm(b.quiet_end, "broadcast.quiet_end")
    b.content = [c.strip().lower() for c in b.content]
    for c in b.content:
        need(c in BROADCAST_CONTENT, "broadcast.content items must be among %s (got %r)" % (", ".join(BROADCAST_CONTENT), c))
    need(len(b.content) > 0, "broadcast.content must list at least one of %s" % ", ".join(BROADCAST_CONTENT))
    need(1 <= b.alert_check_minutes <= 240, "broadcast.alert_check_minutes must be 1-240")
    sev = b.alert_min_severity.strip().capitalize()
    need(sev in SEVERITIES, "broadcast.alert_min_severity must be one of %s" % ", ".join(SEVERITIES))
    b.alert_min_severity = sev
    if b.alert_channel_index is not None:
        need(0 <= b.alert_channel_index <= 7, "broadcast.alert_channel_index must be 0-7 (or null)")
    if b.channel_index == 0 and b.enabled:
        warnings.append("broadcasting on channel 0 (primary); a dedicated secondary channel is more polite")

    # ---------------------------------------------------------------- commands
    c = cfg.commands
    c.prefix = (c.prefix or "").strip()
    need(len(c.prefix) <= 3, "commands.prefix must be at most 3 characters")
    for ch in c.channels:
        need(0 <= ch <= 7, "commands.channels entries must be 0-7")
    need(c.cooldown_seconds >= 0, "commands.cooldown_seconds must be >= 0")
    c.admins = [normalize_node_id(a) for a in c.admins]

    # ---------------------------------------------------------------- misc
    cfg.log_level = cfg.log_level.strip().upper()
    need(cfg.log_level in LOG_LEVELS, "log_level must be one of %s" % ", ".join(LOG_LEVELS))
    return warnings


# =========================================================================== loading


def find_config_path(explicit: Optional[str] = None) -> Optional[Path]:
    """Return the config file to use, or None if none exists."""
    if explicit:
        return Path(os.path.expanduser(explicit))
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(os.path.expanduser(env))
    for p in default_config_paths():
        if p.is_file():
            return p
    return None


def load_config(path: Optional[str] = None) -> Config:
    """Load, parse and validate the config file.

    Search order: ``path`` argument, ``$MESHWX_CONFIG``, ./config.yaml,
    ~/.config/meshwx/config.yaml, /etc/meshwx/config.yaml.
    """
    p = find_config_path(path)
    if p is None:
        raise ConfigError(
            "no config file found (looked for %s). Run 'meshwx setup' or copy config.example.yaml to config.yaml"
            % ", ".join(str(x) for x in default_config_paths())
        )
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (p, exc)) from exc
    try:
        cfg = config_from_dict(load_yaml_text(text))
    except ConfigError as exc:
        raise ConfigError("%s: %s" % (p, exc), user_message=exc.user_message) from exc
    cfg.source_path = str(p)
    return cfg
