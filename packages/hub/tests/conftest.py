"""Fixtures for the link tests: a real LinkServer on 127.0.0.1 (ephemeral port, own thread, closed in teardown) and
a fake host that is the reference LinkClient. Keys and objects come from the contract's vector generator."""

from __future__ import annotations

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.link import LinkClient, LinkServer, Registry, TlsIdentity
from orch_hub.link.ops import StubSnapshots

NOW = G.NOW


class Invites:
    def __init__(self, **bound: str | None) -> None:
        self.codes = dict(bound)  # code -> bound workspace_id or None

    def check(self, invite, now_ms):
        if invite not in self.codes:
            raise H.Refused("bad_invite")
        return self.codes[invite]

    def consume(self, invite, workspace_id):
        self.codes.pop(invite, None)


class Hub:
    def __init__(self, registry=None, **server_kw):
        self.identity = TlsIdentity.generate()
        self.op = KS.OpSigner(G.K.priv["hk_op"])
        self.registry = registry or Registry()
        self.invites = Invites(inv1=None, inv2=None, inv3=None, invB=G.WS_B)
        self.clients: list[LinkClient] = []
        server_kw.setdefault("dev_mode", True)
        self.server = LinkServer(
            identity=self.identity,
            op_signer=self.op,
            hub_id=G.HUB_ID,
            registry=self.registry,
            invites=self.invites,
            snapshots=StubSnapshots(self.op, G.HUB_ID, G.OP_CERT_ID, lambda: NOW),
            now_ms=lambda: NOW,
            **server_kw,
        ).start()

    @property
    def port(self) -> int:
        return self.server.address[1]

    def open(self, ws=G.WS_A, wsk="wsk_a", *, first=False, pin=None, **kw) -> LinkClient:
        c = LinkClient.open(
            "127.0.0.1",
            self.port,
            tls_spki_sha256=pin or self.identity.spki_sha256,
            hub_id=G.HUB_ID,
            workspace_id=ws,
            wsk=KS.WskSigner(G.K.priv[wsk]),
            hk_op_pub=G.K.pub["hk_op"],
            send_wsk_pub=first,
            now_ms=lambda: NOW,
            timeout=5,
            **kw,
        )
        self.clients.append(c)
        return c

    def join_body(self, invite="inv1", ws=G.WS_A, pk="pk_alice", wsk="wsk_a", seq=1, **binding_kw):
        return {
            "invite": invite,
            "card": G.v2_card(ws=ws, pk=pk, wsk=wsk),
            "pk_pub": b64u(G.K.pub[pk]),
            "binding": G.binding(ws=ws, wsk=wsk, seq=seq, **binding_kw),
        }

    def close(self):
        for c in self.clients:
            c.close()
        self.server.close()
        self.registry.close()


@pytest.fixture
def hub():
    h = Hub()
    yield h
    h.close()


@pytest.fixture
def make_hub():
    made = []

    def make(**kw):
        h = Hub(**kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()
