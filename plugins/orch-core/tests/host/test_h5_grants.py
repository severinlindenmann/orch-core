# ruff: noqa: F811
"""V3 C10 (design-c10 M6, hub protocol 8.8): the host side of ``grant.requested``.

A hub-signed request lands; in the same lock hold the host appends a host-actor ``grant.issued`` naming it (a fresh
``secret_hash``, the request's terms, ``Grant.hub_id``). The secret reaches the runner only for a pending local hand-off
(``hub.handoff {kind: grant}``) of the same canonical person, binding and exact terms, used once; otherwise the grant
is inert and the secret is destroyed (``hub_grant_without_handoff_inert``, ``hub_grant_with_handoff_once``,
``handoff_other_workspace_or_binding_refused``). Real signatures from the fake hub vector keys (test_hub_deliver)."""

from __future__ import annotations

from typing import Any

import pytest

from orch.host import hubdeliver, hubops
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_hub_deliver import ALICE, Env, mint
from tests.host.test_hub_ops import dev_workspace, stub  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

TERMS = {"scope": "workable", "verbs": "agent", "hours": 8}


@pytest.fixture
def env(served, human, stub, monkeypatch):
    e = Env(served, human, alias=True)  # the owner's person is the vector hub user (admin)
    e.state.addr = "hub.example.test:4443"  # hub.handoff builds its link from the pinned address
    e.service.submit(hubops._save_state, e.service, e.state).result()  # noqa: SLF001
    e.captured = []  # type: ignore[attr-defined]
    monkeypatch.setattr(hubdeliver, "_runner_ready", lambda service, runners: True)

    def capture(service: Any, landing: Any) -> None:
        e.captured += landing.deposits  # type: ignore[attr-defined]
        landing.deposits = []

    monkeypatch.setattr(hubdeliver, "_to_runner", capture)
    return e


def request(env: Env, **over: Any) -> dict[str, Any]:
    env.n += 1
    base = {
        "id": f"01J9ZP000000000000000{env.n:05d}",
        "based_on": env.head("workspace"),
        "roster_v": env.roster_v(),
        **TERMS,
    }
    return mint(env.wid, "workspace", "grant.requested", **{**base, **over})


def handoff(env: Env, **args: Any) -> dict[str, Any]:
    return env.served.client().req("hub.handoff", {"kind": "grant", **{**TERMS, **args}})


def grants(env: Env) -> list[dict[str, Any]]:
    return [e for e in env.lines("workspace") if e["type"] == "grant.issued" and e["actor"]["kind"] == "host"]


def test_a_request_is_answered_by_a_host_grant_that_names_it_and_the_secret_goes_nowhere(env):
    e = request(env)
    assert env.deliver(e, "workspace") is None
    lines = env.lines("workspace")
    req, grant = lines[-2], lines[-1]
    assert (req["type"], req["id"]) == ("grant.requested", e["id"])
    assert grant["type"] == "grant.issued" and grant["actor"] == {"kind": "host"} and grant["request"] == e["id"]
    assert (grant["scope"], grant["verbs"], grant["hours"]) == ("workable", "agent", 8)
    assert grant["issued_at"] == req["at"] and grant["secret_hash"].startswith("sha256:")
    store = env.store()
    try:
        g = store.state._core.ws.grants[grant["grant"]]  # noqa: SLF001
        owner = next(p for p, m in store.state.workspace.members.items() if m.role == "owner")
        assert g.hub_id == env.state.hub_id and g.person == owner
        assert not store.chain_errors()
    finally:
        store.close()
    assert env.captured == []  # no pending hand-off: the secret was destroyed, the grant is inert (I5)
    assert "secret" not in repr(grant).replace("secret_hash", "")


def test_hub_grant_with_handoff_gives_the_runner_the_secret_once(env):
    data = ok(handoff(env))["data"]
    assert data["pending"] is True and data["kind"] == "grant"
    assert env.deliver(request(env), "workspace") is None
    (plan, secret), *rest = env.captured
    assert not rest and secret.startswith(plan["grant"] + ".") and plan["until"]
    g = grants(env)[-1]
    assert plan["grant"] == g["grant"] and plan["person"].startswith("p_")
    # used once: a second request of the same terms finds no hand-off
    env.captured.clear()
    assert env.deliver(request(env), "workspace") is None
    assert env.captured == [] and len(grants(env)) == 2


@pytest.mark.parametrize("over", [{"hours": 7}, {"scope": "all"}])
def test_a_hand_off_for_other_terms_gives_nothing(env, over):
    ok(handoff(env))
    assert env.deliver(request(env, **over), "workspace") is None
    assert env.captured == [] and len(grants(env)) == 1  # the grant exists (honest log), inert


def test_a_cancelled_hand_off_is_gone(env):
    ok(handoff(env))
    cancelled = ok(env.served.client().req("hub.handoff", {"kind": "grant", "cancel": True}))["data"]["cancelled"]
    assert cancelled == 1
    assert env.deliver(request(env), "workspace") is None
    assert env.captured == []


def test_a_hand_off_is_dropped_when_the_binding_changes(env):
    ok(handoff(env))
    env.served.service.submit(_rebind, env.served.service).result()
    assert env.deliver(request(env), "workspace") is None
    assert env.captured == []


def _rebind(service: Any) -> None:
    """A hand-off made under another binding: rewrite its binding_seq (as a leave and rejoin would leave it)."""
    from orch.host import grantoff

    h = grantoff.GrantHandoffs(service)
    with service.store.locked():
        items = h._read()  # noqa: SLF001
        assert items
        for i in items:
            i["binding_seq"] += 1
        h._write(items)  # noqa: SLF001


def test_an_old_hand_off_is_gone(env):
    ok(handoff(env))
    env.served.lab.ws.clock[0] += 601
    assert env.deliver(request(env), "workspace") is None
    assert env.captured == []


def test_without_a_runner_the_hand_off_stays_and_the_grant_is_inert(env, monkeypatch):
    monkeypatch.setattr(hubdeliver, "_runner_ready", lambda service, runners: False)
    ok(handoff(env))
    assert env.deliver(request(env), "workspace") is None
    assert env.captured == []


def test_a_request_naming_a_human_only_operation_is_refused_and_writes_nothing(env):
    before = env.lines("workspace")
    r = env.deliver(request(env, verbs=["approve"]), "workspace")
    assert r == "forbidden_role"
    assert env.lines("workspace") == before


def test_the_hand_off_is_for_full_mode_owners_and_takes_exact_terms(env):
    r = env.served.client().req("hub.handoff", {"kind": "grant", "scope": "workable"})
    assert err(r)  # needs verbs and hours
    r = env.served.client().req("hub.handoff", {"kind": "gate", "ticket": "X-1", "hours": 3})
    assert err(r)  # terms belong to the grant kind only
    assert ALICE  # the vector user is the hub identity the owner's person is aliased to
