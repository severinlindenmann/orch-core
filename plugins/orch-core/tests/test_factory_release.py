"""Dark AI Factory phase 6: the release recipe, the charter's `release` field and the runner's release step.

The runner's own git runs for real, against temporary repositories (a workspace checkout and a bare "remote" on disk;
no network). The recipe's programs are real files in a temporary folder (resolved and pinned by the real checks), but
a fake runner stands in for running them: no test runs a real gh, merge, push or deploy."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from orch import actor as orch_actor
from orch.cli import run as cli_run
from orch.core import epics, factory_release as fr, factory_report, factory_sessions as fs, ledger, permits, store
from orch.core.events import Actor, read_events
from orch.errors import HumanOnlyError, UsageError, ValidationError

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def _g(cwd, *args) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.hooksPath=",
                        *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _not_stopping():
    fr._STOPPING.clear()
    yield
    fr._STOPPING.clear()


@pytest.fixture(autouse=True)
def bin_dir(tmp_path_factory, monkeypatch):
    """The recipe's programs: real files (stand-ins, never run by these tests) on a real PATH entry."""
    d = tmp_path_factory.mktemp("bin")
    for name in ("gh", "make"):
        p = d / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


@pytest.fixture(autouse=True)
def remote(ws_root, tmp_path):
    """The workspace is a git checkout with one commit on main, and the recipe's remote a bare clone of it."""
    _g(ws_root, "init", "-q", "-b", "main")
    _g(ws_root, "commit", "-q", "--allow-empty", "-m", "base")
    r = tmp_path / "remote.git"
    _g(tmp_path, "clone", "-q", "--bare", str(ws_root), str(r))
    return r


def _main_commit(ws_root, remote, files: dict, push: bool = True) -> None:
    for f, body in files.items():
        (ws_root / f).parent.mkdir(parents=True, exist_ok=True)
        (ws_root / f).write_text(body, encoding="utf-8")
        _g(ws_root, "add", f)
    _g(ws_root, "commit", "-q", "-m", "main work")
    if push:
        _g(ws_root, "push", "-q", str(remote), "main")


def _msg(name: str) -> list[str]:
    """A message orch's commit-msg check takes (the release checks every child commit's message)."""
    import re
    key = re.search(r"[a-z]+-\d+", name)
    return ["-m", f"{key.group(0).upper() if key else 'L-0001'} work on {name}", "-m", "What: work", "-m",
            "Why: the ticket", "-m", "Risk: low"]


def _wt(name: str) -> str:
    """The linked worktree (relative to the workspace root) a test branch is built in."""
    return ".claude/worktrees/" + name.replace("/", "-")


def _branch(ws_root, name: str, files: dict, start: str = "main") -> str:
    """Commit `files` on branch `name` (reset to `start`) in its own linked worktree of the workspace: a child with a
    worktree of its own is one the release fetches from the workspace checkout (one without gets a clone)."""
    wt = ws_root / _wt(name)
    if wt.is_dir():
        _g(wt, "checkout", "-q", "-B", name, start)
    else:
        _g(ws_root, "worktree", "add", "-q", "-B", name, str(wt), start)
    for f, body in files.items():
        (wt / f).parent.mkdir(parents=True, exist_ok=True)
        (wt / f).write_text(body, encoding="utf-8")
        _g(wt, "add", f)
    _g(wt, "commit", "-q", "--allow-empty", *_msg(name))
    return _g(wt, "rev-parse", "HEAD")


def _work(fa, ws_root, cid: str, name: str, files: dict) -> str:
    """Child `cid`'s branch `name` with `files`, in a worktree of its own, linked on the ticket."""
    sha = _branch(ws_root, name, files)
    fa.link(cid, repo="app", branch=name, worktree=_wt(name))
    return sha


def _stage(name, commands=None, check=None, timeout=60, **more):
    return {"name": name, "commands": commands or [["make", f"{name}-it"]], "check": check or {"argv": ["make", "ok"]},
            "timeout": timeout, **more}


MERGE = _stage("merge", commands=[["gh", "pr", "create", "--base", "{base}", "--head", "{branch}"],
                                  ["gh", "pr", "merge", "{branch}", "--match-head-commit", "{sha}"]],
               check={"argv": ["gh", "pr", "view", "{branch}", "--json", "state", "-q", ".state"], "expect": "MERGED"})
DEV = _stage("dev", commands=[["make", "deploy-dev"]], check={"argv": ["make", "dev-health"]})
ROLLBACK = {"commands": [["make", "rollback-prod", "{epic}"]], "check": {"argv": ["make", "prod-health"], "expect": "ok"}}
PROD = _stage("production", commands=[["make", "deploy-prod", "{sha}"]],
              check={"argv": ["make", "prod-live", "{sha}"], "expect": "live {sha}"})


def _recipe(remote, **over):
    return {"stages": [MERGE, DEV], "sensitive_paths": [".github", "**/*.lock"], "base": "main",
            "remote": str(remote), **over}


@pytest.fixture
def recipe(remote):
    return _recipe(remote)


class Fake:
    """Stands in for the recipe's commands (by the program's arguments); the runner's git is never faked."""
    def __init__(self):
        self.calls, self.results, self.on_call = [], {}, None

    def __call__(self, argv, cwd, env, timeout, started=None):
        self.calls.append((argv, cwd, env, timeout))
        if started:
            started(os.getpid())
        if self.on_call:
            self.on_call(argv)
        key = " ".join(argv[1:3])
        r = {"code": 0, "out": "MERGED\n" if "view" in argv else "done\n", "err": "", "timed_out": False}
        r.update(self.results.get(key, {}))
        r.setdefault("out_size", len(r["out"]))
        return r

    def ran(self):
        return [" ".join([os.path.basename(a[0]), *a[1:]]) for a, *_ in self.calls]


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)


@pytest.fixture
def fws(configure, human):
    from orch.core.ops import Ops
    w = configure(factory={"enabled": True})
    Ops(w, human).set_factory_dark(True)
    return w


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


