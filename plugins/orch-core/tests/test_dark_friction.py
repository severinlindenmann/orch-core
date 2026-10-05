"""Dark AI Factory, the friction round: what a live run with real agent sessions showed (docs/factory.md, "What the
live run showed"). orch's own --file options, the git-basic baseline, requests for commands the profile already
allows, and profile hygiene. No real agent, tmux or git remote."""
import json
import subprocess

import pytest

from orch.core import dark_profile, permits
from orch.hooks.guard import evaluate
from test_dark_profile import _ok, _run, switch  # noqa: F401  (switch is a fixture)
from test_factory_planner import _bound_session

COMMIT = {"agent_may": {"commit": True}}  # the guard refuses agents' commits unless the workspace allows them


@pytest.fixture
def dws(configure, human):
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True}, git=COMMIT)
    Ops(ws, human).set_factory_dark(True)
    return ws


# -- orch's --file options ------------------------------------------------------------------------------------------

def test_orchs_file_options_match_a_prefix_rule_and_other_programs_stay_refused(dws, human):
    dark_profile.add_baseline(dws, human)
    for cmd in ("orch section set L-1 Plan --file orchestrator/temporary/plan.md", "orch state L-1 --file s.md",
                "orch task add L-1 --file orchestrator/temporary/t.yaml", "orch task add L-1 --file=t.yaml",
                "/opt/bin/orch state L-1 --file s.md"):
        words = dark_profile.simple_tokens(cmd)
        assert dark_profile._runs_code(words) is None, cmd
    assert dark_profile.match(dws, "orch task add L-1 --file orchestrator/temporary/t.yaml") is not None
    dark_profile.add(dws, human, "prefix", "npm run verify")
    for cmd in ("npm run verify --file x", "./orch state L-1 --file s.md", "orchx state --file s",
                "orch task add L-1 --fil t.yaml"):  # another program, a relative path, an abbreviation
        assert dark_profile._runs_code(dark_profile.simple_tokens(cmd)) is not None, cmd


def test_a_bound_session_still_cannot_hand_orch_a_file_outside_the_workspace(ws, human, tmp_path, capsys,
                                                                                  monkeypatch):
    from orch.core import store
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir()
    secret.write_text("PRIVATE", encoding="utf-8")
    tid = json.loads(_ok(capsys, "new", "-t", "x", "--json"))["id"]
    _bound_session(ws, human, monkeypatch)
    for args in (("section", "set", tid, "Plan", "--file", str(secret)), ("state", tid, "--file", str(secret)),
                 ("task", "add", tid, "--file", str(secret))):
        assert dark_profile._runs_code(["orch", *args]) is None  # the profile would let it run ...
        code, out = _run(capsys, *args)
        assert code != 0 and "an agent cannot hand orch the file" in out.err, (args, out.err)  # ... orch refuses
    assert "PRIVATE" not in json.dumps(store.load(ws, tid)[1].sections)


# -- the git-basic baseline -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,rule", dark_profile.BASELINES["git-basic"])
def test_every_git_basic_rule_is_real_git_and_passes_the_checks(dws, kind, rule):
    ws = dws
    value = rule.split() if kind == "prefix" else rule
    assert dark_profile.refusal(kind, value) is None
    assert dark_profile.check_rule(ws, kind, rule) == (kind, value)
    assert permits.never_grantable(ws, rule) is None
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": rule}, "cwd": str(ws.root)}).allow
    r = subprocess.run(["git", *rule.split()[1:2], "-h"], capture_output=True, text=True, cwd=ws.root)
    assert r.returncode == 129 and "usage: git" in r.stdout, rule  # a real subcommand prints its usage


def test_git_basic_allows_a_workers_commit_and_nothing_that_rewrites_or_reaches_out(dws, human):
    res = dark_profile.add_baseline(dws, human, name="git-basic")
    assert len(res["added"]) == len(dark_profile.BASELINES["git-basic"]) and res["failed"] == []
    for cmd in ("git status", "git status --short", "git diff --stat", "git log --oneline -5", "git show HEAD",
                "git add src/a.py tests/test_a.py", 'git commit -m "T-3 add the export (CSV)"',
                "git branch --show-current"):
        assert dark_profile.match(dws, cmd) is not None, cmd
    for cmd in ("git push", "git push origin main", "git reset --hard", "git clean -fd", "git -c core.pager=x log",
                "git checkout main", "git switch x", "git rebase main", "git branch -D x", "git branch x",
                "git branch --show-current -D main", "git commit --no-verify -m x", "git diff --output=/tmp/x",
                "git diff --ext-diff", "git log -p", "git fetch", "git config user.name x", "git stash",
                "git add x && git commit -m y", "git commit -m x; git push", "git -C /tmp status"):
        assert dark_profile.match(dws, cmd) is None, cmd


def test_add_baseline_by_name_is_idempotent_and_an_unknown_name_lists_the_names(dws, human):
    from orch.errors import UsageError
    dark_profile.add_baseline(dws, human, name="git-basic")
    n = len(dark_profile.rules(dws))
    assert dark_profile.add_baseline(dws, human, name="git-basic") == {"added": [], "failed": []}
    assert len(dark_profile.rules(dws)) == n and dark_profile.baseline_todo(dws, "orch")  # the other one is separate
    with pytest.raises(UsageError, match="orch, git-basic"):
        dark_profile.baseline_todo(dws, "everything")


