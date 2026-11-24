"""Tests for meshwx.setup_wizard: template value replacement and the scripted question flow."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from meshwx import setup_wizard
from meshwx.config import config_from_dict, load_config, load_yaml_text
from meshwx.errors import GeocodeError
from meshwx.geocode import FAILED_MESSAGE
from meshwx.models import Location
from meshwx.setup_wizard import (HEADER, SERVICE_CLI, TEMPLATE_PATH, Answers, build_config_text, run_wizard,
                                 set_template_value)

TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
PORTS = [("/dev/ttyAMA0", "ttyAMA0", False), ("/dev/ttyUSB0", "CP2102 USB to UART Bridge Controller", True)]
CUSTOM = Answers("99501", "metric", "/dev/ttyUSB0", 2, 60, ["!a1b2c3d4", "!deadbeef"])


class Script:
    """Scripted ``input_fn`` (EOF when the answers run out) that also records prompts and printed lines."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: List[str] = []
        self.printed: List[str] = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def print(self, *args: Any) -> None:
        self.printed.append(" ".join(str(a) for a in args))

    @property
    def output(self) -> str:
        return "\n".join(self.printed)


class StubGeocoder:
    """ZipGeocoder stand-in: known places, or a GeocodeError user message per ZIP."""

    def __init__(self, places: Optional[Dict[str, str]] = None, errors: Optional[Dict[str, str]] = None) -> None:
        self.places = places or {}
        self.errors = errors or {}
        self.calls: List[str] = []

    def lookup(self, zip_code: str, country: str = "us") -> Location:
        self.calls.append(zip_code)
        if zip_code in self.errors:
            raise GeocodeError("lookup of %s failed" % zip_code, user_message=self.errors[zip_code])
        return Location(1.0, 2.0, self.places.get(zip_code, "Somewhere, ST"), zip_code)


def wizard(dest: Path, script: Script, **kwargs: Any) -> int:
    return run_wizard(dest, input_fn=script.input, print_fn=script.print, **kwargs)


