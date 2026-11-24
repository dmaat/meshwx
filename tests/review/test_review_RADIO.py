"""Adversarial review, area RADIO: MeshtasticLink, PacedSender, App threads/shutdown, ``meshwx send``.

Regression tests from the adversarial code review: each test first reproduced one
verified defect (finding id in the comments); all of them are fixed now.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT

# ============================================================================ RADIO-1

# Runs App.run() on the main thread of a child process (signal handlers only
# work there) and delivers SIGTERM at a random moment, many times.  tick 0
# widens the window in which the main thread is inside threading.Event.wait()
# while holding that Event's internal lock - the same window exists with the
# production 1 s tick, it is just hit less often.  A deadlocked main thread is
# reported by faulthandler (stack dump, exit code 1) instead of hanging pytest.
_SIGTERM_SCRIPT = r'''
import faulthandler, io, random, signal, sys, tempfile, threading
from datetime import timezone
sys.path.insert(0, ROOT)
from meshwx.app import App
from meshwx.config import config_from_dict
from meshwx.formatting import Formatter
from meshwx.mesh import ConsoleLink, PacedSender
from meshwx.models import Location
from meshwx.state import RuntimeSettings

class Weather:
    def alerts_supported(self): return False
    def home_location(self): return Location(42.3576, -71.0684, "Boston, MA", "02108")
    def invalidate(self): pass

class Scheduler:
    next_run_at = None
    def poll(self): return False
    def reschedule(self): pass

faulthandler.dump_traceback_later(DEADLINE, exit=True)
tmp = tempfile.mkdtemp()
cfg = config_from_dict({"location": {"zip_code": "02108"}, "state_dir": tmp, "broadcast": {"enabled": False}})
settings = RuntimeSettings(cfg, tmp)
for i in range(RUNS):
    link = ConsoleLink(200, output_stream=io.StringIO(), read_input=False)
    app = App(cfg, settings=settings, weather=Weather(), formatter=Formatter(cfg.display, 200,
              default_tz=timezone.utc), link=link, sender=PacedSender(link, 0.01), scheduler=Scheduler(),
              tick_seconds=0.0)
    timer = threading.Timer(random.uniform(0.0, 0.02), signal.raise_signal, (signal.SIGTERM,))
    timer.start()
    rc = app.run()
    timer.join()
    assert rc == 0, rc
print("all %d runs stopped cleanly" % RUNS)
'''


def test_sigterm_always_stops_the_service(tmp_path):
    script = tmp_path / "sigterm_stress.py"
    script.write_text(textwrap.dedent(_SIGTERM_SCRIPT).replace("ROOT", repr(str(ROOT)))
                      .replace("DEADLINE", "12").replace("RUNS", "30"), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), capture_output=True, text=True,
                          timeout=90)
    assert proc.returncode == 0 and "stopped cleanly" in proc.stdout, (
        "App.run() did not return after SIGTERM - main thread stuck:\n" + proc.stderr[-2500:])