def _ready_epic(fws, fa, fh, human, close_tasks, *, release="dev", recipe=None, kids=1, arm=True, files=None,
                charter=None, epic_reqs="Build billing.py"):
    if recipe is not None:
        fr.set_recipe(fws, human, recipe)
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fa.set_section(e.id, "Requirements", epic_reqs)  # a named file by default, so the coverage check can pass
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": release, **(charter or {})})
    ids = []
    for i in range(kids):
        c = fa.new(f"child {i}", epic=e.id)
        _refine(fa, c.id)
        fa.set_section(c.id, "Requirements", "Part of billing.py")
        fa.epic_auto_approve(c.id)
        # the first child commits the file the epic names (the release checks it is in a child's commit); only one
        # child adds it, as two children adding one file conflict when they are merged
        _work(fa, fws.root, c.id, f"feat/{c.id.lower()}-work",
              {**(files or {f"src/{c.id}.py": "print(1)\n"}), **({"billing.py": "print(1)\n"} if i == 0 else {})})
        fa.claim(c.id)
        close_tasks(fa, c.id)
        fa.set_section(c.id, "Verification", "- AC1: ran `pytest -q` on the branch, 12 passed")
        fa.move(c.id, "testing")
        ids.append(c.id)
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    if arm:
        fs.arm(fws, human, d["id"])
    assert factory_report.ready(fws, store.load(fws, e.id)[1]) is not None
    return e.id, ids, d


@pytest.fixture
def ready(fws, fa, fh, human, close_tasks, recipe):
    def _make(**kw):
        kw.setdefault("recipe", recipe)
        return _ready_epic(fws, fa, fh, human, close_tasks, **kw)
    return _make


def _status(fws, eid):
    epic = store.load(fws, eid)[1]
    return fr.status(fws, epic, permits.factory_delegation(fws, epic))


def _states(fws, eid):
    return {s["name"]: s["state"] for s in _status(fws, eid)["stages"]}


def _stopped(fws, eid):
    return [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])]


# -- the recipe ---------------------------------------------------------------------------------------------------

def _bad(remote, **stage_over):
    return {"stages": [{**MERGE, **stage_over}], "remote": str(remote)}


@pytest.mark.parametrize("make,why", [
    (lambda r: _bad(r, commands=["gh pr merge x"]), "shell string"),
    (lambda r: _bad(r, commands=[["sh", "-c", "gh pr merge {sha} {base}"]]), "runs a string"),
    (lambda r: _bad(r, commands=[["nohup", "gh", "{sha}", "{base}"]]), "runs a string"),
    (lambda r: _bad(r, commands=[["timeout", "5", "gh", "{sha}", "{base}"]]), "runs a string"),
    (lambda r: _bad(r, commands=[["python3", "-c", "x", "{sha}", "{base}"]]), "runs code given as text"),
    (lambda r: _bad(r, commands=[["git", "-c", "alias.x=!sh", "x", "{sha}", "{base}"]]), "alias"),
    (lambda r: {"stages": [_stage("production")], "remote": str(r)}, "comes after a dev stage"),
    (lambda r: {"stages": [MERGE, _stage("production")], "remote": str(r)}, "comes after a dev stage"),
    (lambda r: {"stages": [_stage("staging")], "remote": str(r)}, "is not a stage"),
    (lambda r: {"stages": [DEV, MERGE], "remote": str(r)}, "in this order: merge, dev, production"),
    (lambda r: {"stages": [DEV, PROD, MERGE], "remote": str(r)}, "in this order"),
    (lambda r: {"stages": [MERGE, _stage("dev", window={"min_hours_since_last": 5})], "remote": str(r)},
     "only the production stage has a window"),
    (lambda r: {"stages": [{**MERGE, "rollback": ROLLBACK}], "remote": str(r)}, "only the production stage"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "window": {"min_hours_since_last": 0}}], "remote": str(r)},
     "never skipped"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "window": {"hours": 3}}], "remote": str(r)}, "window is"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "window": {"min_hours_since_last": True}}], "remote": str(r)},
     "whole number of hours"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "rollback": {"commands": [["make", "back"]]}}], "remote": str(r)},
     "both required"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "rollback": {**ROLLBACK, "commands": ["make back"]}}],
                "remote": str(r)}, "shell string"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "rollback": {**ROLLBACK, "commands": [["bash", "-c", "x"]]}}],
                "remote": str(r)}, "runs a string"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "rollback": {**ROLLBACK, "check": {"argv": ["sh", "-c", "x"]}}}],
                "remote": str(r)}, "runs a string"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "rollback": {**ROLLBACK, "commands": [["make", "{branch}"]]}}],
                "remote": str(r)}, "only filled in for a stage that runs per child"),
    (lambda r: {"stages": [MERGE, DEV, {**PROD, "per": "child"}], "remote": str(r)}, "once per epic"),
    (lambda r: {"stages": [MERGE, MERGE], "remote": str(r)}, "twice"),
    (lambda r: _bad(r, per="epic"), "runs per child"),
    (lambda r: _bad(r, commands=[["gh", "pr", "merge", "{branch}", "--base", "{base}"]]), "must name {sha}"),
    (lambda r: _bad(r, commands=[["gh", "pr", "merge", "{sha}"]]), "must name {base}"),
    (lambda r: _bad(r, commands=[["gh", "{nope}", "{sha}", "{base}"]]), "not a placeholder"),
    (lambda r: _bad(r, commands=[["gh", "{branch", "{sha}", "{base}"]]), "brace"),
    (lambda r: _bad(r, commands=[["gh", "{repo}", "{sha}", "{base}"]]), "needs the recipe's repo"),
    (lambda r: {"stages": [MERGE, _stage("dev", commands=[["make", "{branch}"]])], "remote": str(r)},
     "only filled in for a stage that runs per child"),
    (lambda r: {"stages": [MERGE, _stage("dev", per="child")], "remote": str(r)}, "once per epic"),
    (lambda r: {"stages": [MERGE, _stage("dev", precheck={"argv": ["make", "x"]})], "remote": str(r)},
     "only the merge stage has a precheck"),
    (lambda r: _bad(r, commands=[["{branch}", "x"]]), "placeholder"),
    (lambda r: _bad(r, timeout=1801), "timeout"),
    (lambda r: _bad(r, timeout=True), "timeout"),
    (lambda r: {"stages": [{k: v for k, v in MERGE.items() if k != "check"}], "remote": str(r)}, "proven only by"),
    (lambda r: {**_bad(r), "base": "-main"}, "base"),
    (lambda r: {**_bad(r), "base": "a..b"}, "base"),
    (lambda r: {**_bad(r), "extra": 1}, "nothing else"),
    (lambda r: {"stages": [MERGE]}, "remote are required"),
    (lambda r: {**_bad(r), "remote": "ext::sh -c id"}, "remote is"),
    (lambda r: {**_bad(r), "remote": "-uupload"}, "remote is"),
    (lambda r: {**_bad(r), "git_config": {"core.hooksPath": "x"}}, "git_config"),
    (lambda r: {"stages": [], "remote": str(r)}, "1 to"),
    (lambda r: _bad(r, commands=[["gh", "{sha}", "{base}"]] * 11), "1 to 10"),
    (lambda r: _bad(r, commands=[["gh", "x\ny", "{sha}", "{base}"]]), "printable"),
    (lambda r: _bad(r, check={"argv": ["gh"], "expect": "x" * 1025}), "expect"),
    (lambda r: {**_bad(r), "sensitive_paths": "x"}, "sensitive_paths"),
    (lambda r: {**_bad(r), "sensitive_paths": ["deploy/"]}, "no leading or trailing"),
])
def test_recipe_refusals(ws, remote, make, why):
    with pytest.raises(ValidationError) as e:
        fr.check_recipe(make(remote), ws)
    assert why in str(e.value)


