"""V3 C7 / R5a: ``orch publish`` goes through the hub (``publish_out``, ``card_put``), end to end on a served workspace:
the hub-path request verifies byte for byte (the contract's own bytes, ported in ``contract.py``), a hub that is down
fails closed, and a workspace without a hub (never joined, or left) refuses to publish: "join a hub" (D69, V3-D36).

The hub here is the stub of ``tests/host/test_publishing.py`` (``PublishHub`` in front of ``FakeServer``); the
contract's own checks are in ``test_publish_out.py``."""

from __future__ import annotations

import json

import pytest

from orch.host import hubops
from tests.host import human_helpers
from tests.host.dispatch_helpers import err, ok
from tests.host.test_hub_ops import join_args
from tests.host.test_publishing import LINK, REPORT, World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def world(served, human, monkeypatch):
    w = World(served, human, monkeypatch, join=False)
    w.addon()
    w.configure()
    w.hub_server = w.server  # orch-publish behind the hub; there is no other server (V3-D36)
    return w


def joined(w, human, served):
    w.join()


def staged_share(w, name="report.md", data=REPORT):
    uid, key = w.ticket_with(**{name: data})
    staged = w.prepare(key, name)["staged_id"]
    w.approve(staged)
    return staged, key


def share(w, **kw):
    staged, key = staged_share(w, **kw)
    return w.send(staged), key


def prepare_attempt(w, name="report.md", data=REPORT):
    """The raw ``publish.prepare`` reply: the route is checked at prepare, so a refusal comes here, before any send."""
    uid, key = w.ticket_with(**{name: data})
    return w.req("publish.prepare", {"ref": key, "artifacts": [name]})


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
    # ... and the registration went as the card through the hub (R5a, V3-D36), not as a request to orch-publish
    assert not any(t.endswith("/register") for _, t in kinds(w.server))
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
    ok(w.req("publish.revoke", {"share_id": share_id}))
    assert w.hub_server.shares[share_id]["state"] == "revoked"
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is True and live["shares"][0]["state"] == "revoked"


def test_a_hub_that_is_down_fails_closed_and_nothing_goes_direct(world, human, served, monkeypatch):
    w = world
    joined(w, human, served)
    ok(share(w)[0])  # registers, and shows the path works
    hub_before = len(w.server.requests)
    monkeypatch.setattr(hubops, "TRANSPORT", None)  # the hub cannot be reached
    res, _ = share(w, name="second.md")
    assert err(res) == "hub.unreachable"
    assert len(w.server.requests) == hub_before  # nothing was sent anywhere
    live = ok(w.req("publish.list", {"live": True}))["data"]  # a read degrades, like a dead server
    assert live["live"] is False and len(w.server.requests) == hub_before


def test_a_hub_refusal_is_hub_refused_not_a_direct_retry(world, human, served):
    w = world
    joined(w, human, served)
    ok(share(w)[0])
    before = len(w.server.requests)
    w.hub.error = "not_bound"
    assert err(share(w, name="again.md")[0]) == "hub.refused"
    assert len(w.server.requests) == before


def test_joined_but_not_connected_publishes_nothing(world, human, served):
    w = world
    human.run("hub.join", join_args())  # the owner signed; hub.connect never ran
    res = prepare_attempt(w)  # refused at prepare, in plain words: connect first
    assert err(res) == "hub.not_joined" and "not connected" in res["error"]["message"]
    assert w.server.requests == [] and w.hub_server.requests == [] and w.hub.sent == []


def test_after_leave_the_workspace_publishes_nothing_and_the_old_binding_is_not_used(world, human, served):
    w = world
    joined(w, human, served)
    ok(share(w)[0])
    via_hub = len(w.hub_server.requests)
    human.run("hub.leave", {})
    # the log says left (hub.json still holds the bound binding until `orch hub unbind`): a workspace without a hub
    # is local only (D69): "join a hub", and nothing is sent, neither through the hub nor anywhere else
    res = prepare_attempt(w, name="after.md")  # refused at prepare, plain words
    assert err(res) == "hub.not_joined" and "Join a hub" in res["error"]["message"]
    assert len(w.hub_server.requests) == via_hub and w.hub.ops().count("publish_out") == 3
    # a rejoin (binding_seq 2) is a new binding: the old connected state does not carry over
    human.run("hub.join", join_args())
    assert err(prepare_attempt(w, name="third.md")) == "hub.not_joined"
    assert len(w.hub_server.requests) == via_hub


def test_a_file_over_the_link_frame_is_said_at_prepare_before_anything_is_signed(world, human, served):
    w = world
    joined(w, human, served)
    res = prepare_attempt(w, name="big.bin", data=b"\x07" * 600_000)
    assert err(res) == "quota.host" and "can't go through the hub yet" in res["error"]["message"]
    assert w.hub_server.requests == []


def test_a_file_over_the_link_frame_still_sends_nothing_when_it_reaches_send(world, human, served, monkeypatch):
    w = world
    joined(w, human, served)
    with monkeypatch.context() as m:  # past prepare's early refusal: send keeps its own check
        m.setattr("orch.hubclient.publish.HUB_FILE_MAX", 10_000_000)
        staged, _ = staged_share(w, name="big.bin", data=b"\x07" * 600_000)
    res = w.send(staged)
    assert err(res) == "publish.server_refused"
    assert [r for r in w.hub_server.requests if r["method"] == "PUT" and "/files/" in r["target"]] == []


def test_a_standalone_workspace_refuses_publish_with_join_a_hub(world):
    w = world  # never joined: local only (D69, V3-D36). No direct path exists.
    res = prepare_attempt(w)
    assert err(res) == "hub.not_joined" and "Join a hub" in res["error"]["message"]
    reg = w.req("publish.register", {})
    assert err(reg) == "hub.not_joined" and "Join a hub" in reg["error"]["message"]
    live = ok(w.req("publish.list", {"live": True}))["data"]  # a read says plainly there is no server to ask
    assert live["live"] is False
    assert w.hub.sent == [] and w.server.requests == []
