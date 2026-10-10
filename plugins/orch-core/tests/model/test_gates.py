"""§5.7: what makes a decision count: hash, policy hash, generation, source list, eligibility, completeness."""

import pytest

from orch import canon
from orch.model import Code
from tests.model.world import SHA1, SHA2, World, pol, refused


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member", "vera": "viewer"})


def counting(w, uid, gate):
    return w.view(uid).gates[gate].counting


def test_gate_hash_input_is_rebuilt_from_events_and_hashes_with_canon(w):
    uid = w.ticket()
    w.fill(uid)
    w.with_repo(uid)
    v = w.view(uid)
    for gate in ("requirements", "plan", "verify", "code"):
        g = v.gates[gate]
        assert set(g.input) == set(canon.GATE_KEYS)
        assert g.hash == canon.gate_hash(_thaw(g.input))
    plan = v.gates["plan"].input
    assert plan["tasks"][0]["id"] == "T1" and plan["tasks"][0]["verify"]["cmd"] == "make test"
    assert plan["prior"]["requirements"]["gen"] == v.gates["requirements"].gen
    assert v.gates["verify"].input["source_sha"][0]["sha"] == SHA1
    assert v.gates["requirements"].input["workspace_id"] == w.workspace_id
    assert v.gates["code"].input["sections"] == {} and v.gates["code"].input["artifacts"] == {}


def _thaw(o):
    if hasattr(o, "items"):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, tuple | list):
        return [_thaw(v) for v in o]
    return o


def test_approval_counts_and_gate_is_approved(w):
    uid = w.ticket()
    w.fill(uid)
    w.decide(uid, "sev", "requirements")
    g = w.view(uid).gates["requirements"]
    assert g.approved and g.counting == (w.people["sev"],)
    assert not w.state().workspace.invalid


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"hash": "sha256:" + "ab" * 32}, Code.GATE_STALE),
        ({"policy_hash": "sha256:" + "cd" * 32}, Code.GATE_STALE),
        ({"gate_gen": 99}, Code.GATE_STALE),
    ],
)
def test_stale_binding_is_refused(w, over, code):
    uid = w.ticket()
    w.fill(uid)
    assert w.decide(uid, "sev", "requirements", try_=True, **over)[1].code == code


def test_gate_stale_after_content_change_and_voided_approval_is_retired_for_good(w):
    uid = w.ticket()
    w.fill(uid)
    w.decide(uid, "sev", "requirements")
    old_hash = w.view(uid).gates["requirements"].hash
    old_gen = w.view(uid).gates["requirements"].gen
    w.edit(uid, "sev", sections={"context": "changed"})
    assert not w.view(uid).gates["requirements"].approved
    w.edit(uid, "sev", sections={"context": "context text"})  # revert: the old hash is back ...
    g = w.view(uid).gates["requirements"]
    assert g.hash != old_hash or g.gen > old_gen  # ... but the generation is not
    assert g.gen > old_gen and not g.approved and not g.counting
    assert w.decide(uid, "sev", "requirements", try_=True, gate_gen=old_gen, hash=old_hash)[1].code == Code.GATE_STALE


def test_eligibility_tokens_and_viewer_never_approves(w):
    uid = w.ticket()
    w.fill(uid)
    assert w.decide(uid, "tom", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert (
        w.decide(uid, "mara", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    )  # owner token is the owner role
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["vera"]], remove=[])
    w.wev("policy.changed", "sev", gates={"requirements": pol(["reviewers"])})
    assert w.decide(uid, "vera", "requirements", try_=True)[1].code == Code.ROLE_DENIED  # a viewer writes nothing


def test_not_excludes_even_when_also_approver(w):
    uid = w.ticket()
    w.fill(uid)
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, ["maintainer"])})
    assert w.decide(uid, "mara", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"


def test_count_needs_distinct_persons(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 2)})
    uid = w.ticket()
    w.fill(uid)
    w.decide(uid, "sev", "requirements")
    assert not w.view(uid).gates["requirements"].approved
    w.decide(uid, "sev", "requirements")  # the same person again
    assert not w.view(uid).gates["requirements"].approved and len(counting(w, uid, "requirements")) == 1
    w.decide(uid, "mara", "requirements")
    assert w.view(uid).gates["requirements"].approved


def test_incomplete_gate_is_refused_with_what_is_missing(w):
    uid = w.ticket()
    w.edit(uid, "sev", sections={"context": "c"})
    e, r = w.decide(uid, "sev", "requirements", try_=True)
    assert r.code == Code.GATE_INCOMPLETE and "requirements" in r.detail and "acceptance" in r.detail
    assert "out_of_scope" in r.detail
    w.edit(
        uid,
        "sev",
        sections={"requirements": "r", "out_of_scope": "o"},
        sets={"ticket.acceptance": [{"id": "AC1", "text": "t"}]},
    )
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"
    plan = w.decide(uid, "sev", "plan", try_=True)[1]
    assert plan.code == Code.GATE_INCOMPLETE and "tasks" in plan.detail and "plan" in plan.detail