def test_a_precheck_naming_the_base_is_enough(ws, remote):
    stage = {**MERGE, "commands": [["gh", "pr", "merge", "{branch}", "--match-head-commit", "{sha}"]],
             "precheck": {"argv": ["gh", "pr", "list", "--head", "{branch}", "--json", "baseRefName", "--jq",
                                   'map(select(.baseRefName != "{base}"))|length'], "expect": "0"}}
    rec = fr.check_recipe({"stages": [stage], "remote": "acme/app"}, ws)
    assert rec["remote_url"] == "https://github.com/acme/app.git" and rec["repo"] == "acme/app"


def test_program_checks_are_the_real_ones(ws, remote, bin_dir):
    good = {"stages": [MERGE], "remote": str(remote)}
    fr.check_recipe(good, ws)
    (bin_dir / "gh").chmod(0o777)  # writable by group and others
    with pytest.raises(ValidationError, match="trusted path"):
        fr.check_recipe(good, ws)
    (bin_dir / "gh").chmod(0o755)
    inside = ws.root / "tools"
    inside.mkdir()
    (inside / "gh2").write_text("#!/bin/sh\n", encoding="utf-8")
    (inside / "gh2").chmod(0o755)
    stage = {**MERGE, "commands": [[str(inside / "gh2"), "{sha}", "{base}"]]}
    with pytest.raises(ValidationError, match="inside the workspace"):
        fr.check_recipe({"stages": [stage], "remote": str(remote)}, ws)
    with pytest.raises(ValidationError, match="trusted path"):
        fr.check_recipe({"stages": [{**MERGE, "commands": [["no-such-program-xyz", "{sha}", "{base}"]]}],
                         "remote": str(remote)}, ws)


def test_recipe_is_stored_per_workspace_with_pinned_programs_by_the_human_only(ws, human, agent, recipe, bin_dir):
    with pytest.raises(HumanOnlyError):
        fr.set_recipe(ws, agent, recipe)
    assert fr.recipe(ws) is None and "no release recipe" in fr.load(ws)[1]
    rec = fr.set_recipe(ws, human, recipe)
    assert fr.recipe(ws)["stages"] == rec["stages"] and rec["programs"]["gh"]["path"] == os.path.realpath(
        bin_dir / "gh")
    data = json.loads(fr.path().read_text())
    assert list(data["workspaces"]) == [ledger.workspace_id(ws)] and oct(fr.path().stat().st_mode)[-3:] == "600"
    assert set(data["workspaces"][ledger.workspace_id(ws)]) == {"recipe", "programs"}
    with pytest.raises(HumanOnlyError):
        fr.clear_recipe(ws, agent)
    assert fr.clear_recipe(ws, human) is True and fr.recipe(ws) is None


def test_a_program_changed_after_it_was_pinned_is_refused_at_run_time(fws, ready, human, bin_dir):
    eid, _, _ = ready()
    (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert "not the one you pinned" in lines[0] and fake.calls == []
    assert _states(fws, eid)["merge"] == "waiting"  # refused before the intent: no attempt used


def test_a_damaged_or_foreign_writable_recipe_file_is_no_recipe(ws, human, recipe):
    fr.set_recipe(ws, human, recipe)
    os.chmod(fr.path(), 0o666)
    assert fr.recipe(ws) is None and "not yours alone" in fr.load(ws)[1]
    os.chmod(fr.path(), 0o600)
    fr.path().write_text("{nope", encoding="utf-8")
    assert fr.recipe(ws) is None
    with pytest.raises(ValidationError, match="damaged"):
        fr.set_recipe(ws, human, recipe)


def test_up_to_needs_every_stage_before_the_target():
    rec = {"stages": [{"name": "merge"}]}
    assert fr.up_to(rec, "merge") == [{"name": "merge"}] and fr.up_to(rec, "dev") is None
    assert fr.up_to({"stages": [{"name": "dev"}]}, "dev") is None and fr.up_to(rec, "production") is None


@pytest.mark.parametrize("pattern,path,hit", [
    (".github", ".github/workflows/ci.yml", True), (".github", ".GitHub/Workflows/CI.yml", True),
    (".github", "docs/.github/x", False), ("**/.github", "docs/.github/x", True), ("deploy", "deploy/a/b/c.sh", True),
    ("deploy", "deployment.md", False), ("**/migrations/*", "migrations/1.sql", True),
    ("**/migrations/*", "app/db/migrations/1.sql", True), ("*/migrations/*", "migrations/1.sql", False),
    ("*.lock", "poetry.lock", True), ("*.lock", "a/b/Cargo.LOCK", True), ("src/*.py", "SRC/A.PY", True),
])
def test_sensitive_patterns(pattern, path, hit):
    assert fr.sensitive(path, [pattern]) is hit


# -- the CLI ------------------------------------------------------------------------------------------------------

@pytest.fixture
def switch(monkeypatch):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setattr(orch_actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(orch_actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)
    return Switch()


def test_cli_set_show_clear_are_human_only_and_confirmed(ws, capsys, switch, tmp_path, recipe, bin_dir):
    f = tmp_path / "recipe.json"
    f.write_text(json.dumps(recipe), encoding="utf-8")
    switch.agent()
    for args in (["set", "--file", str(f)], ["show"], ["clear"]):
        assert cli_run(["factory", "release", *args]) != 0
    assert fr.recipe(ws) is None
    switch.human("nope")
    assert cli_run(["factory", "release", "set", "--file", str(f)]) != 0 and fr.recipe(ws) is None
    switch.human("RELEASE")
    capsys.readouterr()
    assert cli_run(["factory", "release", "set", "--file", str(f)]) == 0
    io = capsys.readouterr()
    out = io.out + io.err
    assert "--match-head-commit" in out and "production" in out and fr.recipe(ws) is not None
    assert os.path.realpath(bin_dir / "gh") in out and "sha256" in out  # the pins are shown before the confirmation
    assert cli_run(["factory", "release", "show"]) == 0 and "deploy-dev" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"stages": [_stage("staging")], "remote": "acme/app"}), encoding="utf-8")
    assert cli_run(["factory", "release", "set", "--file", str(bad)]) != 0
    assert "is not a stage" in capsys.readouterr().err
    switch.human("CLEAR")
    assert cli_run(["factory", "release", "clear"]) == 0 and fr.recipe(ws) is None


