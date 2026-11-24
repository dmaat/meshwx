"""Tests for meshwx.formatting: chunking, units, and every Formatter output."""

from __future__ import annotations

import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from meshwx import formatting
from meshwx.config import DisplayConfig
from meshwx.formatting import (
    Formatter,
    ascii_fold,
    c_to_f,
    compass,
    hpa_to_inhg,
    km_to_mi,
    kmh_to_mph,
    mm_to_in,
    pack_segments,
    split_text,
    utf8_len,
)
from meshwx.models import (
    Arrival,
    CurrentConditions,
    ForecastPeriod,
    Location,
    StopArrivals,
    TransitStop,
    WeatherAlert,
    WeatherReport,
)
from meshwx.timeutil import from_epoch, get_tz, parse_iso

from conftest import load_fixture

NOW = datetime(2026, 11, 13, 19, 15, tzinfo=timezone.utc)  # Friday 2:15p in Boston
NY = get_tz("America/New_York")
BOSTON = Location(42.3576, -71.0684, "Boston, MA", "02108", timezone="America/New_York")


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(formatting, "utcnow", lambda: NOW)


# ============================================================================ helpers


def f2c(f: float) -> float:
    return (f - 32) * 5 / 9


def mph(v: float) -> float:
    return v * 1.609344


def ny(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=NY)


def fmt(max_bytes: int = 200, prefix: str = "!", default_tz=NY, **display) -> Formatter:
    return Formatter(DisplayConfig(**display), max_bytes, default_tz=default_tz, command_prefix=prefix)


def check(out: List[str], max_bytes: int = 200, ascii_only: bool = False) -> List[str]:
    """Byte invariant (part numbers included), stripped, non-empty, ASCII when asked."""
    for s in out:
        assert s and s == s.strip(), repr(s)
        assert utf8_len(s) <= max_bytes, (utf8_len(s), s)
        if ascii_only:
            assert s.isascii(), repr(s)
    return out


def nonws(text: str) -> str:
    return "".join(c for c in text if not c.isspace())


def strip_parts(chunks: List[str]) -> List[str]:
    """Remove and verify " i/N" suffixes."""
    if len(chunks) < 2:
        return chunks
    out = []
    for i, c in enumerate(chunks, 1):
        suffix = " %d/%d" % (i, len(chunks))
        assert c.endswith(suffix), (c, suffix)
        out.append(c[: -len(suffix)])
    return out


def conditions(**kw) -> CurrentConditions:
    base = dict(description="Cloudy", temperature_c=f2c(54), feels_like_c=f2c(50), humidity_pct=65,
                wind_speed_kmh=mph(12), wind_gust_kmh=mph(20), wind_dir_deg=315,
                pressure_hpa=30.12 * 33.8638866667, observed_at=NOW)
    base.update(kw)
    return CurrentConditions(**base)


def report(**kw) -> WeatherReport:
    base = dict(location=BOSTON, provider="nws", fetched_at=NOW, timezone="America/New_York",
                current=conditions(), alerts_supported=True)
    base.update(kw)
    return WeatherReport(**base)


def nws_periods(fixture: str) -> List[ForecastPeriod]:
    """ForecastPeriods from a real NWS /forecast capture (minimal local adapter)."""
    out = []
    for p in load_fixture(fixture)["properties"]["periods"]:
        temp_c = f2c(p["temperature"])
        speeds = [int(x) for x in re.findall(r"\d+", p.get("windSpeed") or "")]
        out.append(ForecastPeriod(
            name=p["name"], start=parse_iso(p["startTime"]), end=parse_iso(p["endTime"]),
            is_daytime=p["isDaytime"],
            temp_high_c=temp_c if p["isDaytime"] else None,
            temp_low_c=None if p["isDaytime"] else temp_c,
            description=p["shortForecast"],
            precip_chance_pct=(p.get("probabilityOfPrecipitation") or {}).get("value"),
            wind_speed_kmh=mph(max(speeds)) if speeds else None,
            wind_dir=p.get("windDirection") or None,
        ))
    return out


def sample_alerts() -> dict:
    """WeatherAlerts from the real nws_alerts_sample.json capture, keyed by event."""
    out = {}
    for feature in load_fixture("nws_alerts_sample.json")["features"]:
        p = feature["properties"]
        nws_headline = (p["parameters"].get("NWSheadline") or [None])[0]
        out[p["event"]] = WeatherAlert(
            id=p["id"], event=p["event"], headline=nws_headline or p["headline"], severity=p["severity"],
            message_type=p["messageType"], onset=parse_iso(p["onset"]),
            ends=parse_iso(p["ends"] or p["expires"]), description=p["description"],
            instruction=p["instruction"])
    return out


def wind_advisory(**kw) -> WeatherAlert:
    base = dict(id="wa", event="Wind Advisory", headline="WIND ADVISORY IN EFFECT UNTIL 6 PM EST FRIDAY",
                severity="Moderate", message_type="Alert", onset=ny(13, 6), ends=ny(13, 18))
    base.update(kw)
    return WeatherAlert(**base)


def tornado(**kw) -> WeatherAlert:
    """A Tornado Warning issued at 2:10p (NOW is 2:15p), shaped like a real NWS warning."""
    base = dict(
        id="tor", event="Tornado Warning", severity="Extreme", message_type="Alert",
        headline=("Tornado Warning issued November 13 at 2:10PM EST until November 13 at 3:45PM EST "
                  "by NWS Boston/Norton MA"),
        onset=ny(13, 14, 10), ends=ny(13, 15, 45),
        description=("The National Weather Service in Norton has issued a\n\n* Tornado Warning for...\n"
                     "  Central Suffolk County in eastern Massachusetts...\n\n* Until 345 PM EST.\n\n"
                     "* At 210 PM EST, a severe thunderstorm capable of producing a tornado was located "
                     "over Brookline, moving northeast at 30 mph."),
        instruction=("TAKE COVER NOW! Move to a basement or an interior room on the lowest floor of a "
                     "sturdy building. Avoid windows."))
    base.update(kw)
    return WeatherAlert(**base)


def arrival(seconds: float, route: str = "Red", headsign: Optional[str] = "Alewife", **kw) -> Arrival:
    return Arrival(stop_id="s", route_id=route, route_name=route, headsign=headsign,
                   time=NOW + timedelta(seconds=seconds), **kw)


PARK = TransitStop("place-pktrm", "Park St")


# ============================================================================ module helpers


