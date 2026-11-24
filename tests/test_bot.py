"""Tests for meshwx.bot.CommandBot (stub services/formatter, real RuntimeSettings)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pytest

from meshwx.bot import BotContext, CommandBot
from meshwx.errors import GeocodeError, MeshWxError, TransitError, WeatherError
from meshwx.httpclient import HttpError
from meshwx.models import (
    BROADCAST,
    IncomingMessage,
    Location,
    OutgoingMessage,
    StopArrivals,
    TransitStop,
    WeatherAlert,
    WeatherReport,
    node_id_from_num,
)
from meshwx.state import RuntimeSettings
from meshwx.timeutil import utcnow

from conftest import make_config

MY_NODE = 0x0A0B0C0D
USER = 0x11223344
OTHER_USER = 0x55667788
ADMIN = 0x000000AA
ADMIN_ID = "!000000aa"

LOCATIONS = {
    "02108": Location(42.357, -71.064, "Boston, MA", "02108", timezone="America/New_York"),
    "90210": Location(34.090, -118.406, "Beverly Hills, CA", "90210", timezone="America/Los_Angeles"),
}
STOPS = [TransitStop("place-pktrm", "Park St"), TransitStop("place-harsq", "Harvard")]


# ============================================================================ stubs (docs/CONTRACTS.md shapes)


class StubWeather:
    """WeatherService stand-in serving canned reports for LOCATIONS."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.report_calls: List[Optional[str]] = []
        self.location_calls: List[str] = []
        self.fail: Optional[BaseException] = None
        self.alerts = [WeatherAlert(id="a1", event="Wind Advisory"), WeatherAlert(id="a2", event="Flood Watch")]

    def location_for_zip(self, zip_code: str) -> Location:
        self.location_calls.append(zip_code)
        if zip_code not in LOCATIONS:
            raise GeocodeError("no such ZIP", user_message="ZIP %s not found" % zip_code)
        return LOCATIONS[zip_code]

    def get_report(self, zip_code: Optional[str] = None, *, force: bool = False) -> WeatherReport:
        self.report_calls.append(zip_code)
        if self.fail is not None:
            raise self.fail
        loc = LOCATIONS[zip_code or self.settings.zip_code]
        return WeatherReport(location=loc, provider="nws", fetched_at=utcnow(), timezone=loc.timezone,
                             alerts=list(self.alerts), alerts_supported=True)


class StubTransit:
    """TransitService stand-in: find_stop by 1-based number or label prefix."""

    def __init__(self, stops: Sequence[TransitStop]) -> None:
        self._stops = list(stops)
        self.arrival_calls: List[Any] = []
        self.fail: Optional[BaseException] = None

    @property
    def stops(self) -> List[TransitStop]:
        return list(self._stops)

    def find_stop(self, query: str) -> Optional[TransitStop]:
        q = query.strip().lower()
        if q.isdigit():
            return self._stops[int(q) - 1] if 0 < int(q) <= len(self._stops) else None
        return next((s for s in self._stops if s.label.lower().startswith(q)), None)

    def get_arrivals(self, stops: Optional[Sequence[TransitStop]] = None,
                     now: Optional[datetime] = None) -> List[StopArrivals]:
        self.arrival_calls.append((stops, now))
        if self.fail is not None:
            raise self.fail
        return [StopArrivals(stop=s) for s in (stops or self._stops)]


