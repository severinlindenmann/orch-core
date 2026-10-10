# ruff: noqa: E731
"""§5.7 raise table, row by row: which gates an event raises, at most +1 each, in every status."""

import pytest

from orch import canon
from orch.model import Code
from tests.model.world import SHA1, SHA2, World, pol, refused
from tests.schema.examples import digest

ALL = ("requirements", "plan", "verify", "code")
FROM_PLAN = ("plan", "verify", "code")
FROM_VERIFY = ("verify", "code")


def gens(w, uid):
    return {g: gv.gen for g, gv in w.view(uid).gates.items()}


def delta(w, uid, act):
    before = gens(w, uid)
    act()
    after = gens(w, uid)
    return tuple(g for g in ALL if after[g] - before[g] == 1), {g: after[g] - before[g] for g in ALL}


@pytest.fixture
def env():
    """A filled feature ticket with a linked repo, the code gate on, an agent claim, one artifact."""
    w = World().bootstrap({"mara": "maintainer", "tom": "member"})
    w.wev("policy.changed", "sev", gates={"code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    w.with_repo(uid)
    a = w.claim(uid)
    w.tev(uid, "artifact.added", a, name="shot.png", kind="screenshot", sha256=digest("shot"), bytes=10)
    return w, uid, a


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("ticket.size", "m", ALL),
        ("ticket.acceptance", [{"id": "AC1", "text": "it works"}, {"id": "AC2", "text": "more"}], ALL),
        (
            "ticket.tasks",
            [{"id": "T1", "text": "do it again", "verify": {"cmd": "make test"}, "proves": ["AC1"]}],
            FROM_PLAN,
        ),
        ("ticket.links", {"repos": ["dbt"], "branches": {"dbt": "feat/other"}, "prs": [], "external": []}, FROM_VERIFY),
        ("ticket.title", "New title", ()),
        ("ticket.priority", "high", ()),
        ("ticket.labels", ["x"], ()),
    ],
)
def test_ticket_updated_raises_the_gates_whose_bound_paths_it_names(env, path, value, expected):
    w, uid, a = env
    got, _ = delta(w, uid, lambda: w.edit(uid, a, {path: value}))
    assert got == expected


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("summary", ALL),
        ("context", ALL),
        ("requirements", ALL),
        ("out_of_scope", ALL),
        ("plan", FROM_PLAN),
        ("decisions", FROM_PLAN),
        ("verification", FROM_VERIFY),
        ("current_state", ()),
    ],
)
def test_sections_raise_their_gate_and_the_later_ones(env, section, expected):
    w, uid, a = env
    got, _ = delta(w, uid, lambda: w.edit(uid, a, sections={section: "new text"}))
    assert got == expected


def test_same_value_still_raises(env):
    w, uid, a = env
    w.edit(uid, a, {"ticket.size": "m"})
    got, _ = delta(w, uid, lambda: w.edit(uid, a, {"ticket.size": "m"}))
    assert got == ALL


def test_at_most_one_raise_however_many_rows_match(env):
    w, uid, a = env
    _, d = delta(
        w,
        uid,
        lambda: w.edit(
            uid,
            a,
            {"ticket.size": "l", "ticket.acceptance": [{"id": "AC1", "text": "t"}]},
            {"context": "c2", "plan": "p2"},
        ),
    )
    assert d == {"requirements": 1, "plan": 1, "verify": 1, "code": 1}
    _, d = delta(w, uid, lambda: w.push(uid, SHA2))  # row 5 and the cascade row both match code
    assert d == {"requirements": 0, "plan": 0, "verify": 1, "code": 1}


def test_edit_external_raises_the_gate_of_the_section_and_voided_gates_is_derived(env):
    w, uid, a = env
    new = {"hash": canon.section_hash("outside"), "refs": []}
    got, _ = delta(
        w, uid, lambda: w.tev(uid, "edit.external", w.HOST, sections={"plan": new}, voided_gates=[], normalised=False)
    )
    assert got == FROM_PLAN
    w.decide(uid, "sev", "requirements")
    new2 = {"hash": canon.section_hash("outside 2"), "refs": []}
    wrong = w.try_(uid, "edit.external", w.HOST, sections={"context": new2}, voided_gates=[], normalised=False)[1]
    assert wrong.code == Code.AUTH_INVALID_EVENT
    ok = w.try_(
        uid, "edit.external", w.HOST, sections={"context": new2}, voided_gates=["requirements"], normalised=False
    )[1]
    assert ok.__class__.__name__ == "Ok"


def test_artifacts_raise_verify_and_requirements_only_when_referenced(env):
    w, uid, a = env
    got, _ = delta(
        w, uid, lambda: w.tev(uid, "artifact.added", a, name="b.log", kind="log", sha256=digest("b"), bytes=3)
    )
    assert got == FROM_VERIFY
    w.edit(uid, a, sections={"context": "see (artifact:b.log)"})
    got, _ = delta(
        w,
        uid,
        lambda: w.tev(
            uid, "artifact.replaced", a, name="b.log", kind="log", sha256=digest("b2"), bytes=4, replaces=digest("b")
        ),
    )
    assert got == ALL
    got, _ = delta(
        w,
        uid,
        lambda: w.tev(
            uid,
            "artifact.replaced",
            a,
            name="shot.png",
            kind="screenshot",
            sha256=digest("s2"),
            bytes=4,
            replaces=digest("shot"),
        ),
    )
    assert got == FROM_VERIFY


