"""V3 C13: the host's space ops on a served workspace joined (full and selective) to a TLS test hub that plays the
hub's side of contract 10.4 with its own op key: what is verified, what is refused locally, and who may do what."""

from __future__ import annotations

import os

import pytest

from orch import crypto
from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.sealing import seal
from orch.host import peerops
from orch.hubclient import spaces as SP
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis, make_card, now_ms
from tests.host.test_hub_ops import keystore  # noqa: F401  (the epoch key store a full join seals)
from tests.host.test_peer_ops import PEER_CARD, PEER_OWNER_PK, PEER_WS
from tests.hub.tlshub import TlsHub
from tests.wsproto.corpus import priv

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class SpaceHub:
    """The hub's side of 10.4 for ONE space of two members (the lab workspace and PEER_WS), signed with the test hub's
    op key. ``entries`` are what ``space_get`` serves; ``puts`` records what ``space_put`` received."""

    def __init__(self, hub: TlsHub, card: dict, me: str, wxk_pub: bytes) -> None:
        self.hub, self.me, self.wxk_pub = hub, me, wxk_pub
        self.op_cert_id = card["op_cert"]["o"]["cert_id"]
        self.space_id = os.urandom(16).hex()
        self.members = sorted([me, PEER_WS])
        self.epoch = 1
        self.sk = os.urandom(32)
        self.entries: list[dict] = []
        self.puts: list[dict] = []
        self.accepts: list[dict] = []

    def _signed(self, kind: str, label: bytes, **f) -> dict:
        o = {"v": 2, "suite": 2, "kind": kind, "hv": 0, "hub_id": self.hub.hub_id, "op_cert_id": self.op_cert_id,
             "space_id": self.space_id, "epoch": self.epoch, "dev": True, **f}  # fmt: skip
        return {"o": o, "sig": crypto.b64u(suite.sign(self.hub.hk_op, label + cj(o)))}

    def record(self) -> dict:
        return self._signed("hub.space", SP.SIG_SPACE, members=self.members,
                            sk_commit=SP.sk_commit(self.space_id, self.epoch, self.sk), issued_ms=1)  # fmt: skip

    def wrap(self) -> dict:
        blob = seal(self.wxk_pub, bytes.fromhex(self.me), "space-sk", bytes.fromhex(self.space_id), self.epoch, self.sk,
                    bytes.fromhex(self.hub.hub_id) + (1).to_bytes(4, "big"))  # fmt: skip
        return self._signed("hub.space_wrap", SP.SIG_SPACE_WRAP, workspace_id=self.me, wxk_version=1,
                            sealed=crypto.b64u(blob))  # fmt: skip

    def peer_item(self, name: str, data: bytes, created_ms: int = 5, by: str = "person:p_peer") -> None:
        item_id, signed = SP.build_item(
            hub_id=self.hub.hub_id, space_id=self.space_id, epoch=self.epoch, members=self.members,
            workspace_id=PEER_WS, body={"name": name, "size": len(data), "data": crypto.b64u(data), "by": by},
            created_ms=created_ms, dev=True, sign=lambda m: suite.sign(priv("wsk_b.sig"), m),
        )  # fmt: skip
        sealed = SP.seal_item(self.sk, self.space_id, self.epoch, item_id, signed)
        self.entries.append({"seq": len(self.entries) + 1, "item_id": item_id, "epoch": self.epoch, "revoked": False,
                             "sealed": sealed})  # fmt: skip

    def handle(self, h: TlsHub, o: dict):
        op, body = o["op"], o["body"]
        if op == "space_list":
            meta = {self.space_id: {"name": "Team notes", "kind": "wiki"}}
            return h.reply(o, body={"spaces": [self.record()], "meta": meta}, ts_ms=o["ts_ms"])
        if op == "space_get":
            es = [e for e in self.entries if e["seq"] > body["since"]]
            return h.reply(o, ts_ms=o["ts_ms"], body={"space": self.record(), "wraps": [self.wrap()], "entries": es,
                                                      "next": es[-1]["seq"] if es else body["since"]})  # fmt: skip
        if op == "space_put":
            self.puts.append(body)
            return h.reply(o, body={"seq": 100 + len(self.puts)}, ts_ms=o["ts_ms"])
        if op == "space_accept":
            self.accepts.append(body["accept"])
        if op == "space_revoke":
            return h.reply(o, body={"seq": 7}, ts_ms=o["ts_ms"])
        return h.reply(o, body={"state": "queued"} if op == "route_out" else {}, ts_ms=o["ts_ms"])


