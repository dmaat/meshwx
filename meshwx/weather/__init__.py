"""Weather providers (NWS, Open-Meteo) and the :class:`WeatherService` facade."""

from __future__ import annotations

from ..config import Config
from ..errors import ConfigError
from ..httpclient import HttpClient
from .base import WeatherProvider
from .nws import NwsProvider
from .openmeteo import OpenMeteoProvider
from .service import WeatherService

__all__ = ["NwsProvider", "OpenMeteoProvider", "WeatherProvider", "WeatherService", "create_weather_provider"]

_PROVIDERS = {"nws": NwsProvider, "openmeteo": OpenMeteoProvider}


def create_weather_provider(name: str, http: HttpClient, cfg: Config) -> WeatherProvider:
    """Build the provider called ``name`` ("nws" | "openmeteo"); :class:`ConfigError` otherwise."""
    cls = _PROVIDERS.get((name or "").strip().lower())
    if cls is None:
        raise ConfigError("unknown weather provider %r (choose %s)" % (name, " or ".join(sorted(_PROVIDERS))))
    return cls(http, cfg)
