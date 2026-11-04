# ruff: noqa: F811
"""FB15 (hub protocol 8.8b): an add-on grant approved in the hub, host side.

A full-mode workspace cannot sign ``addon.granted`` locally. The owner prepares the grant as a hand-off request
(``human.prepare {handoff}``), ``hub.handoff {kind: addon}`` names it and builds the one link, the hub's channel signs
``addon.requested`` naming the request, and the host answers with a host-actor ``addon.granted`` built from the staged
package it re-checks (and installs it). Real signatures from the fake hub vector keys (test_hub_deliver)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.host import hubops
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.market_helpers import add_market, addon_dir, stage
from tests.host.test_hub_deliver import ALICE, Env, mint
from tests.host.test_hub_ops import dev_workspace, stub  # noqa: F401

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def env(served, human, stub, net):
    net.add("standup", "1.0.0")
    add_market(served, net)  # before the join: a joined workspace asks the hub's policy for every marketplace
    st = ok(stage(served, "standup"))["data"]  # staged before the join, for the same reason
    e = Env(served, human, alias=True)  # the owner's person is the vector hub user (admin)
    e.state.addr = "hub.example.test:4443"
    e.service.submit(hubops._save_state, e.service, e.state).result()  # noqa: SLF001
    e.human = human  # type: ignore[attr-defined]
    e.st = st  # type: ignore[attr-defined]
    return e


def prepare_handoff(env: Env, name: str = "standup") -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage the package, prepare the grant as a hand-off. Returns (the host's prepared answer, its grant event)."""
    res = env.human.prepare_res(
        "addon.grant",
        {
            "name": name,
            "staged": env.st["stage"],
            "digest": env.st["package_sha256"],
            "caps": env.st["grant"]["capabilities"],
        },
        handoff=True,
    )
    prepared = ok(res)["data"]
    return prepared, prepared["events"][0]


def terms_of(ev: dict[str, Any], handoff: str, **over: Any) -> dict[str, Any]:
    t = {k: copy.deepcopy(ev[k]) for k in ("name", "version", "package_sha256", "capabilities")}
    t["handoff"] = handoff
    if ev.get("runtime") is not None:
        t["runtime"] = copy.deepcopy(ev["runtime"])
    return {**t, **over}


def request(env: Env, terms: dict[str, Any]) -> dict[str, Any]:
    env.n += 1
    base = {"id": f"01J9ZP000000000000000{env.n:05d}", "based_on": env.head("workspace"), "roster_v": env.roster_v()}
    return mint(env.wid, "workspace", "addon.requested", **{**base, **terms})


def granted(env: Env) -> list[dict[str, Any]]:
    return [e for e in env.lines("workspace") if e["type"] == "addon.granted"]


def test_in_full_mode_the_grant_is_prepared_as_a_hand_off_and_nothing_is_signed_or_installed(env):
    prepared, ev = prepare_handoff(env)
    assert ev["type"] == "addon.granted" and prepared["request"]["state"] == "prepared"
    # the request is a hand-off with a code: nothing is in the log and nothing is installed until the hub signs
    assert granted(env) == [] and not addon_dir(env.served).exists()


def test_hub_handoff_names_the_request_and_builds_the_one_link(env):
    prepared, ev = prepare_handoff(env)
    rid = prepared["request"]["id"]
    got = ok(env.served.client().req("hub.handoff", {"kind": "addon", "request": rid}))["data"]
    assert got["kind"] == "addon" and got["pending"] is True
    assert got["summary"]["name"] == "standup" and got["summary"]["package_sha256"] == ev["package_sha256"]
    assert got["url"].startswith(f"https://hub.example.test:4443/#/w/{env.wid}/addon/")
    # not another person's, not a request that is no add-on grant, not an unknown id
    assert err(env.served.client().req("hub.handoff", {"kind": "addon", "request": "hr_" + "0" * 26})) == "not_found"
    assert err(env.served.client().req("hub.handoff", {"kind": "addon"})) == "invalid.input"


