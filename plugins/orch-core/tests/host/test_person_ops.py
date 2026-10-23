"""H6b, the host person operations ``move``, ``policy.set`` and ``settings.set`` (§0.8): served only through
``human.prepare`` and ``human.submit`` (a plain request is ``human_only``), judged by the model, reviewed by the signer
like every person event."""

from __future__ import annotations

import pytest

from orch.host import review
from orch.ops import person_ops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, UID
from tests.host.human_helpers import Human, err, ok
from tests.host.live_helpers import ok as ok_
from tests.host.live_helpers import world
from tests.host.test_reads_query import on_store

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

GATES = ("requirements", "plan", "verify", "code")


def policy(**kw):
    return {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False, **kw}


def code_policy(**kw):
    return {"approvers": ["owner"], "count": 1, "not": ["assignees"], "applies": "all", "independent": True, **kw}


def events_of(served, log: str, typ: str) -> list[dict]:
    store = served.lab.ws.other()
    try:
        if log == "workspace":
            return [e for _, e in store.merged_events() if e["type"] == typ and _ == "workspace"]
        return [e for e in store.events(log) if e["type"] == typ]
    finally:
        store.close()


def status(served, key: str) -> str:
    """The status the replayed log gives the ticket (read from a second store: no other read op is involved)."""
    store = served.lab.ws.other()
    try:
        return store.ticket(key).status
    finally:
        store.close()


@pytest.fixture
def maint(tmp_path):
    """A world whose test UID is mapped to ``quinn`` (a member), with quinn's own Human."""
    w = world(tmp_path, map_to="quinn")
    try:
        yield w, Human(w, w.quinn)
    finally:
        w.close()
        w.lab.close()


# --- the ops are not registry ops ------------------------------------------------------------------------------------


def test_the_host_person_ops_are_not_registry_ops():
    import orch.ops as ops
    from orch.model.emits import EMITS_V1

    want = {"move", "policy.set", "settings.set", "people.set", "publish.share"}  # B12D, P5
    want |= {
        "ac.accept",
        "ac.unaccept",
        "task.mark_done",
        "hub.join",
        "hub.mode",
        "hub.leave",
        "device.mark_exposed",
        "device.rotate",
    }  # V3 C2a, C2b, C10, #1097
    assert set(person_ops.OPS) == set(review.HOST_PERSON_OPS) == want
    for name in person_ops.OPS:
        assert name not in {op.name for op in ops.all()} and name not in EMITS_V1  # the frozen table is untouched
        assert review.person_op_types(name) == review.HOST_PERSON_OPS[name]
        assert person_ops.get(name).who == "human"
    assert review.events_fit("move", ["ticket.moved"]) and not review.events_fit("move", ["ticket.moved"] * 2)
    assert not review.events_fit("move", ["policy.changed"])
    assert not review.events_fit("settings.set", ["ticket.moved"])


def test_a_plain_request_of_a_person_op_is_human_only(served):
    uid, key = served.ticket()
    c = served.client()
    for op, args in (
        ("move", {"ref": key, "to": "open"}),
        ("policy.set", {"gates": {"plan": policy()}}),
        ("settings.set", {"set": {"grant_hours": 4}}),
    ):
        assert err(c.req(op, args, idem=True)) == "human_only"  # the person's connection, no grant
        assert err(c.req(op, args, grant=served.grant, session=SESSION, idem=True)) == "human_only"  # an agent grant
    assert status(served, key) == "open"  # nothing ran


# --- move ------------------------------------------------------------------------------------------------------------


def test_move_goes_through_prepare_sign_submit(human, served):
    uid, key = human.ticket()
    human.run("move", {"ref": key, "to": "backlog"})  # a new ticket is open
    prepared, done, signed = human.run("move", {"ref": key, "to": "open", "reason": "ready for work"})
    (event,) = signed.events
    assert event["type"] == "ticket.moved" and event["from"] == "backlog" and event["to"] == "open"
    assert event["reason"] == "ready for work" and prepared["log"] == uid
    (review_item,) = prepared["material"]["review"]
    assert review_item["payload"] == {"from": "backlog", "to": "open", "reason": "ready for work"}
    assert prepared["fingerprint"] and done["data"] == {"from": "backlog", "to": "open"}
    assert status(served, key) == "open"
    assert [e["to"] for e in events_of(served, uid, "ticket.moved")] == ["backlog", "open"]
    assert "reason" not in events_of(served, uid, "ticket.moved")[0]


