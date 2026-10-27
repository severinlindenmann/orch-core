"""H11d: ``orch worktree`` in POSIX direct mode (D-P2-38): the host's worktree rules without a host."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orch import worktree_cli
from orch.gitread.client import PinStore
from orch.layout import Layout
from tests.ops.helpers import Cli, Ws

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


def _build(ws, cli, setenv):
    home = ws.tmp / "home"
    home.mkdir()
    setenv("HOME", str(home))
    setenv("ORCH_NO_SERVICE_MANAGER", "1")
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
                          "person": person, "agent": cli})  # fmt: skip


@pytest.fixture(scope="module")
def _scene(tmp_path_factory):
    """The workspace, the repository and the three tickets, built once per module (a copy of the files is kept)."""
    live = tmp_path_factory.mktemp("wtd")
    pristine = tmp_path_factory.mktemp("wtd-pristine") / "files"
    w = Ws(live)
    w.bootstrap()
    with pytest.MonkeyPatch.context() as mp:
        built = _build(w, Cli(w), mp.setenv)
    w.store.close()
    w.store.fs.close()  # the root fd too: the files are replaced under it for every test
    w.store = None
    shutil.copytree(live, pristine, symlinks=True)
    yield built, live, pristine, w.clock[0]


@pytest.fixture
def d(_scene, monkeypatch):
    """The module's scene with its files put back (a test creates worktrees, claims, edits): the paths stay the same,
    because the repository's path is in the signed settings."""
    built, live, pristine, clock = _scene
    shutil.rmtree(live)
    shutil.copytree(pristine, live, symlinks=True)
    built.ws.clock[0] = clock
    monkeypatch.setenv("HOME", str(live / "home"))
    monkeypatch.setenv("ORCH_NO_SERVICE_MANAGER", "1")
    return built


def wt(d):
    wid8 = (
        json.loads((d.ws.root / ".orch" / "config.json").read_text())["workspace"]["id"][:8]
        if (d.ws.root / ".orch" / "config.json").exists()
        else None
    )
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
    marker = d.ws.store_dir / ".state"
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
    copy = git.ticket_copy(Layout.new_at(d.ws.root), {"proj": {"path": str(d.repo)}}, d.ws.uid(d.key), "proj")
    assert copy.registered and str(copy.path) == path and copy.gen == 1 and copy.pin["path"] == path


def test_a_repos_own_config_and_hooks_run_nothing_in_any_command(d):
    """The orch.gitsafe invariant: a repo whose own config names a program is refused before git starts; a local
    hooksPath (husky) is allowed because -c core.hooksPath=/dev/null overrides it on every command."""
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
    # control: plain git does run the repo's fsmonitor, so the assertion below can fail
    subprocess.run(["git", "-C", str(d.repo), "status"], capture_output=True, check=False)
    assert marker.exists(), "the control must run the planted script"
    marker.unlink()
    d.ws.git("config", "--unset-all", "core.fsmonitor")
    for key, value in (("core.fsmonitor", str(script)), ("core.sshCommand", str(script)),
                       ("credential.helper", f"!{script}"), ("filter.x.smudge", str(script))):  # fmt: skip
        d.ws.git("config", key, value)
        got = doc(d.run("create", d.key))
        assert got["code"] == "observe.unavailable" and key.split(".")[0] in got["message"].lower(), got
        assert value not in got["message"]
        d.ws.git("config", "--unset-all", key)
    assert not marker.exists()
    d.ws.git("config", "core.hooksPath", str(hooks))
    path = doc(d.run("create", d.key))["data"]["path"]
    assert d.run("list", "--check").code == 0
    open(os.path.join(path, "scratch.txt"), "w").write("x\n")
    assert doc(d.run("remove", d.key))["code"] == "worktree.dirty"
    # planted after create (#565): list --check and remove's clean check are orch's reader (no git process), so the
    # filter never runs and the answer is the real one; git's remove itself is still gated, and refused
    open(os.path.join(path, ".gitattributes"), "w").write("* filter=x\n")
    d.ws.git("config", "filter.x.clean", str(script))
    assert doc(d.run("remove", d.key))["code"] == "worktree.dirty"
    listed = doc(d.run("list", "--check"))["data"][0]
    assert listed["dirty"] == 2 and listed["head"], listed
    os.remove(os.path.join(path, "scratch.txt"))
    os.remove(os.path.join(path, ".gitattributes"))
    assert doc(d.run("remove", d.key))["code"] == "observe.unavailable"  # clean: git's remove is next, the gate refuses
    assert os.path.exists(path)
    d.ws.git("config", "--unset-all", "filter.x.clean")
    assert d.run("remove", d.key, "--force").code == 0
    assert not marker.exists()


