"""V3 C7: ``orch publish`` on a workspace that joined a hub goes through the hub (``publish_out``), end to end on a
served workspace: the hub-path request verifies byte for byte (the contract's own bytes, ported in ``contract.py``),
a hub that is down fails closed (no direct request), and a workspace that left the hub publishes directly again.

The hub here is a stub that checks what the host SENDS (the contract's checks are in ``test_publish_out.py``)."""

from __future__ import annotations

import copy
import json
import re

import pytest

from orch import crypto
from orch.crypto import suite as S
from orch.host import hubops, publishing
from tests.host import human_helpers
from tests.host.dispatch_helpers import err, ok
from tests.host.test_hub_ops import HUB_ID, connect_args, join_args
from tests.host.test_publishing import LINK, REPORT, FakeServer, World
from tests.hub import contract as H
from tests.hub.fake import sign_with
from tests.hub.vec import private_key, pub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class HubServer(FakeServer):
    """orch-publish behind a hub: only the hub-path signature counts (own label, the hub, no approval), and the hub's
    own signature must verify under HK_op. Everything else is the fake server's routes."""

    def _authentic(self, headers, method, netloc, target, body):
        h = {k.lower(): v for k, v in headers.items()}
        if "orch-hub" not in h or "orch-hub-signature" not in h or "orch-owner-approval" in h:  # the hub added two
            return False
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), method, netloc,
            target, body, h["orch-hub"], None,
        )  # fmt: skip
        return (
            h["orch-hub"] == HUB_ID
            and S.verify(self.wsk_pub, crypto.unb64u(h["orch-signature"], 64), signed)
            and S.verify(pub("hk_op"), crypto.unb64u(h["orch-hub-signature"], 64), H.hub_publish_bytes(HUB_ID, signed))
        )


class PublishHub:
    """Takes ``join``, forwards ``publish_out`` to :class:`HubServer` (adding the hub's signature, as the contract says)
    and answers every message signed by the vector HK_op."""

    def __init__(self, server: HubServer) -> None:
        self.server, self.sent, self.error = server, [], None

    def ops(self):
        return [m["o"]["op"] for m in self.sent]

    def send(self, message):
        self.sent.append(copy.deepcopy(message))
        m = message["o"]
        body = {}
        if m["op"] == "card_put":
            body = {"error": self.error} if self.error else {}
        elif m["op"] == "publish_out":
            body = {"error": self.error} if self.error else self._forward(m["body"]["request"])
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": m["hub_id"], "workspace_id": m["workspace_id"],
            "dir": "h2w", "seq": 1, "ts_ms": m["ts_ms"], "op": m["op"], "re": m["seq"], "body": body,
        }  # fmt: skip
        return sign_with(private_key("hk_op"), "sig_hub_route", o)

    def _forward(self, req):
        h = {k.lower(): v for k, v in req["headers"].items()}
        assert set(h) == {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature"}, "the host's header set (11.1)"
        h["orch-hub"] = HUB_ID
        wsk = self.server.wsk_pub
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), req["method"],
            req["host"], req["target"], bytes.fromhex(req["body_hex"]), h["orch-hub"], None,
        )  # fmt: skip
        assert S.verify(wsk, crypto.unb64u(h["orch-signature"], 64), signed), "the host's signature must verify"
        headers = dict(req["headers"]) | {"Orch-Hub": HUB_ID}
        headers["Orch-Hub-Signature"] = crypto.b64u(
            S.sign(private_key("hk_op"), H.hub_publish_bytes(h["orch-hub"], signed))
        )
        status, data = self.server(
            "https", req["host"], req["method"], req["target"], headers, bytes.fromhex(req["body_hex"]), 1.0
        )
        return {"status": status, "body_hex": data.hex()}


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def world(served, human, monkeypatch):
    w = World(served, human)
    w.addon()
    w.configure()
    store = served.lab.ws.other()
    try:
        genesis = store.genesis_event()
    finally:
        store.close()
    card = {"delegation": {"o": {"wsk_pub": genesis["wsk_pub"]}}, "o": {}, "sig": "", "sealed": ""}
    monkeypatch.setattr(hubops, "CARD_SOURCE", lambda service: card)
    w.hub_server = HubServer(crypto.unb64u(genesis["wsk_pub"]))
    w.hub = PublishHub(w.hub_server)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: w.hub)
    yield w
    publishing.TRANSPORT = None


def joined(w, human, served):
    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))


def staged_share(w, name="report.md", data=REPORT):
    uid, key = w.ticket_with(**{name: data})
    staged = w.prepare(key, name)["staged_id"]
    w.approve(staged)
    return staged, key


def share(w, **kw):
    staged, key = staged_share(w, **kw)
    return w.send(staged), key


def kinds(server):
    return [(r["method"], r["target"].split("/", 4)[4]) for r in server.requests]


