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
