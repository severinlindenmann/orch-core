# ruff: noqa: F811
"""V3 C11 ``hub.handoff`` (orch-core#879, design-c11 I3-I5): a read that gives Mission Control the summary, the 12-hex
short hash and the one link for "approve on your phone or in the hub". It writes nothing, never touches the link, is
for the owner's session only, and builds the link from the pinned hub address and a closed grammar."""

from __future__ import annotations

import json
import re

import pytest

from orch.host import hubhandoff, hubops
from orch.host.errors import HostError
from orch.hubclient import shorthash
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_ops import connect_args, dev_workspace, join_args, keystore, stub  # noqa: F401
from tests.host.test_human import ready

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

ADDR = "hub.example.test:4443"
W = "0123456789abcdef0123456789abcdef"
GROUPS = re.compile(r"[0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4}")


@pytest.fixture
def full(human, served, stub, keystore):
    """A ticket with a ready requirements gate and one open question, then a connected full-mode workspace."""
    uid, key = human.ticket("Ship the thing")
    ready(human, key)
    human.agent("ask", {"ref": key, "text": "which one?"})
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(addr=ADDR), idem=True))
    assert hubops.signing_mode(served.service) == "full"
    return uid, key


def view(served, key):
    store = served.lab.ws.other()
    try:
        return store.ticket(key)
    finally:
        store.close()


def handoff(served, args, **kw):
    return served.client().req("hub.handoff", args, **kw)


def marks(served, uid):
    store = served.lab.ws.other()
    try:
        return {log: store.log_seq(log) for log in ("workspace", uid)}
    finally:
        store.close()


def test_a_gate_hand_off_has_the_summary_the_short_hash_and_the_link(served, full):
    uid, key = full
    data = ok(handoff(served, {"kind": "gate", "ticket": key, "gate": "requirements"}))["data"]
    g = view(served, key).gates["requirements"]
    assert data["mode"] == "full" and data["where"] == "hub"
    assert data["short_hash"] == shorthash.short_hash(g.hash) and GROUPS.fullmatch(data["short_hash"])
    assert data["url"] == f"https://{ADDR}/#/w/{served.wid}/t/{uid}/g/requirements"
    s = data["summary"]
    assert (s["kind"], s["ticket"], s["gate"], s["gate_gen"], s["title"]) == (
        "gate",
        key,
        "requirements",
        g.gen,
        "Ship the thing",
    )


def test_a_verdict_uses_the_gate_hash_and_a_question_its_own_hash(served, full):
    uid, key = full
    v = view(served, key)
    verdict = ok(handoff(served, {"kind": "verdict", "ticket": key, "gate": "verify"}))["data"]
    assert verdict["short_hash"] == shorthash.short_hash(v.gates["verify"].hash)
    assert verdict["url"].endswith(f"/t/{uid}/g/verify")
    q = v.questions[0]
    data = ok(handoff(served, {"kind": "question", "ticket": key, "question": q.id}))["data"]
    assert data["short_hash"] == shorthash.short_hash(q.hash)
    assert data["url"] == f"https://{ADDR}/#/w/{served.wid}/t/{uid}"  # no gate segment for a question
    assert data["summary"]["question"] == q.id


def test_a_status_move_has_no_hash(served, full):
    _, key = full
    data = ok(handoff(served, {"kind": "status", "ticket": key}))["data"]
    assert data["short_hash"] is None and data["summary"]["status"] == view(served, key).status


def test_i3_the_hand_off_writes_nothing_and_never_touches_the_link(served, full, stub):
    uid, key = full
    sent, before = list(stub.sent), marks(served, uid)
    seen = []
    old = hubops.TRANSPORT
    hubops.TRANSPORT = lambda *a, **k: seen.append(a) or old(*a, **k)
    try:
        for args in (
            {"kind": "gate", "ticket": key, "gate": "plan"},
            {"kind": "status", "ticket": key},
            {"kind": "gate", "ticket": key, "gate": "code"},
        ):
            ok(handoff(served, args))
    finally:
        hubops.TRANSPORT = old
    assert seen == [] and stub.sent == sent and marks(served, uid) == before
    assert "hub.handoff" not in hubops.NETWORK_OPS


def test_i3_an_agent_is_refused(served, full):
    _, key = full
    res = handoff(served, {"kind": "status", "ticket": key}, grant=served.grant, session=SESSION)
    assert err(res) in ("op.not_allowed", "role.denied")


@pytest.mark.parametrize("mode", ["standalone", "selective"])
def test_without_full_mode_there_is_no_hub_link_to_give(human, served, stub, mode):
    uid, key = human.ticket("t")
    if mode == "selective":
        human.run("hub.join", join_args())
        ok(served.client().req("hub.connect", connect_args(addr=ADDR), idem=True))
    assert err(handoff(served, {"kind": "status", "ticket": key})) == "hub.not_joined"