def test_utf8_len():
    assert utf8_len("") == 0
    assert utf8_len("a°⚠\U0001F600") == 1 + 2 + 3 + 4


@pytest.mark.parametrize("text, expected", [
    ("54°F feels 50°", "54F feels 50"),
    ("⚠ Tornado Warning", "! Tornado Warning"),
    ("⚠\ufe0f Alert", "! Alert"),
    ("a–b—c", "a-b-c"),
    ("wait…", "wait..."),
    ("“quoted” ‘single’", "\"quoted\" 'single'"),
    ("Mayagüez, PR", "Mayaguez, PR"),
    ("Straße", "Strasse"),
    ("½ in", "1/2 in"),
    ("rain \U0001F327\ufe0f 日本", "rain  "),
])
def test_ascii_fold(text, expected):
    assert ascii_fold(text) == expected


@pytest.mark.parametrize("deg, expected", [
    (0, "N"), (360, "N"), (11.2, "N"), (11.25, "NNE"), (22.5, "NNE"), (315, "NW"), (-45, "NW"),
    (180, "S"), (348.75, "N"), (None, None), (float("nan"), None),
])
def test_compass(deg, expected):
    assert compass(deg) == expected


def test_unit_conversions():
    assert c_to_f(0) == 32 and c_to_f(100) == 212 and c_to_f(-40) == -40
    assert kmh_to_mph(1.609344) == pytest.approx(1)
    assert km_to_mi(16.09344) == pytest.approx(10)
    assert mm_to_in(25.4) == pytest.approx(1)
    assert hpa_to_inhg(1013.25) == pytest.approx(29.92, abs=0.005)


# ============================================================================ split_text / pack_segments


def test_split_text_prefers_line_then_sentence_then_comma():
    assert split_text("the first line is long enough\nsecond line words words", 40) == [
        "the first line is long enough", "second line words words"]
    assert split_text("Sentence one is here. Sentence two goes on and on", 30) == [
        "Sentence one is here.", "Sentence two goes on and on"]
    assert split_text("alpha beta gamma, delta epsilon zeta", 25) == ["alpha beta gamma,", "delta epsilon zeta"]


def test_split_text_does_not_waste_packets_on_early_breaks():
    chunks = split_text("Hi. aaaa bbbb cccc dddd eeee ffff gggg", 20)
    assert chunks[0] != "Hi." and utf8_len(chunks[0]) > 10
    # a comma break must fill two thirds of the chunk ("Boston, MA" stays together here)
    assert split_text("Boston, MA til 3:45p: TAKE COVER NOW", 30) == ["Boston, MA til 3:45p: TAKE", "COVER NOW"]
    # NWS "..." runs are not sentence ends
    assert split_text("Tornado Warning for... Central Suffolk", 30) == ["Tornado Warning for... Central", "Suffolk"]
    assert split_text("Tornado Warning. For Central Suffolk", 30) == ["Tornado Warning.", "For Central Suffolk"]


def test_split_text_hard_cuts_on_code_point_boundaries():
    assert split_text("\U0001F600" * 30, 10) == ["\U0001F600" * 2] * 15
    chunks = split_text("x" * 25, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]
    decomposed = split_text("e\u0301" * 10, 5)  # never separate an accent from its letter
    assert all(c.startswith("e") for c in decomposed) and "".join(decomposed) == "e\u0301" * 10
    thumbs = "\U0001F44D\U0001F3FD"  # emoji + skin-tone modifier stay together
    assert split_text(thumbs * 3, 12) == [thumbs] * 3
    coder = "\U0001F469\u200d\U0001F4BB"  # ZWJ sequence stays together
    assert split_text(coder * 2, 15) == [coder] * 2


def test_split_text_edges():
    assert split_text("", 60) == [] and split_text("  \n ", 60) == []
    assert split_text("  short  ", 60) == ["short"]
    with pytest.raises(ValueError):
        split_text("abc", 3)
    with pytest.raises(ValueError):
        Formatter(DisplayConfig(), 3)


def test_pack_segments_greedy_and_numbering():
    assert pack_segments(["a", "", "  ", "b"], 60) == ["a\nb"]
    assert pack_segments([], 60) == [] and pack_segments(["", " "], 60) == []
    assert pack_segments(["a", "b", "c"], 60, sep=" | ") == ["a | b | c"]
    segs = ["a" * 50, "b" * 50, "c" * 50]
    assert pack_segments(segs, 60) == ["a" * 50 + " 1/3", "b" * 50 + " 2/3", "c" * 50 + " 3/3"]
    assert pack_segments(segs, 60, number_parts=False) == segs


def test_pack_segments_repacks_when_suffix_does_not_fit():
    segs = ["a" * 58, "b" * 58]
    chunks = check(pack_segments(segs, 60), 60)
    assert nonws("".join(strip_parts(chunks))) == "a" * 58 + "b" * 58


def test_pack_segments_two_digit_part_numbers():
    chunks = check(pack_segments(["word " * 300], 60), 60)
    assert len(chunks) >= 10
    assert chunks[-1].endswith(" %d/%d" % (len(chunks), len(chunks)))
    assert nonws("".join(strip_parts(chunks))) == "word" * 300


def test_pack_segments_repacks_again_when_part_count_gains_a_digit():
    segs = ["%d" % i + "a" * 57 for i in range(9)]  # 9 packets without numbers; with " i/9" they overflow
    assert len(pack_segments(segs, 60, number_parts=False)) == 9
    chunks = check(pack_segments(segs, 60), 60)
    assert len(chunks) >= 10 and chunks[0].endswith(" 1/%d" % len(chunks))
    assert nonws("".join(strip_parts(chunks))) == nonws("".join(segs))


def test_pack_segments_long_segment_fills_room_left_in_packet():
    chunks = pack_segments(["intro line", "word " * 30], 60, number_parts=False)
    assert chunks[0].startswith("intro line\nword word")
    assert len(chunks) == 3


def test_pack_segments_survives_lone_surrogates():
    assert pack_segments(["a\ud800b"], 60) == ["a?b"]


