"""R3 (#1591, design I6; hub contract 10.2c "Presence for actions", D88): the signer's presence for a remote-device
stream.

A nonce is issued by the signer for ONE stream (<= 8 outstanding per stream, 5 min). The device signs
``remote-presence| || workspace_id || device_id || hh || nonce || n || digest[n]`` with its presence key (a key file,
UP+UV only for a FIDO2 key, never a synced passkey); the signer accepts it once and keeps a **batch**: exactly the
listed digests, valid until the nonce's issue time + 5 min, on that stream only. Each digest authorizes ONE action
(:meth:`Presence.take`). The facts (``workspace_id``, ``device_id``, ``hh``, the key) are the host's, never the
caller's.

:func:`check` is the pure part (the contract's ``presence`` vectors replay it); :class:`Presence` is the state."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from orch.crypto.encoding import EncodingError, unb64u
from orch.hubclient.errors import HubRefused

from .protocol import WS_ID, ClickError

__all__ = ["BATCH_MS", "DIGESTS_MAX", "NONCES_MAX", "Presence", "check", "digest_bytes", "presence_bytes"]

LABEL = b"orch/v2/remote-presence|"
BATCH_MS = 300_000
NONCES_MAX = 8
BATCHES_MAX = 32
STREAMS_MAX = 256
DIGESTS_MAX = 16
_DEVICE = "rd_"


def _no(code: str = "malformed") -> HubRefused:
    return HubRefused(code)


def digest_bytes(text: Any) -> bytes:
    """``sha256:<64 hex>`` -> the 32 bytes."""
    if type(text) is not str or len(text) != 71 or not text.startswith("sha256:"):
        raise _no()
    try:
        raw = bytes.fromhex(text[7:])
    except ValueError:
        raise _no() from None
    if text[7:] != raw.hex():  # lower case only
        raise _no()
    return raw


def presence_bytes(workspace_id: str, device_id: str, hh: bytes, nonce: bytes, digests: list[bytes]) -> bytes:
    """The bytes the presence key signs (the contract's ``presence_bytes``)."""
    if (
        type(workspace_id) is not str
        or not WS_ID.fullmatch(workspace_id)
        or type(device_id) is not str
        or not device_id.startswith(_DEVICE)
        or len(device_id) != 35
        or len(hh) != 32
        or len(nonce) != 32
        or not 1 <= len(digests) <= DIGESTS_MAX
        or any(len(d) != 32 for d in digests)
        or digests != sorted(set(digests))
    ):
        raise _no()
    try:
        dev = bytes.fromhex(device_id[3:])
    except ValueError:
        raise _no() from None
    return LABEL + bytes.fromhex(workspace_id) + dev + hh + nonce + bytes([len(digests)]) + b"".join(digests)


def check(
    entry: Mapping[str, Any],
    workspace_id: str,
    hh: bytes,
    nonce: bytes,
    digests: list[bytes],
    assertion: Any,
    held: int,
    *,
    issued: Mapping[bytes, int],
    now_ms: int,
) -> int:
    """The nonce is one the signer issued, unused and at most 5 min old (``replay`` otherwise), then the assertion over
    the bytes; returns the sign counter to record. Raises ``HubRefused`` with the contract's code."""
    from orch.host.remote.enrollment import assertion_ok

    at = issued.get(nonce)
    if at is None or not at <= now_ms < at + BATCH_MS:
        raise _no("replay")
    msg = presence_bytes(workspace_id, str(entry["device_id"]), hh, nonce, digests)
    return assertion_ok(str(entry["presence_pub"]), str(entry["presence_kind"]), msg, assertion, held)


@dataclass
class _Batch:
    expires_ms: int
    digests: set[bytes]


class Presence:
    """The nonces and batches of the streams this signer serves; memory only (a restart drops them, so a device simply
    signs again)."""

    def __init__(self, wall: Callable[[], float]) -> None:
        self._wall = wall
        self._nonces: dict[str, dict[bytes, int]] = {}
        self._batches: dict[str, list[_Batch]] = {}

    def _now(self) -> int:
        return int(self._wall() * 1000)

    def _prune(self, now: int) -> None:
        for sid in list(self._nonces):
            live = {n: at for n, at in self._nonces[sid].items() if at <= now < at + BATCH_MS}
            if live:
                self._nonces[sid] = live
            else:
                del self._nonces[sid]
        for sid in list(self._batches):
            live_b = [b for b in self._batches[sid] if b.digests and now < b.expires_ms]
            if live_b:
                self._batches[sid] = live_b
            else:
                del self._batches[sid]

    def issue(self, stream: str) -> bytes:
        now = self._now()
        self._prune(now)
        mine = self._nonces.get(stream, {})
        if len(mine) >= NONCES_MAX or (stream not in self._nonces and len(self._nonces) >= STREAMS_MAX):
            raise ClickError("rate.limited", "too many presence requests", retry_after_ms=10_000, retryable=True)
        nonce = secrets.token_bytes(32)
        self._nonces.setdefault(stream, {})[nonce] = now
        return nonce

    def consume(self, stream: str, nonce: bytes) -> tuple[Mapping[bytes, int], int]:
        """Take the nonce out (single use, even when the assertion then fails). Returns ``({nonce: issued}, now)`` for
        :func:`check`; a nonce never issued for this stream is simply absent there (``replay``)."""
        now = self._now()
        self._prune(now)
        at = self._nonces.get(stream, {}).pop(nonce, None)
        return ({nonce: at} if at is not None else {}), now

    def store(self, stream: str, issued_ms: int, digests: list[bytes]) -> None:
        batches = self._batches.setdefault(stream, [])
        if len(batches) >= BATCHES_MAX:
            raise ClickError("rate.limited", "too many presence batches", retry_after_ms=10_000, retryable=True)
        batches.append(_Batch(issued_ms + BATCH_MS, set(digests)))

    def take(self, stream: str, digest: bytes) -> bool:
        """Remove ``digest`` from a live batch of THIS stream; True when it was there (each digest, one action)."""
        now = self._now()
        self._prune(now)
        for batch in self._batches.get(stream, []):
            if digest in batch.digests:
                batch.digests.discard(digest)
                return True
        return False

    def drop(self, stream: str) -> None:
        self._nonces.pop(stream, None)
        self._batches.pop(stream, None)


def b64_exact(value: Any, n: int) -> bytes:
    try:
        return unb64u(value, n)
    except EncodingError:
        raise _no() from None
