"""Dark AI Factory (phase 5, core): the `factory.dark` switch, the `--dark` charter flag, the signed Dark profile and
the permission hook answering from it."""
import hashlib
import json

import pytest

from orch import actor as orch_actor
from orch.cli import run
from orch.core import dark_profile, epics, ledger, permits, store
from orch.core.canonical import canonical_json
from orch.errors import HumanOnlyError, NotFoundError, UsageError, ValidationError
from test_factory import _behavior, _payload, _refine, bind

DARK = {"enabled": True, "dark": True}


@pytest.fixture
def dws(configure):
    return configure(factory=DARK)


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


# -- the switch and the charter ------------------------------------------------------------------------------------

def test_dark_is_off_by_default_and_validates(ws):
    from orch.config.load import DEFAULTS, deep_merge, validate_schema
    assert not permits.dark_on(ws)
    assert DEFAULTS["factory"] == {"enabled": False, "dark": False}
    good = deep_merge(DEFAULTS, {"customer": "x", "factory": {"enabled": True, "dark": True, "max_concurrency": 2}})
    assert validate_schema(good) == []
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "factory": {"dark": "yes"}}))
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x", "factory": {"max_concurrency": -1}}))


def test_check_accepts_a_dark_config(dws):
    from orch.config.load import validate_schema
    from orch.core.check import run_checks
    assert dws.config["factory"] == DARK and validate_schema(dws.config) == []
    assert not [f for f in run_checks(dws, emit_events=False) if f.code == "config"]


def test_dark_needs_the_factory_switch_too(configure):
    assert not permits.dark_on(configure(factory={"enabled": False, "dark": True}))
    assert permits.dark_on(configure(factory=DARK))


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
    ws = configure(factory={"enabled": True})
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


def test_cli_dark_start(capsys, switch, configure):
    configure(factory={"enabled": True})
    eid = _cli_epic(capsys)
    switch.human(eid)
    code, out = _run(capsys, "approve", eid, "requirements", "--dark")
    assert code != 0 and "Dark AI Factory is switched off" in out.err
    configure(factory=DARK)
    switch.agent()
    code, out = _run(capsys, "approve", eid, "requirements", "--dark")
    assert code != 0 and "agent harness" in out.err
    switch.human(eid)
    out = _ok(capsys, "approve", eid, "requirements", "--dark")  # --dark implies --factory
    assert "AI Factory: on" in out and "Dark: on" in out and "without asking" in out
    assert "Dark AI Factory active" in out
    assert epics.latest_charter(configure(factory=DARK), eid)["delegate"]["dark"] is True


def test_cli_profile_add_list_remove(capsys, switch, dws):
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
])
def test_broad_rules_are_refused(dws, human, kind, value, why):
    with pytest.raises((ValidationError, UsageError), match=why):
        dark_profile.add(dws, human, kind, value)
    assert dark_profile.rules(dws) == []


def test_rule_ids_are_stable(dws, human):
    e = dark_profile.add(dws, human, "prefix", "npm  run verify")
    assert e["rule_id"] == dark_profile.rule_id("prefix", ["npm", "run", "verify"])
    assert e["rule_id"] != dark_profile.rule_id("exact", "npm run verify")


# -- matching ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,ok", [
    ("npm run verify", True), ("npm run verify --quiet", True), ("npm run verify 'a b'", True),
    ("npm run verify; rm -rf x", False), ("npm run verify && rm -rf x", False), ("npm run verify > f", False),
    ("npm run verify < f", False), ("npm run verify $(x)", False), ("npm run verify `x`", False),
    ("npm run verify | sh", False), ("npm run verify\nrm -rf x", False), ("npm run verify & x", False),
    ("npm run verify ${HOME}", False), ("npm run verify \\; x", False), ("(npm run verify)", False),
    ("npm run", False), ("npm run verifyx", False), ("FOO=1 npm run verify", False), ("npm run 'verify", False),
])
def test_prefix_matches_only_a_simple_command(dws, human, command, ok):
    dark_profile.add(dws, human, "prefix", "npm run verify")
    assert (dark_profile.match(dws, command) is not None) is ok


def test_exact_matches_only_identical_text(dws, human):
    dark_profile.add(dws, human, "exact", "make test > out.txt")
    assert dark_profile.match(dws, "make test > out.txt") is not None
    for other in ("make test > out.txt ", "make test >out.txt", "make test > out.txt; rm x", "make test"):
        assert dark_profile.match(dws, other) is None