def test_move_never_goes_into_another_status_or_takes_other_arguments(human, served):
    _, key = human.ticket()
    for to in ("in_progress", "testing", "done", "closed", "waiting"):
        assert err(human.prepare_res("move", {"ref": key, "to": to})) == "invalid.input"
    for bad in (
        {"ref": key},
        {"to": "open"},
        {"ref": key, "to": "open", "extra": 1},
        {"ref": key, "to": "open", "reason": ""},
    ):
        assert err(human.prepare_res("move", bad)) == "invalid.input"
    assert err(human.prepare_res("move", {"ref": key, "to": "open", "reason": "x" * 501})) == "invalid.input"
    human.prepare("move", {"ref": key, "to": "backlog", "reason": "x" * 500})
    assert status(served, key) == "open"  # preparing signs and appends nothing


def test_move_refuses_a_ticket_in_the_wrong_status_or_with_a_live_claim(human, served):
    _, key = human.ticket()
    ok(served.client().req("claim", {"ref": key}, grant=served.grant, session=SESSION, idem=True))
    assert status(served, key) == "in_progress"
    assert err(human.prepare_res("move", {"ref": key, "to": "open"})) == "transition.refused"  # a live claim
    assert err(human.prepare_res("move", {"ref": "DEMO-9999", "to": "open"})) == "not_found"
    served.lab.ws.clock[0] += 3 * 3600  # the claim lapses with its grant (1 h) and its idle time (2 h)
    prepared, _, _ = human.run("move", {"ref": key, "to": "backlog"})  # in_progress without a live claim: allowed
    assert prepared["material"]["review"][0]["payload"]["from"] == "in_progress"
    assert status(served, key) == "backlog"


def test_move_from_a_closed_ticket_is_refused(human, served):
    _, key = human.ticket()
    human.run("close", {"ref": key})
    assert err(human.prepare_res("move", {"ref": key, "to": "open"})) == "transition.refused"


def test_move_is_for_owners_and_maintainers_only(maint):
    w, quinn = maint
    uid, key = w.ticket()
    assert err(quinn.prepare_res("move", {"ref": key, "to": "backlog"})) == "role.denied"  # a member
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    quinn.run("move", {"ref": key, "to": "backlog"})  # a maintainer
    assert status(w, key) == "backlog"
    w.person_event("role.changed", person=w.quinn.ref, role="viewer")
    assert err(quinn.prepare_res("move", {"ref": key, "to": "open"})) == "role.denied"


def test_move_of_a_ticket_the_person_cannot_see_is_not_found(maint):
    w, quinn = maint
    uid, key = w.ticket()
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    w.restrict(uid, [w.lab.owner])
    assert err(quinn.prepare_res("move", {"ref": key, "to": "open"})) == "not_found"


# --- policy.set ------------------------------------------------------------------------------------------------------


def test_policy_set_changes_the_workspace_default_policy(human, served):
    plan = policy(approvers=["owner", "maintainer"], count=2)
    unsorted = policy(approvers=["owner", "maintainer", "owner"], count=2)  # the op sends the canonical form
    prepared, done, signed = human.run("policy.set", {"gates": {"plan": unsorted}})
    (event,) = signed.events
    assert event["type"] == "policy.changed" and event["gates"] == {
        "plan": {**plan, "approvers": ["maintainer", "owner"]}
    }
    assert prepared["log"] == "workspace" and done["data"] == {"gates": ["plan"]}
    shown = ok(served.client().req("workspace.settings"))["data"]["gates"]
    assert shown["plan"]["count"] == 2 and shown["plan"]["approvers"] == ["maintainer", "owner"]
    assert [e["gates"] for e in events_of(served, "workspace", "policy.changed")] == [event["gates"]]


def test_policy_set_with_a_ref_overrides_one_ticket(human, served):
    uid, key = human.ticket()
    override = {"verify": policy(approvers=["reviewers"], count=2, **{"not": ["assignees"]})}
    prepared, _, signed = human.run("policy.set", {"ref": key, "gates": override})
    assert prepared["log"] == uid and signed.events[0]["type"] == "policy.changed"
    assert [e for e in events_of(served, uid, "policy.changed")][0]["gates"]["verify"]["count"] == 2
    assert not events_of(served, "workspace", "policy.changed")


