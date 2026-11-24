"""Exception hierarchy.

Every exception that may reach a mesh user carries ``user_message``: a short,
safe, plain-ASCII text (< 80 bytes) suitable for sending over the radio.  The
full ``str(exc)`` is for logs only.
"""

from __future__ import annotations

from typing import Optional


class MeshWxError(Exception):
    """Base class for all meshwx errors."""

    default_user_message = "Error, try again later"

    def __init__(self, message: str = "", user_message: Optional[str] = None) -> None:
        super().__init__(message or self.default_user_message)
        self.user_message = user_message or self.default_user_message


class ConfigError(MeshWxError):
    default_user_message = "Config error"


class GeocodeError(MeshWxError):
    """Unknown/invalid ZIP code, or the geocoding services are unreachable."""

    default_user_message = "ZIP not found"


class ProviderError(MeshWxError):
    """A data provider (weather or transit) failed."""


class WeatherError(ProviderError):
    default_user_message = "Weather unavailable, try later"


class TransitError(ProviderError):
    default_user_message = "Transit unavailable, try later"


class MeshLinkError(MeshWxError):
    """Radio link problem (not connected, send failed, ...)."""

    default_user_message = "Radio link error"


class NoDeviceError(MeshLinkError):
    """No Meshtastic serial device could be found/opened."""

    default_user_message = "No radio found"
