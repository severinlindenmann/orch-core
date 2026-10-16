"""R6c-2: the relay-pairing control ops (11A.15.9) on a real host's control socket: thin wrappers over the internal
API (``relaypair.api``). The offer's link is in its direct answer only; list and show carry no secret; refusals carry
the §0.6 codes; the settlement paths are never an op. The relay routes are fakes; never a real relay."""

from __future__ import annotations

import errno

import pytest

from orch.host import control, relayhook, relaypair, relayr6
from orch.host.custody import write_private
from orch.host.relaylink import link_path
from tests.host.lifecycle_helpers import Lab
from tests.identity.helpers import Person
from tests.relay.pairkit import FakeMembership, FakeRelay, Phone, Runner

ORIGIN = "https://relay.example"


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def ok(res):
    assert res["t"] == "res" and res["ok"] is True, res
    return res["result"]["data"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


def _primary(owner) -> Person:
    p = Person()
    p.pk, p.pk_pub = owner.pk, owner.pk_pub
    return p


@pytest.fixture
def linked(lab, monkeypatch):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["workspace_id"]
        service = h.host.services[wid]
        lp = link_path(lab.state, wid)
        lp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(lp, {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": "linked"})
        relayr6.keys_for(service)
        monkeypatch.setattr(relaypair.HostPairing, "offer_blocked", lambda self: None)
        engine = relaypair.engine_for(service)
        engine.routes, engine.membership, engine.post = FakeRelay(), FakeMembership(), Runner()
        try:
            yield c, wid, service, engine, lab.ws.owner
        finally:
            relaypair.close(service)
            relayr6.close(service)


def test_primary_set_then_offer_returns_the_link_only_in_its_direct_answer(linked):
    c, wid, service, engine, owner = linked
    assert err(c.call("host.relay_pair_offer", {"workspace_id": wid})) == "relay.no_primary"
    p = _primary(owner)
    got = ok(c.call("host.relay_primary_set", {"workspace_id": wid, "cert": p.cert(scopes=("look",)), "label": "Mac"}))
    assert got["device_id"] == p.device[2:]
    offer = ok(c.call("host.relay_pair_offer", {"workspace_id": wid, "scopes": ["look", "decide"]}))
    assert offer["link"]["native"].startswith("orch://pair?relay=") and "/pair#v2." in offer["link"]["https"]
    assert offer["state"] == "open" and offer["scopes"] == ["look", "decide"]
    oid = offer["offer_id"]
    listed = ok(c.call("host.relay_pair_list", {"workspace_id": wid}))
    shown = ok(c.call("host.relay_pair_show", {"workspace_id": wid, "offer_id": oid}))
    rec = engine.store.get(oid)
    for doc in (listed, shown, relayhook.status(service) or {}):
        text = repr(doc)
        assert "orch://" not in text and "#v2." not in text and rec["secret"] not in text
    assert shown["offer"]["state"] == "open" and [o["offer_id"] for o in listed["offers"]] == [oid]
    assert err(c.call("host.relay_pair_offer", {"workspace_id": wid})) == "relay.pair_open"


def test_answer_cancel_and_the_codes(linked):
    c, wid, service, engine, owner = linked
    relaypair.primary_set(service, _primary(owner).cert(scopes=("look",)), "Mac")
    offer = ok(c.call("host.relay_pair_offer", {"workspace_id": wid}))
    oid = offer["offer_id"]
    assert err(c.call("host.relay_pair_answer", {"workspace_id": wid, "offer_id": oid, "code": "AAAAAA",
                                                 "approve": True})) == "relay.pair_closed"  # fmt: skip
    phone = Phone(offer["link"]["native"])
    engine.receive(*phone.envelope(phone.meta(), ts=engine.now_ms()))
    got = ok(c.call("host.relay_pair_answer", {"workspace_id": wid, "offer_id": oid, "code": "AAAAAA",
                                               "approve": True}))  # fmt: skip
    assert (got["state"], got["attempts_left"]) == ("pending", 2)
    assert ok(c.call("host.relay_pair_cancel", {"workspace_id": wid, "offer_id": oid}))["state"] == "cancelled"
    assert err(c.call("host.relay_pair_abandon", {"workspace_id": wid, "offer_id": oid})) == "relay.pair_closed"
    assert err(c.call("host.relay_pair_show", {"workspace_id": wid, "offer_id": "ab" * 16})) == "not_found"


@pytest.mark.parametrize(
    "op, args",
    [
        ("host.relay_pair_offer", {"scopes": ["look", "decide", "operate"]}),
        ("host.relay_pair_offer", {"ttl_ms": 11 * 60_000}),
        ("host.relay_pair_show", {"offer_id": "XYZ"}),
        ("host.relay_pair_answer", {"offer_id": "ab" * 16, "code": "x"}),
        ("host.relay_primary_set", {"cert": {}, "label": ""}),
    ],
)
def test_the_op_schemas_refuse_bad_input(linked, op, args):
    c, wid, *_ = linked
    assert err(c.call(op, {"workspace_id": wid, **args})) == "invalid.input"


def test_an_unregistered_workspace_is_not_found(linked):
    c, *_ = linked
    assert err(c.call("host.relay_pair_list", {"workspace_id": "b" * 32})) == "not_found"


def test_primary_set_refuses_another_persons_certificate(linked):
    c, wid, *_ = linked
    assert err(c.call("host.relay_primary_set", {"workspace_id": wid, "cert": Person().cert(scopes=("look",)),
                                                 "label": "x"})) == "invalid.input"  # fmt: skip


def test_no_control_op_reaches_a_settlement_path():
    for op in control.CONTROL_OPS:
        for name in ("settle", "committed", "refused", "recover", "adopt", "precommit", "existing"):
            assert name not in op, op
