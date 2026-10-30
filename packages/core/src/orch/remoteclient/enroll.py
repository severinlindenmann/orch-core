"""Adding this computer to a workspace (R5 design 3.1; hub contract 10.2c "Enrollment", row 68).

The person pastes a code made at the workspace (``orch-rc1.<ws>.<b64u 48>.<b64u 32>``); the client makes the
transport key,
opens an ``enroll`` stream, proves the code inside the handshake (HMAC under the code's secret, bound to both device
keys
and the handshake hash), signs the open bytes with the presence key behind Touch ID / the security key, and sends one
``enroll`` message. The host answers ``pending`` with the fingerprint of the device; the client **computes the
fingerprint
from its own keys and refuses on a difference**, shows the six words, and waits (at most 15 min) for the owner to
confirm
at the workspace (``added``). The code, its secret and the keys never reach a URL or a log."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orch.canon.jcs import JcsError, dumps, loads_strict
from orch.crypto.encoding import EncodingError, b64u, unb64u
from orch.hubclient.custodial_shape import text_ok
from orch.model import remote as model_remote

from . import presence as P
from .channel import Channel
from .codes import ClientError
from .noise_init import x25519_public

__all__ = ["Code", "enroll_message", "parse_code", "run_enroll"]

MAC_LABEL = b"orch/v2/remote-enroll|"
WRK_LABEL = b"orch/v2/remote-wrk|"
_KIND_BYTE = {"platform": 1, "fido2": 2}
_HEX32 = re.compile(r"[0-9a-f]{32}")
WAIT_S = 15 * 60


@dataclass(frozen=True)
class Code:
    workspace_id: str
    code_id: str  # rc_<32 hex>
    secret: bytes
    wrk_pub: bytes


def parse_code(text: str) -> Code:
    """Strict: four dot parts, canonical base64url, the WRK fingerprint in the code must equal the hash of the WRK in
    it."""
    if type(text) is not str or not 1 <= len(text) <= 512 or text != text.strip():
        raise ClientError("remote_client.bad_code")
    if text.startswith("orch-rb1."):
        raise ClientError("remote_client.browser_code")
    parts = text.split(".")
    if len(parts) != 4 or parts[0] != "orch-rc1" or not _HEX32.fullmatch(parts[1]):
        raise ClientError("remote_client.bad_code")
    try:
        body, wrk = unb64u(parts[2], 48), unb64u(parts[3], 32)
    except EncodingError:
        raise ClientError("remote_client.bad_code") from None
    if not hmac.compare_digest(hashlib.sha256(WRK_LABEL + wrk).digest()[:16], body[32:]):
        raise ClientError("remote_client.wrk_mismatch")
    return Code(parts[1], "rc_" + body[:16].hex(), body[16:32], wrk)


def enroll_message(
    code: Code, hh: bytes, transport_pub: bytes, presence_pub: bytes, kind: str, name: str, presence: dict[str, Any]
) -> dict[str, Any]:
    mac = hmac.new(
        code.secret, MAC_LABEL + hh + transport_pub + presence_pub + bytes([_KIND_BYTE[kind]]), hashlib.sha256
    )
    return {
        "t": "enroll", "name": name, "presence_kind": kind, "presence_pub": b64u(presence_pub),
        "mac": b64u(mac.digest()), "presence": presence,
    }  # fmt: skip


def run_enroll(
    ch: Channel,
    code: Code,
    *,
    transport_key: bytes,
    presence_kind: str,
    presence_pub: bytes,
    assert_: Callable[[bytes, P.Prompt], dict[str, Any]],
    name: str,
    on_words: Callable[[str], None],
    wait_s: float = WAIT_S,
    clock: Callable[[], float] = time.monotonic,
    cancel: threading.Event | None = None,
) -> str:
    """After the handshake. Returns the new ``device_id``. Raises :class:`ClientError` (and closes the stream) on any
    mismatch, cancel or close."""
    name = name if text_ok(name, 64) else "This computer"
    transport_pub = x25519_public(transport_key)
    try:
        fp = model_remote.device_fp(presence_kind, transport_pub, presence_pub)
    except ValueError:
        raise ClientError("remote_client.assertion") from None
    try:
        _check(cancel)
        msg = P.open_bytes(code.workspace_id, code.code_id, ch.hh, ch.challenge)
        prompt = P.Prompt(
            "Add this computer",
            (f"Add this computer to workspace {code.workspace_id[-8:]}",),
            f"Add this computer to workspace {code.workspace_id[-8:]}",
        )
        presence = assert_(msg, prompt)
        _check(cancel)
        ch.send(dumps(enroll_message(code, ch.hh, transport_pub, presence_pub, presence_kind, name, presence)))
        end = clock() + wait_s
        pending = _next(ch, end, clock, cancel)
        if set(pending) != {"t", "fingerprint"} or pending["t"] != "pending":
            raise ClientError("remote_client.protocol")
        if pending["fingerprint"] != fp.hex():
            raise ClientError("remote_client.fingerprint")
        on_words(model_remote.fingerprint_words(fp))
        added = _next(ch, end, clock, cancel)
        did = added.get("device_id")
        if set(added) != {"t", "device_id"} or added["t"] != "added" or not re.fullmatch(r"rd_[0-9a-f]{32}", str(did)):
            raise ClientError("remote_client.protocol")
    except ClientError as exc:
        if exc.code in ("remote_client.cancelled", "remote_client.fingerprint", "remote_client.protocol"):
            ch.close("presence" if exc.code == "remote_client.cancelled" else "malformed")
        else:
            ch.close()
        raise
    ch.close()
    return str(did)


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ClientError("remote_client.cancelled")


def _next(ch: Channel, end: float, clock: Callable[[], float], cancel: threading.Event | None = None) -> dict[str, Any]:
    while True:
        _check(cancel)
        left = end - clock()
        if left <= 0:
            raise ClientError("remote_client.timeout")
        pt = ch.recv(min(left, 1.0))
        if pt is None:
            continue
        try:
            o = loads_strict(pt)
        except (JcsError, UnicodeDecodeError, ValueError):
            raise ClientError("remote_client.protocol") from None
        if type(o) is not dict:
            raise ClientError("remote_client.protocol")
        return o