def _random_text(rng: random.Random) -> str:
    words = []
    for _ in range(rng.randint(0, 40)):
        r = rng.random()
        if r < 0.05:  # huge words
            words.append(rng.choice(["x" * rng.randint(50, 400), "\u00e9" * rng.randint(30, 150),
                                     "\U0001F600" * rng.randint(20, 80), "e\u0301" * rng.randint(20, 90)]))
        elif r < 0.35:
            words.append(rng.choice(["Mayagüez", "Ünïcödé", "\U0001F600",
                                     "\U0001F44D\U0001F3FD", "日本語", "naïve", "54°F",
                                     "⚠", "end.", "a,b", "..."]))
        else:
            words.append("".join(rng.choice(string.ascii_letters + string.digits)
                                 for _ in range(rng.randint(1, 12))))
    return "".join(w + rng.choice([" ", " ", " ", ". ", ", ", "\n", "  ", "\t", " | "]) for w in words)


def test_chunking_property_random_inputs():
    rng = random.Random(20261113)
    sizes = [60, 60, 61, 64, 75, 100, 137, 200, 228]
    for case in range(400):
        max_bytes = sizes[case % len(sizes)]
        segments = [_random_text(rng) for _ in range(rng.randint(0, 6))] + [""] * rng.randint(0, 2)
        rng.shuffle(segments)
        sep = rng.choice(["\n", " "])
        number_parts = rng.random() < 0.7
        chunks = pack_segments(segments, max_bytes, sep=sep, number_parts=number_parts)
        check(chunks, max_bytes)
        bodies = strip_parts(chunks) if number_parts else chunks
        assert all(bodies), chunks
        assert nonws("".join(bodies)) == nonws("".join(segments)), case

        text = _random_text(rng)
        limit = rng.choice([4, 5, 7, 10, 60, 200])
        pieces = split_text(text, limit)
        check(pieces, limit)
        assert nonws("".join(pieces)) == nonws(text), case


# ============================================================================ current


def test_current_style_target():
    assert check(fmt().current(report())) == [
        "Boston, MA 2:15p: 54°F feels 50° Cloudy. Wind NW 12 G20mph. RH 65%. 30.12inHg"]


def test_current_alerts_and_stale():
    # WeatherService notes ("via ...", "cached 25m") are not repeated: the age comes from fetched_at.
    rep = report(alerts=[wind_advisory(), tornado()], stale=True, fetched_at=NOW - timedelta(minutes=25),
                 notes=["via Open-Meteo", "cached 25m"])
    assert check(fmt().current(rep)) == [
        "Boston, MA 2:15p: 54°F feels 50° Cloudy. Wind NW 12 G20mph. RH 65%. 30.12inHg. "
        "2 alerts: !alerts (cached 25m)"]
    (line,) = fmt(prefix="").current(report(alerts=[wind_advisory()]))
    assert line.endswith("30.12inHg. 1 alert: alerts")
    rep = report(stale=True, fetched_at=NOW - timedelta(minutes=130))
    assert fmt().current(rep)[0].endswith("30.12inHg (cached 2h10m)")


def test_current_feels_threshold_calm_and_missing_parts():
    rep = report(current=conditions(feels_like_c=f2c(52), wind_speed_kmh=0.5, wind_gust_kmh=None,
                                    humidity_pct=None, pressure_hpa=None, description=None))
    assert fmt().current(rep) == ["Boston, MA 2:15p: 54°F. Calm"]
    rep = report(current=conditions(wind_dir_deg=None, wind_gust_kmh=mph(12), temperature_c=None))
    assert fmt().current(rep) == ["Boston, MA 2:15p: Cloudy. Wind 12mph. RH 65%. 30.12inHg"]
    rep = report(current=conditions(wind_speed_kmh=0, wind_gust_kmh=mph(15), humidity_pct=None, pressure_hpa=None))
    assert fmt().current(rep) == ["Boston, MA 2:15p: 54°F feels 50° Cloudy. Gusts 15mph"]
    rep = report(current=conditions(wind_speed_kmh=None, wind_gust_kmh=None, humidity_pct=None, pressure_hpa=None))
    assert fmt().current(rep) == ["Boston, MA 2:15p: 54°F feels 50° Cloudy"]


def test_current_without_observation_falls_back_to_forecast():
    rep = report(current=CurrentConditions(station="KBOS"),
                 forecast=[ForecastPeriod("This Afternoon", ny(13, 14), ny(13, 18), True, f2c(45), None,
                                          "Chance Light Rain", 49)])
    assert fmt().current(rep) == ["Boston, MA: no current obs. This Aftn: Hi 45° Chc Lt Rain 49%"]
    assert fmt().current(report(current=None)) == ["Boston, MA: no current obs"]


def test_current_without_observation_time_is_labelled_forecast():
    # the NWS hourly forecast standing in for missing observations has no observation time
    rep = report(current=conditions(observed_at=None, station="NWS forecast", feels_like_c=None,
                                    wind_gust_kmh=None, pressure_hpa=None))
    assert fmt().current(rep) == ["Boston, MA fcst 2:15p: 54°F Cloudy. Wind NW 12mph. RH 65%"]
    assert fmt().broadcast(rep, ["current"], now=NOW)[0].startswith("Boston, MA fcst 2:15p: 54°F")


def test_current_metric_24h_ascii():
    (line,) = check(fmt(units="metric", time_format="24h", ascii_only=True).current(report()), ascii_only=True)
    assert line == "Boston, MA 14:15: 12C Cloudy. Wind NW 19 G32km/h. RH 65%. 1020hPa"


def test_current_visibility_rounding_and_negative_zero():
    rep = report(current=conditions(temperature_c=-0.3, feels_like_c=None, visibility_km=0.8,
                                    wind_speed_kmh=None, wind_gust_kmh=None))
    assert fmt(units="metric").current(rep) == ["Boston, MA 2:15p: 0°C Cloudy. RH 65%. 1020hPa. Vis 0.8km"]
    rep = report(current=conditions(temperature_c=-17.9, feels_like_c=None, visibility_km=3.2))
    assert fmt().current(rep)[0].startswith("Boston, MA 2:15p: 0°F Cloudy.")
    assert fmt().current(rep)[0].endswith("Vis 2mi")


def test_current_old_observation_gets_day_prefix_and_location_tz_wins():
    rep = report(current=conditions(observed_at=ny(12, 23)))
    assert fmt().current(rep)[0].startswith("Boston, MA Thu 11p:")
    la = Location(34.05, -118.24, "Los Angeles, CA", timezone="America/Los_Angeles")
    rep = report(location=la, timezone=None)
    assert fmt(default_tz=None).current(rep)[0].startswith("Los Angeles, CA 11:15a:")
    rep = report(location=Location(0, 0, "Null Island"), timezone=None)
    assert fmt(default_tz=None).current(rep)[0].startswith("Null Island 7:15p:")  # no tz anywhere: UTC
    rep = report(location=Location(0, 0, "Null Island"), timezone="Not/AZone")
    assert fmt().current(rep)[0].startswith("Null Island 2:15p:")  # unknown tz name: default_tz


