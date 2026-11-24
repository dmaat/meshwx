"""Adversarial review - area OPS (installer, CLI, setup wizard, config validation, docs).

Regression tests from the adversarial code review: each test first reproduced one
verified defect (finding id in the comments); all of them are fixed now.

    python -m pytest tests/review/test_review_OPS.py -q

The installer tests source ``deploy/install.sh`` (it only runs ``main`` when
executed, so its functions can be called one by one) in a real bash; they are
skipped when no usable bash is found (on Windows: Git for Windows' bash).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meshwx import cli, setup_wizard  # noqa: E402
from meshwx.config import config_from_dict, load_yaml_text  # noqa: E402
from meshwx.errors import ConfigError  # noqa: E402
from meshwx.models import Location, WeatherAlert  # noqa: E402
from meshwx.timeutil import utcnow  # noqa: E402

INSTALL_SH = ROOT / "deploy" / "install.sh"
README = ROOT / "README.md"


# ============================================================================ helpers


def _bash() -> str:
    """A bash that can run the installer's functions, or skip."""
    candidates: List[str] = []
    if os.name == "nt":  # Git for Windows; its launcher puts sed/tail/cmp on PATH
        for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"):
            if base:
                candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and not (os.name == "nt" and "system32" in found.lower()):  # not the WSL launcher
        candidates.append(found)
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            probe = subprocess.run([candidate, "-c", "echo ok; command -v sed tail cmp >/dev/null && echo tools"],
                                   capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.stdout.split() == ["ok", "tools"]:
            return candidate
    pytest.skip("no usable bash (with sed, tail, cmp) found")
    raise AssertionError("unreachable")


def _sh(path: Any) -> str:
    """A path as bash sees it (Git Bash understands C:/... with forward slashes)."""
    return str(path).replace("\\", "/")


def _write(path: Path, text: str) -> Path:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def _fake_venv(tmp_path: Path) -> Path:
    """A "$VENV" whose bin/python is this interpreter with the checkout importable.

    install.sh runs ``"$VENV/bin/python" -I -c ...``; ``-I`` would ignore
    PYTHONPATH, so the shim drops it (the installed venv has meshwx in
    site-packages instead).
    """
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    shim = _write(venv / "bin" / "python",
                  "#!/bin/sh\n"
                  "[ \"$1\" = \"-I\" ] && shift\n"
                  "PYTHONPATH='%s' exec '%s' \"$@\"\n" % (_sh(ROOT), _sh(sys.executable)))
    os.chmod(shim, 0o755)
    return venv


def _run_bash(bash: str, script: str, *args: Any, cwd: Path, stdin: Optional[str] = None,
              extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("MESHWX_CONFIG", None)
    env.pop("MESHWX_STATE_DIR", None)
    if extra_env:
        env.update(extra_env)
    argv = [bash] + (["-c", script, "_"] + [_sh(a) for a in args] if script else list(args))
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=180, cwd=str(cwd), env=env)


# ============================================================================ OPS-1


def test_installer_tells_why_a_config_with_a_yaml_syntax_error_is_invalid(tmp_path):
    """install.sh validate_config prints only the LAST line of the error ("tail -n 1").

    For a YAML syntax error (bad indentation, a tab, an unquoted !nodeid - the
    commonest hand-editing mistakes) the last line of PyYAML's message is the
    caret line, so the user is told "config.yaml is not valid:" followed by a
    lone "^" and the service is left stopped.
    """
    bash = _bash()
    venv = _fake_venv(tmp_path)
    good = _write(tmp_path / "good.yaml", 'location:\n  zip_code: "02108"\nbroadcast:\n  channel_index: 1\n')
    bad = _write(tmp_path / "bad.yaml",
                 'location:\n  zip_code: "02108"\nbroadcast:\n  channel_index: 1\n   interval_minutes: 60\n')
    script = ('source "$1"; VENV="$2"; CONFIG="$3"; '
              'if validate_config; then echo RESULT=valid; else echo RESULT=invalid; fi')

    control = _run_bash(bash, script, INSTALL_SH, venv, good, cwd=tmp_path)
    assert "RESULT=valid" in control.stdout, (control.stdout, control.stderr)  # the shim works

    res = _run_bash(bash, script, INSTALL_SH, venv, bad, cwd=tmp_path)
    assert "RESULT=invalid" in res.stdout, (res.stdout, res.stderr)
    shown = res.stderr
    # Observed today:  "WARNING: .../bad.yaml is not valid:\n                           ^"
    assert "mapping values are not allowed" in shown, shown


# ============================================================================ OPS-2


def _documented_to_examples() -> List[Tuple[str, str]]:
    """The shell examples for "meshwx send --to" in --help and README.md."""
    examples: List[Tuple[str, str]] = []
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    to = next(a for a in sub.choices["send"]._actions if "--to" in a.option_strings)
    for m in re.finditer(r"(--to \S+)", to.help or ""):
        examples.append(("meshwx send --help", m.group(1)))
    for m in re.finditer(r"`(--to [^`]+)`", README.read_text(encoding="utf-8")):
        examples.append(("README.md", m.group(1)))
    return examples


def test_documented_send_to_examples_survive_an_interactive_bash(tmp_path):
    """bash history expansion turns "!a1b2c3d4" (unquoted OR in double quotes) into an error.

    In the Pi user's interactive login shell, ``meshwx send --to "!a1b2c3d4" hi``
    (README CLI reference) and ``--to !a1b2c3d4`` (``meshwx send --help``) print
    "bash: !a1b2c3d4: event not found" and the command never runs.  Single
    quotes, a backslash, or simply "a1b2c3d4" (normalize_node_id accepts it) work.
    """
    bash = _bash()
    examples = _documented_to_examples()
    failures = []
    for source, example in examples:
        line = "unset HISTFILE\necho RAN meshwx send %s hello\nexit\n" % example
        res = _run_bash(bash, "", "--norc", "--noprofile", "-i", cwd=tmp_path, stdin=line,
                        extra_env={"HISTFILE": ""})
        if "event not found" in res.stderr or "RAN" not in res.stdout:
            failures.append("%s: %s -> %s" % (source, example,
                                              [ln for ln in res.stderr.splitlines() if "event" in ln]))
    assert not failures, failures


# ============================================================================ OPS-3


def test_setup_stops_before_the_questions_when_the_file_cannot_be_written(tmp_path):
    """On the Pi, "meshwx setup" (the installed wrapper) runs as the meshwx user with
    MESHWX_CONFIG=/etc/meshwx/config.yaml, a root-owned 0640 file in a 0750 root
    directory: the wizard asks "Replace it?" and all six questions, then ends with
    "Error: cannot write /etc/meshwx/config.yaml: Permission denied".  Simulated
    here with a read-only existing config.
    """
    dest = tmp_path / "config.yaml"
    dest.write_text(setup_wizard.TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(dest, stat.S_IREAD)
    prompts: List[str] = []
    printed: List[str] = []
    answers = iter(["y", "90210", "", "", "1", "60", "none"] + [""] * 20)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    try:
        if os.access(str(dest), os.W_OK):
            pytest.skip("cannot make a file read-only here (running as root?)")
        rc = setup_wizard.run_wizard(dest, input_fn=ask, print_fn=lambda *a, **k: printed.append(" ".join(map(str, a))),
                                     port_lister=lambda: [])
    finally:
        os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)
    assert rc == 1, printed
    asked = [p for p in prompts if "ZIP" in p or "Channel" in p or "Admin" in p]
    assert not asked, "questions asked although the file cannot be written: %r / %r" % (asked, printed[-3:])


# ============================================================================ OPS-4


class _StubWeather:
    """WeatherService stand-in with one active NWS alert."""

    def __init__(self, cfg: Any, settings: Any, http: Any) -> None:
        self.location = Location(42.3576, -71.0684, "Boston, MA", "02108", timezone="America/New_York")

    def home_location(self) -> Location:
        return self.location

    def location_for_zip(self, zip_code: str) -> Location:
        return self.location

    def get_alerts(self):
        from datetime import timedelta
        alert = WeatherAlert(id="urn:oid:review-ops-4", event="Tornado Warning", severity="Extreme",
                             message_type="Alert", headline="Tornado Warning issued for Suffolk County",
                             instruction="Take cover now.", ends=utcnow() + timedelta(hours=2))
        return [alert], "America/New_York"

    def alerts_supported(self) -> bool:
        return True


def test_dry_run_does_not_mark_alerts_as_sent_in_the_service_state(tmp_path, monkeypatch):
    """The setup wizard (run by install.sh for /etc/meshwx/config.yaml) tells the user
    "Try it without radio: meshwx run -c /etc/meshwx/config.yaml --dry-run".  On the
    Pi "meshwx" is the wrapper, which sets STATE_DIRECTORY=/var/lib/meshwx - the live
    service's state.  The dry run's alert watch then records every active alert in
    seen_alerts there; the service re-reads state.json within 30 s and never
    broadcasts those alerts (README: dry runs must "never mark real alerts as
    already sent").  App.build(dry_run=True) only swaps the radio for the console.
    """
    import meshwx.app as app_module
    from conftest import make_config

    service_state = tmp_path / "var-lib-meshwx"
    service_state.mkdir()
    # Relative paths: meshwx splits STATE_DIRECTORY on ":" (systemd's list syntax),
    # which would cut a Windows drive letter off when this runs on Windows.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MESHWX_STATE_DIR", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", "var-lib-meshwx")  # what the wrapper exports
    monkeypatch.setenv("CACHE_DIRECTORY", "var-cache-meshwx")
    monkeypatch.setattr(app_module, "WeatherService", _StubWeather)
    cfg = make_config({"broadcast": {"channel_index": 1}})

    app = app_module.App.build(cfg, dry_run=True)
    try:
        app._do_alert_check()  # what the dry run's alert timer does 30 s after start
    finally:
        try:
            app._http.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup of a never-started app
            pass

    state_file = service_state / "state.json"
    doc = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    assert "urn:oid:review-ops-4" not in json.dumps(doc.get("data", {})), doc


# ============================================================================ OPS-5


@pytest.mark.parametrize("yaml_text", [
    "broadcast:\n  quiet_start: 22:00\n  quiet_end: 06:00\n",
    "broadcast:\n  times: [07:00, 18:00]\n",
])
def test_unquoted_times_are_understood_or_explained(yaml_text):
    """YAML 1.1 reads 22:00 / 18:00 as base-60 integers (1320 / 1080), but 06:00 /
    07:00 as strings.  The user gets "'broadcast.quiet_start' must be a 24-hour time
    like 07:30 (got '1320')" for a value that IS a 24-hour time.  The loader
    already keeps leading-zero numbers literal for ZIP codes; times need the same
    (or at least a "put it in quotes" hint, as the ZIP error gives).
    """
    text = 'location:\n  zip_code: "02108"\n' + yaml_text
    try:
        cfg = config_from_dict(load_yaml_text(text))
    except ConfigError as exc:
        message = str(exc)
        assert "quot" in message.lower() and "1320" not in message and "1080" not in message, message
    else:
        assert cfg.broadcast.quiet_start in ("", "22:00")
        assert cfg.broadcast.times in ([], ["07:00", "18:00"])
