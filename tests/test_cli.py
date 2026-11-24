"""Tests for meshwx.cli (offline: FakeHttp via ``cli.make_http``, fake ports/App, dry-run links)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import meshwx.cli as cli
from meshwx import __version__
from meshwx.config import load_config
from meshwx.errors import MeshLinkError, NoDeviceError
from meshwx.geocode import ZipGeocoder
from meshwx.mesh import ConsoleLink
from meshwx.models import OutgoingMessage
from meshwx.setup_wizard import TEMPLATE_PATH

from conftest import FakeHttp

ZIPPO = "https://api.zippopotam.us/"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
OM = "https://api.open-meteo.com/v1/forecast"
MBTA = "https://api-v3.mbta.com/predictions"
OPENMETEO_ONLY = "weather:\n  provider: openmeteo\n  fallback_provider: ''\n"


@pytest.fixture(autouse=True)
def log_levels(monkeypatch) -> List[str]:
    """configure_logging would replace pytest's log handlers: record the requested levels instead."""
    levels: List[str] = []
    monkeypatch.setattr(cli, "configure_logging", lambda level="INFO": levels.append(level))
    return levels


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(cli, "make_http", lambda cfg=None: fake)
    return fake


def write_config(tmp_path: Path, extra: str = "", name: str = "config.yaml") -> Path:
    """A config with ZIP 02108 whose state lives in ``tmp_path``; ``extra`` adds other sections."""
    path = tmp_path / name
    path.write_text("location:\n  zip_code: \"02108\"\nstate_dir: '%s'\n%s" % (tmp_path.as_posix(), extra),
                    encoding="utf-8")
    return path


def state(tmp_path: Path) -> Dict[str, Any]:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


# ============================================================================ basics


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "meshwx %s" % __version__


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: meshwx" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_example_config_prints_the_template(capsys):
    assert cli.main(["example-config"]) == 0
    assert capsys.readouterr().out == TEMPLATE_PATH.read_text(encoding="utf-8")


def test_config_errors_exit_2(tmp_path, capsys):
    bad = write_config(tmp_path, "broadcast:\n  channel_index: 9\n")
    assert cli.main(["status", "-c", str(bad)]) == 2
    assert capsys.readouterr().err.startswith("config error: ")
    assert cli.main(["status", "-c", str(tmp_path / "missing.yaml")]) == 2
    assert "config error: " in capsys.readouterr().err


def test_default_log_levels(tmp_path, log_levels, http, monkeypatch):
    monkeypatch.setattr(cli, "list_serial_ports", lambda: [])
    cfg = str(write_config(tmp_path))
    cli.main(["status", "-c", cfg])
    cli.main(["ports", "--log-level", "debug"])
    assert log_levels == ["WARNING", "DEBUG"]


# ============================================================================ run


@pytest.fixture
def fake_app(monkeypatch) -> Any:
    """Replace App by a fake class: ``calls`` records build() arguments, ``run_result`` is returned/raised."""

    class FakeApp:
        calls: List[Tuple[Any, Dict[str, Any]]] = []
        run_result: Any = 0

        @classmethod
        def build(cls, cfg: Any, **kwargs: Any) -> "FakeApp":
            cls.calls.append((cfg, kwargs))
            return cls()

        def run(self) -> int:
            if isinstance(self.run_result, BaseException):
                raise self.run_result
            return self.run_result

    monkeypatch.setattr(cli, "App", FakeApp)
    return FakeApp


def test_run_passes_the_options_on(tmp_path, fake_app, log_levels):
    cfg = write_config(tmp_path, "log_level: DEBUG\n")
    argv = ["run", "-c", str(cfg), "--zip", "90210", "--interval", "2h", "--port", "COM3", "--dry-run"]
    assert cli.main(argv) == 0
    loaded, kwargs = fake_app.calls[0]
    assert loaded.source_path == str(cfg)
    assert kwargs == {"dry_run": True, "port": "COM3", "cli_zip": "90210", "cli_interval": 120}
    assert log_levels == ["INFO", "DEBUG"]  # then the config's log_level

    # options before the command name; an explicit --log-level wins over the config
    fake_app.run_result = 3
    assert cli.main(["-c", str(cfg), "--log-level", "warning", "run"]) == 3
    assert fake_app.calls[1][1] == {"dry_run": False, "port": None, "cli_zip": None, "cli_interval": None}
    assert log_levels[2:] == ["WARNING"]