@pytest.mark.parametrize("hour, minute, h12, h24", [
    (0, 0, "12a", "00:00"), (0, 5, "12:05a", "00:05"), (6, 0, "6a", "06:00"), (12, 0, "12p", "12:00"),
    (14, 15, "2:15p", "14:15"), (23, 59, "11:59p", "23:59"),
])
def test_clock_formats(hour, minute, h12, h24):
    rep = report(current=conditions(observed_at=ny(13, hour, minute)))
    assert fmt().current(rep)[0].startswith("Boston, MA %s:" % h12)
    assert fmt(time_format="24h").current(rep)[0].startswith("Boston, MA %s:" % h24)


# ============================================================================ forecast


def test_forecast_from_real_nws_capture():
    rep = report(forecast=nws_periods("nws_forecast_02108.json"))
    assert check(fmt().forecast(rep, periods=4)) == [
        "Boston, MA fcst:\n"
        "Today: Hi 45° Chc Lt Rain then Mstly Cloudy 49%\n"
        "Tonight: Lo 35° Mstly Cloudy\n"
        "Sat: Hi 47° Ptly Sunny\n"
        "Sat Night: Lo 34° Ptly Cloudy"]
    full = check(fmt().forecast(rep))  # all 14 periods: a period line is never split across packets
    assert len(full) == 3 and full[2] == "Thu Night: Lo 38° Mstly Clear 3/3"
    lines = [line for chunk in strip_parts(full) for line in chunk.split("\n")]
    assert len(lines) == 15 and lines[5:7] == ["Sun: Hi 49° Sunny", "Sun Night: Lo 35° Mstly Clear"]


def test_forecast_real_alaska_capture_abbreviations():
    rep = report(location=Location(61.2116, -149.8761, "Anchorage, AK"), timezone="America/Anchorage",
                 forecast=nws_periods("nws_forecast_99501.json"))
    lines = "\n".join(strip_parts(check(fmt().forecast(rep)))).split("\n")
    assert lines[:2] == ["Anchorage, AK fcst:", "Fri: Hi 32° Mstly Cloudy"]  # "Tonight" ended at 6a AKST
    assert "Sat Night: Lo 22° Lt Snow Lkly 59%" in lines
    assert "Wed: Hi 32° Sl Chc Lt Snow" in lines  # 15% precip is not shown


def test_forecast_skips_periods_that_ended_in_a_cached_report():
    periods = [ForecastPeriod("This Afternoon", ny(13, 12), ny(13, 14), True, f2c(45), description="Sunny"),
               ForecastPeriod("Tonight", ny(13, 18), ny(14, 6), False, None, f2c(30), "Clear")]
    rep = report(forecast=periods, current=None, stale=True, fetched_at=NOW - timedelta(hours=3))
    assert fmt().forecast(rep) == ["Boston, MA fcst (cached 3h):\nTonight: Lo 30° Clear"]
    assert fmt().current(rep) == ["Boston, MA: no current obs. Tonight: Lo 30° Clear (cached 3h)"]
    assert fmt().forecast(rep, periods=1) == ["Boston, MA fcst (cached 3h):\nTonight: Lo 30° Clear"]
    rep = report(forecast=periods[:1])
    assert fmt().forecast(rep) == ["No forecast for Boston, MA"]


def test_forecast_period_names_and_holidays():
    periods = [
        ForecastPeriod("This Afternoon", ny(13, 14), is_daytime=True, temp_high_c=f2c(58), description="Sunny"),
        ForecastPeriod("Overnight", ny(14, 0), is_daytime=False, temp_low_c=f2c(40), description="Clear"),
        ForecastPeriod("Saturday Night", ny(14, 18), is_daytime=False, temp_low_c=f2c(40), description="Clear"),
        ForecastPeriod("Veterans Day", ny(11, 6), is_daytime=True, temp_high_c=f2c(50), description="Sunny"),
        ForecastPeriod("Veterans Day", ny(11, 18), is_daytime=False, temp_low_c=f2c(35), description="Clear"),
        ForecastPeriod("Thanksgiving Day", ny(26, 6), is_daytime=True, temp_high_c=f2c(44), description="Rain"),
        ForecastPeriod("Christmas Day", None, is_daytime=True, temp_high_c=f2c(30), description="Snow"),
    ]
    lines = fmt().forecast(report(forecast=periods))[0].split("\n")
    assert lines == ["Boston, MA fcst:", "This Aftn: Hi 58° Sunny", "Overnight: Lo 40° Clear",
                     "Sat Night: Lo 40° Clear", "Wed: Hi 50° Sunny", "Wed Night: Lo 35° Clear",
                     "Thu: Hi 44° Rain", "Christmas Day: Hi 30° Snow"]


def test_forecast_daily_periods_precip_and_wind_thresholds():
    periods = [
        ForecastPeriod("Fri", ny(13, 0), temp_high_c=f2c(58), temp_low_c=f2c(41), description="Sunny",
                       precip_chance_pct=19.6),
        ForecastPeriod("Sat", ny(14, 0), temp_high_c=f2c(50), temp_low_c=f2c(40), description="Rain",
                       precip_chance_pct=80, precip_mm=12.7, wind_speed_kmh=mph(25), wind_gust_kmh=mph(40),
                       wind_dir="W"),
        ForecastPeriod("Sun", ny(15, 0), temp_high_c=f2c(52), description="Cloudy", precip_chance_pct=19.4,
                       precip_mm=0.5, wind_speed_kmh=mph(19), wind_dir="W"),
        ForecastPeriod("Mon", ny(16, 0), temp_low_c=f2c(30), precip_chance_pct=30, wind_speed_kmh=mph(20)),
        ForecastPeriod("Tue", ny(17, 0), temp_high_c=f2c(45), description="Windy",
                       wind_speed_kmh=31.4, wind_dir="NW"),  # thresholds apply to the shown value: 20 mph, 31 km/h
    ]
    rep = report(forecast=periods)
    assert fmt().forecast(rep)[0].split("\n")[1:] == [
        "Fri: 58°/41° Sunny 20%", "Sat: 50°/40° Rain 80% 0.5in W 25 G40mph",
        "Sun: Hi 52° Cloudy", "Mon: Lo 30° Precip 30% Wind 20mph", "Tue: Hi 45° Windy NW 20mph"]
    assert fmt(units="metric").forecast(rep)[0].split("\n")[1:] == [
        "Fri: 14°/5° Sunny 20%", "Sat: 10°/4° Rain 80% 13mm W 40 G64km/h",
        "Sun: Hi 11° Cloudy", "Mon: Lo -1° Precip 30% Wind 32km/h", "Tue: Hi 7° Windy"]