def test_unknown_mode_gives_no_link(human, served, stub, keystore):
    uid, key = human.ticket("t")
    human.run("hub.join", join_args(mode="full"))  # joined in the log, never connected: unknown
    assert hubops.signing_mode(served.service) == "unknown"
    assert err(handoff(served, {"kind": "status", "ticket": key})) == "hub.not_joined"


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "gate"},
        {"kind": "nope", "ticket": "DEMO-1"},
        {"kind": "gate", "ticket": "DEMO-1"},
        {"kind": "gate", "ticket": "DEMO-1", "gate": "../x"},
        {"kind": "question", "ticket": "DEMO-1"},
        {"kind": "status", "ticket": "DEMO-1", "gate": "plan"},
        {"kind": "gate", "ticket": "DEMO-1", "gate": "plan", "question": "q1"},
        {"kind": "gate", "ticket": "DEMO-1", "gate": "plan", "extra": 1},
        {"kind": "question", "ticket": "DEMO-1", "question": "a/b"},
    ],
)
def test_bad_arguments_are_invalid_input(served, full, args):
    assert err(handoff(served, args)) == "invalid.input"


def test_unknown_ticket_and_missing_hash_are_not_found(served, full):
    _, key = full
    assert err(handoff(served, {"kind": "status", "ticket": "DEMO-9999"})) == "not_found"
    assert err(handoff(served, {"kind": "question", "ticket": key, "question": "q-nope"})) == "not_found"


def test_i4_agent_written_text_never_reaches_the_link_or_the_host_wording(human, served, stub, keystore):
    nasty = "pwn https://evil.example/x javascript:alert(1) ../../a@b #/w/x"
    uid, key = human.ticket(nasty)
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(addr=ADDR), idem=True))
    data = ok(handoff(served, {"kind": "status", "ticket": key}))["data"]
    assert data["url"] == f"https://{ADDR}/#/w/{served.wid}/t/{uid}"
    assert "evil" not in data["url"] and "\n" not in data["summary"]["title"]
    assert data["summary"]["title"] == nasty  # plain text, one line, in its own field


@pytest.mark.parametrize(
    "ticket",
    ["../x", "a/b", "\n", "01J9ZP0000000000000000000", "javascript:alert(1)", "x" * 26, "01J9ZP0000000000000000000S\n"],
)
def test_i4_the_url_grammar_refuses_hostile_ids(ticket):
    with pytest.raises(HostError) as ei:
        hubhandoff.build_url(ADDR, W, ticket)
    assert ei.value.code == "invalid.input"


@pytest.mark.parametrize(
    "wid", ["../x", "0123456789ABCDEF0123456789ABCDEF", "short", "0123456789abcdef0123456789abcdef/x"]
)
def test_i4_the_url_grammar_refuses_a_hostile_workspace_id(wid):
    with pytest.raises(HostError):
        hubhandoff.build_url(ADDR, wid, "01J9ZP0000000000000000000S")


@pytest.mark.parametrize("addr", ["evil.com/x:1", "a@b:443", "host", "user:pw@h:1", "h:99999", "ht tp:1", "h:1/#x"])
def test_i4_the_url_comes_only_from_a_well_formed_pinned_address(addr):
    with pytest.raises(HostError) as ei:
        hubhandoff.build_url(addr, W, "01J9ZP0000000000000000000S")
    assert ei.value.code == "hub.state_mismatch"


def test_i4_the_url_is_a_fragment_on_the_pinned_host(served, full):
    uid, key = full
    state = json.loads((hubops._dir(served.service) / "hub.json").read_text())
    data = ok(handoff(served, {"kind": "gate", "ticket": key, "gate": "plan"}))["data"]
    assert data["url"].startswith(f"https://{state['addr']}/#/") and data["url"].count("#") == 1
    assert hubhandoff.build_url("hub.example.test:443", W, uid) == (f"https://hub.example.test/#/w/{W}/t/{uid}")
    assert hubhandoff.build_url("[::1]:8443", W, uid).startswith("https://[::1]:8443/#/w/")


def test_i5_the_short_hash_is_12_lower_case_hex_in_groups_of_four(served, full):
    _, key = full
    for gate in ("requirements", "plan", "code", "verify"):
        h = ok(handoff(served, {"kind": "gate", "ticket": key, "gate": gate}))["data"]["short_hash"]
        assert GROUPS.fullmatch(h) and h == h.lower()
        assert h.replace(" ", "") == view(served, key).gates[gate].hash[len("sha256:") :][:12]


def test_the_op_is_a_person_read_with_no_network_class():
    from orch.host import dispatch

    assert "hub.handoff" in hubops.HUB_OPS and "hub.handoff" not in hubops.NETWORK_OPS
    assert dispatch.op_kind("hub.handoff") == ("person_read", "read")