def test_git_commit_is_not_added_where_the_workspace_forbids_agents_to_commit(configure, human):
    ws = configure(factory={"enabled": True})
    res = dark_profile.add_baseline(ws, human, name="git-basic")
    assert [f["rule"] for f in res["failed"]] == ["git commit"] and "agents do not commit" in res["failed"][0]["error"]
    assert len(res["added"]) == len(dark_profile.BASELINES["git-basic"]) - 1


def test_cli_baseline_git_basic(capsys, switch, configure):  # noqa: F811
    ws = configure(factory={"enabled": True}, git=COMMIT)
    code, _ = _run(capsys, "dark", "profile", "add", "--baseline", "git-basic")
    assert code != 0 and dark_profile.rules(ws) == []  # an agent never adds
    switch.human("BASELINE")
    code, out = _run(capsys, "dark", "profile", "add", "--baseline", "nope")
    assert code != 0 and "orch, git-basic" in out.err and dark_profile.rules(ws) == []
    code, out = _run(capsys, "dark", "profile", "add", "--prefix", "make test", "git-basic")
    assert code != 0 and dark_profile.rules(ws) == []
    out = _ok(capsys, "dark", "profile", "add", "--baseline", "git-basic")
    assert "added 7 baseline rules" in out and "git branch --show-current" in out
    assert {dark_profile.text(r["kind"], r["rule"]) for r in dark_profile.rules(ws)} == {
        *dark_profile.GIT_BASIC, *dark_profile.GIT_BASIC_EXACT}
    assert "already" in _ok(capsys, "dark", "profile", "add", "--baseline", "git-basic")
    out = _ok(capsys, "dark", "profile", "add", "--baseline")  # the default is still the orch one
    assert f"added {len(dark_profile.BASELINE)} baseline rules" in out


# -- requests for commands the profile already covers ---------------------------------------------------------------

@pytest.fixture
def dark_child(dws, agent, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    from test_dark_profile import _dark
    return _dark(Ops(dws, agent), human_ops(dws, human))


def _session_actor():
    from orch.core.events import Actor
    from test_factory import SESSION
    return Actor("agent", "claude-code", "cli", SESSION)


def test_a_bound_dark_session_asking_for_an_allowed_command_files_nothing(dws, dark_child, human):
    from orch.core import store
    from orch.core.events import read_events
    eid, cid = dark_child
    dark_profile.add_baseline(dws, human)
    t = store.load(dws, cid)[1]
    r = permits.request(dws, _session_actor(), t, f"orch task done {cid} T1-T4")
    assert r["allowed"] and r["rule"] and permits.open_requests(dws) == []
    assert not [e for e in read_events(dws) if e.kind == "permit.requested"]
    r = permits.request(dws, _session_actor(), t, "make deploy")  # not in the profile: a card, as before
    assert r["id"].startswith("P-") and [x["id"] for x in permits.open_requests(dws)] == [r["id"]]


def test_cli_says_just_run_it(dws, dark_child, human, capsys, monkeypatch):
    from test_factory import SESSION
    eid, cid = dark_child
    dark_profile.add_baseline(dws, human)
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    out = _ok(capsys, "permit", "request", f"orch task done {cid} T1", "--ticket", cid)
    assert "already allowed by the Dark profile: just run it" in out and permits.open_requests(dws) == []


def test_a_request_the_profile_covers_later_is_hidden_in_a_dark_epic_only(dws, dark_child, human, agent):
    from orch.core import store
    from test_factory import _refine
    from conftest import human_ops
    from orch.core.ops import Ops
    eid, cid = dark_child
    r = permits.request(dws, agent, store.load(dws, cid)[1], "make deploy")  # an agent without a binding
    a, h = Ops(dws, agent), human_ops(dws, human)
    plain_epic = a.new("Plain", type="epic").id  # an ordinary factory epic
    _refine(a, plain_epic, plan=None)
    h.approve(plain_epic, "requirements", delegate={"factory": True})
    plain_child = a.new("child", epic=plain_epic).id
    p = permits.request(dws, agent, store.load(dws, plain_child)[1], "make deploy")
    assert {x["id"] for x in permits.open_requests(dws)} == {r["id"], p["id"]}
    dark_profile.add(dws, human, "exact", "make deploy")
    assert [x["id"] for x in permits.open_requests(dws)] == [p["id"]]  # the profile answers nothing there
    a.set_factory_dark(False)  # the brake: the Dark epic is an ordinary one again, its card is back
    assert {x["id"] for x in permits.open_requests(dws)} == {r["id"], p["id"]}


def test_the_dark_denial_names_the_request_and_says_not_to_retry(dws, dark_child):
    from test_factory import _payload
    eid, cid = dark_child
    out = permits.hook_decision(dws, _payload("make deploy"))
    msg = out["hookSpecificOutput"]["decision"]["message"]
    (r,) = permits.open_requests(dws)
    for words in ("not in the Dark profile", f"Request {r['id']} is open", "the human can add it",
                  f"orch wait {cid}", "Do not retry variants", "do not file another request"):
        assert words in msg, words