def test_a_held_workspace_lock_is_lock_busy(d, monkeypatch):
    import fcntl

    monkeypatch.setattr(worktree_cli, "LOCK_WAIT", 0.1)
    lock = d.ws.host_state / "hosts" / _wid(d) / "worktrees.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        got = doc(d.run("create", d.key))
        assert got["code"] == "lock.busy"
        assert doc(d.run("remove", d.key))["code"] == "lock.busy"
    finally:
        os.close(fd)
    assert d.run("create", d.key).code == 0


def test_concurrent_direct_commands_never_lose_a_registration(d, monkeypatch):
    """Codex #439: two direct creates (another process each, in practice) are serialized by the workspace lock and
    the pin file is loaded under it: both registrations survive, and a remove racing a create keeps the other one."""
    import threading
    import time as _time

    from orch.runner import wtgit

    assert d.agent("set", d.nob, 'links={"repos":["proj"],"branches":{"proj":"feat/y"}}').code == 0  # nob is claimed
    real = wtgit.run_git

    def slow(args, own, **kw):
        _time.sleep(0.3)  # both commands are inside their git step at once unless the lock serializes them
        return real(args, own, **kw)

    monkeypatch.setattr(wtgit, "run_git", slow)
    codes: dict[str, int] = {}

    def go(ref):
        codes[ref] = d.run("create", ref).code

    ts = [threading.Thread(target=go, args=(k,)) for k in (d.key, d.nob)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == {d.key: 0, d.nob: 0}, codes
    pins = PinStore(d.ws.host_state / "hosts" / _wid(d) / "git-pins.json")
    assert {uid for uid, _r, _s in pins.worktrees()} == {d.ws.uid(d.key), d.ws.uid(d.nob)}
    t = threading.Thread(target=lambda: codes.__setitem__("rm", d.run("remove", d.key).code))
    t.start()
    codes["again"] = d.run("create", d.nob).code  # worktree.exists, read under the lock
    t.join()
    assert codes["rm"] == 0 and codes["again"] != 0
    pins = PinStore(d.ws.host_state / "hosts" / _wid(d) / "git-pins.json")
    assert [uid for uid, _r, _s in pins.worktrees()] == [d.ws.uid(d.nob)]


def test_direct_create_refuses_a_path_that_is_a_declared_repository(d, monkeypatch):
    """Opus #439 F4: parity with the host's "the worktree path is a declared repository"."""
    from orch.host import worktrees

    monkeypatch.setattr(worktrees, "worktree_path", lambda declared, wid, key: declared)
    got = doc(d.run("create", d.key))
    assert got["code"] == "worktree.exists" and "declared repository" in got["message"]


def test_list_check_reports_moved_and_stale_branch_as_the_host_does(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    assert doc(d.run("list", "--check"))["data"][0]["state"] == "ok"
    subprocess.run(["git", "-C", path, "checkout", "-q", "-b", "elsewhere"], check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(d.ws.tmp), "GIT_CONFIG_NOSYSTEM": "1"})  # fmt: skip
    assert doc(d.run("list", "--check"))["data"][0]["state"] == "stale_branch"
    subprocess.run(["git", "-C", path, "checkout", "-q", "feat/x"], check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(d.ws.tmp), "GIT_CONFIG_NOSYSTEM": "1"})  # fmt: skip
    assert doc(d.run("list", "--check"))["data"][0]["state"] == "ok"
    gitfile = os.path.join(path, ".git")
    saved = open(gitfile).read()
    other = d.ws.tmp / "elsewhere-gitdir"
    other.mkdir()
    open(gitfile, "w").write(f"gitdir: {other}\n")
    assert doc(d.run("list", "--check"))["data"][0]["state"] == "moved"
    open(gitfile, "w").write(saved)
    assert d.agent("release").code == 0 and d.agent("claim", d.key).code == 0
    assert d.agent("set", d.key, 'links={"repos":["proj"],"branches":{"proj":"feat/z"}}').code == 0
    assert doc(d.run("list", "--check"))["data"][0]["state"] == "stale_branch"


# -- D58 fails closed on the registration (direct mode here; host mode through bound_copy) -----------------------------