def test_policy_set_checks_its_input_and_the_code_gate_rule(human):
    assert err(human.prepare_res("policy.set", {"gates": {}})) == "invalid.input"
    assert err(human.prepare_res("policy.set", {"gates": {"nope": policy()}})) == "invalid.input"
    assert err(human.prepare_res("policy.set", {"gates": {g: policy() for g in (*GATES, "plan2")}})) == "invalid.input"
    assert err(human.prepare_res("policy.set", {"gates": {"plan": {"approvers": ["owner"]}}})) == "invalid.input"
    assert err(human.prepare_res("policy.set", {"gates": {"plan": policy(count=10)}})) == "invalid.input"
    assert err(human.prepare_res("policy.set", {"gates": {"plan": policy(approvers=[])}})) == "invalid.input"
    # the code gate always excludes assignees and is independent (D59): the model refuses anything else
    assert human.prepare_res("policy.set", {"gates": {"code": policy()}})["ok"] is False
    human.prepare("policy.set", {"gates": {"code": code_policy()}})


def test_policy_set_default_and_settings_set_are_for_owners_only(maint):
    w, quinn = maint
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    assert err(quinn.prepare_res("policy.set", {"gates": {"plan": policy()}})) == "role.denied"  # the default
    assert err(quinn.prepare_res("settings.set", {"set": {"grant_hours": 4}})) == "role.denied"
    w.person_event("role.changed", person=w.quinn.ref, role="owner")
    quinn.run("settings.set", {"set": {"grant_hours": 4}})
    quinn.run("policy.set", {"gates": {"plan": policy()}})


def test_policy_set_with_a_ref_is_for_the_ticket_owner_owners_and_maintainers(maint):
    """§0.8 / F1 5.4.2: ``claims.may_manage`` for a ticket override (not for the workspace default)."""
    w, quinn = maint
    uid, key = w.ticket()
    args = {"ref": key, "gates": {"plan": policy()}}
    assert err(quinn.prepare_res("policy.set", args)) == "role.denied"  # a member who does not own the ticket
    w.person_event("people.changed", uid, role="owner", add=[w.quinn.ref], remove=[])
    quinn.run("policy.set", args)  # now the ticket owner (a plain member)
    assert len(events_of(w, uid, "policy.changed")) == 1
    w.person_event("people.changed", uid, role="owner", add=[w.lab.owner], remove=[])
    assert err(quinn.prepare_res("policy.set", args)) == "role.denied"
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    quinn.run("policy.set", args)  # a maintainer
    assert len(events_of(w, uid, "policy.changed")) == 2
    assert err(quinn.prepare_res("policy.set", {"gates": {"plan": policy()}})) == "role.denied"  # default: owner only
    w.person_event("people.changed", uid, role="owner", add=[w.quinn.ref], remove=[])
    w.person_event("role.changed", person=w.quinn.ref, role="viewer")
    assert err(quinn.prepare_res("policy.set", args)) == "role.denied"  # a viewer owning the ticket


def test_people_set_is_for_whoever_may_manage_the_ticket_and_names_members_only(maint):
    """B12D: ``claims.may_manage`` (ticket owner, owners, maintainers); an owner is exactly one person."""
    w, quinn = maint
    uid, key = w.ticket()
    args = {"ref": key, "role": "assignees", "add": [w.quinn.ref], "remove": []}
    assert err(quinn.prepare_res("people.set", args)) == "role.denied"  # a member who does not own the ticket
    w.person_event("people.changed", uid, role="owner", add=[w.quinn.ref], remove=[])
    quinn.run("people.set", args)
    assert [e["add"] for e in events_of(w, uid, "people.changed")][-1] == [w.quinn.ref]
    assert err(quinn.prepare_res("people.set", {**args, "add": ["p_" + "9" * 32]})) == "invalid.input"  # not a member
    assert err(quinn.prepare_res("people.set", {**args, "add": [], "remove": []})) == "invalid.input"  # nothing
    assert (
        err(quinn.prepare_res("people.set", {**args, "add": [w.quinn.ref], "remove": [w.quinn.ref]})) == "invalid.input"
    )
    owner = {"ref": key, "role": "owner"}
    assert err(quinn.prepare_res("people.set", {**owner, "add": [w.quinn.ref, w.lab.owner]})) == "invalid.input"
    assert (
        err(quinn.prepare_res("people.set", {**owner, "add": [w.quinn.ref], "remove": [w.lab.owner]}))
        == "invalid.input"
    )
    assert err(quinn.prepare_res("people.set", {**args, "role": "watchers"})) == "invalid.input"  # not offered yet


