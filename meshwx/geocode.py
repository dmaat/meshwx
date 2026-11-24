"""ZIP code -> :class:`~meshwx.models.Location`, with a persistent cache.

Sources, in order:

1. zippopotam.us ``/us/{zip}``.  US territories mostly resolve only under
   their own country code, so the territory path chosen by ZIP prefix is tried
   next (``/pr/00901`` -> "San Juan, PR").  zippopotam's territory data has
   garbage entries (Guam 96932 listed at longitude -144.7 as well as 144.8;
   ``/us/96951``, Rota MP, answers with a point in Texas), so a territory
   place must lie in that territory's box (:data:`TERRITORY_BOXES`); an
   otherwise identical place with the sign of its longitude flipped counts.
   Territory locations carry their IANA zone (:data:`TERRITORY_ZONES`).
2. Open-Meteo geocoding.  It matches postal codes by *prefix* and returns the
   *place* whose ``postcodes`` list contains the code, so only a result listing
   the exact ZIP is accepted.  Its coordinates are the city centre.

Found ZIPs are cached forever (memory + JSON file, least recently used dropped
beyond :data:`MAX_FOUND`).  Unknown ZIPs are remembered for 24 h in memory
only, so mesh users typing random ZIPs never cause SD-card writes.  A cache
file from an older :data:`CACHE_VERSION` loses its territory entries (they
are looked up again); one of an unknown version is ignored.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .cache import atomic_write_json, read_json
from .config import normalize_zip
from .errors import ConfigError, GeocodeError
from .httpclient import HttpClient, HttpError
from .models import Location

log = logging.getLogger(__name__)

ZIPPOPOTAM_URL = "https://api.zippopotam.us/%s/%s"
OPENMETEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

NEGATIVE_TTL_SECONDS = 24 * 3600
MAX_FOUND = 500  # cached locations (memory + file); least recently used dropped first
MAX_MISSING = 200
CACHE_VERSION = 3  # file: {"version", "locations": [Location fields + "key"], least recently used first}
#: Older cache versions with the same layout; their territory entries may be wrong (v2: Guam/Rota
#: coordinates, no territory zone) and are dropped on load.
_OLD_CACHE_VERSIONS = (1, 2)

FAILED_MESSAGE = "ZIP lookup failed, try later"
NOT_FOUND_MESSAGE = "ZIP %s not found"

_TWO_LETTERS = re.compile(r"[A-Z]{2}")

#: Territory (zippopotam country code) -> (lat_min, lat_max, lon_min, lon_max) around its islands.
TERRITORY_BOXES: Dict[str, Tuple[float, float, float, float]] = {
    "pr": (17.5, 18.8, -68.1, -65.1),  # incl. Mona, Vieques, Culebra
    "vi": (17.5, 18.6, -65.2, -64.4),  # St Thomas, St John, St Croix
    "gu": (13.1, 13.8, 144.5, 145.1),
    "mp": (13.9, 20.7, 144.7, 146.2),  # Rota to Farallon de Pajaros
    "as": (-14.7, -10.9, -171.2, -168.0),  # incl. Swains Island and Rose Atoll
}

#: Territory -> IANA zone.  NWS /points gives the forecast office's zone at marine points
#: (St Thomas harbour: "America/New_York", although the USVI keep AST all year).
TERRITORY_ZONES: Dict[str, str] = {
    "pr": "America/Puerto_Rico", "vi": "America/St_Thomas", "gu": "Pacific/Guam",
    "mp": "Pacific/Saipan", "as": "Pacific/Pago_Pago",
}

#: Open-Meteo ``admin1`` (full state name) -> USPS abbreviation.
US_STATE_ABBR: Dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Washington, D.C.": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


class _SourceFailed(Exception):
    """A geocoding source could not answer (network, HTTP or format problem)."""


def territory_for_zip(zip5: str) -> Optional[str]:
    """zippopotam.us country code for US-territory ZIPs (they 404 under ``/us/``), else None."""
    prefix = zip5[:3]
    if prefix in ("006", "007", "009"):
        return "pr"
    if prefix == "008":
        return "vi"
    if "96910" <= zip5 <= "96939":
        return "gu"
    if zip5 == "96799":
        return "as"
    if zip5.startswith("9695"):
        return "mp"
    return None


class ZipGeocoder:
    """Thread-safe postal-code lookup with an in-memory + JSON-file cache.

    ``clock`` (monotonic seconds) only times the 24 h negative cache; tests may
    inject a fake one.
    """

    def __init__(self, http: HttpClient, cache_file: Optional[Path] = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._http = http
        self._file = Path(cache_file) if cache_file is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._found: Dict[str, Location] = self._load()  # insertion order = least recently used first
        self._missing: Dict[str, float] = {}  # key -> clock() when every source said "not found"

    def lookup(self, zip_code: str, country: str = "us") -> Location:
        """Return the location of ``zip_code`` or raise :class:`GeocodeError`.

        ``user_message`` is "ZIP 12345 not found" when every source answered and
        none knows the code, or "ZIP lookup failed, try later" when a source
        could not be reached (such failures are not cached).
        """
        country = (country or "us").strip().lower()
        try:
            code = normalize_zip(zip_code, country)
        except ConfigError as exc:
            raise GeocodeError(str(exc), user_message=exc.user_message) from exc
        key = "%s:%s" % (country, code)
        with self._lock:
            hit = self._found.pop(key, None)
            if hit is not None:
                self._found[key] = hit  # mark as most recently used
                return hit
            missing_since = self._missing.get(key)
            if missing_since is not None and self._clock() - missing_since < NEGATIVE_TTL_SECONDS:
                raise GeocodeError("ZIP %s not found (cached)" % key, user_message=NOT_FOUND_MESSAGE % code)

        loc = self._resolve(code, country)  # network I/O happens outside the lock
        with self._lock:
            if loc is None:
                self._remember_missing(key)
            else:
                self._remember_found(key, loc)
        if loc is None:
            raise GeocodeError("ZIP %s not found by any source" % key, user_message=NOT_FOUND_MESSAGE % code)
        return loc

    # ------------------------------------------------------------------ sources
    def _resolve(self, code: str, country: str) -> Optional[Location]:
        """Ask each source in turn; None when all of them answered "not found"."""
        territory = territory_for_zip(code) if country == "us" else None
        failures: List[str] = []
        for source in (self._from_zippopotam, self._from_openmeteo):
            try:
                loc = source(code, country, territory)
            except _SourceFailed as exc:
                log.warning("geocoding %s: %s", code, exc)
                failures.append(str(exc))
                continue
            if loc is not None:
                return loc
        if failures:
            raise GeocodeError("ZIP %s lookup failed: %s" % (code, "; ".join(failures)),
                               user_message=FAILED_MESSAGE)
        return None

    def _from_zippopotam(self, code: str, country: str, territory: Optional[str]) -> Optional[Location]:
        failure: Optional[Exception] = None
        for path_cc in [country] + ([territory] if territory else []):
            url = ZIPPOPOTAM_URL % (path_cc, quote(code, safe=""))
            try:
                data = self._http.get_json(url)
                loc = _parse_zippopotam(data, code, country, territory)
            except HttpError as exc:
                if exc.status != 404:  # 404 = unknown here; anything else = could not ask
                    failure = exc
                continue
            except (KeyError, TypeError, ValueError) as exc:
                failure = exc
                continue
            if loc is not None:
                return loc
        if failure is not None:
            raise _SourceFailed("zippopotam.us: %s" % failure)
        return None

    def _from_openmeteo(self, code: str, country: str, territory: Optional[str]) -> Optional[Location]:
        cc = (territory or country).upper()
        params = {"name": code, "count": 10, "language": "en", "format": "json", "countryCode": cc}
        try:
            data = self._http.get_json(OPENMETEO_GEOCODE_URL, params=params)
        except (HttpError, ValueError) as exc:
            raise _SourceFailed("Open-Meteo geocoding: %s" % exc) from exc
        if not isinstance(data, dict):
            raise _SourceFailed("Open-Meteo geocoding: unexpected response")
        for result in data.get("results") or []:  # no "results" key at all = no match
            if not isinstance(result, dict) or str(result.get("country_code") or "").upper() != cc:
                continue
            if code.upper() not in {str(p).strip().upper() for p in result.get("postcodes") or []}:
                continue  # prefix match on another code (e.g. "2108" -> 21087)
            try:
                lat, lon = float(result["latitude"]), float(result["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if territory:
                state = cc
            elif country == "us":
                state = US_STATE_ABBR.get(str(result.get("admin1") or "").strip(), "")
            else:
                state = ""
            name = _join_name(str(result.get("name") or code).strip(), state)
            log.info("ZIP %s: using Open-Meteo city-centre coordinates of %s", code, name)
            zone = TERRITORY_ZONES.get(territory or "") or result.get("timezone") or None
            return Location(latitude=lat, longitude=lon, name=name, zip_code=code,
                            country=country.upper(), timezone=zone)
        return None

    # ------------------------------------------------------------------ cache (lock held)
    def _remember_found(self, key: str, loc: Location) -> None:
        self._found[key] = loc
        self._missing.pop(key, None)
        while len(self._found) > MAX_FOUND:
            del self._found[next(iter(self._found))]
        self._save()

    def _remember_missing(self, key: str) -> None:
        self._missing[key] = self._clock()
        if len(self._missing) > MAX_MISSING:
            del self._missing[min(self._missing, key=self._missing.__getitem__)]

    def _load(self) -> Dict[str, Location]:
        """Read the cache file; a list keeps the least-recently-used order across restarts."""
        if self._file is None:
            return {}
        doc = read_json(self._file, default=None)
        entries = doc.get("locations") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            if doc is not None:
                log.warning("ignoring malformed geocode cache %s", self._file)
            return {}
        version = doc.get("version")
        if version != CACHE_VERSION and version not in _OLD_CACHE_VERSIONS:
            log.info("ignoring geocode cache %s of version %r", self._file, version)
            return {}
        found: Dict[str, Location] = {}
        for item in entries:
            try:
                key = str(item["key"])
                if version != CACHE_VERSION and _is_territory_key(key):
                    log.info("dropping geocode cache entry %s of version %r: looked up again", key, version)
                    continue
                found[key] = _location_from_json(item)
            except (KeyError, TypeError, ValueError):
                log.debug("dropping bad geocode cache entry %r", item)
        return found

    def _save(self) -> None:
        if self._file is None:
            return
        entries = [dict(asdict(loc), key=key) for key, loc in self._found.items()]
        doc = {"version": CACHE_VERSION, "locations": entries}
        try:
            atomic_write_json(self._file, doc)
        except OSError as exc:
            log.warning("cannot write geocode cache %s: %s", self._file, exc)


# ---------------------------------------------------------------------- helpers


def _join_name(city: str, state: str) -> str:
    return "%s, %s" % (city, state) if state else city


def _is_territory_key(key: str) -> bool:
    """Cache key ("us:96932") of a US-territory ZIP."""
    country, _, code = key.partition(":")
    return country == "us" and territory_for_zip(code) is not None


def _in_box(lat: float, lon: float, box: Tuple[float, float, float, float]) -> bool:
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def _parse_zippopotam(data: Any, code: str, country: str, territory: Optional[str]) -> Optional[Location]:
    """zippopotam.us body -> Location; None when it lists no (plausible) place.

    Entries can be duplicated with different quality (state abbreviation "72"
    vs "PR"), so a place with a two-letter abbreviation wins.  For a US
    territory (``territory``, from the ZIP prefix) only places inside its box
    count - failing that, a place whose longitude has the wrong sign - and the
    location gets the territory's name suffix and time zone.
    """
    if not isinstance(data, dict):
        raise ValueError("unexpected response")
    places = [{str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in p.items()}
              for p in data.get("places") or [] if isinstance(p, dict)]
    candidates: List[Tuple[Dict[str, Any], float, float]] = []  # (place, lat, lon)
    for p in places:
        try:
            lat, lon = float(p["latitude"]), float(p["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            candidates.append((p, lat, lon))
    if places and not candidates:
        raise ValueError("no place with usable coordinates")
    if territory:
        box = TERRITORY_BOXES[territory]
        plausible = [c for c in candidates if _in_box(c[1], c[2], box)]
        if not plausible:  # e.g. Guam at -144.7 instead of 144.7
            plausible = [(p, lat, -lon) for p, lat, lon in candidates if _in_box(lat, -lon, box)]
        if not plausible and candidates:
            log.info("zippopotam.us places for %s are outside %s: %s", code, territory.upper(),
                     ["%s,%s" % (lat, lon) for _, lat, lon in candidates])
        candidates = plausible
    if not candidates:
        return None
    place, lat, lon = next((c for c in candidates
                            if _TWO_LETTERS.fullmatch(str(c[0].get("state abbreviation") or ""))), candidates[0])
    if territory:
        state = territory.upper()
    else:
        abbr = str(place.get("state abbreviation") or "")
        state = abbr if _TWO_LETTERS.fullmatch(abbr) else ""
    city = str(place.get("place name") or code)
    return Location(latitude=lat, longitude=lon, name=_join_name(city, state), zip_code=code,
                    country=country.upper(), timezone=TERRITORY_ZONES.get(territory or ""))


def _location_from_json(item: Any) -> Location:
    name = str(item["name"])
    if not name:
        raise ValueError("empty name")
    return Location(latitude=float(item["latitude"]), longitude=float(item["longitude"]), name=name,
                    zip_code=item.get("zip_code") or None, country=str(item.get("country") or "US"),
                    timezone=item.get("timezone") or None)
