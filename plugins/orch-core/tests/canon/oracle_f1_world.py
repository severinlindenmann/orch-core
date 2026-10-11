"""Independent oracle, part 3: P-256 test keys, deterministic ECDSA (RFC 6979), signed objects and a builder for fully
signed F1 logs. Only ``hashlib``, ``hmac``, ``json`` and ``cryptography`` are used; nothing here imports ``orch``.

**The private keys are published on purpose** (they are derived from the key names below): they exist so that the
vector files hold real signatures, and must never protect anything.

Signatures are deterministic (RFC 6979, SHA-256, no low-s normalisation), so the committed vector files equal what the
oracle writes. Consumers do not re-sign: they *verify* the stored signature, and refuse the tampered variants.

The builder (:class:`World`) writes *scenarios*: lists of steps ``{"log", "event", "expect", ...}`` in which every
event is complete (person ``sig`` and ``host_sig`` included). ``expect`` is ``"ok"`` (the event is appended) or the
refusal code the model gives; a refused event is not part of the chain that later steps continue.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
W = "0123456789abcdef0123456789abcdef"
SCOPES = ["look", "decide", "operate", "type"]
B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

LABEL = {
    "ticket_event": "orch/v2/sig/ticket-event|",
    "ws_event": "orch/v2/sig/ws-event|",
    "host_event": "orch/v2/sig/host-event|",
    "checkpoint": "orch/v2/sig/checkpoint|",
    "device_cert": "orch/v2/sig/device-cert|",
    "revocation": "orch/v2/sig/revocation|",
    "ws_delegation": "orch/v2/sig/ws-delegation|",
    "event": "orch/v2/event|",
    "id_person": "orch/v2/id/person|",
    "id_device": "orch/v2/id/device|",
}


def cj(o: Any) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def head(event: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(LABEL["event"].encode() + cj(event)).hexdigest()


# --- keys and signatures ------------------------------------------------------------------------------------


class Key:
    def __init__(self, name: str) -> None:
        self.name = name
        self.d = int.from_bytes(hashlib.sha256(("orch-f1-test-key|" + name).encode()).digest(), "big") % (N - 1) + 1
        self.pub = (
            ec.derive_private_key(self.d, ec.SECP256R1())
            .public_key()
            .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        )

    @property
    def pub_b64u(self) -> str:
        return b64u(self.pub)


_KEYS: dict[str, Key] = {}


def key(name: str) -> Key:
    if name not in _KEYS:
        _KEYS[name] = Key(name)
    return _KEYS[name]


def _k6979(d: int, h1: bytes) -> int:
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(h1, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(who: str, msg: bytes) -> bytes:
    """ECDSA P-256 / SHA-256, RFC 6979 nonce, raw 64-byte ``r || s``."""
    d = key(who).d
    h1 = hashlib.sha256(msg).digest()
    k = _k6979(d, h1)
    r = ec.derive_private_key(k, ec.SECP256R1()).public_key().public_numbers().x % N
    s = pow(k, -1, N) * (int.from_bytes(h1, "big") + r * d) % N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def person_id(pk_pub: bytes) -> str:
    return hashlib.sha256(LABEL["id_person"].encode() + bytes([2]) + pk_pub).digest()[:16].hex()


def device_id(dk_pub: bytes) -> str:
    return hashlib.sha256(LABEL["id_device"].encode() + bytes([2]) + dk_pub).digest()[:16].hex()


def signed_object(kind: str, o: dict[str, Any], signer: str) -> dict[str, Any]:
    return {"o": o, "sig": b64u(sign(signer, LABEL[kind].encode() + cj(o)))}


def make_cert(person: str, dev: str, scopes: list[str] | None = None, *, created_ms: int = 1_790_000_000_000,
              expires_ms: int | None = None, signer: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A protocol §6.1 device certificate for device key ``dk_<dev>`` of person key ``pk_<person>``."""
    o = {
        "v": 2,
        "suite": 2,
        "kind": "device_cert",
        "device_id": device_id(key("dk_" + dev).pub),
        "person_id": person_id(key("pk_" + person).pub),
        "dk_sig_pub": key("dk_" + dev).pub_b64u,
        "dk_kx_pub": key("kx_" + dev).pub_b64u,
        "label_sealed": b64u(b"label " + dev.encode()),
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "scopes_max": list(SCOPES if scopes is None else scopes),
    }
    return signed_object("device_cert", o, signer or "pk_" + person)


