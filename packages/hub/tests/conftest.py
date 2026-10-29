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

# --- #1198 C9: the reference host signs an owner proof (9.6) with every join and every new or mode-switching binding.
# The link tests do not care about it, so the client adds one when a test did not say (a test that names
# ``owner_proof`` itself, e.g. test_owner_proof_link.py, gets exactly that).
_STATE: dict[str, dict] = {}  # workspace_id -> {"pk": key name, "status", "mode", "hub_id", "binding_seq"}
_orig_request = LinkClient.request


def _key_name(pk_b64: str) -> str:
    return next(n for n in G.K.pub if n.startswith("pk_") and b64u(G.K.pub[n]) == pk_b64)


def _proof(o: dict, ws: str, pk: str, op: str, now: int = NOW) -> dict:
    payload = {
        "hub_id": o["hub_id"],
        "hk_root_pub": o["hk_root_pub"],
        "mode": o["mode"],
        "binding_seq": o["binding_seq"],
        "aliases": [{"person_id": G.P_ALICE, "user_id": G.U_ALICE}],
    }
    return G.owner_proof(op, payload, pk=pk, workspace_id=ws, issued_ms=now - 1_000, expires_ms=now + 300_000)


def _request(self, op, body):
    sent = body
    try:
        if op == "join" and "owner_proof" not in body:
            b = body["binding"]["o"]
            pk = _key_name(body["pk_pub"])
            sent = {**body, "owner_proof": _proof(b, b["workspace_id"], pk, "hub.join", self._now())}
        elif op == "binding_put" and "owner_proof" not in body:
            obj = body["object"]
            o = obj["o"]
            proof = None
            if o["kind"] == "hub.binding":
                held = _STATE.get(o["workspace_id"])
                if held is not None and held["status"] == "bound" and held["binding_seq"] == o["binding_seq"]:
                    pass  # an identical re-send takes no proof
                else:
                    switch = (
                        held is not None
                        and held["status"] == "bound"
                        and held["mode"] == "selective"
                        and o["mode"] == "full"
                    )
                    pk = held["pk"] if held is not None else "pk_alice"
                    proof = _proof(o, o["workspace_id"], pk, "hub.mode" if switch else "hub.join", self._now())
            sent = {**body, "owner_proof": proof}
    except KeyError, TypeError, StopIteration:
        sent = body
    out = _orig_request(self, op, sent)
    try:  # remember what the hub accepted
        if op == "join":
            b = body["binding"]["o"]
            _STATE[b["workspace_id"]] = {
                "pk": _key_name(body["pk_pub"]),
                "status": "bound",
                "mode": b["mode"],
                "binding_seq": b["binding_seq"],
            }
        elif op == "binding_put":
            o = body["object"]["o"]
            held = _STATE.setdefault(o["workspace_id"], {"pk": "pk_alice"})
            if o["kind"] == "hub.binding":
                held.update(status="bound", mode=o["mode"], binding_seq=o["binding_seq"])
            else:
                held.update(status="unbound", binding_seq=o["binding_seq"])
    except KeyError, TypeError, StopIteration:
        pass
    return out


LinkClient.request = _request


@pytest.fixture(autouse=True)
def _fresh_proof_state():
    _STATE.clear()
    yield


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


@pytest.fixture(autouse=True)
def _no_real_mcp_probe(monkeypatch):
    """The page's lazy status check must never reach the real network from a test (#1439)."""
    from orch_hub.access import mcpprobe

    monkeypatch.setattr(mcpprobe, "probe", lambda url, **kw: {"status": "failing", "checked_ms": 1, "detail": "dns"})
