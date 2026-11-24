"""WMO weather interpretation codes (Open-Meteo ``weather_code``) and wind-direction helpers.

The code table follows the Open-Meteo documentation (checked 2026-11-13),
including code 97 ("heavy thunderstorm"), which newer models report.  The
texts are kept short because every byte counts on the mesh.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

WMO_CODES: Dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    97: "Heavy thunderstorm",
    99: "Thunderstorm with heavy hail",
}

UNKNOWN_CODE = "?"

#: 16-point compass, clockwise from north (index * 22.5 = degrees).
COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def describe(code: Optional[int]) -> Optional[str]:
    """Short text for a WMO weather code; None for None, ``"?"`` for unknown codes.

    Integral JSON floats (``73.0``) count as codes; anything else unparseable is unknown.
    """
    if code is None:
        return None
    try:
        number = float(code)
    except (TypeError, ValueError):
        return UNKNOWN_CODE
    if isinstance(code, bool) or not number.is_integer():
        return UNKNOWN_CODE
    return WMO_CODES.get(int(number), UNKNOWN_CODE)


def compass_point(degrees: Optional[float]) -> Optional[str]:
    """Meteorological degrees -> 16-point compass name ("NW"); None for None/NaN/infinity."""
    if degrees is None or not math.isfinite(degrees):
        return None
    return COMPASS_POINTS[int((float(degrees) % 360.0) / 22.5 + 0.5) % 16]


def compass_degrees(point: Optional[str]) -> Optional[float]:
    """16-point compass name ("NW") -> degrees; None for empty/unknown text (e.g. calm "")."""
    if not point:
        return None
    try:
        return COMPASS_POINTS.index(point.strip().upper()) * 22.5
    except ValueError:
        return None
