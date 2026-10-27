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
from tests.host.test_hub_deliver import Env, mint
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
    res = env.human.prepare_res("addon.grant", {"name": name, "staged": env.st["stage"]}, handoff=True)
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