class StubFormatter:
    """Formatter stand-in: tags every output so tests can see which method produced it."""

    max_bytes = 200

    def __init__(self) -> None:
        self.transit_calls: List[Any] = []

    def current(self, report: WeatherReport) -> List[str]:
        return ["CURRENT:" + report.location.name]

    def forecast(self, report: WeatherReport, periods: Optional[int] = None) -> List[str]:
        return ["FC1:" + report.location.name, "FC2:" + report.location.name]

    def alerts(self, report: WeatherReport) -> List[str]:
        return ["ALERTS:%d" % len(report.alerts)]

    def alert_detail(self, alert: WeatherAlert, index: int, tz_name: Optional[str], max_parts: int = 3) -> List[str]:
        return ["ALERT#%d:%s:%s" % (index, alert.event, tz_name)]

    def transit(self, results: Sequence[StopArrivals], now: datetime, max_arrivals: int = 3) -> List[str]:
        self.transit_calls.append((now, max_arrivals))
        return ["TRANSIT:" + ",".join(r.stop.label for r in results)]

    def text(self, text: str) -> List[str]:
        return ["TXT:" + text]

    def help(self, transit_enabled: bool, is_admin: bool) -> List[str]:
        return ["HELP transit=%s admin=%s" % (transit_enabled, is_admin)]

    def status(self, info: Dict[str, Any]) -> List[str]:
        return ["STATUS:%s" % info["zip_code"]]

    def error(self, user_message: str) -> List[str]:
        return ["ERR:" + user_message]