def test_completeness_follows_ticket_type(w):
    uid = w.ticket(type_="chore")
    w.edit(uid, "sev", sections={"requirements": "r"}, sets={"ticket.acceptance": [{"id": "AC1", "text": "t"}]})
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"
    epic = w.ticket(type_="epic")
    w.edit(
        epic,
        "sev",
        sections={"context": "c", "requirements": "r", "out_of_scope": "o"},
        sets={"ticket.acceptance": [{"id": "AC1", "text": "t"}]},
    )
    assert w.decide(epic, "sev", "requirements", try_=True)[1].code == Code.GATE_INCOMPLETE  # epics need a summary


def test_unknown_artifact_reference_makes_gate_incomplete(w):
    uid = w.ticket()
    w.fill(uid)
    w.tev(
        uid,
        "edit.external",
        w.HOST,
        sections={"context": {"hash": canon.section_hash("see (artifact:nope.png)"), "refs": ["nope.png"]}},
        voided_gates=[],
        normalised=False,
    )
    r = w.decide(uid, "sev", "requirements", try_=True)[1]
    assert r.code == Code.GATE_INCOMPLETE and "artifact:nope.png" in r.detail


def test_changes_requested_raises_later_gates_and_voids_their_approvals(w):
    uid = w.ticket()
    w.fill(uid)
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "sev", "plan")
    assert w.view(uid).gates["plan"].approved
    w.decide(uid, "sev", "requirements", kind="changes")
    v = w.view(uid)
    assert not v.gates["requirements"].approved and not v.gates["plan"].approved
    assert v.gates["requirements"].decisions[-1].kind == "changes"


def test_delayed_approval_after_change_request_is_refused(w):
    uid = w.ticket()
    w.fill(uid)
    v = w.view(uid).gates["requirements"]
    w.decide(uid, "sev", "requirements", kind="changes")
    assert (
        w.try_(
            uid, "gate.approved", "sev", gate="requirements", gate_gen=v.gen, hash=v.hash, policy_hash=v.policy_hash
        )[1].code
        == Code.GATE_STALE
    )


def test_gate_that_does_not_apply_is_refused_and_not_needed(w):
    uid = w.ticket()
    w.to_testing(uid)
    assert (
        refused(
            w,
            uid,
            "gate.approved",
            "mara",
            gate="code",
            gate_gen=0,
            hash="sha256:" + "11" * 32,
            policy_hash="sha256:" + "22" * 32,
            source_sha=[{"repo": "https://github.com/acme/energy-dbt", "ref": "refs/heads/feat/x", "sha": SHA1}],
        )
        == Code.GATE_NOT_APPLICABLE
    )


def test_blocked_gate_has_no_eligible_approver(w):
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "policy.changed", "sev", gates={"requirements": pol(["owner"])})
    w.wev("policy.changed", "sev", gates={"requirements": pol(["maintainer"])})  # intersection is now empty
    g = w.view(uid).gates["requirements"]
    assert g.blocked and g.eligible == ()
    assert w.decide(uid, "sev", "requirements", try_=True)[1].code == Code.GATE_NO_ELIGIBLE
    assert any(n.kind == "no_eligible" for n in w.view(uid).needs)


