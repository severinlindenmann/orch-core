"""H11d: ``orch worktree`` in POSIX direct mode (D-P2-38): the host's worktree rules without a host."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from orch import worktree_cli
from orch.gitread.client import PinStore
from tests.ops.helpers import Cli

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


@pytest.fixture
def d(ws, cli, monkeypatch):
    home = ws.tmp / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_NO_SERVICE_MANAGER", "1")
    repo = ws.repo()
    ws.git("checkout", "-q", "-b", "main")  # feat/x is free for a worktree
    r = cli("new", "A ticket", "-m", "text")
    assert r.code == 0, r.err
    key = r.first.split()[1]
    assert cli("claim", key).code == 0
    assert cli("set", key, LINKS).code == 0
    assert cli("release").code == 0  # a session claims one ticket at a time
    r = cli("new", "Second", "-m", "text")
    other = r.first.split()[1]
    assert cli("claim", other).code == 0
    assert cli("set", other, LINKS).code == 0, "the second ticket links the same repo and branch"
    assert cli("release").code == 0
    r = cli("new", "No branch", "-m", "text")
    nob = r.first.split()[1]
    assert cli("claim", nob).code == 0
    assert cli("set", nob, 'links={"repos":["proj"]}').code == 0
    person = Cli(ws, grant=False, session=None)

    def run(*argv: str, **env):
        return person(*["worktree", *argv, "--json"], **env)

    return type("D", (), {"ws": ws, "repo": repo, "key": key, "other": other, "nob": nob, "run": staticmethod(run),
                          "person": person})  # fmt: skip


def wt(d):
    wid8 = json.loads((d.ws.root / ".orch" / "config.json").read_text())["workspace"]["id"][:8] if (
        d.ws.root / ".orch" / "config.json").exists() else None
    return wid8


def doc(r):
    return json.loads(r.out)


def test_create_list_remove_round_trip(d):
    r = d.run("create", d.key)
    assert r.code == 0, r.out
    row = doc(r)["data"]
    assert row["branch"] == "feat/x" and row["ticket"] == d.key and row["generation"] == 1
    assert row["path"] == f"{d.repo}.worktrees/{row['path'].rsplit('/', 1)[1]}" and row["path"].endswith(f"-{d.key}")
    assert os.path.exists(os.path.join(row["path"], "impl.txt"))
    listed = doc(d.run("list", "--check"))["data"]
    assert [x["path"] for x in listed] == [row["path"]] and listed[0]["dirty"] == 0 and listed[0]["state"] == "ok"
    pins = PinStore(d.ws.host_state / "hosts" / _wid(d) / "git-pins.json")  # the file a host reads later
    assert pins.worktree(d.ws.uid(d.key), "proj")["reg"]["path"] == row["path"]
    assert doc(d.run("create", d.key))["code"] == "worktree.exists"
    r = d.run("remove", d.key)
    assert r.code == 0 and doc(r)["data"] == {"removed": True}
    assert not os.path.exists(row["path"]) and doc(d.run("list"))["data"] == []
    d.ws.git("rev-parse", "--verify", "refs/heads/feat/x")  # the branch survives
    assert doc(d.run("remove", d.key))["code"] == "worktree.none"
    again = doc(d.run("create", d.key))["data"]
    assert again["generation"] == 3


def _wid(d) -> str:
    cfg = json.loads(next(d.ws.root.rglob("config.json")).read_text())
    return cfg["workspace"]["id"]


def test_dirty_remove_refuses_without_force(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "scratch.txt"), "w").write("x\n")
    r = d.run("remove", d.key)
    assert r.code == 3 and doc(r)["code"] == "worktree.dirty" and os.path.exists(path)
    assert d.run("remove", d.key, "--force").code == 0 and not os.path.exists(path)


def test_a_ticket_without_a_linked_branch_is_worktree_no_branch(d):
    r = d.run("create", d.nob)
    assert r.code == 3 and doc(r)["code"] == "worktree.no_branch"


def test_the_branch_of_another_ticket_is_worktree_branch_in_use(d):
    assert d.run("create", d.key).code == 0
    r = d.run("create", d.other)
    assert r.code == 3 and doc(r)["code"] == "worktree.branch_in_use"


def test_it_refuses_while_a_host_serves_the_workspace(d):
    marker = d.ws.root / ".state"
    marker.mkdir(exist_ok=True)
    (marker / "host-served").write_text("")
    for argv in (("list",), ("create", d.key), ("remove", d.key)):
        r = d.run(*argv)
        assert r.code == 3 and doc(r)["code"] == "host.required", argv


def test_an_agent_grant_is_refused(d):
    r = Cli(d.ws)("worktree", "list", "--json")  # the agent's env carries ORCH_GRANT
    assert r.code == 3 and doc(r)["code"] == "op.not_allowed"


def test_windows_answers_unsupported_platform(d, monkeypatch):
    import io

    monkeypatch.setattr(worktree_cli.os, "name", "nt")
    out = io.StringIO()
    code = worktree_cli.main(["list", "--json"], env={}, out=out)
    assert code == 3 and json.loads(out.getvalue())["code"] == "unsupported.platform"


def test_usage_errors_exit_2(d):
    assert d.run("bogus").code == 2
    assert d.run("create").code == 2
    assert subprocess.run  # keep the import used


def test_direct_d58_reads_the_registered_worktree_not_the_declared_copy(d):
    from orch.ops.caps import Git

    path = doc(d.run("create", d.key))["data"]["path"]
    pins = PinStore(d.ws.host_state / "hosts" / _wid(d) / "git-pins.json")
    git = Git(enabled=True, worktrees=pins)
    copy = git.ticket_copy(d.ws.root, {"proj": {"path": str(d.repo)}}, d.ws.uid(d.key), "proj")
    assert copy.registered and str(copy.path) == path and copy.gen == 1 and copy.pin["path"] == path


def test_a_repos_own_config_and_hooks_run_nothing_in_any_command(d):
    marker = d.ws.tmp / "ran"
    script = d.ws.tmp / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    hooks = d.ws.tmp / "hooks"
    hooks.mkdir()
    for h in ("post-checkout", "pre-commit"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / h).chmod(0o755)
    d.ws.git("config", "core.fsmonitor", str(script))
    d.ws.git("config", "core.hooksPath", str(hooks))
    d.ws.git("config", "core.sshCommand", str(script))
    d.ws.git("config", "credential.helper", f"!{script}")
    # control: plain git does run the repo's fsmonitor, so the assertion below can fail
    subprocess.run(["git", "-C", str(d.repo), "status"], capture_output=True, check=False)
    assert marker.exists(), "the control must run the planted script"
    marker.unlink()
    path = doc(d.run("create", d.key))["data"]["path"]
    assert d.run("list", "--check").code == 0
    open(os.path.join(path, "scratch.txt"), "w").write("x\n")
    assert doc(d.run("remove", d.key))["code"] == "worktree.dirty"
    assert d.run("remove", d.key, "--force").code == 0
    assert not marker.exists()


# -- D58 fails closed on the registration (direct mode here; host mode through bound_copy) -----------------------------


@pytest.fixture
def problems(d):
    import time
    from types import SimpleNamespace

    from orch.ops.caps import Git
    from orch.ops.human import Human

    uid = d.ws.uid(d.key)
    wid = _wid(d)
    store = SimpleNamespace(root=d.ws.root, workspace_id=wid,
                            state=SimpleNamespace(workspace=SimpleNamespace(repos={"proj": {"path": str(d.repo)}})))  # fmt: skip
    h = Human.__new__(Human)
    h.ctx = SimpleNamespace(caps=SimpleNamespace(git=Git(enabled=True), clock=time.time), prepare=None)
    h.call = SimpleNamespace(ws=SimpleNamespace(state_dir=d.ws.host_state), store=store)
    view = SimpleNamespace(uid=uid, fields={"links": {"repos": ["proj"], "branches": {"proj": "feat/x"}}})
    pinfile = d.ws.host_state / "hosts" / wid / "git-pins.json"
    return SimpleNamespace(run=lambda: h._tree_problems(view, []), pinfile=pinfile, d=d, uid=uid)


def test_d58_direct_accepts_an_intact_registration(problems):
    path = doc(problems.d.run("create", problems.d.key))["data"]["path"]
    got = problems.run()
    assert not any("registered" in p or "pinned" in p or "missing" in p or "cannot be read" in p for p in got), got
    assert any("not recorded" in p for p in got)  # judged on the worktree's own HEAD (nothing is signed here)
    assert path


def test_d58_direct_without_a_pin_file_uses_the_declared_copy_only_when_nothing_was_ever_registered(problems):
    assert not problems.pinfile.exists()
    got = problems.run()  # the declared copy is on main, not the ticket's branch: refused on its own merits
    assert got and not any("registered" in p or "pinned" in p for p in got), got  # refused on its own merits


def test_d58_direct_refuses_a_malformed_pin_file(problems):
    problems.d.run("create", problems.d.key)
    problems.pinfile.write_text("{not json")
    assert any("registrations cannot be read" in p for p in problems.run())
    problems.pinfile.write_text(json.dumps({"v": 1, "repos": {}, "worktrees": {"u": {"r": {"gen": "x", "reg": None}}}}))
    assert any("registrations cannot be read" in p for p in problems.run())


def test_d58_direct_refuses_a_registration_without_pins(problems):
    problems.d.run("create", problems.d.key)
    doc_ = json.loads(problems.pinfile.read_text())
    doc_["worktrees"][problems.uid]["proj"]["reg"]["pins"] = None
    problems.pinfile.write_text(json.dumps(doc_))
    got = problems.run()
    assert any("not pinned" in p for p in got), got
    assert not any("working tree is on refs" in p for p in got)  # and never the declared copy


def test_d58_direct_refuses_a_repointed_git_file(problems):
    path = doc(problems.d.run("create", problems.d.key))["data"]["path"]
    other = problems.d.ws.tmp / "elsewhere-gitdir"
    other.mkdir()
    (open(os.path.join(path, ".git"), "w")).write(f"gitdir: {other}\n")
    got = problems.run()
    assert any(p.startswith("proj:") and ("changed since" in p or "gitdir" in p or ".git" in p) for p in got), got


def test_d58_direct_refuses_a_swapped_or_missing_worktree_directory(problems):
    import shutil

    path = doc(problems.d.run("create", problems.d.key))["data"]["path"]
    shutil.rmtree(path)
    assert any("worktree is missing" in p for p in problems.run())


def test_bound_copy_fails_closed_on_every_branch(tmp_path):
    from types import SimpleNamespace

    from orch.host.errors import HostError
    from orch.host.human import bound_copy
    from orch.ops.caps import WorkingCopy

    def git(copy, *, enabled=True, reader=object()):
        return SimpleNamespace(enabled=enabled, reader=reader, principal="p", ticket_copy=lambda *a: copy)

    store = SimpleNamespace(root=tmp_path, state=SimpleNamespace(workspace=SimpleNamespace(repos={})))
    declared = WorkingCopy(tmp_path / "r", 0, None, False, None)
    want = [declared.binding("u", "proj")]
    assert bound_copy(git(declared), store, "u", "proj", want) == tmp_path / "r"  # the one open case
    for g, copies, why in (
        (git(declared, enabled=False), want, "git disabled"),
        (git(declared, reader=None), want, "no reader"),
        (git(declared), None, "no binding recorded"),
        (git(declared), [], "empty bindings"),
        (git(declared), [{**want[0], "gen": 5}], "generation mismatch"),
        (git(declared), [{**want[0], "registered": True}], "registration mismatch"),
        (git(declared), [{**want[0], "pin": {"path": "/x", "dev": 1, "ino": 2}}], "pin mismatch"),
        (git(WorkingCopy(None, 1, None, True, "proj: the ticket's worktree is not pinned")), want, "unpinned"),
        (git(WorkingCopy(tmp_path, 1, {"path": "/x", "dev": 1, "ino": 2}, True, None)), want, "removed registration"),
    ):
        with pytest.raises(HostError) as e:
            bound_copy(g, store, "u", "proj", copies)
        assert e.value.code == "observe.unavailable", why
