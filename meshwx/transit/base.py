"""Transit provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Sequence

from ..models import StopArrivals, TransitStop


class TransitProvider(ABC):
    """Returns upcoming arrivals for configured stops.

    Implementations:

    * must use the injected :class:`meshwx.httpclient.HttpClient`;
    * return one :class:`StopArrivals` per requested stop, in the same order;
      a failure for a single stop sets ``StopArrivals.error`` instead of
      raising; raise :class:`meshwx.errors.TransitError` only when nothing
      could be fetched at all;
    * apply ``stop.routes`` / ``stop.direction_id`` filters, drop arrivals
      that are in the past (``time < now - 60s``) or beyond ``horizon_minutes``,
      drop cancelled trips / skipped stops, and sort by time;
    * must be thread-safe.
    """

    name: str = ""

    @abstractmethod
    def get_arrivals(
        self,
        stops: Sequence[TransitStop],
        now: datetime,
        horizon_minutes: int,
    ) -> List[StopArrivals]:
        """Arrivals between ``now`` and ``now + horizon_minutes`` for each stop."""