# -- guard and never-grantable ------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat {base}/permits/factory-release.json", "echo x > $ORCH_STATE_DIR/permits/factory-release.json",
    "rm ~/.config/orch/permits/factory-release.json", "cd {base}/permits && cat factory-release.json",
    "ls {base}/permits/release-records", "git -C {base}/permits/release-repos/x/repo log",
    "orch factory release set --file r.json", "orch factory release clear",
    "orch factory release retry L-0001 --stage merge", "uv run orch factory release show",
    "python3 -c 'from orch.core import factory_release'",
    "python3 -c 'from orch.core import factory_close'", "python3 -c 'import orch.core.factory_sessions'",
    "python3 -c 'from orch.core import epics, factory_clones'", "python3 -c 'import orch.core.factory_runner'",
    "python3 -c 'from orch.dashboard.factory_runner import release_once'",
    "python3 -c 'from orch.dashboard import factory_runner'",
    "orch factory clones clean L-0002", "orch factory clones list",
    "python3 -c \"from orch.cli import app; app(['factory', 'release', 'clear'])\"",
])
def test_guard_keeps_agents_from_the_recipe_and_its_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=ledger.base_dir())},
                      "cwd": str(ws.root)})
    assert not d.allow, cmd
    assert permits.never_grantable(ws, cmd.format(base=ledger.base_dir())) is not None


def test_the_childrens_clones_are_never_grantable_on_their_own():
    assert any(p.search("orch factory clones clean L-0002") for p, _ in permits._NEVER)


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("Read", "file_path")])
def test_guard_keeps_file_tools_from_the_recipe(ws, tool, key):
    from orch.hooks.guard import evaluate
    inp = {key: str(ledger.base_dir() / "permits" / "factory-release.json"), "content": "{}", "old_string": "a",
           "new_string": "b"}
    assert not evaluate(ws, {"tool_name": tool, "tool_input": inp, "cwd": str(ws.root)}).allow


def test_guard_still_lets_ordinary_release_words_through(ws):
    from orch.hooks.guard import evaluate
    for cmd in ("npm run release", "git log --oneline release/1.0", "orch log L-0001 -m 'release notes updated'"):
        assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow, cmd


# -- the charter field --------------------------------------------------------------------------------------------

def test_release_is_hashed_only_when_set():
    base = {"factory": True, "dark": True}
    assert epics.normalize_delegate(base) == epics.normalize_delegate({**base, "release": None}) \
        == epics.normalize_delegate({**base, "release": "none"})
    assert "release" not in epics.normalize_delegate(base)
    assert epics.normalize_delegate({**base, "release": "dev"})["release"] == "dev"
    with pytest.raises(UsageError, match="--dark"):
        epics.normalize_delegate({"factory": True, "release": "merge"})
    with pytest.raises(UsageError, match="merge, dev or prod"):
        epics.normalize_delegate({**base, "release": "production"})


def test_existing_charter_hashes_are_unchanged(ws, aops):
    e = aops.new("E", type="epic")
    epic = store.load(ws, e.id)[1]
    for delegate in ({"factory": True}, {"factory": True, "dark": True}, {}):
        assert epics.charter(ws, epic, delegate)["hash"] == epics.charter(ws, epic, {**delegate, "release": None})[
            "hash"]
    dark = {"factory": True, "dark": True}
    assert epics.charter(ws, epic, dark)["hash"] != epics.charter(ws, epic, {**dark, "release": "merge"})["hash"]


def test_approve_release_needs_dark_and_a_recipe_with_the_stages(fws, fa, fh, human, remote):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    with pytest.raises(ValidationError, match="no release recipe"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "merge"})
    fr.set_recipe(fws, human, {"stages": [MERGE], "remote": str(remote)})
    with pytest.raises(ValidationError, match="up to dev"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "dev"})
    with pytest.raises(UsageError):
        fh.approve(e.id, "requirements", delegate={"factory": True, "release": "merge"})
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "merge"})
    assert epics.delegation(fws, store.load(fws, e.id)[1])["release"] == "merge"


def test_cli_approve_release_shows_what_runs(fws, fa, capsys, switch, human, recipe):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    switch.human(e.id)
    assert cli_run(["approve", e.id, "requirements", "--factory", "--release", "dev"]) != 0
    assert "--release goes with --dark" in capsys.readouterr().err
    fr.set_recipe(fws, human, recipe)
    assert cli_run(["approve", e.id, "requirements", "--dark", "--release", "dev"]) == 0
    out = capsys.readouterr().out
    assert "releases up to dev by itself using the recipe on this machine" in out
    assert "nothing releases to production" in out and "verdict is yours" in out


# -- the executor: the happy path, in the runner's own repository -------------------------------------------------

def test_happy_path_merges_each_child_then_deploys_dev(fws, ready, human, remote):
    eid, (c1, c2), _ = ready(kids=2)
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: merge of {c1} proven", f"{eid}: merge of {c2} proven", f"{eid}: dev of {eid} proven"]
    b1 = f"feat/{c1.lower()}-work"
    sha1 = _g(fws.root, "rev-parse", b1)
    assert fake.ran()[:3] == [f"gh pr create --base main --head {b1}", f"gh pr merge {b1} --match-head-commit {sha1}",
                              f"gh pr view {b1} --json state -q .state"]
    assert fake.ran()[-2:] == ["make deploy-dev", "make dev-health"]
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"} and _stopped(fws, eid) == []
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 8  # nothing runs twice
    ev = [e for e in read_events(fws) if e.kind == "release.stage"]
    assert [e.data for e in ev][-1] == {"stage": "dev", "child": eid, "proven": True, "exit": 0}