@pytest.fixture
def problems(d):
    import time
    from types import SimpleNamespace

    from orch.ops.caps import Git
    from orch.ops.human import Human

    uid = d.ws.uid(d.key)
    wid = _wid(d)
    store = SimpleNamespace(root=d.ws.root, layout=Layout.new_at(d.ws.root), workspace_id=wid,
                            state=SimpleNamespace(
                                workspace=SimpleNamespace(repos={"proj": {"path": str(d.repo)}})))  # fmt: skip
    h = Human.__new__(Human)
    h.ctx = SimpleNamespace(
        caps=SimpleNamespace(git=Git(enabled=True), clock=time.time, env=dict(os.environ)), prepare=None
    )
    h.call = SimpleNamespace(ws=SimpleNamespace(state_dir=d.ws.host_state), store=store)
    view = SimpleNamespace(uid=uid, key=d.key, fields={"links": {"repos": ["proj"], "branches": {"proj": "feat/x"}}})
    pinfile = d.ws.host_state / "hosts" / wid / "git-pins.json"
    return SimpleNamespace(run=lambda: h._tree_problems(view, []), pinfile=pinfile, d=d, uid=uid, h=h)


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

    some_reader = object()

    def git(copy, *, enabled=True, reader=some_reader):
        return SimpleNamespace(enabled=enabled, reader=reader, principal="p", ticket_copy=lambda *a: copy)

    store = SimpleNamespace(
        root=tmp_path, layout=Layout.new_at(tmp_path), state=SimpleNamespace(workspace=SimpleNamespace(repos={}))
    )
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


def test_d58_direct_refuses_a_worktree_on_disk_without_a_pin_file(problems):
    """Opus #439 F3: a deleted git-pins.json never turns into a fallback to the declared copy."""
    problems.d.run("create", problems.d.key)
    problems.pinfile.unlink()
    got = problems.run()
    assert any("on disk but no registration" in p for p in got), got


@pytest.mark.parametrize("interleaving", ["remove", "remove-recreate"])
def test_d58_direct_precommit_refuses_a_registration_changed_since_the_first_look(problems, interleaving):
    """Codex #439: the copy the person was shown (generation, registration, pin) is the copy the precommit reads."""
    problems.d.run("create", problems.d.key)
    first = problems.run()
    assert not any("changed while you were deciding" in p for p in first), first
    assert problems.d.run("remove", problems.d.key).code == 0
    if interleaving == "remove-recreate":
        assert problems.d.run("create", problems.d.key).code == 0
    got = problems.run()  # the precommit's look, same Human
    assert any("changed while you were deciding" in p for p in got), got
    assert not any("working tree is on refs/heads/main" in p for p in got)  # never judged on the declared copy


def test_d58_direct_runs_nothing_a_registered_worktrees_config_names(problems):
    """#565: direct D58 reads the registered worktree with orch's reader (no git process): a clean filter its config
    names never runs, and the tree's answer is the real one. Positive control: plain git status runs it."""
    path = doc(problems.d.run("create", problems.d.key))["data"]["path"]
    marker = problems.d.ws.tmp / "d58-filter-ran"
    script = problems.d.ws.tmp / "d58-filter.sh"
    script.write_text(f"#!/bin/sh\necho x >> {marker}\ncat\n")
    script.chmod(0o755)
    problems.d.ws.git("config", "filter.x.clean", str(script))
    with open(os.path.join(path, ".git"), "rb") as f:
        assert f.read().startswith(b"gitdir: ")
    open(os.path.join(path, ".gitattributes"), "w").write("* filter=x\n")
    got = problems.run()
    assert any("uncommitted changes" in p for p in got), got  # .gitattributes is untracked: dirty, read without git
    assert not any("cannot be read" in p for p in got), got
    assert not marker.exists()
    subprocess.run(["git", "-C", path, "add", "-A"], capture_output=True, check=False)  # control: git runs it
    assert marker.exists()


def test_forced_remove_keeps_observe_unavailable_from_a_gate_refusal(d):
    """Codex #439 low: ``remove --force`` skips the cleanliness read, so the refusal comes from the runner's gate; its
    code must stay ``observe.unavailable`` (not ``worktree.git_failed``)."""
    path = doc(d.run("create", d.key))["data"]["path"]
    d.ws.git("config", "filter.x.clean", "/bin/false")
    r = d.run("remove", d.key, "--force")
    assert r.code == 3 and doc(r)["code"] == "observe.unavailable", r.out
    assert os.path.exists(path)
    d.ws.git("config", "--unset-all", "filter.x.clean")
    assert d.run("remove", d.key, "--force").code == 0