def test_a_ticket_owner_fixes_a_no_eligible_gate_with_policy_set(maint):
    w, quinn = maint
    uid, key = w.ticket()
    w.person_event("people.changed", uid, role="owner", add=[w.quinn.ref], remove=[])
    on_store(w, lambda ws: ws.fill(uid))  # the gates wait
    w.person_event("policy.changed", gates={"requirements": policy(approvers=["maintainer", "owner"])})
    w.person_event("policy.changed", uid, gates={"requirements": policy(approvers=["maintainer"])})
    w.person_event("policy.changed", gates={"requirements": policy(approvers=["owner"])})  # nobody holds a token now
    needs = ok_(w.client().req("needs", {}))["data"]["items"]
    assert [i["kind"] for i in needs] == ["no_eligible"] and needs[0]["ref"] == "requirements"
    quinn.run("policy.set", {"ref": key, "gates": {"requirements": policy(approvers=["owner"])}})
    after = ok(w.client().req("needs", {}))["data"]["items"]
    assert not [i for i in after if i["kind"] == "no_eligible"]


# --- settings.set ----------------------------------------------------------------------------------------------------


def git_dir(tmp_path, name="proj"):
    """A folder the host accepts as a repo (it checks the folder and its .git; it runs no git)."""
    d = tmp_path / name
    (d / ".git").mkdir(parents=True)
    return str(d)


def test_settings_set_changes_the_durations_and_repos(human, served, tmp_path):
    changes = {
        "grant_hours": 4,
        "claim_ttl_min": 30,
        "repos": {"proj": {"path": git_dir(tmp_path), "default_branch": "main"}, "old": None},
    }
    prepared, done, signed = human.run("settings.set", {"set": changes})
    (event,) = signed.events
    assert event["type"] == "settings.changed" and event["set"] == changes
    assert prepared["log"] == "workspace" and done["data"] == {"set": ["claim_ttl_min", "grant_hours", "repos"]}
    got = ok(served.client().req("workspace.settings"))["data"]
    assert got["settings"]["grant_hours"] == 4 and got["settings"]["claim_ttl_min"] == 30
    assert got["settings"]["lease_ttl_min"] == 60 and got["defaults"] == ["lease_ttl_min"]
    assert got["settings"]["repos"]["proj"]["default_branch"] == "main"


def test_settings_set_shows_the_current_values_as_unsigned_before(human, tmp_path):
    prepared = human.prepare(
        "settings.set", {"set": {"grant_hours": 2, "repos": {"proj": {"path": git_dir(tmp_path)}}}}
    )
    assert prepared["material"]["before"] == {"grant_hours": 8, "repos": {"proj": None}}  # the host's current values
    review_item = prepared["material"]["review"][0]
    assert review_item["type"] == "settings.changed" and review_item["payload"]["set"]["grant_hours"] == 2
    human.sign(prepared)  # the signer accepts it: each before key is a key of `set`


def test_settings_set_checks_its_input(human, tmp_path):
    d = git_dir(tmp_path, "x")
    bad = [
        {"set": {}},
        {},
        {"set": {"grant_hours": 0}},
        {"set": {"grant_hours": 169}},
        {"set": {"claim_ttl_min": 14}},
        {"set": {"lease_ttl_min": 4}},
        {"set": {"other": 1}},
        {"set": {"repos": {}}},
        {"set": {"repos": {"Bad": {"path": "/x"}}}},
        {"set": {"repos": {"x": {"remote": "https://h/x"}}}},  # no path
        {"set": {"repos": {f"r{i}": {"path": "/x"} for i in range(33)}}},
    ]
    for args in bad:
        assert err(human.prepare_res("settings.set", args)) == "invalid.input", args
    # the model's own rules: a remote is a canonical identity, a local one names its own repo
    assert (
        human.prepare_res("settings.set", {"set": {"repos": {"x": {"path": d, "remote": "ssh://h/x"}}}})["ok"] is False
    )
    human.prepare("settings.set", {"set": {"repos": {"x": {"path": d, "remote": "https://example.org/x"}}}})


