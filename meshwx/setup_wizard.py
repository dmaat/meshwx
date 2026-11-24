"""Interactive ``meshwx setup``: a few questions, then a fully commented config file.

The file is the bundled template (``meshwx/data/config.example.yaml``) with
only the answered values replaced (:func:`set_template_value`), so every
comment and every other default stays as documented, under a one-line
:data:`HEADER`.  Transit is not set up here: the user edits the ``transit:``
section afterwards.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, List, Optional, Sequence, Tuple, TypeVar

from .config import Config, config_from_dict, load_config, load_yaml_text, normalize_node_id, normalize_zip, validate
from .errors import ConfigError, GeocodeError
from .geocode import FAILED_MESSAGE
from .state import validate_interval

TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "config.example.yaml"
#: First line of every file the wizard writes.  It also keeps the file from ever
#: being byte-identical to the untouched template, which deploy/install.sh
#: treats as "not set up yet" (and then does not start the service).
HEADER = '# Created with "meshwx setup". Edit as you like: the comments below explain every setting.\n'
# Where deploy/install.sh puts the service's config and the CLI (/usr/local/bin/meshwx
# runs the latter as the "meshwx" user, which may read but not write the config).
SERVICE_CONFIG = "/etc/meshwx/config.yaml"
SERVICE_CLI = "/opt/meshwx/venv/bin/meshwx"

PortLister = Callable[[], Sequence[Tuple[str, str, bool]]]
T = TypeVar("T")

_KEY_LINE = re.compile(r"(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>(?:[ \t].*)?)")


@dataclass
class Answers:
    """The settings the wizard asks for."""

    zip_code: str
    units: str = "imperial"
    serial_port: str = ""  # "" = auto-detect
    channel_index: int = 0
    interval_minutes: int = 180
    admins: List[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Config) -> "Answers":
        return cls(cfg.location.zip_code, cfg.display.units, cfg.meshtastic.serial_port,
                   cfg.broadcast.channel_index, cfg.broadcast.interval_minutes, list(cfg.commands.admins))

    def template_values(self) -> List[Tuple[str, str, str]]:
        """``(section, key, YAML value)`` for every setting the wizard writes."""
        return [
            ("location", "zip_code", json.dumps(self.zip_code)),
            ("display", "units", self.units),
            ("meshtastic", "serial_port", json.dumps(self.serial_port)),
            ("broadcast", "channel_index", str(self.channel_index)),
            ("broadcast", "interval_minutes", str(self.interval_minutes)),
            ("commands", "admins", json.dumps(self.admins)),
        ]


def run_wizard(dest: Path, *, input_fn: Callable[[str], str] = input, print_fn: Callable[..., Any] = print,
               port_lister: Optional[PortLister] = None, geocoder: Optional[Any] = None) -> int:
    """Ask the questions and write the config file ``dest``.

    ``port_lister`` returns ``(device, description, likely_meshtastic)`` tuples;
    ``geocoder`` (a :class:`~meshwx.geocode.ZipGeocoder`) checks the ZIP and
    shows its place name.  Returns 0 when the file was written, 1 otherwise
    (also, before any question, when ``dest`` cannot be written).
    """
    dest = Path(dest)
    problem = _unwritable(dest)
    if problem:
        print_fn("Error: cannot write %s: %s." % (dest, problem))
        for line in _write_hint(dest):
            print_fn(line)
        return 1
    ui = _Prompter(input_fn, print_fn)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    try:
        print_fn("meshwx setup: a few questions to create %s." % dest)
        print_fn("Press Enter to keep the value in [brackets]. Everything can be edited in the file later.")
        defaults: Optional[Config] = None
        if dest.exists():
            if not ui.confirm("%s exists. Replace it? Only these answers are kept; other settings go back "
                              "to the template defaults" % dest):
                print_fn("Nothing written.")
                return 1
            defaults = _load_existing(dest, print_fn)
        answers = _ask_all(ui, defaults or config_from_dict(load_yaml_text(template)), defaults is not None,
                           port_lister, geocoder)
    except EOFError:
        print_fn("")
        print_fn("Setup aborted; nothing written.")
        return 1

    text = HEADER + build_config_text(template, answers)
    try:
        cfg = _validated(text, answers)
    except ConfigError as exc:
        print_fn("Error: the new configuration is not valid (%s); nothing written." % exc)
        return 1
    for note in validate(cfg):
        print_fn("Note: %s" % note)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        print_fn("Error: cannot write %s: %s" % (dest, exc))
        return 1
    _print_next_steps(print_fn, dest)
    return 0


def build_config_text(template: str, answers: Answers) -> str:
    """The template with the answered values filled in."""
    text = template
    for section, key, value in answers.template_values():
        text = set_template_value(text, section, key, value)
    return text


def _validated(text: str, answers: Answers) -> Config:
    """Load ``text`` like the service will; :class:`ConfigError` unless it is valid and holds ``answers``."""
    cfg = config_from_dict(load_yaml_text(text))
    if Answers.from_config(cfg) != answers:
        raise ConfigError("the config template does not hold the answers")
    return cfg


def set_template_value(text: str, section: str, key: str, value: str) -> str:
    """Replace the YAML value of ``section.key`` in ``text``; everything else stays byte for byte.

    ``value`` must already be YAML (e.g. ``'"02108"'``).  Only a direct child
    of the top-level ``section`` is changed; a trailing ``# comment`` keeps its
    column when there is room.  Raises ``KeyError`` when the key is missing.
    """
    lines = text.splitlines(keepends=True)
    in_section = False
    child_indent: Optional[int] = None
    for i, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.fullmatch(content)
        indent = len(content) - len(content.lstrip(" "))
        if indent == 0:
            if in_section:
                break
            in_section = m is not None and m.group("key") == section
            continue
        if not in_section:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and m is not None and m.group("key") == key:
            lines[i] = _with_value(m, value) + line[len(content):]
            return "".join(lines)
    raise KeyError("%s.%s is not in the config template" % (section, key))


def _with_value(m: "re.Match[str]", value: str) -> str:
    head = "%s%s: %s" % (m.group("indent"), m.group("key"), value)
    rest = m.group("rest")
    pos = _comment_start(rest)
    if pos < 0:
        return head
    column = m.start("rest") + pos
    return head + " " * max(1, column - len(head)) + rest[pos:]


def _comment_start(rest: str) -> int:
    """Index of the ``#`` that starts a YAML comment in ``rest`` (quotes respected), or -1."""
    quote = ""
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1  # skip the escaped character
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return i
        i += 1
    return -1


