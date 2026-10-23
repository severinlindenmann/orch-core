"""``PublishSigner``: the WSK signature of an orch-publish request (protocol-v2 §12.1, design-publish-addon §2.4).

It sits beside :class:`orch.wsproto.sign.RelaySigner`, under the same API rule (``tests/relay/test_import_ban.py``): the
key arrives as a :class:`orch.wsproto.sign.WorkspaceKey`, is captured in a closure, and the signer exposes one purpose
method, :meth:`PublishSigner.request`. There is **no** generic ``sign(payload)``. The bytes it signs are built here and
only here::

    signed = "orch/v2/publish|" || suite || workspace_id (16) || u64(ts_ms) || nonce (16)
             || lp16(METHOD) || lp16(host) || lp16(request-target) || H(body)

The custody backend signs only payloads that start with a registered signature label (``orch/v2/publish|`` is one, and
``HOST_LABELS`` lets the host use it), so a caller that bypassed this class still could not sign other bytes with this
label's meaning.

What is checked before signing (fail closed, ``SignerRefused`` carries a rule name and never any input bytes):

* the **host is not a parameter**: it is derived from the stored API origin (``wire.check_origin``:
  ``https`` anywhere, ``http`` only for ``127.0.0.1``/``localhost``), lower-case, with a port only when it is not the
  scheme default. A response, redirect or caller can never name another host (invariant I6).
* the **target** is the path and query exactly as it will be sent: ASCII, printable, no space, ``#`` or ``\\``, starts
  with ``/v1/ws/<this workspace>/`` (the server also pins path ``<ws>`` to the signed header, invariant I3), at most
  2048 bytes. Nothing is normalised or decoded.
* the **method** is one of ``GET PUT POST DELETE``; the **body** is ``bytes`` (any size, hashed, never held after).
* the **nonce** is 16 fresh random bytes per call (a retry is a new call). A nonce seen before by this signer, or one
  already in the optional durable ledger, is refused (a broken RNG cannot sign twice under one nonce).
* the **timestamp** is the signer's clock in whole milliseconds (``time.time_ns() // 1_000_000``), never an input.
"""

from __future__ import annotations

import collections
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from orch import crypto
from orch.crypto.labels import L
from orch.wsproto import wire
from orch.wsproto.sign import SignerRefused, WorkspaceKey
from orch.wsproto.state import SignLedger

__all__ = ["METHODS", "TARGET_MAX", "PublishContext", "PublishSigner", "SignedPublish", "publish_signed_bytes"]

METHODS = ("GET", "PUT", "POST", "DELETE")
TARGET_MAX = 2048
_NONCE_LEN = 16
_MAX_TS = 2**53 - 1
_SEEN_MAX = 4096


def _lp16(b: bytes) -> bytes:
    if len(b) > 0xFFFF:
        raise SignerRefused("field over 65535 bytes")
    return len(b).to_bytes(2, "big") + b


def publish_signed_bytes(
    workspace: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes
) -> bytes:
    """The §12.1 bytes, byte-equal to orch-relay ``orch_protocol_ref.publish_signed_bytes`` (pinned by the vectors).
    Pure and total over well-formed input; shape errors raise :class:`SignerRefused`."""
    if type(workspace) is not bytes or len(workspace) != 16:
        raise SignerRefused("workspace id")
    if type(ts_ms) is not int or not 0 <= ts_ms <= _MAX_TS:
        raise SignerRefused("timestamp")
    if type(nonce) is not bytes or len(nonce) != _NONCE_LEN:
        raise SignerRefused("nonce")
    if method not in METHODS:
        raise SignerRefused("method")
    if type(body) is not bytes:
        raise SignerRefused("body must be bytes")
    try:
        parts = (method.encode("ascii"), host.encode("ascii"), target.encode("ascii"))
    except (UnicodeEncodeError, AttributeError):
        raise SignerRefused("host or target is not ASCII") from None
    return (
        L["sig_publish"]
        + bytes([crypto.SUITE_ID])
        + workspace
        + ts_ms.to_bytes(8, "big")
        + nonce
        + b"".join(_lp16(p) for p in parts)
        + crypto.sha256(body)
    )


_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def _segment_ok(seg: str) -> bool:
    """A path segment is a name: not empty, not ``.``/``..``, no separator or NUL, in any of up to three layers of
    percent-decoding (``%2e%2e``, ``%252e``, ``%2f``): a server, proxy or filesystem that decodes once more must see
    the same name we signed. The raw segment must be valid percent-encoding; a later layer that is not (``50%25`` is
    the name ``50%``) simply ends the walk."""
    cur = seg
    for layer in range(4):
        if cur in ("", ".", "..") or any(c in cur for c in "/\\\x00"):
            return False
        if "%" not in cur:
            return True
        if cur.count("%") != len(_PCT.findall(cur)):
            return layer > 0  # a stray ``%`` is invalid encoding in the raw segment, an ordinary name afterwards
        cur = _PCT.sub(lambda m: chr(int(m.group()[1:], 16)), cur)
    return False  # more layers than anything legitimate uses