def test_settings_set_checks_repo_paths_on_the_host(human, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for path in ("/no/such/folder", "relative/dir", str(plain), str(tmp_path / "a" / ".." / "plain")):
        res = human.prepare_res("settings.set", {"set": {"repos": {"x": {"path": path}}}})
        assert err(res) == "invalid.input", path
    # removing a name needs no path; a good folder passes
    human.prepare("settings.set", {"set": {"repos": {"x": None}}})


# --- the whole path --------------------------------------------------------------------------------------------------


def test_each_event_type_passes_the_signer_end_to_end(human, served):
    """prepare -> signer.check -> sign -> submit for all three, then the replayed workspace shows the effect."""
    _, key = human.ticket()
    for op, args in (
        ("move", {"ref": key, "to": "backlog"}),
        ("policy.set", {"gates": {"requirements": policy(count=3)}}),
        ("settings.set", {"set": {"lease_ttl_min": 10}}),
    ):
        prepared = human.prepare(op, args)
        assert prepared["op"] == op and set(prepared["material"]) >= {"kind", "review"}
        human.check(prepared)
        signed = human.sign(prepared)
        assert ok(human.submit_res(prepared["request"]["id"], signed.events))["data"]
    shown = ok(served.client().req("workspace.settings"))["data"]
    assert shown["settings"]["lease_ttl_min"] == 10 and shown["gates"]["requirements"]["count"] == 3
    assert status(served, key) == "backlog"


def test_the_signer_refuses_a_person_op_event_outside_its_table(human):
    _, key = human.ticket()
    prepared = human.prepare("move", {"ref": key, "to": "backlog"})
    prepared["op"] = "settings.set"  # an event the op does not emit
    with pytest.raises(Exception, match="do not fit|knows"):
        human.check(prepared)
    assert UID  # (the UID is the one the helpers map to the owner)


# --- the signer knows the three ops: confirmation words (§0.8.1 item 7) and per-op checks ---------------------------


def _registry(prefix: str = "MINE"):
    return lambda wid: ("my workspace", prefix)


def test_the_confirmation_words_of_the_three_ops(human):
    from orch.host import signer

    _, key = human.ticket()
    moved = human.prepare("move", {"ref": key, "to": "backlog"})
    assert signer.confirm_word(moved, _registry()) == f"{key}/{moved['log'][-8:]}"
    ticket_policy = human.prepare("policy.set", {"ref": key, "gates": {"requirements": policy(count=2)}})
    assert signer.confirm_word(ticket_policy, _registry()) == f"{key}/{ticket_policy['log'][-8:]}"
    ws_policy = human.prepare("policy.set", {"gates": {"requirements": policy(count=2)}})
    # the prefix comes from the signer's own registry, never from the host
    ws8 = ws_policy["workspace_id"][-8:]
    assert signer.confirm_word(ws_policy, _registry("MINE")) == f"MINE~{ws8}/POLICY"
    settings = human.prepare("settings.set", {"set": {"lease_ttl_min": 10}})
    assert signer.confirm_word(settings, _registry("MINE")) == f"MINE~{ws8}/SETTINGS"
    other = {**settings, "workspace_id": settings["workspace_id"][:-8] + "0" * 8}  # same prefix, another workspace
    assert signer.confirm_word(other, _registry("MINE")) != signer.confirm_word(settings, _registry("MINE"))
    with pytest.raises(signer.SignerRefused):
        signer.confirm_word(settings, lambda wid: None)  # not in the signer's registry: nothing to type


def test_the_signer_header_names_the_three_ops_in_plain_words(human):
    from orch.host import signer

    _, key = human.ticket()
    for op, args, word in (
        ("move", {"ref": key, "to": "backlog"}, "move "),
        ("policy.set", {"gates": {"requirements": policy(count=2)}}, "set the gate policies"),
        ("settings.set", {"set": {"lease_ttl_min": 10}}, "change the workspace settings"),
    ):
        lines = signer.header(human.prepare(op, args), _registry())
        assert word in lines[1], (op, lines[1])


@pytest.mark.parametrize(
    "op,args,tamper",
    [
        ("move", None, lambda e: e.update(to="in_progress")),
        ("policy.set", {"gates": {"requirements": None}}, lambda e: e["gates"].update(nope={})),
        ("settings.set", {"set": {"lease_ttl_min": 10}}, lambda e: e["set"].update(other=1)),
    ],
)
def test_the_signer_refuses_a_person_op_event_that_changes_what_its_op_does_not(human, op, args, tamper):
    from orch.host import signer

    _, key = human.ticket()
    if op == "move":
        args = {"ref": key, "to": "backlog"}
    if op == "policy.set":
        args = {"gates": {"requirements": policy(count=2)}}
    prepared = human.prepare(op, args)
    tamper(prepared["events"][0])
    with pytest.raises(signer.SignerRefused, match="cannot sign"):
        human.check(prepared)


def test_a_workspace_policy_set_on_a_ticket_log_is_refused(human):
    from orch.host import signer

    _, key = human.ticket()
    prepared = human.prepare("policy.set", {"ref": key, "gates": {"requirements": policy(count=2)}})
    prepared["material"] = {**prepared["material"], "ticket": None}  # claims workspace scope on a ticket log
    with pytest.raises(signer.SignerRefused):
        human.check(prepared)
