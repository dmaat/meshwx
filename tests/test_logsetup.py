"""Log output never carries API keys from request URLs (meshwx.logsetup)."""

from __future__ import annotations

import contextlib
import io
import logging
from typing import Iterator

import pytest

from meshwx import logsetup
from meshwx.logsetup import configure_logging, redact

KEY = "FAKEKEY123"


@pytest.mark.parametrize("text, expected", [
    ("/transit/StopMonitoring?api_key=Ab12-cd34&stopcode=15553",
     "/transit/StopMonitoring?api_key=<redacted>&stopcode=15553"),
    ("https://bustime.mta.info/api/siri/stop-monitoring.json?key=K123&MonitoringRef=308209",
     "https://bustime.mta.info/api/siri/stop-monitoring.json?key=<redacted>&MonitoringRef=308209"),
    ("/api/where/arrivals-and-departures-for-stop/1_10001.json?key=TEST&minutesBefore=0&minutesAfter=75",
     "/api/where/arrivals-and-departures-for-stop/1_10001.json?key=<redacted>&minutesBefore=0&minutesAfter=75"),
    ("/ws/V2/arrivals?locIDs=8989&appID=0123ABCD", "/ws/V2/arrivals?locIDs=8989&appID=<redacted>"),
    ("/feed?x-api-key=s3cr3t;access_token=t0k", "/feed?x-api-key=<redacted>;access_token=<redacted>"),
    ("url: /v2/rest/stops?apikey=TL-1 (Caused by ReadTimeout)",
     "url: /v2/rest/stops?apikey=<redacted> (Caused by ReadTimeout)"),
    ("'/stops?apiKey=abc'", "'/stops?apiKey=<redacted>'"),
    # ODPT (Japan) names its key "acl:consumerKey"; requests percent-encodes the colon
    ("/api/v4/gtfs/realtime/x?acl%3AconsumerKey=S3CR3T", "/api/v4/gtfs/realtime/x?acl%3AconsumerKey=<redacted>"),
    ("/api/v4/gtfs/realtime/x?acl:consumerKey=S3CR3T", "/api/v4/gtfs/realtime/x?acl:consumerKey=<redacted>"),
])
def test_redact_replaces_credential_values(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "/predictions?filter%5Bstop%5D=place-pktrm&include=route%2Ctrip&page%5Blimit%5D=10",
    "/transit/StopMonitoring?agency=SF&format=json&stopcode=15553",
    "api_key=abc without a URL query",            # no ? & or ; in front: not a query parameter
    "/feed?key=&stop=1",                           # empty value: nothing to hide
    "key rotated; see https://example.test/docs",
])
def test_redact_leaves_other_text_alone(text):
    assert redact(text) == text


@contextlib.contextmanager
def configured_logging(level: str = "INFO") -> Iterator[io.StringIO]:
    """configure_logging() with its handler writing to a StringIO.

    The previous root handlers and levels are restored before the test phase ends, so
    pytest's own per-phase log handlers are left exactly as pytest expects them.
    """
    root = logging.getLogger()
    handlers, root_level = root.handlers[:], root.level
    lib_levels = {name: logging.getLogger(name).level for name in logsetup._NOISY}
    try:
        configure_logging(level)
        [handler] = root.handlers
        stream = io.StringIO()
        handler.setStream(stream)
        yield stream
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in handlers:
            root.addHandler(h)
        root.setLevel(root_level)
        for name, lvl in lib_levels.items():
            logging.getLogger(name).setLevel(lvl)


def test_urllib3_retry_warning_is_redacted():
    with configured_logging() as stream:
        # urllib3.connectionpool's own format string for a retried request
        logging.getLogger("urllib3.connectionpool").warning(
            "Retrying (%r) after connection broken by '%r': %s", "Retry(total=1)", "NewConnectionError()",
            "/transit/StopMonitoring?api_key=%s&stopcode=15553" % KEY)
    out = stream.getvalue()
    assert KEY not in out
    assert "/transit/StopMonitoring?api_key=<redacted>&stopcode=15553" in out


def test_debug_library_request_lines_are_redacted(monkeypatch):
    monkeypatch.setenv("MESHWX_DEBUG_LIBS", "1")
    with configured_logging() as stream:
        # urllib3.connectionpool's per-request DEBUG line
        logging.getLogger("urllib3.connectionpool").debug(
            '%s://%s:%s "%s %s %s" %s %s', "https", "bustime.mta.info", 443, "GET",
            "/api/siri/stop-monitoring.json?key=%s&MonitoringRef=308209" % KEY, "HTTP/1.1", 200, 512)
    out = stream.getvalue()
    assert KEY not in out
    assert '"GET /api/siri/stop-monitoring.json?key=<redacted>&MonitoringRef=308209 HTTP/1.1" 200' in out


def test_exception_tracebacks_are_redacted():
    with configured_logging() as stream:
        try:
            raise ConnectionError("Max retries exceeded with url: /stops?apikey=%s (Caused by timeout)" % KEY)
        except ConnectionError:
            logging.getLogger("meshwx.transit").exception("fetch failed")
    out = stream.getvalue()
    assert KEY not in out
    assert "fetch failed" in out and "apikey=<redacted> (Caused by timeout)" in out