def test_task_events(env):
    w, uid, a = env
    t = lambda typ, **kw: w.tev(uid, typ, a, task="T1", **kw)
    assert delta(w, uid, lambda: t("task.started"))[0] == ()
    assert delta(w, uid, lambda: t("task.blocked", reason="x"))[0] == ()
    assert delta(w, uid, lambda: t("task.skipped", reason="x"))[0] == FROM_VERIFY
    assert delta(w, uid, lambda: t("task.reopened"))[0] == FROM_VERIFY
    receipt = {"cmd": "make test", "exit": 0, "ms": 1, "repo": "dbt", "commit": SHA1}
    assert delta(w, uid, lambda: t("task.done", receipt=receipt))[0] == FROM_VERIFY


def test_branch_pushed_raises_verify_and_code(env):
    w, uid, a = env
    assert delta(w, uid, lambda: w.push(uid, SHA2))[0] == FROM_VERIFY


def test_people_changed_raises_gates_whose_effective_policy_names_the_role(env):
    w, uid, a = env
    p = w.people
    assert (
        delta(w, uid, lambda: w.tev(uid, "people.changed", "sev", role="watchers", add=[p["tom"]], remove=[]))[0] == ()
    )
    assert (
        delta(w, uid, lambda: w.tev(uid, "people.changed", "sev", role="reviewers", add=[p["tom"]], remove=[]))[0]
        == FROM_VERIFY
    )
    assert (
        delta(w, uid, lambda: w.tev(uid, "people.changed", "sev", role="assignees", add=[p["tom"]], remove=[]))[0]
        == FROM_VERIFY
    )
    assert delta(w, uid, lambda: w.tev(uid, "people.changed", "sev", role="owner", add=[p["mara"]], remove=[]))[0] == ()
    # `independent` adds assignees to the roles a gate names
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner"], 1, [], "all", True)})
    assert (
        delta(w, uid, lambda: w.tev(uid, "people.changed", "sev", role="assignees", add=[p["mara"]], remove=[]))[0]
        == FROM_PLAN
    )


def test_policy_changed_raises_the_named_gate_and_later_ones(env):
    w, uid, a = env
    assert delta(w, uid, lambda: w.tev(uid, "policy.changed", "sev", gates={"plan": pol(["owner"], 2)}))[0] == FROM_PLAN
    assert (
        delta(w, uid, lambda: w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"])}))[0]
        == ALL
    )


def test_addon_events_raise_gates_their_binds_name(env):
    w, uid, a = env
    binds = {"fields": {"points": ["plan"]}, "sections": []}
    granted = lambda: w.wev(
        "addon.granted",
        "sev",
        name="estimate",
        version="1.0.0",
        package_sha256=digest("pkg"),
        capabilities=[],
        binds=binds,
    )
    assert delta(w, uid, granted)[0] == FROM_PLAN
    assert w.view(uid).gates["plan"].input["addon_packages"] == {"estimate": digest("pkg")}
    assert delta(w, uid, lambda: w.wev("addon.disabled", "sev", name="estimate"))[0] == FROM_PLAN
    assert w.view(uid).gates["plan"].input["addon_packages"] == {}
    assert delta(w, uid, lambda: w.wev("addon.purged", "sev", name="estimate"))[0] == FROM_PLAN


def test_changes_requested_and_failing_verdict(env):
    w, uid, a = env
    assert delta(w, uid, lambda: w.decide(uid, "sev", "plan", kind="changes"))[0] == FROM_PLAN
    assert delta(w, uid, lambda: w.decide(uid, "sev", "requirements", kind="changes"))[0] == ALL
    w2 = World().bootstrap({"mara": "maintainer"})
    uid2 = w2.ticket()
    w2.to_testing(uid2)
    w2.tev(uid2, "people.changed", "sev", role="reviewers", add=[w2.people["mara"]], remove=[])
    got, _ = delta(w2, uid2, lambda: w2.decide(uid2, "mara", "verify", kind="fail"))
    assert got == ("verify",)  # code does not apply here


def test_reopen_and_restore_raise_every_gate(env):
    w, uid, a = env
    w.tev(uid, "ticket.closed", "sev", resolution="other")
    assert delta(w, uid, lambda: w.tev(uid, "ticket.reopened", "sev"))[0] == ALL
    head = w.ws[-1]
    assert (
        delta(
            w,
            uid,
            lambda: w.wev(
                "restore",
                "sev",
                from_seq=head["seq"],
                head=canon.event_head(head),
                abandoned=None,
                abandoned_decisions=[],
                reason="r",
                based_on_override=canon.event_head(head),
            ),
        )[0]
        == ALL
    )


def test_a_change_of_the_first_count_approvals_raises_later_gates(env):
    w, uid, a = env
    got, _ = delta(w, uid, lambda: w.decide(uid, "sev", "requirements"))
    assert got == FROM_PLAN
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner", "maintainer"], 2)})
    w.decide(uid, "sev", "plan")
    got, _ = delta(w, uid, lambda: w.decide(uid, "mara", "plan"))
    assert got == FROM_VERIFY  # the second plan approval changes plan's first-count set


def test_member_removed_voids_only_gates_that_have_not_reached_count(env):
    w, uid, a = env
    w.wev(
        "policy.changed",
        "sev",
        gates={"requirements": pol(["owner", "maintainer"], 2), "plan": pol(["owner", "maintainer"])},
    )
    w.decide(uid, "mara", "requirements")
    w.decide(uid, "mara", "plan")
    assert w.view(uid).gates["plan"].approved and not w.view(uid).gates["requirements"].approved
    got, _ = delta(w, uid, lambda: w.wev("member.removed", "sev", person=w.people["mara"]))
    assert got == ALL  # requirements was unreached: voided, raised, and everything after it
    assert w.view(uid).gates["requirements"].counting == ()
    # plan reached its count before the removal and would keep it: but requirements' raise retired it by `prior`


def test_member_removed_keeps_approvals_on_a_reached_gate(env):
    w, uid, a = env
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner", "maintainer"])})
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "mara", "plan")
    got, _ = delta(w, uid, lambda: w.wev("member.removed", "sev", person=w.people["mara"]))
    assert got == () and w.view(uid).gates["plan"].approved
    assert w.people["mara"] in w.view(uid).gates["plan"].counting


