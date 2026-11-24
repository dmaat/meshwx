"""Radio link interface (real Meshtastic node, or a console stand-in for dry runs)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..models import IncomingMessage, OutgoingMessage

MessageHandler = Callable[[IncomingMessage], None]
ConnectionHandler = Callable[[bool], None]


class MeshLink(ABC):
    """A connection to a Meshtastic node.

    Threading contract:

    * :meth:`start` returns immediately; connecting and *reconnecting* happen
      on the link's own background thread, forever, until :meth:`stop`.
    * ``on_message`` is invoked on a library thread.  It must return quickly
      (the app just enqueues the message).  Messages sent by our own node are
      never delivered.  Only TEXT_MESSAGE_APP packets are delivered.
    * ``on_connection`` is called with True/False when the link goes up/down.
    * :meth:`send` sends exactly ONE packet and blocks until it is handed to
      the radio.  It raises :class:`meshwx.errors.MeshLinkError` when not
      connected or the send fails.  It must reject text over the size limit
      (raise ``ValueError``) rather than let the library raise mid-send.
    """

    def __init__(self) -> None:
        self.on_message: Optional[MessageHandler] = None
        self.on_connection: Optional[ConnectionHandler] = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def wait_connected(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds for the link to be up; return is_connected()."""

    @abstractmethod
    def send(self, msg: OutgoingMessage) -> None: ...

    @abstractmethod
    def my_node_num(self) -> Optional[int]: ...

    def node_name(self, num: int) -> Optional[str]:
        """Short/long name of a node if known (best effort)."""
        return None

    def describe(self) -> str:
        """Human readable description, e.g. 'serial /dev/ttyUSB0 (!a1b2c3d4 "WX Bot")'."""
        return self.__class__.__name__