def test_the_hubs_request_turns_the_add_on_on_and_installs_it(env):
    prepared, ev = prepare_handoff(env)
    rid = prepared["request"]["id"]
    e = request(env, terms_of(ev, rid))
    assert env.deliver(e, "workspace") is None
    lines = env.lines("workspace")
    req, grant = lines[-2], lines[-1]
    assert (req["type"], req["id"]) == ("addon.requested", e["id"])
    assert grant["type"] == "addon.granted" and grant["actor"] == {"kind": "host"} and grant["request"] == e["id"]
    assert grant["package_sha256"] == ev["package_sha256"] and grant["fields"] == ev["fields"]
    assert addon_dir(env.served).exists()  # the staged bytes are in place
    store = env.store()
    try:
        assert store.state.workspace.addons["standup"]["package_sha256"] == ev["package_sha256"]
        assert not store.chain_errors()
    finally:
        store.close()
    done = env.service.submit(lambda: env.service.requests.read(rid)).result()
    assert done["state"] == "submitted" and done["outcome"]["state"] == "committed"
    # the same request is answered once: another request for the same hand-off is inert
    again = request(env, terms_of(ev, rid))
    assert env.deliver(again, "workspace") is None
    assert len(granted(env)) == 1


def test_a_request_without_a_live_hand_off_is_in_the_log_and_turns_nothing_on(env):
    prepared, ev = prepare_handoff(env)
    other = terms_of(ev, "hr_" + "1" * 26)  # names no request of this host
    assert env.deliver(request(env, other), "workspace") is None
    assert [e["type"] for e in env.lines("workspace")][-1] == "addon.requested"
    assert granted(env) == [] and not addon_dir(env.served).exists()


@pytest.mark.parametrize(
    "over",
    [
        {"version": "9.9.9"},
        {"package_sha256": "sha256:" + "b" * 64},
        {"capabilities": ["network", "pty"]},
    ],
)
def test_terms_that_differ_from_the_prepared_grant_turn_nothing_on(env, over):
    prepared, ev = prepare_handoff(env)
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"], **over)), "workspace") is None
    assert granted(env) == [] and not addon_dir(env.served).exists()


def test_an_expired_hand_off_is_inert(env):
    prepared, ev = prepare_handoff(env)
    env.served.lab.ws.clock[0] += 3600  # past the request's ten minutes
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert granted(env) == [] and not addon_dir(env.served).exists()


def test_a_refusal_names_its_real_reason_in_the_host_log(env, caplog):
    """FB15 round 3: the wire code of most model refusals is "forbidden_role"; the host log says which one it was."""
    prepared, ev = prepare_handoff(env)
    assert M.mfiles(env.served).drop_stage(env.st["stage"])
    with caplog.at_level("WARNING"):
        assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is not None
    assert any("addon.requested refused" in r.message and "model grant.terms" in r.message for r in caplog.records)


def test_a_changed_stage_is_refused_and_nothing_lands(env):
    prepared, ev = prepare_handoff(env)
    f = M.mfiles(env.served)
    assert f.drop_stage(env.st["stage"])  # the staged bytes are gone: the grant cannot be checked again
    code = env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace")
    assert code is not None  # a final refusal: the hub is told, nothing lands
    assert granted(env) == [] and [e["type"] for e in env.lines("workspace")][-1] != "addon.requested"


def test_the_same_hand_off_answered_by_two_requests_gives_one_grant(env):
    prepared, ev = prepare_handoff(env)
    t = terms_of(ev, prepared["request"]["id"])
    assert env.deliver(request(env, t), "workspace") is None
    assert env.deliver(request(env, t), "workspace") is None  # a second, different request event, same hand-off
    assert len(granted(env)) == 1


def test_a_bundled_add_on_with_nothing_staged_is_turned_on_through_the_hub(env):
    """B3: no stage file at all (a shipped or already installed add-on): the grant is judged on its own digest."""
    res = env.human.prepare_res("addon.grant", {"name": "wiki"}, handoff=True)
    prepared = ok(res)["data"]
    ev = prepared["events"][0]
    assert ev["type"] == "addon.granted"
    got = ok(env.served.client().req("hub.handoff", {"kind": "addon", "request": prepared["request"]["id"]}))["data"]
    assert got["summary"]["name"] == "wiki"
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    grant = granted(env)[-1]
    assert grant["name"] == "wiki" and grant["actor"] == {"kind": "host"}
    store = env.store()
    try:
        assert "wiki" in store.state.workspace.addons
    finally:
        store.close()


def test_a_market_install_prepares_the_same_op_and_is_a_hub_hand_off(env):
    """B2: `market_grant` is `addon.grant {name, staged}` (dashboard host_call); the record is marked for the hub."""
    prepared, _ = prepare_handoff(env)
    rec = env.service.submit(lambda: env.service.requests.read(prepared["request"]["id"])).result()
    assert rec["op"] == "addon.grant" and rec["staged"] and rec["hub_handoff"] is True


