"""Compact, radio-friendly text: weather, alerts, transit and bot replies.

Everything sent to the mesh is built here:

* :func:`pack_segments` / :func:`split_text` cut text into packets of at most
  ``max_bytes`` UTF-8 bytes - part numbers (" 2/3") included - breaking at the
  separator, sentence ends, commas or spaces, and never inside a code point.
* :class:`Formatter` renders models as short lines such as
  ``"Boston, MA 2:15p: 54°F feels 50° Cloudy. Wind NW 12 G20mph"``.  It is the
  only place that converts the SI units stored in the models for display.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import DisplayConfig
from .models import Arrival, CurrentConditions, ForecastPeriod, StopArrivals, WeatherAlert, WeatherReport
from .timeutil import get_tz, utcnow

log = logging.getLogger(__name__)

#: Smallest usable packet budget: a single code point needs up to 4 UTF-8 bytes.
_MIN_LIMIT = 4
#: Share of a chunk that must be filled before the separator (or an extra break) may end it.
_SEP_FILL = 0.5
#: Break points tried after the separator when one segment must be split, with their minimum fill.
_SENTENCE_BREAKS = ((". ", 0.5), (", ", 2.0 / 3.0))
#: Separator between the route groups of one transit stop (also a preferred break).
_GROUP_SEP = " | "
#: Alert severities whose immediate broadcast carries the call to action.
_URGENT = frozenset({"severe", "extreme"})

_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
#: Words of NWS period names that are kept (not replaced by a weekday).
_RELATIVE_WORDS = frozenset({"today", "tonight", "overnight", "tomorrow", "this", "rest",
                             "late", "early", "afternoon", "evening", "morning"})
_NAME_WORDS = {"afternoon": "Aftn", "evening": "Eve", "morning": "Morn"}
#: Transit statuses that mean "at the stop" (shown as "now").
_AT_STOP = frozenset({"boarding", "arriving", "approaching"})

#: Shared, light-touch abbreviations for weather descriptions.
_ABBREVIATIONS = {
    "thunderstorms": "T-storms",
    "thunderstorm": "T-storm",
    "slight chance": "Sl Chc",
    "chance": "Chc",
    "showers": "Shwrs",
    "likely": "Lkly",
    "partly": "Ptly",
    "mostly": "Mstly",
    "light": "Lt",
    "heavy": "Hvy",
    "freezing": "Frzg",
    "and": "&",
}
_ABBREV_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(k) for k in sorted(_ABBREVIATIONS, key=len, reverse=True)),
    re.IGNORECASE,
)

#: Applied after NFKD by :func:`ascii_fold` (NFKD already turns "…" into "..." and "½" into "1⁄2").
_FOLD_TABLE = str.maketrans({
    "°": "",  # degree sign: "54°F" -> "54F"
    "⚠": "!",  # warning sign
    "‐": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "‹": "'", "›": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "«": '"', "»": '"',
    "•": "*", "·": ".", "×": "x", "⁄": "/",
    "ß": "ss", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "Ø": "O", "ø": "o", "Ł": "L", "ł": "l", "Đ": "D", "đ": "d",
    "Ð": "D", "ð": "d", "Þ": "Th", "þ": "th", "ı": "i",
})

_BULLET_RE = re.compile(r"(?m)^[ \t]*[*•-][ \t]+")
_SENTENCE_END_RE = re.compile(r"(?<!\.)[.!?](?=\s|$)")
#: NWS's generic headline ("Wind Advisory issued November 13 at ... by NWS Boston MA") says nothing new.
_GENERIC_HEADLINE_RE = re.compile(r"\bissued\b.*\bby\b", re.IGNORECASE)
#: Boilerplate that opens NWS warning descriptions.
_NWS_PREAMBLE_RE = re.compile(r"^The National Weather Service in .{1,80}? has issued an? ", re.IGNORECASE)

#: One transit countdown entry: (text, is_minutes, scheduled_only).
_Token = Tuple[str, bool, bool]


# =========================================================================== text + chunking


def utf8_len(s: str) -> int:
    """Size of ``s`` in UTF-8 bytes - what a Meshtastic packet budget counts."""
    return len(s.encode("utf-8", "surrogatepass"))


def ascii_fold(s: str) -> str:
    """Plain-ASCII version of ``s`` (``display.ascii_only``).

    "54°F" -> "54F", "⚠" -> "!", dashes -> "-", "…" -> "...", smart quotes ->
    ASCII quotes, accents removed ("Mayagüez" -> "Mayaguez"); anything still
    not ASCII afterwards (emoji, CJK, ...) is dropped.
    """
    folded = unicodedata.normalize("NFKD", s).translate(_FOLD_TABLE)
    return folded.encode("ascii", "ignore").decode("ascii")


def split_text(text: str, max_bytes: int) -> List[str]:
    """Cut ``text`` into stripped, non-empty chunks of at most ``max_bytes`` UTF-8 bytes.

    Breaks preferably at a newline or after ". " (if the chunk is at least
    half full), then after ", " (two thirds full), else at the last
    whitespace; a word longer than a chunk is cut on a code-point boundary.
    """
    _check_limit(max_bytes)
    return _split(_valid(text or ""), max_bytes, _breaks("\n"))


def pack_segments(segments: Sequence[str], max_bytes: int, *, sep: str = "\n",
                  number_parts: bool = True) -> List[str]:
    """Greedily join ``segments`` with ``sep`` into packets of at most ``max_bytes`` bytes.

    Empty segments are dropped and a segment too long for one packet is split
    like :func:`split_text` (preferring ``sep`` as the break).  With
    ``number_parts`` and more than one packet, " i/N" is appended to each one
    and the text is re-packed whenever that suffix would not fit, so the size
    limit always holds including the suffix.
    """
    return _pack(segments, max_bytes, sep, number_parts)


def _check_limit(max_bytes: int) -> None:
    if max_bytes < _MIN_LIMIT:
        raise ValueError("max_bytes must be at least %d (got %r)" % (_MIN_LIMIT, max_bytes))


def _valid(s: str) -> str:
    """Replace lone surrogates (possible in decoded JSON) so the text is encodable UTF-8."""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


def _fits(text: str, limit: int) -> bool:
    """``utf8_len(text) <= limit`` without encoding more than ``limit`` characters."""
    return len(text) <= limit and utf8_len(text) <= limit


def _clean_segments(segments: Iterable[str]) -> List[str]:
    if isinstance(segments, str):
        segments = [segments]
    return [s for s in (_valid(x).strip() for x in segments if x) if s]


def _breaks(sep: str, extra: Sequence[str] = ()) -> Tuple[Tuple[str, float], ...]:
    """Preferred ``(break, min_fill)`` pairs, best first: the separator, extras, sentence and comma ends.

    A separator made only of spaces is left out: breaking at the last space
    is the general fallback anyway.
    """
    out: Dict[str, float] = {}
    for b, fill in [(sep, _SEP_FILL)] + [(e, _SEP_FILL) for e in extra] + list(_SENTENCE_BREAKS):
        if b.strip(" ") and b not in out:
            out[b] = fill
    return tuple(out.items())


def _fit_chars(text: str, limit: int) -> int:
    """Number of leading characters of ``text`` that fit in ``limit`` UTF-8 bytes."""
    return len(text[:limit].encode("utf-8")[:limit].decode("utf-8", "ignore"))


def _attached(text: str, i: int) -> bool:
    """True when a cut before ``text[i]`` would tear a mark, emoji modifier or ZWJ sequence apart."""
    ch = text[i]
    return (unicodedata.category(ch).startswith("M") or ch == "\u200d" or text[i - 1] == "\u200d"
            or "\U0001F3FB" <= ch <= "\U0001F3FF")


def _soft_cut(text: str, limit: int, breaks: Sequence[Tuple[str, float]]) -> Optional[Tuple[str, str]]:
    """Split stripped ``text`` (longer than ``limit`` bytes) at a break into ``(head, rest)``.

    ``head`` is non-empty and fits in ``limit`` bytes.  The first preferred
    break (in ``breaks`` order) whose head reaches the break's minimum fill
    wins; "." / "," of a sentence break stay with the head, other breaks are
    dropped, and an NWS-style "... " is no sentence end.  Otherwise the last
    whitespace is used.  None when there is no whitespace to break at.
    """
    n = _fit_chars(text, limit)
    for b, fill in breaks:
        keep = 1 if b.strip() in (".", ",") else 0
        p = text.rfind(b, 1, n - keep + len(b))
        while p > 0 and b == ". " and text[p - 1] == ".":
            p = text.rfind(b, 1, p)
        if p > 0:
            head = text[:p + keep].rstrip()
            if utf8_len(head) >= fill * limit:
                return head, text[p + len(b):].lstrip()
    for i in range(min(n, len(text) - 1), 0, -1):
        if text[i].isspace():
            return text[:i].rstrip(), text[i:].lstrip()
    return None


def _cut(text: str, limit: int, breaks: Sequence[Tuple[str, float]]) -> Tuple[str, str]:
    """Like :func:`_soft_cut`, falling back to a hard cut on a code-point boundary."""
    cut = _soft_cut(text, limit, breaks)
    if cut is not None:
        return cut
    n = _fit_chars(text, limit)
    if n == 0:
        raise ValueError("cannot split text into %d-byte chunks" % limit)
    while n > 1 and _attached(text, n):
        n -= 1
    return text[:n], text[n:].lstrip()


def _split(text: str, limit: int, breaks: Sequence[Tuple[str, float]]) -> List[str]:
    chunks: List[str] = []
    rest = text.strip()
    while rest and not _fits(rest, limit):
        head, rest = _cut(rest, limit, breaks)
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


def _greedy(segs: Sequence[str], limit: int, sep: str, breaks: Sequence[Tuple[str, float]]) -> List[str]:
    """Pack stripped segments into chunks of at most ``limit`` bytes (no numbering).

    A segment too long for any chunk first fills the room left in the current
    chunk (at a word boundary) before the rest is split.
    """
    chunks: List[str] = []
    cur, cur_len = "", 0
    sep_len = utf8_len(sep)
    for seg in segs:
        seg_len = utf8_len(seg)
        if cur:
            if cur_len + sep_len + seg_len <= limit:
                cur += sep + seg
                cur_len += sep_len + seg_len
                continue
            room = limit - cur_len - sep_len
            if seg_len > limit and 4 * room >= limit:
                cut = _soft_cut(seg, room, breaks)
                if cut is not None:
                    cur += sep + cut[0]
                    seg = cut[1]
            chunks.append(cur)
            cur, cur_len = "", 0
        pieces = _split(seg, limit, breaks)
        if pieces:
            chunks.extend(pieces[:-1])
            cur, cur_len = pieces[-1], utf8_len(pieces[-1])
    if cur:
        chunks.append(cur)
    return chunks


def _suffix_len(parts: int) -> int:
    """Bytes of the longest part suffix " N/N" for ``parts`` packets."""
    return 2 + 2 * len(str(parts))


def _numbered(chunks: List[str]) -> List[str]:
    if len(chunks) < 2:
        return chunks
    n = len(chunks)
    return ["%s %d/%d" % (c, i, n) for i, c in enumerate(chunks, 1)]


def _pack(segments: Iterable[str], max_bytes: int, sep: str, number_parts: bool,
          extra: Sequence[str] = ()) -> List[str]:
    _check_limit(max_bytes)
    segs = _clean_segments(segments)
    breaks = _breaks(sep, extra)
    chunks = _greedy(segs, max_bytes, sep, breaks)
    if not number_parts or len(chunks) < 2:
        return chunks
    n = len(chunks)
    if all(utf8_len(c) + len(" %d/%d" % (i, n)) <= max_bytes for i, c in enumerate(chunks, 1)):
        return _numbered(chunks)
    parts = n
    while True:  # re-pack with room for " N/N"; repeat if N gains a digit
        limit = max_bytes - _suffix_len(parts)
        if limit < _MIN_LIMIT:
            log.debug("no room for part numbers in %d-byte packets", max_bytes)
            return chunks
        repacked = _greedy(segs, limit, sep, breaks)
        if len(str(len(repacked))) <= len(str(parts)):
            return _numbered(repacked)
        parts = len(repacked)


def _pack_text(text: str, max_bytes: int, max_parts: int, number_parts: bool) -> List[str]:
    """``text`` in at most ``max_parts`` packets; when it does not fit, the last one ends in "...".

    The packets before the last break like :func:`split_text`; the last one
    is filled up to a word boundary so as little as possible is lost.
    """
    chunks = _pack([text], max_bytes, "\n", number_parts)
    if len(chunks) <= max_parts:
        return chunks
    number = number_parts and max_parts > 1
    limit = max_bytes - (_suffix_len(max_parts) if number else 0)
    if limit < _MIN_LIMIT:
        number, limit = False, max_bytes
    breaks = _breaks("\n")
    out: List[str] = []
    rest = _valid(text).strip()
    while len(out) < max_parts - 1 and not _fits(rest, limit):
        head, rest = _cut(rest, limit, breaks)
        out.append(head)
    if rest:
        out.append(rest if _fits(rest, limit) else _ellipsize(rest, limit))
    return _numbered(out) if number else out


def _ellipsize(text: str, limit: int) -> str:
    """Shorten ``text`` at a word boundary so that ``text + "..."`` fits in ``limit`` bytes."""
    room = limit - 3
    if not _fits(text, room):
        text = _cut(text, room, ())[0] if room >= _MIN_LIMIT else ""
    return text.rstrip(" .,;:") + "..."


# =========================================================================== units


def compass(deg: Optional[float]) -> Optional[str]:
    """Meteorological degrees -> 16-point compass name ("NW"); None when unknown."""
    v = _finite(deg)
    if v is None:
        return None
    return _COMPASS[int((v % 360.0) / 22.5 + 0.5) % 16]


def c_to_f(c: float) -> float:
    """Degrees Celsius -> Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def kmh_to_mph(v: float) -> float:
    """km/h -> mph."""
    return v / 1.609344


