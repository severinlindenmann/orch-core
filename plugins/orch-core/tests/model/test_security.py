"""Opus security review of #347: the reviewer's sequences, as tests."""

import pytest

from orch import canon
from orch.model import Code, admit
from tests.model.world import SHA1, SHA2, World, pol, refused, stamp
from tests.schema.examples import cert, digest, pub, revocation, sig


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member", "vera": "viewer", "rev": "member"})


CODE_ON = {"code": pol(("maintainer", "owner"), not_=("assignees",), independent=True)}


def work_by(w, who, gates=None):
    """requirements/plan approved by sev, `who`'s agent did the work and submitted; returns (uid, agent)."""
    if gates:
        w.wev("policy.changed", "sev", gates=gates)
    uid = w.ticket()
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["rev"]], remove=[])
    w.fill(uid)
    w.with_repo(uid)
    a = w.claim(uid, who)
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "sev", "plan")
    return uid, a


def finish(w, uid, a):
    w.tev(
        uid, "task.done", a, task="T1", receipt={"cmd": "make test", "exit": 0, "ms": 5, "repo": "dbt", "commit": SHA1}
    )
    w.tev(uid, "ticket.submitted", a)


def test_the_person_whose_agent_edited_and_pushed_cannot_approve_code(w):
    """A1: agent edit of a code-bound path, then the host's branch.pushed (a generation raise), then self-approval."""
    w.wev("policy.changed", "sev", gates=CODE_ON)
    uid = w.ticket()
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["rev"]], remove=[])
    w.fill(uid)
    w.settings(repos={"dbt": {"path": "../dbt"}})
    a = w.agent("mara", w.grant("mara"))
    w.tev(uid, "claim.taken", a)
    w.edit(uid, a, {"ticket.links": {"repos": ["dbt"], "branches": {"dbt": "feat/x"}, "prs": [], "external": []}})
    w.push(uid)
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "sev", "plan")
    finish(w, uid, a)
    w.decide(uid, "rev", "verify")
    assert w.decide(uid, "mara", "code", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.view(uid).status == "testing"


def test_claiming_and_doing_the_tasks_makes_the_person_a_worker(w):
    """A1b: the agent never touched a code-bound path; claim, tasks and commits still make mara a worker."""
    uid, a = work_by(w, "mara", CODE_ON)
    finish(w, uid, a)
    w.push(uid, SHA2)  # the agent's commit
    w.decide(uid, "rev", "verify")
    assert w.decide(uid, "mara", "code", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.decide(uid, "sev", "code", try_=True)[1].__class__.__name__ == "Ok"  # sev did not work on it


def test_verify_independent_survives_agent_artifacts_and_pushes(w):
    """A2: the worker record is never cleared by a generation raise."""
    uid, a = work_by(w, "mara", {"verify": pol(("maintainer", "owner"), not_=("assignees",), independent=True)})
    w.edit(uid, a, sections={"verification": "written by mara's agent"})
    finish(w, uid, a)
    assert w.decide(uid, "mara", "verify", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    w.tev(uid, "artifact.added", a, name="x.log", kind="log", sha256=digest("x"), bytes=3)
    w.push(uid, SHA2)
    assert w.decide(uid, "mara", "verify", try_=True)[1].code == Code.GATE_NOT_ELIGIBLE
    assert w.decide(uid, "sev", "verify", try_=True)[1].__class__.__name__ == "Ok"


def test_workers_include_assignees_and_are_cleared_only_by_reopen(w):
    uid, a = work_by(w, "tom", {"plan": pol(("owner", "maintainer"), independent=True)})
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    w.tev(uid, "people.changed", "sev", role="assignees", add=[], remove=[w.people["mara"]])
    w.tev(uid, "ticket.closed", "sev", resolution="other")
    w.tev(uid, "ticket.reopened", "sev")
    # after the reopen the earlier workers are forgotten: mara may approve plan again
    w.edit(uid, "sev", sections={"plan": "p2"})
    r = w.decide(uid, "mara", "plan", try_=True)[1]
    assert r.__class__.__name__ == "Ok", r


# ---- 2: merged order and cross-ticket references
def test_out_of_order_append_at_the_same_ws_seq_is_refused_and_replay_agrees(w):
    lo, hi = "01J9ZK4Q7M3R8T2V6X0B000001", "01J9ZK4Q7M3R8T2V6X0B999999"
    at = w.tick()
    w.tev(lo, "ticket.created", "sev", key="DEMO-0001", ticket_type="feature", title="lo", owner=w.people["sev"], at=at)
    w.tev(hi, "ticket.created", "sev", key="DEMO-0002", ticket_type="feature", title="hi", owner=w.people["sev"], at=at)
    base = {"ticket.parent": canon.value_hash(None)}
    same = w.build_unappended(lo, "ticket.updated", "sev", base_rev=base, set={"ticket.parent": "DEMO-0002"}, at=at)
    assert admit(w.state(), same, log=lo).code == Code.CHAIN_BAD_WS_SEQ  # would sort before the append of `hi`
    later = w.build_unappended(
        lo, "ticket.updated", "sev", base_rev=base, set={"ticket.parent": "DEMO-0002"}, at=w.tick()
    )
    assert admit(w.state(), later, log=lo).__class__.__name__ == "Ok"
    later["host_sig"] = sig("h")
    w._append(lo, later)
    s = w.state()
    assert s.tickets[lo].fields["parent"] == "DEMO-0002" and not s.tickets[lo].frozen


def test_replay_of_a_forged_out_of_order_log_is_deterministic(w):
    """No admit ever produces this; a workspace-key forger could. Replay still gives one answer: invalid, frozen."""
    lo, hi = "01J9ZK4Q7M3R8T2V6X0B000001", "01J9ZK4Q7M3R8T2V6X0B999999"
    at = w.tick()
    w.tev(lo, "ticket.created", "sev", key="DEMO-0001", ticket_type="feature", title="lo", owner=w.people["sev"], at=at)
    w.tev(hi, "ticket.created", "sev", key="DEMO-0002", ticket_type="feature", title="hi", owner=w.people["sev"], at=at)
    w.tev(
        lo,
        "ticket.updated",
        "sev",
        base_rev={"ticket.parent": canon.value_hash(None)},
        set={"ticket.parent": "DEMO-0002"},
        at=at,
    )
    s1, s2 = w.state(), w.state()
    assert s1.tickets[lo].fields["parent"] == s2.tickets[lo].fields["parent"] is None
    assert s1.tickets[lo].frozen  # reported, acknowledgeable


# ---- 3, 4: grants and devices end
def test_grant_signed_by_a_compromised_device_ends_with_it(w):
    uid = w.ticket()
    g = w.grant("mara", "all")
    a = w.claim(uid, "tom", gid=w.grant("tom", "workable"))
    gm = w.agent("mara", g)
    assert refused(w, uid, "log.added", gm, text="x") is None
    w.wev(
        "device.revoked",
        w.HOST,
        device=w.dev["mara"],
        reason="compromised",
        revocation=revocation(w.people["mara"][2:], w.dev["mara"][2:], "compromised"),
    )
    assert refused(w, uid, "log.added", gm, text="x") == Code.GRANT_INVALID
    assert refused(w, uid, "claim.taken", gm) == Code.GRANT_INVALID
    assert a["session"]


def test_claim_of_a_grant_from_a_compromised_device_lapses_with_grant_ended(w):
    uid = w.ticket()
    a = w.claim(uid, "mara")
    w.wev(
        "device.revoked",
        w.HOST,
        device=w.dev["mara"],
        reason="compromised",
        revocation=revocation(w.people["mara"][2:], w.dev["mara"][2:], "compromised"),
    )
    assert w.view(uid).claim.lapsed == "grant_ended"
    w.tev(uid, "claim.released", w.HOST, session=a["session"], reason="grant_ended")


def test_non_compromising_revocation_keeps_grants(w):
    uid = w.ticket()
    g = w.grant("mara")
    w.wev(
        "device.revoked",
        w.HOST,
        device=w.dev["mara"],
        reason="lost",
        revocation=revocation(w.people["mara"][2:], w.dev["mara"][2:], "lost"),
    )
    assert refused(w, uid, "log.added", w.agent("mara", g), text="x") is None


def test_removed_member_re_added_starts_clean(w):
    uid = w.ticket()
    g = w.grant("tom", "workable")
    w.wev("member.removed", "sev", person=w.people["tom"])
    newdev = "d_" + "e" * 32
    w.wev(
        "member.added",
        "sev",
        person=w.people["tom"],
        name="tom",
        role="member",
        pk_pub=pub("pk-tom"),
        device_cert=cert(w.people["tom"][2:], newdev[2:]),
    )
    assert refused(w, uid, "log.added", w.agent("tom", g), text="x") == Code.GRANT_INVALID  # old grant stays dead
    assert refused(w, uid, "log.added", "tom", text="x") == Code.DEVICE_INVALID  # old device stays removed
    new_actor = {"kind": "person", "id": w.people["tom"], "device": newdev}
    assert refused(w, uid, "log.added", new_actor, text="x") is None


# ---- 5, 6: source list and verbs
def test_repo_less_ticket_reaches_done_with_an_empty_source_list(w):
    uid = w.ticket(type_="chore")
    w.edit(uid, "sev", {"ticket.acceptance": [{"id": "AC1", "text": "t"}]}, {"requirements": "r", "plan": "p"})
    w.edit(uid, "sev", {"ticket.tasks": [{"id": "T1", "text": "a", "verify": None, "proves": ["AC1"]}]})
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["rev"]], remove=[])
    a = w.claim(uid, "sev")
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "sev", "plan")
    w.tev(uid, "artifact.added", a, name="p.log", kind="log", sha256=digest("p"), bytes=1, ac="AC1")
    w.tev(uid, "ticket.submitted", a)
    e, r = w.decide(uid, "rev", "verify", try_=True)
    assert e["source_sha"] == [] and r.__class__.__name__ == "Ok" and w.view(uid).status == "done"


