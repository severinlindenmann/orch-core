"""Dark AI Factory phase 6: the release recipe, the charter's `release` field and the runner's release step. A fake
runner stands in for every command: no test runs a real gh, git, merge, push or deploy."""
import json
import os

import pytest

from orch import actor as orch_actor
from orch.cli import run as cli_run
from orch.core import epics, factory_release as fr, factory_report, factory_runner, factory_sessions as fs, ledger, \
    permits, store
from orch.core.events import Actor, read_events
from orch.errors import HumanOnlyError, UsageError, ValidationError

SHA = "a" * 40
SHA2 = "b" * 40


def _stage(name, per=None, commands=None, check=None, timeout=60):
    s = {"name": name, "commands": commands or [["make", f"{name}-it"]], "check": check or {"argv": ["make", "ok"]},
         "timeout": timeout}
    if per:
        s["per"] = per
    return s


MERGE = _stage("merge", commands=[["gh", "pr", "merge", "{branch}", "--match-head-commit", "{sha}"]],
               check={"argv": ["gh", "pr", "view", "{branch}", "--json", "state", "-q", ".state"], "expect": "MERGED"})
DEV = _stage("dev", commands=[["make", "deploy-dev"]], check={"argv": ["make", "dev-health"]})
RECIPE = {"stages": [MERGE, DEV], "sensitive_paths": [".github/*", "*.lock"], "base": "main"}


@pytest.fixture(autouse=True)
def _trusted_programs(monkeypatch):
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: f"/opt/test/{os.path.basename(name)}")


class Fake:
    """Answers git with a branch commit and a list of changed paths; every other command with `results` (by the
    program's name, then the default)."""
    def __init__(self, paths=("src/app.py",), sha=SHA):
        self.calls, self.paths, self.sha, self.results = [], list(paths), sha, {}
        self.on_call = None

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((argv, cwd, env, timeout))
        if self.on_call:
            self.on_call(argv)
        if os.path.basename(argv[0]) == "git":
            if "rev-parse" in argv:
                return {"code": 0, "out": self.sha + "\n", "out_size": 41, "err": "", "timed_out": False}
            out = "\x00".join(self.paths)
            return {"code": 0, "out": out, "out_size": len(out), "err": "", "timed_out": False}
        key = " ".join(argv[1:3])
        r = {"code": 0, "out": "MERGED\n" if "view" in argv else "done\n", "err": "", "timed_out": False}
        r.update(self.results.get(key, {}))
        r.setdefault("out_size", len(r["out"]))
        return r

    def ran(self):
        return [" ".join([os.path.basename(a[0]), *a[1:]]) for a, *_ in self.calls
                if os.path.basename(a[0]) != "git"]


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


def _ready_epic(fws, fa, fh, human, close_tasks, *, release="dev", recipe=RECIPE, kids=1, arm=True):
    if recipe is not None:
        fr.set_recipe(fws, human, recipe)
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": release})
    ids = []
    for i in range(kids):
        c = fa.new(f"child {i}", epic=e.id)
        _refine(fa, c.id)
        fa.epic_auto_approve(c.id)
        fa.link(c.id, repo="app", branch=f"feat/{c.id.lower()}-work")
        fa.claim(c.id)
        close_tasks(fa, c.id)
        fa.set_section(c.id, "Verification", "- AC1: ran the suite, green")
        fa.move(c.id, "testing")
        ids.append(c.id)
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    if arm:
        fs.arm(fws, human, d["id"])
    assert factory_report.ready(fws, store.load(fws, e.id)[1]) is not None
    return e.id, ids, d


def _status(fws, eid):
    epic = store.load(fws, eid)[1]
    return fr.status(fws, epic, permits.factory_delegation(fws, epic))


def _states(fws, eid):
    return {s["name"]: s["state"] for s in _status(fws, eid)["stages"]}


def _stopped(fws, eid):
    return [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])]


