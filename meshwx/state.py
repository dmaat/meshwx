"""Runtime-changeable settings and small persistent app state.

Two settings can change while the service runs - the ZIP code and the
broadcast interval - via mesh admin commands (``setzip``/``setinterval``) or
``meshwx set zip|interval`` on the Pi.  Changes are persisted to
``<state_dir>/state.json`` and picked up live by a running service (it polls
the file's signature every ~30 s).

Precedence - the newest decision wins:

1. a change made while the service runs (mesh admin command, or
   ``meshwx set`` in another process - applied when the running service
   notices the new entry; setting a value back to the config.yaml value is
   recorded too, as a "reset" entry, so it also reaches the service);
2. ``--zip`` / ``--interval`` command-line flags of ``meshwx run``: they beat
   whatever state.json held when the service started, and stay in force until
   a *new* change arrives - an unrelated write to state.json does not revive
   an older saved value;
3. a saved override from state.json - **only if** the config.yaml value is
   still the one the override was based on.  If you edit config.yaml
   afterwards, your edit wins and the stale override is dropped;
4. config.yaml.

The file also stores small bits of app state under ``data`` (e.g. ids of NWS
alerts already broadcast) so restarts don't repeat them.

Concurrency: the service and ``meshwx set`` (another process) both rewrite
state.json.  Every read-modify-write holds an advisory lock on the sidecar
file ``state.json.lock`` (flock on Linux, msvcrt on Windows), re-reads the file
inside the lock and replaces it atomically, so neither side can erase the
other's change.  Plain reads need no lock (the file is replaced atomically);
the signature is taken *before* reading so a write racing the read is picked
up on the next poll.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Optional, Tuple

from .cache import atomic_write_json, read_json
from .config import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, Config, normalize_zip
from .errors import ConfigError
from .timeutil import utcnow

log = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
LOCK_FILENAME = STATE_FILENAME + ".lock"
STATE_VERSION = 1
KEY_ZIP = "zip_code"
KEY_INTERVAL = "interval_minutes"
SETTING_KEYS = (KEY_ZIP, KEY_INTERVAL)
LOCK_TIMEOUT_SECONDS = 10.0

Listener = Callable[[str, Any], None]
_Signature = Tuple[int, int, int]  # (st_ino, st_mtime_ns, st_size)


def validate_interval(minutes: Any) -> int:
    """Return a valid interval in minutes (0 = off) or raise :class:`ConfigError`."""
    if isinstance(minutes, str):
        text = minutes.strip().lower()
        if text in ("off", "none", "disable", "disabled", "0"):
            return 0
        if text.endswith("h") and text[:-1].strip().isdigit():
            minutes = int(text[:-1]) * 60
        elif text.endswith("m") and text[:-1].strip().isdigit():
            minutes = int(text[:-1])
        elif text.isdigit():
            minutes = int(text)
        else:
            raise ConfigError("interval must be minutes, e.g. 60 or 2h (got %r)" % minutes,
                              user_message="Interval: minutes, e.g. 60, 2h, off")
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ConfigError("interval must be a whole number of minutes", user_message="Interval: minutes, e.g. 60")
    if minutes != 0 and not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        raise ConfigError(
            "interval must be 0 (off) or %d-%d minutes" % (MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES),
            user_message="Interval must be %d-%d min or off" % (MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES),
        )
    return minutes


# --------------------------------------------------------------------------- cross-process lock

if os.name == "nt":  # pragma: no cover - exercised on Windows dev machines only
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> None:
        # flock (not lockf): it also excludes a second open() of the file within the same process.
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class _StateFileLock:
    """Exclusive advisory lock on ``state.json.lock`` for one read-modify-write.

    Never fails the caller: if the lock file can't be opened, or stays busy for
    :data:`LOCK_TIMEOUT_SECONDS`, it logs a warning and carries on unlocked
    (a lost update is better than a service that stops answering).
    """

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self._path = path
        self._timeout = timeout
        self._fh: Optional[IO[bytes]] = None

    def __enter__(self) -> "_StateFileLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fh: IO[bytes] = open(self._path, "a+b")
            except PermissionError:  # created by another user: a read-only handle still locks
                fh = open(self._path, "rb")
        except OSError as exc:
            log.warning("cannot open %s (%s); writing state.json without a lock", self._path, exc)
            return self
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                _try_lock(fh)
                self._fh = fh
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("%s stayed locked for %.0f s; writing state.json without the lock",
                                self._path, self._timeout)
                    fh.close()
                    return self
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError:
            pass
        finally:
            fh.close()


def _identity(entry: Any) -> Optional[str]:
    """Comparable fingerprint of one override entry (detects new/changed entries on reload)."""
    if entry is None:
        return None
    return json.dumps(entry, sort_keys=True, default=str)


# --------------------------------------------------------------------------- settings


class RuntimeSettings:
    """Thread-safe effective values for the runtime-changeable settings."""

    def __init__(
        self,
        cfg: Config,
        state_dir: Optional[Path] = None,
        *,
        cli_zip: Optional[str] = None,
        cli_interval: Optional[Any] = None,
    ) -> None:
        self._cfg = cfg
        self._dir = Path(state_dir) if state_dir is not None else cfg.state_path()
        self._path = self._dir / STATE_FILENAME
        self._lock_path = self._dir / LOCK_FILENAME
        self._lock = threading.RLock()
        self._listeners: List[Listener] = []
        self._signature: Optional[_Signature] = None
        self._doc: Dict[str, Any] = {"version": STATE_VERSION, "overrides": {}, "data": {}}
        self._applied: Dict[str, Optional[str]] = {k: None for k in SETTING_KEYS}
        self._values: Dict[str, Any] = {
            KEY_ZIP: cfg.location.zip_code,
            KEY_INTERVAL: cfg.broadcast.interval_minutes,
        }
        self._sources: Dict[str, str] = {KEY_ZIP: "config", KEY_INTERVAL: "config"}
        with self._lock:
            _changed, dirty = self._load(initial=True)
        if dirty:  # drop stale/invalid saved overrides from the file
            self._mutate(lambda: None)
        if cli_zip:
            self._values[KEY_ZIP] = normalize_zip(cli_zip, cfg.location.country)
            self._sources[KEY_ZIP] = "command line"
        if cli_interval is not None:
            self._values[KEY_INTERVAL] = validate_interval(cli_interval)
            self._sources[KEY_INTERVAL] = "command line"

    # ------------------------------------------------------------------ public read API
    @property
    def path(self) -> Path:
        return self._path

    @property
    def zip_code(self) -> str:
        with self._lock:
            return self._values[KEY_ZIP]

    @property
    def interval_minutes(self) -> int:
        with self._lock:
            return self._values[KEY_INTERVAL]

    @property
    def zip_is_override(self) -> bool:
        """True when the ZIP differs from config.yaml (so a config lat/lon must be ignored)."""
        with self._lock:
            return self._sources[KEY_ZIP] != "config"

    def source(self, key: str) -> str:
        with self._lock:
            return self._sources[key]

    def describe(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: {"value": self._values[k], "source": self._sources[k]} for k in SETTING_KEYS}

    # ------------------------------------------------------------------ changes
    def add_listener(self, callback: Listener) -> None:
        """``callback(key, new_value)`` runs (outside the locks) after a value changes."""
        with self._lock:
            self._listeners.append(callback)

    def set_zip(self, value: Any, set_by: str = "local") -> str:
        z = normalize_zip(value, self._cfg.location.country)
        self._set(KEY_ZIP, z, self._cfg.location.zip_code, set_by)
        return z

    def set_interval(self, minutes: Any, set_by: str = "local") -> int:
        m = validate_interval(minutes)
        self._set(KEY_INTERVAL, m, self._cfg.broadcast.interval_minutes, set_by)
        return m

    def refresh(self) -> List[str]:
        """Re-read state.json if another process changed it; return the setting keys that changed."""
        with self._lock:
            sig = self._stat()
            if sig == self._signature:
                return []
            changed, _dirty = self._load(initial=False)
        self._announce_external(changed)
        return changed

    # ------------------------------------------------------------------ misc persistent data
    def get_data(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._doc.setdefault("data", {}).get(key, default)

    def set_data(self, key: str, value: Any) -> None:
        def change() -> None:
            self._doc.setdefault("data", {})[key] = value

        self._announce_external(self._mutate(change))

    # ------------------------------------------------------------------ internals
    def _config_value(self, key: str) -> Any:
        return self._cfg.location.zip_code if key == KEY_ZIP else self._cfg.broadcast.interval_minutes

    def _stat(self) -> Optional[_Signature]:
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _set(self, key: str, value: Any, base: Any, set_by: str) -> None:
        before: Dict[str, Any] = {}

        def change() -> None:
            before["old"] = self._values[key]
            entry: Dict[str, Any] = {"base": base, "set_by": set_by,
                                     "set_at": utcnow().isoformat(timespec="microseconds")}
            if value == base:
                entry["reset"] = True  # an explicit "back to config.yaml" that other processes can see
                self._sources[key] = "config"
            else:
                entry["value"] = value
                self._sources[key] = "set by %s" % set_by
            self._doc.setdefault("overrides", {})[key] = entry
            self._applied[key] = _identity(entry)
            self._values[key] = value

        external = self._mutate(change)
        self._announce_external([k for k in external if k != key])
        old = before.get("old")
        log.info("runtime setting %s: %r -> %r (by %s)", key, old, value, set_by)
        if old != value:
            self._notify(key, value)

    def _mutate(self, change: Callable[[], None]) -> List[str]:
        """Locked read-modify-write: this object's lock, then the cross-process file lock.

        Re-reads the file inside the lock (so another process's change is merged,
        never overwritten), applies ``change`` and writes atomically.  Returns the
        setting keys another process changed (the caller announces them).
        """
        with self._lock:
            with _StateFileLock(self._lock_path):
                external, _dirty = self._load(initial=False)
                change()
                self._save()
        return external

    def _load(self, initial: bool) -> Tuple[List[str], bool]:
        """Read state.json and apply override entries that are new or changed (lock held).

        Returns ``(changed_keys, dirty)``; ``dirty`` means the file holds stale or
        invalid overrides that the next write should drop.
        """
        sig = self._stat()  # BEFORE reading: a write racing this read changes the signature again
        doc = read_json(self._path, default=None)
        self._signature = sig
        if not isinstance(doc, dict):
            if sig is not None:  # present but unreadable/corrupt: keep what we have, rewrite on next save
                log.warning("state file %s is not a JSON object; ignoring it", self._path)
                return [], False
            doc = {}  # missing (deleted): no overrides
        overrides = doc.get("overrides")
        data = doc.get("data")
        doc["overrides"] = overrides if isinstance(overrides, dict) else {}
        doc["data"] = data if isinstance(data, dict) else {}
        self._doc = doc
        before = dict(self._values)
        dirty = False
        for key in SETTING_KEYS:
            entry = doc["overrides"].get(key)
            ident = _identity(entry)
            if not initial and ident == self._applied.get(key):
                continue  # unchanged since we last looked: whatever is in effect (e.g. a --flag) stays
            self._applied[key] = ident
            if entry is None:
                if self._sources[key].startswith("set by"):  # the override we followed was removed
                    self._values[key] = self._config_value(key)
                    self._sources[key] = "config"
                continue
            value = self._entry_value(key, entry)
            if value is _INVALID:
                dirty = True
                continue
            self._values[key] = value
            self._sources[key] = "config" if entry.get("reset") else "set by %s" % entry.get("set_by", "unknown")
        changed = [k for k in SETTING_KEYS if self._values[k] != before[k]]
        return changed, dirty

    def _entry_value(self, key: str, entry: Any, quiet: bool = False) -> Any:
        """The value an override entry stands for, or ``_INVALID`` (logged) if it must be ignored."""
        warn = (lambda *a: None) if quiet else log.warning
        if not isinstance(entry, dict):
            warn("ignoring malformed saved %s override", key)
            return _INVALID
        if entry.get("base") != self._config_value(key):
            if not entry.get("reset") and not quiet:
                log.info("ignoring saved %s override %r: config.yaml changed since it was set (now %r)",
                         key, entry.get("value"), self._config_value(key))
            return _INVALID
        if entry.get("reset"):
            return self._config_value(key)
        if "value" not in entry:
            warn("ignoring saved %s override without a value", key)
            return _INVALID
        try:
            if key == KEY_ZIP:
                return normalize_zip(entry["value"], self._cfg.location.country)
            return validate_interval(entry["value"])
        except ConfigError as exc:
            warn("ignoring invalid saved %s override: %s", key, exc)
            return _INVALID

    def _save(self) -> None:
        """Write the document atomically (both locks held), dropping stale/invalid overrides."""
        overrides = self._doc.setdefault("overrides", {})
        for key in list(overrides):
            if key not in SETTING_KEYS or self._entry_value(key, overrides[key], quiet=True) is _INVALID:
                overrides.pop(key, None)
                if key in self._applied:
                    self._applied[key] = None
        self._doc["version"] = STATE_VERSION
        try:
            atomic_write_json(self._path, self._doc)
        except OSError as exc:
            log.error("cannot write state file %s: %s", self._path, exc)
            return
        self._signature = self._stat()  # still under the file lock: nobody else wrote in between

    def _announce_external(self, changed: List[str]) -> None:
        for key in changed:
            log.info("runtime setting %s changed externally -> %r", key, self._values[key])
            self._notify(key, self._values[key])

    def _notify(self, key: str, value: Any) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(key, value)
            except Exception:  # noqa: BLE001 - a listener must not break the setter
                log.exception("settings listener failed")


class _Invalid:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<invalid override>"


_INVALID: Any = _Invalid()