@pytest.mark.parametrize("text, expected", [
    ("Slight Chance Showers And Thunderstorms", "Sl Chc Shwrs & T-storms"),
    ("Partly cloudy", "Ptly cloudy"),
    ("Heavy thunderstorm", "Hvy t-storm"),
    ("Freezing Rain Likely", "Frzg Rain Lkly"),
    ("Mostly Sunny then Sandy Showery Andover", "Mstly Sunny then Sandy Showery Andover"),
])
def test_description_abbreviations(text, expected):
    rep = report(forecast=[ForecastPeriod("Fri", ny(13, 0), description=text)])
    assert fmt().forecast(rep) == ["Boston, MA fcst:\nFri: %s" % expected]


def test_forecast_empty_stale_and_period_limit():
    rep = report(forecast=[], stale=True, fetched_at=NOW - timedelta(minutes=125))
    assert fmt().forecast(rep) == ["No forecast for Boston, MA (cached 2h5m)"]
    rep = report(forecast=nws_periods("nws_forecast_02108.json"), stale=True,
                 fetched_at=NOW - timedelta(minutes=25))
    out = fmt().forecast(rep, periods=1)
    assert out == ["Boston, MA fcst (cached 25m):\nToday: Hi 45° Chc Lt Rain then Mstly Cloudy 49%"]


# ============================================================================ alerts


def test_alerts_none_unsupported_and_list():
    assert fmt().alerts(report()) == ["No alerts for Boston, MA"]
    assert fmt().alerts(report(alerts_supported=False)) == ["Alerts: US/NWS only"]
    freeze = WeatherAlert("fz", "Freeze Warning", severity="Severe", onset=ny(14, 3), ends=ny(14, 9))
    statement = WeatherAlert("sws", "Special Weather Statement")
    out = fmt().alerts(report(alerts=[wind_advisory(ends=ny(14, 18)), freeze, statement]))
    assert out == ["Boston, MA: 3 alerts\n1) Wind Advisory til Sat 6p\n2) Freeze Warning from Sat 3a\n"
                   "3) Special Weather Statement\n!alerts 1 = details"]
    out = fmt(prefix="").alerts(report(alerts=[wind_advisory()], stale=True,
                                       fetched_at=NOW - timedelta(minutes=10)))
    assert out == ["Boston, MA: 1 alert (cached 10m)\n1) Wind Advisory til 6p\nalerts 1 = details"]


def test_alert_times_far_away_and_real_capture_timezones():
    far = wind_advisory(ends=ny(23, 18))
    assert fmt().alerts(report(alerts=[far]))[0].split("\n")[1] == "1) Wind Advisory til 11/23 6p"
    alerts = sample_alerts()
    rep = report(alerts=list(alerts.values()), timezone="America/Chicago")
    lines = fmt().alerts(rep)[0].split("\n")
    assert lines[2] == "2) Freeze Warning from Sat 12a"
    assert lines[1] == "1) Flood Advisory til 8:15a"  # 6:15a PST shown in the report's Central time


def test_alert_detail_real_capture_cleaned():
    freeze = sample_alerts()["Freeze Warning"]
    out = check(fmt().alert_detail(freeze, 2, "America/Chicago"))
    assert out == [
        "2) Freeze Warning (Severe) from Sat 12a til Sat 8a: WHAT...Sub-freezing temperatures as low as 26 across "
        "Northern Alabama. WHERE...Portions of north central, northeast, and northwest Alabama. 1/3",
        "WHEN...For the first Freeze Warning, from midnight tonight to 8 AM CST Saturday. For the second Freeze "
        "Warning, from midnight Saturday Night to 8 AM CST Sunday. 2/3",
        "IMPACTS...Frost and freeze conditions could kill crops, other sensitive vegetation and possibly damage "
        "unprotected outdoor plumbing. Take steps now to protect tender plants from the cold. 3/3"]
    assert fmt().alert_detail(freeze, 2, "America/Chicago", max_parts=10) == out  # it fits: nothing cut


def test_alert_detail_truncates_at_a_word_and_fills_the_last_packet():
    flood = sample_alerts()["Flood Advisory"]
    out = check(fmt().alert_detail(flood, 1, "America/Los_Angeles"))
    assert len(out) == 3
    assert out[0] == ("1) Flood Advisory (Minor) til 6:15a: WHAT...Urban area and small stream flooding caused by "
                      "excessive rainfall is expected. 1/3")
    assert out[2].endswith(" thunderstorms. This will cause... 3/3") and utf8_len(out[2]) >= 195
    joined = " ".join(strip_parts(out))
    assert "*" not in joined and "\n" not in joined and "  " not in joined and "- At 414" not in joined
    full = strip_parts(check(fmt().alert_detail(flood, 1, "America/Los_Angeles", max_parts=9)))
    assert len(full) == 5 and full[-1].endswith("dangers of flooding.")


def test_alert_detail_drops_nws_preamble():
    out = check(fmt().alert_detail(tornado(), 1, "America/New_York"))
    assert out == [
        "1) Tornado Warning (Extreme) til 3:45p: Tornado Warning for... Central Suffolk County in eastern "
        "Massachusetts... Until 345 PM EST. 1/3",
        "At 210 PM EST, a severe thunderstorm capable of producing a tornado was located over Brookline, moving "
        "northeast at 30 mph. 2/3",
        "TAKE COVER NOW! Move to a basement or an interior room on the lowest floor of a sturdy building. "
        "Avoid windows. 3/3"]


def test_alert_detail_small_packets_and_headline_fallback():
    flood = sample_alerts()["Flood Advisory"]
    out = check(fmt(max_bytes=60).alert_detail(flood, 1, "America/Los_Angeles", max_parts=2), 60)
    assert len(out) == 2 and out[1].endswith("... 2/2")
    bare = WeatherAlert("x", "Wind Advisory", headline="Gusts to 50 mph", severity="Unknown")
    assert fmt().alert_detail(bare, 1, None) == ["1) Wind Advisory: Gusts to 50 mph"]
    out = fmt(number_parts=False).alert_detail(flood, 1, None, max_parts=1)
    assert len(out) == 1 and out[0].endswith("...") and utf8_len(out[0]) <= 200


