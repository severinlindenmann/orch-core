"""Estimate: points are the ticket's size (xs..xl), set through the ordinary `set size=` hand-off, only while ON."""

import pytest
from pydantic import TypeAdapter, ValidationError
from test_shares import AddonLink, Host  # the shared stub host and add-on list link

from orch_dashboard import handoff
from orch_dashboard.addon_switches import busy, needs
from orch_dashboard.errors import HostError


def parse(body):
    return TypeAdapter(handoff.PrepareBody).validate_python(body)


def test_estimate_is_a_shipped_addon_switched_like_the_others_with_plain_words():
    from orch.setup import facts

    assert isinstance(parse({"action": "addon_grant", "name": "estimate"}), handoff.AddonGrant)
    off = parse({"action": "addon_disable", "name": "estimate"})
    assert handoff.host_call(off) == ("addon.disable", {"name": "estimate"})
    assert needs("estimate", None) is None and busy("estimate", [{"kind": "agent"}]) is None
    listed = {a.id: a for a in facts.catalog(optional=True)}
    assert listed["estimate"].title == "Estimate" and listed["estimate"].capabilities == ()
    assert "estimate" not in {a.id for a in facts.catalog()}  # the setup wizard does not nag about it


@pytest.mark.parametrize("size", ["xs", "s", "m", "l", "xl", "none"])
def test_an_estimate_is_one_ordinary_size_change(size):
    body = parse({"action": "details", "key": "DEMO-1", "size": size, "via": "estimate"})
    # `via` never reaches the host
    assert handoff.host_call(body) == ("set", {"ref": "DEMO-1", "pairs": [f"size={size}"]})


def test_an_estimate_carries_a_size_and_nothing_else():
    for extra in ({"priority": "high"}, {"labels": ["a"]}, {"parent": "DEMO-2"}):
        with pytest.raises(ValidationError):
            parse({"action": "details", "key": "DEMO-1", "size": "m", "via": "estimate", **extra})
    with pytest.raises(ValidationError):
        parse({"action": "details", "key": "DEMO-1", "priority": "high", "via": "estimate"})
    with pytest.raises(ValidationError):
        parse({"action": "details", "key": "DEMO-1", "size": "xxl", "via": "estimate"})  # not on the scale


def test_the_session_lists_estimate_only_while_the_addon_is_on(gateway, stub, monkeypatch):
    class Link(Host):
        def has(self, cap):
            return False

        def call(self, op, args=None, **kw):
            return {"addons": self.rows}

        def supports(self, op, cap):
            return False

    link = Link()
    monkeypatch.setattr(gateway, "enabled", lambda ws: link)
    link.rows = [{"name": "estimate", "state": "active"}]
    assert "estimate" in gateway.features(stub.ws)
    for state in ("not_granted", "disabled", "changed", "missing"):
        link.rows = [{"name": "estimate", "state": state}]
        assert "estimate" not in gateway.features(stub.ws)


def test_the_hand_off_of_an_estimate_is_refused_while_the_addon_is_off(gateway, stub, monkeypatch):
    class Link(AddonLink):
        welcome = {"peer": {"person": "p_me"}, "ops": {"set": 1}}

    link = Link([{"name": "estimate", "state": "disabled"}])
    monkeypatch.setattr(gateway, "link", lambda ws: link)
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: {"doc": {"labels": []}, "gates": {}})
    body = parse({"action": "details", "key": "DEMO-1", "size": "m", "via": "estimate"})
    with pytest.raises(HostError) as err:
        handoff.plan(gateway, stub.ws, body)
    assert err.value.error["code"] == "addon.disabled"
    link.rows = [{"name": "estimate", "state": "active"}]
    assert handoff.plan(gateway, stub.ws, body).args == {"ref": "DEMO-1", "pairs": ["size=m"]}
    # the same size change from the New ticket follow-up (no `via`) is not an estimate and is not gated
    link.rows = [{"name": "estimate", "state": "disabled"}]
    assert handoff.plan(gateway, stub.ws, parse({"action": "details", "key": "DEMO-1", "size": "m"})).op == "set"
