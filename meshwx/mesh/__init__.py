"""Radio side of meshwx: links (real node or console), the paced sender, port listing.

Importing this package never imports ``meshtastic``; the real link loads it
lazily on its own thread.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Collection, List, Optional, Tuple

from .base import MeshLink
from .console_link import ConsoleLink
from .meshtastic_link import MeshtasticLink
from .sender import PacedSender

if TYPE_CHECKING:
    from ..config import Config

__all__ = ["ConsoleLink", "MeshLink", "MeshtasticLink", "PacedSender", "create_link", "list_serial_ports"]

log = logging.getLogger(__name__)


def create_link(cfg: Config, *, dry_run: bool = False, port_override: Optional[str] = None) -> MeshLink:
    """The link the app should use: a :class:`ConsoleLink` for dry runs, else the real radio."""
    if dry_run:
        return ConsoleLink(cfg.meshtastic.max_message_bytes)
    return MeshtasticLink(cfg.meshtastic, port_override=port_override)


def list_serial_ports() -> List[Tuple[str, str, bool]]:
    """All serial ports as ``(device, description, likely_meshtastic)``, sorted by device.

    "Likely" uses the meshtastic library's own auto-detect rule, i.e. what an
    empty ``meshtastic.serial_port`` would pick: ports with a known Meshtastic
    USB vendor id or, when there are none, any USB port not on the library's
    blacklist (most boards use generic CP210x/CH34x USB-serial chips).
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        log.warning("pyserial is not installed; cannot list serial ports")
        return []
    whitelist, blacklist = _vendor_ids()
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    known = {p.device for p in ports if p.vid is not None and p.vid in whitelist}

    def likely(port: Any) -> bool:
        if known:
            return port.device in known
        return bool(whitelist or blacklist) and port.vid is not None and port.vid not in blacklist

    return [(p.device, p.description or "", likely(p)) for p in ports]


def _vendor_ids() -> Tuple[Collection[int], Collection[int]]:
    """The library's (whitelist, blacklist) of USB vendor ids; empty when unavailable."""
    try:
        from meshtastic.util import blacklistVids, whitelistVids
    except Exception:  # noqa: BLE001 - not installed, or a version without these lists
        log.debug("meshtastic vendor-id lists unavailable", exc_info=True)
        return (), ()
    return whitelistVids, blacklistVids