def test_new_alert_tornado():
    # Generic NWS headline -> first sentence of the description (minus the NWS preamble), then the call to action.
    out = check(fmt().new_alert(tornado(), "Boston, MA", "America/New_York"))
    assert out == ["⚠ Tornado Warning for Boston, MA til 3:45p: Tornado Warning for... Central Suffolk County in "
                   "eastern Massachusetts... Until 345 PM EST. TAKE COVER NOW!"]
    update = check(fmt().new_alert(tornado(message_type="Update"), "Boston, MA", None))
    assert update[0].startswith("UPDATE: ⚠ Tornado Warning for Boston, MA til 3:45p: ")
    ascii_out = check(fmt(ascii_only=True).new_alert(tornado(), "Boston, MA", None), ascii_only=True)
    assert ascii_out[0].startswith("! Tornado Warning for Boston, MA til 3:45p: Tornado Warning for...")
    future = fmt().new_alert(tornado(onset=ny(13, 16), ends=ny(13, 17)), "Boston, MA", None)
    assert future[0].startswith("⚠ Tornado Warning for Boston, MA from 4p til 5p: ")


def test_new_alert_real_captures():
    alerts = sample_alerts()
    freeze = check(fmt().new_alert(alerts["Freeze Warning"], "Huntsville, AL", "America/Chicago"))
    assert len(freeze) == 2  # Severe: NWS headline first, then the first sentence of the instruction
    assert freeze[0].startswith("⚠ Freeze Warning for Huntsville, AL from Sat 12a til Sat 8a: FREEZE WARNING IN "
                                "EFFECT FROM MIDNIGHT TONIGHT TO 8 AM CST SATURDAY")
    assert freeze[1].endswith("8 AM CST SUNDAY Take steps now to protect tender plants from the cold. 2/2")
    fog = check(fmt().new_alert(alerts["Special Weather Statement"], "Kennewick, WA", "America/Los_Angeles"))
    assert fog == ["⚠ Special Weather Statement for Kennewick, WA til 9a: Areas of dense fog have been "
                   "observed along the Columbia Basin of Washington including but not limited to: "
                   "Interstate 82, WA-240."]
    flood = fmt().new_alert(alerts["Flood Advisory"], "", "America/Los_Angeles")  # Minor: no instruction
    assert flood == ["⚠ Flood Advisory til 6:15a: FLOOD ADVISORY IN EFFECT UNTIL 615 AM PST THIS MORNING"]


def test_new_alert_call_to_action_survives_long_headlines():
    long = tornado(headline="TORNADO WARNING " + "X " * 400, description=None)
    out = check(fmt().new_alert(long, "Boston, MA", None))
    assert len(out) == 2
    assert out[0].startswith("⚠ Tornado Warning for Boston, MA til 3:45p: TAKE COVER NOW! TORNADO WARNING X X")
    assert out[1].endswith("X X... 2/2")
    for size in (60, 61, 100, 137, 228):
        out = check(fmt(max_bytes=size).new_alert(long, "Boston, MA", None), size)
        assert len(out) == 2 and "TAKE COVER NOW!" in " ".join(strip_parts(out))
    assert fmt(max_bytes=60).new_alert(long, "Boston, MA", None)[0] == (
        "⚠ Tornado Warning for Boston, MA til 3:45p: TAKE COVER 1/2")
    # A call to action longer than half a packet is not forced in front of the headline.
    wordy = tornado(headline="TORNADO WARNING " + "X " * 400, instruction="Seek " * 30 + "shelter now.")
    out = check(fmt().new_alert(wordy, "Boston, MA", None))
    assert len(out) == 2 and "Seek" not in "".join(out)


def test_new_alert_minimal_alert():
    bare = WeatherAlert("x", "", severity="Severe")
    assert fmt().new_alert(bare, "", None) == ["⚠ Alert"]


# ============================================================================ transit


def test_transit_real_mbta_park_street_sample():
    now = from_epoch(1794573834)
    rows = [("Ashmont", 1794573858), ("Alewife", 1794573891), ("Alewife", 1794573964), ("Alewife", 1794574156),
            ("Braintree", 1794574264), ("Alewife", 1794574300), ("Ashmont", 1794574605)]
    arrivals = [Arrival("70075", "Red", "Red", h, from_epoch(t)) for h, t in rows]
    out = fmt().transit([StopArrivals(PARK, arrivals)], now)
    assert out == ["Park St: Red>Ashmont now,13m | Red>Alewife 1,2,5m | Red>Braintree 7m"]


def test_transit_onebusaway_sample_marks_scheduled_times():
    # the arrivals of tests/fixtures/oba_arrivals_sample.json (synthetic)
    now = from_epoch(1794571200.25)
    rows = [("5", "Downtown", 1794572100, True), ("5", "Downtown", 1794573180, False),
            ("8", "Hillcrest", 1794573690, True), ("12", "Riverside Park", 1794574050, False),
            ("5", "Downtown", 1794574530, True)]
    arrivals = [Arrival("1_10001", r, r, h, from_epoch(t), realtime=rt) for r, h, t, rt in rows]
    out = check(fmt().transit([StopArrivals(TransitStop("1_10001", "Main St"), arrivals)], now))
    assert out == ["Main St: 5>Downtown 15,33*,55m | 8>Hillcrest 41m | 12>Riverside Park 47m*\n*=scheduled"]


def test_transit_countdown_rule():
    arrivals = [arrival(30), arrival(31), arrival(89, headsign="B"), arrival(90, headsign="B"),
                arrival(200, headsign="C", status="Boarding"), arrival(300, headsign="C", status=" APPROACHING "),
                arrival(400, headsign="C", status="Stopped 2 stops away"),
                arrival(-59, headsign="D"), arrival(-61, headsign="E")]
    out = fmt().transit([StopArrivals(PARK, arrivals)], NOW, max_arrivals=5)
    assert out == ["Park St: Red>D now | Red>Alewife now,1m | Red>B 1,2m | Red>C now,now,7m"]


