"""H11d: worktrees (§12.7) end to end, the §12.13 acceptance: a real host, a real reader, the runner in this process
(its git runs for real, in a throwaway repository under ``tmp_path``; HOME is a temporary directory)."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host.pins import AgentPins
from orch.runner import enablement
from tests.host.dispatch_helpers import SESSION, Served, err, ok
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import UID, Lab, short_dir
from tests.host.test_runner_socket import ME, Runner
from tests.ops.helpers import Cli
from tests.ops.humans import _hws, make_ticket, to_testing

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only"),
]

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'
CODE_POLICY = {"approvers": ["maintainer", "owner"], "count": 1, "not": ["assignees"], "applies": "all",
               "independent": True}  # fmt: skip
ADDON = {"name": "worktrees", "version": "1.0.0", "package_sha256": "sha256:" + "0" * 64, "capabilities": [],
         "fields": {},
         "sections": [], "artifact_kinds": []}  # fmt: skip


def gitc(path: Path, *a: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(path), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    return subprocess.run(
        ["git", "-C", str(path), *a], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def w(tmp_path, monkeypatch):
    short = short_dir()  # before HOME moves: the base of a short socket path is under the real home
    base = tmp_path.resolve()
    home = base / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_NO_SERVICE_MANAGER", "1")
    gen = _hws(base, monkeypatch, live=False)
    hws = next(gen)
    agent, me = Cli(hws), Cli(hws, grant=False, session=None)
    repo = hws.repo()  # on feat/x, one commit
    gitc(repo, "checkout", "-q", "-b", "main")  # feat/x is free to be checked out in a worktree
    hws.store.append(
        hws.person_event(hws.owner, "workspace", "policy.changed", gates={"code": CODE_POLICY}), log="workspace"
    )
    hws.store.append(hws.person_event(hws.owner, "workspace", "addon.granted", **ADDON), log="workspace")
    key = make_ticket(agent, "A linked ticket")
    assert agent("set", key, LINKS).code == 0
    to_testing(agent, me, hws.tmp, key)
    r = agent("new", "Another ticket", "--priority", "high", "-m", "Not claimed.")
    assert r.code == 0, r.err
    other = r.first.split()[1]
    assert agent("set", other, LINKS).code == 0, "the other ticket links the same branch"
    r = agent("new", "No branch", "--priority", "high", "-m", "Links a repo only.")
    assert r.code == 0, r.err
    nob = r.first.split()[1]
    assert agent("set", nob, 'links={"repos":["proj"]}').code == 0
    hws.store.close()
    hws.store = None
    lab = Lab.__new__(Lab)
    lab.ws, lab.tmp, lab.state = hws, base, hws.host_state
    lab.short = short
    lab.run = lab.short / "r"
    lab.pins_dir = base / "pins"
    lab.pins_dir.mkdir()
    lab.pins = AgentPins(lab.pins_dir, owner=UID)
    lab.peers = lambda person_ref=None, uid=UID: {str(uid): {"class": "person", "person": hws.owner.ref}}
    try:
        served = Served(lab, runner_identity=lambda cred: cred.pid == ME)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    rstate = base / "runner-state"
    rstate.mkdir(mode=0o700)
    enablement.set_enabled(Path(lab.state), served.wid, True, UID, terminals=False, text="t")
    runner = Runner(SimpleNamespace(lab=lab, h=served.h), rstate)
    ns = SimpleNamespace(served=served, human=Human(served, hws.owner), repo=repo, key=key, other=other, nob=nob,
                        hws=hws,
                        c=served.client(), runner=runner, tmp=base)  # fmt: skip
    try:
        yield ns
    finally:
        runner.close()
        served.close()
        lab.close()
        for _ in gen:
            pass


def create(w, ref=None, *, g=False, **args):
    kw = {"grant": w.served.grant, "session": SESSION} if g else {}
    return w.c.req("worktree.create", {"ticket": ref or w.key, **args}, idem=True, **kw)


def remove(w, ref=None, *, g=False, **args):
    kw = {"grant": w.served.grant, "session": SESSION} if g else {}
    return w.c.req("worktree.remove", {"ticket": ref or w.key, **args}, **kw)


def listing(w, **args):
    return ok(w.c.req("worktree.list", args))["data"]["worktrees"]


def wt_path(w) -> Path:
    return Path(f"{w.repo}.worktrees") / f"{w.served.wid[:8]}-{w.key}"


# -- create, list ---------------------------------------------------------------------------------------------------


def test_create_makes_the_worktree_on_the_linked_branch_at_the_host_chosen_path(w):
    row = ok(create(w))["data"]["worktree"]
    path = wt_path(w)
    assert row["path"] == str(path) and row["branch"] == "feat/x" and row["ticket"] == w.key and row["state"] == "ok"
    assert (path / "impl.txt").exists() and gitc(path, "rev-parse", "--abbrev-ref", "HEAD") == "feat/x"
    (listed,) = listing(w, check=True)
    assert listed["head"] == gitc(w.repo, "rev-parse", "feat/x") and listed["dirty"] == 0 and listed["state"] == "ok"
    assert listed["generation"] == row["generation"]


def test_create_twice_is_worktree_exists_and_an_idem_replay_answers_the_same_object(w):
    from orch.host.control_client import new_idem

    idem = new_idem()
    first = ok(w.c.req("worktree.create", {"ticket": w.key}, idem=idem))["data"]["worktree"]
    assert ok(w.c.req("worktree.create", {"ticket": w.key}, idem=idem))["data"]["worktree"] == first
    assert err(create(w)) == "worktree.exists"


def test_no_linked_branch_is_worktree_no_branch(w):
    assert err(create(w, w.nob)) == "worktree.no_branch"
    assert listing(w) == []


def test_one_ticket_per_branch(w):
    ok(create(w))
    assert err(create(w, w.other)) == "worktree.branch_in_use"


def test_a_branch_checked_out_elsewhere_is_worktree_branch_in_use(w):
    gitc(w.repo, "checkout", "-q", "feat/x")  # the declared working copy has it
    assert err(create(w)) == "worktree.branch_in_use"
    assert listing(w) == []


def test_a_planted_post_checkout_hook_never_runs(w):
    hook = w.repo / ".git" / "hooks" / "post-checkout"
    marker = w.tmp / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    ok(create(w))
    assert not marker.exists()


# -- remove ---------------------------------------------------------------------------------------------------------


def test_remove_deletes_the_directory_and_registration_but_never_the_branch(w):
    ok(create(w))
    assert ok(remove(w))["data"] == {"removed": True}
    assert not wt_path(w).exists() and listing(w) == []
    assert gitc(w.repo, "rev-parse", "--verify", "refs/heads/feat/x")  # the branch survives
    assert NAME not in gitc(w.repo, "worktree", "list") if (NAME := wt_path(w).name) else False
    assert err(remove(w)) == "worktree.none"
    ok(create(w))  # and it can be created again, with a new generation


def test_dirty_remove_refuses_without_force_and_an_agent_cannot_force(w):
    gen1 = ok(create(w))["data"]["worktree"]["generation"]
    (wt_path(w) / "scratch.txt").write_text("uncommitted\n")
    assert err(remove(w)) == "worktree.dirty"
    assert err(remove(w, g=True)) == "worktree.dirty"
    assert err(remove(w, g=True, force=True)) == "op.not_allowed"
    assert wt_path(w).exists()
    assert ok(remove(w, force=True))["data"] == {"removed": True}
    assert not wt_path(w).exists()
    assert ok(create(w))["data"]["worktree"]["generation"] > gen1


def test_a_missing_directory_is_removed_by_pruning(w):
    import shutil

    ok(create(w))
    shutil.rmtree(wt_path(w))
    assert listing(w, check=True)[0]["state"] == "missing"
    assert ok(remove(w))["data"] == {"removed": True}
    assert "feat/x" not in gitc(w.repo, "worktree", "list")


# -- columns, gate ----------------------------------------------------------------------------------------------------


def test_g_works_only_on_the_ticket_its_session_claims(w):
    assert err(create(w, w.other, g=True)) == "op.not_allowed"  # not claimed by this session
    assert ok(create(w, g=True))["data"]["worktree"]["ticket"] == w.key
    assert ok(remove(w, g=True))["data"] == {"removed": True}


def test_the_claim_is_rechecked_under_the_lock_right_before_git_runs(w, monkeypatch):
    from orch.host import worktrees

    real, calls = worktrees._claim_ok, []

    def flaky(call, view):  # true while planning, false once the runner is about to run git
        calls.append(1)
        return real(call, view) if len(calls) == 1 else False

    monkeypatch.setattr(worktrees, "_claim_ok", flaky)
    assert err(create(w, g=True)) == "op.not_allowed"
    assert not wt_path(w).exists() and listing(w) == []


def test_runner_off_for_this_machine_is_runner_disabled_naming_the_machine(w):
    enablement.set_enabled(Path(w.served.lab.state), w.served.wid, False, UID, terminals=False, text="t")
    res = create(w)
    assert err(res) == "runner.disabled" and "machine" in res["error"]["message"]
    assert not wt_path(w).exists()


def test_a_person_cannot_name_a_repo_the_ticket_does_not_link(w):
    assert err(create(w, repo="elsewhere")) == "not_found"


# -- D58 and tree.check on the registered worktree --------------------------------------------------------------------


def decision(w):
    return ("verdict", {"ref": w.key, "outcome": "pass"})


def commit_in_worktree(w, text="fixed\n"):
    (wt_path(w) / "impl.txt").write_text(text)
    gitc(wt_path(w), "add", "impl.txt")
    gitc(wt_path(w), "commit", "-qm", "work in the worktree")
    return gitc(wt_path(w), "rev-parse", "HEAD")


def test_a_verdict_on_a_ticket_worked_in_its_worktree_passes(w):
    name, args = decision(w)
    # without a worktree the declared copy is read: it is on main, not on the ticket's branch
    assert err(w.human.prepare_res(name, args)) == "observe.unavailable"
    ok(create(w))
    prepared = w.human.prepare(name, args)
    (check,) = prepared["checks"]
    assert check["clean"] is True
    signed = w.human.sign(prepared)
    ok(w.human.submit_res(prepared["request"]["id"], signed.events))


def test_a_dirty_worktree_blocks_the_decision_while_the_declared_copy_is_clean(w):
    ok(create(w))
    (wt_path(w) / "scratch.txt").write_text("x\n")
    res = w.human.prepare_res(*decision(w))
    assert err(res) == "observe.unavailable" and "uncommitted" in res["error"]["message"]


def test_re_registration_between_prepare_and_binding_invalidates_the_request(w):
    ok(create(w))
    name, args = decision(w)
    prepared = w.human.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    ok(remove(w))
    ok(create(w))  # the same path, a new generation
    res = w.human.c.req("human.get", {"request_id": rid, "signer": w.human.signer})
    assert err(res) == "observe.unavailable" and "registration changed" in res["error"]["message"]


def test_a_removed_worktree_never_falls_back_to_the_declared_copy(w):
    ok(create(w))
    name, args = decision(w)
    prepared = w.human.prepare(name, args, handoff=True)
    ok(remove(w))
    res = w.human.c.req("human.get", {"request_id": prepared["request"]["id"], "signer": w.human.signer})
    assert err(res) == "observe.unavailable"


def test_a_repointed_git_file_is_observe_unavailable(w):
    ok(create(w))
    other = w.tmp / "other-gitdir"
    other.mkdir()
    (wt_path(w) / ".git").write_text(f"gitdir: {other}\n")
    assert err(w.human.prepare_res(*decision(w))) == "observe.unavailable"
    assert listing(w, check=True)[0]["state"] == "moved"


def test_a_moved_worktree_is_observe_unavailable(w):
    ok(create(w))
    wt_path(w).rename(w.tmp / "moved-away")
    assert err(w.human.prepare_res(*decision(w))) == "observe.unavailable"
    assert listing(w, check=True)[0]["state"] == "missing"


def test_a_relinked_branch_is_observe_unavailable_never_the_old_or_declared_copy(w):
    ok(create(w))
    gitc(w.repo, "branch", "feat/y")
    w.human.agent("show", {"ref": w.key, "full": True})  # the session reads the field before it sets it
    assert w.human.agent("set", {"ref": w.key, "pairs": ['links={"repos":["proj"],"branches":{"proj":"feat/y"}}']})
    assert err(w.human.prepare_res(*decision(w))) == "observe.unavailable"
    assert listing(w, check=True)[0]["state"] == "stale_branch"


def test_a_bound_request_rechecks_the_copy_binding_inside_the_tree_check_window(w):
    """Codex #439: the 5 s tree-check throttle never skips the copy binding: remove and recreate right after binding
    and the next signer-bearing get is observe.unavailable."""
    ok(create(w))
    name, args = decision(w)
    prepared = w.human.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    ok(w.human.c.req("human.get", {"request_id": rid, "signer": w.human.signer}))  # bound, trees just checked
    ok(remove(w))
    ok(create(w))  # well inside RECHECK_SECONDS
    res = w.human.c.req("human.get", {"request_id": rid, "signer": w.human.signer})
    assert err(res) == "observe.unavailable" and "registration changed" in res["error"]["message"]


@pytest.mark.parametrize("bound_first", [False, True])
def test_a_relink_after_prepare_is_observe_unavailable_at_binding(w, bound_first):
    """Codex #439: binding compares with the ticket's current links.branches, loaded fresh, not the prepared ref;
    the worktree itself is unchanged (generation and pin still match)."""
    ok(create(w))
    name, args = decision(w)
    prepared = w.human.prepare(name, args, handoff=True)
    rid = prepared["request"]["id"]
    if bound_first:
        ok(w.human.c.req("human.get", {"request_id": rid, "signer": w.human.signer}))
    gitc(w.repo, "branch", "feat/y")
    w.human.agent("show", {"ref": w.key, "full": True})
    assert w.human.agent("set", {"ref": w.key, "pairs": ['links={"repos":["proj"],"branches":{"proj":"feat/y"}}']})
    res = w.human.c.req("human.get", {"request_id": rid, "signer": w.human.signer})
    assert err(res) == "observe.unavailable" and "branch changed" in res["error"]["message"]


# -- worktree.diff (focus mode P2-5) --------------------------------------------------------------------------------


def wt_diff(w, **args):
    return w.c.req("worktree.diff", {"ticket": w.key, **args})


def test_diff_reads_the_uncommitted_changes_of_the_registered_worktree(w):
    assert ok(wt_diff(w))["data"]["available"] is False  # nothing registered yet
    ok(create(w))
    clean = ok(wt_diff(w))["data"]
    assert clean["available"] is True and clean["files"] == [] and clean["unified"] == ""
    (wt_path(w) / "impl.txt").write_text("changed by the agent\n")
    (wt_path(w) / "scratch.txt").write_text("new\nfile\n")
    got = ok(wt_diff(w))["data"]
    assert got["ticket"] == w.key and got["repo"] == "proj" and got["head"] == gitc(w.repo, "rev-parse", "feat/x")
    files = {f["path"]: f for f in got["files"]}
    assert files["impl.txt"]["status"] == "M" and files["scratch.txt"]["status"] == "?"
    assert files["scratch.txt"]["added"] == 2
    assert "+changed by the agent" in got["unified"] and "+++ b/scratch.txt" in got["unified"]
    assert str(w.repo) not in got["unified"]


def test_diff_is_person_only_and_follows_the_repo_rules(w):
    ok(create(w))
    assert err(w.c.req("worktree.diff", {"ticket": w.key}, grant=w.served.grant, session=SESSION)) == "op.not_allowed"
    assert err(wt_diff(w, repo="other")) == "not_found"
    no_repo = ok(w.c.req("worktree.diff", {"ticket": w.nob}))["data"]
    assert no_repo["available"] is False
