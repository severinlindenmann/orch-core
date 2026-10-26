"""Builders for identity tests: real keys, a signed genesis, certificates, events."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from orch import canon, crypto
from orch.identity import certs

WS = "705d40abbb8c1c90354a1acaa94c935c"
NOW = 1_790_000_000_000
ULID = "01J9ZP0000000000000000000A"


class Person:
    """A person with a person key and one device (all software keys, in memory)."""

    def __init__(self):
        self.pk = crypto.generate_private_key()
        self.pk_pub = crypto.public_bytes(self.pk)
        self.dk_sig = crypto.generate_private_key()
        self.dk_kx = crypto.generate_private_key()
        self.sig_pub = crypto.public_bytes(self.dk_sig)
        self.kx_pub = crypto.public_bytes(self.dk_kx)

    def pk_sign(self, payload: bytes) -> bytes:
        return crypto.sign(self.pk, payload)

    def dk_sign(self, payload: bytes) -> bytes:
        return crypto.sign(self.dk_sig, payload)

    @property
    def ref(self) -> str:
        return certs.person_ref(self.pk_pub)

    @property
    def device(self) -> str:
        return certs.device_ref(self.sig_pub)

    def cert(self, *, scopes=("look", "decide", "operate", "type"), created=NOW, expires=None, label=b"lbl") -> dict:
        return certs.make_device_cert(
            self.pk_pub,
            self.pk_sign,
            dk_sig_pub=self.sig_pub,
            dk_kx_pub=self.kx_pub,
            label_sealed=label,
            created_ms=created,
            expires_ms=expires,
            scopes_max=list(scopes),
        )


@dataclass
class Genesis:
    owner: Person
    wsk: object
    event: dict
    keys: dict = field(default_factory=dict)


def sign_person(person: Person, event: dict, log: str = "workspace", ws: str = WS) -> None:
    event["sig"] = crypto.b64u(person.dk_sign(canon.person_signing_bytes(ws, log, event)))


def host_sign(wsk, event: dict, log: str = "workspace", ws: str = WS) -> None:
    event.pop("host_sig", None)
    event["host_sig"] = crypto.b64u(crypto.sign(wsk, canon.host_signing_bytes(ws, log, event)))


def build_genesis(owner: Person | None = None, wsk=None, *, client_hosted=False) -> Genesis:
    owner = owner or Person()
    wsk = wsk or crypto.generate_private_key()
    wsk_pub = crypto.public_bytes(wsk)
    delegation = certs.make_delegation(
        owner.pk_pub, owner.pk_sign, workspace_id=WS, wsk_pub=wsk_pub, client_hosted=client_hosted, issued_ms=NOW
    )
    ev = {
        "v": 2,
        "id": ULID,
        "seq": 1,
        "at": "2026-10-10T10:00:00Z",
        "type": "workspace.created",
        "actor": {"kind": "person", "id": owner.ref, "device": owner.device},
        "based_on": None,
        "prev": None,
        "hash_v": 1,
        "roster_v": 0,
        "auth": "passphrase",
        "workspace_id": WS,
        "prefix": "DEMO",
        "host_id": "h_" + ULID,
        "wsk_pub": crypto.b64u(wsk_pub),
        "owner": {"person": owner.ref, "name": "Owner", "pk_pub": crypto.b64u(owner.pk_pub)},
        "delegation": delegation,
        "device_cert": owner.cert(),
    }
    sign_person(owner, ev)
    host_sign(wsk, ev)
    return Genesis(owner, wsk, ev)


def resign(g: Genesis, ev: dict, *, person=True, host=True) -> dict:
    ev = copy.deepcopy(ev)
    if person:
        sign_person(g.owner, ev)
    if host:
        host_sign(g.wsk, ev)
    return ev
