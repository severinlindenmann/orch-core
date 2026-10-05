"""Dark AI Factory (phase 5, core): the signed Dark switch, the `--dark` charter flag, the signed Dark profile and the
permission hook answering from it."""
import hashlib
import json

import pytest

from orch import actor as orch_actor
from orch.cli import run
from orch.core import dark_profile, epics, ledger, permits, store
from orch.core.canonical import canonical_json
from orch.errors import HumanOnlyError, NotFoundError, UsageError, ValidationError
from test_factory import _behavior, _payload, _refine, bind

FACTORY = {"enabled": True}


def _switch_on(ws, actor):
    from orch.core.ops import Ops
    Ops(ws, actor).set_factory_dark(True)


@pytest.fixture
def dws(configure, human):
    ws = configure(factory=FACTORY)
    _switch_on(ws, human)
    return ws


@pytest.fixture
def da(dws, agent):
    from orch.core.ops import Ops
    return Ops(dws, agent)


@pytest.fixture
def dh(dws, human):
    from conftest import human_ops
    return human_ops(dws, human)


def _epic(da, dh, **delegate):
    e = da.new("Billing revamp", type="epic")
    _refine(da, e.id, plan=None)
    dh.approve(e.id, "requirements", delegate={"factory": True, **delegate})
    c = da.new("child", epic=e.id)
    _refine(da, c.id)
    da.epic_auto_approve(c.id)
    da.claim(c.id)
    bind(dh.ws, dh.actor, e.id, c.id)
    return e.id, c.id


def _dark(da, dh):
    return _epic(da, dh, dark=True)


def _msg(out):
    return out["hookSpecificOutput"]["decision"].get("message", "")


def _forge(ws, human, kind, value, *, op="add", rid=None, checkout=None):
    """An entry written straight into the ledger, past every check of `add`."""
    from orch.actor import process_evidence
    return ledger.record(ws, ticket=None, kind="dark_profile", actor=human, evidence=process_evidence(), op=op,
                         rule_id=rid or dark_profile.rule_id(kind, value), rule_kind=kind, rule=value,
                         checkout=checkout or ledger.checkout_id(ws))


def _second_root(tmp_path, **over):
    """A second checkout of a workspace with the same customer and id prefix (so the same workspace id)."""
    from conftest import make_config
    from orch.core.workspace import Workspace
    root = tmp_path / "second checkout"
    (root / "orchestrator").mkdir(parents=True)
    (root / "orchestrator" / "config.json").write_text(json.dumps(make_config(**over)), encoding="utf-8")
    return Workspace.open(root)


# -- the switch: signed, never a config value ---------------------------------------------------------------------------

def test_dark_is_off_by_default(ws, configure):
    from orch.config.load import DEFAULTS
    assert not permits.dark_on(ws)
    assert DEFAULTS["factory"] == {"enabled": False}
    assert not permits.dark_on(configure(factory=FACTORY))  # factory on alone: Dark stays off


def test_schema_has_no_dark_key_and_checks_max_concurrency(dws):
    from orch.config.load import DEFAULTS, deep_merge, validate_schema
    from orch.core.check import run_checks
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "factory": {"enabled": True,
                                                                                "max_concurrency": 2}})) == []
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "factory": {"dark": True}}))
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "factory": {"max_concurrency": -1}}))
    assert not [f for f in run_checks(dws, emit_events=False) if f.code == "config"]


def test_an_agent_config_edit_cannot_turn_dark_on(configure):
    from orch.config.load import validate_schema
    from orch.hooks.guard import evaluate
    ws = configure(factory={"enabled": True, "dark": True})  # what an agent's Edit/Write of config.json could do
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {
        "file_path": str(ws.home / "config.json"), "content": json.dumps(ws.config)}}).allow  # the guard lets it
    assert not permits.dark_on(ws)  # but nothing in the config turns Dark on
    assert validate_schema(ws.config)  # and `orch check` reports the unknown key
    ws.config["factory"]["dark"] = True
    assert not permits.dark_on(ws)