def test_run_interval_is_validated(tmp_path, fake_app, capsys):
    assert cli.main(["run", "-c", str(write_config(tmp_path)), "--interval", "5"]) == 2
    assert "interval must be 0 (off) or 15-10080 minutes" in capsys.readouterr().err
    assert cli.main(["run", "-c", str(write_config(tmp_path)), "--interval", "off"]) == 0
    assert fake_app.calls[0][1]["cli_interval"] == 0


def test_run_interrupt_and_errors(tmp_path, fake_app, capsys):
    cfg = str(write_config(tmp_path))
    fake_app.run_result = KeyboardInterrupt()
    assert cli.main(["run", "-c", cfg]) == 130
    fake_app.run_result = NoDeviceError("no Meshtastic device found; is it plugged in?")
    assert cli.main(["run", "-c", cfg]) == 1
    assert "error: no Meshtastic device found" in capsys.readouterr().err


# ============================================================================ status


def test_status_shows_settings_sources_and_next_broadcasts(tmp_path, capsys, http):
    cfg = write_config(tmp_path, "broadcast:\n  channel_index: 2\n  quiet_start: '22:00'\n  quiet_end: '06:00'\n")
    assert cli.main(["status", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("meshwx %s\n" % __version__)
    assert re.search(r"Config file:\s+%s" % re.escape(str(cfg)), out)
    assert re.search(r"State file:\s+%s \(not created yet\)" % re.escape(str(tmp_path / "state.json")), out)
    assert re.search(r"ZIP code:\s+02108 \(config.yaml\)", out)
    assert re.search(r"Interval:\s+180 min \(config.yaml\)", out)
    assert re.search(r"Broadcasts:\s+on channel 2: current, forecast, alerts", out)
    assert "every 180 min, aligned to the clock; quiet 22:00-06:00" in out
    assert len(re.findall(r"\d{4}-\d\d-\d\d (?:00|03|06|09|12|15|18|21):00", out)) == 3
    assert not re.search(r"\d{4}-\d\d-\d\d (?:22|23|00|01|02|03|04|05):\d\d", out)  # quiet hours skipped
    assert re.search(r"Alert watch:\s+every 5 min, Moderate or worse, on channel 2", out)
    assert re.search(r"Radio:\s+serial \(auto-detect\); max 200 bytes per packet, 8s between packets", out)


def test_status_variants(tmp_path, capsys, http):
    cfg = write_config(tmp_path, OPENMETEO_ONLY + "broadcast:\n  times: ['18:00', '07:00']\n")
    assert cli.main(["status", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "daily at 07:00, 18:00" in out
    assert "off (weather provider openmeteo has no alerts)" in out

    cfg = write_config(tmp_path, "broadcast:\n  enabled: false\n", name="off.yaml")
    assert cli.main(["status", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"Broadcasts:\s+disabled", out) and "off (broadcasts are disabled)" in out
    assert "Next broadcasts" not in out


# ============================================================================ set


def test_set_zip_geocodes_saves_and_shows_in_status(tmp_path, capsys, http):
    http.add_json(ZIPPO + "us/99501", "zippopotam_99501.json")
    cfg = str(write_config(tmp_path))
    assert cli.main(["set", "zip", "99501", "-c", cfg]) == 0
    out = capsys.readouterr().out
    assert "Home ZIP set to 99501 (Anchorage, AK)." in out
    assert "A running meshwx service applies this within ~30 s." in out
    override = state(tmp_path)["overrides"]["zip_code"]
    assert (override["value"], override["base"], override["set_by"]) == ("99501", "02108", "cli")
    assert cli.main(["status", "-c", cfg]) == 0
    assert "99501 (set by cli; config.yaml: 02108)" in capsys.readouterr().out


def test_set_zip_back_to_the_config_value(tmp_path, capsys, http):
    http.add_json(ZIPPO + "us/99501", "zippopotam_99501.json").add_json(ZIPPO + "us/02108", "zippopotam_02108.json")
    cfg = str(write_config(tmp_path))
    cli.main(["set", "zip", "99501", "-c", cfg])
    assert cli.main(["set", "zip", "02108-1234", "-c", cfg]) == 0
    assert "Home ZIP: 02108 (Boston, MA), the config.yaml value." in capsys.readouterr().out
    assert all("value" not in e for e in state(tmp_path)["overrides"].values())  # only "back to config" markers


def test_set_unknown_zip_changes_nothing(tmp_path, capsys, http):
    http.add_json(ZIPPO, {}, status=404).add_json(OM_GEO, "openmeteo_geocode_00000.json")
    assert cli.main(["set", "zip", "00000", "-c", str(write_config(tmp_path))]) == 1
    assert "ZIP 00000 not found; nothing changed" in capsys.readouterr().err
    assert not (tmp_path / "state.json").exists()


def test_set_invalid_values_exit_2(tmp_path, capsys, http):
    cfg = str(write_config(tmp_path))
    assert cli.main(["set", "zip", "123", "-c", cfg]) == 2
    assert "meshwx set zip: ZIP must be 5 digits (got '123')" in capsys.readouterr().err
    assert cli.main(["set", "interval", "5", "-c", cfg]) == 2
    assert "meshwx set interval: Interval must be 15-10080 min or off (got '5')" in capsys.readouterr().err
    assert cli.main(["set", "volume", "11", "-c", cfg]) == 2
    assert not (tmp_path / "state.json").exists() and not http.calls


def test_set_interval(tmp_path, capsys):
    cfg = str(write_config(tmp_path))
    assert cli.main(["set", "interval", "2h", "-c", cfg]) == 0
    assert "Broadcast interval set to 120 min." in capsys.readouterr().out
    assert state(tmp_path)["overrides"]["interval_minutes"]["value"] == 120
    assert cli.main(["set", "interval", "off", "-c", cfg]) == 0
    assert "Scheduled broadcasts turned off." in capsys.readouterr().out
    assert cli.main(["set", "interval", "20", "-c", cfg]) == 0
    assert "every 20 min is a lot of airtime" in capsys.readouterr().out
    assert cli.main(["set", "interval", "180", "-c", cfg]) == 0
    assert "(This is the config.yaml value.)" in capsys.readouterr().out
    assert all("value" not in e for e in state(tmp_path)["overrides"].values())  # only "back to config" markers


def test_set_interval_notes_when_the_interval_is_unused(tmp_path, capsys):
    cfg = str(write_config(tmp_path, "broadcast:\n  times: ['07:00']\n"))
    assert cli.main(["set", "interval", "60", "-c", cfg]) == 0
    assert "broadcast.times" in capsys.readouterr().out
    cfg = str(write_config(tmp_path, "broadcast:\n  enabled: false\n", name="off.yaml"))
    assert cli.main(["set", "interval", "60", "-c", cfg]) == 0
    assert "broadcast.enabled is false" in capsys.readouterr().out


def test_set_reports_a_state_file_it_cannot_write(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("location:\n  zip_code: \"02108\"\nstate_dir: '%s'\n" % (blocker / "state").as_posix(),
                   encoding="utf-8")
    assert cli.main(["set", "interval", "60", "-c", str(cfg)]) == 1
    err = capsys.readouterr().err
    assert "could not save" in err and "sudo -u meshwx" in err


# ============================================================================ preview


def test_preview_prints_every_packet_with_its_size(tmp_path, capsys, http):
    http.add_json(ZIPPO + "us/02108", "zippopotam_02108.json").add_json(OM, "openmeteo_forecast_02108.json")
    cfg = write_config(tmp_path, OPENMETEO_ONLY)
    assert cli.main(["preview", "-c", str(cfg), "--what", "current,alerts,broadcast"]) == 0
    out = capsys.readouterr().out
    assert "Weather for ZIP 02108: Boston, MA (openmeteo)" in out
    assert "== current: 1 packet ==" in out and "Boston, MA" in out
    assert "== alerts: 1 packet ==\n-- 1/1, 19 bytes --\nAlerts: US/NWS only" in out
    assert "== broadcast on channel 0:" in out
    sections = re.findall(r"^== (.+?): \d+ packets? ==$", out, re.M)
    assert sections == ["current", "alerts", "broadcast on channel 0"]
    sizes = [int(n) for n in re.findall(r"^-- \d+/\d+, (\d+) bytes --$", out, re.M)]
    assert sizes and max(sizes) <= 200


def test_preview_other_zip(tmp_path, capsys, http):
    http.add_json(ZIPPO + "us/99501", "zippopotam_99501.json").add_json(OM, "openmeteo_forecast_02108.json")
    cfg = write_config(tmp_path, OPENMETEO_ONLY)
    assert cli.main(["preview", "-c", str(cfg), "--zip", "99501", "--what", "current"]) == 0
    assert "Weather for ZIP 99501: Anchorage, AK" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists()  # --zip is not saved


def test_preview_weather_failure_exits_1(tmp_path, capsys, http):
    http.add_json(ZIPPO + "us/02108", "zippopotam_02108.json").add_error(OM, status=503)
    assert cli.main(["preview", "-c", str(write_config(tmp_path, OPENMETEO_ONLY))]) == 1
    captured = capsys.readouterr()
    assert "weather for ZIP 02108 failed" in captured.err
    assert "== broadcast on channel 0: 0 packets ==\n(nothing to send)" in captured.out


def test_preview_transit(tmp_path, capsys, http):
    http.add_json(MBTA, "mbta_predictions_sample.json")
    transit = "transit:\n  enabled: true\n  provider: mbta\n  stops:\n    - id: place-pktrm\n      label: Park St\n"
    assert cli.main(["preview", "-c", str(write_config(tmp_path, transit)), "--what", "transit"]) == 0
    out = capsys.readouterr().out
    assert "== transit: 1 packet ==" in out and "Park St: " in out
    assert http.calls_to(MBTA) and not http.calls_to(ZIPPO)  # no weather needed


def test_preview_transit_requires_it_enabled(tmp_path, capsys, http):
    assert cli.main(["preview", "-c", str(write_config(tmp_path)), "--what", "transit"]) == 1
    assert "transit is not enabled" in capsys.readouterr().err
    assert cli.main(["preview", "-c", str(write_config(tmp_path)), "--what", "radar"]) == 2


# ============================================================================ ports


def test_ports_table(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_serial_ports",
                        lambda: [("/dev/ttyACM0", "RAK4631", True), ("/dev/ttyS0", "ttyS0", False)])
    assert cli.main(["ports"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["DEVICE", "MESHTASTIC", "DESCRIPTION"]
    assert re.fullmatch(r"/dev/ttyACM0\s+likely\s+RAK4631", lines[1])
    assert re.fullmatch(r"/dev/ttyS0\s+-\s+ttyS0", lines[2])
    assert "uses the first likely port" in lines[-1]


def test_ports_none_found(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_serial_ports", lambda: [])
    assert cli.main(["ports"]) == 1
    assert "No serial ports found" in capsys.readouterr().out


# ============================================================================ send


def test_send_dry_run_broadcast(tmp_path, capsys):
    cfg = write_config(tmp_path, "broadcast:\n  channel_index: 3\n")
    assert cli.main(["send", "-c", str(cfg), "--dry-run", "hello", "mesh"]) == 0
    out = capsys.readouterr().out
    assert "[TX ch3 -> ^all] hello mesh  (10B)" in out
    assert "Sent 1 packet(s) to ^all on channel 3." in out


def test_send_dry_run_direct_message(tmp_path, capsys):
    cfg = write_config(tmp_path, "broadcast:\n  channel_index: 3\n")
    assert cli.main(["send", "-c", str(cfg), "--dry-run", "--to", "A1B2C3D4", "hi"]) == 0
    assert "[TX ch0 -> !a1b2c3d4] hi  (2B)" in capsys.readouterr().out  # DMs default to channel 0
    assert cli.main(["send", "-c", str(cfg), "--dry-run", "--to", "!a1b2c3d4", "--channel", "1", "hi"]) == 0
    assert "[TX ch1 -> !a1b2c3d4] hi" in capsys.readouterr().out


def test_send_long_text_is_split_into_numbered_packets(tmp_path, capsys):
    cfg = write_config(tmp_path, "meshtastic:\n  message_delay_seconds: 0.5\n")
    assert cli.main(["send", "-c", str(cfg), "--dry-run"] + ["word"] * 60) == 0
    out = capsys.readouterr().out
    assert out.count("[TX ch0 -> ^all]") == 2 and " 1/2  (" in out and " 2/2  (" in out


def test_send_help_shows_a_node_id_that_bash_leaves_alone(capsys):
    # In an interactive bash, "!a1b2c3d4" - unquoted or in double quotes - fails with
    # "event not found" before meshwx even runs.
    assert cli.main(["send", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())  # undo argparse's line wrapping
    assert "--to a1b2c3d4" in out
    assert not re.search(r"--to \"?!", out)


def test_send_usage_errors(tmp_path, capsys):
    cfg = str(write_config(tmp_path))
    assert cli.main(["send", "-c", cfg, "--dry-run", "--to", "bob", "hi"]) == 2
    assert cli.main(["send", "-c", cfg, "--dry-run", "--channel", "8", "hi"]) == 2
    assert cli.main(["send", "-c", cfg, "--dry-run", "  "]) == 2
    assert "nothing to send" in capsys.readouterr().err


class _DeadLink(ConsoleLink):
    """Never connects."""

    def start(self) -> None:
        pass


class _FailingLink(ConsoleLink):
    """Connects, but every send fails."""

    def send(self, msg: OutgoingMessage) -> None:
        raise MeshLinkError("radio send failed")


def test_send_without_a_connection_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "create_link", lambda cfg, port_override=None: _DeadLink(200, read_input=False))
    monkeypatch.setattr(cli, "SEND_CONNECT_TIMEOUT", 0.05)
    assert cli.main(["send", "-c", str(write_config(tmp_path)), "--port", "COM9", "hi"]) == 1
    assert "no radio connection" in capsys.readouterr().err


def test_send_failure_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "create_link", lambda cfg, port_override=None: _FailingLink(200, read_input=False))
    cfg = write_config(tmp_path, "meshtastic:\n  message_delay_seconds: 0.5\n")
    assert cli.main(["send", "-c", str(cfg), "hi"]) == 1
    assert "sent 0 of 1 packet(s)" in capsys.readouterr().err


# ============================================================================ setup


def test_setup_destination_and_helpers(tmp_path, monkeypatch, http, log_levels):
    calls: List[Tuple[Path, Dict[str, Any]]] = []
    monkeypatch.setattr(cli.setup_wizard, "run_wizard", lambda dest, **kw: calls.append((dest, kw)) or 0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["setup"]) == 0
    monkeypatch.setenv("MESHWX_CONFIG", str(tmp_path / "env.yaml"))
    assert cli.main(["setup"]) == 0
    assert cli.main(["setup", "-c", str(tmp_path / "given.yaml")]) == 0
    assert [dest for dest, _ in calls] == [Path("config.yaml"), tmp_path / "env.yaml", tmp_path / "given.yaml"]
    kwargs = calls[0][1]
    assert kwargs["port_lister"] is cli.list_serial_ports and isinstance(kwargs["geocoder"], ZipGeocoder)
    assert log_levels == ["ERROR"] * 3


def test_setup_end_to_end(tmp_path, monkeypatch, capsys, http):
    http.add_json(ZIPPO + "us/99501", "zippopotam_99501.json")
    monkeypatch.setattr(cli, "list_serial_ports", lambda: [("/dev/ttyUSB0", "CP2102", True)])
    answers = iter(["99501", "metric", "1", "2", "60", "!a1b2c3d4"])
    wizard = cli.setup_wizard.run_wizard
    monkeypatch.setattr(cli.setup_wizard, "run_wizard",
                        lambda dest, **kw: wizard(dest, input_fn=lambda prompt: next(answers), **kw))
    dest = tmp_path / "etc" / "config.yaml"
    assert cli.main(["setup", "-c", str(dest)]) == 0
    assert "-> Anchorage, AK" in capsys.readouterr().out
    cfg = load_config(str(dest))
    assert (cfg.location.zip_code, cfg.display.units, cfg.meshtastic.serial_port) == ("99501", "metric", "/dev/ttyUSB0")
    assert (cfg.broadcast.channel_index, cfg.broadcast.interval_minutes, cfg.commands.admins) == (2, 60, ["!a1b2c3d4"])