def _submodule_in(path: str, tmp, marker) -> None:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip

    def g(cwd, *a):
        subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)

    sub = os.path.join(path, "sub")
    os.mkdir(sub)
    g(sub, "init", "-q", "-b", "main")
    open(os.path.join(sub, "a"), "w").write("a\n")
    open(os.path.join(sub, ".gitattributes"), "w").write("* filter=m\n")
    g(sub, "add", ".")
    g(sub, "commit", "-qm", "s")
    g(path, "add", "sub")
    g(path, "commit", "-qm", "gitlink")
    g(sub, "config", "filter.m.clean", f"touch {marker}; cat")
    open(os.path.join(sub, "a"), "w").write("a\nchanged\n")  # a dirty submodule


def test_a_populated_submodule_is_never_inspected_or_reported_clean(d):
    """Codex #439 High/Medium: direct ``list --check`` and ``remove`` never run a submodule's own filter and never call
    a tree with a checked-out submodule clean (``dirty`` null: unknown; remove: ``observe.unavailable``, orch's reader
    refuses it, #565)."""
    path = doc(d.run("create", d.key))["data"]["path"]
    marker = d.ws.tmp / "submodule-filter-ran"
    _submodule_in(path, d.ws.tmp, marker)
    listed = doc(d.run("list", "--check"))["data"][0]
    assert listed["dirty"] is None, listed
    r = d.run("remove", d.key)
    assert r.code == 3 and doc(r)["code"] == "observe.unavailable" and os.path.exists(path), r.out
    assert not marker.exists()


def test_d58_direct_refuses_a_registered_worktree_with_a_populated_submodule(problems):
    path = doc(problems.d.run("create", problems.d.key))["data"]["path"]
    marker = problems.d.ws.tmp / "submodule-filter-ran"
    _submodule_in(path, problems.d.ws.tmp, marker)
    got = problems.run()
    assert any("submodule" in p for p in got), got
    assert not marker.exists()


@pytest.mark.parametrize(
    "gitconfig,xdg",
    [("[include]\n\tpath = ~/.gc2\n", None), ('[includeIf "gitdir:/"]\n\tpath = ~/.gc2\n', None),
     (None, "[core]\n\texcludesFile =\n")],
    ids=["include", "includeIf", "empty"],
)  # fmt: skip
def test_remove_refuses_a_file_git_shows_whatever_the_persons_ignore_config(d, gitconfig, xdg):
    """Opus M2 on #583: git's own remove runs --force since #565, so orch's clean check is the only guard. When the
    person's config sets excludesFile through an include, or sets it empty, git does not use its default ignore file;
    the reader must not either, or z.def (untracked to git) would be deleted with the worktree."""
    home = Path(os.environ["HOME"])
    (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "git" / "ignore").write_text("*.def\n")
    (home / ".gc2").write_text(f"[core]\n\texcludesFile = {home / '.gi_other'}\n")
    (home / ".gi_other").write_text("*.oth\n")
    for p, text in ((home / ".gitconfig", gitconfig), (home / ".config" / "git" / "config", xdg)):
        if text is not None:
            p.write_text(text)
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "z.def"), "w").write("keep me\n")
    got = doc(d.run("remove", d.key))
    assert got["code"] == "worktree.dirty", got
    assert os.path.exists(os.path.join(path, "z.def"))
    assert d.run("remove", d.key, "--force").code == 0  # the person's explicit force still removes it


def test_d58_direct_reads_head_and_tree_in_one_read_of_the_registered_pins(problems, monkeypatch):
    """Opus L4 on #583: direct D58 takes HEAD, its branch and the tree from ONE reader call on the registration's
    pins (as host mode's tree.check), never from separate reads that find the repository by path again."""
    from orch.gitread import local
    from orch.ops.caps import Git

    problems.d.run("create", problems.d.key)

    def no(*a, **k):
        raise AssertionError("a separate read")

    monkeypatch.setattr(Git, "head", no)
    monkeypatch.setattr(Git, "symbolic_head", no)
    monkeypatch.setattr(Git, "dirty", no)
    seen = []
    real = local.LocalGitReader.tree_check

    def tree_check(self, path, env=None, *, pins=None):
        seen.append(pins)
        return real(self, path, env, pins=pins)

    monkeypatch.setattr(local.LocalGitReader, "tree_check", tree_check)
    got = problems.run()
    assert any("not recorded" in p for p in got), got  # judged (nothing is signed here), no "cannot be read"
    assert not any("cannot be read" in p for p in got), got
    assert len(seen) == 1 and seen[0] is not None  # one read, of the registered pins


# -- #583 round 3 (orch-07): removal never depends on ignore rules