def test_dark_on_is_human_only_and_anyone_may_turn_it_off(configure, human, agent, monkeypatch):
    from orch.core.ops import Ops
    ws = configure(factory=FACTORY)
    with pytest.raises(HumanOnlyError):
        Ops(ws, agent).set_factory_dark(True)
    assert not permits.dark_on(ws)
    with monkeypatch.context() as m, pytest.raises(HumanOnlyError, match="agent harness"):
        m.setattr(orch_actor, "agent_harness", lambda: "claude-code")
        Ops(ws, human).set_factory_dark(True)
    Ops(ws, human).set_factory_dark(True)
    assert permits.dark_on(ws)
    assert not permits.dark_on(configure(factory={"enabled": False}))  # needs factory.enabled too
    ws = configure(factory=FACTORY)
    Ops(ws, agent).set_factory_dark(False)  # off takes power away: anyone
    assert not permits.dark_on(ws)


def test_dark_switch_is_bound_to_the_checkout(dws, tmp_path):
    other = _second_root(tmp_path, factory=FACTORY)
    assert ledger.workspace_id(other) == ledger.workspace_id(dws)
    assert permits.dark_on(dws) and not permits.dark_on(other)


# -- the charter ----------------------------------------------------------------------------------------------------------

def test_old_charter_hash_is_byte_identical(dws, da):
    e = da.new("E", type="epic")
    _refine(da, e.id, plan=None)
    epic = store.load(dws, e.id)[1]
    old = {"max_children": 25, "max_size": "m", "factory": True, "max_hours": 72}  # as phase 1 signed it
    body = {"epic": epic.id, "epic_hash": epics.gate_hash(epic, "requirements"), "hash_v": epics.HASH_VERSION,
            "children": [], "delegate": old}
    want = "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()
    assert epics.charter(dws, epic, {"factory": True})["hash"] == want
    assert epics.charter(dws, epic, {"factory": True, "dark": False})["hash"] == want
    dark = epics.charter(dws, epic, {"factory": True, "dark": True})
    assert dark["hash"] != want and dark["content_hash"] == epics.charter(dws, epic, {"factory": True})["content_hash"]
    assert epics.normalize_delegate({"factory": True, "dark": True})["dark"] is True


def test_dark_requires_factory():
    with pytest.raises(UsageError, match="--factory"):
        epics.normalize_delegate({"dark": True})


def test_dark_charter_is_signed(dws, da, dh):
    eid, _ = _dark(da, dh)
    d = epics.delegation(dws, store.load(dws, eid)[1])
    assert d["dark"] is True and d["factory"] is True
    assert permits.dark_delegation(dws, store.load(dws, eid)[1])["id"] == d["id"]