class Ticker:
    """Injected monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    bot: CommandBot
    ctx: BotContext
    weather: StubWeather
    transit: Optional[StubTransit]
    formatter: StubFormatter
    clock: Ticker
    broadcasts: List[int]


@pytest.fixture
def make_bot(tmp_path):
    """Factory: ``make_bot(commands={...}, weather={...}, ...)`` -> Env (cooldown 0 unless given)."""

    def factory(*, transit_service: bool = True, stops: Sequence[TransitStop] = STOPS,
                **sections: Dict[str, Any]) -> Env:
        overrides: Dict[str, Any] = {"commands": {"admins": [ADMIN_ID], "channels": [0, 2], "cooldown_seconds": 0}}
        for name, values in sections.items():
            overrides.setdefault(name, {}).update(values)
        cfg = make_config(dict(overrides, state_dir=str(tmp_path)))
        settings = RuntimeSettings(cfg, tmp_path)
        weather = StubWeather(settings)
        transit = StubTransit(stops) if transit_service else None
        formatter = StubFormatter()
        broadcasts: List[int] = []
        ctx = BotContext(cfg=cfg, settings=settings, weather=weather, transit=transit, formatter=formatter,
                         status_fn=lambda: {"zip_code": settings.zip_code},
                         broadcast_fn=lambda: broadcasts.append(1))
        clock = Ticker()
        return Env(CommandBot(ctx, clock=clock), ctx, weather, transit, formatter, clock, broadcasts)

    return factory


def dm(text: str, sender: int = USER, channel_index: int = 0, packet_id: int = 4242, **kw: Any) -> IncomingMessage:
    return IncomingMessage(text=text, from_num=sender, from_id=node_id_from_num(sender), to_num=MY_NODE,
                           channel_index=channel_index, is_dm=True, packet_id=packet_id, **kw)


def chan(text: str, channel: int = 0, sender: int = USER, **kw: Any) -> IncomingMessage:
    return IncomingMessage(text=text, from_num=sender, from_id=node_id_from_num(sender), to_num=0xFFFFFFFF,
                           channel_index=channel, is_dm=False, packet_id=4243, **kw)


def texts(out: List[OutgoingMessage]) -> List[str]:
    return [m.text for m in out]


# ============================================================================ parsing


@pytest.mark.parametrize(
    "word, command",
    [
        ("help", "help"), ("?", "help"), ("h", "help"), ("cmds", "help"),
        ("wx", "wx"), ("weather", "wx"), ("w", "wx"), ("now", "wx"),
        ("fc", "fc"), ("forecast", "fc"), ("f", "fc"),
        ("alerts", "alerts"), ("alert", "alerts"), ("warn", "alerts"),
        ("bus", "bus"), ("transit", "bus"), ("next", "bus"), ("t", "bus"),
        ("ping", "ping"), ("test", "ping"), ("status", "status"),
        ("setzip", "setzip"), ("zip", "setzip"), ("setinterval", "setinterval"), ("interval", "setinterval"),
        ("broadcast", "broadcast"), ("bcast", "broadcast"),
    ],
)
def test_aliases_case_insensitive(make_bot, word, command):
    bot = make_bot().bot
    assert bot.parse(dm(word + " Arg")) == (command, ["Arg"])
    assert bot.parse(chan("!" + word.upper())) == (command, [])


def test_prefix_and_channel_rules(make_bot):
    parse = make_bot().bot.parse
    assert parse(dm("wx")) == ("wx", [])
    assert parse(dm("!wx 02108")) == ("wx", ["02108"])  # prefix optional in DMs
    assert parse(dm("  WX   90210  ")) == ("wx", ["90210"])
    assert parse(dm("wx", channel_index=5)) == ("wx", [])  # DMs work on any channel
    assert parse(chan("!Wx")) == ("wx", [])
    assert parse(chan("! wx")) == ("wx", [])
    assert parse(chan("!wx", channel=2)) == ("wx", [])
    assert parse(chan("wx")) is None  # channel commands need the prefix
    assert parse(chan("!wx", channel=1)) is None  # not a command channel
    for empty in ("", "   ", "!", "! "):
        assert parse(dm(empty)) is None and parse(chan(empty)) is None


def test_unknown_words(make_bot):
    env = make_bot()
    assert env.bot.parse(dm("Hello there")) == ("unknown", ["Hello"])
    assert env.bot.parse(chan("!hello")) is None
    assert env.bot.parse(chan("hello everyone")) is None
    assert texts(env.bot.handle(dm("hello"))) == ["ERR:Unknown command. Send help"]
    assert env.bot.handle(chan("!hello")) == []


def test_trailing_punctuation_is_ignored(make_bot):
    parse = make_bot().bot.parse
    assert parse(dm("Wx.")) == ("wx", [])  # iOS turns a double space into ". "
    assert parse(dm("help?")) == ("help", [])
    assert parse(chan("!fc 90210.")) == ("fc", ["90210"])
    assert parse(dm("bus park st.")) == ("bus", ["park", "st"])
    assert parse(dm("ping !")) == ("ping", [])
    assert parse(dm("??")) == ("unknown", ["??"])  # still answered: someone is lost


@pytest.mark.parametrize("reaction", ["\N{THUMBS UP SIGN}", "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}",
                                      "\N{FACE WITH TEARS OF JOY}" * 2,
                                      "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"])
def test_emoji_reactions_get_no_reply(make_bot, reaction):
    env = make_bot(commands={"cooldown_seconds": 30})
    assert env.bot.parse(dm(reaction)) is None  # a tapback on our reply arrives as a text DM
    assert env.bot.handle(dm(reaction)) == []
    assert texts(env.bot.handle(dm("ping"))) == ["TXT:pong"]  # and does not start a cooldown


def test_other_prefixes(make_bot):
    env = make_bot(commands={"prefix": ""})
    assert env.bot.parse(chan("wx 90210")) == ("wx", ["90210"])
    assert env.bot.parse(chan("good morning")) is None
    env = make_bot(commands={"prefix": "Bot"})
    assert env.bot.parse(chan("bot fc")) == ("fc", [])
    assert env.bot.parse(chan("fc")) is None


def test_commands_disabled(make_bot):
    env = make_bot(commands={"enabled": False})
    assert env.bot.parse(dm("wx")) is None
    assert env.bot.handle(dm("wx")) == [] and env.weather.report_calls == []


# ============================================================================ addressing


def test_dm_reply_addressing(make_bot):
    [out] = make_bot().bot.handle(dm("ping", channel_index=1, packet_id=77))
    assert out == OutgoingMessage(text="TXT:pong", destination=USER, channel_index=1, reply_id=77,
                                  want_ack=True, kind="reply")
    [out] = make_bot(meshtastic={"dm_want_ack": False}).bot.handle(dm("ping"))
    assert out.want_ack is False


def test_channel_reply_goes_to_the_channel(make_bot):
    [out] = make_bot().bot.handle(chan("!ping", channel=2))
    assert out == OutgoingMessage(text="TXT:pong", destination=BROADCAST, channel_index=2, reply_id=4243,
                                  want_ack=False, kind="reply")


def test_channel_command_answered_by_dm_when_configured(make_bot):
    [out] = make_bot(commands={"reply_on_channel": False}).bot.handle(chan("!ping", channel=2))
    assert (out.destination, out.channel_index, out.want_ack, out.reply_id, out.kind) == (USER, 2, True, 4243, "reply")


def test_one_packet_per_formatter_chunk(make_bot):
    out = make_bot().bot.handle(chan("!fc"))
    assert texts(out) == ["FC1:Boston, MA", "FC2:Boston, MA"]
    assert all(m.destination == BROADCAST and m.reply_id == 4243 for m in out)


def test_formatter_is_taken_from_the_context(make_bot):
    env = make_bot()

    class Shouting(StubFormatter):
        def text(self, text: str) -> List[str]:
            return ["LOUD:" + text.upper()]

    env.ctx.formatter = Shouting()
    assert texts(env.bot.handle(dm("ping"))) == ["LOUD:PONG"]


# ============================================================================ rate limiting


def test_rate_limit_dm_says_please_wait_once(make_bot):
    env = make_bot(commands={"cooldown_seconds": 30})
    assert texts(env.bot.handle(dm("ping"))) == ["TXT:pong"]
    env.clock.t += 5
    assert texts(env.bot.handle(dm("ping"))) == ["ERR:Please wait 25s"]
    env.clock.t += 1
    assert env.bot.handle(dm("wx")) == []  # already told: silent
    env.clock.t += 24  # 30 s after the first command
    assert texts(env.bot.handle(dm("ping"))) == ["TXT:pong"]
    env.clock.t += 0.5
    assert texts(env.bot.handle(dm("ping"))) == ["ERR:Please wait 30s"]  # new cooldown, told again


def test_rate_limit_on_channel_is_silent(make_bot):
    env = make_bot(commands={"cooldown_seconds": 30})
    assert env.bot.handle(chan("hello all")) == []  # chatter is no command and does not count
    assert texts(env.bot.handle(chan("!ping"))) == ["TXT:pong"]
    assert env.bot.handle(chan("!ping")) == []
    assert texts(env.bot.handle(dm("ping"))) == ["ERR:Please wait 30s"]


def test_rate_limit_is_per_node_and_skips_admins(make_bot):
    env = make_bot(commands={"cooldown_seconds": 30})
    assert texts(env.bot.handle(dm("ping"))) == ["TXT:pong"]
    assert texts(env.bot.handle(dm("ping", sender=OTHER_USER))) == ["TXT:pong"]
    for _ in range(3):
        assert texts(env.bot.handle(chan("!ping", sender=ADMIN))) == ["TXT:pong"]


def test_failed_and_unknown_commands_count(make_bot):
    env = make_bot(commands={"cooldown_seconds": 30})
    assert texts(env.bot.handle(dm("wx 123"))) == ["ERR:ZIP must be 5 digits"]
    assert texts(env.bot.handle(dm("wx"))) == ["ERR:Please wait 30s"]
    assert texts(env.bot.handle(dm("hi", sender=OTHER_USER))) == ["ERR:Unknown command. Send help"]
    assert env.bot.handle(chan("!wx", sender=OTHER_USER)) == []


def test_cooldown_table_stays_small(make_bot):
    env = make_bot(commands={"cooldown_seconds": 30})
    for n in range(200):
        assert texts(env.bot.handle(dm("ping", sender=0x1000 + n))) == ["TXT:pong"]
        env.clock.t += 31
    assert len(env.bot._cooldowns) <= 64  # expired entries are pruned (Pi Zero memory)


# ============================================================================ weather


def test_wx_and_fc_for_home_and_other_zips(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("wx"))) == ["CURRENT:Boston, MA"]
    assert texts(env.bot.handle(dm("weather 90210"))) == ["CURRENT:Beverly Hills, CA"]
    assert texts(env.bot.handle(dm("f 90210-1234"))) == ["FC1:Beverly Hills, CA", "FC2:Beverly Hills, CA"]
    assert texts(env.bot.handle(dm("wx 02108"))) == ["CURRENT:Boston, MA"]
    assert env.weather.report_calls == [None, "90210", "90210", None]  # the home ZIP means home


def test_wx_rejects_malformed_zip_without_fetching(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("wx 123"))) == ["ERR:ZIP must be 5 digits"]
    assert texts(env.bot.handle(dm("wx boston"))) == ["ERR:ZIP must be 5 digits"]
    assert env.weather.report_calls == []


def test_other_zips_can_be_disabled(make_bot):
    env = make_bot(weather={"allow_other_zips": False})
    assert texts(env.bot.handle(dm("fc 90210"))) == ["ERR:Other ZIPs disabled"]
    assert texts(env.bot.handle(dm("wx 02108"))) == ["CURRENT:Boston, MA"]
    env.ctx.settings.set_zip("90210", set_by="test")  # the home ZIP follows the runtime setting
    assert texts(env.bot.handle(dm("wx 90210"))) == ["CURRENT:Beverly Hills, CA"]
    assert texts(env.bot.handle(dm("wx 02108"))) == ["ERR:Other ZIPs disabled"]
    assert env.weather.report_calls == [None, None]


@pytest.mark.parametrize(
    "exc, reply",
    [
        (WeatherError("both providers down", user_message="Weather unavailable, try later"),
         "ERR:Weather unavailable, try later"),
        (GeocodeError("unknown", user_message="ZIP 90210 not found"), "ERR:ZIP 90210 not found"),
        (HttpError("GET failed", "https://api.weather.gov/points/1,2", 503), "ERR:Network error, try later"),
        (MeshWxError("odd"), "ERR:Error, try again later"),
        (RuntimeError("bug"), "ERR:Internal error"),
    ],
)
def test_errors_become_one_error_packet(make_bot, exc, reply):
    env = make_bot()
    env.weather.fail = exc
    assert texts(env.bot.handle(dm("wx"))) == [reply]


def test_unexpected_errors_are_logged(make_bot, caplog):
    env = make_bot()
    env.weather.fail = RuntimeError("bug")
    with caplog.at_level(logging.ERROR, logger="meshwx.bot"):
        env.bot.handle(dm("fc"))
    assert "crashed" in caplog.text and "RuntimeError: bug" in caplog.text


def test_alerts_list_and_details(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("alerts"))) == ["ALERTS:2"]
    assert texts(env.bot.handle(dm("warn 2"))) == ["ALERT#2:Flood Watch:America/New_York"]
    assert texts(env.bot.handle(chan("!alert #1"))) == ["ALERT#1:Wind Advisory:America/New_York"]
    assert texts(env.bot.handle(dm("alerts 3"))) == ["ERR:No alert #3"]
    assert texts(env.bot.handle(dm("alerts 0"))) == ["ERR:No alert #0"]
    assert texts(env.bot.handle(dm("alerts soon"))) == ["ERR:Usage: !alerts [number]"]
    assert env.weather.report_calls == [None] * 5  # always the home location; no fetch for bad input


# ============================================================================ transit


def test_bus_all_stops(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("bus"))) == ["TRANSIT:Park St,Harvard"]
    [(stops, now)] = env.transit.arrival_calls
    assert stops is None and now.utcoffset() == timedelta(0) and abs(utcnow() - now) < timedelta(seconds=5)
    assert env.formatter.transit_calls == [(now, 3)]


def test_bus_one_stop_by_label_or_number(make_bot):
    env = make_bot(transit={"max_arrivals": 2})
    assert texts(env.bot.handle(dm("t park st"))) == ["TRANSIT:Park St"]
    assert texts(env.bot.handle(chan("!next 2"))) == ["TRANSIT:Harvard"]
    assert [stops for stops, _ in env.transit.arrival_calls] == [[STOPS[0]], [STOPS[1]]]
    assert [n for _, n in env.formatter.transit_calls] == [2, 2]


def test_bus_unknown_stop_lists_labels(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("bus nowhere"))) == ["ERR:Unknown stop. Try: Park St, Harvard"]
    assert env.transit.arrival_calls == []
    many = [TransitStop(str(n), "Stop number %d" % n) for n in range(20)]
    [hint] = texts(make_bot(stops=many).bot.handle(dm("bus nowhere")))
    assert hint.startswith("ERR:Unknown stop. Try: Stop number 0, Stop number 1") and len(hint) <= 84


def test_bus_without_transit_or_failing(make_bot):
    assert texts(make_bot(transit_service=False).bot.handle(dm("bus"))) == ["ERR:Transit not configured"]
    env = make_bot()
    env.transit.fail = TransitError("feed down")
    assert texts(env.bot.handle(dm("bus"))) == ["ERR:Transit unavailable, try later"]


# ============================================================================ ping / status / help


@pytest.mark.parametrize(
    "hops, mqtt, reply",
    [
        (None, False, "TXT:pong"),
        (2, False, "TXT:pong (2 hops)"),
        (1, False, "TXT:pong (1 hop)"),
        (0, True, "TXT:pong (direct) via MQTT"),
        (None, True, "TXT:pong via MQTT"),
    ],
)
def test_ping(make_bot, hops, mqtt, reply):
    assert texts(make_bot().bot.handle(dm("test", hops_away=hops, via_mqtt=mqtt))) == [reply]


def test_status_and_help(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(dm("status"))) == ["STATUS:02108"]
    assert texts(env.bot.handle(dm("?"))) == ["HELP transit=True admin=False"]
    assert texts(env.bot.handle(dm("help", sender=ADMIN))) == ["HELP transit=True admin=True"]
    assert texts(make_bot(transit_service=False).bot.handle(dm("cmds"))) == ["HELP transit=False admin=False"]


# ============================================================================ admin commands


def test_setzip_by_admin_geocodes_then_saves(make_bot, tmp_path):
    env = make_bot()
    assert texts(env.bot.handle(dm("setzip 90210", sender=ADMIN))) == ["TXT:Home ZIP now 90210 (Beverly Hills, CA)"]
    assert env.weather.location_calls == ["90210"]
    assert env.ctx.settings.zip_code == "90210" and env.ctx.settings.source("zip_code") == "set by " + ADMIN_ID
    assert RuntimeSettings(env.ctx.cfg, tmp_path).zip_code == "90210"  # persisted


def test_setzip_rejects_bad_zips_before_saving(make_bot, tmp_path):
    env = make_bot()
    assert texts(env.bot.handle(dm("zip 99999", sender=ADMIN))) == ["ERR:ZIP 99999 not found"]
    assert texts(env.bot.handle(dm("zip 9021", sender=ADMIN))) == ["ERR:ZIP must be 5 digits"]
    assert texts(env.bot.handle(dm("setzip", sender=ADMIN))) == ["ERR:Usage: !setzip <zip>"]
    assert env.weather.location_calls == ["99999"]
    assert env.ctx.settings.zip_code == "02108" and not (tmp_path / "state.json").exists()


def test_setinterval_by_admin(make_bot):
    env = make_bot()
    settings = env.ctx.settings
    assert texts(env.bot.handle(dm("setinterval 2h", sender=ADMIN))) == ["TXT:Broadcast every 120 min"]
    assert settings.interval_minutes == 120 and settings.source("interval_minutes") == "set by " + ADMIN_ID
    assert texts(env.bot.handle(chan("!interval OFF", sender=ADMIN))) == ["TXT:Scheduled broadcasts off"]
    assert settings.interval_minutes == 0
    assert texts(env.bot.handle(dm("interval 5", sender=ADMIN))) == ["ERR:Interval must be 15-10080 min or off"]
    assert texts(env.bot.handle(dm("interval soon", sender=ADMIN))) == ["ERR:Interval: minutes, e.g. 60, 2h, off"]
    assert texts(env.bot.handle(dm("setinterval", sender=ADMIN))) == ["ERR:Usage: !setinterval <min|Nh|off>"]
    assert settings.interval_minutes == 0


@pytest.mark.parametrize(
    "broadcast, text, reply",
    [
        ({"times": ["07:00", "18:00"]}, "setinterval 60", "TXT:Interval 60 min saved, but fixed times in config are used"),
        ({"times": ["07:00"]}, "interval off", "TXT:Interval off saved, but fixed times in config are used"),
        ({"enabled": False}, "interval 2h", "TXT:Interval 120 min saved, but broadcasts are off in config"),
    ],
)
def test_setinterval_reply_when_the_config_makes_it_moot(make_bot, broadcast, text, reply):
    env = make_bot(broadcast=broadcast)
    assert texts(env.bot.handle(dm(text, sender=ADMIN))) == [reply]
    assert env.ctx.settings.source("interval_minutes") == "set by " + ADMIN_ID  # saved all the same


def test_broadcast_by_admin(make_bot):
    env = make_bot()
    assert texts(env.bot.handle(chan("!bcast", sender=ADMIN))) == ["TXT:Broadcast queued"]
    assert env.broadcasts == [1]


@pytest.mark.parametrize("text", ["setzip 90210", "interval 60", "broadcast"])
def test_admin_commands_refused_for_others(make_bot, tmp_path, text):
    env = make_bot()
    assert texts(env.bot.handle(dm(text))) == ["ERR:Admin only"]
    assert env.broadcasts == [] and env.weather.location_calls == []
    assert (env.ctx.settings.zip_code, env.ctx.settings.interval_minutes) == ("02108", 180)
    assert not (tmp_path / "state.json").exists()


def test_admin_match_ignores_id_case_and_missing_id(make_bot):
    env = make_bot()
    msg = dm("broadcast", sender=ADMIN)
    msg.from_id = "!000000AA"
    assert texts(env.bot.handle(msg)) == ["TXT:Broadcast queued"]
    msg.from_id = ""  # node DB did not know the sender: fall back to from_num
    assert texts(env.bot.handle(msg)) == ["TXT:Broadcast queued"]


def test_works_with_the_real_formatter(make_bot):
    """Smoke test of the calls the bot makes on the real Formatter (output details are its own tests')."""
    from meshwx.formatting import Formatter

    env = make_bot(commands={"cooldown_seconds": 30})
    env.ctx.formatter = Formatter(env.ctx.cfg.display, 200)
    for text, sender in (("ping", USER), ("hello", OTHER_USER), ("help", ADMIN), ("status", ADMIN),
                         ("broadcast", 0x99), ("ping", USER)):
        out = env.bot.handle(dm(text, sender=sender, hops_away=1))
        assert len(out) >= 1 and all(0 < len(m.text.encode("utf-8")) <= 200 for m in out), text
    assert "pong" in env.bot.handle(dm("ping", sender=ADMIN))[0].text


def test_handled_commands_are_logged(make_bot, caplog):
    env = make_bot()
    with caplog.at_level(logging.INFO, logger="meshwx.bot"):
        env.bot.handle(dm("wx 90210"))
    assert "!11223344" in caplog.text and "wx" in caplog.text and "90210" in caplog.text
