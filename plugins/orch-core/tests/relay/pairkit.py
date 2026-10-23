"""R6c test kit: a pairing engine over a fresh ``membership/`` with fake host, relay and membership thread, a
reference phone (pair requests and answer opening, proto §8.3) and a reference primary (PK-signed challenge,
certificate and rejection, made with the owner's corpus key). Never a real relay."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import certs, pairing
from orch.relay.r6.pair import (
    GateRefused,
    PairContext,
    PairEngine,
    PairRefused,
    PairRelayError,
    PrimaryInfo,
)
from orch.relay.r6.pairstore import PairStore
from orch.wsproto import sign, wire

from . import corpus
from .helpers import WS, WSK_PUB, Key, MemLedger

T0 = 1_790_000_000_000
ORIGIN = "https://relay.dev.severin.io"
OWNER_PRIV = corpus.priv("person_alice.sig")
OWNER_PK = corpus.pub("person_alice.sig")
OWNER = corpus.IDS["person_alice"]
PRIMARY_DEV = corpus.IDS["device_primary"]


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class Runner:
    """The pair thread's ``post``, run by hand: jobs run after the current one ends (never re-entrant)."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], None]] = []

    def __call__(self, fn: Callable[[], None]) -> None:
        self.jobs.append(fn)

    def run(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


class FakeHost:
    def __init__(self) -> None:
        self.gate_error: GateRefused | None = None
        self.gates: list[tuple[str, dict[str, str]]] = []
        self.decl: tuple[Any, Any] = (None, None)
        self.revoked_ids: set[str] = set()
        self.primary_kx = crypto.generate_private_key()
        self.has_primary = True
        self.blocked: PairRefused | None = None
        self.card = 2
        self.cp: dict[str, Any] | None = {"n": 7, "hash": "sha256:" + "ab" * 32}

    def gate(self, meta, offer_id, who):
        self.gates.append((offer_id, dict(who)))
        if self.gate_error is not None:
            raise self.gate_error
        return self.decl

    def revoked(self, device_id: str) -> bool:
        return device_id in self.revoked_ids

    def primary(self):
        if not self.has_primary:
            return None
        return PrimaryInfo(PRIMARY_DEV, crypto.public_bytes(self.primary_kx), "MacBook Pro")

    def offer_blocked(self):
        return self.blocked

    def card_seq(self):
        return self.card

    def checkpoint(self):
        return self.cp

    #: the host's view of the log for the recorded intent (the store lock is the caller's in production)
    landed_fn: Callable[[Any], bool] = staticmethod(lambda intent: False)

    def settle(self, fn):
        self.settled = getattr(self, "settled", 0) + 1
        return fn(self.landed_fn)


class FakeRelay:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str, bytes]] = []
        self.requests: list[dict[str, Any]] = []
        self.request_state: dict[str, Any] | None = None
        self.devices: list[tuple[str, dict[str, Any]]] = []
        self.baselines: list[tuple[str, str]] = []
        self.fail: dict[str, str] = {}

    def _maybe(self, route: str) -> None:
        kind = self.fail.get(route)
        if kind is not None:
            raise PairRelayError(kind)

    def post_answer(self, offer_id, rid, envelope):
        self.answers.append((offer_id, rid, envelope))
        self._maybe("answer")

    def post_cert_request(self, person_id, signed):
        assert person_id == signed["o"]["person_id"]
        self._maybe("request")
        self.requests.append(signed)

    def get_cert_request(self, request_id):
        self._maybe("get")
        return self.request_state or {"state": "open", "challenge": None, "cert": None, "rejection": None}

    def put_device(self, device_id, cert):
        self._maybe("device")
        self.devices.append((device_id, cert))

    def post_baseline(self, hash_, offer_id):
        self._maybe("baseline")
        self.baselines.append((hash_, offer_id))


class FakeMembership:
    """A fake membership thread: never blocks; the test commits or refuses through the bound events."""

    def __init__(self, ready: bool = True) -> None:
        self.events: Any = None
        self.is_ready = ready
        self.additions: list[tuple[str, str]] = []
        self.exclusions: list[tuple[str, str, str]] = []

    def bind(self, events):
        self.events = events

    def ready(self):
        return self.is_ready

    def post_addition(self, offer_id, device_id, cert):
        self.additions.append((offer_id, device_id))

    def post_exclusion(self, offer_id, device_id, reason):
        self.exclusions.append((offer_id, device_id, reason))


