"""The seam between the bridge host loop and the Remote tab (Workspace & addons)."""
from __future__ import annotations
from typing import Optional, Protocol, TypedDict

class LinkStatus(TypedDict):
    state: str                 # "off" | "connecting" | "online" | "reconnecting" | "stopped" | "error"
    since: Optional[float]     # epoch seconds of the last state change
    last_error: Optional[str]  # a fixed code such as "host_taken"; never secret text
    host_online: bool

class BridgeLink(Protocol):
    def status(self) -> LinkStatus: ...
    def disconnect(self) -> None: ...   # the kill switch: stop talking to TIX; the local dashboard keeps running

class NullLink:
    def status(self) -> LinkStatus:
        return {"state": "off", "since": None, "last_error": None, "host_online": False}
    def disconnect(self) -> None:
        return None