def test_transit_clock_times_headsigns_errors_and_empty():
    arrivals = [arrival(90 * 60, route="39", headsign=None), arrival(91 * 60, route="39", headsign=None),
                arrival(60, route="66", headsign="66"), arrival(120, route="", headsign="Downtown")]
    stops = [StopArrivals(PARK, arrivals),
             StopArrivals(TransitStop("1", "Fenwood"), error="Stop data loading, try later"),
             StopArrivals(TransitStop("2", "Quiet"))]
    out = fmt().transit(stops, NOW)
    assert out == ["Park St: 66 1m | ?>Downtown 2m | 39 90m,3:46p\nFenwood: Stop data loading, try later\n"
                   "Quiet: no arrivals soon"]
    late = datetime(2026, 11, 14, 4, 30, tzinfo=timezone.utc)  # 11:30p Friday in Boston
    out = fmt(time_format="24h").transit([StopArrivals(PARK, [Arrival("s", "1", "1", None,
                                                                      late + timedelta(minutes=95))])], late)
    assert out == ["Park St: 1 Sat 01:05"]
    assert fmt().transit([], NOW) == []


def test_transit_stop_error_text_reaches_the_user():
    stops = [StopArrivals(TransitStop("1", "Fenwood"), error="  Stop  not found "),
             StopArrivals(TransitStop("2", "Quiet"), error="   ")]  # nothing to say: generic text
    assert fmt().transit(stops, NOW) == ["Fenwood: Stop not found\nQuiet: unavailable"]
    out = fmt(ascii_only=True).broadcast(None, ["transit"], [StopArrivals(PARK, error="Données indisponibles")], NOW)
    assert out == ["Park St: Donnees indisponibles"]


def test_transit_max_arrivals_and_group_order():
    arrivals = [arrival(700, headsign="Ashmont"), arrival(100, route="Green", headsign="Riverside", status="ARRIVING"),
                arrival(900, headsign="Ashmont"), arrival(400), arrival(1000)]
    out = fmt().transit([StopArrivals(PARK, arrivals)], NOW, max_arrivals=1)
    assert out == ["Park St: Green>Riverside now | Red>Alewife 7m | Red>Ashmont 12m"]
    out = fmt(ascii_only=True).transit([StopArrivals(TransitStop("x", "Plaza Mayagüez"), arrivals)], NOW, 2)
    assert out == ["Plaza Mayaguez: Green>Riverside now | Red>Alewife 7,17m | Red>Ashmont 12,15m"]


def test_transit_long_stop_line_breaks_between_groups():
    arrivals = [arrival(60 * i, route="R%d" % i, headsign="Somewhere Far Away %d" % i) for i in range(1, 9)]
    out = check(fmt(max_bytes=100).transit([StopArrivals(PARK, arrivals)], NOW), 100)
    assert len(out) > 1
    for chunk in strip_parts(out):
        assert not chunk.endswith("|") and not chunk.startswith("|")
        assert re.search(r"Away \d+ \d+m$", chunk), chunk  # every packet ends on a complete group


# ============================================================================ broadcast


def _full_report(**kw) -> WeatherReport:
    periods = [ForecastPeriod("Tonight", ny(13, 18), is_daytime=False, temp_low_c=f2c(41), description="Rain",
                              precip_chance_pct=60)]
    base = dict(forecast=periods, alerts=[wind_advisory()])
    base.update(kw)
    return report(**base)


def test_broadcast_combinations():
    rep = _full_report()
    out = fmt().broadcast(rep, ["current", "forecast", "alerts"])
    assert out == ["Boston, MA 2:15p: 54°F feels 50° Cloudy. Wind NW 12 G20mph. RH 65%. 30.12inHg. "
                   "1 alert: !alerts\nTonight: Lo 41° Rain 60%\n⚠ Wind Advisory til 6p"]
    assert fmt().broadcast(rep, ["forecast"]) == ["Boston, MA fcst:\nTonight: Lo 41° Rain 60%"]
    out = fmt().broadcast(rep, ["forecast", "current", "current"])
    assert out[0].startswith("Boston, MA fcst:\nTonight") and out[0].count("Boston, MA") == 2
    assert fmt().broadcast(report(), ["alerts"]) == []
    assert fmt().broadcast(None, ["current", "forecast", "alerts"]) == []
    assert fmt().broadcast(rep, []) == []


def test_broadcast_stale_and_future_alerts():
    freeze = WeatherAlert("fz", "Freeze Warning", severity="Severe", onset=ny(14, 3), ends=ny(14, 9))
    rep = _full_report(stale=True, fetched_at=NOW - timedelta(minutes=40), alerts=[freeze])
    assert fmt().broadcast(rep, ["forecast", "alerts"]) == [
        "Boston, MA fcst (cached 40m):\nTonight: Lo 41° Rain 60%\n⚠ Freeze Warning from Sat 3a"]
    out = fmt(time_format="24h").broadcast(rep, ["current", "forecast"])
    assert out == ["Boston, MA 14:15: 54°F feels 50° Cloudy. Wind NW 12 G20mph. RH 65%. 30.12inHg. "
                   "1 alert: !alerts (cached 40m)\nTonight: Lo 41° Rain 60%"]
    rep = report(current=None, forecast=[], alerts=[])
    assert fmt().broadcast(rep, ["current", "forecast", "alerts"]) == ["Boston, MA: no current obs"]


def test_broadcast_with_transit_and_without_report():
    stops = [StopArrivals(PARK, [arrival(240, headsign="Ashmont"), arrival(600, realtime=False)])]
    out = fmt().broadcast(None, ["current", "transit"], transit=stops, now=NOW)
    assert out == ["Park St: Red>Ashmont 4m | Red>Alewife 10m*\n*=scheduled"]
    out = fmt().broadcast(_full_report(), ["current", "transit"], transit=stops)
    assert out[0].endswith("1 alert: !alerts\nPark St: Red>Ashmont 4m | Red>Alewife 10m*\n*=scheduled")
    assert fmt().broadcast(None, ["transit"], transit=None) == []


def test_broadcast_splits_over_packets_ascii():
    rep = report(forecast=nws_periods("nws_forecast_02108.json"), alerts=[wind_advisory()])
    out = check(fmt(ascii_only=True).broadcast(rep, ["current", "forecast", "alerts"]), ascii_only=True)
    assert len(out) == 3 and out[-1].endswith(" 3/3") and "! Wind Advisory til 6p" in out[-1]


# ============================================================================ bot replies


