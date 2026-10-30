"""Presence on the person's computer (R5 design section 2.2, 3.3; hub contract 10.2c "Presence", D88).

A presence key signs exactly two messages (custody role ``remote-presence``,
:data:`orch.custody.base.REMOTE_PRESENCE_LABELS`):

* ``remote-open| ws(16) device(16) hh(32) challenge(32)``: to open a stream;
* ``remote-presence| ws(16) device(16) hh(32) nonce(32) n(1) digest(32)*n``: to let a batch of 1..16 actions through.

:func:`check_signable` re-checks the label and the exact length before ANY prompt, whoever calls a backend. There is no
software presence class in this package: a key file is refused (:func:`orch.remoteclient.state.refuse_key_file`) and
a test
that needs a signer injects its own backend through the :class:`PresenceBackend` seam.

Also here: the client's own recomputation of what a prompt would confirm (:func:`route_digest`,
:func:`template_matches`), because the client never asks a person to confirm a digest the hub or the workspace named
unless it can derive the same digest from what it sent."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .codes import ClientError
from .frames import clean_path

__all__ = [
    "DIGESTS_MAX",
    "OPEN_LABEL",
    "PRESENCE_LABEL",
    "Prompt",
    "PresenceBackend",
    "check_signable",
    "open_bytes",
    "presence_bytes",
    "route_digest",
    "template_matches",
]

OPEN_LABEL = b"orch/v2/remote-open|"
PRESENCE_LABEL = b"orch/v2/remote-presence|"
ROUTE_LABEL = b"orch/v2/remote-route|"
DIGESTS_MAX = 16
_RD, _RC, _HEX32 = (re.compile(p) for p in (r"rd_[0-9a-f]{32}", r"rc_[0-9a-f]{32}", r"[0-9a-f]{32}"))
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET")
_TEMPLATE = re.compile(r"[\x21-\x7e]+")
_PATH = re.compile(r"/[\x21-\x7e]*")
_QUERY = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*")


@dataclass(frozen=True)
class Prompt:
    """What a person is asked to confirm. Built by the client from the end-to-end session, never from the hub.
    ``reason`` is the one printable line a Touch ID sheet shows (1..200 bytes); ``lines`` is the list the home page
    mirrors (one per action)."""

    title: str
    lines: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        r = self.reason or self.title
        if not 1 <= len(r.encode("utf-8")) <= 200 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in r):
            raise ValueError("a prompt reason is 1..200 bytes without control characters")
        object.__setattr__(self, "reason", r)


class PresenceBackend(Protocol):
    """``kind`` is ``platform`` (a Secure Enclave / TPM key signing the bytes) or ``fido2`` (CTAP2 getAssertion)."""

    kind: str

    def create(self, name: str) -> dict[str, Any]:
        """Make the presence key: ``{"kind", "pub65": bytes, "ref": str}``. ``ref`` is opaque, never a path."""

    def assert_(self, ref: str, msg: bytes, prompt: Prompt) -> dict[str, Any]:
        """Sign ``msg`` behind the person's presence: ``{"form": "p256", "sig"}`` or ``{"form": "ctap2", "auth_data",
        "sig"}``. Raises :class:`ClientError` (``remote_client.cancelled``, ...). Must call :func:`check_signable`."""


def _hex16(s: object) -> bytes:
    if type(s) is not str or _HEX32.fullmatch(s) is None:
        raise ClientError("malformed")
    return bytes.fromhex(s)


def _ident(s: object) -> bytes:
    if type(s) is not str or not (_RD.fullmatch(s) or _RC.fullmatch(s)):
        raise ClientError("malformed")
    return bytes.fromhex(s[3:])


def open_bytes(workspace_id: str, ident: str, hh: bytes, challenge: bytes) -> bytes:
    if len(hh) != 32 or len(challenge) != 32:
        raise ClientError("malformed")
    return OPEN_LABEL + _hex16(workspace_id) + _ident(ident) + hh + challenge


def presence_bytes(workspace_id: str, device_id: str, hh: bytes, nonce: bytes, digests: list[bytes]) -> bytes:
    if (
        _RD.fullmatch(str(device_id)) is None
        or len(hh) != 32
        or len(nonce) != 32
        or not 1 <= len(digests) <= DIGESTS_MAX
        or any(len(d) != 32 for d in digests)
        or list(digests) != sorted(set(digests))
    ):
        raise ClientError("malformed")
    return (
        PRESENCE_LABEL
        + _hex16(workspace_id)
        + _ident(device_id)
        + hh
        + nonce
        + bytes([len(digests)])
        + b"".join(digests)
    )


def check_signable(msg: object) -> str:
    """The domain limit of a presence key: ``"open"`` or ``"presence"`` for a message that is exactly one of the two
    (label, exact length, sorted unique digests), else :class:`ClientError` ``malformed``. Called before any prompt."""
    if type(msg) is not bytes:
        raise ClientError("malformed")
    if msg.startswith(OPEN_LABEL):
        if len(msg) != len(OPEN_LABEL) + 16 + 16 + 32 + 32:
            raise ClientError("malformed")
        return "open"
    if msg.startswith(PRESENCE_LABEL):
        body = msg[len(PRESENCE_LABEL) :]
        fixed = 16 + 16 + 32 + 32 + 1
        if len(body) < fixed + 32 or body[fixed - 1] not in range(1, DIGESTS_MAX + 1):
            raise ClientError("malformed")
        n = body[fixed - 1]
        if len(body) != fixed + 32 * n:
            raise ClientError("malformed")
        digests = [body[fixed + 32 * i : fixed + 32 * (i + 1)] for i in range(n)]
        if digests != sorted(set(digests)):
            raise ClientError("malformed")
        return "presence"
    raise ClientError("malformed")


# --- the route digest, recomputed by the client ---------------------------------------------------------------------


def route_digest(workspace_id: str, method: str, template: str, path: str, query: str, body: bytes) -> bytes:
    """``SHA-256(remote-route| ws(16) u8 len method u16 len template u16 len path u16 len query SHA-256(body))``
    (vectors ``route_digest_*``)."""
    if (
        method not in _METHODS
        or type(template) is not str
        or not 0 < len(template) <= 1024
        or _TEMPLATE.fullmatch(template) is None
        or type(path) is not str
        or not 1 <= len(path) <= 2048
        or _PATH.fullmatch(path) is None
        or not clean_path(path)
        or type(query) is not str
        or len(query) > 4096
        or _QUERY.fullmatch(query) is None
        or (method == "WEBSOCKET" and body)
    ):
        raise ClientError("remote_client.no_digest")

    def u16(s: str) -> bytes:
        raw = s.encode("ascii")
        return len(raw).to_bytes(2, "big") + raw

    raw = (
        ROUTE_LABEL
        + _hex16(workspace_id)
        + bytes([len(method)])
        + method.encode("ascii")
        + u16(template)
        + u16(path)
        + u16(query)
        + hashlib.sha256(body).digest()
    )
    return hashlib.sha256(raw).digest()


def template_matches(template: str, path: str, workspace_id: str) -> bool:
    """``{ws}`` is this workspace (32 hex, or its dashed UUID spelling); any other ``{x}`` is exactly one non-empty
    segment; ``{x:path}`` is one or more.
    Everything else must match literally, segment by segment."""
    t, p = template.strip("/").split("/"), path.strip("/").split("/")
    i = 0
    for j, seg in enumerate(t):
        if seg.startswith("{") and seg.endswith("}"):
            if ":path" in seg:
                return j == len(t) - 1 and len(p) > i and all(p[i:])
            if i >= len(p) or not p[i]:
                return False
            if seg == "{ws}" and p[i].replace("-", "") != workspace_id:
                return False
            i += 1
        else:
            if i >= len(p) or p[i] != seg:
                return False
            i += 1
    return i == len(p)