def test_verdict_binds_the_commit(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    v = w.view(uid).gates["verify"]
    sl = [dict(x) for x in w.view(uid).source_list]
    w.push(uid, SHA2)
    # the signed generation is old now, and so is the source list
    r = w.try_(
        uid,
        "verdict.given",
        "mara",
        outcome="pass",
        gate_gen=v.gen,
        hash=v.hash,
        policy_hash=v.policy_hash,
        source_sha=sl,
    )[1]
    assert r.code == Code.GATE_STALE
    v2 = w.view(uid).gates["verify"]
    r = w.try_(
        uid,
        "verdict.given",
        "mara",
        outcome="pass",
        gate_gen=v2.gen,
        hash=v2.hash,
        policy_hash=v2.policy_hash,
        source_sha=sl,
    )[1]
    assert r.code == Code.GATE_STALE and "source" in r.detail
    w.decide(uid, "mara", "verify")
    assert w.view(uid).status == "done"


def test_source_missing_when_a_linked_repo_was_never_observed(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.settings(repos={"dbt": {"path": "../dbt"}, "api": {"path": "../api"}})
    links = {"repos": ["api", "dbt"], "branches": {"api": "b", "dbt": "feat/x"}, "prs": [], "external": []}
    w.edit(uid, "sev", {"ticket.links": links})
    assert w.decide(uid, "mara", "verify", try_=True)[1].code == Code.SOURCE_MISSING
    w.push(uid, SHA2, repo="api", repo_id="https://github.com/acme/api", ref="refs/heads/b")
    assert w.decide(uid, "mara", "verify", try_=True)[1].__class__.__name__ == "Ok"
    assert [x["repo"] for x in w.view(uid).source_list] == [
        "https://github.com/acme/api",
        "https://github.com/acme/energy-dbt",
    ]


def test_code_gate_never_by_an_assignee_and_needs_the_same_commit(w):
    w.wev(
        "policy.changed",
        "sev",
        gates={
            "code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True),
            "verify": pol(["maintainer", "owner"]),
        },
    )
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    w.decide(uid, "sev", "verify")
    assert w.view(uid).status == "testing"
    assert w.decide(uid, "mara", "code", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    w.tev(uid, "people.changed", "sev", role="assignees", add=[], remove=[w.people["mara"]])
    # removing the assignee changed the people hash: sign again
    assert (
        w.decide(uid, "mara", "code", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    )  # independent: she was an assignee in this generation


def test_code_policy_rule_d59(w):
    assert refused(w, "workspace", "policy.changed", "sev", gates={"code": pol(["owner"])}) == Code.POLICY_INVALID
    assert (
        refused(w, "workspace", "policy.changed", "sev", gates={"code": pol(["owner"], 1, ["assignees"], "all", False)})
        == Code.POLICY_INVALID
    )
    assert (
        refused(w, "workspace", "policy.changed", "sev", gates={"code": pol(["owner"], 1, ["assignees"], "all", True)})
        is None
    )


def test_independent_excludes_the_signer_who_touched_the_content_through_an_agent(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    g = w.grant("sev")
    a = w.agent("sev", g)
    w.edit(
        uid,
        a,
        {"ticket.acceptance": [{"id": "AC1", "text": "t"}]},
        {"context": "c", "requirements": "r", "out_of_scope": "o"},
    )
    assert w.decide(uid, "sev", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.decide(uid, "mara", "requirements", try_=True)[1].__class__.__name__ == "Ok"
    # independent off (the default): a sole owner approves their own agent's work
    uid2 = w.ticket()
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner"])})
    w.edit(uid2, a, sections={"plan": "p", "decisions": "d"}, sets={"ticket.tasks": []})
    assert (
        w.decide(uid2, "sev", "plan", try_=True)[1].code == Code.GATE_INCOMPLETE
    )  # no task yet, but not "not_eligible"


def test_independent_excludes_someone_who_was_an_assignee_in_this_generation(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    w.tev(uid, "people.changed", "sev", role="assignees", add=[], remove=[w.people["mara"]])
    assert w.decide(uid, "mara", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"


def test_not_assignees_without_independent_only_excludes_current_assignees(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, ["assignees"])})
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    assert w.decide(uid, "mara", "requirements", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    w.tev(uid, "people.changed", "sev", role="assignees", add=[], remove=[w.people["mara"]])
    assert w.decide(uid, "mara", "requirements", try_=True)[1].__class__.__name__ == "Ok"


def test_prior_binds_later_gates_to_the_earlier_approval(w):
    uid = w.ticket()
    w.fill(uid)
    before = w.view(uid).gates["plan"].input["prior"]["requirements"]["approvals"]
    assert before == ()
    e = w.decide(uid, "sev", "requirements")
    after = w.view(uid).gates["plan"].input["prior"]["requirements"]["approvals"]
    assert after == (e["id"],)


def test_gate_invalidated_lists_exactly_the_voided_approvals(w):
    uid = w.ticket()
    w.fill(uid)
    e = w.decide(uid, "sev", "requirements")
    w.edit(uid, "sev", sections={"context": "new"})
    bad = w.try_(uid, "gate.invalidated", w.HOST, gate="requirements", cause="content_changed", voided=[])[1]
    assert bad.code == Code.GATE_INVALIDATED_MISMATCH
    ok = w.try_(uid, "gate.invalidated", w.HOST, gate="requirements", cause="content_changed", voided=[e["id"]])[1]
    assert ok.__class__.__name__ == "Ok"
    # recorded: nothing pending any more, and it raised nothing
    gen = w.view(uid).gates["requirements"].gen
    assert (
        w.try_(uid, "gate.invalidated", w.HOST, gate="requirements", cause="content_changed", voided=[e["id"]])[1].code
        == Code.GATE_INVALIDATED_MISMATCH
    )
    assert w.view(uid).gates["requirements"].gen == gen


def test_source_sha_only_on_verdicts_and_code_approvals(w):
    uid = w.ticket()
    w.fill(uid)
    g = w.view(uid).gates["requirements"]
    r = w.try_(
        uid,
        "gate.approved",
        "sev",
        gate="requirements",
        gate_gen=g.gen,
        hash=g.hash,
        policy_hash=g.policy_hash,
        source_sha=[{"repo": "https://github.com/a/b", "ref": "refs/heads/x", "sha": SHA1}],
    )[1]
    assert r.code == Code.GATE_STALE


def test_evidence_of_receipts_is_bound_into_the_verify_hash(w):
    uid = w.ticket()
    a = w.to_testing(uid)
    h = w.view(uid).gates["verify"].hash
    w.tev(uid, "task.reopened", a, task="T1")
    w.tev(
        uid, "task.done", a, task="T1", receipt={"cmd": "make test", "exit": 0, "ms": 9, "repo": "dbt", "commit": SHA1}
    )
    assert w.view(uid).gates["verify"].hash != h
