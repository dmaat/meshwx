"""Shared pytest helpers.

* ``FakeHttp``  - drop-in for :class:`meshwx.httpclient.HttpClient` that serves
  canned responses (usually real API captures from ``tests/fixtures``).
* ``fixture_path`` / ``load_fixture`` - read files from ``tests/fixtures``.
* ``make_config`` - build a validated Config from a partial dict.

No test may touch the network: FakeHttp raises for any unrouted URL.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Pattern, Tuple, Union
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meshwx.config import Config, config_from_dict  # noqa: E402
from meshwx.httpclient import HttpBytes, HttpError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_path(name: str) -> Path:
    p = FIXTURES / name
    if not p.exists():
        pytest.skip("fixture %s not present" % name)
    return p


def load_fixture(name: str) -> Any:
    """JSON fixtures are parsed; anything else is returned as bytes."""
    p = fixture_path(name)
    if p.suffix == ".json":
        return json.loads(p.read_text(encoding="utf-8-sig"))
    return p.read_bytes()


def full_url(url: str, params: Optional[Mapping[str, Any]] = None) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params, doseq=True)


Responder = Union[Any, Callable[[str], Any]]


class _Route:
    def __init__(self, pattern: Union[str, Pattern[str]], body: Responder, status: int, kind: str,
                 headers: Optional[Dict[str, str]]) -> None:
        self.pattern = pattern
        self.body = body
        self.status = status
        self.kind = kind  # "json" | "bytes"
        self.headers = headers or {}

    def matches(self, url: str) -> bool:
        if isinstance(self.pattern, str):
            return url.startswith(self.pattern)
        return self.pattern.search(url) is not None


class FakeHttp:
    """Routes GETs by URL prefix (str) or regex (compiled pattern), first match wins.

    ``body`` may be: a JSON-able object, a fixture file name (str ending in
    .json/.pb/.zip/...), raw bytes, an Exception instance (raised), or a
    callable ``f(full_url) -> body``.
    """

    def __init__(self) -> None:
        self.routes: List[_Route] = []
        self.calls: List[Tuple[str, Dict[str, str]]] = []

    # ------------------------------------------------------------- routing helpers
    def add_json(self, pattern: Union[str, Pattern[str]], body: Responder, status: int = 200,
                 headers: Optional[Dict[str, str]] = None) -> "FakeHttp":
        self.routes.insert(len(self.routes), _Route(pattern, body, status, "json", headers))
        return self

    def add_bytes(self, pattern: Union[str, Pattern[str]], body: Responder, status: int = 200,
                  headers: Optional[Dict[str, str]] = None) -> "FakeHttp":
        self.routes.append(_Route(pattern, body, status, "bytes", headers))
        return self

    def add_error(self, pattern: Union[str, Pattern[str]], status: Optional[int] = 503) -> "FakeHttp":
        exc = HttpError("fake failure", str(pattern), status)
        self.routes.append(_Route(pattern, exc, status or 0, "json", None))
        return self

    def calls_to(self, needle: str) -> List[str]:
        return [u for u, _ in self.calls if needle in u]

    # ------------------------------------------------------------- internals
    def _resolve(self, url: str, headers: Optional[Mapping[str, str]]) -> Tuple[_Route, Any]:
        self.calls.append((url, dict(headers or {})))
        for route in self.routes:
            if route.matches(url):
                body = route.body
                if callable(body) and not isinstance(body, (bytes, str)):
                    body = body(url)
                if isinstance(body, BaseException):
                    raise body
                if isinstance(body, str) and re.search(r"\.(json|pb|zip|txt|bin)$", body):
                    p = fixture_path(body)
                    body = p.read_bytes() if route.kind == "bytes" or p.suffix != ".json" else json.loads(
                        p.read_text(encoding="utf-8-sig"))
                if not 200 <= route.status < 300 and route.status != 304:
                    raise HttpError("fake HTTP %d for %s" % (route.status, url), url, route.status)
                return route, copy.deepcopy(body)
        raise HttpError("FakeHttp: no route for %s" % url, url, None)

    # ------------------------------------------------------------- HttpClient API
    def get_json(self, url: str, *, params: Optional[Mapping[str, Any]] = None,
                 headers: Optional[Mapping[str, str]] = None, timeout: Any = None) -> Any:
        _, body = self._resolve(full_url(url, params), headers)
        if isinstance(body, (bytes, bytearray)):
            return json.loads(bytes(body).decode("utf-8-sig"))
        return body

    def get_bytes(self, url: str, *, params: Optional[Mapping[str, Any]] = None,
                  headers: Optional[Mapping[str, str]] = None, timeout: Any = None,
                  etag: Optional[str] = None, last_modified: Optional[str] = None) -> HttpBytes:
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        route, body = self._resolve(full_url(url, params), hdrs)
        if route.status == 304:
            return HttpBytes(content=b"", status=304, etag=etag, last_modified=last_modified, not_modified=True)
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode("utf-8")
        return HttpBytes(content=bytes(body), status=route.status,
                         etag=route.headers.get("ETag"), last_modified=route.headers.get("Last-Modified"),
                         date=route.headers.get("Date"))

    def download(self, url: str, dest: Any, *, params: Optional[Mapping[str, Any]] = None,
                 headers: Optional[Mapping[str, str]] = None, timeout: Any = None,
                 etag: Optional[str] = None, last_modified: Optional[str] = None,
                 max_bytes: Optional[int] = None, chunk_size: int = 65536) -> HttpBytes:
        """Like HttpClient.download: writes the routed body to ``dest`` (untouched on 304)."""
        res = self.get_bytes(url, params=params, headers=headers, etag=etag, last_modified=last_modified)
        if res.not_modified:
            return res
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(res.content)
        return HttpBytes(content=b"", status=res.status, etag=res.etag, last_modified=res.last_modified)

    def get(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - providers should use get_json/get_bytes
        raise NotImplementedError("use get_json/get_bytes in providers")

    def close(self) -> None:
        pass


def make_config(overrides: Optional[Dict[str, Any]] = None) -> Config:
    """Validated Config from defaults + a partial dict (deep-merged)."""
    base: Dict[str, Any] = {"location": {"zip_code": "02108"}}

    def merge(dst: Dict[str, Any], src: Mapping[str, Any]) -> Dict[str, Any]:
        for k, v in src.items():
            if isinstance(v, Mapping) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)
        return dst

    return config_from_dict(merge(base, overrides or {}))


@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: no test may write state/cache files into the real home directory."""
    base = tmp_path_factory.mktemp("meshwx-state")
    monkeypatch.setenv("MESHWX_STATE_DIR", str(base))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "xdg"))
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.delenv("CACHE_DIRECTORY", raising=False)
    monkeypatch.delenv("MESHWX_CONFIG", raising=False)


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def config_factory() -> Callable[..., Config]:
    return make_config