def test_the_keep_log_probe_blocks_remove_whatever_the_ignore_rules_say(d):
    """Opus delta M1 end to end: info/exclude ``*.log`` / ``!Keep.log``; git lists keep.log as untracked, a
    case-sensitive match hid it, and remove deleted it. A removal applies no ignore rule: it blocks, names the file."""
    path = doc(d.run("create", d.key))["data"]["path"]
    info = d.repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("*.log\n!Keep.log\n")
    open(os.path.join(path, "keep.log"), "w").write("the person's notes\n")
    got = doc(d.run("remove", d.key))
    assert got["code"] == "worktree.dirty" and "keep.log" in got["message"], got
    assert "--discard-untracked" in got["message"]
    assert os.path.exists(os.path.join(path, "keep.log"))


def test_an_ignored_directory_blocks_with_its_size(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, ".gitignore"), "w").write("node_modules/\n")
    os.makedirs(os.path.join(path, "node_modules", "pkg"))
    for i in range(3):
        open(os.path.join(path, "node_modules", "pkg", f"f{i}.js"), "w").write("x" * 1000)
    got = doc(d.run("remove", d.key))
    assert got["code"] == "worktree.dirty" and "node_modules/ (3 files, 2.9 KB)" in got["message"], got


def _token(d):
    """The token the refusal prints (#723): a person reads the list, then passes its token."""
    got = doc(d.run("remove", d.key))
    assert got["code"] == "worktree.dirty", got
    m = re.search(r"--discard-untracked=([0-9a-f]{12})", got["message"])
    assert m, got
    return m.group(1)


def test_discard_untracked_deletes_exactly_the_listed_set_and_prints_it_first(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    for name in ("a.tmp", "build/out.o"):
        os.makedirs(os.path.dirname(os.path.join(path, name)), exist_ok=True)
        open(os.path.join(path, name), "w").write("x\n")
    r = d.run("remove", d.key, f"--discard-untracked={_token(d)}")
    assert r.code == 0, r.out
    printed = r.err
    assert printed.startswith("discarding exactly these untracked files:"), printed
    assert "a.tmp" in printed and "build/ (1 files" in printed
    assert doc(r)["data"]["discarded"] == ["untracked: a.tmp", "untracked: build/ (1 files, 2 B)"]
    assert not os.path.exists(path)


def test_discard_untracked_without_a_token_deletes_nothing(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "a.tmp"), "w").write("x\n")
    got = doc(d.run("remove", d.key, "--discard-untracked"))
    assert got["code"] == "worktree.dirty" and "--discard-untracked=" in got["message"], got
    assert os.path.exists(os.path.join(path, "a.tmp"))


def test_discard_untracked_with_a_stale_token_refuses_and_prints_the_new_list_and_token(d):
    """#723: a file added after the person read the refusal is not deleted unseen."""
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "a.tmp"), "w").write("x\n")
    old = _token(d)
    open(os.path.join(path, "late.txt"), "w").write("new\n")
    r = d.run("remove", d.key, f"--discard-untracked={old}")
    got = doc(r)
    new = re.search(r"--discard-untracked=([0-9a-f]{12})", got["message"]).group(1)
    assert r.code != 0 and got["code"] == "worktree.dirty" and "late.txt" in got["message"] and new != old, got
    assert os.path.exists(os.path.join(path, "a.tmp")) and os.path.exists(os.path.join(path, "late.txt"))
    assert doc(d.run("remove", d.key, "--discard-untracked=nonsense"))["code"] == "worktree.dirty"
    assert d.run("remove", d.key, f"--discard-untracked={new}").code == 0  # the new token deletes the new list
    assert not os.path.exists(path)


def test_discard_untracked_refuses_when_a_file_appears_between_the_listing_and_the_delete(d, monkeypatch):
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "a.tmp"), "w").write("x\n")
    token = _token(d)
    monkeypatch.setattr(worktree_cli.Direct, "_between",
                        lambda self: open(os.path.join(path, "late.txt"), "w").write("new\n"))  # fmt: skip
    r = d.run("remove", d.key, f"--discard-untracked={token}")
    got = doc(r)
    assert r.code != 0 and got["code"] == "worktree.dirty" and "changed since they were listed" in got["message"]
    assert os.path.exists(os.path.join(path, "a.tmp")) and os.path.exists(os.path.join(path, "late.txt"))


def test_discard_untracked_never_discards_a_modified_tracked_file(d):
    path = doc(d.run("create", d.key))["data"]["path"]
    open(os.path.join(path, "impl.txt"), "a").write("edited\n")
    open(os.path.join(path, "a.tmp"), "w").write("x\n")
    got = doc(d.run("remove", d.key, "--discard-untracked"))
    assert got["code"] == "worktree.dirty" and "impl.txt" in got["message"]
    assert os.path.exists(os.path.join(path, "a.tmp"))