def test_dark_start_refused_with_the_switch_off(configure, agent, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    ws = configure(factory=FACTORY)
    e = Ops(ws, agent).new("E", type="epic")
    _refine(Ops(ws, agent), e.id, plan=None)
    with pytest.raises(UsageError, match="Dark AI Factory is switched off"):
        human_ops(ws, human).approve(e.id, "requirements", delegate={"factory": True, "dark": True})


def test_dark_start_is_human_only(dws, da, human, monkeypatch):
    from conftest import human_ops
    e = da.new("E", type="epic")
    _refine(da, e.id, plan=None)
    with pytest.raises(HumanOnlyError):
        da.approve(e.id, "requirements", expected_hash="sha256:x", delegate={"factory": True, "dark": True})
    monkeypatch.setattr(orch_actor, "agent_harness", lambda: "claude-code")
    with pytest.raises(HumanOnlyError, match="agent harness"):
        human_ops(dws, human).approve(e.id, "requirements", delegate={"factory": True, "dark": True})


# -- the CLI ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def switch(monkeypatch):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(orch_actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(orch_actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


def _run(capsys, *args):
    code = run(list(args))
    return code, capsys.readouterr()


def _ok(capsys, *args):
    code, out = _run(capsys, *args)
    assert code == 0, (args, out)
    return out.out


def _cli_epic(capsys):
    eid = json.loads(_ok(capsys, "new", "-t", "Billing", "--type", "epic", "--json"))["id"]
    _ok(capsys, "section", "set", eid, "Requirements", "-m", "r")
    _ok(capsys, "section", "set", eid, "Acceptance criteria", "-m", "- [ ] a")
    return eid


def test_cli_factory_dark_switch(capsys, switch, configure):
    ws = configure(factory=FACTORY)
    code, _ = _run(capsys, "factory", "dark", "on")
    assert code != 0 and not permits.dark_on(ws)  # an agent never turns it on
    switch.human("wrong")
    code, _ = _run(capsys, "factory", "dark", "on")
    assert code != 0 and not permits.dark_on(ws)
    switch.human("DARK")
    _ok(capsys, "factory", "dark", "on")
    switch.agent()
    assert json.loads(_ok(capsys, "factory", "dark", "status", "--json"))["dark"] is True  # anyone reads it
    assert json.loads(_ok(capsys, "factory", "dark", "off", "--json"))["dark"] is False  # anyone turns it off
    assert not permits.dark_on(ws)


def test_cli_dark_start(capsys, switch, configure, human):
    ws = configure(factory=FACTORY)
    eid = _cli_epic(capsys)
    switch.human(eid)
    code, out = _run(capsys, "approve", eid, "requirements", "--dark")
    assert code != 0 and "Dark AI Factory is switched off" in out.err
    _switch_on(ws, human)
    switch.agent()
    code, out = _run(capsys, "approve", eid, "requirements", "--dark")
    assert code != 0 and "agent harness" in out.err
    switch.human(eid)
    out = _ok(capsys, "approve", eid, "requirements", "--dark")  # --dark implies --factory
    assert "AI Factory: on" in out and "Dark: on" in out and "without asking" in out
    assert "Dark AI Factory active" in out
    assert epics.latest_charter(ws, eid)["delegate"]["dark"] is True


def test_cli_profile_add_list_remove(capsys, dws, switch):
    code, _ = _run(capsys, "dark", "profile", "add", "--prefix", "npm run verify")
    assert code != 0  # an agent never adds
    rid = dark_profile.rule_id("prefix", ["npm", "run", "verify"])
    switch.human(rid)
    assert "added to the Dark profile" in _ok(capsys, "dark", "profile", "add", "--prefix", "npm run verify")
    switch.agent()
    listed = json.loads(_ok(capsys, "dark", "profile", "list", "--json"))  # anyone reads it
    assert [r["id"] for r in listed["rules"]] == [rid] and listed["dark"] is True
    code, _ = _run(capsys, "dark", "profile", "remove", rid)
    assert code != 0
    switch.human("wrong")
    code, _ = _run(capsys, "dark", "profile", "remove", rid)
    assert code != 0 and dark_profile.rules(dws)
    switch.human(rid)
    _ok(capsys, "dark", "profile", "remove", rid)
    assert dark_profile.rules(dws) == []
    code, out = _run(capsys, "dark", "profile", "add", "--prefix", "bash -c")
    assert code != 0 and "list the exact commands" in out.err


# -- add and remove: the human's -------------------------------------------------------------------------------------

def test_add_and_remove_are_human_only(dws, human, agent, monkeypatch):
    with pytest.raises(HumanOnlyError):
        dark_profile.add(dws, agent, "prefix", "npm run verify")
    e = dark_profile.add(dws, human, "prefix", "npm run verify")
    assert e["kind"] == "dark_profile" and e["op"] == "add" and e in ledger.entries(dws)
    assert e["checkout"] == ledger.checkout_id(dws)
    with pytest.raises(HumanOnlyError):
        dark_profile.remove(dws, agent, e["rule_id"])
    with monkeypatch.context() as m, pytest.raises(HumanOnlyError, match="agent harness"):
        m.setattr(orch_actor, "agent_harness", lambda: "claude-code")
        dark_profile.remove(dws, human, e["rule_id"])
    with pytest.raises(ValidationError, match="already"):
        dark_profile.add(dws, human, "prefix", ["npm", "run", "verify"])
    dark_profile.remove(dws, human, e["rule_id"])
    assert dark_profile.rules(dws) == []
    with pytest.raises(NotFoundError):
        dark_profile.remove(dws, human, e["rule_id"])


@pytest.mark.parametrize("kind,value,why", [
    ("prefix", "", "empty"), ("exact", "   ", "empty"), ("prefix", [], "empty"),
    ("prefix", "npm", "two words"),
    ("prefix", "bash scripts/x.sh", "exact commands"), ("prefix", "/bin/sh x", "exact commands"),
    ("prefix", "env FOO=1", "exact commands"), ("prefix", "python3 -m pytest", "exact commands"),
    ("prefix", "xargs -n1", "exact commands"), ("prefix", "curl https://x", "exact commands"),
    ("prefix", "docker run", "exact commands"), ("prefix", "find .", "exact commands"),
    ("prefix", "sed -i", "exact commands"), ("prefix", "sudo ls", "exact commands"),
    ("prefix", "git push", "never a prefix"), ("prefix", "git push origin", "never a prefix"),
    ("prefix", "git reset --hard", "never a prefix"), ("prefix", "git clean -fdx", "never a prefix"),
    ("prefix", "rm -rf", "never a prefix"), ("prefix", "mv a", "never a prefix"),
    ("prefix", "/usr/bin/git push", "never a prefix"), ("prefix", "git -C .", "subcommand first"),
    ("prefix", "npm run verify; rm", "plain words"), ("prefix", "npm run $(x)", "plain words"),
    ("prefix", ["npm", "run;"], "plain words"), ("prefix", ["npm", "ré"], "plain words"),
    ("exact", "make x\nrm -rf ~", "printable"), ("exact", "make x\x1b", "printable"),
    ("exact", "orch permit grant P-1", "never be in the Dark profile"),
    ("exact", "git push --force", "never be in the Dark profile"),
    ("prefix", "gh pr merge", "never be in the Dark profile"),
    ("exact", "cat ~/.claude/settings.json", "never be in the Dark profile"),
    ("bogus", "x", "one of"),
    # the program word: no assignment, no option, casefolded, versioned interpreters, wrappers and editors
    ("prefix", ["FOO=1", "npm", "test"], "plain program"), ("prefix", ["-x", "y"], "plain program"),
    ("prefix", "GIT push", "never a prefix"), ("prefix", "RM -r", "never a prefix"),
    ("prefix", "/usr/bin/Rm x", "never a prefix"), ("prefix", "Python3 x.py", "exact commands"),
    ("prefix", "python3.12 -m", "exact commands"), ("prefix", "pythonw x", "exact commands"),
    ("prefix", "node18 x.js", "exact commands"), ("prefix", "perl5.36 x", "exact commands"),
    ("prefix", "ruby3.2 x", "exact commands"), ("prefix", "php8 x", "exact commands"),
    ("prefix", "nohup make", "exact commands"), ("prefix", "timeout 5", "exact commands"),
    ("prefix", "npx jest", "exact commands"), ("prefix", "uv run", "exact commands"),
    ("prefix", "bunx x", "exact commands"), ("prefix", "nc localhost", "exact commands"),
    ("prefix", "open -a", "exact commands"), ("prefix", "vim x", "exact commands"),
    ("prefix", "xcrun simctl", "exact commands"), ("prefix", "tmux ls", "exact commands"),
    ("prefix", "gh api", "never a prefix"), ("prefix", "gh --repo x", "subcommand first"),
    # program families by name (round 2): shells, interpreters and versions, awk/sed/find, wrappers, debuggers, pagers
    *[("prefix", f"{p} x", "exact commands") for p in (
        "nodejs", "node-18", "gawk", "mawk", "nawk", "gsed", "gfind", "stdbuf", "ionice", "setsid", "flock", "unbuffer",
        "parallel", "expect", "gdb", "lldb", "sqlite3", "less", "man", "pypy3", "ipython3", "irb", "julia", "Rscript",
        "swift", "jshell", "python3.12-intel64", "py", "zsh5", "bash5", "tcsh", "kSh", "BASH.EXE", "python.exe")],
    ("prefix", "git.exe push", "never a prefix"), ("prefix", "rm.exe x", "never a prefix"),
    # subcommands that run, fetch or rewrite
    *[("prefix", s, "never a prefix") for s in (
        "npm x", "pnpm dlx", "pnpm exec", "yarn dlx", "yarn exec", "cargo run", "go run", "gh alias", "gh extension",
        "gh secret", "git grep", "git difftool", "git mergetool", "git filter-branch", "git daemon", "git instaweb",
        "git send-email", "git credential", "git p4", "git svn", "git update-ref", "git replace", "git gc",
        "git branch", "git checkout")],
    ("prefix", "make -f x", "run other code"), ("prefix", "make SHELL=x", "run other code"),
    ("prefix", "make MAKEFLAGS=x", "run other code"), ("prefix", "make test -C x", "run other code"),
    ("prefix", "git fetch", "never a prefix"), ("prefix", "git pull origin", "never a prefix"),
    ("prefix", "git clone x", "never a prefix"), ("prefix", "git rebase -i", "never a prefix"),
    ("prefix", "git config user.name", "never a prefix"), ("prefix", "git worktree add", "never a prefix"),
    ("prefix", "git remote add", "never a prefix"), ("prefix", "git submodule update", "never a prefix"),
    ("prefix", "git ls-remote x", "never a prefix"), ("prefix", "git archive x", "never a prefix"),
    ("prefix", "git bisect run", "never a prefix"),
    ("prefix", "npm --prefix x", "run other code"), ("prefix", "npm exec -c x", "never a prefix"),
    ("prefix", "npm test -c x", "run other code"),
    ("prefix", "npm test --script-shell=x", "run other code"),
])
def test_broad_rules_are_refused(dws, human, kind, value, why):
    with pytest.raises((ValidationError, UsageError), match=why):
        dark_profile.add(dws, human, kind, value)
    assert dark_profile.rules(dws) == []


def test_ordinary_prefix_rules_are_accepted(dws, human):
    for rule in ("npm run verify", "make test", "gh pr view", "git status", "git diff --stat", "./scripts/check.sh x"):
        dark_profile.add(dws, human, "prefix", rule)
    assert len(dark_profile.rules(dws)) == 6


def test_rule_ids_are_stable(dws, human):
    e = dark_profile.add(dws, human, "prefix", "npm  run verify")
    assert e["rule_id"] == dark_profile.rule_id("prefix", ["npm", "run", "verify"])
    assert e["rule_id"] != dark_profile.rule_id("exact", "npm run verify")


# -- the replay: per checkout, each entry checked again --------------------------------------------------------------

def test_profile_is_bound_to_the_checkout(dws, human, tmp_path):
    other = _second_root(tmp_path, factory=FACTORY)
    _switch_on(other, human)
    assert ledger.workspace_id(other) == ledger.workspace_id(dws)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    assert dark_profile.rules(dws) and dark_profile.rules(other) == []
    assert dark_profile.match(other, "npm run verify") is None
    _forge(other, human, "prefix", ["make", "test"], checkout="0" * 16)  # signed for no checkout here
    assert dark_profile.rules(other) == []


@pytest.mark.parametrize("kind,value", [
    ("prefix", []), ("prefix", ["npm"]), ("prefix", ["npm", ""]), ("prefix", ["bash", "-c"]),
    ("prefix", ["npm", "run;"]), ("prefix", ["FOO=1", "npm"]), ("prefix", ["git", "push"]), ("prefix", "npm run"),
    ("prefix", [1, 2]), ("exact", ""), ("exact", "a\nb"), ("exact", 5), ("exact", ["a", "b"]), ("other", "x"),
])
def test_a_signed_entry_of_the_wrong_shape_is_ignored(dws, da, dh, human, kind, value):
    _dark(da, dh)
    _forge(dws, human, kind, value, rid=dark_profile.rule_id(kind, value))
    assert dark_profile.rules(dws) == []
    for command in ("npm run verify", "npm", "bash -c x", "a\nb", "x"):
        assert dark_profile.match(dws, command) is None
    assert _behavior(permits.hook_decision(dws, _payload("npm run verify"))) == "deny"


def test_a_remove_counts_only_for_a_rule_in_force(dws, human):
    rid = dark_profile.rule_id("prefix", ["make", "test"])
    _forge(dws, human, "prefix", ["make", "test"], op="remove")
    _forge(dws, human, "prefix", ["make", "test"])
    assert [r["id"] for r in dark_profile.rules(dws)] == [rid]
    _forge(dws, human, "prefix", ["make", "test"], op="remove", rid="R-unknown")
    assert [r["id"] for r in dark_profile.rules(dws)] == [rid]


# -- matching ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,ok", [
    ("npm run verify", True), ("npm run verify --quiet", True), ("npm run verify 'a b'", True),
    ("npm run verify; rm -rf x", False), ("npm run verify && rm -rf x", False), ("npm run verify > f", False),
    ("npm run verify < f", False), ("npm run verify $(x)", False), ("npm run verify `x`", False),
    ("npm run verify | sh", False), ("npm run verify\nrm -rf x", False), ("npm run verify & x", False),
    ("npm run verify ${HOME}", False), ("npm run verify \\; x", False), ("(npm run verify)", False),
    ("npm run", False), ("npm run verifyx", False), ("FOO=1 npm run verify", False), ("npm run 'verify", False),
    # argument shapes that run other code never match a prefix rule
    ("npm run verify --exec x", False), ("npm run verify --exec=x", False), ("npm run verify -x", False),
    ("npm run verify -c x", False), ("npm run verify -e x", False), ("npm run verify --eval x", False),
    ("npm run verify --upload-pack=x", False), ("npm run verify --receive-pack x", False),
    ("npm run verify --script-shell x", False), ("npm run verify --shell=x", False),
    ("npm run verify --prefix x", False), ("npm run verify --userconfig x", False),
    ("npm run verify --node-options=--require=x", False), ("npm run verify --require x", False),
    ("npm run verify --config x", False),
    # round 2: more shapes, combined short flags, abbreviations and spellings npm accepts
    *[(f"npm run verify {a}", False) for a in (
        "-C /tmp/evil", "-w ../evil", "--workspace ../evil", "--globalconfig=/x", "--open-files-in-pager=x", "-Ox",
        "-O", "--ext-diff", "--textconv", "--output=/x", "-f x", "--file x", "--makefile=x", "SHELL=/x",
        "MAKEFLAGS=x", "-p evil", "--rootdir /tmp", "--confcutdir=/x", "-o addopts=x", "--manifest-path /x",
        "--to-command=x", "--use-compress-program=x", "-I x", "-Ix", "--checkpoint-action=exec=x", "-xc", "-ce x",
        "-vx", "--script-sh=/x", "--scri=/x", "--SCRIPT-SHELL=/x", "--script_shell=/x", "--upload=x", "--exe x")],
    ("npm run verify -v", True), ("npm run verify --quiet --silent", True), ("npm run verify --pretty", True),
    ("npm run verify -1", True), ("npm run verify --ex", True),
])
def test_prefix_matches_only_a_simple_command(dws, human, command, ok):
    dark_profile.add(dws, human, "prefix", "npm run verify")
    assert (dark_profile.match(dws, command) is not None) is ok


