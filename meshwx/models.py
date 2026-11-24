"""Plain data models shared by every meshwx module.

Conventions (all modules must follow them):

* Every ``datetime`` is timezone-aware.  Providers return UTC or the source's
  own offset; formatting converts to the display timezone.
* Physical quantities are stored in SI-ish units (degC, km/h, hPa, mm, km).
  Unit conversion for display happens only in :mod:`meshwx.formatting`.
* ``None`` means "unknown / not reported" - never use 0 as a placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple, Union

#: Meshtastic broadcast destination (same value as ``meshtastic.BROADCAST_ADDR``).
BROADCAST = "^all"


# --------------------------------------------------------------------------- location


@dataclass(frozen=True)
class Location:
    """A geocoded place."""

    latitude: float
    longitude: float
    name: str  # display name, e.g. "Boston, MA"
    zip_code: Optional[str] = None
    country: str = "US"
    timezone: Optional[str] = None  # IANA name (e.g. "America/New_York") when known


# --------------------------------------------------------------------------- weather


@dataclass
class CurrentConditions:
    description: Optional[str] = None  # "Partly Cloudy"
    temperature_c: Optional[float] = None
    feels_like_c: Optional[float] = None  # heat index / wind chill / apparent temp
    humidity_pct: Optional[float] = None
    dewpoint_c: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    wind_gust_kmh: Optional[float] = None
    wind_dir_deg: Optional[float] = None  # meteorological degrees (direction wind comes FROM)
    pressure_hpa: Optional[float] = None  # sea-level / barometric pressure
    visibility_km: Optional[float] = None
    observed_at: Optional[datetime] = None
    station: Optional[str] = None  # e.g. "KBOS" or "Open-Meteo"


@dataclass
class ForecastPeriod:
    """One forecast block.

    NWS returns half-day periods ("Tonight", "Friday"): a daytime period sets
    ``temp_high_c``; a night period sets ``temp_low_c``.  Daily providers
    (Open-Meteo) set both and leave ``is_daytime`` as ``None``.
    """

    name: str  # "Tonight", "Friday", or a short day name such as "Fri"
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    is_daytime: Optional[bool] = None
    temp_high_c: Optional[float] = None
    temp_low_c: Optional[float] = None
    description: str = ""  # short forecast text, e.g. "Chance Showers"
    precip_chance_pct: Optional[float] = None
    precip_mm: Optional[float] = None
    wind_speed_kmh: Optional[float] = None  # max sustained when the source gives a range
    wind_gust_kmh: Optional[float] = None
    wind_dir: Optional[str] = None  # compass point, e.g. "NW"
    detailed: Optional[str] = None  # long narrative text when available


@dataclass
class WeatherAlert:
    id: str
    event: str  # "Wind Advisory"
    headline: Optional[str] = None
    severity: Optional[str] = None  # Extreme | Severe | Moderate | Minor | Unknown
    urgency: Optional[str] = None
    certainty: Optional[str] = None
    message_type: Optional[str] = None  # Alert | Update | Cancel
    onset: Optional[datetime] = None
    ends: Optional[datetime] = None  # "ends" when present, otherwise "expires"
    description: Optional[str] = None
    instruction: Optional[str] = None
    area: Optional[str] = None
    sender: Optional[str] = None
    references: Tuple[str, ...] = ()  # ids of earlier alerts this one updates/cancels


@dataclass
class WeatherReport:
    location: Location
    provider: str  # "nws" | "openmeteo"
    fetched_at: datetime
    timezone: Optional[str] = None  # IANA tz of the location, used for display
    current: Optional[CurrentConditions] = None
    forecast: List[ForecastPeriod] = field(default_factory=list)
    alerts: List[WeatherAlert] = field(default_factory=list)
    alerts_supported: bool = False  # False when the provider has no alert feed
    alerts_error: Optional[str] = None  # user-safe text when the alert feed exists but failed
    stale: bool = False  # True when served from cache after a failed refresh
    notes: List[str] = field(default_factory=list)  # e.g. "obs 3h old"


# --------------------------------------------------------------------------- transit


@dataclass(frozen=True)
class TransitStop:
    """A stop the user wants arrivals for (from config)."""

    id: str  # provider stop id (GTFS stop_id, OBA "1_75403", MBTA "place-pktrm", ...)
    label: str  # short display name, e.g. "Park St"
    routes: Tuple[str, ...] = ()  # optional route_id filter (empty = all routes)
    direction_id: Optional[int] = None  # optional GTFS direction filter (0/1)


@dataclass
class Arrival:
    stop_id: str
    route_id: str
    route_name: str  # short name preferred: "39", "Red"
    headsign: Optional[str]  # destination: "Forest Hills"
    time: datetime  # predicted time if realtime, else scheduled
    realtime: bool = True
    direction_id: Optional[int] = None
    status: Optional[str] = None  # provider status text such as "Boarding"


@dataclass
class StopArrivals:
    stop: TransitStop
    arrivals: List[Arrival] = field(default_factory=list)  # sorted by time
    error: Optional[str] = None  # short, user-safe message when this stop failed


# --------------------------------------------------------------------------- mesh


@dataclass
class IncomingMessage:
    text: str
    from_num: int
    from_id: str  # "!a1b2c3d4"
    to_num: int
    channel_index: int
    is_dm: bool
    packet_id: Optional[int] = None
    from_name: Optional[str] = None  # short/long name when the node DB knows it
    rx_time: Optional[datetime] = None
    via_mqtt: bool = False
    hops_away: Optional[int] = None


#: Outgoing message kinds, also used as send priority (lower = sooner).
KIND_PRIORITY = {"alert": 0, "reply": 1, "broadcast": 2}


@dataclass
class OutgoingMessage:
    """ONE mesh packet worth of text (already chunked to fit)."""

    text: str
    destination: Union[int, str] = BROADCAST  # node number or "^all"
    channel_index: int = 0
    reply_id: Optional[int] = None  # packet id this answers (threads the reply in apps)
    want_ack: bool = False
    kind: str = "reply"  # "reply" | "broadcast" | "alert"
    created_at: Optional[datetime] = None  # set by the sender queue when None
    #: Optional delivery callback ``on_done(msg, outcome)``, called once by the sender queue with
    #: outcome "sent" (handed to the radio), "dropped" (too old / too big / retries exhausted) or
    #: "discarded" (still queued at shutdown).  Runs on the sender thread: keep it quick.
    on_done: Optional[Callable[["OutgoingMessage", str], None]] = field(default=None, repr=False, compare=False)

#: Outcomes passed to :attr:`OutgoingMessage.on_done`.
OUTCOME_SENT = "sent"
OUTCOME_DROPPED = "dropped"
OUTCOME_DISCARDED = "discarded"


def node_id_from_num(num: int) -> str:
    """Meshtastic node number -> canonical node id string ("!a1b2c3d4")."""
    return "!%08x" % (num & 0xFFFFFFFF)


def node_num_from_id(node_id: str) -> int:
    """Canonical node id ("!a1b2c3d4" or "a1b2c3d4") -> node number."""
    return int(node_id.strip().lstrip("!"), 16)
