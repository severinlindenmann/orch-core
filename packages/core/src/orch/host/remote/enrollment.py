"""Enrolling a device (R2 #1590, design note section 3; hub contract 10.2c "Enrollment").

Everything here is memory only (a restart drops codes and pending devices): the one-time codes with their secrets, the
pending confirmations, nothing on disk. R2 builds all but the Noise handshake; R3 calls the seam fixed in the note:

``verify(service, code_id, stream_id, hh, transport_pub, first_msg, challenge) -> dict`` (raises ``HubRefused``),
``wait_added(pid, timeout) -> device_id | None``, ``drop(stream_id)``.

The order is the contract's and nothing is created before all of it passes: a code that is outstanding and unexpired
(else ``remote.no_code``), the shape of the message (``malformed``), the MAC under the code secret (``bad_signature``:
five of them burn the code), the presence assertion over the open bytes. Only then the code is consumed (single use)
and a :class:`Pending` exists; the owner confirms it with ``remote.device_confirm`` (the owner proof, a person op), the
commit hook resolves it (:func:`on_added`)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u
from orch.hubclient.custodial_shape import text_ok
from orch.hubclient.errors import HubRefused

from ..errors import HostError

__all__ = [
    "CODE_TTL_MS", "CODES_MAX", "TRIES_MAX", "assertion_ok", "check_enroll", "code_text", "drop", "forget_all",
    "issue", "on_added", "outstanding", "pending_list", "pending_source", "verify", "wait_added",
]  # fmt: skip

CODE_TTL_MS = 15 * 60_000
CODES_MAX = 4
TRIES_MAX = 5
BATCH_MS = 300_000
_KIND = {"platform": 1, "fido2": 2}
_OPEN = b"orch/v2/remote-open|"
_ENROLL = b"orch/v2/remote-enroll|"


@dataclass
class _Code:
    code_id: str
    secret: bytes
    expires_ms: int
    wrk_fp: bytes
    tries: int = 0


@dataclass
class Pending:
    pid: str
    workspace_id: str
    code_id: str
    stream_id: str
    name: str
    presence_kind: str
    transport_pub: str  # b64u
    presence_pub: str  # b64u
    fingerprint: str  # hex
    words: str
    sign_count: int
    expires_ms: int
    device_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class _Ws:
    def __init__(self) -> None:
        self.mu = threading.RLock()
        self.codes: dict[str, _Code] = {}
        self.pending: dict[str, Pending] = {}


_BY_PID: dict[str, Pending] = {}
_MU = threading.Lock()


def _ws(service: Any) -> _Ws:
    with _MU:
        st = getattr(service, "_remote_enroll", None)
        if st is None:
            st = service._remote_enroll = _Ws()  # noqa: SLF001
        return st  # type: ignore[no-any-return]


def _now(service: Any) -> int:
    return int(service.clock() * 1000)


# -- codes -----------------------------------------------------------------------------------------------------------


def _live(st: _Ws, now_ms: int) -> None:
    for cid in [c for c, v in st.codes.items() if now_ms >= v.expires_ms]:
        del st.codes[cid]


def outstanding(service: Any, now_ms: int) -> dict[str, int]:
    """``{code_id: expires_ms}`` of the codes that are outstanding and unexpired (the gate's enroll check)."""
    st = _ws(service)
    with st.mu:
        _live(st, now_ms)
        return {c: v.expires_ms for c, v in st.codes.items()}


def code_text(workspace_id: str, code_id: str, secret: bytes, wrk_pub: bytes) -> str:
    """``orch-rc1.<ws_id>.<b64u(code_id | secret | wrk_fp)>.<b64u(WRK_pub)>`` (vector ``code_bytes``)."""
    from . import wrk

    body = bytes.fromhex(code_id[3:]) + secret + wrk.wrk_fp(wrk_pub)
    return f"orch-rc1.{workspace_id}.{b64u(body)}.{b64u(wrk_pub)}"


def issue(service: Any, now_ms: int) -> dict[str, Any]:
    """``remote.code``: a one-time code, shown once (never logged; the log gets only ``remote.code_issued``)."""
    from . import append_host, wrk

    st = _ws(service)
    with service.store.locked(), st.mu:
        _live(st, now_ms)
        if len(st.codes) >= CODES_MAX:
            raise HostError("remote.full", "four enrollment codes are already waiting; use or wait for one")
        pub = wrk.public(wrk.create(service))
        code = _Code("rc_" + secrets.token_hex(16), secrets.token_bytes(16), now_ms + CODE_TTL_MS, wrk.wrk_fp(pub))
        append_host(service, "remote.code_issued", code_id=code.code_id, expires_ms=code.expires_ms)
        st.codes[code.code_id] = code
    return {
        "code": code_text(service.workspace_id, code.code_id, code.secret, pub),
        "code_id": code.code_id,
        "expires_ms": code.expires_ms,
    }


# -- the verify seam -------------------------------------------------------------------------------------------------


def _no(code: str = "malformed") -> HubRefused:
    return HubRefused(code)


def _b64(s: Any, n: int | None = None) -> bytes:
    try:
        return unb64u(s, n)
    except EncodingError:
        raise _no() from None


def open_bytes(workspace_id: str, code_id: str, hh: bytes, challenge: bytes) -> bytes:
    if len(hh) != 32 or len(challenge) != 32:
        raise _no()
    return _OPEN + bytes.fromhex(workspace_id) + bytes.fromhex(code_id[3:]) + hh + challenge


def assertion_ok(pub_b64: str, kind: str, msg: bytes, a: Any, held: int) -> int:
    """A presence assertion over ``msg`` (contract: ``p256`` for a platform key, ``ctap2`` for a FIDO2 key with UP and
    UV, never a synced passkey); returns the new signature counter. Raises ``HubRefused``."""
    pub = _b64(pub_b64, 65)
    if type(a) is not dict:
        raise _no()
    want = {"p256": {"form", "sig"}, "ctap2": {"form", "auth_data", "sig"}}.get(a.get("form"))  # type: ignore[arg-type]
    if want is None or set(a) != want:
        raise _no()
    sig = _b64(a["sig"], 64)
    auth = _b64(a["auth_data"], 37) if a["form"] == "ctap2" else b""
    if (a["form"] == "p256") != (kind == "platform"):
        raise _no("factor_mismatch")
    if a["form"] == "p256":
        if not suite.verify(pub, sig, msg):
            raise _no("bad_signature")
        return held
    if auth[:32] != hashlib.sha256(b"orch-remote").digest() or not suite.verify(
        pub, sig, auth + hashlib.sha256(msg).digest()
    ):
        raise _no("bad_signature")
    flags, count = auth[32], int.from_bytes(auth[33:], "big")
    if flags & 0xC0:
        raise _no()
    if flags & 0x05 != 0x05 or flags & 0x18:
        raise _no("factor_too_weak")  # UP and UV needed; BE/BS (a synced passkey) refused
    if (held or count) and count <= held:
        raise _no("replay")
    return count


def _mac_stage(msg: Any, secret: bytes, hh: bytes, transport_pub: bytes) -> tuple[bytes, str, bytes]:
    """Shape, then the MAC: returns ``(presence_pub, kind, fingerprint)``."""
    from orch.model.remote import device_fp

    if type(msg) is not dict or set(msg) != {"t", "name", "presence_kind", "presence_pub", "mac", "presence"}:
        raise _no()
    if msg["t"] != "enroll" or not text_ok(msg["name"], 64) or msg["presence_kind"] not in _KIND:
        raise _no()
    ppub, mac = _b64(msg["presence_pub"], 65), _b64(msg["mac"], 32)
    try:
        fp = device_fp(msg["presence_kind"], transport_pub, ppub)  # also refuses a presence key off the curve
    except ValueError:
        raise _no() from None
    body = _ENROLL + hh + transport_pub + ppub + bytes([_KIND[msg["presence_kind"]]])
    if not hmac.compare_digest(mac, hmac.new(secret, body, hashlib.sha256).digest()):
        raise _no("bad_signature")
    return ppub, msg["presence_kind"], fp


def _result(msg: dict[str, Any], transport_pub: bytes, fp: bytes, count: int) -> dict[str, Any]:
    return {
        "name": msg["name"], "presence_kind": msg["presence_kind"], "presence_pub": msg["presence_pub"],
        "transport_pub": transport_pub.hex(), "fingerprint": fp.hex(), "sign_count": count,
    }  # fmt: skip


def check_enroll(
    msg: Any, *, secret: bytes, workspace_id: str, code_id: str, hh: bytes, transport_pub: bytes, challenge: bytes
) -> dict[str, Any]:
    """The whole first-message check in the contract's order (vectors ``enroll_*``)."""
    _, _, fp = _mac_stage(msg, secret, hh, transport_pub)
    count = assertion_ok(
        msg["presence_pub"], msg["presence_kind"], open_bytes(workspace_id, code_id, hh, challenge), msg["presence"], 0
    )
    return _result(msg, transport_pub, fp, count)


def verify(
    service: Any, code_id: str, stream_id: str, hh: bytes, transport_pub: bytes, first_msg: Any, challenge: bytes
) -> dict[str, Any]:
    """R3's seam: the device's first message on an enroll stream. Raises ``HubRefused`` (``remote.no_code``,
    ``malformed``, ``bad_signature``, ...). On success the code is consumed, a pending device exists and the answer is
    ``{"t": "pending", "fp": hex, "pending": pid, "words": "...", "expires_ms": n}`` (the device is sent ``t`` and
    ``fp``; ``pending`` is for ``wait_added`` and the owner's view). Nothing is created before every check passes."""
    from orch.hubclient import wire

    from . import append_host

    now = _now(service)
    st = _ws(service)
    burned: HubRefused | None = None
    with st.mu:
        _live(st, now)
        code = st.codes.get(code_id)
        if code is None:
            raise _no("remote.no_code")
        try:
            _, _, fp = _mac_stage(first_msg, code.secret, hh, transport_pub)
        except HubRefused as exc:
            if exc.code != "bad_signature":
                raise
            code.tries += 1
            if code.tries < TRIES_MAX:
                raise
            del st.codes[code_id]  # the fifth bad MAC burns the code
            burned = exc
        if burned is None:
            count = assertion_ok(
                first_msg["presence_pub"], first_msg["presence_kind"],
                open_bytes(service.workspace_id, code_id, hh, challenge), first_msg["presence"], 0,
            )  # fmt: skip
            del st.codes[code_id]  # single use: consumed once all three checks passed
            p = Pending(
                pid="rp_" + secrets.token_hex(16), workspace_id=service.workspace_id, code_id=code_id,
                stream_id=stream_id, name=first_msg["name"], presence_kind=first_msg["presence_kind"],
                transport_pub=b64u(transport_pub), presence_pub=first_msg["presence_pub"], fingerprint=fp.hex(),
                words=wire.fingerprint_words(fp), sign_count=count, expires_ms=code.expires_ms,
            )  # fmt: skip
            st.pending[p.pid] = p
            with _MU:
                for pid in [k for k, v in _BY_PID.items() if now > v.expires_ms + 60_000]:
                    del _BY_PID[pid]
                _BY_PID[p.pid] = p
    if burned is not None:  # recorded on the worker thread (the store belongs to it), outside our lock
        service.submit(lambda: append_host(service, "remote.code_burned", code_id=code_id)).result(timeout=30)
        raise burned
    return {"t": "pending", "fp": p.fingerprint, "pending": p.pid, "words": p.words, "expires_ms": p.expires_ms}


# -- pending devices -------------------------------------------------------------------------------------------------


def pending_source(service: Any, pid: str) -> dict[str, Any] | None:
    """What ``remotefacts`` hands ``remote.device_confirm``: the pending device, or ``None`` (``remote.no_pending``)."""
    st = _ws(service)
    now = _now(service)
    with st.mu:
        p = st.pending.get(pid)
        if p is None or now >= p.expires_ms:
            return None
        return {
            "name": p.name, "presence_kind": p.presence_kind, "transport_pub": p.transport_pub,
            "presence_pub": p.presence_pub, "code_id": p.code_id,
        }  # fmt: skip


def pending_list(service: Any, now_ms: int) -> list[dict[str, Any]]:
    """``remote.pending``: what the owner is asked ("Add device <name>, fingerprint <words>?")."""
    st = _ws(service)
    with st.mu:
        return [
            {"pending": p.pid, "name": p.name, "words": p.words, "presence_kind": p.presence_kind,
             "expires_ms": p.expires_ms}
            for p in sorted(st.pending.values(), key=lambda p: p.pid) if now_ms < p.expires_ms
        ]  # fmt: skip


def on_added(service: Any, event: dict[str, Any]) -> None:
    """The commit hook, under the store lock, for a committed ``remote.device_added``: resolve the pending device of
    its code and wake the waiter; a FIDO2 device's signature counter is recorded for R3."""
    st = _ws(service)
    with st.mu:
        hit = next((p for p in st.pending.values() if p.code_id == event["code_id"]), None)
        if hit is None:
            return
        del st.pending[hit.pid]
        hit.device_id = event["device_id"]
        hit.done.set()
    if hit.presence_kind == "fido2":
        _counter(service, event["device_id"], hit.sign_count)


def _counter(service: Any, device_id: str, count: int) -> None:
    from ..custody import ensure_private_dir, read_json, write_private
    from . import wrk

    path = wrk.state_dir(service) / "counters.json"
    doc = read_json(path, service.host_uid, limit=1 << 20)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("fido2"), dict):
        doc = {"v": 1, "fido2": {}}
    doc["fido2"][device_id] = count
    ensure_private_dir(wrk.state_dir(service), service.host_uid)
    write_private(path, doc)


def wait_added(pid: str, timeout: float | None = None) -> str | None:
    """Block until the owner confirmed the pending device (its ``device_id``) or it was dropped / expired / the key
    rotated (``None``). One waiter takes the answer."""
    with _MU:
        p = _BY_PID.get(pid)
    if p is None:
        return None
    p.done.wait(timeout)
    if not p.done.is_set():
        return None
    with _MU:
        _BY_PID.pop(pid, None)
    return p.device_id


def _end(p: Pending) -> None:
    p.done.set()  # device_id stays None: nothing was added


def drop(service: Any, stream_id: str) -> None:
    """The enroll stream closed: its pending device is gone (a later confirm is ``remote.no_pending``)."""
    st = _ws(service)
    with st.mu:
        for pid in [k for k, p in st.pending.items() if p.stream_id == stream_id]:
            _end(st.pending.pop(pid))


def forget_all(service: Any) -> None:
    """Drop every code and pending device (the key rotated: they pinned the old WRK)."""
    st = _ws(service)
    with st.mu:
        st.codes.clear()
        for p in st.pending.values():
            _end(p)
        st.pending.clear()
