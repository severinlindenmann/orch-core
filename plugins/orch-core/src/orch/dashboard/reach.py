"""Where a dashboard request comes from: this machine (local), or a paired device that reached the dashboard through
the remote bridge (remote, with a scope). One answer, read by every gate that cares.

The remote marker is a key of the ASGI scope, set by server-side code that hosts the bridge. It is never read from a
header, a query parameter, a cookie or a body, so nothing a client sends can create or change it. A marker that is
present but malformed is refused, never treated as local.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from orch.core.events import Actor

SCOPE_KEY = "orch.remote"  # the ASGI scope key; only the bridge host sets it
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_DEVICE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]")


class Scope(enum.IntEnum):
    """What a paired device may do, each one including the ones before it."""
    LOOK = 1  # read pages and streams
    DECIDE = 2  # the human decisions: approve, answer, request changes, verdict, move
    OPERATE = 3  # ordinary ticket and wiki edits
    TYPE = 4  # whatever makes the host run something: terminal keys, starting an agent, arming the factory


@dataclass(frozen=True)
class RemoteOrigin:
    device: str  # the paired device's id
    scope: Scope
    label: str = ""  # shown in the actor; reduced to [A-Za-z0-9 ._-], at most 40 characters
    fresh: bool = False  # the bridge host verified a fresh authenticator assertion for this very request

    def __post_init__(self):
        if self.fresh is not True and self.fresh is not False:
            raise ValueError("fresh is a bool")
        if not isinstance(self.device, str) or not _DEVICE.fullmatch(self.device):
            raise ValueError("a remote device id is 1 to 64 characters of A-Z a-z 0-9 . _ -")
        if not isinstance(self.scope, Scope):
            raise ValueError("a remote origin needs a Scope")
        if not isinstance(self.label, str):
            raise ValueError("a remote label is text")

    @property
    def safe_label(self) -> str:
        return _LABEL_UNSAFE.sub("", self.label)[:40].strip() or self.device[:40]


class BadOrigin(Exception):
    """The remote marker is there but is not a RemoteOrigin: refuse, do not guess."""


def remote_origin(source) -> RemoteOrigin | None:
    """The marker of a request (or a bare ASGI scope): a RemoteOrigin, or None when there is none (a local
    request). Raises BadOrigin for anything else under the key."""
    scope = source if isinstance(source, dict) else getattr(source, "scope", None)
    if not isinstance(scope, dict) or SCOPE_KEY not in scope:
        return None
    value = scope[SCOPE_KEY]
    if type(value) is not RemoteOrigin:
        raise BadOrigin()
    return value


@dataclass(frozen=True)
class Reach:
    kind: str  # "local" | "remote" | "refused"
    origin: RemoteOrigin | None = None

    @property
    def scope(self) -> Scope | None:
        return self.origin.scope if self.origin else None


def reach(request) -> Reach:
    """local: a client on this machine addressing a loopback Host. remote: the bridge's marker (the route's own
    scope tag is enforced by the remote gate middleware, not here). refused: any other client, a malformed marker
    included; this is the answer Terminals needs (a shell as the user)."""
    try:
        origin = remote_origin(request)
    except BadOrigin:
        return Reach("refused")
    if origin is not None:
        return Reach("remote", origin)
    client = getattr(request, "client", None)
    if client is None or client.host not in _LOOPBACK:
        return Reach("refused")
    try:
        host = urlsplit("//" + request.headers.get("host", "")).hostname
    except ValueError:
        return Reach("refused")
    return Reach("local") if host in _LOOPBACK else Reach("refused")


# What a local request, and the dashboard's own background work, acts as: exactly as before the bridge existed.
LOCAL_HUMAN = Actor("human", "you", "dashboard")


def request_actor(request) -> Actor:
    """The actor of a dashboard request: the local human, or the paired device that sent it."""
    origin = remote_origin(request)
    if origin is None:
        return LOCAL_HUMAN
    return Actor("human", "you", f"device:{origin.safe_label}", device=origin.device)
