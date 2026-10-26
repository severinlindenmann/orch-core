# ruff: noqa: E731
"""Tasks, receipts, evidence, artifacts (§6, A4)."""

import pytest

from orch.model import Code
from tests.model.world import SHA1, SHA2, World, refused
from tests.schema.examples import digest

TASKS = [
    {"id": "T1", "text": "a", "verify": {"cmd": "make a"}, "proves": ["AC1"]},
    {"id": "T2", "text": "b", "verify": None, "proves": ["AC2"]},
]
AC = [{"id": "AC1", "text": "one"}, {"id": "AC2", "text": "two"}]


@pytest.fixture
def env():
    w = World().bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    w.edit(uid, "sev", {"ticket.acceptance": AC})
    w.edit(uid, "sev", {"ticket.tasks": TASKS})
    w.with_repo(uid)
    a = w.claim(uid)
    return w, uid, a


def done(w, uid, a, cmd="make a", exit_=0, commit=SHA1, repo="dbt"):
    return w.try_(
        uid, "task.done", a, task="T1", receipt={"cmd": cmd, "exit": exit_, "ms": 1, "repo": repo, "commit": commit}
    )


def test_task_state_machine(env):
    w, uid, a = env
    st = lambda: w.view(uid).tasks[0].state  # noqa: E731
    w.tev(uid, "task.started", a, task="T1")
    assert st() == "started"
    w.tev(uid, "task.blocked", a, task="T1", reason="x")
    assert st() == "blocked"
    w.tev(uid, "task.reopened", a, task="T1")
    assert st() == "open"
    assert refused(w, uid, "task.reopened", a, task="T1") == Code.TASK_STATE
    w.tev(uid, "task.skipped", a, task="T1", reason="not needed")
    assert st() == "skipped"
    assert refused(w, uid, "task.done", a, task="T1") == Code.TASK_STATE
    assert refused(w, uid, "task.done", a, task="T9") == Code.TASK_UNKNOWN


def test_a_done_task_always_has_a_holder(env):
    w, uid, a = env
    w.tev(uid, "task.started", a, task="T1")
    done(w, uid, a)
    t = w.view(uid).tasks[0]
    assert t.state == "done" and t.holder == a["session"] and t.leased_by is None
    w.tev(uid, "task.done", a, task="T2")
    assert w.view(uid).tasks[1].holder == a["session"]


def test_receipt_rules(env):
    w, uid, a = env
    assert done(w, uid, a, cmd="other")[1].code == Code.TASK_BAD_RECEIPT
    assert done(w, uid, a, exit_=1)[1].code == Code.TASK_BAD_RECEIPT
    assert done(w, uid, a, repo="elsewhere")[1].code == Code.SOURCE_UNLINKED
    assert (
        refused(
            w, uid, "task.done", a, task="T2", receipt={"cmd": "x", "exit": 0, "ms": 1, "repo": None, "commit": None}
        )
        == Code.TASK_BAD_RECEIPT
    )


def test_ac_evidence_from_receipts_needs_the_current_commit(env):
    w, uid, a = env
    done(w, uid, a)
    ac = {x.id: x.evidence for x in w.view(uid).acceptance}
    assert ac["AC1"] == ("task:T1",) and ac["AC2"] == ()
    w.push(uid, SHA2)
    assert {x.id: x.evidence for x in w.view(uid).acceptance}["AC1"] == ()  # the receipt is for an older commit


def test_ac_evidence_from_artifacts_only_from_granted_agents_and_persons(env):
    w, uid, a = env
    w.tev(uid, "artifact.added", a, name="s.png", kind="screenshot", sha256=digest("s"), bytes=1, ac="AC2")
    assert {x.id: x.evidence for x in w.view(uid).acceptance}["AC2"] == ("artifact:s.png",)
    w.tev(uid, "artifact.added", w.unattended(), name="u.png", kind="screenshot", sha256=digest("u"), bytes=1)
    assert (
        refused(
            w,
            uid,
            "artifact.added",
            w.unattended(),
            name="v.png",
            kind="screenshot",
            sha256=digest("v"),
            bytes=1,
            ac="AC1",
        )
        == Code.UNATTENDED_DENIED
    )
    arts = {x.name: x for x in w.view(uid).artifacts}
    assert arts["s.png"].evidence and not arts["u.png"].evidence


