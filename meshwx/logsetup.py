"""Logging setup (journald-friendly)."""

from __future__ import annotations

import logging
import os
import re
import sys

_NOISY = ("meshtastic", "urllib3", "bleak", "asyncio", "pubsub")

REDACTED = "<redacted>"

# URL query parameters whose values are credentials: 511.org "api_key", MTA Bus Time /
# OneBusAway / CTA "key", TriMet "appID", Transitland "apikey", ODPT "acl:consumerKey"
# (also percent-encoded), plus the usual names for tokens, secrets and signatures.
# urllib3 logs request URLs (retry warnings by default, every request with
# MESHWX_DEBUG_LIBS=1), and logs end up in bug reports.  Matching is by name, so a
# credential in a parameter with some other name is not caught.
_SECRET_PARAM = re.compile(
    r"(?i)([?&;][\w.\-%:]*?(?:key|token|secret|password|passwd|sig|signature|app_?id)=)[^&\s\"'#;<>]+"
)


def redact(text: str) -> str:
    """Replace the values of credential-like URL query parameters with ``<redacted>``."""
    return _SECRET_PARAM.sub(r"\g<1>" + REDACTED, text)


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts credentials from the finished line.

    Working on the final text covers the message, its arguments, exception tracebacks
    and stack info alike.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once (idempotent).

    Under systemd (``JOURNAL_STREAM`` set and stderr not a TTY) timestamps are
    omitted because journald adds its own.  Third-party libraries stay at
    WARNING unless ``MESHWX_DEBUG_LIBS=1`` - the meshtastic library logs every
    packet at DEBUG, which is far too chatty for an SD card.  Credential-like URL
    query parameters (key, api_key, appID, token, ...) are replaced by
    ``<redacted>`` in every line.
    """
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    under_journald = bool(os.environ.get("JOURNAL_STREAM")) and not sys.stderr.isatty()
    fmt = "%(levelname)s %(name)s: %(message)s" if under_journald else "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(lvl)

    libs_level = logging.DEBUG if os.environ.get("MESHWX_DEBUG_LIBS") == "1" else logging.WARNING
    for name in _NOISY:
        logging.getLogger(name).setLevel(libs_level)