def _setup(served, human, mode, keys=None):
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    holder: list[SpaceHub] = []
    hub = TlsHub(wsk_pub=wsk, on_request=lambda h, o: holder[0].handle(h, o))
    hub.workspace_id = served.wid
    hub.start()
    card = make_card(hub, now_ms(served))
    service = next(iter(served.h.host.services.values()))
    human.run("hub.join", {"hub_card": card, "mode": mode})
    # the host's published WXK: created with the local card on first use
    from orch.host import hubops

    hubops.workspace_dev  # noqa: B018 - patched by the autouse fixture below
    key = peerops._wxk(service)  # noqa: SLF001
    sh = SpaceHub(hub, card, served.wid, suite.public_bytes(key))
    holder.append(sh)
    args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{hub.port}"}
    assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
    return hub, sh


@pytest.fixture(autouse=True)
def no_log_sync_sender(monkeypatch):
    """These tests are about spaces over the one shared link; the stub hub does not speak ``log_sync``."""
    from orch.host import logsyncops

    monkeypatch.setattr(logsyncops, "start", lambda service: False)


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def full(human, served, keystore):  # noqa: F811
    hub, sh = _setup(served, human, "full")
    try:
        yield sh
    finally:
        for svc in served.h.host.services.values():
            peerops.stop(svc)
        hub.close()


@pytest.fixture
def selective(human, served):
    hub, sh = _setup(served, human, "selective")
    try:
        yield sh
    finally:
        hub.close()


def req(served, op, args=None, **kw):
    return served.client().req(op, args or {}, idem=True if op not in ("space.list", "space.get") else None, **kw)


def pin_peer(served):
    assert ok(req(served, "peer.add", {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK}))["data"]["pinned"]


def test_the_ops_are_declared():
    from orch.host import spaceops

    assert spaceops.SPACE_READS == {"space.list", "space.get", "space.history"}
    assert set(spaceops.SPACE_OPS) == {
        "space.list",
        "space.get",
        "space.history",
        "space.share",
        "space.agents",
        "space.put",
        "space.restore",
        "space.revoke",
        "space.accept",
    }


def test_without_a_hub_every_space_op_is_hub_not_joined(served):
    assert err(req(served, "space.list")) == "hub.not_joined"


@pytest.mark.parametrize("mode", ["full", "selective"])
def test_list_share_put_and_get_in_both_modes(served, request, mode):
    sh = request.getfixturevalue(mode)
    pin_peer(served)
    rows = ok(req(served, "space.list"))["data"]["spaces"]
    assert rows == [
        {
            "space_id": sh.space_id,
            "epoch": 1,
            "members": sh.members,
            "shared": False,
            "key": False,
            "name": "Team notes",
            "kind": "wiki",
        }  # fmt: skip
    ]
    put_args = {"space_id": sh.space_id, "name": "report.txt", "data": crypto.b64u(b"hello")}
    res = req(served, "space.put", put_args)
    assert err(res) == "invalid.input" and "not approved" in res["error"]["message"] and sh.puts == []
    # the owner approves exactly what they were shown; a different list is refused and stores nothing
    bad = req(served, "space.share", {"space_id": sh.space_id, "members": [served.wid], "understood": True})
    assert err(bad) == "invalid.input" and "not the one you approved" in bad["error"]["message"]
    ok(req(served, "space.share", {"space_id": sh.space_id, "members": sh.members, "understood": True}))
    got = ok(req(served, "space.put", put_args))["data"]
    assert got["epoch"] == 1 and len(sh.puts) == 1
    sent = sh.puts[0]  # what the hub received opens with SK_e and is signed by OUR wsk
    pub = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    o = SP.verify_item(sent["sealed"], sk=sh.sk, space_id=sh.space_id, epoch=1, item_id=sent["item_id"],
                       members=sh.members, wsk_pubs={served.wid: pub}, hub_id=sh.hub.hub_id, dev_mode=True)  # fmt: skip
    assert o["body"]["name"] == "report.txt" and crypto.unb64u(o["body"]["data"]) == b"hello"
    # a co-member's item: verified against OUR pin of it, delivered as untrusted data
    sh.peer_item("plan.md", b"from the peer")
    item = ok(req(served, "space.get", {"space_id": sh.space_id, "name": "plan.md"}))["data"]
    assert crypto.unb64u(item["data"]) == b"from the peer" and item["author"] == PEER_WS and item["untrusted"] is True


def test_an_author_without_a_pin_is_refused_and_counted(served, full):
    full.peer_item("x.txt", b"?")
    res = req(served, "space.get", {"space_id": full.space_id, "name": "x.txt"})
    assert err(res) == "not_found"
    listing = ok(req(served, "space.list", {"space_id": full.space_id}))["data"]
    assert listing["items"] == [] and listing["refused"] == 1


def test_a_grown_member_set_stops_the_put_before_anything_is_sent(served, full):
    ok(req(served, "space.share", {"space_id": full.space_id, "members": full.members, "understood": True}))
    full.members = sorted([*full.members, "cc" * 16])
    full.epoch = 2
    res = req(served, "space.put", {"space_id": full.space_id, "name": "n", "data": crypto.b64u(b"1")})
    assert err(res) == "invalid.input" and "members_changed" in res["error"]["message"] and full.puts == []


