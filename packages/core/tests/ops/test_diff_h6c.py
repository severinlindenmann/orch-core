"""``diff`` since my approval and ``head``/``base`` in direct mode (H6c, §0.8, §0.8.5): the caller's own code approval
is
the base, another person's never is; ``head``/``base`` are authorized by the ticket's pushes."""

from __future__ import annotations

import pytest

from orch.ops.caps import Capabilities
from orch.ops.diff_core import diff_result
from orch.ops.errors import OrchError
from orch.store import observe
from tests.ops.humans import make_ticket, to_testing
from tests.ops.test_verdict import CODE_POLICY, LINKS


@pytest.fixture
def scene(hws, agent, me):
    """feat/x: c1 (main) - c2 (approved by rita) - c3 - c3b - c4 (head, pushed). The owner gave the verdict only."""
    hws.repo()
    s = {"c1": hws.git("rev-parse", "HEAD")}
    hws.git("branch", "main", "HEAD")
    hws.store.append(
        hws.person_event(hws.owner, "workspace", "policy.changed", gates={"code": CODE_POLICY}), log="workspace"
    )

    def commit(name):
        (hws.tmp / "proj" / f"{name}.txt").write_text(name + "\n")
        hws.git("add", f"{name}.txt")
        hws.git("commit", "-qm", name)
        s[name] = hws.git("rev-parse", "HEAD")

    commit("c2")
    key = make_ticket(agent)
    assert agent("set", key, LINKS).code == 0
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "pass", "--ref", key).code == 0
    rita = hws.add_member("rita", "maintainer")
    hws.act_as(rita)
    r = me("approve", "code", "--ref", key)
    assert r.code == 0, r.err
    for n in ("c3", "c3b", "c4"):
        commit(n)
    observe.observe(hws.store, "1")  # the host sees the new head: a push c2 -> c4, which voids the approval
    return s, rita


def diff(hws, **kw):
    store = hws.other()
    try:
        return diff_result(Capabilities.direct({}), store, store.ticket("1"), **kw)
    finally:
        store.close()


def test_since_my_approval_uses_only_the_callers_own_approval(hws, scene):
    s, rita = scene
    assert hws.view("1").gates["code"].counting == ()  # the new push voided it: the approval still names its commit
    got = diff(hws, since="my_approval", person=rita.ref)
    (src,) = got["sources"]
    assert (src["base_sha"], src["head_sha"], src["base_source"], src["base_moved"]) == (
        s["c2"],
        s["c4"],
        "approved",
        0,
    )
    assert {c["sha"] for c in src["commits"]} == {s["c4"], s["c3b"], s["c3"]}
    assert sorted(f["path"] for f in got["files"]) == ["c3.txt", "c3b.txt", "c4.txt"]
    assert set(got) == {"sources", "files", "unified", "truncated"} and "approved" not in got["unified"]
    # the owner gave a verdict and the agent's work was theirs, but no code approval: nothing is borrowed from rita
    for who in (hws.owner.ref, None):
        with pytest.raises(OrchError) as e:
            diff(hws, since="my_approval", person=who)
        assert e.value.code == "not_found" and "no approval of yours" in e.value.message


def test_head_and_base_in_direct_mode(hws, scene):
    s, _ = scene
    got = diff(hws, repo="proj", base=s["c2"])  # the push's before
    assert (got["sources"][0]["base_source"], got["sources"][0]["head_sha"]) == ("pushed", s["c4"])
    got = diff(hws, repo="proj", head=s["c3"], base=s["c2"])
    assert (got["sources"][0]["base_source"], got["sources"][0]["head_sha"]) == ("first_parent", s["c3"])
    assert [f["path"] for f in got["files"]] == ["c3.txt"]
    got = diff(hws, repo="proj", head=s["c3"])
    assert (got["sources"][0]["base_sha"], got["sources"][0]["base_source"]) == (s["c1"], "local_ref")
    for bad in ({"head": "f" * 40}, {"base": s["c3"]}, {"head": s["c2"], "base": s["c3"]}):
        with pytest.raises(OrchError) as e:
            diff(hws, repo="proj", **bad)
        assert e.value.code == "not_found"
    got = diff(hws, repo="proj", head=s["c3"], base=s["c2"])
    assert got["sources"][0]["repo"] == "proj"


@pytest.mark.parametrize(
    "kw",
    [
        {"since": "other"},
        {"since": "my_approval", "head": "a" * 40, "repo": "proj"},
        {"since": "my_approval", "base": "a" * 40, "repo": "proj"},
        {"head": "a" * 40},  # no repo
        {"base": "a" * 40},
        {"repo": "proj", "head": "ABC"},
        {"repo": "proj", "base": "a" * 39},
    ],
)
def test_bad_arguments_are_invalid_input(hws, scene, kw):
    with pytest.raises(OrchError) as e:
        diff(hws, person="x", **kw)
    assert e.value.code == "invalid.input"


def test_a_revoked_approver_device_or_removed_member_leaves_no_approval(hws, scene):
    """Fail closed: the approval of a device that was revoked is no base for its person, and an older approval of the
    same person never replaces it (the walk stops at the newest failing one)."""
    s, rita = scene
    assert diff(hws, since="my_approval", person=rita.ref)["sources"][0]["base_sha"] == s["c2"]
    hws.revoke_device(rita.device, rita)
    for person in (rita.ref, hws.owner.ref):
        with pytest.raises(OrchError) as e:
            diff(hws, since="my_approval", person=person)
        assert e.value.code == "not_found" and "no approval of yours" in e.value.message


def test_a_voided_approval_is_not_used(hws, scene):
    from orch.ops.diff_core import approvals_of

    s, rita = scene
    store = hws.other()
    try:
        view = store.ticket("1")
        assert [a["sha"] for a in approvals_of(store, view, rita.ref)] == [s["c2"]]
        (d,) = [d for d in store.state._core.tickets[view.uid].gates["code"].decisions if d.kind == "approve"]
        d.voided = True  # what a removed member or a compromised device does to a decision
        assert approvals_of(store, view, rita.ref) == []
    finally:
        store.close()
