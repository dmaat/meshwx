"""Tests for the shared spine: config, state, timeutil, cache, httpclient."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from meshwx.cache import TTLCache, atomic_write_json, read_json
from meshwx.config import (
    Config,
    config_from_dict,
    load_config,
    load_yaml_text,
    normalize_node_id,
    normalize_zip,
    parse_hhmm,
)
from meshwx.errors import ConfigError
from meshwx.httpclient import HttpClient, HttpError, default_user_agent
from meshwx.models import node_id_from_num, node_num_from_id
from meshwx.state import RuntimeSettings, validate_interval
from meshwx.timeutil import clock_is_sane, from_epoch, get_tz, parse_iso

from conftest import ROOT, make_config

# ============================================================================ config


def test_example_config_copies_identical_and_valid():
    pkg = ROOT / "meshwx" / "data" / "config.example.yaml"
    root = ROOT / "config.example.yaml"
    assert pkg.read_text(encoding="utf-8") == root.read_text(encoding="utf-8"), (
        "config.example.yaml (repo root) must be an exact copy of meshwx/data/config.example.yaml"
    )
    cfg = load_config(str(pkg))
    assert isinstance(cfg, Config)
    assert cfg.location.zip_code == "02108"
    assert cfg.source_path == str(pkg)


def test_example_config_mentions_every_setting():
    """Every dataclass field should be documented in the template."""
    text = (ROOT / "meshwx" / "data" / "config.example.yaml").read_text(encoding="utf-8")
    from dataclasses import fields

    cfg = Config()
    missing = []
    for section in ("location", "display", "weather", "broadcast", "commands", "meshtastic", "transit"):
        for f in fields(getattr(cfg, section)):
            if "%s:" % f.name not in text:
                missing.append("%s.%s" % (section, f.name))
    assert not missing, "undocumented settings: %s" % missing


@pytest.mark.parametrize(
    "yaml_text, expected",
    [
        ("location: {zip_code: 02134}", "02134"),  # would be octal 1116 in plain YAML
        ("location: {zip_code: 02108}", "02108"),  # invalid octal -> plain YAML keeps str, but check anyway
        ("location: {zip_code: 90210}", "90210"),  # int
        ("location: {zip_code: '02108-1234'}", "02108"),
        ("location: {zip_code: ' 10001 '}", "10001"),
    ],
)
def test_zip_parsing(yaml_text, expected):
    cfg = config_from_dict(load_yaml_text(yaml_text))
    assert cfg.location.zip_code == expected


@pytest.mark.parametrize("bad", ["2108", "123456", "ABCDE", "0213"])
def test_bad_zip_rejected(bad):
    with pytest.raises(ConfigError):
        normalize_zip(bad)


def test_non_us_postal_code():
    assert normalize_zip("sw1a 1aa", "gb") == "SW1A 1AA"


def test_unknown_key_suggests_fix():
    with pytest.raises(ConfigError, match="did you mean 'interval_minutes'"):
        make_config({"broadcast": {"intervall_minutes": 60}})


def test_unknown_section():
    with pytest.raises(ConfigError, match="unknown setting 'wether'"):
        make_config({"wether": {}})


def test_type_coercion():
    cfg = make_config(
        {
            "broadcast": {"interval_minutes": "60", "on_startup": "yes", "channel_index": 1.0},
            "meshtastic": {"message_delay_seconds": "5", "hop_limit": "null"},
        }
    )
    assert cfg.broadcast.interval_minutes == 60
    assert cfg.broadcast.on_startup is True
    assert cfg.broadcast.channel_index == 1
    assert cfg.meshtastic.message_delay_seconds == 5.0
    assert cfg.meshtastic.hop_limit is None


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"broadcast": {"interval_minutes": 5}}, "interval_minutes"),
        ({"broadcast": {"interval_minutes": 99999}}, "interval_minutes"),
        ({"broadcast": {"channel_index": 8}}, "channel_index"),
        ({"broadcast": {"times": ["25:00"]}}, "24-hour"),
        ({"broadcast": {"quiet_start": "22:00"}}, "together"),
        ({"broadcast": {"content": ["weather"]}}, "content"),
        ({"broadcast": {"alert_min_severity": "huge"}}, "alert_min_severity"),
        ({"broadcast": {"alert_channel_index": 9}}, "alert_channel_index"),
        ({"meshtastic": {"connection": "tcp"}}, "tcp_host"),
        ({"meshtastic": {"max_message_bytes": 300}}, "max_message_bytes"),
        ({"transit": {"enabled": True}}, "stops"),
        ({"transit": {"enabled": True, "provider": "onebusaway", "stops": ["1_1"]}}, "base_url"),
        ({"transit": {"provider": "nextbus"}}, "transit.provider"),
        ({"weather": {"provider": "darksky"}}, "weather.provider"),
        ({"display": {"units": "kelvin"}}, "units"),
        ({"commands": {"admins": ["bob"]}}, "node id"),
        ({"location": {"zip_code": "", "latitude": 40.0}}, "together"),
        ({"location": {"zip_code": ""}}, "zip_code"),
        ({"log_level": "LOUD"}, "log_level"),
    ],
)
def test_validation_errors(overrides, match):
    with pytest.raises(ConfigError, match=match):
        make_config(overrides)


def test_latlon_without_zip_ok():
    cfg = make_config({"location": {"zip_code": "", "latitude": 42.0, "longitude": -71.0}})
    assert cfg.location.latitude == 42.0


def test_normalisations():
    cfg = make_config(
        {
            "broadcast": {"times": ["7:05", "18:00", "07:05"], "quiet_start": "9:00", "quiet_end": "6:30",
                          "alert_min_severity": "severe", "content": ["Current", "FORECAST"]},
            "commands": {"admins": ["!A1B2C3D4", "deadbeef"]},
            "weather": {"fallback_provider": "none"},
            "log_level": "debug",
        }
    )
    assert cfg.broadcast.times == ["07:05", "18:00"]
    assert (cfg.broadcast.quiet_start, cfg.broadcast.quiet_end) == ("09:00", "06:30")
    assert cfg.broadcast.alert_min_severity == "Severe"
    assert cfg.broadcast.content == ["current", "forecast"]
    assert cfg.commands.admins == ["!a1b2c3d4", "!deadbeef"]
    assert cfg.weather.fallback_provider == ""
    assert cfg.log_level == "DEBUG"


def test_fallback_same_as_primary_is_disabled():
    cfg = make_config({"weather": {"provider": "openmeteo", "fallback_provider": "openmeteo"}})
    assert cfg.weather.fallback_provider == ""


def test_transit_stops_forms():
    cfg = make_config(
        {
            "transit": {
                "enabled": True,
                "provider": "mbta",
                "stops": ["place-pktrm", {"id": 70075, "label": "Park", "routes": "Red", "direction_id": 1},
                          {"id": "01234"}],
            }
        }
    )
    s = cfg.transit.stops
    assert s[0].id == "place-pktrm" and s[0].label == "place-pktrm" and s[0].routes == ()
    assert s[1].id == "70075" and s[1].routes == ("Red",) and s[1].direction_id == 1
    assert s[2].id == "01234"


def test_yaml_leading_zero_stop_ids_kept():
    data = load_yaml_text("transit:\n  stops:\n    - id: 01234\n")
    assert data["transit"]["stops"][0]["id"] == "01234"


def test_yaml_must_be_mapping():
    with pytest.raises(ConfigError):
        load_yaml_text("- a\n- b\n")
    assert load_yaml_text("") == {}


def test_parse_hhmm():
    assert parse_hhmm("7:05") == (7, 5)
    assert parse_hhmm("23:59") == (23, 59)
    for bad in ("24:00", "7", "07:60", "seven"):
        with pytest.raises(ConfigError):
            parse_hhmm(bad)


def test_node_ids():
    assert normalize_node_id("!DEADBEEF") == "!deadbeef"
    assert normalize_node_id(12345678) == "!12345678"
    assert node_id_from_num(0xDEADBEEF) == "!deadbeef"
    assert node_num_from_id("!deadbeef") == 0xDEADBEEF
    with pytest.raises(ConfigError):
        normalize_node_id("!1234")


def test_load_config_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MESHWX_CONFIG", raising=False)
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_load_config_env(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("location:\n  zip_code: \"10001\"\n", encoding="utf-8")
    monkeypatch.setenv("MESHWX_CONFIG", str(p))
    assert load_config().location.zip_code == "10001"


def test_state_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHWX_STATE_DIR", str(tmp_path / "s"))
    assert make_config().state_path() == tmp_path / "s"
    cfg = make_config({"state_dir": str(tmp_path / "explicit")})
    assert cfg.state_path() == tmp_path / "explicit"
    assert cfg.cache_path() == tmp_path / "explicit" / "cache"


# ============================================================================ state


@pytest.fixture
def cfg():
    return make_config({"broadcast": {"interval_minutes": 180}})


def test_interval_parsing():
    assert validate_interval("2h") == 120
    assert validate_interval("90m") == 90
    assert validate_interval("off") == 0
    assert validate_interval(" 45 ") == 45
    assert validate_interval(0) == 0
    for bad in (5, "abc", 99999, True, 1.5):
        with pytest.raises(ConfigError):
            validate_interval(bad)


def test_settings_defaults_from_config(cfg, tmp_path):
    rs = RuntimeSettings(cfg, tmp_path)
    assert rs.zip_code == "02108" and rs.interval_minutes == 180
    assert rs.source("zip_code") == "config" and not rs.zip_is_override
    assert not (tmp_path / "state.json").exists()  # nothing written until something changes


def test_settings_cli_flags(cfg, tmp_path):
    rs = RuntimeSettings(cfg, tmp_path, cli_zip="90210", cli_interval="1h")
    assert rs.zip_code == "90210" and rs.interval_minutes == 60
    assert rs.zip_is_override
    assert rs.source("interval_minutes") == "command line"


def test_settings_set_persist_and_reload(cfg, tmp_path):
    rs = RuntimeSettings(cfg, tmp_path)
    events = []
    rs.add_listener(lambda k, v: events.append((k, v)))
    assert rs.set_zip("90210-0001", "!a1b2c3d4") == "90210"
    assert rs.set_interval("2h", "!a1b2c3d4") == 120
    assert events == [("zip_code", "90210"), ("interval_minutes", 120)]
    rs2 = RuntimeSettings(cfg, tmp_path)
    assert rs2.zip_code == "90210" and rs2.interval_minutes == 120
    assert rs2.source("zip_code") == "set by !a1b2c3d4"


def test_settings_config_edit_wins_over_stale_override(cfg, tmp_path):
    RuntimeSettings(cfg, tmp_path).set_zip("90210", "cli")
    cfg.location.zip_code = "10001"  # user edited config.yaml afterwards
    rs = RuntimeSettings(cfg, tmp_path)
    assert rs.zip_code == "10001" and rs.source("zip_code") == "config"
    doc = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "zip_code" not in doc["overrides"]


def test_settings_set_back_to_config_value_clears_override(cfg, tmp_path):
    rs = RuntimeSettings(cfg, tmp_path)
    rs.set_interval(60, "x")
    rs.set_interval(180, "x")
    assert rs.source("interval_minutes") == "config"
    doc = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    entry = doc["overrides"]["interval_minutes"]
    assert entry.get("reset") is True and "value" not in entry  # a visible "back to config" marker
    fresh = RuntimeSettings(cfg, tmp_path)
    assert (fresh.interval_minutes, fresh.source("interval_minutes")) == (180, "config")


def test_settings_external_change_picked_up(cfg, tmp_path):
    service = RuntimeSettings(cfg, tmp_path)
    events = []
    service.add_listener(lambda k, v: events.append((k, v)))
    service.set_data("seen", [1])  # service has written the file once
    time.sleep(0.05)
    cli = RuntimeSettings(cfg, tmp_path)  # e.g. `meshwx set interval 60` in another process
    cli.set_interval(60, "cli")
    _bump_mtime(tmp_path / "state.json")
    assert service.refresh() == ["interval_minutes"]
    assert service.interval_minutes == 60 and events == [("interval_minutes", 60)]
    assert service.refresh() == []  # unchanged file -> no work


def test_settings_set_data_does_not_clobber_external_override(cfg, tmp_path):
    service = RuntimeSettings(cfg, tmp_path)
    service.set_data("seen", [1])
    cli = RuntimeSettings(cfg, tmp_path)
    cli.set_zip("90210", "cli")
    _bump_mtime(tmp_path / "state.json")
    service.set_data("seen", [1, 2])  # must merge, not overwrite the cli's override
    assert service.zip_code == "90210"
    assert RuntimeSettings(cfg, tmp_path).zip_code == "90210"
    assert service.get_data("seen") == [1, 2]


def test_settings_invalid_override_ignored(cfg, tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"overrides": {"interval_minutes": {"value": 3, "base": 180}}}), encoding="utf-8")
    rs = RuntimeSettings(cfg, tmp_path)
    assert rs.interval_minutes == 180


def test_settings_corrupt_file(cfg, tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    rs = RuntimeSettings(cfg, tmp_path)
    assert rs.zip_code == "02108"
    rs.set_zip("10001", "x")  # recovers by rewriting
    assert RuntimeSettings(cfg, tmp_path).zip_code == "10001"


def test_settings_bad_values_rejected(cfg, tmp_path):
    rs = RuntimeSettings(cfg, tmp_path)
    with pytest.raises(ConfigError):
        rs.set_zip("12", "x")
    with pytest.raises(ConfigError):
        rs.set_interval("1", "x")
    assert rs.zip_code == "02108" and rs.interval_minutes == 180


def _bump_mtime(p: Path) -> None:
    st = os.stat(p)
    os.utime(p, (st.st_atime, st.st_mtime + 2))


# ============================================================================ timeutil


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2025-11-13T14:00:00-05:00", datetime(2025, 11, 13, 19, 0, tzinfo=timezone.utc)),
        ("2025-11-13T19:00:00Z", datetime(2025, 11, 13, 19, 0, tzinfo=timezone.utc)),
        ("2025-11-13T19:00:00.1234567Z", datetime(2025, 11, 13, 19, 0, 0, 123456, tzinfo=timezone.utc)),
        ("2025-11-13T19:00:00.5+00:00", datetime(2025, 11, 13, 19, 0, 0, 500000, tzinfo=timezone.utc)),
        ("2025-11-13T14:00:00-0500", datetime(2025, 11, 13, 19, 0, tzinfo=timezone.utc)),
        ("2025-11-13T19:00:00", datetime(2025, 11, 13, 19, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_iso(text, expected):
    got = parse_iso(text)
    assert got == expected and got.tzinfo is not None


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2025-13-45T00:00:00Z"])
def test_parse_iso_bad(bad):
    assert parse_iso(bad) is None


def test_misc_time():
    assert from_epoch(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert not clock_is_sane(datetime(1970, 1, 1, tzinfo=timezone.utc))
    assert clock_is_sane(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert get_tz("Nowhere/Special") is None
    assert get_tz("America/New_York") is not None


# ============================================================================ cache


def test_ttl_cache():
    now = [100.0]
    c = TTLCache(clock=lambda: now[0], max_entries=2)
    c.put("a", 1)
    assert c.get("a", 10) == 1
    now[0] += 11
    assert c.get("a", 10) is None
    assert c.get_stale("a", 60) == (1, 11.0)
    c.put("b", 2)
    c.put("c", 3)  # evicts oldest ("a")
    assert c.get_stale("a", 1000) is None and c.get("c", 10) == 3


def test_json_files(tmp_path):
    p = tmp_path / "sub" / "x.json"
    atomic_write_json(p, {"a": 1})
    assert read_json(p) == {"a": 1}
    p.write_text("{oops", encoding="utf-8")
    assert read_json(p, default="d") == "d"
    assert read_json(tmp_path / "missing.json", default=[]) == []
    assert not [f for f in p.parent.iterdir() if f.suffix == ".tmp"]


# ============================================================================ httpclient


def _response(status: int, body: bytes, url: str = "https://x.test/a", headers=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.url = url
    r.headers.update(headers or {})
    return r


def test_http_get_json_bom_and_errors(monkeypatch):
    http = HttpClient()
    monkeypatch.setattr(http.session, "get", lambda *a, **k: _response(200, b"\xef\xbb\xbf{\"ok\": true}"))
    assert http.get_json("https://x.test/a") == {"ok": True}
    monkeypatch.setattr(http.session, "get", lambda *a, **k: _response(503, b"busy"))
    with pytest.raises(HttpError) as ei:
        http.get_json("https://x.test/a")
    assert ei.value.status == 503
    monkeypatch.setattr(http.session, "get", lambda *a, **k: _response(200, b"<html>"))
    with pytest.raises(HttpError):
        http.get_json("https://x.test/a")

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http.session, "get", boom)
    with pytest.raises(HttpError) as ei:
        http.get_json("https://x.test/a")
    assert ei.value.status is None


def test_http_conditional_bytes(monkeypatch):
    http = HttpClient()
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(headers or {})
        return _response(304, b"", headers={"ETag": "\"v1\""})

    monkeypatch.setattr(http.session, "get", fake_get)
    res = http.get_bytes("https://x.test/feed.pb", etag="\"v1\"", last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
    assert res.not_modified and res.content == b""
    assert seen["If-None-Match"] == "\"v1\"" and "If-Modified-Since" in seen


def test_user_agent():
    assert "meshwx/" in default_user_agent()
    assert "ops@example.org" in default_user_agent("ops@example.org")


def test_http_download_streams_atomically(monkeypatch, tmp_path):
    http = HttpClient()

    class _Resp:
        def __init__(self, status, chunks, headers=None):
            self.status_code = status
            self._chunks = chunks
            self.headers = headers or {}
            self.url = "https://x.test/big.zip"

        def iter_content(self, chunk_size=1):
            for c in self._chunks:
                if isinstance(c, Exception):
                    raise c
                yield c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    dest = tmp_path / "cache" / "big.zip"
    monkeypatch.setattr(http.session, "get", lambda *a, **k: _Resp(200, [b"ab", b"cd"], {"ETag": "\"e1\""}))
    res = http.download("https://x.test/big.zip", dest)
    assert dest.read_bytes() == b"abcd" and res.etag == "\"e1\"" and not res.not_modified

    monkeypatch.setattr(http.session, "get", lambda *a, **k: _Resp(304, []))
    res = http.download("https://x.test/big.zip", dest, etag="\"e1\"")
    assert res.not_modified and dest.read_bytes() == b"abcd"

    monkeypatch.setattr(http.session, "get",
                        lambda *a, **k: _Resp(200, [b"xx", requests.ConnectionError("reset")]))
    with pytest.raises(HttpError):
        http.download("https://x.test/big.zip", dest)
    assert dest.read_bytes() == b"abcd"  # old file kept intact
    assert not [f for f in dest.parent.iterdir() if f.suffix == ".part"]

    monkeypatch.setattr(http.session, "get", lambda *a, **k: _Resp(200, [b"x" * 10]))
    with pytest.raises(HttpError):
        http.download("https://x.test/big.zip", dest, max_bytes=5)


def test_transit_siri_and_gtfs_params_config():
    cfg = make_config({"transit": {"enabled": True, "provider": "siri", "stops": ["15553"],
                                   "siri": {"api_key": "k", "params": {"agency": "SF", "format": "json"}}}})
    assert cfg.transit.siri.params == {"agency": "SF", "format": "json"}
    with pytest.raises(ConfigError, match="siri.api_key"):
        make_config({"transit": {"enabled": True, "provider": "siri", "stops": ["1"]}})
    cfg = make_config({"transit": {"gtfs_rt": {"params": {"key": 123}}}})
    assert cfg.transit.gtfs_rt.params == {"key": "123"}