# =========================================================================== questions


class _Prompter:
    """Question/answer loop over the injected input and print functions."""

    def __init__(self, input_fn: Callable[[str], str], print_fn: Callable[..., Any]) -> None:
        self._input = input_fn
        self.say = print_fn

    def ask(self, question: str, parse: Callable[[str], T], default: Optional[str] = None,
            shown: Optional[str] = None) -> T:
        """Ask until ``parse`` accepts the answer (it raises ``ValueError`` with the reason).

        Enter takes ``default`` (displayed as ``shown`` if given); without a
        default an answer is required.
        """
        if default is None:
            prompt = "%s: " % question
        else:
            prompt = "%s [%s]: " % (question, default if shown is None else shown)
        while True:
            answer = self._input(prompt).strip()
            if not answer:
                if default is None:
                    self.say("  Please enter a value.")
                    continue
                answer = default
            try:
                return parse(answer)
            except ValueError as exc:
                self.say("  %s" % exc)

    def confirm(self, question: str) -> bool:
        """Yes/no question; Enter means no."""
        def parse(answer: str) -> bool:
            if answer.lower() in ("y", "yes"):
                return True
            if answer.lower() in ("n", "no"):
                return False
            raise ValueError("Please answer y or n.")

        return self.ask(question, parse, default="n", shown="y/N")