def test_a_joined_workspace_publishes_through_the_hub(world, human, served):
    w = world
    joined(w, human, served)
    res, key = share(w)
    data = ok(res)["data"]
    share_id, token = LINK.fullmatch(data["link"]).groups()
    # the shares went over the hub link, each verified byte for byte by the hub-path bytes of the contract ...
    assert w.hub.ops() == ["join", "snapshot_get", "card_put", "publish_out", "publish_out", "publish_out"]
    assert w.hub_server.bad_signatures == []
    assert kinds(w.hub_server) == [
        ("PUT", f"shares/{share_id}"),
        ("PUT", f"shares/{share_id}/files/report.md"),
        ("POST", f"shares/{share_id}/commit"),
    ]
    assert w.hub_server.shares[share_id]["state"] == "live"
    # ... and the direct server saw nothing: the registration went as the card through the hub (R5a, V3-D36)
    assert w.server.requests == []
    (card_msg,) = [m["o"]["body"] for m in w.hub.sent if m["o"]["op"] == "card_put"]
    assert card_msg["card"] == hubops.CARD_SOURCE(None) and set(card_msg) == {"card", "pk_pub"}
    assert all(token.encode() not in r["body"] and token not in r["target"] for r in w.hub_server.requests)
    audit = [json.loads(line) for line in (served.lab.state / "host" / "audit-security.jsonl").read_text().splitlines()]
    assert [a["via"] for a in audit if a["event"] == "publish.share"] == ["hub"]


def test_register_goes_through_the_hub_as_card_put_and_never_direct(world, human, served):
    w = world
    joined(w, human, served)
    out = ok(w.req("publish.register", {}))["data"]
    assert out["registered"] is True
    assert w.hub.ops()[-1] == "card_put" and w.server.requests == [] and w.hub_server.requests == []


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("owner_not_allowed", "owner_not_allowed"),
        ("unknown_workspace", "unknown_workspace"),
        ("workspace_blocked", "blocked"),
        ("delegation_changed", "conflict"),
        ("stale_card", "rejected"),
        ("delegation_refused", "rejected"),
        ("other_person", "rejected"),
        ("delegation_mismatch", "rejected"),
    ],
)
def test_register_refusals_of_publish_reach_the_owner_and_nothing_goes_direct(world, human, served, code, reason):
    w = world
    joined(w, human, served)
    w.hub.error = code
    res = w.req("publish.register", {})
    assert err(res) == "publish.server_refused"
    assert res["error"]["detail"]["reason"] == reason
    assert w.server.requests == []
    assert list(served.lab.state.rglob("registered.json")) == []  # a refused registration is not remembered


def test_revoke_and_live_list_go_through_the_hub_too(world, human, served):
    w = world
    joined(w, human, served)
    res, _ = share(w)
    share_id = LINK.fullmatch(ok(res)["data"]["link"]).group(1)
    direct_before = len(w.server.requests)
    ok(w.req("publish.revoke", {"share_id": share_id}))
    assert w.hub_server.shares[share_id]["state"] == "revoked"
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is True and live["shares"][0]["state"] == "revoked"
    assert len(w.server.requests) == direct_before  # nothing direct


def test_a_hub_that_is_down_fails_closed_and_nothing_goes_direct(world, human, served, monkeypatch):
    w = world
    joined(w, human, served)
    ok(share(w)[0])  # registers, and shows the path works
    direct_before, hub_before = len(w.server.requests), len(w.hub_server.requests)
    monkeypatch.setattr(hubops, "TRANSPORT", None)  # the hub cannot be reached
    res, _ = share(w, name="second.md")
    assert err(res) == "hub.unreachable"
    assert len(w.server.requests) == direct_before and len(w.hub_server.requests) == hub_before
    live = ok(w.req("publish.list", {"live": True}))["data"]  # a read degrades, like a dead server
    assert live["live"] is False and len(w.server.requests) == direct_before


def test_a_hub_refusal_is_hub_refused_not_a_direct_retry(world, human, served):
    w = world
    joined(w, human, served)
    ok(share(w)[0])
    direct_before = len(w.server.requests)
    w.hub.error = "not_bound"
    assert err(share(w, name="again.md")[0]) == "hub.refused"
    assert len(w.server.requests) == direct_before


def test_joined_but_not_connected_publishes_nothing(world, human, served):
    w = world
    human.run("hub.join", join_args())  # the owner signed; hub.connect never ran
    assert err(share(w)[0]) == "hub.not_joined"
    assert w.server.requests == [] and w.hub_server.requests == []


def test_after_leave_the_workspace_publishes_directly_and_the_old_binding_is_not_used(world, human, served):
    w = world
    joined(w, human, served)
    ok(share(w)[0])
    via_hub = len(w.hub_server.requests)
    human.run("hub.leave", {})
    # the log says left (hub.json still holds the bound binding until `orch hub unbind`): no hub mode, so direct
    res, _ = share(w, name="after.md")
    ok(res)
    assert len(w.hub_server.requests) == via_hub and w.hub.ops().count("publish_out") == 3
    assert any(m == "PUT" and t.endswith("/files/after.md") for m, t in kinds(w.server))
    # a rejoin (binding_seq 2) is a new binding: the old connected state does not carry over
    human.run("hub.join", join_args())
    assert err(share(w, name="third.md")[0]) == "hub.not_joined"
    assert len(w.hub_server.requests) == via_hub


def test_a_file_over_the_link_frame_says_so_and_sends_nothing(world, human, served):
    w = world
    joined(w, human, served)
    res, _ = share(w, name="big.bin", data=b"\x07" * 600_000)
    assert err(res) == "publish.server_refused"
    assert [r for r in w.hub_server.requests if r["method"] == "PUT" and "/files/" in r["target"]] == []


def test_no_hub_no_change(world):
    w = world  # never joined: the direct path, as before (the whole of test_publishing.py runs this way)
    res, _ = share(w)
    assert re.fullmatch(r".*/s/[0-9a-f]{32}#t=.*", ok(res)["data"]["link"])
    assert w.hub.sent == [] and w.hub_server.requests == []