def make_revocation(person: str, dev: str, reason: str, *, revoked_ms: int = 1_790_000_100_000,
                    signer: str | None = None) -> dict[str, Any]:  # fmt: skip
    o = {
        "v": 2,
        "suite": 2,
        "kind": "revocation",
        "person_id": person_id(key("pk_" + person).pub),
        "device_id": device_id(key("dk_" + dev).pub),
        "revoked_ms": revoked_ms,
        "reason": reason,
    }
    return signed_object("revocation", o, signer or "pk_" + person)


def make_delegation(owner: str, *, workspace_id: str = W, wsk: str = "wsk", signer: str | None = None,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    o = {
        "v": 2,
        "suite": 2,
        "kind": "ws_delegation",
        "workspace_id": workspace_id,
        "wsk_pub": key(wsk).pub_b64u,
        "owner_person_id": person_id(key("pk_" + owner).pub),
        "client_hosted": False,
        "issued_ms": 1_790_000_000_000,
        **(extra or {}),
    }
    return signed_object("ws_delegation", o, signer or "pk_" + owner)


def pid(person: str) -> str:
    return "p_" + person_id(key("pk_" + person).pub)


def did(dev: str) -> str:
    return "d_" + device_id(key("dk_" + dev).pub)


# --- signing bytes (ticket-format 5.3, 5.5) -----------------------------------------------------------------

PERSON_EXCLUDED = ("seq", "at", "prev", "ws_seq", "sig", "host_sig")


def person_bytes(workspace_id: str, log: str, event: dict[str, Any], *, contract: int = 1, suite: int = 2,
                 label: str | None = None) -> bytes:  # fmt: skip
    e = {k: v for k, v in event.items() if k not in PERSON_EXCLUDED}
    lab = label or ("ws_event" if log == "workspace" else "ticket_event")
    ctx = {"contract": contract, "suite": suite, "workspace_id": workspace_id, "log": log, "event": e}
    return LABEL[lab].encode() + cj(ctx)


def host_bytes(workspace_id: str, log: str, event: dict[str, Any], *, contract: int = 1) -> bytes:
    e = {k: v for k, v in event.items() if k != "host_sig"}
    ctx = {"contract": contract, "suite": 2, "workspace_id": workspace_id, "log": log, "event": e}
    return LABEL["host_event"].encode() + cj(ctx)


# --- the world ----------------------------------------------------------------------------------------------


def ulid(n: int) -> str:
    s = ""
    for _ in range(12):
        s = B32[n % 32] + s
        n //= 32
    return "01J9ZK" + "0" * 8 + s


def stamp(epoch: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


T0 = 1_791_626_400  # 2026-10-10T10:00:00Z
TICKET = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
TICKET2 = "01J9ZK4Q7M3R8T2V6X0B5N1C9E"


class World:
    """Builds a scenario: every ``ev`` call makes one complete, signed event and records a step."""

    def __init__(self, name: str, pins: str, workspace_id: str = W, wsk: str = "wsk") -> None:
        self.name, self.pins, self.workspace_id, self.wsk = name, pins, workspace_id, wsk
        self.logs: dict[str, list[dict[str, Any]]] = {"workspace": []}
        self.steps: list[dict[str, Any]] = []
        self.n = 0
        self.clock = T0
        self.roster_v = 0
        self.dev: dict[str, str] = {}  # person -> the device the person signs with
        self.genesis: str | None = None

    # -- helpers
    def actor(self, person: str, dev: str | None = None) -> dict[str, Any]:
        return {"kind": "person", "id": pid(person), "device": did(dev or self.dev[person])}

    def agent(self, person: str, grant: str, session: str = "s_01J9ZK00000000000000000S01") -> dict[str, Any]:
        return {"kind": "agent", "id": "claude-code", "session": session, "for": pid(person), "grant": grant}

    HOST = {"kind": "host"}

    def tick(self, seconds: int = 10) -> str:
        self.clock += seconds
        return stamp(self.clock)

    def nid(self) -> str:
        self.n += 1
        return ulid(self.n)

    def ws_len(self) -> int:
        return len(self.logs["workspace"])

    # -- events
    def ev(self, log: str, typ: str, actor: dict[str, Any], payload: dict[str, Any] | None = None, *,
           signer: str | None = None, expect: str = "ok", note: str | None = None, at: str | None = None,
           roster_v: int | None = None, ws_seq: int | None = None, eid: str | None = None,
           ctx_workspace: str | None = None, ctx_log: str | None = None, ctx_label: str | None = None,
           ctx_contract: int = 1, ctx_suite: int = 2, host_key: str | None = None, host_ctx_log: str | None = None,
           host_ctx_workspace: str | None = None, tamper: Callable[[dict[str, Any]], None] | None = None,
           tamper_after_host: Callable[[dict[str, Any]], None] | None = None, after: dict[str, Any] | None = None,
           schema_refused: bool = False) -> dict[str, Any]:  # fmt: skip
        """Build, sign and (when ``expect == "ok"``) append one event. ``signer`` is the device key that signs a
        person event (default: the actor's own); ``ctx_*`` change what the *person* signs over; ``tamper`` changes the
        event after the person signed and before the host signs; ``host_*`` and ``tamper_after_host`` do the same for
        ``host_sig``. A refused step keeps its place in ``steps`` but not in the chain."""
        events = self.logs.setdefault(log, [])
        prev = head(events[-1]) if events else None
        e: dict[str, Any] = {
            "v": 2,
            "id": eid or self.nid(),
            "seq": len(events) + 1,
            "at": at or self.tick(),
            "type": typ,
            "actor": copy.deepcopy(actor),
            "based_on": prev,
            "prev": prev,
            "hash_v": 1,
        }
        if log != "workspace":
            e["ws_seq"] = self.ws_len() if ws_seq is None else ws_seq
        person = actor["kind"] == "person"
        if person:
            e["auth"] = "passphrase"
            e["roster_v"] = (0 if typ == "workspace.created" else self.roster_v) if roster_v is None else roster_v
        e.update(copy.deepcopy(payload or {}))
        if person:
            who = signer or "dk_" + _dev_name(actor["device"])
            msg = person_bytes(
                ctx_workspace or self.workspace_id, ctx_log or log, e, contract=ctx_contract, suite=ctx_suite,
                label=ctx_label,
            )  # fmt: skip
            e["sig"] = b64u(sign(who, msg))
        if tamper:
            tamper(e)
        hlog = host_ctx_log or log
        e["host_sig"] = b64u(sign(host_key or self.wsk, host_bytes(host_ctx_workspace or self.workspace_id, hlog, e)))
        if tamper_after_host:
            tamper_after_host(e)
        step: dict[str, Any] = {"log": log, "event": e, "expect": expect}
        if note:
            step["note"] = note
        if after:
            step["after"] = after
        if schema_refused:
            step["schema_refused"] = True  # orch.schema refuses the event before the model sees it
        self.steps.append(step)
        if expect == "ok":
            events.append(e)
            if log == "workspace":
                if typ == "workspace.created":
                    self.genesis = head(e)
                if typ in ("workspace.created", "member.added", "member.removed", "role.changed"):
                    self.roster_v += 1
        return e

    # -- the common prefix
    def genesis_event(self, owner: str = "sev", dev: str = "sev1", **kw: Any) -> dict[str, Any]:
        self.dev[owner] = dev
        payload = {
            "workspace_id": self.workspace_id,
            "prefix": "DEMO",
            "host_id": "h_01J9ZK00000000000000000H01",
            "wsk_pub": key(self.wsk).pub_b64u,
            "owner": {"person": pid(owner), "name": owner.capitalize(), "pk_pub": key("pk_" + owner).pub_b64u},
            "delegation": make_delegation(owner),
            "device_cert": make_cert(owner, dev),
        }
        return self.ev("workspace", "workspace.created", self.actor(owner, dev), payload, **kw)

    def member(self, person: str, role: str, dev: str | None = None, by: str = "sev", **kw: Any) -> dict[str, Any]:
        dev = dev or person + "1"
        self.dev[person] = dev
        payload = {
            "person": pid(person),
            "name": person.capitalize(),
            "role": role,
            "pk_pub": key("pk_" + person).pub_b64u,
            "device_cert": make_cert(person, dev),
        }
        return self.ev("workspace", "member.added", self.actor(by), payload, **kw)

    def ticket(self, uid: str = TICKET, owner: str = "sev", key_: str = "DEMO-0001", ty: str = "feature", **kw: Any):
        payload = {"key": key_, "ticket_type": ty, "title": "A ticket", "owner": pid(owner)}
        return self.ev(uid, "ticket.created", self.actor(owner), payload, **kw)

    # -- output
    def scenario(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": self.name,
            "pins": self.pins,
            "workspace_id": self.workspace_id,
            "genesis": self.genesis,
            "now": stamp(self.clock + 60),
            **extra,
            "steps": self.steps,
        }


def _dev_name(device_ref: str) -> str:
    """The key name (``sev1``) of a ``d_...`` device id, looked up among the keys made so far."""
    for name, k in _KEYS.items():
        if name.startswith("dk_") and "d_" + device_id(k.pub) == device_ref:
            return name[3:]
    raise KeyError(device_ref)