def test_commands_run_in_the_runners_repository_with_a_scrubbed_env(fws, ready, human, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("ORCH_DASHBOARD_TOKEN", "x")
    eid, (c,), _ = ready()
    fake = Fake()
    seen_heads = []
    fake.on_call = lambda argv: seen_heads.append(_g(fr.repo_dir(fws), "rev-parse", "HEAD"))
    fr.tick(fws, human, fake)
    repo = str(fr.repo_dir(fws))
    assert repo.startswith(str(ledger.base_dir() / "permits" / "release-repos"))
    for argv, cwd, env, timeout in fake.calls:
        assert cwd == repo and cwd != str(fws.root)
        assert set(env) <= set(__import__("orch.core.factory_runner", fromlist=["x"]).ENV_ALLOW) | {
            "PATH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ATTR_NOSYSTEM", "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_TERMINAL_PROMPT"}
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and os.path.getsize(env["GIT_CONFIG_GLOBAL"]) == 0
        assert "GH_TOKEN" not in env
    sha = _g(fws.root, "rev-parse", f"feat/{c.lower()}-work")
    assert seen_heads[:3] == [sha] * 3  # the merge stage ran at exactly the checked commit
    assert seen_heads[-1] == _g(fr.repo_dir(fws), "rev-parse", "refs/remotes/release/main")  # dev at the remote base
    cfg = (fr.repo_dir(fws) / ".git" / "config").read_text()
    assert "hooksPath = /dev/null" in cfg and "fsmonitor = false" in cfg and "[remote" not in cfg


def test_a_malicious_workspace_git_config_runs_nothing(fws, ready, human, tmp_path):
    """The workspace's .git/config is agent-written: hooksPath and fsmonitor there must never run in the release."""
    eid, _, _ = ready()
    marker = tmp_path / "pwned.log"
    tools = fws.root / "tools"
    tools.mkdir()
    for hook in ("post-checkout", "post-merge", "reference-transaction", "pre-push", "post-index-change", "fsmon",
                 "pre-auto-gc", "post-rewrite"):
        (tools / hook).write_text(f"#!/bin/sh\necho {hook} >> '{marker}'\n", encoding="utf-8")
        (tools / hook).chmod(0o755)
    with open(fws.root / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[core]\n\thooksPath = {tools}\n\tfsmonitor = {tools / 'fsmon'}\n[diff]\n\tignoreSubmodules = all\n"
                "[alias]\n\tdiff = !touch pwned\n")
    fr.tick(fws, human, Fake())
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"}
    assert not marker.exists()


# -- classification: the reviewer's attacks, with real git ----------------------------------------------------------

def test_a_renamed_sensitive_file_is_seen_as_both_paths(fws, ready, human, remote):
    _main_commit(fws.root, remote, {".github/CODEOWNERS": "* @owner\n"})
    eid, (c,), _ = ready(files={})
    b = f"feat/{c.lower()}-work"
    w = fws.root / _wt(b)
    (w / "docs").mkdir(exist_ok=True)
    _g(w, "mv", ".github/CODEOWNERS", "docs/CODEOWNERS")
    _g(w, "commit", "-q", "-m", "move it away")
    renamed = _g(fws.root, "diff", "-M", "--name-only", f"main...{b}")
    assert ".github/CODEOWNERS" not in renamed  # with rename detection the sensitive side would be hidden
    fake = Fake()
    fr.tick(fws, human, fake)
    assert _stopped(fws, eid) == ["sensitive"] and fake.calls == []
    assert ".github/CODEOWNERS" in factory_report.stopped(fws, store.load(fws, eid)[1])[0]["text"]


def test_a_gitlink_hidden_by_ignore_submodules_is_seen(fws, ready, human, remote):
    eid, (c,), _ = ready(files={".gitmodules": '[submodule "x"]\n\tpath = vendor/deploy\n\tignore = all\n'})
    b = f"feat/{c.lower()}-work"
    w = fws.root / _wt(b)
    _g(w, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/deploy")
    _g(w, "commit", "-q", "-m", "a gitlink")
    _g(fws.root, "config", "diff.ignoreSubmodules", "all")
    rec = fr.load(fws)[0]
    rec = {**rec, "sensitive_paths": ["vendor/deploy"]}
    found, hits, errors = fr.classify(fws, rec, [store.load(fws, c)[1]])
    assert errors == {} and hits == {c: ["vendor/deploy (a submodule)", ".gitmodules"]}


def test_a_replace_ref_in_the_workspace_hides_nothing(fws, ready, human, remote):
    eid, (c,), _ = ready(files={".github/workflows/ci.yml": "evil\n"})
    b = f"feat/{c.lower()}-work"
    innocent = _branch(fws.root, "decoy", {"src/ok.py": "ok\n"})
    _g(fws.root, "replace", _g(fws.root, "rev-parse", b), innocent)
    assert ".github" not in _g(fws.root, "diff", "--name-only", f"main...{b}")  # the workspace's own view is fooled
    fr.tick(fws, human, Fake())
    assert _stopped(fws, eid) == ["sensitive"]


def test_a_moved_local_base_hides_nothing(fws, ready, human, remote):
    """The agent commits a sensitive change onto its local main (never pushed) and branches from there: the runner
    compares with the remote's base, not the workspace's."""
    _main_commit(fws.root, remote, {".github/workflows/ci.yml": "evil\n"}, push=False)
    eid, (c,), _ = ready()
    assert ".github" not in _g(fws.root, "diff", "--name-only", f"main...feat/{c.lower()}-work")
    fr.tick(fws, human, Fake())
    assert _stopped(fws, eid) == ["sensitive"]


def test_a_sensitive_change_reverted_later_on_the_branch_is_seen(fws, ready, human):
    eid, (c,), _ = ready(files={".github/x.yml": "evil\n"})
    b = f"feat/{c.lower()}-work"
    w = fws.root / _wt(b)
    _g(w, "rm", "-q", ".github/x.yml")
    _g(w, "commit", "-q", "-m", "hide it")
    fr.tick(fws, human, Fake())
    assert _stopped(fws, eid) == ["sensitive"]  # every commit the branch brings in, not only the net diff


def test_a_case_differing_path_is_seen(fws, ready, human):
    eid, _, _ = ready(files={".GitHub/Workflows/CI.yml": "evil\n"})
    fr.tick(fws, human, Fake())
    assert _stopped(fws, eid) == ["sensitive"]


def test_a_sensitive_path_stops_before_anything_is_merged_and_retry_checks_again(fws, ready, human):
    eid, (c1, c2), _ = ready(kids=2, files={"src/a.py": "a\n", "poetry.lock": "x\n"})
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: release stopped: a sensitive path is touched; nothing was merged"]
    assert fake.ran() == [] and _stopped(fws, eid) == ["sensitive"]
    assert "poetry.lock" in factory_report.stopped(fws, store.load(fws, eid)[1])[0]["text"]
    assert fr.tick(fws, human, fake) == []  # stays stopped until the human retries
    for k in (c1, c2):
        b = f"feat/{k.lower()}-work"
        w = fws.root / _wt(b)
        _g(w, "rm", "-q", "poetry.lock")
        _g(w, "commit", "-q", "-m", "drop the lock")
    fr.retry(fws, human, eid, "merge", eid)
    fr.tick(fws, human, fake)
    assert _stopped(fws, eid) == ["sensitive"]  # every commit counts: the lock is still in the branch's history


def test_sensitive_paths_are_escaped_in_the_reason(fws, ready, human):
    eid, _, _ = ready(files={".github/‮x.yml": "x\n"})
    fr.tick(fws, human, Fake())
    text = factory_report.stopped(fws, store.load(fws, eid)[1])[0]["text"]
    assert "‮" not in text and "\\u202e" in text


# -- the executor: failures, crash safety, retries --------------------------------------------------------------

def test_a_failing_command_stops_the_release_and_nothing_after_it_runs(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fake.results["pr merge"] = {"code": 1, "out": "boom <script>\n"}
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: merge of {c} failed (exit code 1)"]
    assert [x.split()[2] for x in fake.ran()] == ["create", "merge"]
    assert _states(fws, eid) == {"merge": "failed", "dev": "waiting"} and _stopped(fws, eid) == ["release-failed"]
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 2  # at most one automatic attempt


def test_a_failing_check_or_wrong_output_proves_nothing(fws, ready, human):
    eid, _, _ = ready()
    fake = Fake()
    fake.results["pr view"] = {"code": 0, "out": "OPEN\n"}
    assert "check's output is not what the recipe expects" in fr.tick(fws, human, fake)[0]
    assert _states(fws, eid)["merge"] == "failed"


def test_placeholders_in_expect_are_filled_in(fws, ready, human, remote):
    stage = {**MERGE, "check": {"argv": ["gh", "pr", "view", "{branch}"], "expect": "{base} {sha} MERGED"}}
    eid, (c,), _ = ready(recipe=_recipe(remote, stages=[stage]), release="merge")
    sha = _g(fws.root, "rev-parse", f"feat/{c.lower()}-work")
    fake = Fake()
    fake.results["pr view"] = {"out": f"main {sha} MERGED\n"}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven"}


def test_a_precheck_that_refuses_fails_the_merge_before_any_command(fws, ready, human, remote):
    stage = {**MERGE, "precheck": {"argv": ["gh", "pr", "list", "--head", "{branch}", "--jq",
                                            'map(select(.baseRefName != "{base}"))|length'], "expect": "0"}}
    eid, _, _ = ready(recipe=_recipe(remote, stages=[stage]), release="merge")
    fake = Fake()
    fake.results["pr list"] = {"out": "1\n"}  # a pull request is open against another base
    assert "precheck refused" in fr.tick(fws, human, fake)[0]
    assert [x.split()[2] for x in fake.ran()] == ["list"] and _states(fws, eid) == {"merge": "failed"}


def test_check_exit_code_decides(fws, ready, human):
    eid, _, _ = ready(release="merge")
    fake = Fake()
    fake.results["pr view"] = {"code": 3, "out": "MERGED\n"}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "failed"}


def test_a_timeout_leaves_the_stage_unknown(fws, ready, human):
    eid, _, _ = ready()
    fake = Fake()
    fake.results["pr create"] = {"code": None, "timed_out": True}
    assert "timed out after 60 seconds" in fr.tick(fws, human, fake)[0]
    assert _states(fws, eid)["merge"] == "unknown" and fake.calls[-1][3] == 60  # killed midway: it may have merged
    assert _stopped(fws, eid) == ["release-unknown"]


def test_a_crash_between_intent_and_outcome_is_unknown_and_never_rerun(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()

    def crash(argv):
        if "create" in argv:
            raise KeyboardInterrupt  # the runner dies while the command runs
    fake.on_call = crash
    with pytest.raises(KeyboardInterrupt):
        fr.tick(fws, human, fake)
    fake.on_call = None
    assert _states(fws, eid)["merge"] == "unknown" and _stopped(fws, eid) == ["release-unknown"]
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 1


def test_the_lock_holds_while_the_recorded_command_group_lives(fws, ready, human, monkeypatch):
    eid, _, _ = ready()
    assert fr.acquire(fws, eid, 60)
    fr._refresh(fws, eid, 60, pgid=777)
    starts = {os.getpid(): "dashboard gone", 777: "Mon Oct  4 10:00:00 2026"}
    monkeypatch.setattr(fs, "proc_start", lambda pid: starts.get(pid))
    assert fr.lock_holder(fws)["epic"] == eid  # the dashboard died, its command still runs
    starts[777] = None
    assert fr.lock_holder(fws) is None and fr.acquire(fws, eid, 60)


def test_terminate_all_stops_running_commands_promptly(tmp_path):
    result = {}
    t = threading.Thread(target=lambda: result.update(fr.run_command(
        ["/bin/sleep", "30"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, 60)))
    t.start()
    for _ in range(100):
        if fr._RUNNING:
            break
        time.sleep(0.02)
    began = time.monotonic()
    assert fr.terminate_all(grace=1.0) == 1
    t.join(10)
    assert not t.is_alive() and time.monotonic() - began < 5 and result["code"] is not None and result["code"] < 0
    assert fr.run_command(["/bin/echo", "x"], str(tmp_path), {}, 5)["code"] is None  # nothing new starts


def test_restart_reruns_nothing_proven(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fake.results["deploy-dev"] = {"code": 2}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "failed"}
    fr.retry(fws, human, eid, "dev", eid)
    fake.results.clear()
    n = len(fake.ran())
    assert fr.tick(fws, human, fake) == [f"{eid}: dev of {eid} proven"]
    assert fake.ran()[n:] == ["make deploy-dev", "make dev-health"]  # the merge was not run again


def test_retry_is_human_only_and_allows_exactly_one_more_attempt(fws, ready, human, agent):
    eid, (c,), _ = ready()
    fake = Fake()
    fake.results["pr merge"] = {"code": 1}
    fr.tick(fws, human, fake)
    with pytest.raises(HumanOnlyError):
        fr.retry(fws, agent, eid, "merge", c)
    with pytest.raises(ValidationError, match="only a failed, unknown"):
        fr.retry(fws, human, eid, "dev", eid)
    fr.retry(fws, human, eid, "merge", c)
    with pytest.raises(ValidationError):
        fr.retry(fws, human, eid, "merge", c)  # one retry per failure
    assert _states(fws, eid)["merge"] == "waiting" and _stopped(fws, eid) == []
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["merge"] == "failed" and [x.split()[2] for x in fake.ran()].count("merge") == 2
    fr.tick(fws, human, fake)
    assert len(fake.ran()) == 4


def test_retry_after_unknown_closes_the_open_attempt(fws, ready, human):
    eid, (c,), _ = ready()
    fs._create(fr._dir(fws, eid) / fr._name("merge", c, 1, "intent"), {"stage": "merge"})
    assert _states(fws, eid)["merge"] == "unknown"
    fr.retry(fws, human, eid, "merge", c)
    fr.tick(fws, human, Fake())
    assert _states(fws, eid)["merge"] == "proven"


# -- staleness ----------------------------------------------------------------------------------------------------

def test_a_child_that_changed_after_its_merge_makes_the_release_stale(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"}
    _branch(fws.root, f"feat/{c.lower()}-work", {"src/more.py": "more\n"}, start=f"feat/{c.lower()}-work")
    lines = fr.tick(fws, human, fake)
    assert "out of date" in lines[0]
    assert _states(fws, eid) == {"merge": "stale", "dev": "stale"} and "release-stale" in _stopped(fws, eid)
    fr.retry(fws, human, eid, "merge", c)
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "stale"}  # dev ran for the old commit
    fr.retry(fws, human, eid, "dev", eid)
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"} and _stopped(fws, eid) == []


def test_a_child_added_after_dev_was_proven_makes_dev_stale(fws, ready, fa, human, close_tasks):
    eid, _, _ = ready()
    fr.tick(fws, human, Fake())
    c = fa.new("late child", epic=eid)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    _work(fa, fws.root, c.id, f"feat/{c.id.lower()}-work", {"src/late.py": "x\n"})
    fa.claim(c.id)
    close_tasks(fa, c.id)
    fa.set_section(c.id, "Verification", "- AC1: ok")
    fa.move(c.id, "testing")
    assert _states(fws, eid)["dev"] == "stale" and "release-stale" in _stopped(fws, eid)


# -- placeholders, branches, the gate -----------------------------------------------------------------------------

def _set_branch(fws, c, branch):
    """What an agent can do: write any branch name into its ticket file."""
    path = store.resolve(fws, c).path
    text = path.read_text(encoding="utf-8").replace(f"feat/{c.lower()}-work", json.dumps(branch)[1:-1])
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("branch", ["feat/X;rm -rf ~", "--upload-pack=evil-X", "feat/$(id)-X", "main", "other-work",
                                    "feat/../X", "X --exec=sh"])
def test_branch_names_are_validated_before_they_reach_a_command(fws, ready, human, branch):
    eid, (c,), _ = ready()
    _set_branch(fws, c, branch.replace("X", c.lower()))
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert fake.ran() == [] and "not started" in lines[0] and _stopped(fws, eid) == ["release-failed"]


def test_placeholders_fill_whole_validated_values_only():
    rec = {"base": "main", "remote_url": "https://github.com/acme/app.git", "repo": "acme/app"}
    ctx = fr._context("L-0001", "0123456789abcdef", rec, "L-0002", "feat/l-0002", "a" * 40)
    assert fr.expand(["x-{child}", "{branch}", "{sha}", "{base}", "{repo}"], ctx) == [
        "x-L-0002", "feat/l-0002", "a" * 40, "main", "acme/app"]
    for bad in (("L-1; rm", "0123456789abcdef"), ("L-0001", "nothex")):
        with pytest.raises(ValidationError):
            fr._context(*bad)
    with pytest.raises(ValidationError):
        fr._context("L-0001", "0123456789abcdef", rec, "L-0002", "-x", "a" * 40)
    with pytest.raises(ValidationError):
        fr._context("L-0001", "0123456789abcdef", rec, "L-0002", "feat/l-0002", "abc")
    with pytest.raises(ValidationError):
        fr.expand(["{branch}"], fr._context("L-0001", "0123456789abcdef", rec))


def test_a_branch_that_moves_between_commands_is_not_merged(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()

    def move(argv):
        if "create" in argv:
            _branch(fws.root, f"feat/{c.lower()}-work", {"src/sneak.py": "x\n"}, start=f"feat/{c.lower()}-work")
    fake.on_call = move
    assert "branch moved since it was checked" in fr.tick(fws, human, fake)[0]
    assert [x.split()[2] for x in fake.ran()] == ["create"]  # the merge command never ran


@pytest.mark.parametrize("block", ["request", "pause", "edit", "off", "dark-off", "cut", "unarmed", "budget"])
def test_the_release_does_not_start_when_blocked(fws, fa, fh, ready, human, block, monkeypatch, configure):
    eid, (c,), d = ready(arm=block != "unarmed")
    w = fws
    if block == "request":
        permits.request(fws, Actor("agent", "claude-code", "cli", "7f3c9a21-0000"), store.load(fws, c)[1], "make x")
    elif block == "pause":
        fh.epic_pause(eid)
    elif block == "edit":
        fa.set_section(eid, "Requirements", "changed")
    elif block == "off":
        w = configure(factory={"enabled": False})
    elif block == "dark-off":
        from orch.core.ops import Ops
        Ops(fws, human).set_factory_dark(False)
    elif block == "cut":
        monkeypatch.setattr(ledger, "head_ok", lambda: False)
    elif block == "budget":
        monkeypatch.setattr(permits, "budget_reason", lambda *a, **k: "time budget used up")
    fake = Fake()
    assert fr.tick(w, human, fake) == [] and fake.calls == []


def test_a_pause_between_commands_aborts_before_the_next_one(fws, ready, fh, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fake.on_call = lambda argv: fh.epic_pause(eid) if "create" in argv else None
    lines = fr.tick(fws, human, fake)
    assert "stopped before the next command" in lines[0]
    assert [x.split()[2] for x in fake.ran()] == ["create"]


def test_the_lock_keeps_two_epics_from_releasing_at_once(fws, ready, human):
    eid, _, _ = ready()
    assert fr.acquire(fws, "L-9999", 60)
    fake = Fake()
    assert fr.tick(fws, human, fake) == [f"{eid}: the release waits: another release holds the lock"]
    assert fake.calls == [] and not fr.acquire(fws, eid, 60)
    fr.release_lock(fws)
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["dev"] == "proven" and fr.lock_holder(fws) is None


def test_output_is_kept_escaped_in_the_record_and_never_in_events(fws, ready, human):
    eid, _, _ = ready()
    fake = Fake()
    fake.results["pr merge"] = {"code": 1, "out": "TOKEN=hunter2 ‮" + "x" * 9000}
    fr.tick(fws, human, fake)
    unit = _status(fws, eid)["stages"][0]["units"][0]
    assert len(unit["tail"]) <= fr.TAIL and "‮" not in unit["tail"]
    for p in (fws.home / ".state").glob("**/*"):
        if p.is_file():
            assert "hunter2" not in p.read_text(errors="replace")
    for t in store.scan(fws):
        assert "hunter2" not in t.path.read_text()


def test_the_executor_refuses_an_agent(fws, agent):
    with pytest.raises(HumanOnlyError):
        fr.tick(fws, agent, Fake())


def test_the_executor_refuses_a_process_under_an_agent_harness(fws, human, monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    with pytest.raises(HumanOnlyError):
        fr.tick(fws, human, Fake())


def test_never_runs_without_a_release_target_or_for_a_non_dark_epic(fws, fa, fh, human, close_tasks, recipe):
    fr.set_recipe(fws, human, recipe)
    for delegate in ({"factory": True}, {"factory": True, "dark": True}):
        e = fa.new("E", type="epic")
        _refine(fa, e.id, plan=None)
        fh.approve(e.id, "requirements", delegate=delegate)
        fs.arm(fws, human, epics.delegation(fws, store.load(fws, e.id)[1])["id"])
        c = fa.new("c", epic=e.id)
        _refine(fa, c.id)
        fa.epic_auto_approve(c.id)
        fa.link(c.id, repo="app", branch=f"feat/{c.id.lower()}")
        fa.claim(c.id)
        close_tasks(fa, c.id)
        fa.set_section(c.id, "Verification", "- AC1: ok")
        fa.move(c.id, "testing")
        assert fr.status(fws, store.load(fws, e.id)[1], epics.delegation(fws, store.load(fws, e.id)[1])) is None
    fake = Fake()
    assert fr.tick(fws, human, fake) == [] and fake.calls == []


def test_a_cleared_recipe_releases_nothing(fws, ready, human):
    ready()
    fr.clear_recipe(fws, human)
    fake = Fake()
    assert "no release" in fr.tick(fws, human, fake)[0] and fake.calls == []


def test_a_base_the_remote_does_not_have_fails_closed(fws, ready, human, remote):
    eid, _, _ = ready(recipe=_recipe(remote, base="release-train"))
    fake = Fake()
    assert "could not be fetched from the recipe's remote" in fr.tick(fws, human, fake)[0]
    assert fake.calls == [] and _stopped(fws, eid) == ["release-failed"]


def test_a_child_without_a_matching_branch_fails_the_merge_without_running_it(fws, ready, human):
    eid, (c,), _ = ready()
    _set_branch(fws, c, "feat/unrelated")
    fake = Fake()
    assert f"does not name {c}" in fr.tick(fws, human, fake)[0]
    assert fake.ran() == [] and _stopped(fws, eid) == ["release-failed"]


def test_run_command_keeps_only_the_tail_and_kills_on_timeout(tmp_path):
    r = fr.run_command([sys.executable, "-c", "print('x' * 10000)"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, 30)
    assert r["code"] == 0 and r["out_size"] > fr.TAIL and len(r["out"]) <= fr.TAIL
    r = fr.run_command(["/bin/sleep", "30"], str(tmp_path), {"PATH": "/usr/bin"}, 1)
    assert r["timed_out"] and r["code"] is None


def test_records_are_written_through_random_temporary_names(tmp_path):
    target = tmp_path / "x.json"
    trap = tmp_path / "elsewhere"
    (tmp_path / ".x.json.tmp").symlink_to(trap)  # a planted predictable name is never followed
    fr._atomic(target, "{}")
    assert target.read_text() == "{}" and not trap.exists()


# -- the human's own git config is never used by the release (re-review: an agent can write it) -------------------

def _hostile_home(tmp_path, monkeypatch, marker, extra=""):
    """HOME and XDG_CONFIG_HOME as an agent could leave them: a global smudge filter, an attributes file that applies
    it everywhere, and `extra`; conftest's empty GIT_CONFIG_GLOBAL is taken away so nothing protects git but the
    runner itself."""
    home = tmp_path / "hostile-home"
    xdg = home / ".config"
    (xdg / "git").mkdir(parents=True)
    attrs = home / "attrs"
    attrs.write_text("* filter=pwn\n", encoding="utf-8")
    cfg = (f'[filter "pwn"]\n\tsmudge = touch {marker}; cat\n\tclean = cat\n[core]\n\tattributesFile = {attrs}\n'
           f'{extra}')
    (home / ".gitconfig").write_text(cfg, encoding="utf-8")
    (xdg / "git" / "config").write_text(cfg, encoding="utf-8")
    (xdg / "git" / "attributes").write_text("* filter=pwn\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)


def test_a_hostile_global_filter_and_attributes_file_run_nothing(fws, ready, human, tmp_path, monkeypatch):
    eid, (c,), _ = ready(files={".gitattributes": "* filter=pwn\n", "src/a.py": "x\n"})
    marker = tmp_path / "PWNED_BY_SMUDGE"
    _hostile_home(tmp_path, monkeypatch, marker)
    # the attack is real: plain git with this HOME runs the filter on checkout
    subprocess.run(["git", "clone", "-q", "--branch", f"feat/{c.lower()}-work", str(fws.root), str(tmp_path / "c")],
                   check=True, capture_output=True)
    assert marker.exists()
    marker.unlink()
    fr.tick(fws, human, Fake())
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"}
    assert not marker.exists()  # neither the runner's fetches, classification nor its checkouts ran it


def test_a_hostile_global_url_rewrite_does_not_swap_the_base(fws, ready, human, tmp_path, monkeypatch, remote):
    eid, (c,), _ = ready(files={".github/workflows/ci.yml": "evil\n"})
    fake = tmp_path / "fake.git"
    _g(tmp_path, "init", "-q", "--bare", str(fake))
    _g(fws.root, "push", "-q", str(fake), f"feat/{c.lower()}-work:refs/heads/main")
    real_base = _g(fws.root, "rev-parse", "main")
    _hostile_home(tmp_path, monkeypatch, tmp_path / "PWNED",
                  extra=f'[url "{fake}"]\n\tinsteadOf = {remote}\n')
    seen = subprocess.run(["git", "ls-remote", str(remote), "refs/heads/main"], check=True, capture_output=True,
                          text=True).stdout.split()[0]
    assert seen != real_base  # the attack is real: plain git would read the fake base
    rec = fr.load(fws)[0]
    fr.ensure_repo(fws, rec)
    assert fr.fetch_base(fws, rec) == real_base
    fr.tick(fws, human, Fake())
    assert _stopped(fws, eid) == ["sensitive"]  # compared with the real base, the sensitive path is seen