def test_selective_accept_signs_exactly_one_transition(served, selective):
    both = sorted([served.wid, PEER_WS])
    selective.members = [PEER_WS]  # not a member yet: the hub does not list the space for us
    args = {"space_id": selective.space_id, "epoch": 2, "prev_members": [PEER_WS], "members": both}
    ok(req(served, "space.accept", args))
    (acc,) = selective.accepts
    o = acc["o"]
    assert (o["space_id"], o["workspace_id"], o["epoch"]) == (selective.space_id, served.wid, 2)
    assert o["prev_members_hash"] == SP.members_hash([PEER_WS]) and o["members_hash"] == SP.members_hash(both)
    pub = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    assert suite.verify(pub, crypto.unb64u(acc["sig"], 64), b"orch/v2/sig/space-accept|" + cj(o))


def test_accept_refuses_what_is_not_the_transition_of_adding_this_workspace(served, selective):
    both = sorted([served.wid, PEER_WS])
    selective.members = [PEER_WS]
    base = {"space_id": selective.space_id, "epoch": 2}
    for prev, now in (
        ([PEER_WS], both + ["cc" * 16]),  # someone else is added in the same breath
        ([PEER_WS], [PEER_WS]),  # not even us
        ([PEER_WS, served.wid], both),  # we are already in the "before"
    ):
        res = req(served, "space.accept", {**base, "prev_members": prev, "members": sorted(now)})
        assert err(res) == "hub.refused" or err(res) == "invalid.input"
    assert selective.accepts == []
    selective.members = both  # a member already: nothing to accept
    res = req(served, "space.accept", {**base, "prev_members": [PEER_WS], "members": both})
    assert err(res) == "invalid.input" and "already a member" in res["error"]["message"] and selective.accepts == []


def test_an_oversize_file_is_refused_before_anything_is_signed(served, full):
    ok(req(served, "space.share", {"space_id": full.space_id, "members": full.members, "understood": True}))
    big = crypto.b64u(b"x" * (190 * 1024 + 1))
    res = req(served, "space.put", {"space_id": full.space_id, "name": "big", "data": big})
    assert err(res) == "invalid.input" and "too large" in res["error"]["message"] and full.puts == []


def test_an_agent_never_shares_switches_revokes_or_accepts(served, full):
    assert ok(req(served, "space.list", grant=served.grant, session=SESSION))["data"]["spaces"]
    sid = full.space_id
    for op, args in (
        ("space.share", {"space_id": sid, "members": full.members, "understood": True}),
        ("space.agents", {"space_id": sid, "on": True}),
        ("space.revoke", {"space_id": sid, "item_id": "ab" * 16}),
        ("space.accept", {"space_id": sid, "epoch": 2, "prev_members": [], "members": full.members}),
    ):
        assert err(served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)) == "op.not_allowed"
    assert full.puts == [] and full.accepts == []


def test_names_are_per_author_and_the_hubs_seq_decides_nothing(served, full):
    pin_peer(served)
    ok(req(served, "space.share", {"space_id": full.space_id, "members": full.members, "understood": True}))
    ok(req(served, "space.put", {"space_id": full.space_id, "name": "plan.md", "data": crypto.b64u(b"mine")}))
    # the put went to the hub; play the hub storing it, then the peer shadows the name with its own file
    sent = full.puts[0]
    full.entries.append({"seq": 5, "item_id": sent["item_id"], "epoch": 1, "revoked": False, "sealed": sent["sealed"]})
    full.peer_item("plan.md", b"theirs")
    get = {"space_id": full.space_id, "name": "plan.md"}
    res = req(served, "space.get", get)
    assert err(res) == "invalid.input" and "ambiguous: 2 authors" in res["error"]["message"]
    assert PEER_WS in res["error"]["message"] and served.wid in res["error"]["message"]
    mine = ok(req(served, "space.get", {**get, "from": served.wid}))["data"]
    theirs = ok(req(served, "space.get", {**get, "from": PEER_WS}))["data"]
    assert crypto.unb64u(mine["data"]) == b"mine" and crypto.unb64u(theirs["data"]) == b"theirs"
    # the hub reorders its cursor: nothing changes, and a newer item of the same author wins by its signed clock
    full.entries[0]["seq"], full.entries[1]["seq"] = 9, 1
    again = ok(req(served, "space.get", {**get, "from": PEER_WS}))["data"]
    assert crypto.unb64u(again["data"]) == b"theirs"
    full.peer_item("plan.md", b"theirs v2", created_ms=50)
    full.entries[1]["seq"] = 8  # the old one now has the higher seq, the new one a low seq but the newer clock
    full.entries[-1]["seq"] = 2
    newest = ok(req(served, "space.get", {**get, "from": PEER_WS}))["data"]
    assert crypto.unb64u(newest["data"]) == b"theirs v2"
