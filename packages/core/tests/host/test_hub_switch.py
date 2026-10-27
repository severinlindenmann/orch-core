"""H1 (27 Oct): ``hub.switch``: the host relays one-time switch tickets and the rail list over the hub link and sends
nothing else (the hub decides the person). Reuses the stub hub of test_hub_ops with a per-op reply body."""

from __future__ import annotations

import copy

import pytest

from orch.host import hubops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_ops import CARD, HUB_ID, StubHub, connect_args, dev_workspace, join_args  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class ReplyHub(StubHub):
    """The stub hub with a reply body per op (``replies``); other ops answer ``{}`` as the stub does."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: dict[str, dict] = {}

    def send(self, message):
        from tests.hub.fake import sign_with
        from tests.hub.vec import private_key

        m = message["o"]
        (self.snapshot_gets if m["op"] == "snapshot_get" else self.sent).append(copy.deepcopy(message))
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": m["hub_id"], "workspace_id": m["workspace_id"],
            "dir": "h2w", "seq": 1, "ts_ms": m["ts_ms"], "op": m["op"], "re": m["seq"],
            "body": self.replies.get(m["op"], {}),
        }  # fmt: skip
        return sign_with(private_key("hk_op"), "sig_hub_route", o)


@pytest.fixture
def hub(monkeypatch, served):
    h = ReplyHub()
    store = served.lab.ws.other()
    try:
        genesis = store.genesis_event()
    finally:
        store.close()
    card = {"delegation": {"o": {"wsk_pub": genesis["wsk_pub"]}}, "o": {}, "sig": "", "sealed": ""}
    monkeypatch.setattr(hubops, "CARD_SOURCE", lambda service: card)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: h)
    return h


def bound(human, served):
    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))


def test_switch_needs_a_bound_hub(human, served, hub):
    c = served.client()
    args = {"kind": "rail", "origin": "http://127.0.0.1:8000"}
    assert err(c.req("hub.switch", args, idem=False)) == "hub.not_joined"
    assert hub.sent == []


def test_ticket_redeem_and_rail_relay_exactly_the_closed_bodies(human, served, hub):
    bound(human, served)
    n = len(hub.sent)
    hub.replies = {
        "switch_ticket": {"ticket": "a" * 32, "url": f"https://hub.example/#t={'a' * 32}"},
        "switch_redeem": {"user_id": "u1", "name": "Ann", "return_to": "/t/X-1", "kind": "open"},
        "rail_get": {"hub": {"name": "Acme", "origin": "https://hub.example"}, "user_id": "u1", "workspaces": []},
    }
    c = served.client()
    t = ok(
        c.req(
            "hub.switch",
            {"kind": "ticket", "origin": "http://127.0.0.1:8000", "return_to": "/", "ticket_kind": "open"},
            idem=False,
        )
    )
    assert t["data"]["url"].endswith("#t=" + "a" * 32)
    r = ok(c.req("hub.switch", {"kind": "redeem", "ticket": "w." + "b" * 32}, idem=False))
    assert r["data"]["user_id"] == "u1" and r["data"]["kind"] == "open"
    rail = ok(c.req("hub.switch", {"kind": "rail", "origin": "http://127.0.0.1:8000"}, idem=False))
    assert rail["data"]["hub"]["name"] == "Acme"
    sent = [(m["o"]["op"], m["o"]["body"]) for m in hub.sent[n:]]
    assert sent == [
        ("switch_ticket", {"origin": "http://127.0.0.1:8000", "return_to": "/", "kind": "open"}),
        ("switch_redeem", {"ticket": "w." + "b" * 32}),
        ("rail_get", {"origin": "http://127.0.0.1:8000"}),
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "ticket", "origin": "http://127.0.0.1:8000"},  # missing parts
        {"kind": "redeem", "ticket": "x" * 20, "origin": "http://a"},  # extra part
        {"kind": "rail", "origin": "javascript:alert(1)"},
        {"kind": "redeem", "ticket": "bad ticket!"},
    ],
)
def test_switch_bodies_are_closed(human, served, hub, args):
    bound(human, served)
    n = len(hub.sent)
    assert err(served.client().req("hub.switch", args, idem=False)) == "invalid.input"
    assert len(hub.sent) == n


def test_a_hub_refusal_is_a_plain_refusal(human, served, hub):
    bound(human, served)
    hub.replies = {"switch_redeem": {"error": "bad_ticket"}}
    assert (
        err(served.client().req("hub.switch", {"kind": "redeem", "ticket": "w." + "c" * 32}, idem=False))
        == "hub.refused"
    )


def test_an_agent_is_refused_every_switch_kind(human, served, hub):
    """B2: column P and the owner role, like the other hub ops: no ticket and no rail for anyone else."""
    bound(human, served)
    n = len(hub.sent)
    for args in (
        {"kind": "rail", "origin": "http://127.0.0.1:8000"},
        {"kind": "ticket", "origin": "http://127.0.0.1:8000", "return_to": "/", "ticket_kind": "open"},
        {"kind": "redeem", "ticket": "w." + "b" * 32},
    ):
        res = served.client().req("hub.switch", args, idem=False, grant=served.grant, session=SESSION)
        assert err(res) in ("op.not_allowed", "role.denied")
    assert len(hub.sent) == n