# -- the recipe ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data,why", [
    ({"stages": [_stage("merge", commands=["gh pr merge x"])]}, "shell string"),
    ({"stages": [_stage("merge", commands=[["sh", "-c", "gh pr merge"]])]}, "runs shell strings"),
    ({"stages": [_stage("merge", commands=[["bash", "x"]])]}, "runs shell strings"),
    ({"stages": [_stage("production")]}, "not built yet"),
    ({"stages": [_stage("staging")]}, "not built yet"),
    ({"stages": [DEV, MERGE]}, "merge stage comes before"),
    ({"stages": [MERGE, MERGE]}, "twice"),
    ({"stages": [_stage("merge", commands=[["gh", "{nope}"]])]}, "not a placeholder"),
    ({"stages": [_stage("merge", commands=[["gh", "{branch"]])]}, "brace"),
    ({"stages": [_stage("dev", commands=[["make", "{branch}"]])]}, "only filled in for a stage that runs per child"),
    ({"stages": [_stage("dev", per="child")]}, "once per epic"),
    ({"stages": [_stage("merge", commands=[["{branch}", "x"]])]}, "placeholder"),
    ({"stages": [_stage("merge", timeout=1801)]}, "timeout"),
    ({"stages": [_stage("merge", timeout=True)]}, "timeout"),
    ({"stages": [{"name": "merge", "commands": [["gh"]]}]}, "proven only by its check"),
    ({"stages": [_stage("merge")], "base": "-main"}, "base"),
    ({"stages": [_stage("merge")], "base": "a..b"}, "base"),
    ({"stages": [_stage("merge")], "extra": 1}, "nothing else"),
    ({"stages": []}, "1 to"),
    ({"stages": [_stage("merge", commands=[["gh"]] * 11)]}, "1 to 10"),
    ({"stages": [_stage("merge", commands=[["gh", "x\ny"]])]}, "printable"),
    ({"stages": [_stage("merge", check={"argv": ["gh"], "expect": "x" * 1025})]}, "expect"),
    ({"stages": [_stage("merge")], "sensitive_paths": "x"}, "sensitive_paths"),
])
def test_recipe_refusals(ws, data, why):
    with pytest.raises(ValidationError) as e:
        fr.check_recipe(data, ws)
    assert why in str(e.value)


def test_a_program_must_resolve_at_a_trusted_path_outside_the_workspace(ws, monkeypatch):
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: None)
    with pytest.raises(ValidationError, match="trusted path"):
        fr.check_recipe({"stages": [_stage("merge")]}, ws)
    inside = ws.root / "scripts" / "deploy"
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: str(inside))
    with pytest.raises(ValidationError, match="inside the workspace"):
        fr.check_recipe({"stages": [_stage("merge")]}, ws)


def test_recipe_is_stored_per_workspace_by_the_human_only(ws, human, agent):
    with pytest.raises(HumanOnlyError):
        fr.set_recipe(ws, agent, RECIPE)
    assert fr.recipe(ws) is None and "no release recipe" in fr.load(ws)[1]
    rec = fr.set_recipe(ws, human, RECIPE)
    assert fr.recipe(ws) == rec and rec["stages"][0]["per"] == "child" and rec["stages"][1]["per"] == "epic"
    data = json.loads(fr.path().read_text())
    assert list(data["workspaces"]) == [ledger.workspace_id(ws)] and oct(fr.path().stat().st_mode)[-3:] == "600"
    with pytest.raises(HumanOnlyError):
        fr.clear_recipe(ws, agent)
    assert fr.clear_recipe(ws, human) is True and fr.recipe(ws) is None


def test_a_damaged_or_foreign_writable_recipe_file_is_no_recipe(ws, human):
    fr.set_recipe(ws, human, RECIPE)
    os.chmod(fr.path(), 0o666)
    assert fr.recipe(ws) is None and "not yours alone" in fr.load(ws)[1]
    os.chmod(fr.path(), 0o600)
    fr.path().write_text("{nope", encoding="utf-8")
    assert fr.recipe(ws) is None
    with pytest.raises(ValidationError, match="damaged"):
        fr.set_recipe(ws, human, RECIPE)


def test_up_to_needs_every_stage_before_the_target():
    rec = {"stages": [{"name": "merge"}]}
    assert fr.up_to(rec, "merge") == [{"name": "merge"}] and fr.up_to(rec, "dev") is None
    assert fr.up_to({"stages": [{"name": "dev"}]}, "dev") is None and fr.up_to(rec, "production") is None


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


