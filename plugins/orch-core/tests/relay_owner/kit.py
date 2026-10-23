"""Test kit for ``orch.relay_owner``: vector device signers, a loopback client against the recorded-fixture relay,
and the fixtures' request checks. The signed origin is the fixtures' (``V_ORIGIN``); every byte goes to the loopback
mock relay (no external network)."""

from __future__ import annotations

import json
from typing import Any

from orch import crypto
from orch.crypto.labels import L
from orch.net import http as rhttp
from orch.relay_owner import OwnerClient
from orch.wsproto import wire

from ..relay import corpus
from ..relay.helpers import V_ORIGIN
from ..relay.mock import Expect, MockRelay, fixture_expect, json_reply

OPEN: list[rhttp.RelayConn] = []
IDS = corpus.IDS
PERSON = IDS["person_alice"]
PRIMARY = IDS["device_primary"]
PHONE = IDS["device_phone"]
PK = corpus.pub("person_alice.sig")


class VecSigner:
    """A vector device key behind the :class:`DeviceSigner` interface; like custody's ``PrimarySession`` it signs
    relay logins only."""

    def __init__(self, name: str = "primary") -> None:
        self._key = corpus.priv(f"{name}.sig")
        self.dk_sig_pub = corpus.pub(f"{name}.sig")
        self.device_id = crypto.device_id(self.dk_sig_pub)
        self.signed: list[bytes] = []

    def sign(self, payload: bytes) -> bytes:
        assert payload.startswith(L["sig_relay_auth"]), "the primary signs relay logins only"
        self.signed.append(payload)
        return crypto.sign(self._key, payload)


def client(relay: MockRelay, signer: Any = None, **kw: Any) -> OwnerClient:
    conn = rhttp.RelayConn(relay.origin())
    OPEN.append(conn)
    return OwnerClient(V_ORIGIN, signer or VecSigner(), transport=conn, **kw)


def challenge_of(name: str = "session_challenge", case: str = "success") -> bytes:
    return crypto.unb64u(corpus.case(name, case)["response"]["json"]["challenge"])


def proof_ok(body: dict[str, Any], device_hex: str, pub: bytes) -> bool:
    """``sig`` verifies over ``auth_bytes(V_ORIGIN, challenge, "device", device)`` under ``pub``."""
    ch = crypto.unb64u(body["challenge"])
    return crypto.verify(pub, crypto.unb64u(body["sig"]), wire.auth_bytes(V_ORIGIN, ch, "device", device_hex))


def login_check(device_hex: str, pub: bytes):
    def check(raw: bytes) -> bool:
        b = json.loads(raw)
        return (
            set(b) == {"challenge", "kind", "id", "sig"}
            and b["kind"] == "device"
            and b["id"] == device_hex
            and proof_ok(b, device_hex, pub)
        )

    return check


def login_expects(token: str = "FAKE-primary", *, signer: VecSigner | None = None, challenge: bytes | None = None):
    """A challenge (the fixture's, or the one given) and a checked device login answered with ``token``."""
    s = signer or VecSigner()
    ch = (
        fixture_expect("session_challenge", "success")
        if challenge is None
        else Expect("POST", "/v2/session/challenge",
                    reply=json_reply(200, {"challenge": crypto.b64u(challenge), "expires_ms": 1_790_000_060_000}))
    )  # fmt: skip
    return [
        ch,
        Expect(
            "POST", "/v2/session/login", {}, {"Content-Type": "application/json"},
            login_check(s.device_id.hex(), s.dk_sig_pub),
            json_reply(200, {"token": token, "expires_ms": 1_790_000_900_000}),
        ),
    ]  # fmt: skip


class VecPK:
    """A vector person key behind the ``PersonKeySource`` surface the builders use (``pk_pub``, ``person_id``,
    ``signer(label)``, ``derive_label_key()``); records every label it signed."""

    def __init__(self, name: str = "person_alice") -> None:
        self._k = corpus.priv(f"{name}.sig")
        self.pk_pub = corpus.pub(f"{name}.sig")
        self.person_id = crypto.person_id(self.pk_pub)
        self.labels: list[str] = []

    def signer(self, label: str):
        def sign(payload: bytes) -> bytes:
            assert payload.startswith(L[label])
            self.labels.append(label)
            return crypto.sign(self._k, payload)

        return sign

    def derive_label_key(self) -> bytes:
        return b"\x07" * 32