def test_only_an_unsigned_add_on_grant_hand_off_is_marked_for_the_hub_and_never_by_the_caller(env):
    """FB15 narrow path: core sets ``hub_handoff`` for ``addon.grant`` with ``handoff: true`` and no signer only. A
    hand-off of another op is refused (it is prepared with a signer, never marked: the hub link refuses it); the caller
    can never set the mark itself."""

    def record(rid):
        return env.service.submit(lambda: env.service.requests.read(rid)).result()

    _, key = env.human.ticket()
    assert err(env.human.prepare_res("log", {"ref": key, "text": "not for the hub"}, handoff=True)) == "invalid.input"
    other = ok(env.human.prepare_res("log", {"ref": key, "text": "not for the hub"}))["data"]
    rid = other["request"]["id"]
    assert record(rid)["hub_handoff"] is False
    assert err(env.served.client().req("hub.handoff", {"kind": "addon", "request": rid})) == "not_found"
    signed = env.human.prepare_res(
        "addon.grant",
        {
            "name": "standup",
            "staged": env.st["stage"],
            "digest": env.st["package_sha256"],
            "caps": env.st["grant"]["capabilities"],
        },
    )
    if signed.get("ok"):  # a local signature stays valid in every mode (A1): it is bound, never a hub hand-off
        assert record(ok(signed)["data"]["request"]["id"])["hub_handoff"] is False
    body = {
        "op": "addon.grant",
        "args": {
            "name": "standup",
            "staged": env.st["stage"],
            "digest": env.st["package_sha256"],
            "caps": env.st["grant"]["capabilities"],
        },
        "handoff": True,
    }
    assert err(env.human.c.req("human.prepare", {**body, "hub_handoff": True})) == "invalid.input"
    assert err(env.human.c.req("human.prepare", {**body, "signer": env.human.signer})) == "invalid.input"
    prepared, _ = prepare_handoff(env)
    assert record(prepared["request"]["id"])["hub_handoff"] is True
    dry = ok(env.human.c.req("human.prepare", body, dry_run=True))["data"]
    assert dry["request"] is None  # a dry run stores nothing, hand-off or not


def test_an_unmarked_lapsed_or_foreign_record_gets_no_link_and_no_answer(env):
    prepared, ev = prepare_handoff(env)
    rid = prepared["request"]["id"]
    env.served.lab.ws.clock[0] += 3600  # lapsed
    assert err(env.served.client().req("hub.handoff", {"kind": "addon", "request": rid})) == "not_found"


def test_a_phone_style_hand_off_record_is_not_answered_by_the_hub(env):
    """Defer: only a record prepared for the hub (hub_handoff) is answered; strip the mark and the request is inert."""
    prepared, ev = prepare_handoff(env)
    rid = prepared["request"]["id"]

    def unmark():
        rec = env.service.requests.read(rid)
        env.service.requests.write({**rec, "hub_handoff": False})

    env.service.submit(unmark).result()
    assert env.deliver(request(env, terms_of(ev, rid)), "workspace") is None
    assert granted(env) == []


@pytest.fixture
def env_unaliased(served, human, stub, net):
    """The real setup of run 12a: the workspace owner is the One click person, the hub user (a hub admin) joined through
    a console link, so NO alias in hub.joined: the hub user is `u_...`, owner by the hub role (admin = owner, D70)."""
    net.add("standup", "1.0.0")
    add_market(served, net)
    st = ok(stage(served, "standup"))["data"]
    e = Env(served, human)  # alias=False
    e.state.addr = "hub.example.test:4443"
    e.service.submit(hubops._save_state, e.service, e.state).result()  # noqa: SLF001
    e.human = human  # type: ignore[attr-defined]
    e.st = st  # type: ignore[attr-defined]
    return e


def bind_owner_user(env, prepared, user, monkeypatch):
    """The host asks the hub (`owner_get`, its own link) who the owner is; here the answer is faked, once per call."""
    from orch.host import hubops

    monkeypatch.setattr(hubops, "owner_user", lambda service, state: user)
    got = env.served.client().req("hub.handoff", {"kind": "addon", "request": prepared["request"]["id"]})
    ok(got)