def test_cli_set_show_clear_are_human_only_and_confirmed(ws, capsys, switch, tmp_path):
    f = tmp_path / "recipe.json"
    f.write_text(json.dumps(RECIPE), encoding="utf-8")
    switch.agent()
    for args in (["set", "--file", str(f)], ["show"], ["clear"]):
        assert cli_run(["factory", "release", *args]) != 0
    assert fr.recipe(ws) is None
    switch.human("nope")
    assert cli_run(["factory", "release", "set", "--file", str(f)]) != 0 and fr.recipe(ws) is None
    switch.human("RELEASE")
    capsys.readouterr()
    assert cli_run(["factory", "release", "set", "--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "gh" in out and "--match-head-commit" in out and "production" in out and fr.recipe(ws) is not None
    assert cli_run(["factory", "release", "show"]) == 0 and "deploy-dev" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"stages": [_stage("production")]}), encoding="utf-8")
    assert cli_run(["factory", "release", "set", "--file", str(bad)]) != 0
    assert "not built yet" in capsys.readouterr().err
    switch.human("CLEAR")
    assert cli_run(["factory", "release", "clear"]) == 0 and fr.recipe(ws) is None


# -- guard and never-grantable ------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat {base}/permits/factory-release.json", "echo x > $ORCH_STATE_DIR/permits/factory-release.json",
    "rm ~/.config/orch/permits/factory-release.json", "cd {base}/permits && cat factory-release.json",
    "ls {base}/permits/release-records", "orch factory release set --file r.json", "orch factory release clear",
    "orch factory release retry L-0001 --stage merge", "uv run orch factory release show",
    "python3 -c 'from orch.core import factory_release'",
    "python3 -c \"from orch.cli import app; app(['factory', 'release', 'clear'])\"",
])
def test_guard_keeps_agents_from_the_recipe_and_its_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=ledger.base_dir())},
                      "cwd": str(ws.root)})
    assert not d.allow, cmd
    assert permits.never_grantable(ws, cmd.format(base=ledger.base_dir())) is not None


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
    with pytest.raises(UsageError, match="production is not built"):
        epics.normalize_delegate({**base, "release": "production"})


def test_existing_charter_hashes_are_unchanged(ws, aops):
    e = aops.new("E", type="epic")
    epic = store.load(ws, e.id)[1]
    for delegate in ({"factory": True}, {"factory": True, "dark": True}, {}):
        assert epics.charter(ws, epic, delegate)["hash"] == epics.charter(ws, epic, {**delegate, "release": None})[
            "hash"]
    dark = {"factory": True, "dark": True}
    assert epics.charter(ws, epic, dark)["hash"] != epics.charter(ws, epic, {**dark, "release": "merge"})["hash"]


def test_approve_release_needs_dark_and_a_recipe_with_the_stages(fws, fa, fh, human):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    with pytest.raises(ValidationError, match="no release recipe"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "merge"})
    fr.set_recipe(fws, human, {"stages": [MERGE]})
    with pytest.raises(ValidationError, match="up to dev"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "dev"})
    with pytest.raises(UsageError):
        fh.approve(e.id, "requirements", delegate={"factory": True, "release": "merge"})
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "merge"})
    assert epics.delegation(fws, store.load(fws, e.id)[1])["release"] == "merge"


def test_cli_approve_release_shows_what_runs(fws, fa, capsys, switch, human):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    switch.human(e.id)
    assert cli_run(["approve", e.id, "requirements", "--factory", "--release", "dev"]) != 0
    assert "--release goes with --dark" in capsys.readouterr().err
    fr.set_recipe(fws, human, RECIPE)
    assert cli_run(["approve", e.id, "requirements", "--dark", "--release", "dev"]) == 0
    out = capsys.readouterr().out
    assert "releases up to dev by itself using the recipe on this machine" in out
    assert "nothing releases to production" in out and "verdict is yours" in out


# -- the executor -------------------------------------------------------------------------------------------------

