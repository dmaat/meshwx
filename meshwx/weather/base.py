"""Weather provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..models import Location, WeatherAlert, WeatherReport


class WeatherProvider(ABC):
    """Fetches a :class:`WeatherReport` for a :class:`Location`.

    Implementations:

    * must use the injected :class:`meshwx.httpclient.HttpClient` (never ``requests`` directly);
    * must convert everything to the units documented in :mod:`meshwx.models`;
    * raise :class:`meshwx.errors.WeatherError` (with a short ``user_message``)
      when the data cannot be produced - never return a half-empty report
      silently: missing *parts* (e.g. no current observation) are fine and
      should be noted in ``report.notes``;
    * must be thread-safe for concurrent ``fetch`` calls (keep caches behind a lock).
    """

    #: short id used in config ("nws", "openmeteo")
    name: str = ""
    #: True if :meth:`fetch_alerts` returns real data
    supports_alerts: bool = False

    @abstractmethod
    def fetch(self, location: Location, *, periods: int, include_alerts: bool) -> WeatherReport:
        """Current conditions + ``periods`` forecast periods (+ alerts if supported/requested)."""

    def fetch_alerts(self, location: Location) -> List[WeatherAlert]:
        """Active alerts only (cheap call used by the alert watcher)."""
        return []