# -- the hook ---------------------------------------------------------------------------------------------------------

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


def test_a_live_grant_still_allows_in_a_dark_epic(dws, da, dh, human):
    _dark(da, dh)
    permits.hook_decision(dws, _payload("make e2e"))
    (r,) = permits.open_requests(dws)
    permits.permit_grant(dws, human, r["id"], "once", expected_sha=r["sha"])
    assert _behavior(permits.hook_decision(dws, _payload("make e2e"))) == "allow"
    assert _behavior(permits.hook_decision(dws, _payload("make e2e"))) == "deny"


def _forge_rule(ws, human, kind, value):
    """A rule written straight into the ledger, past every check of `add`."""
    from orch.actor import process_evidence
    return ledger.record(ws, ticket=None, kind="dark_profile", actor=human, evidence=process_evidence(), op="add",
                         rule_id=dark_profile.rule_id(kind, value), rule_kind=kind, rule=value)


@pytest.mark.parametrize("kind,value,command", [
    ("exact", "sudo make install", "sudo make install"),
    ("exact", "orch permit grant P-1", "orch permit grant P-1"),
    ("prefix", ["git", "push"], "git push --force origin main"),
    ("prefix", ["cat", "x"], "cat x ~/.claude/settings.json"),
])
def test_never_grantable_is_never_allowed_even_if_listed(dws, da, dh, human, kind, value, command):
    _dark(da, dh)
    _forge_rule(dws, human, kind, value)
    assert dark_profile.rules(dws)
    assert dark_profile.match(dws, command) is None
    out = permits.hook_decision(dws, _payload(command))
    assert _behavior(out) == "deny" and "never granted" in _msg(out)


def test_dark_off_means_the_profile_is_ignored(dws, da, dh, human, configure):
    _dark(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    off = configure(factory={"enabled": True, "dark": False})
    out = permits.hook_decision(off, _payload("npm run verify"))
    assert _behavior(out) == "deny" and "waiting for permission" in _msg(out)
    (r,) = permits.open_requests(off)
    assert r["source"] == "harness"
    assert permits.dark_delegation(off, store.load(off, r["epic"])[1]) is None


def test_a_non_dark_factory_epic_ignores_the_profile(dws, da, dh, human):
    _epic(da, dh)
    dark_profile.add(dws, human, "prefix", "npm run verify")
    out = permits.hook_decision(dws, _payload("npm run verify"))
    assert _behavior(out) == "deny" and "waiting for permission" in _msg(out)


def test_another_workspaces_profile_does_not_apply(dws, da, dh, human, configure):
    _dark(da, dh)
    other = configure(customer="other", factory=DARK)
    dark_profile.add(other, human, "prefix", "npm run verify")
    assert dark_profile.rules(other)
    mine = configure(factory=DARK)
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
    assert dark_profile.rules(dws) == []
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
    assert permits.open_requests(dws) == []  # the rule answers the card; nothing else is signed
    assert not [x for x in ledger.entries(dws) if x.get("kind") in ("grant", "permit_deny")]
    assert _behavior(permits.hook_decision(dws, _payload("make deploy-staging"))) == "allow"
    assert _behavior(permits.hook_decision(dws, _payload("make deploy-staging --x"))) == "deny"


def test_only_an_open_dark_request_of_this_workspace(dws, da, dh, human, configure):
    _epic(da, dh)  # an ordinary factory epic: its request is a harness card
    permits.hook_decision(dws, _payload("make e2e"))
    (r,) = permits.open_requests(dws)
    with pytest.raises(ValidationError, match="not filed by a Dark factory"):
        dark_profile.add_from_request(dws, human, r["id"], expected_sha=r["sha"])
    other = configure(customer="other", factory=DARK)
    with pytest.raises(NotFoundError):
        dark_profile.add_from_request(other, human, r["id"], expected_sha=r["sha"])
    permits.permit_deny(configure(factory=DARK), human, r["id"], expected_sha=r["sha"])
    with pytest.raises(ValidationError, match="answered already"):
        dark_profile.add_from_request(configure(factory=DARK), human, r["id"], expected_sha=r["sha"])
    with pytest.raises(NotFoundError):
        dark_profile.add_from_request(dws, human, "P-00000000", expected_sha="sha256:00")