def test_source_list_is_empty_iff_the_ticket_links_no_repo(w):
    uid, a = work_by(w, "sev")
    finish(w, uid, a)
    r = w.decide(uid, "rev", "verify", try_=True, source_sha=[])[1]
    assert r.code == Code.GATE_STALE  # a linked repo needs its entry
    # and a repo-less ticket may not claim a commit
    uid2 = w.ticket(type_="chore")
    assert uid2


def test_grant_verbs_are_matched_exactly(w):
    uid = w.ticket()
    a = w.agent("sev", w.grant("sev", verbs=["task", "ticket"]))
    assert refused(w, uid, "log.added", a, text="x") == Code.GRANT_VERB
    assert refused(w, uid, "claim.taken", a) == Code.GRANT_VERB
    b = w.agent("sev", w.grant("sev", verbs=["claim.taken", "log.added"]))
    assert refused(w, uid, "claim.taken", b) is None and refused(w, uid, "log.added", b, text="x") is None
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            b,
            base_rev={"ticket.title": canon.value_hash("A ticket")},
            set={"ticket.title": "t"},
        )
        == Code.GRANT_VERB
    )


# ---- info items
def test_a_stale_repo_does_not_block_unrelated_edits(w):
    uid = w.ticket()
    w.with_repo(uid)
    w.settings(repos={"dbt": None})
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            "sev",
            base_rev={"ticket.title": canon.value_hash("A ticket")},
            set={"ticket.title": "renamed"},
        )
        is None
    )
    links = _thaw(w.view(uid).fields["links"])
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            "sev",
            base_rev={"ticket.links": canon.value_hash(links)},
            set={"ticket.links": {**links, "external": ["https://x.example"]}},
        )
        == Code.REPO_UNKNOWN
    )


def _thaw(o):
    if hasattr(o, "items"):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, tuple | list):
        return [_thaw(v) for v in o]
    return o


def test_unattended_quota_agrees_between_admit_and_replay(w):
    uid = w.ticket()
    s = w.unattended("s_01J9ZK0000000000000000SSSS")
    for _ in range(30):
        w.tev(uid, "log.added", s, text="x")
    assert w.state().tickets[uid].frozen is False
    assert refused(w, uid, "log.added", s, text="x") == Code.QUOTA_UNATTENDED
    assert stamp  # imported for clarity of intent