def test_happy_path_merges_each_child_then_deploys_dev(fws, fa, fh, human, close_tasks):
    eid, (c1, c2), _ = _ready_epic(fws, fa, fh, human, close_tasks, kids=2)
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: merge of {c1} proven", f"{eid}: merge of {c2} proven", f"{eid}: dev of {eid} proven"]
    b1 = f"feat/{c1.lower()}-work"
    assert fake.ran()[:2] == [f"gh pr merge {b1} --match-head-commit {SHA}", f"gh pr view {b1} --json state -q .state"]
    assert fake.ran()[-2:] == ["make deploy-dev", "make dev-health"]
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"} and _stopped(fws, eid) == []
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 6  # nothing runs twice
    ev = [e for e in read_events(fws) if e.kind == "release.stage"]
    assert [e.data for e in ev][-1] == {"stage": "dev", "child": eid, "proven": True, "exit": 0}


def test_git_diff_classification_runs_by_argv_against_the_base(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fr.tick(fws, human, fake)
    gits = [a for a, *_ in fake.calls if a[0].endswith("/git")]
    assert gits[0][-3:] == ["--verify", "--quiet", f"refs/heads/feat/{c.lower()}-work^{{commit}}"]
    assert gits[1][-1] == f"main...{SHA}" and "--no-renames" in gits[1] and "-z" in gits[1]
    assert all(isinstance(a, list) for a, *_ in fake.calls)  # never a shell string


def test_env_is_scrubbed_and_cwd_is_the_workspace(fws, fa, fh, human, close_tasks, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("ORCH_DASHBOARD_TOKEN", "x")
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fr.tick(fws, human, fake)
    for argv, cwd, env, timeout in fake.calls:
        assert cwd == str(fws.root.resolve()) and set(env) <= set(factory_runner.ENV_ALLOW) | {"PATH"}
        assert "GH_TOKEN" not in env and env["PATH"].startswith("/opt/test")


def test_a_failing_command_stops_the_release_and_nothing_after_it_runs(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": 1, "out": "boom <script>\n"}
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: merge of {c} failed (exit code 1)"]
    assert fake.ran() == [f"gh pr merge feat/{c.lower()}-work --match-head-commit {SHA}"]
    assert _states(fws, eid) == {"merge": "failed", "dev": "waiting"} and _stopped(fws, eid) == ["release-failed"]
    reason = factory_report.stopped(fws, store.load(fws, eid)[1])[0]
    assert "merge stage" in reason["text"] and "exit code 1" in reason["text"]
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 1  # at most one automatic attempt


def test_a_failing_check_or_wrong_output_proves_nothing(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr view"] = {"code": 0, "out": "OPEN\n"}
    assert "check's output is not what the recipe expects" in fr.tick(fws, human, fake)[0]
    assert _states(fws, eid)["merge"] == "failed"


def test_check_exit_code_decides(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks, release="merge")
    fake = Fake()
    fake.results["pr view"] = {"code": 3, "out": "MERGED\n"}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "failed"}


def test_a_timeout_fails_the_stage(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": None, "timed_out": True}
    assert "timed out after 60 seconds" in fr.tick(fws, human, fake)[0]
    assert _states(fws, eid)["merge"] == "failed" and fake.calls[-1][3] == 60


def test_a_crash_between_intent_and_outcome_is_unknown_and_never_rerun(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()

    def crash(argv):
        if "merge" in argv and argv[1] == "pr":
            raise KeyboardInterrupt  # the runner dies while the command runs
    fake.on_call = crash
    with pytest.raises(KeyboardInterrupt):
        fr.tick(fws, human, fake)
    fr.release_lock(fws)  # the lock of a dead process expires; here it is let go by hand
    fake.on_call = None
    assert _states(fws, eid)["merge"] == "unknown" and _stopped(fws, eid) == ["release-unknown"]
    assert fr.tick(fws, human, fake) == []
    assert fake.ran() == [f"gh pr merge feat/{c.lower()}-work --match-head-commit {SHA}"]


def test_a_lock_of_a_dead_holder_holds_nothing(fws, fa, fh, human, close_tasks, monkeypatch):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    assert fr.acquire(fws, eid, 60)
    assert fr.lock_holder(fws)["epic"] == eid
    monkeypatch.setattr(fs, "proc_start", lambda pid: "another start")
    assert fr.lock_holder(fws) is None and fr.acquire(fws, eid, 60)


def test_restart_reruns_nothing_proven(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["deploy-dev"] = {"code": 2}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "failed"}
    fr.retry(fws, human, eid, "dev", eid)
    fake.results.clear()
    n = len(fake.ran())
    assert fr.tick(fws, human, fake) == [f"{eid}: dev of {eid} proven"]
    assert fake.ran()[n:] == ["make deploy-dev", "make dev-health"]  # the merge was not run again


def test_retry_is_human_only_and_allows_exactly_one_more_attempt(fws, fa, fh, human, agent, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": 1}
    fr.tick(fws, human, fake)
    with pytest.raises(HumanOnlyError):
        fr.retry(fws, agent, eid, "merge", c)
    with pytest.raises(ValidationError, match="only a failed or unknown"):
        fr.retry(fws, human, eid, "dev", eid)
    fr.retry(fws, human, eid, "merge", c)
    with pytest.raises(ValidationError):
        fr.retry(fws, human, eid, "merge", c)  # one retry per failure
    assert _states(fws, eid)["merge"] == "waiting" and _stopped(fws, eid) == []
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["merge"] == "failed" and fake.ran().count(
        f"gh pr merge feat/{c.lower()}-work --match-head-commit {SHA}") == 2
    fr.tick(fws, human, fake)
    assert len(fake.ran()) == 2


def test_retry_after_unknown_closes_the_open_attempt(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    d = fr._dir(fws, eid)
    fs._create(d / fr._name("merge", c, 1, "intent"), {"stage": "merge"})
    assert _states(fws, eid)["merge"] == "unknown"
    fr.retry(fws, human, eid, "merge", c)
    fake = Fake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["merge"] == "proven"


def test_a_sensitive_path_stops_before_anything_is_merged(fws, fa, fh, human, close_tasks):
    eid, (c1, c2), _ = _ready_epic(fws, fa, fh, human, close_tasks, kids=2)
    fake = Fake(paths=["src/a.py", ".github/workflows/ci.yml", "poetry.lock"])
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: release stopped: a sensitive path is touched; nothing was merged"]
    assert fake.ran() == [] and _stopped(fws, eid) == ["sensitive"]
    text = factory_report.stopped(fws, store.load(fws, eid)[1])[0]["text"]
    assert ".github/workflows/ci.yml" in text and "poetry.lock" in text and "nothing was merged" in text
    fake.paths = ["src/a.py"]
    assert fr.tick(fws, human, fake) == []  # stays stopped until the human retries
    fr.retry(fws, human, eid, "merge", eid)
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"}


def test_a_renamed_sensitive_file_is_seen(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake(paths=[".github/CODEOWNERS", "docs/CODEOWNERS"])  # --no-renames lists both sides
    fr.tick(fws, human, fake)
    assert _stopped(fws, eid) == ["sensitive"]


def test_sensitive_paths_are_escaped_in_the_reason(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fr.tick(fws, human, Fake(paths=[".github/‮x\nb"]))
    text = factory_report.stopped(fws, store.load(fws, eid)[1])[0]["text"]
    assert "‮" not in text and "\n" not in text and "\\u202e" in text


def _set_branch(fws, c, branch):
    """What an agent can do: write any branch name into its ticket file."""
    path = store.resolve(fws, c).path
    text = path.read_text(encoding="utf-8").replace(f"feat/{c.lower()}-work", json.dumps(branch)[1:-1])
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("branch", ["feat/X;rm -rf ~", "--upload-pack=evil-X", "feat/$(id)-X", "main", "other-work",
                                    "feat/../X", "X --exec=sh"])
def test_branch_names_are_validated_before_they_reach_a_command(fws, fa, fh, human, close_tasks, branch):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    _set_branch(fws, c, branch.replace("X", c.lower()))
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert fake.ran() == [] and "not started" in lines[0] and _stopped(fws, eid) == ["release-failed"]
    for argv, *_ in fake.calls:
        assert not any(";" in a or "$(" in a or "--upload" in a or "--exec" in a for a in argv)


def test_placeholders_fill_whole_validated_values_only():
    ctx = fr._context("L-0001", "0123456789abcdef", "L-0002", "feat/l-0002", SHA)
    assert fr.expand(["x-{child}", "{branch}", "{sha}"], ctx) == ["x-L-0002", "feat/l-0002", SHA]
    for bad in (("L-1; rm", "0123456789abcdef"), ("L-0001", "nothex")):
        with pytest.raises(ValidationError):
            fr._context(*bad)
    with pytest.raises(ValidationError):
        fr._context("L-0001", "0123456789abcdef", "L-0002", "-x", SHA)
    with pytest.raises(ValidationError):
        fr._context("L-0001", "0123456789abcdef", "L-0002", "feat/l-0002", "abc")
    with pytest.raises(ValidationError):
        fr.expand(["{branch}"], fr._context("L-0001", "0123456789abcdef"))


def test_a_branch_that_moved_since_it_was_checked_is_not_merged(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    seen = []

    def move(argv):
        if "rev-parse" in argv:
            seen.append(1)
            fake.sha = SHA if len(seen) == 1 else SHA2
    fake.on_call = move
    assert "moved since it was checked" in fr.tick(fws, human, fake)[0]
    assert fake.ran() == []


@pytest.mark.parametrize("block", ["request", "pause", "edit", "off", "dark-off", "cut", "unarmed", "budget"])
def test_the_release_does_not_start_when_blocked(fws, fa, fh, human, close_tasks, block, monkeypatch, configure):
    eid, (c,), d = _ready_epic(fws, fa, fh, human, close_tasks, arm=block != "unarmed")
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


def test_a_pause_between_commands_aborts_before_the_next_one(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()

    def pause(argv):
        if "merge" in argv and argv[1] == "pr":
            fh.epic_pause(eid)  # the human stops the run while the merge command runs
    fake.on_call = pause
    lines = fr.tick(fws, human, fake)
    assert "stopped before the next command" in lines[0]
    assert fake.ran() == [f"gh pr merge feat/{c.lower()}-work --match-head-commit {SHA}"]  # the check never ran


def test_the_lock_keeps_two_epics_from_releasing_at_once(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    assert fr.acquire(fws, "L-9999", 60)
    fake = Fake()
    assert fr.tick(fws, human, fake) == [f"{eid}: the release waits: another release holds the lock"]
    assert fake.calls == []
    assert not fr.acquire(fws, eid, 60)
    fr.release_lock(fws)
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["dev"] == "proven" and fr.lock_holder(fws) is None


def test_output_is_kept_escaped_in_the_record_and_never_in_events(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fake = Fake()
    fake.results["pr merge"] = {"code": 1, "out": "TOKEN=hunter2 ‮" + "x" * 9000}
    fr.tick(fws, human, fake)
    unit = _status(fws, eid)["stages"][0]["units"][0]
    assert len(unit["tail"]) <= fr.TAIL and "‮" not in unit["tail"]
    raw = (fws.home / ".state").glob("**/*")
    for p in raw:
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


def test_never_runs_without_a_release_target_or_for_a_non_dark_epic(fws, fa, fh, human, close_tasks):
    fr.set_recipe(fws, human, RECIPE)
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


def test_a_cleared_recipe_releases_nothing(fws, fa, fh, human, close_tasks):
    eid, _, _ = _ready_epic(fws, fa, fh, human, close_tasks)
    fr.clear_recipe(fws, human)
    fake = Fake()
    assert "no release" in fr.tick(fws, human, fake)[0] and fake.calls == []


def test_a_child_without_a_branch_fails_the_merge_without_running_it(fws, fa, fh, human, close_tasks):
    eid, (c,), _ = _ready_epic(fws, fa, fh, human, close_tasks)
    _set_branch(fws, c, "feat/unrelated")
    fake = Fake()
    assert f"does not name {c}" in fr.tick(fws, human, fake)[0]
    assert fake.ran() == [] and _stopped(fws, eid) == ["release-failed"]


def test_run_command_kills_on_timeout_and_keeps_only_the_tail(tmp_path):
    import sys
    r = fr.run_command([sys.executable, "-c", "print('x' * 10000)"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, 30)
    assert r["code"] == 0 and r["out_size"] > fr.TAIL and len(r["out"]) <= fr.TAIL
    r = fr.run_command([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), {"PATH": "/usr/bin"}, 1)
    assert r["timed_out"] and r["code"] is None