def test_role_changed_voids_like_a_removal(env):
    w, uid, a = env
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 2)})
    w.decide(uid, "mara", "requirements")
    got, _ = delta(w, uid, lambda: w.wev("role.changed", "sev", person=w.people["mara"], role="member"))
    assert got == ALL


def test_compromised_device_voids_even_a_reached_gate_and_flags_done_tickets(env):
    w, uid, a = env
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"])})
    w.decide(uid, "mara", "requirements")
    assert w.view(uid).gates["requirements"].approved
    from tests.schema.examples import revocation

    rev = revocation(w.people["mara"][2:], w.dev["mara"][2:], "compromised")
    got, _ = delta(
        w, uid, lambda: w.wev("device.revoked", "tom", device=w.dev["mara"], reason="compromised", revocation=rev)
    )
    assert got == ALL and not w.view(uid).gates["requirements"].approved


def test_gate_invalidated_raises_nothing_and_changes_no_status(env):
    w, uid, a = env
    e = w.decide(uid, "sev", "requirements")
    w.edit(uid, a, sections={"context": "x"})
    status = w.view(uid).status
    got, _ = delta(
        w,
        uid,
        lambda: w.tev(uid, "gate.invalidated", w.HOST, gate="requirements", cause="content_changed", voided=[e["id"]]),
    )
    assert got == () and w.view(uid).status == status


def test_done_is_sticky_and_leaves_only_by_reopen_push_or_code_rule():
    w = World().bootstrap({"mara": "maintainer", "tom": "member"})
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(uid, "mara", "verify")
    assert w.view(uid).status == "done"
    w.tev(uid, "people.changed", "sev", role="watchers", add=[w.people["tom"]], remove=[])  # not named by a policy
    assert w.view(uid).status == "done"
    w.wev("policy.changed", "sev", gates={"verify": pol(["reviewers", "owner"], 1, ["assignees"])})
    assert w.view(uid).status == "done"  # a workspace policy change doesn't move a done ticket
    assert w.view(uid).gates["verify"].gen > 0
    verdict_id = [d for d in w.view(uid).gates["verify"].decisions if d.kind == "pass"][0].id
    assert (
        refused(w, uid, "people.changed", "sev", role="reviewers", add=[w.people["tom"]], remove=[])
        == Code.TICKET_FROZEN
    )
    assert (
        refused(
            w, uid, "ticket.updated", "sev", base_rev={"ticket.size": canon.value_hash(None)}, set={"ticket.size": "m"}
        )
        == Code.TICKET_FROZEN
    )
    w.tev(uid, "ticket.reopened", "sev")
    v = w.view(uid)
    assert v.status == "open" and not v.gates["verify"].approved
    assert any(
        d.id == verdict_id and not d.counting for d in v.gates["verify"].decisions
    )  # the old verdict doesn't count


def test_push_sends_a_done_ticket_back_to_testing_only_for_a_new_sha_on_an_existing_ref():
    w = World().bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(uid, "mara", "verify")
    assert w.view(uid).status == "done"
    # a ref or identity change on a done ticket is shown, not appended
    assert (
        refused(
            w,
            uid,
            "branch.pushed",
            w.HOST,
            repo_name="dbt",
            repo_id="https://github.com/acme/energy-dbt",
            ref="refs/heads/other",
            sha=SHA2,
            before={"repo_id": "https://github.com/acme/energy-dbt", "ref": "refs/heads/feat/x", "sha": SHA1},
        )
        == Code.SOURCE_NOT_NEW
    )
    w.push(uid, SHA2)
    assert w.view(uid).status == "testing"