def _target_ok(target: object, workspace_hex: str) -> str:
    if type(target) is not str or not target.isascii() or not 1 <= len(target) <= TARGET_MAX:
        raise SignerRefused("target")
    if any(not 0x21 <= ord(c) <= 0x7E for c in target) or "#" in target or "\\" in target:
        raise SignerRefused("target characters")
    prefix = f"/v1/ws/{workspace_hex}/"
    if not target.startswith(prefix):
        raise SignerRefused("target is outside this workspace's routes")
    path, q, query = target.partition("?")
    if q and not query:
        raise SignerRefused("target ends in an empty query")
    # every segment after the workspace is a plain name: the path is already in the form no one needs to normalise
    if not all(_segment_ok(seg) for seg in path[len(prefix) :].split("/")):
        raise SignerRefused("target has an empty, dot or encoded-separator segment")
    return target


@dataclass(frozen=True)
class PublishContext:
    """What every publish signature is bound to: the workspace id (32 lower-case hex) and the API origin from the
    host's signed publish config, stored verbatim."""

    workspace_id: str
    api_origin: str

    def __post_init__(self) -> None:
        try:
            crypto.unhex(self.workspace_id, 16)
            wire.check_origin(self.api_origin)
        except (crypto.EncodingError, wire.WireError):
            raise SignerRefused("publish context") from None

    @property
    def host(self) -> str:
        """The lower-case host a client sends, with a port only if it is not the scheme's default (§12.1)."""
        scheme, host, port = wire.origin_parts(self.api_origin)
        return host if port == (443 if scheme == "https" else 80) else f"{host}:{port}"


@dataclass(frozen=True)
class SignedPublish:
    """One signed request: where it goes (``host``) and the four headers. Holds no body and no key."""

    method: str
    host: str
    target: str
    headers: dict[str, str]

    def __repr__(self) -> str:  # the signature is not secret, but keep reprs short and free of the target's query
        return f"SignedPublish({self.method} {self.host})"


def _bind(key: WorkspaceKey) -> Callable[[bytes], bytes]:
    def sign_bytes(payload: bytes) -> bytes:
        return key.sign(payload)

    return sign_bytes


class PublishSigner:
    """Purpose-specific WSK signatures for orch-publish requests. Only the host process calls it: after a verified
    human approval (``publish.share``) or a person op (``publish.revoke``, ``publish.list``)."""

    __slots__ = ("_clock", "_ledger", "_lock", "_pub", "_rand", "_seen", "_sign_bytes", "ctx")

    def __init__(
        self,
        key: WorkspaceKey,
        ctx: PublishContext,
        *,
        ledger: SignLedger | None = None,
        clock_ms: Callable[[], int] | None = None,
        rand: Callable[[int], bytes] | None = None,
    ) -> None:
        self.ctx = ctx
        self._pub = bytes(key.public_key)
        self._sign_bytes = _bind(key)
        self._ledger = ledger
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._rand = rand or os.urandom
        self._seen: collections.deque[bytes] = collections.deque(maxlen=_SEEN_MAX)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"PublishSigner(workspace={self.ctx.workspace_id}, host={self.ctx.host})"

    @property
    def public_key(self) -> bytes:
        return self._pub

    def request(self, method: str, target: str, body: bytes = b"") -> SignedPublish:
        """Sign one request to this signer's API host. Each call makes a fresh nonce and reads the clock again, so a
        retry is just another call (§12.1: the server records every nonce for 240 s)."""
        _target_ok(target, self.ctx.workspace_id)
        if method not in METHODS:
            raise SignerRefused("method")
        if type(body) is not bytes:
            raise SignerRefused("body must be bytes")
        ts = self._clock()
        nonce = self._rand(_NONCE_LEN)
        signed = publish_signed_bytes(
            bytes.fromhex(self.ctx.workspace_id), ts, nonce, method, self.ctx.host, target, body
        )
        digest = crypto.sha256(signed).hex()
        with self._lock:
            if nonce in self._seen:
                raise SignerRefused("nonce reuse")
            if self._ledger is not None and self._ledger.get("publish", nonce.hex()) is not None:
                raise SignerRefused("nonce reuse")
            sig = self._sign_bytes(signed)
            if type(sig) is not bytes or len(sig) != crypto.SIG_LEN:
                raise SignerRefused("the key returned a malformed signature")
            self._seen.append(nonce)
            if self._ledger is not None:
                self._ledger.record("publish", nonce.hex(), digest, crypto.b64u(sig))
        return SignedPublish(
            method,
            self.ctx.host,
            target,
            {
                "Orch-Workspace": self.ctx.workspace_id,
                "Orch-Ts": str(ts),
                "Orch-Nonce": nonce.hex(),
                "Orch-Signature": crypto.b64u(sig),
            },
        )