def hpa_to_inhg(v: float) -> float:
    """Hectopascal -> inches of mercury."""
    return v / 33.8638866667


def mm_to_in(v: float) -> float:
    """Millimetres -> inches."""
    return v / 25.4


def km_to_mi(v: float) -> float:
    """Kilometres -> statute miles."""
    return v / 1.609344


def _finite(x: Any) -> Optional[float]:
    """``x`` as a float, or None when it is unknown, NaN or infinite."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _round(x: float) -> int:
    """Round half up to an int (an int never prints as "-0")."""
    return int(math.floor(x + 0.5))


def _dec(v: float, places: int) -> str:
    """``v`` with up to ``places`` decimals, trailing zeros dropped ("0.5", "2")."""
    s = "%.*f" % (places, v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _duration(seconds: float) -> str:
    """Compact duration: "25m", "2h13m", "3d4h"."""
    minutes = max(0, int(seconds) // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return "%dd%dh" % (days, hours) if hours else "%dd" % days
    if hours:
        return "%dh%dm" % (hours, minutes) if minutes else "%dh" % hours
    return "%dm" % minutes


def _plural(n: int, word: str) -> str:
    """ "1 alert" / "2 alerts"."""
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# =========================================================================== text helpers


def _collapse(s: Any) -> str:
    """Whitespace-collapsed, stripped text; "" for None."""
    return " ".join(str(s).split()) if s else ""


def _titled(head: str, parts: Sequence[str]) -> str:
    """ "head: part part" - just ``head`` when every part is empty."""
    body = " ".join(p for p in parts if p)
    return "%s: %s" % (head, body) if body else head


def _abbrev(text: str) -> str:
    """Apply the shared description abbreviations ("Mostly Cloudy" -> "Mstly Cloudy")."""
    def repl(m: "re.Match[str]") -> str:
        short = _ABBREVIATIONS[m.group(0).lower()]
        return short.lower() if m.group(0).islower() else short

    return _ABBREV_RE.sub(repl, text)


def _alert_text(text: Optional[str]) -> str:
    """NWS alert prose without "* " bullet markers, whitespace collapsed."""
    return _collapse(_BULLET_RE.sub("", text)) if text else ""


def _description(alert: WeatherAlert) -> str:
    """Cleaned alert description without the "The National Weather Service in X has issued a" opener."""
    return _NWS_PREAMBLE_RE.sub("", _alert_text(alert.description))


def _first_sentence(text: str) -> str:
    """First sentence of cleaned alert prose ("TAKE COVER NOW!"); NWS "..." runs do not end it."""
    m = _SENTENCE_END_RE.search(text)
    return text[:m.end()] if m else text


def _weekday_index(word: str) -> Optional[int]:
    """Index into :data:`_DAYS` for "saturday", "sat", "tues"...; None otherwise."""
    if len(word) >= 3:
        for i, name in enumerate(_WEEKDAY_NAMES):
            if name.startswith(word):
                return i
    return None


@lru_cache(maxsize=32)
def _zone(name: Optional[str]) -> Optional[tzinfo]:
    """Cached :func:`meshwx.timeutil.get_tz` (an unknown name is logged only once)."""
    return get_tz(name)


# =========================================================================== formatter


class Formatter:
    """Builds mesh-ready packets (lists of strings) from weather, alert and transit data.

    Every returned string is at most ``max_bytes`` UTF-8 bytes including the
    " 1/3" part numbering, and is ASCII-folded when ``display.ascii_only``.
    Times are shown in the report/location timezone, else ``default_tz``,
    else UTC.  ``command_prefix`` appears in hints such as "!alerts 1 = details".
    """

    def __init__(self, display: DisplayConfig, max_bytes: int, *,
                 default_tz: Optional[tzinfo] = None, command_prefix: str = "!") -> None:
        _check_limit(max_bytes)
        self.display = display
        self.max_bytes = int(max_bytes)
        self.default_tz = default_tz
        self.command_prefix = (command_prefix or "").strip()
        self._metric = (display.units or "").strip().lower() == "metric"
        self._h24 = (display.time_format or "").strip().lower() == "24h"
        self._speed_unit = "km/h" if self._metric else "mph"

    # ------------------------------------------------------------------ weather
    def current(self, report: WeatherReport) -> List[str]:
        """Current conditions (normally one packet) with alert count and cache age."""
        return self._out([self._current_line(report, utcnow(), fallback=True)])

    def forecast(self, report: WeatherReport, periods: Optional[int] = None) -> List[str]:
        """Header line plus one line per forecast period (all periods when ``periods`` is None).

        Periods that have already ended (possible in a cached report) are skipped.
        """
        now = utcnow()
        lines = self._forecast_lines(report, periods, now)
        if not lines:
            return self._out(["No forecast for %s%s" % (self._place(report), self._stale_note(report, now))])
        return self._out([self._forecast_header(report, now)] + lines)

    def alerts(self, report: WeatherReport) -> List[str]:
        """Numbered list of active alerts with a hint how to get the details."""
        now = utcnow()
        place = self._place(report)
        stale = self._stale_note(report, now)
        if not report.alerts:
            if report.alerts_error:
                return self._out([report.alerts_error])
            if not report.alerts_supported:
                return self._out(["Alerts: US/NWS only"])
            return self._out(["No alerts for %s%s" % (place, stale)])
        tz = self._report_tz(report)
        segs = ["%s: %s%s" % (place, _plural(len(report.alerts), "alert"), stale)]
        segs += ["%d) %s" % (i, self._alert_line(a, tz, now)) for i, a in enumerate(report.alerts, 1)]
        segs.append("%s 1 = details" % self._cmd("alerts"))
        return self._out(segs)

    def alert_detail(self, alert: WeatherAlert, index: int, tz_name: Optional[str],
                     max_parts: int = 3) -> List[str]:
        """One alert in full (description, else headline, then instruction), at most ``max_parts`` packets."""
        head = "%d) %s" % (index, _collapse(alert.event) or "Alert")
        severity = _collapse(alert.severity)
        if severity and severity.lower() != "unknown":
            head += " (%s)" % severity
        when = self._alert_when(alert, self._tz(tz_name), utcnow(), both=True)
        if when:
            head += " " + when
        body = [_description(alert) or _collapse(alert.headline), _alert_text(alert.instruction)]
        return self._limited(_titled(head, body), max_parts)

    def new_alert(self, alert: WeatherAlert, location_name: str, tz_name: Optional[str]) -> List[str]:
        """Immediate severe-weather broadcast, 1-2 packets.

        "⚠ Tornado Warning for Boston, MA til 3:45p: <headline> TAKE COVER NOW!":
        Severe/Extreme alerts add the first sentence of the instruction.  When
        headline and instruction do not both fit in two packets, a short call
        to action moves in front of the headline so it is never cut off.
        """
        head = "⚠ " + (_collapse(alert.event) or "Alert")
        place = _collapse(location_name)
        if place:
            head += " for " + place
        when = self._alert_when(alert, self._tz(tz_name), utcnow(), both=True)
        if when:
            head += " " + when
        if (alert.message_type or "").strip().lower() == "update":
            head = "UPDATE: " + head
        summary = self._alert_summary(alert)
        action = ""
        if (alert.severity or "").strip().lower() in _URGENT:
            action = _first_sentence(_alert_text(alert.instruction))
        body = [summary, action]
        if summary and action:
            out = self._out([_titled(head, body)])
            if len(out) <= 2:
                return out
            body = [action, summary] if 2 * utf8_len(action) <= self.max_bytes else [summary]
        return self._limited(_titled(head, body), 2)

    # ------------------------------------------------------------------ broadcast + transit
    def broadcast(self, report: Optional[WeatherReport], content: Sequence[str],
                  transit: Optional[Sequence[StopArrivals]] = None, now: Optional[datetime] = None,
                  max_arrivals: int = 3) -> List[str]:
        """Scheduled broadcast: the ``content`` items (current, forecast, alerts, transit) in order.

        Weather items are skipped when ``report`` is None, transit when
        ``transit`` is empty; nothing to say gives ``[]``.
        """
        now = now or utcnow()
        segs: List[str] = []
        done = set()
        named = False  # the current line already names the place
        scheduled = False
        for item in content:
            item = str(item).strip().lower()
            if item in done:
                continue
            done.add(item)
            if report is not None and item == "current":
                segs.append(self._current_line(report, now, fallback=False))
                named = True
            elif report is not None and item == "forecast":
                lines = self._forecast_lines(report, None, now)
                if lines and not named:
                    lines.insert(0, self._forecast_header(report, now))
                segs.extend(lines)
            elif report is not None and item == "alerts":
                tz = self._report_tz(report)
                segs.extend("⚠ " + self._alert_line(a, tz, now) for a in report.alerts)
            elif item == "transit" and transit:
                stop_segs, sched = self._transit_segments(transit, now, max_arrivals)
                segs.extend(stop_segs)
                scheduled = scheduled or sched
        if scheduled:
            segs.append("*=scheduled")
        return self._out(segs, extra=(_GROUP_SEP,))

    def transit(self, results: Sequence[StopArrivals], now: datetime, max_arrivals: int = 3) -> List[str]:
        """One line per stop: "Park St: Red>Alewife now,9,16m | Red>Ashmont 4m"."""
        segs, scheduled = self._transit_segments(results, now, max_arrivals)
        if scheduled:
            segs.append("*=scheduled")
        return self._out(segs, extra=(_GROUP_SEP,))

    # ------------------------------------------------------------------ bot replies
    def text(self, text: str) -> List[str]:
        """Generic text, chunked (line breaks kept, runs of spaces collapsed)."""
        return self._out((text or "").splitlines())

    def help(self, transit_enabled: bool, is_admin: bool) -> List[str]:
        """Command list (plus the admin commands for admins)."""
        cmds = ["wx [zip]", "fc [zip]", "alerts [n]"]
        if transit_enabled:
            cmds.append("bus [stop]")
        cmds += ["status", "ping"]
        line = "Cmds: %s." % ", ".join(self._cmd(c) for c in cmds)
        if self.command_prefix:
            line += " In DMs the %s is optional." % self.command_prefix
        segs = [line]
        if is_admin:
            segs.append("Admin: %s" % ", ".join(
                self._cmd(c) for c in ("setzip ZIP", "setinterval min|off", "broadcast")))
        return self._out(segs)

    def status(self, info: Mapping[str, Any]) -> List[str]:
        """Service status from ``App.status()`` (missing keys are skipped)."""
        now = utcnow()
        parts = []
        where = " ".join(x for x in ("ZIP %s" % info["zip_code"] if info.get("zip_code") else "",
                                     _collapse(info.get("location"))) if x)
        if where:
            parts.append(where + ".")
        if any(k in info for k in ("broadcast_enabled", "interval_minutes", "times")):
            parts.append(self._schedule(info, now))
        link = []
        if info.get("link"):
            link.append("Link %s" % info["link"])
        if info.get("queue") is not None:
            link.append("queue %s" % info["queue"])
        if info.get("uptime_s") is not None:
            link.append("up " + _duration(info["uptime_s"]))
        if link:
            parts.append(", ".join(link) + ".")
        alerts = info.get("alerts_active")
        if alerts:
            parts.append(_plural(alerts, "alert") + " active.")
        if info.get("version"):
            parts.append("v%s" % info["version"])
        return self._out(parts or ["No status"], sep=" ")

    def error(self, user_message: str) -> List[str]:
        """Exactly one packet (cut with "..." if too long)."""
        return self._limited(user_message, 1) or self._limited("Error", 1)

    # ------------------------------------------------------------------ output
    def _out(self, segments: Iterable[str], *, sep: str = "\n", extra: Sequence[str] = ()) -> List[str]:
        """Clean, ASCII-fold (if configured) and pack segments into packets."""
        segs = [self._prep(s) for s in segments]
        return _pack(segs, self.max_bytes, sep, self.display.number_parts, extra)

    def _limited(self, text: str, max_parts: int) -> List[str]:
        """Like :meth:`_out` for one text, cut to at most ``max_parts`` packets ending in "..."."""
        return _pack_text(self._prep(text), self.max_bytes, max(1, max_parts), self.display.number_parts)

    def _prep(self, s: str) -> str:
        s = _collapse(s)
        return _collapse(ascii_fold(s)) if self.display.ascii_only else s

    def _cmd(self, word: str) -> str:
        return self.command_prefix + word

    # ------------------------------------------------------------------ time
    def _report_tz(self, report: WeatherReport) -> tzinfo:
        return (_zone(report.timezone) or _zone(report.location.timezone)
                or self.default_tz or timezone.utc)

    def _tz(self, name: Optional[str]) -> tzinfo:
        return _zone(name) or self.default_tz or timezone.utc

    def _hm(self, hour: int, minute: int) -> str:
        """Time of day: "14:15" (24h) or "2:15p" / "6p" / "12a" (12h)."""
        if self._h24:
            return "%02d:%02d" % (hour, minute)
        h12, ampm = hour % 12 or 12, "a" if hour < 12 else "p"
        return "%d%s" % (h12, ampm) if minute == 0 else "%d:%02d%s" % (h12, minute, ampm)

    def _when(self, dt: datetime, tz: tzinfo, now: datetime) -> str:
        """Clock time, prefixed by the weekday ("Fri 6p") - or the date beyond a week - unless today."""
        local = dt.astimezone(tz)
        days = (local.date() - now.astimezone(tz).date()).days
        clock = self._hm(local.hour, local.minute)
        if days == 0:
            return clock
        if -6 <= days <= 6:
            return "%s %s" % (_DAYS[local.weekday()], clock)
        return "%d/%d %s" % (local.month, local.day, clock)

    # ------------------------------------------------------------------ units
    def _temp(self, c: Optional[float]) -> Optional[int]:
        v = _finite(c)
        if v is None:
            return None
        return _round(v if self._metric else c_to_f(v))

    def _speed(self, kmh: Optional[float]) -> Optional[int]:
        v = _finite(kmh)
        if v is None:
            return None
        return _round(v if self._metric else kmh_to_mph(v))

    def _pressure(self, hpa: float) -> str:
        return "%dhPa" % _round(hpa) if self._metric else "%.2finHg" % hpa_to_inhg(hpa)

    def _distance(self, km: float) -> str:
        v, unit = (km, "km") if self._metric else (km_to_mi(km), "mi")
        return (_dec(v, 2) if v < 1 else str(_round(v))) + unit

    def _amount(self, mm: float) -> str:
        if self._metric:
            return _dec(mm, 1 if mm < 10 else 0) + "mm"
        return _dec(mm_to_in(mm), 2) + "in"

    # ------------------------------------------------------------------ weather pieces
    def _place(self, report: WeatherReport) -> str:
        loc = report.location
        return _collapse(loc.name) or "%.3f,%.3f" % (loc.latitude, loc.longitude)

    def _stale_note(self, report: WeatherReport, now: datetime) -> str:
        """ " (cached 25m)" for a report served from cache after a failed refresh, else ""."""
        if not report.stale:
            return ""
        return " (cached %s)" % _duration((now - report.fetched_at).total_seconds())

    def _forecast_header(self, report: WeatherReport, now: datetime) -> str:
        return "%s fcst%s:" % (self._place(report), self._stale_note(report, now))

    def _current_line(self, report: WeatherReport, now: datetime, fallback: bool) -> str:
        """The current-conditions line; ``fallback`` adds the first forecast period when there is no obs.

        Conditions without an observation time are forecast values standing in
        for missing observations (NWS hourly forecast): "Place fcst 4:47a: ...".
        """
        tz = self._report_tz(report)
        cur = report.current
        sentences = self._conditions(cur) if cur is not None else []
        if cur is not None and sentences:
            if cur.observed_at is not None:
                head = "%s %s" % (self._place(report), self._when(cur.observed_at, tz, now))
            else:
                head = "%s fcst %s" % (self._place(report), self._when(report.fetched_at, tz, now))
            text = "%s: %s" % (head, ". ".join(sentences))
        else:
            text = "%s: no current obs" % self._place(report)
            upcoming = self._upcoming(report, now) if fallback else []
            first = self._period_line(upcoming[0], tz) if upcoming else ""
            if first:
                text += ". " + first
        if report.alerts:
            text += ". %s: %s" % (_plural(len(report.alerts), "alert"), self._cmd("alerts"))
        return text + self._stale_note(report, now)

    def _conditions(self, cur: CurrentConditions) -> List[str]:
        """Sentences of the current line: temperature + sky, wind, humidity, pressure, visibility."""
        first = []
        temp = self._temp(cur.temperature_c)
        if temp is not None:
            first.append("%d°%s" % (temp, "C" if self._metric else "F"))
            feels = self._temp(cur.feels_like_c)
            if feels is not None and abs(feels - temp) >= 3:
                first.append("feels %d°" % feels)
        desc = _abbrev(_collapse(cur.description))
        if desc:
            first.append(desc)
        out = [" ".join(first)] if first else []
        wind = self._current_wind(cur)
        if wind:
            out.append(wind)
        rh = _finite(cur.humidity_pct)
        if rh is not None:
            out.append("RH %d%%" % _round(rh))
        hpa = _finite(cur.pressure_hpa)
        if hpa is not None:
            out.append(self._pressure(hpa))
        vis = _finite(cur.visibility_km)
        if vis is not None and vis < 10:
            out.append("Vis " + self._distance(vis))
        return out

    def _current_wind(self, cur: CurrentConditions) -> str:
        """ "Wind NW 12 G20mph", "Calm", "Gusts 15mph" (no or unknown sustained wind), or "" when unknown."""
        speed, gust = self._speed(cur.wind_speed_kmh), self._speed(cur.wind_gust_kmh)
        if not speed:
            if gust:
                return "Gusts %d%s" % (gust, self._speed_unit)
            return "Calm" if speed == 0 else ""
        text = "Wind"
        direction = compass(cur.wind_dir_deg)
        if direction:
            text += " " + direction
        text += " %d" % speed
        if gust and gust > speed:
            text += " G%d" % gust
        return text + self._speed_unit

    @staticmethod
    def _upcoming(report: WeatherReport, now: datetime) -> List[ForecastPeriod]:
        """Forecast periods that have not ended by ``now``."""
        return [p for p in report.forecast if p.end is None or p.end > now]

    def _forecast_lines(self, report: WeatherReport, periods: Optional[int], now: datetime) -> List[str]:
        fc = self._upcoming(report, now)
        if periods is not None:
            fc = fc[:max(0, periods)]
        tz = self._report_tz(report)
        return [line for line in (self._period_line(p, tz) for p in fc) if line]

    def _period_line(self, p: ForecastPeriod, tz: tzinfo) -> str:
        """ "Tonight: Lo 41° Rain 60%" / "Fri: 58°/41° Sunny 20%"; "" if the period says nothing."""
        parts = []
        temps = self._period_temps(p)
        if temps:
            parts.append(temps)
        desc = _abbrev(_collapse(p.description))
        pop = _finite(p.precip_chance_pct)
        pop_text = "%d%%" % _round(pop) if pop is not None and _round(pop) >= 20 else ""
        if desc or pop_text:
            parts.append(desc or "Precip")
        if pop_text:
            parts.append(pop_text)
        mm = _finite(p.precip_mm)
        if mm is not None and mm >= 1:
            parts.append(self._amount(mm))
        wind = self._period_wind(p)
        if wind:
            parts.append(wind)
        if not parts:
            return ""
        name = self._period_name(p, tz)
        return "%s: %s" % (name, " ".join(parts)) if name else " ".join(parts)

    def _period_temps(self, p: ForecastPeriod) -> str:
        hi, lo = self._temp(p.temp_high_c), self._temp(p.temp_low_c)
        if p.is_daytime is None and hi is not None and lo is not None:
            return "%d°/%d°" % (hi, lo)
        order = ((lo, "Lo"), (hi, "Hi")) if p.is_daytime is False else ((hi, "Hi"), (lo, "Lo"))
        for value, label in order:
            if value is not None:
                return "%s %d°" % (label, value)
        return ""

    def _period_wind(self, p: ForecastPeriod) -> str:
        """ "W 25mph" when the max sustained wind is at least 20 mph (32 km/h), else ""."""
        speed = self._speed(p.wind_speed_kmh)
        if speed is None or speed < (32 if self._metric else 20):
            return ""
        text = "%s %d" % (_collapse(p.wind_dir) or "Wind", speed)
        gust = self._speed(p.wind_gust_kmh)
        if gust and gust > speed:
            text += " G%d" % gust
        return text + self._speed_unit

    def _period_name(self, p: ForecastPeriod, tz: tzinfo) -> str:
        """Short period name: "Sat Night", "Tonight", "This Aftn"; holidays -> weekday of the start."""
        words = _collapse(p.name).split()
        low = [w.lower() for w in words]
        night = "night" in low[1:]
        if words:
            day = _weekday_index(low[0])
            if day is not None:
                return _DAYS[day] + (" Night" if night else "")
            if any(w in _RELATIVE_WORDS for w in low):
                return " ".join(_NAME_WORDS.get(w, words[i]) for i, w in enumerate(low))
        if p.start is not None:
            local = p.start.astimezone(tz)
            return _DAYS[local.weekday()] + (" Night" if night or p.is_daytime is False else "")
        return " ".join(words)

    # ------------------------------------------------------------------ alert pieces
    def _alert_when(self, alert: WeatherAlert, tz: tzinfo, now: datetime, both: bool = False) -> str:
        """ "til Fri 6p", or "from Sat 3a" (+ " til Sat 9a" with ``both``) for a future onset."""
        if alert.onset is not None and alert.onset > now:
            text = "from " + self._when(alert.onset, tz, now)
            if both and alert.ends is not None and alert.ends > alert.onset:
                text += " til " + self._when(alert.ends, tz, now)
            return text
        return "til " + self._when(alert.ends, tz, now) if alert.ends is not None else ""

    def _alert_line(self, alert: WeatherAlert, tz: tzinfo, now: datetime) -> str:
        event = _collapse(alert.event) or "Alert"
        when = self._alert_when(alert, tz, now)
        return "%s %s" % (event, when) if when else event

    @staticmethod
    def _alert_summary(alert: WeatherAlert) -> str:
        """The headline - unless it is NWS's generic "... issued ... by NWS ..." one: then the
        first sentence of the description (without the "The National Weather Service in X has
        issued a" preamble)."""
        headline = _collapse(alert.headline)
        if headline and not _GENERIC_HEADLINE_RE.search(headline):
            return headline
        return _first_sentence(_description(alert)) or headline

    # ------------------------------------------------------------------ transit pieces
    def _transit_segments(self, results: Sequence[StopArrivals], now: datetime,
                          max_arrivals: int) -> Tuple[List[str], bool]:
        """One segment per stop, and whether any shown time is schedule-only."""
        tz = self.default_tz or now.tzinfo or timezone.utc
        segs = []
        scheduled = False
        for result in results:
            label = _collapse(result.stop.label) or result.stop.id
            if result.error:  # the provider's short, user-safe text ("Stop data loading, try later")
                segs.append("%s: %s" % (label, _collapse(result.error) or "unavailable"))
                continue
            groups = self._arrival_groups(result.arrivals, now, tz, max(1, max_arrivals))
            if not groups:
                segs.append("%s: no arrivals soon" % label)
                continue
            scheduled = scheduled or any(t[2] for _, tokens in groups for t in tokens)
            segs.append("%s: %s" % (label, _GROUP_SEP.join(
                "%s %s" % (name, _render_times(tokens)) for name, tokens in groups)))
        return segs, scheduled

    def _arrival_groups(self, arrivals: Sequence[Arrival], now: datetime, tz: tzinfo,
                        limit: int) -> List[Tuple[str, List[_Token]]]:
        """Countdowns grouped by (route, headsign), groups ordered by their first arrival."""
        groups: Dict[Tuple[str, str], List[_Token]] = {}
        for a in sorted(arrivals, key=lambda x: x.time):
            secs = (a.time - now).total_seconds()
            if secs < -60:  # already gone (cached data); providers use the same cut-off
                continue
            route = _collapse(a.route_name) or _collapse(a.route_id) or "?"
            headsign = _collapse(a.headsign)
            key = (route, headsign if headsign.lower() != route.lower() else "")
            tokens = groups.setdefault(key, [])
            if len(tokens) < limit:
                tokens.append(self._countdown(a, secs, now, tz))
        return [(route + (">" + head if head else ""), tokens) for (route, head), tokens in groups.items()]

    def _countdown(self, a: Arrival, secs: float, now: datetime, tz: tzinfo) -> _Token:
        """CONTRACTS countdown rule: "now" (<= 30 s or at the stop), minutes (half up), clock time > 90 min."""
        scheduled = not a.realtime
        if secs <= 30 or (a.status or "").strip().lower() in _AT_STOP:
            return "now", False, scheduled
        minutes = int((secs + 30) // 60)
        if minutes > 90:
            return self._when(a.time, tz, now), False, scheduled
        return str(minutes), True, scheduled

    # ------------------------------------------------------------------ status pieces
    def _schedule(self, info: Mapping[str, Any], now: datetime) -> str:
        """ "Bcast every 180m on ch0, next 3p." / "Bcast at 7a,6p ..." / "Sched bcast off." """
        if info.get("broadcast_enabled") is False:
            return "Bcast disabled."
        times = [self._hhmm(t) for t in info.get("times") or []]
        interval = info.get("interval_minutes")
        if times:
            text = "Bcast at " + ",".join(times)
        elif interval:
            text = "Bcast every %sm" % interval
        else:
            return "Sched bcast off."
        if info.get("channel_index") is not None:
            text += " on ch%s" % info["channel_index"]
        nxt = info.get("next_broadcast")
        if isinstance(nxt, datetime):
            text += ", next " + self._when(nxt, self.default_tz or nxt.tzinfo or timezone.utc, now)
        return text + "."

    def _hhmm(self, value: Any) -> str:
        """Config "HH:MM" -> display time ("07:00" -> "7a" in 12h mode)."""
        try:
            hour, minute = (int(x) for x in str(value).split(":"))
        except ValueError:
            return str(value)
        return self._hm(hour, minute)


def _render_times(tokens: Sequence[_Token]) -> str:
    """ "now,9,16m": "m" closes each run of minute values; "*" marks schedule-only times."""
    out = []
    for i, (text, is_minutes, scheduled) in enumerate(tokens):
        if is_minutes and (i + 1 == len(tokens) or not tokens[i + 1][1]):
            text += "m"
        out.append(text + ("*" if scheduled else ""))
    return ",".join(out)