def test_artifact_rules(env):
    w, uid, a = env
    kw = {"kind": "log", "sha256": digest("l"), "bytes": 1}
    w.tev(uid, "artifact.added", a, name="a.log", **kw)
    assert refused(w, uid, "artifact.added", a, name="a.log", **kw) == Code.ARTIFACT_EXISTS
    assert refused(w, uid, "artifact.added", a, name="b.log", ac="AC9", **kw) == Code.TICKET_BAD_REFERENCE
    assert (
        refused(w, uid, "artifact.added", a, name="b.log", kind="feedback", sha256=digest("f"), bytes=1)
        == Code.ARTIFACT_KIND
    )
    assert refused(w, uid, "artifact.added", "sev", name="f.md", kind="feedback", sha256=digest("f"), bytes=1) is None
    assert (
        refused(
            w, uid, "artifact.replaced", a, name="a.log", replaces=digest("wrong"), **{**kw, "sha256": digest("l2")}
        )
        == Code.ARTIFACT_BAD_REPLACES
    )
    assert refused(w, uid, "artifact.replaced", a, name="zzz", replaces=digest("l"), **kw) == Code.ARTIFACT_UNKNOWN
    assert (
        refused(w, uid, "artifact.replaced", a, name="a.log", replaces=digest("l"), **{**kw, "sha256": digest("l2")})
        is None
    )
    assert refused(w, uid, "artifact.added", a, name="c", kind="chart", addon="nope", ref="r") == Code.ADDON_UNKNOWN


def test_body_references_must_name_manifest_artifacts(env):
    w, uid, a = env
    v = w.view(uid)
    assert (
        w.try_(
            uid,
            "ticket.updated",
            a,
            base_rev={"body.context": v.gates["requirements"].input["sections"]["context"]},
            sections={"context": {"hash": digest("x"), "refs": ["ghost.png"]}},
        )[1].code
        == Code.BODY_UNKNOWN_ARTIFACT
    )


def test_edit_conflicts_and_references(env):
    w, uid, a = env
    stale = "sha256:" + "0" * 64
    assert (
        refused(w, uid, "ticket.updated", a, base_rev={"ticket.title": stale}, set={"ticket.title": "x"})
        == Code.CONFLICT_SECTION
    )
    v = w.view(uid)
    h = v.gates["requirements"].input["sections"]["context"]
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            a,
            base_rev={"body.context": stale},
            sections={"context": {"hash": digest("n"), "refs": []}},
        )
        == Code.CONFLICT_SECTION
    )
    assert h
    from orch import canon

    rev = lambda path, cur: {path: canon.value_hash(cur)}  # noqa: E731
    bad_proves = [{"id": "T1", "text": "a", "verify": None, "proves": ["AC7"]}]
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            a,
            base_rev=rev("ticket.tasks", [dict(t) for t in TASKS]),
            set={"ticket.tasks": bad_proves},
        )
        is not None
    )
    assert (
        refused(w, uid, "ticket.updated", a, base_rev=rev("ticket.parent", None), set={"ticket.parent": "DEMO-0099"})
        == Code.TICKET_BAD_REFERENCE
    )
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            a,
            base_rev=rev("ticket.links", {"repos": ["dbt"], "branches": {"dbt": "feat/x"}, "prs": [], "external": []}),
            set={"ticket.links": {"repos": ["nope"], "branches": {}, "prs": [], "external": []}},
        )
        == Code.REPO_UNKNOWN
    )
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            a,
            base_rev={"body.findings": canon.section_hash("")},
            sections={"findings": {"hash": digest("n"), "refs": []}},
        )
        == Code.BODY_UNKNOWN_SECTION
    )  # not a feature section


def test_done_task_in_a_closed_ticket_and_submit_needs_branch_and_evidence(env):
    w, uid, a = env
    e = w.try_(uid, "ticket.submitted", a)[1]
    assert e.code == Code.SUBMIT_INCOMPLETE and "AC1" in e.detail and "AC2" in e.detail and "verification" in e.detail
