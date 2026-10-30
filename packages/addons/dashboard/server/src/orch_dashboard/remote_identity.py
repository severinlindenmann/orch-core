"""Who a stream of a remote device is, and the one context value only the stream adapter sets (R3, orch-core #1591;
spec D89, design note r3-session-design.md I1-I3).

* `RemoteIdentity`: copied by the in-process adapter from the HOST's frame (the host computed it from its model at
  that moment) into ``scope["state"]["remote"]``. Nothing in a device's bytes can name or change it.
* `REMOTE_STREAM`: the id of the stream a request belongs to, set by the adapter (and by nothing else) for exactly the
  time its ASGI call runs. Contextvars follow `run_in_threadpool`, so sync handlers see it. Two readers: `Security`
  (a stream token works only on its own stream) and `click_signer.Signer` (stamps `remote: true` and the stream id on
  EVERY signer call, so a handler cannot forget it and a caller cannot say otherwise).
* `route_digest`: the `view_digest` of a presence route (contract protocol 10.2c "Presence for a route")."""

import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass

from .policy import clean_path
from .routegate import LEVELS

#: the stream the running request belongs to; None outside a stream. Only the adapter sets it.
REMOTE_STREAM: ContextVar[str | None] = ContextVar("orch_remote_stream", default=None)

_LABEL = b"orch/v2/remote-route|"
_UUID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_TEMPLATE = re.compile(r"[\x21-\x7e]{1,1024}")
_PATH = re.compile(r"[\x21-\x7e]+")
_QUERY = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*")
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET")


def workspace_key(workspace_id: str) -> str | None:
    """The 32 lower-case hex digits of a workspace id (the dashes of a UUID spelling dropped), or None."""
    if not isinstance(workspace_id, str) or _UUID.fullmatch(workspace_id) is None:
        return None
    return workspace_id.replace("-", "")


@dataclass(frozen=True)
class RemoteIdentity:
    stream_id: str
    workspace_id: str
    device_id: str
    person: str
    level: str

    def __post_init__(self):
        for value in (self.stream_id, self.workspace_id, self.device_id, self.person):
            if not isinstance(value, str) or not value:
                raise ValueError("a remote identity is made of ids")
        if self.level not in LEVELS or workspace_key(self.workspace_id) is None:
            raise ValueError("a remote identity needs one of our levels and a workspace id")


def route_digest(workspace_id: str, method: str, template: str, path: str, query: str, body: bytes) -> bytes:
    """``SHA-256("orch/v2/remote-route|" || ws (16) || u8 len || method || u16 len || template || u16 len || path ||
    u16 len || query || SHA-256(body))`` (lengths big-endian); the contract's `route_digest_*` vectors check it."""
    key = workspace_key(workspace_id)
    if (
        key is None
        or method not in _METHODS
        or _TEMPLATE.fullmatch(template) is None
        or not 1 <= len(path) <= 2048
        or _PATH.fullmatch(path) is None
        or clean_path(path) is None
        or _QUERY.fullmatch(query) is None
        or len(query) > 4096
    ):
        raise ValueError("not a route")
    if method == "WEBSOCKET" and body:
        raise ValueError("a WebSocket has no body")

    def u16(text: str) -> bytes:
        raw = text.encode("ascii")
        if len(raw) > 0xFFFF:
            raise ValueError("too long")
        return len(raw).to_bytes(2, "big") + raw

    raw = (
        _LABEL
        + bytes.fromhex(key)
        + bytes([len(method)])
        + method.encode("ascii")
        + u16(template)
        + u16(path)
        + u16(query)
        + hashlib.sha256(body).digest()
    )
    return hashlib.sha256(raw).digest()