def _ask_all(ui: _Prompter, defaults: Config, existing: bool, port_lister: Optional[PortLister],
             geocoder: Optional[Any]) -> Answers:
    """Every question, in order; ``defaults`` come from the existing file (``existing``) or the template."""
    d = Answers.from_config(defaults)
    country = defaults.location.country
    zip_code = ui.ask("US ZIP code for the weather", _zip_parser(ui, geocoder, country),
                      default=d.zip_code if existing and d.zip_code else None)
    units = ui.ask("Units: imperial (F, mph) or metric (C, km/h)", _parse_units, default=d.units)
    port = _ask_port(ui, port_lister, d.serial_port)
    ui.say("Broadcast channel: 0 is the primary channel (often the public LongFast). A secondary channel, "
           "e.g. 1 named \"Weather\" on your node and the listeners' apps, is kinder to everyone else.")
    channel = ui.ask("Channel index for broadcasts (0-7)", _parse_channel, default=str(d.channel_index))
    interval = ui.ask("Broadcast every how many minutes (15-10080, e.g. 60 or 2h; off = no schedule)",
                      _parse_interval, default=str(d.interval_minutes))
    ui.say("Admins may change the ZIP or interval and trigger broadcasts over the mesh. A node id looks "
           "like !a1b2c3d4 (node details in the Meshtastic app).")
    admins = ui.ask("Admin node id(s), comma-separated", _parse_admins, default=", ".join(d.admins) or "none")
    return Answers(zip_code, units, port, channel, interval, admins)


def _zip_parser(ui: _Prompter, geocoder: Optional[Any], country: str) -> Callable[[str], str]:
    def parse(answer: str) -> str:
        try:
            code = normalize_zip(answer, country)
        except ConfigError as exc:
            raise ValueError(exc.user_message) from exc
        if geocoder is None:
            return code
        try:
            place = geocoder.lookup(code, country)
        except GeocodeError as exc:
            if exc.user_message != FAILED_MESSAGE:
                raise ValueError(exc.user_message) from exc  # "ZIP 12345 not found"
            ui.say("  Could not look up ZIP %s right now (offline?); using it anyway." % code)
            return code
        ui.say("  -> %s" % place.name)
        return code

    return parse


def _ask_port(ui: _Prompter, port_lister: Optional[PortLister], default: str) -> str:
    ports: List[Tuple[str, str, bool]] = []
    if port_lister is not None:
        try:
            ports = list(port_lister())
        except Exception as exc:  # noqa: BLE001 - the list is only a convenience
            ui.say("(Could not list serial ports: %s)" % exc)
        else:
            ui.say("Serial ports found:" if ports else "No serial ports found (plug the node in to see it listed).")
        for i, (device, description, likely) in enumerate(ports, 1):
            hint = "  <- looks like a Meshtastic node" if likely else ""
            ui.say("  %d) %s  %s%s" % (i, device, description, hint))

    def parse(answer: str) -> str:
        if answer.lower() in ("", "auto", "auto-detect"):
            return ""
        if answer.isdigit() and ports:
            if not 1 <= int(answer) <= len(ports):
                raise ValueError("Pick 1-%d, type a device path, or press Enter for auto-detect." % len(ports))
            return ports[int(answer) - 1][0]
        return answer

    question = "Serial port of the node (%sdevice path, or auto)" % ("number, " if ports else "")
    return ui.ask(question, parse, default=default, shown=default or "auto-detect")


def _parse_units(answer: str) -> str:
    for units in ("imperial", "metric"):
        if units.startswith(answer.lower()):
            return units
    raise ValueError("Please answer imperial or metric.")


def _parse_channel(answer: str) -> int:
    if not (answer.isdigit() and 0 <= int(answer) <= 7):
        raise ValueError("Enter a channel index from 0 to 7.")
    return int(answer)


def _parse_interval(answer: str) -> int:
    try:
        return validate_interval(answer)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc


def _parse_admins(answer: str) -> List[str]:
    if answer.lower() in ("none", "-"):
        return []
    try:
        return [normalize_node_id(part) for part in re.split(r"[\s,]+", answer) if part]
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc


def _load_existing(dest: Path, print_fn: Callable[..., Any]) -> Optional[Config]:
    """The current file as defaults for the questions; None when it is not a valid config."""
    try:
        return load_config(str(dest))
    except ConfigError as exc:
        print_fn("(The existing file is not valid, so the template defaults are offered: %s)" % exc)
        return None


def _is_service_config(dest: Path) -> bool:
    """Whether ``dest`` is the installed service's config (:data:`SERVICE_CONFIG`)."""
    service = PurePosixPath(SERVICE_CONFIG)
    return PurePosixPath(dest.as_posix()) == service or \
        PurePosixPath(Path(os.path.abspath(str(dest))).as_posix()) == service


def _unwritable(dest: Path) -> Optional[str]:
    """Why ``dest`` cannot be written, or None when it can.

    Checked before the first question so that the answers are not lost: an
    existing file must be writable, otherwise the nearest existing parent
    directory (the missing ones are created).  ``os.path`` never raises here,
    unlike ``Path.exists`` for a file in a directory the user may not enter.
    """
    path = os.path.abspath(str(dest))
    if os.path.isdir(path):
        return "it is a directory"
    if os.path.exists(path):
        return None if os.access(path, os.W_OK) else "permission denied"
    parent = os.path.dirname(path)
    while not os.path.exists(parent) and os.path.dirname(parent) != parent:
        parent = os.path.dirname(parent)
    if not os.path.isdir(parent):
        return "%s is not a directory" % parent
    if not os.access(parent, os.W_OK | os.X_OK):
        return "no permission to create files in %s" % parent
    return None


def _write_hint(dest: Path) -> List[str]:
    """What to do when ``dest`` cannot be written."""
    if _is_service_config(dest):  # e.g. "meshwx setup" through the Pi's meshwx command
        return ["To set up the installed service again, run the wizard as root, then restart meshwx:",
                "  sudo %s setup -c %s" % (SERVICE_CLI, SERVICE_CONFIG),
                "  sudo chown root:meshwx %s && sudo chmod 640 %s" % (SERVICE_CONFIG, SERVICE_CONFIG),
                "  sudo systemctl restart meshwx"]
    return ["Choose another file with -c PATH, or run the wizard as a user who may write this one.",
            "(On the Pi, the installed \"meshwx\" command runs as the meshwx user; %s runs as yourself.)"
            % SERVICE_CLI]


def _print_next_steps(print_fn: Callable[..., Any], dest: Path) -> None:
    print_fn("")
    print_fn("Wrote %s." % dest)
    print_fn("Next steps:")
    if _is_service_config(dest):
        # The meshwx command uses this config. Dry runs: as yourself with a copy (README,
        # "Testing without a radio"), never as the service user next to the service.
        print_fn("  * Transit arrivals are not set up by this wizard: edit the \"transit:\" section "
                 "(sudo nano %s)." % SERVICE_CONFIG)
        print_fn("  * Check the settings:    meshwx status")
        print_fn("  * Preview the messages:  meshwx preview")
        print_fn("  * Try it without radio, as yourself with a copy of the config (the service's "
                 "config and state stay untouched):")
        print_fn("        sudo cat %s > ~/meshwx-test.yaml" % SERVICE_CONFIG)
        print_fn("        %s run --dry-run -c ~/meshwx-test.yaml" % SERVICE_CLI)
        print_fn("  * After editing the config:  sudo systemctl restart meshwx")
        return
    path = '"%s"' % dest if " " in str(dest) else str(dest)
    print_fn("  * Transit arrivals are not set up by this wizard: edit the \"transit:\" section of %s." % path)
    print_fn("  * Check the settings:    meshwx status -c %s" % path)
    print_fn("  * Preview the messages:  meshwx preview -c %s" % path)
    print_fn("  * Try it without radio:  meshwx run --dry-run -c %s" % path)
    print_fn("  * As a service (Pi):     sudo systemctl restart meshwx")