def test_an_unaliased_hub_admin_turns_an_add_on_on_for_the_local_owner(env_unaliased, monkeypatch):
    """Round 4: `addon.requested` was refused `role.denied` (addon.requested is for a workspace owner) because the hub
    user is not a workspace member; the ticket-level question answer needs no member. Fails before the fix."""
    env = env_unaliased
    prepared, ev = prepare_handoff(env)
    bind_owner_user(env, prepared, ALICE, monkeypatch)
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert [e["type"] for e in env.lines("workspace")][-2:] == ["addon.requested", "addon.granted"]
    assert addon_dir(env.served).exists()


def test_an_unaliased_hub_user_who_is_not_an_admin_is_refused(served, human, stub, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    st = ok(stage(served, "standup"))["data"]
    e = Env(served, human, role="edit")  # hub role edit = member
    e.state.addr = "hub.example.test:4443"
    e.service.submit(hubops._save_state, e.service, e.state).result()  # noqa: SLF001
    res = human.prepare_res(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        handoff=True,
    )
    prepared = ok(res)["data"]
    assert e.deliver(request(e, terms_of(prepared["events"][0], prepared["request"]["id"])), "workspace") is not None
    assert granted(e) == []


def test_another_hub_admin_cannot_answer_the_owners_hand_off(env_unaliased, monkeypatch):
    """Security review: the hub user must be THE one the hub named as owner when the hand-off was made."""
    env = env_unaliased
    prepared, ev = prepare_handoff(env)
    bind_owner_user(env, prepared, "ab" * 16, monkeypatch)  # the hub names somebody else as the owner
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert granted(env) == [] and not addon_dir(env.served).exists()


def test_without_a_hub_named_owner_an_unaliased_hub_user_answers_nothing(env_unaliased):
    env = env_unaliased
    prepared, ev = prepare_handoff(env)
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert granted(env) == []


def test_a_second_handoff_call_cannot_rebind_hub_user(env_unaliased, monkeypatch):
    """The hub user is derived by the host and written once: asking again (even when the hub now names the signer)
    changes nothing, so no caller can rebind it to their own hub id."""
    env = env_unaliased
    prepared, ev = prepare_handoff(env)
    bind_owner_user(env, prepared, "ab" * 16, monkeypatch)  # first answer: somebody else
    bind_owner_user(env, prepared, ALICE, monkeypatch)  # a later call, the hub now says ALICE
    rec = env.service.submit(lambda: env.service.requests.read(prepared["request"]["id"])).result()
    assert rec["hub_user"] == "ab" * 16
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert granted(env) == []


def test_hub_user_is_not_an_argument_of_hub_handoff(env_unaliased):
    env = env_unaliased
    prepared, _ = prepare_handoff(env)
    res = env.served.client().req(
        "hub.handoff", {"kind": "addon", "request": prepared["request"]["id"], "hub_user": ALICE}
    )
    assert err(res) == "invalid.input"


def test_schedules_is_turned_on_through_the_hub_like_wiki(env):
    """Run 12 blocker 6.1: Schedules in Full mode: switch (hand-off) -> hub request -> on."""
    res = env.human.prepare_res("addon.grant", {"name": "schedules"}, handoff=True)
    prepared = ok(res)["data"]
    ev = prepared["events"][0]
    got = ok(env.served.client().req("hub.handoff", {"kind": "addon", "request": prepared["request"]["id"]}))["data"]
    assert got["summary"]["name"] == "schedules"
    assert env.deliver(request(env, terms_of(ev, prepared["request"]["id"])), "workspace") is None
    assert granted(env)[-1]["name"] == "schedules"


def test_the_hub_summary_names_the_events_in_words_and_the_signed_terms_keep_their_shape(env, monkeypatch):
    from orch.host import hubhandoff

    prepared, ev = prepare_handoff(env)
    rid = prepared["request"]["id"]
    plain = ok(env.served.client().req("hub.handoff", {"kind": "addon", "request": rid}))["data"]
    assert "events" not in plain["summary"]  # an add-on without events: nothing new
    decl = [{"kind": "run.finished", "title": "Run finished", "statuses": ["failed"], "ref": "int", "interval_s": 300}]
    real = hubhandoff.prepared_event
    monkeypatch.setattr(
        hubhandoff,
        "prepared_event",
        lambda r: {**real(r), "events": decl},
    )
    got = ok(env.served.client().req("hub.handoff", {"kind": "addon", "request": rid}))["data"]
    (line,) = got["summary"]["events"]
    assert "Run finished" in line and "every 5 min" in line and "can reach the internet" in line
    assert got["url"] == plain["url"]  # the link (the signed terms) is exactly what it was