def comment_lines(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


# ============================================================================ template editing


@pytest.mark.parametrize("section, key, value", CUSTOM.template_values())
def test_every_key_the_wizard_writes_is_replaced_in_place(section, key, value):
    new = set_template_value(TEMPLATE, section, key, value)
    old_lines, new_lines = TEMPLATE.splitlines(), new.splitlines()
    assert len(old_lines) == len(new_lines)
    changed = [(o, n) for o, n in zip(old_lines, new_lines) if o != n]
    assert len(changed) == 1
    old_line, new_line = changed[0]
    assert new_line.startswith("  %s: %s" % (key, value))
    if " #" in old_line:  # the trailing comment survives
        assert new_line.endswith(old_line[old_line.index(" #") + 1:])
    assert load_yaml_text(new)[section][key] == load_yaml_text("v: %s" % value)["v"]


def test_trailing_comment_keeps_its_column_when_there_is_room():
    old = next(line for line in TEMPLATE.splitlines() if line.startswith("  units:"))
    new = next(line for line in set_template_value(TEMPLATE, "display", "units", "metric").splitlines()
               if line.startswith("  units:"))
    assert new.index("#") == old.index("#") and new.startswith("  units: metric ")
    long_value = set_template_value(TEMPLATE, "display", "units", "x" * 40)
    line = next(x for x in long_value.splitlines() if x.startswith("  units:"))
    assert ("x" * 40) + " # imperial" in line


def test_only_direct_children_of_the_section_are_matched():
    with pytest.raises(KeyError):
        set_template_value(TEMPLATE, "transit", "api_key", '"k"')  # only nested deeper (transit.mbta.api_key)
    with pytest.raises(KeyError):
        set_template_value(TEMPLATE, "location", "latitude", "1.0")  # commented out in the template
    with pytest.raises(KeyError):
        set_template_value(TEMPLATE, "nowhere", "zip_code", '"1"')
    data = load_yaml_text(set_template_value(TEMPLATE, "commands", "enabled", "false"))
    assert data["commands"]["enabled"] is False
    assert data["broadcast"]["enabled"] is True and data["transit"]["enabled"] is False


def test_comment_detection_respects_quotes():
    text = "sec:\n  a: \"x # no\"   # real\n  b: 'it''s # no' # c2\n  c: 1\nother:\n  a: 2\n"
    out = set_template_value(set_template_value(text, "sec", "a", '"y"'), "sec", "b", "'z'")
    for old, new, head, comment in zip(text.splitlines()[1:3], out.splitlines()[1:3],
                                       ('  a: "y" ', "  b: 'z' "), ("# real", "# c2")):
        assert new.startswith(head) and new.endswith(comment)
        assert new.index(comment) == old.index(comment)  # the comment keeps its column
    assert load_yaml_text(out) == {"sec": {"a": "y", "b": "z", "c": 1}, "other": {"a": 2}}


def test_default_answers_reproduce_the_template_exactly():
    assert build_config_text(TEMPLATE, Answers("02108")) == TEMPLATE


def test_custom_answers_round_trip_through_the_config_loader():
    text = build_config_text(TEMPLATE, CUSTOM)
    assert Answers.from_config(config_from_dict(load_yaml_text(text))) == CUSTOM
    assert comment_lines(text) == comment_lines(TEMPLATE)


# ============================================================================ the questions


def test_wizard_writes_a_config_that_loads(tmp_path):
    dest = tmp_path / "config.yaml"
    script = Script("2108", "99501",  # invalid, then valid
                    "m",
                    "3", "2",  # out of range, then the likely Meshtastic port
                    "9", "2",
                    "5", "1h",
                    "bob", "!A1B2C3D4, deadbeef")
    geocoder = StubGeocoder({"99501": "Anchorage, AK"})
    assert wizard(dest, script, port_lister=lambda: PORTS, geocoder=geocoder) == 0
    assert Answers.from_config(load_config(str(dest))) == CUSTOM
    out = script.output
    assert "ZIP must be 5 digits" in out and "-> Anchorage, AK" in out
    assert "2) /dev/ttyUSB0  CP2102 USB to UART Bridge Controller  <- looks like a Meshtastic node" in out
    assert "Pick 1-2" in out and "Enter a channel index from 0 to 7." in out
    assert "interval must be 0 (off) or 15-10080 minutes" in out and "node id 'bob'" in out
    assert "Next steps" in out and 'edit the "transit:" section' in out
    written = dest.read_text(encoding="utf-8")
    assert comment_lines(written) == [HEADER.rstrip("\n")] + comment_lines(TEMPLATE)
    transit = TEMPLATE[TEMPLATE.index("\ntransit:"):]
    assert transit in written  # transit is never configured here


def test_enter_keeps_the_template_defaults(tmp_path):
    dest = tmp_path / "config.yaml"
    script = Script("", "02108", "", "", "", "", "")
    assert wizard(dest, script) == 0
    # Only the header line differs: install.sh never mistakes a set-up file for the
    # untouched example (which it does not start).
    assert dest.read_text(encoding="utf-8") == HEADER + TEMPLATE
    assert script.prompts[0] == "US ZIP code for the weather: "  # no default ZIP for a new file
    assert "Please enter a value." in script.output
    assert script.prompts[3].endswith("[auto-detect]: ") and script.prompts[-1].endswith("[none]: ")
    assert "Note: broadcasting on channel 0" in script.output


def test_zip_lookup_not_found_is_asked_again_offline_is_accepted(tmp_path):
    dest = tmp_path / "config.yaml"
    geocoder = StubGeocoder(errors={"12345": "ZIP 12345 not found", "02108": FAILED_MESSAGE})
    script = Script("12345", "02108", "", "", "", "", "")
    assert wizard(dest, script, geocoder=geocoder) == 0
    assert geocoder.calls == ["12345", "02108"]
    assert "ZIP 12345 not found" in script.output and "Could not look up ZIP 02108 right now" in script.output
    assert load_config(str(dest)).location.zip_code == "02108"


def test_port_answers(tmp_path):
    script = Script("02108", "", "/dev/ttyACM0", "", "", "")
    assert wizard(tmp_path / "a.yaml", script, port_lister=lambda: []) == 0
    assert "No serial ports found" in script.output
    assert load_config(str(tmp_path / "a.yaml")).meshtastic.serial_port == "/dev/ttyACM0"

    def broken() -> List[Any]:
        raise OSError("no permission")

    script = Script("02108", "", "auto", "", "", "")
    assert wizard(tmp_path / "b.yaml", script, port_lister=broken) == 0
    assert "(Could not list serial ports: no permission)" in script.output
    assert "No serial ports found" not in script.output
    assert load_config(str(tmp_path / "b.yaml")).meshtastic.serial_port == ""


def test_existing_file_is_kept_unless_confirmed(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text("location: {zip_code: '10001'}\n", encoding="utf-8")
    for answer in ("n", ""):
        script = Script(answer)
        assert wizard(dest, script) == 1
        assert "Nothing written." in script.output
    assert dest.read_text(encoding="utf-8") == "location: {zip_code: '10001'}\n"


def test_existing_config_values_are_offered_as_defaults(tmp_path):
    dest = tmp_path / "config.yaml"
    previous = Answers("90210", "metric", "COM3", 3, 60, ["!a1b2c3d4"])
    dest.write_text(build_config_text(TEMPLATE, previous), encoding="utf-8")
    script = Script("y", "", "", "", "", "", "")
    assert wizard(dest, script) == 0
    assert script.prompts[1] == "US ZIP code for the weather [90210]: "
    assert Answers.from_config(load_config(str(dest))) == previous


def test_invalid_existing_file_falls_back_to_template_defaults(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text("broadcast: {channel_index: 99}\n", encoding="utf-8")
    script = Script("yes", "02108", "", "", "", "", "")
    assert wizard(dest, script) == 0
    assert "The existing file is not valid" in script.output
    assert load_config(str(dest)).broadcast.channel_index == 0


def test_eof_aborts_without_writing(tmp_path):
    dest = tmp_path / "config.yaml"
    script = Script("02108", "metric")
    assert wizard(dest, script) == 1
    assert "Setup aborted; nothing written." in script.output
    assert not dest.exists()


def test_nothing_is_written_when_the_template_does_not_hold_the_answers(tmp_path, monkeypatch):
    drifted = tmp_path / "template.yaml"  # a second zip_code line: YAML keeps the last one
    drifted.write_text(TEMPLATE.replace('  name: ""\n', '  name: ""\n  zip_code: "02108"\n'), encoding="utf-8")
    monkeypatch.setattr("meshwx.setup_wizard.TEMPLATE_PATH", drifted)
    dest = tmp_path / "config.yaml"
    script = Script("99501", "", "", "", "", "")
    assert wizard(dest, script) == 1
    assert "the config template does not hold the answers" in script.output and not dest.exists()


def test_parent_directories_are_created(tmp_path):
    dest = tmp_path / "etc" / "meshwx" / "config.yaml"
    assert wizard(dest, Script("02108", "", "", "1", "", "")) == 0
    assert load_config(str(dest)).broadcast.channel_index == 1


def test_running_the_wizard_again_keeps_one_header(tmp_path):
    dest = tmp_path / "config.yaml"
    assert wizard(dest, Script("02108", "", "", "1", "", "")) == 0
    assert wizard(dest, Script("y", "", "", "", "", "", "")) == 0
    text = dest.read_text(encoding="utf-8")
    assert text.startswith(HEADER) and text.count(HEADER) == 1
    assert load_config(str(dest)).broadcast.channel_index == 1


# ============================================================================ where it writes


def _deny_access(monkeypatch, locked: Path) -> None:
    """os.access says no for ``locked`` (directories are always writable on Windows, and root may write anything)."""
    real_access = os.access
    monkeypatch.setattr(os, "access",
                        lambda path, mode, *a, **k: False if Path(path) == locked else real_access(path, mode, *a, **k))


def test_a_read_only_file_stops_the_wizard_before_the_first_question(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text(TEMPLATE, encoding="utf-8")
    os.chmod(dest, stat.S_IREAD)
    try:
        if os.access(str(dest), os.W_OK):
            pytest.skip("cannot make a file read-only here (running as root?)")
        script = Script("y", "90210", "", "", "1", "60", "none")
        assert wizard(dest, script) == 1
    finally:
        os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)
    assert script.prompts == []
    assert script.printed[0] == "Error: cannot write %s: permission denied." % dest
    assert "Choose another file with -c PATH" in script.output and SERVICE_CLI in script.output
    assert dest.read_text(encoding="utf-8") == TEMPLATE


def test_unwritable_destinations_are_explained(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    for dest, reason in [(tmp_path, "it is a directory"),
                         (blocker / "config.yaml", "%s is not a directory" % blocker)]:
        script = Script()
        assert wizard(dest, script) == 1
        assert script.printed[0] == "Error: cannot write %s: %s." % (dest, reason) and script.prompts == []
    locked = tmp_path / "locked"
    locked.mkdir()
    _deny_access(monkeypatch, locked)
    script = Script()
    dest = locked / "new" / "config.yaml"  # the nearest existing directory counts
    assert wizard(dest, script) == 1
    assert script.printed[0] == "Error: cannot write %s: no permission to create files in %s." % (dest, locked)
    assert not (locked / "new").exists()


def test_the_service_config_gets_the_sudo_recipe_when_not_writable(tmp_path, monkeypatch):
    # "meshwx setup" through the Pi's meshwx command runs as the meshwx user, which
    # may read /etc/meshwx/config.yaml but not write it.
    service = tmp_path / "etc" / "config.yaml"
    service.parent.mkdir()
    service.write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "SERVICE_CONFIG", service.as_posix())
    _deny_access(monkeypatch, service)
    script = Script("y", "90210")
    assert wizard(service, script) == 1
    assert script.prompts == []
    assert "  sudo %s setup -c %s" % (SERVICE_CLI, service.as_posix()) in script.printed
    assert "  sudo systemctl restart meshwx" in script.printed


def test_next_steps_for_the_service_config_suggest_a_dry_run_with_a_copy(tmp_path, monkeypatch):
    # A dry run as the service user would share the running service's state.
    service = tmp_path / "etc" / "config.yaml"
    monkeypatch.setattr(setup_wizard, "SERVICE_CONFIG", service.as_posix())
    script = Script("02108", "", "", "1", "", "")
    assert wizard(service, script) == 0
    steps = script.printed[script.printed.index("Next steps:"):]
    assert "        sudo cat %s > ~/meshwx-test.yaml" % service.as_posix() in steps
    assert "        %s run --dry-run -c ~/meshwx-test.yaml" % SERVICE_CLI in steps
    assert "  * Check the settings:    meshwx status" in steps
    assert not [line for line in steps if "--dry-run" in line and service.as_posix() in line]

    other = tmp_path / "mine.yaml"
    script = Script("02108", "", "", "1", "", "")
    assert wizard(other, script) == 0
    assert "  * Try it without radio:  meshwx run --dry-run -c %s" % other in script.printed
