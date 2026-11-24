"""Shared HTTP client: one pooled ``requests.Session`` with sane timeouts.

Providers must never call ``requests`` directly; they receive an
:class:`HttpClient` so tests can substitute :class:`tests.conftest.FakeHttp`.

Retry policy: idempotent GETs are retried on connection errors and on
500/502/503/504 with exponential backoff.  429 is *not* retried (callers fall
back to cached data instead of hammering a rate-limited API) and
``Retry-After`` is ignored so a worker thread can never block for an hour.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .errors import MeshWxError

log = logging.getLogger(__name__)

Timeout = Union[float, Tuple[float, float]]

DEFAULT_TIMEOUT: Tuple[float, float] = (6.0, 25.0)  # (connect, read) seconds


class HttpError(MeshWxError):
    """Network failure or non-2xx response (after retries)."""

    default_user_message = "Network error, try later"

    def __init__(self, message: str, url: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status  # None for connection/timeout errors


@dataclass
class HttpBytes:
    content: bytes
    status: int
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False  # True on HTTP 304 (content is b"")
    date: Optional[str] = None  # server's HTTP Date header (lets callers detect a wrong local clock)


def default_user_agent(contact: str = "") -> str:
    ua = "meshwx/%s (+https://github.com/dmaat/meshwx; weather/transit mesh bot)" % __version__
    contact = (contact or "").strip()
    if contact:
        ua = "meshwx/%s (%s)" % (__version__, contact)
    return ua


class HttpClient:
    def __init__(
        self,
        user_agent: Optional[str] = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        retries: int = 2,
        backoff: float = 1.0,
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent or default_user_agent()
        self.session.headers["Accept-Encoding"] = "gzip, deflate"
        retry_kwargs: Dict[str, Any] = dict(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff,
            status_forcelist=(500, 502, 503, 504),
            raise_on_status=False,
            respect_retry_after_header=False,
        )
        try:
            retry = Retry(allowed_methods=frozenset({"GET", "HEAD"}), **retry_kwargs)
        except TypeError:  # urllib3 < 1.26
            retry = Retry(method_whitelist=frozenset({"GET", "HEAD"}), **retry_kwargs)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ core
    def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Timeout] = None,
        allow_status: Tuple[int, ...] = (),
    ) -> requests.Response:
        """GET ``url``; raise :class:`HttpError` on network errors or non-2xx.

        Status codes listed in ``allow_status`` (e.g. 304, 404) are returned
        instead of raising, so callers can handle them explicitly.
        """
        try:
            resp = self.session.get(
                url,
                params=params,
                headers=dict(headers or {}),
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise HttpError("GET %s failed: %s" % (url, exc), url) from exc
        if resp.status_code in allow_status:
            return resp
        if not 200 <= resp.status_code < 300:
            snippet = (resp.text or "")[:200].replace("\n", " ")
            raise HttpError(
                "GET %s -> HTTP %d: %s" % (resp.url, resp.status_code, snippet),
                url,
                status=resp.status_code,
            )
        return resp

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Timeout] = None,
    ) -> Any:
        """GET and decode JSON (tolerates a UTF-8 BOM, which some APIs emit)."""
        resp = self.get(url, params=params, headers=headers, timeout=timeout)
        try:
            return json.loads(resp.content.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError("GET %s: invalid JSON: %s" % (url, exc), url, resp.status_code) from exc

    def get_bytes(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Timeout] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> HttpBytes:
        """GET raw bytes, optionally as a conditional request (ETag / Last-Modified)."""
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        resp = self.get(url, params=params, headers=hdrs, timeout=timeout, allow_status=(304,))
        return HttpBytes(
            content=b"" if resp.status_code == 304 else resp.content,
            status=resp.status_code,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
            not_modified=resp.status_code == 304,
            date=resp.headers.get("Date"),
        )

    def download(
        self,
        url: str,
        dest: Union[str, Path],
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Timeout] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        max_bytes: Optional[int] = None,
        chunk_size: int = 64 * 1024,
    ) -> HttpBytes:
        """Stream a (possibly large) response body to ``dest`` without holding it in RAM.

        The file is written to a temp file next to ``dest`` and renamed into
        place only when complete, so a power cut never leaves a truncated file.
        Supports conditional requests: on HTTP 304 ``dest`` is left untouched
        and ``not_modified`` is True.  ``content`` is always ``b""``.
        """
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = self.session.get(url, params=params, headers=hdrs, timeout=timeout or self.timeout, stream=True)
        except requests.RequestException as exc:
            raise HttpError("GET %s failed: %s" % (url, exc), url) from exc
        with resp:
            got_etag = resp.headers.get("ETag")
            got_lm = resp.headers.get("Last-Modified")
            if resp.status_code == 304:
                return HttpBytes(content=b"", status=304, etag=got_etag or etag,
                                 last_modified=got_lm or last_modified, not_modified=True,
                                 date=resp.headers.get("Date"))
            if not 200 <= resp.status_code < 300:
                raise HttpError("GET %s -> HTTP %d" % (resp.url, resp.status_code), url, status=resp.status_code)
            fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=str(dest.parent))
            total = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            raise HttpError("GET %s: response larger than %d bytes" % (url, max_bytes), url,
                                            status=resp.status_code)
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, dest)
            except requests.RequestException as exc:
                _unlink_quietly(tmp)
                raise HttpError("GET %s: download interrupted: %s" % (url, exc), url, resp.status_code) from exc
            except BaseException:
                _unlink_quietly(tmp)
                raise
        log.debug("downloaded %s -> %s (%d bytes)", url, dest, total)
        return HttpBytes(content=b"", status=resp.status_code, etag=got_etag, last_modified=got_lm,
                         date=resp.headers.get("Date"))

    def close(self) -> None:
        self.session.close()


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