def test_help():
    assert fmt().help(True, False) == [
        "Cmds: !wx [zip], !fc [zip], !alerts [n], !bus [stop], !status, !ping. In DMs the ! is optional."]
    assert fmt().help(False, True) == [
        "Cmds: !wx [zip], !fc [zip], !alerts [n], !status, !ping. In DMs the ! is optional.\n"
        "Admin: !setzip ZIP, !setinterval min|off, !broadcast"]
    assert fmt(prefix="").help(True, True) == [
        "Cmds: wx [zip], fc [zip], alerts [n], bus [stop], status, ping.\n"
        "Admin: setzip ZIP, setinterval min|off, broadcast"]
    assert "In DMs the # is optional." in fmt(prefix="#").help(False, False)[0]


def _status(**kw) -> dict:
    base = dict(version="0.1.0", zip_code="02108", zip_source="config", location="Boston, MA",
                interval_minutes=180, interval_source="config", times=[],
                next_broadcast=datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc), broadcast_enabled=True,
                channel_index=0, link="up", link_desc="serial /dev/ttyUSB0", queue=0, uptime_s=2 * 3600 + 13 * 60,
                alerts_active=0, transit_enabled=False)
    base.update(kw)
    return base


def test_status():
    assert fmt().status(_status()) == [
        "ZIP 02108 Boston, MA. Bcast every 180m on ch0, next 3p. Link up, queue 0, up 2h13m. v0.1.0"]
    off = _status(interval_minutes=0, next_broadcast=None, alerts_active=2, uptime_s=3 * 86400 + 4 * 3600)
    assert fmt().status(off) == [
        "ZIP 02108 Boston, MA. Sched bcast off. Link up, queue 0, up 3d4h. 2 alerts active. v0.1.0"]
    assert fmt().status(_status(alerts_active=1, uptime_s=59, queue=3))[0].endswith(
        "Link up, queue 3, up 0m. 1 alert active. v0.1.0")
    assert "Bcast disabled." in fmt().status(_status(broadcast_enabled=False))[0]
    out = fmt().status(_status(times=["07:00", "12:00", "18:30"], channel_index=1,
                               next_broadcast=ny(14, 0, 0)))
    assert "Bcast at 7a,12p,6:30p on ch1, next Sat 12a." in out[0]
    out = fmt(time_format="24h").status(_status(times=["07:00", "bogus"], location=None, link="down"))
    assert out == ["ZIP 02108. Bcast at 07:00,bogus on ch0, next 15:00. Link down, queue 0, up 2h13m. v0.1.0"]
    assert fmt().status({}) == ["No status"]
    assert fmt().status({"link": "up"}) == ["Link up."]


def test_text_and_error():
    assert fmt().text("pong\n  hops:   2 ") == ["pong\nhops: 2"]
    assert fmt().text("") == []
    long = check(fmt(max_bytes=60).text("word " * 100), 60)
    assert len(long) > 2 and long[-1].endswith("/%d" % len(long))
    assert fmt().error("ZIP 12345 not found") == ["ZIP 12345 not found"]
    (err,) = check(fmt(max_bytes=60).error("Something failed because " + "very " * 50 + "long"), 60)
    assert err.endswith("...") and err.startswith("Something failed because very")
    assert fmt().error("") == ["Error"]
    assert fmt(ascii_only=True).error("⚠ \U0001F600") == ["!"]


# ============================================================================ invariant sweep


def _stress_inputs():
    alerts = list(sample_alerts().values()) + [tornado(message_type="Update"), wind_advisory(ends=ny(25, 6))]
    rep = report(location=Location(18.2, -67.1, "Mayagüez \U0001F334 " + "Very Long Place Name " * 6),
                 forecast=nws_periods("nws_forecast_02108.json"), alerts=alerts, stale=True,
                 fetched_at=NOW - timedelta(minutes=95), notes=["via Open-Meteo", "obs 3h old — “stale”"])
    arrivals = [arrival(45 * i + 10, route="R%d" % (i % 4), headsign="Destination é %d" % (i % 3),
                        realtime=i % 2 == 0) for i in range(30)]
    stops = [StopArrivals(PARK, arrivals), StopArrivals(TransitStop("x", "Err"), error="boom"),
             StopArrivals(TransitStop("y", "Empty"))]
    return rep, alerts, stops


@pytest.mark.parametrize("ascii_only", [False, True])
@pytest.mark.parametrize("number_parts", [True, False])
def test_every_output_respects_byte_limit(ascii_only, number_parts):
    rep, alerts, stops = _stress_inputs()
    for size in list(range(60, 229, 21)) + [228]:
        f = fmt(max_bytes=size, ascii_only=ascii_only, number_parts=number_parts)
        outputs = [f.current(rep), f.forecast(rep), f.alerts(rep), f.help(True, True), f.status(_status()),
                   f.text("x" * 1000), f.error("y " * 300), f.transit(stops, NOW, 5),
                   f.broadcast(rep, ["current", "forecast", "alerts", "transit"], stops, NOW, 5)]
        outputs += [f.alert_detail(a, i, "America/Chicago", 3) for i, a in enumerate(alerts, 1)]
        outputs += [f.new_alert(a, rep.location.name, None) for a in alerts]
        for out in outputs:
            check(out, size, ascii_only)
        assert all(len(f.new_alert(a, "Here", None)) <= 2 for a in alerts)
        assert all(len(f.alert_detail(a, 1, None, 3)) <= 3 for a in alerts)
        assert len(f.error("z " * 500)) == 1


def test_formatter_random_texts_respect_limits():
    rng = random.Random(1113)
    for case in range(200):
        size = rng.choice([60, 61, 75, 100, 137, 200, 228])
        ascii_only, number_parts = rng.random() < 0.3, rng.random() < 0.8
        f = fmt(max_bytes=size, ascii_only=ascii_only, number_parts=number_parts)
        text = _random_text(rng)
        alert = WeatherAlert("r", _random_text(rng)[:40], headline=_random_text(rng),
                             severity=rng.choice(["Severe", "Extreme", "Minor", None]),
                             message_type=rng.choice(["Alert", "Update"]), description=_random_text(rng),
                             instruction=_random_text(rng), ends=NOW + timedelta(hours=rng.randint(-5, 200)))
        max_parts = rng.randint(1, 4)
        for out, limit in ((f.error(text), 1), (f.alert_detail(alert, case, None, max_parts), max_parts),
                           (f.new_alert(alert, text[:30], None), 2)):
            check(out, size, ascii_only)
            assert 1 <= len(out) <= limit, case
        out = check(f.text(text), size, ascii_only)
        if not ascii_only:
            bodies = strip_parts(out) if number_parts else out
            assert nonws("".join(bodies)) == nonws(text), case