class Kit:
    def __init__(self, tmp: Path, *, membership: Any = None, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        (tmp / "membership").mkdir(mode=0o700, exist_ok=True)  # R6a's key store makes it in production
        self.store = PairStore(tmp / "membership")
        self.host, self.relay = FakeHost(), FakeRelay()
        self.membership = membership if membership is not None else FakeMembership()
        self.runner = Runner()
        self.ledger = MemLedger()
        self.ctx = PairContext(WS, ORIGIN, WSK_PUB, OWNER_PK)
        self.engine = PairEngine(
            self.ctx, self.store, None, self.host, self.relay, self.membership, now_ms=self.clock, post=self.runner,
        )  # fmt: skip
        eng = self.engine
        self.engine.signer = sign.RelaySigner(
            Key(), sign.LinkContext(WS, ORIGIN), ledger=self.ledger, owner_person_id=OWNER,
            primary_device=lambda: PRIMARY_DEV if self.host.has_primary else None,
            pair_live=lambda o: eng.is_live(o), pair_binding=lambda *b: eng.binding_held(*b),
        )  # fmt: skip

    def close(self) -> None:
        self.store.close()

    def rec(self, offer_id: str) -> dict[str, Any]:
        r = self.store.get(offer_id)
        assert r is not None
        return r

    def last_answer(self, phone: Phone) -> tuple[dict[str, Any], int]:
        offer, rid, env = self.relay.answers[-1]
        return phone.open_answer(env), wire.parse_header(env[: wire.HEADER_LEN]).flags


class Phone:
    """The reference phone of proto §8.3: fresh keys per attempt, the link's ``S``, requests and answer checks."""

    def __init__(self, link: str, *, attempt: str | None = None, sig=None, kx=None) -> None:
        parsed = pairing.parse_pair_link(link)
        self.offer, self.secret = parsed.fields.offer_id, parsed.fields.secret
        self.ws = parsed.fields.workspace_id
        self.sig = sig or crypto.generate_private_key()
        self.kx = kx or crypto.generate_private_key()
        self.attempt = attempt or secrets.token_hex(16)
        self.wsk_pin = parsed.fields.wsk_pin

    @property
    def sig_pub(self) -> bytes:
        return crypto.public_bytes(self.sig)

    @property
    def kx_pub(self) -> bytes:
        return crypto.public_bytes(self.kx)

    @property
    def device_id(self) -> str:
        return crypto.hex_id(crypto.device_id(self.sig_pub))

    def meta(self, *, label: str = "Severin's iPhone", cert: Any = None, **extra: Any) -> dict[str, Any]:
        return {"op": "pair", "dk_sig_pub": crypto.b64u(self.sig_pub), "dk_kx_pub": crypto.b64u(self.kx_pub),
                "label": label, "cert": cert, "attempt": self.attempt, **extra}  # fmt: skip

    def envelope(self, meta: dict[str, Any], *, ts: int, rid: str | None = None, signer=None) -> tuple[bytes, str]:
        rid = rid or secrets.token_hex(16)
        hdr = wire.Header(
            2, 1, 0, crypto.SUITE_ID, bytes.fromhex(self.ws), crypto.device_id(self.sig_pub), bytes.fromhex(rid),
            bytes.fromhex(self.offer), 1, ts, 0, secrets.token_bytes(16),
        ).pack()  # fmt: skip
        k = wire.k_pair(self.secret, bytes.fromhex(self.ws), bytes.fromhex(self.offer))
        body = wire.seal_body(k, hdr, wire.build_plaintext(meta, b""))
        sig = crypto.sign(signer or self.sig, wire.bridge_signed_bytes(hdr, body))
        return wire.build_envelope(hdr, body, sig), rid

    def status(self, *, ts: int) -> tuple[bytes, str]:
        return self.envelope({"op": "pair_status"}, ts=ts)

    def open_answer(self, env: bytes) -> dict[str, Any]:
        h, body, sig = wire.parse_envelope(env, direction=2)
        assert h.epoch == 0 and h.stream.hex() == self.offer and h.seq == 0
        k = wire.k_pair(self.secret, h.workspace, h.stream)
        meta, data = wire.parse_plaintext(wire.open_body(k, env[: wire.HEADER_LEN], body))
        assert data == b""
        wsk = crypto.unb64u(meta["wsk_pub"])
        assert crypto.wsk_pin(wsk) == self.wsk_pin
        assert crypto.verify(wsk, sig, wire.bridge_signed_bytes(env[: wire.HEADER_LEN], body))
        return meta


def pk_sign(label: str, o: dict[str, Any], priv: Any = None) -> dict[str, Any]:
    return {"o": o, "sig": crypto.b64u(crypto.sign(priv or OWNER_PRIV, L[label] + canon.dumps(o)))}


def make_challenge(request_o: dict[str, Any], phone: Phone, priv: Any = None) -> dict[str, Any]:
    o = {
        "v": 2, "suite": crypto.SUITE_ID, "kind": "cert_challenge", "request_id": request_o["request_id"],
        "workspace_id": request_o["workspace_id"], "person_id": request_o["person_id"],
        "offer_id": request_o["offer_id"], "attempt": request_o["attempt"],
        "device_id": request_o["device_id"], "dk_sig_pub": crypto.b64u(phone.sig_pub),
        "dk_kx_pub": crypto.b64u(phone.kx_pub), "primary_nonce": crypto.b64u(secrets.token_bytes(32)),
        "expires_ms": request_o["expires_ms"],
    }  # fmt: skip
    return pk_sign("sig_cert_challenge", o, priv)


def make_rejection(request_o: dict[str, Any]) -> dict[str, Any]:
    o = {k: request_o[k] for k in ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id",
                                     "dk_hash", "expires_ms")}  # fmt: skip
    return pk_sign("sig_cert_rejection", {"v": 2, "suite": crypto.SUITE_ID, "kind": "cert_rejection", **o})


def make_cert(
    phone: Phone, *, scopes=("look", "decide"), created_ms: int = T0 - 1000, pk_priv=None, expires_ms=None
) -> dict[str, Any]:
    priv = pk_priv or OWNER_PRIV
    pk = crypto.public_bytes(priv)
    return certs.make_device_cert(
        pk, lambda payload: crypto.sign(priv, payload), dk_sig_pub=phone.sig_pub, dk_kx_pub=phone.kx_pub,
        label_sealed=secrets.token_bytes(40), created_ms=created_ms, expires_ms=expires_ms, scopes_max=list(scopes),
    )  # fmt: skip