def test_refused_argument_shapes_can_still_match_an_exact_rule(dws, human):
    dark_profile.add(dws, human, "exact", "pytest -x tests")
    assert dark_profile.match(dws, "pytest -x tests") is not None
    assert dark_profile.match(dws, "pytest -x tests/other") is None


def test_exact_matches_only_identical_text(dws, human):
    dark_profile.add(dws, human, "exact", "make test > out.txt")
    assert dark_profile.match(dws, "make test > out.txt") is not None
    for other in ("make test > out.txt ", "make test >out.txt", "make test > out.txt; rm x", "make test"):
        assert dark_profile.match(dws, other) is None


# -- the hook ---------------------------------------------------------------------------------------------------------

def test_the_binding_records_the_runners_checkout(dws, da, dh):
    from orch.core import factory_sessions
    from test_factory import SESSION
    _dark(da, dh)
    b = factory_sessions.binding(dws, SESSION)
    assert b["checkout"] == ledger.checkout_id(dws) and b["start"] == str(dws.root)


def test_a_forged_git_file_does_not_move_the_session_to_another_checkout(dws, da, dh, human, tmp_path):
    """The agent rewrites its checkout's `.git` file so checkout_id names another clone's common dir."""
    from orch.core.workspace import Workspace
    _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "make e2e")
    assert _behavior(permits.hook_decision(dws, _payload("make e2e"))) == "allow"
    before = ledger.checkout_id(dws)
    other_git = tmp_path / "other clone" / ".git"
    other_git.mkdir(parents=True)
    fake = tmp_path / "fake gitdir"
    fake.mkdir()
    (fake / "commondir").write_text(str(other_git), encoding="utf-8")
    (dws.root / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")
    ledger._checkouts.clear()
    moved = Workspace.open(dws.root)
    assert ledger.checkout_id(moved) != before  # the forged `.git` file now names the other clone
    out = permits.hook_decision(moved, _payload("make e2e"))
    assert _behavior(out) == "deny" and "not in the checkout the runner started it in" in _msg(out)


def test_a_session_that_moves_into_another_clone_gets_nothing_from_it(dws, da, dh, human, tmp_path):
    """A second clone with the same customer and prefix, its own Dark switch on and a rule the first lacks."""
    import shutil
    _dark(da, dh)
    other = _second_root(tmp_path, factory=FACTORY)
    shutil.copytree(dws.home / "tickets", other.home / "tickets", dirs_exist_ok=True)
    shutil.copytree(dws.home / ".state", other.home / ".state", dirs_exist_ok=True)
    _switch_on(other, human)
    dark_profile.add(other, human, "prefix", "make deploy")
    assert dark_profile.match(other, "make deploy") is not None and dark_profile.match(dws, "make deploy") is None
    out = permits.hook_decision(other, _payload("make deploy"))  # the hook opened from the clone it moved into
    assert _behavior(out) == "deny" and "not in the checkout the runner started it in" in _msg(out)
    out = permits.hook_decision(dws, _payload("make deploy"))  # back home: its own profile, which lacks the rule
    assert _behavior(out) == "deny" and "not in the Dark profile" in _msg(out)


def test_hook_allows_listed_and_parks_unlisted_as_a_dark_card(dws, da, dh, human):
    eid, cid = _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    for _ in range(3):  # a standing rule: nothing is used up
        assert _behavior(permits.hook_decision(dws, _payload("npm run verify --quiet"))) == "allow"
    out = permits.hook_decision(dws, _payload("make deploy"))
    assert _behavior(out) == "deny" and "not in the Dark profile" in _msg(out) and "can add it" in _msg(out)
    (r,) = permits.open_requests(dws)
    assert (r["epic"], r["ticket"], r["command"], r["source"]) == (eid, cid, "make deploy", "dark")
    assert permits.requests(dws)[r["id"]]["source"] == "dark"
    # a compound command is never a prefix match: denied and a card
    assert _behavior(permits.hook_decision(dws, _payload("npm run verify; make deploy"))) == "deny"


@pytest.mark.parametrize("cmd", ["cd /Users/x/ws && orch claim L-0002", "cd /Users/x/ws; orch show L-0002",
                                 "cd '/Users/x/my ws' && /opt/bin/orch wait L-0002 --json"])
def test_a_cd_before_orch_is_denied_with_how_to_run_it_instead(dws, da, dh, human, cmd):
    _dark(da, dh)
    dark_profile.add_baseline(dws, human)
    assert dark_profile.match(dws, cmd) is None  # a chain never matches, whatever the baseline holds
    out = permits.hook_decision(dws, _payload(cmd))
    assert _behavior(out) == "deny" and "run the orch command from your current folder, without cd" in _msg(out)
    assert "Do not retry variants" not in _msg(out)  # the plain command is the fix, not a variant to avoid
    (r,) = permits.open_requests(dws)
    assert "without cd" in permits.requests(dws)[r["id"]]["reason"]  # the card says it too
    plain = "orch " + cmd.split("orch ", 1)[1]
    assert dark_profile.match(dws, plain) is not None  # and that plain command runs without a card
    other = permits.hook_decision(dws, _payload("make deploy"))
    assert "without cd" not in _msg(other)


@pytest.mark.parametrize("tool_input", [{}, {"command": 5}, {"command": None}, {"command": ""}, "npm run verify"])
def test_hook_denies_a_missing_or_odd_command_in_a_dark_epic(dws, da, dh, human, tool_input):
    _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    payload = {**_payload("x"), "tool_input": tool_input}
    assert _behavior(permits.hook_decision(dws, payload)) == "deny"
    assert permits.open_requests(dws) == []


def test_a_live_grant_still_allows_in_a_dark_epic(dws, da, dh, human):
    _dark(da, dh)
    permits.hook_decision(dws, _payload("make e2e"))
    (r,) = permits.open_requests(dws)
    permits.permit_grant(dws, human, r["id"], "once", expected_sha=r["sha"])
    assert _behavior(permits.hook_decision(dws, _payload("make e2e"))) == "allow"
    assert _behavior(permits.hook_decision(dws, _payload("make e2e"))) == "deny"


@pytest.mark.parametrize("kind,value,command", [
    ("exact", "sudo make install", "sudo make install"),
    ("exact", "orch permit grant P-1", "orch permit grant P-1"),
    ("prefix", ["cat", "x"], "cat x ~/.claude/settings.json"),
    ("prefix", ["make", "x"], "make x --no-verify"),
])
def test_never_grantable_is_never_allowed_even_if_listed(dws, da, dh, human, kind, value, command):
    _dark(da, dh)
    _forge(dws, human, kind, value)
    assert dark_profile.rules(dws)
    assert dark_profile.match(dws, command) is None
    out = permits.hook_decision(dws, _payload(command))
    assert _behavior(out) == "deny" and "never granted" in _msg(out)


def test_dark_off_means_the_profile_is_ignored(dws, da, dh, human, agent):
    _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    from orch.core.ops import Ops
    Ops(dws, agent).set_factory_dark(False)  # the brake anyone may pull
    out = permits.hook_decision(dws, _payload("npm run verify"))
    assert _behavior(out) == "deny" and "waiting for permission" in _msg(out)
    (r,) = permits.open_requests(dws)
    assert r["source"] == "harness"
    assert permits.dark_delegation(dws, store.load(dws, r["epic"])[1]) is None


def test_a_non_dark_factory_epic_ignores_the_profile(dws, da, dh, human):
    _epic(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    out = permits.hook_decision(dws, _payload("npm run verify"))
    assert _behavior(out) == "deny" and "waiting for permission" in _msg(out)


def test_another_workspaces_profile_does_not_apply(dws, da, dh, human, configure):
    _dark(da, dh)
    other = configure(customer="other", factory=FACTORY)
    dark_profile.add(other, human, "prefix", "npm run verify")
    assert dark_profile.rules(other)
    mine = configure(factory=FACTORY)
    assert dark_profile.rules(mine) == []
    assert _behavior(permits.hook_decision(mine, _payload("npm run verify"))) == "deny"


def test_a_cut_ledger_disables_the_profile(dws, da, dh, human):
    _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    dark_profile.add(dws, human, "exact", "make lint")
    path = ledger.ledger_path(dws)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")  # the newest entry cut away
    assert not ledger.head_ok()
    assert dark_profile.rules(dws) == [] and not permits.dark_on(dws)
    out = permits.hook_decision(dws, _payload("npm run verify"))
    assert _behavior(out) == "deny" and "cut" in _msg(out)


def test_a_paused_dark_epic_answers_from_grants_only(dws, da, dh, human):
    eid, _ = _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    dh.epic_pause(eid)
    assert permits.dark_delegation(dws, store.load(dws, eid)[1]) is None
    assert _behavior(permits.hook_decision(dws, _payload("npm run verify"))) == "deny"


# -- a Dark request becomes a rule -------------------------------------------------------------------------------------

def test_request_becomes_an_exact_rule(dws, da, dh, human, agent):
    _dark(da, dh)
    permits.hook_decision(dws, _payload("make deploy-staging"))
    (r,) = permits.open_requests(dws)
    with pytest.raises(HumanOnlyError):
        dark_profile.add_from_request(dws, agent, r["id"], expected_sha=r["sha"])
    with pytest.raises(ValidationError, match="not the one you were shown"):
        dark_profile.add_from_request(dws, human, r["id"], expected_sha="sha256:00")
    e = dark_profile.add_from_request(dws, human, r["id"], expected_sha=r["sha"])
    assert (e["rule_kind"], e["rule"]) == ("exact", "make deploy-staging")
    assert permits.open_requests(dws) == []  # the rule hides the card; nothing else is signed
    assert not [x for x in ledger.entries(dws) if x.get("kind") in ("grant", "permit_deny")]
    assert _behavior(permits.hook_decision(dws, _payload("make deploy-staging"))) == "allow"
    assert _behavior(permits.hook_decision(dws, _payload("make deploy-staging --x"))) == "deny"
    dark_profile.remove(dws, human, e["rule_id"])
    assert r["id"] in [x["id"] for x in permits.open_requests(dws)]  # removing the rule brings the card back


@pytest.mark.parametrize("how", ["paused", "switched off"])
def test_request_to_rule_needs_an_active_dark_epic(dws, da, dh, human, agent, how):
    from orch.core.ops import Ops
    eid, _ = _dark(da, dh)
    permits.hook_decision(dws, _payload("make deploy-staging"))
    (r,) = permits.open_requests(dws)
    if how == "paused":
        dh.epic_pause(eid)
    else:
        Ops(dws, agent).set_factory_dark(False)
    with pytest.raises(ValidationError, match="not an active Dark factory epic"):
        dark_profile.add_from_request(dws, human, r["id"], expected_sha=r["sha"])
    assert dark_profile.rules(dws) == []


def test_only_an_open_dark_request_of_this_workspace(dws, da, dh, human, configure):
    _epic(da, dh)  # an ordinary factory epic: its request is a harness card
    permits.hook_decision(dws, _payload("make e2e"))
    (r,) = permits.open_requests(dws)
    with pytest.raises(ValidationError, match="not filed by a Dark factory"):
        dark_profile.add_from_request(dws, human, r["id"], expected_sha=r["sha"])
    other = configure(customer="other", factory=FACTORY)
    with pytest.raises(NotFoundError):
        dark_profile.add_from_request(other, human, r["id"], expected_sha=r["sha"])
    mine = configure(factory=FACTORY)
    permits.permit_deny(mine, human, r["id"], expected_sha=r["sha"])
    with pytest.raises(ValidationError, match="answered already"):
        dark_profile.add_from_request(mine, human, r["id"], expected_sha=r["sha"])
    with pytest.raises(NotFoundError):
        dark_profile.add_from_request(mine, human, "P-00000000", expected_sha="sha256:00")
