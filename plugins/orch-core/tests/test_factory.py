"""AI Factory, phase 1 (#2): factory epics, signed permission grants and the PermissionRequest hook."""
import json
from datetime import timedelta

import pytest

from orch.core import epics, ledger, permits, store
from orch.core.events import read_events
from orch.errors import HumanOnlyError, UsageError, ValidationError

SESSION = "7f3c9a21-0000"  # the `agent` fixture's session


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


def _factory(fa, fh, **limits):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, **limits})
    c = fa.new("child", epic=e.id)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    fa.claim(c.id)
    return e.id, c.id


def _payload(command, session=SESSION, tool="Bash"):
    return {"session_id": session, "hook_event_name": "PermissionRequest", "tool_name": tool,
            "tool_input": {"command": command}, "permission_mode": "default"}


def _behavior(out):
    return out["hookSpecificOutput"]["decision"]["behavior"] if out else None


# -- the switch and the charter ----------------------------------------------------------------------------------

def test_factory_is_off_by_default(ws, aops, hops):
    assert not permits.enabled(ws)
    e = aops.new("E", type="epic")
    _refine(aops, e.id, plan=None)
    with pytest.raises(UsageError, match="switched off"):
        hops.approve(e.id, "requirements", delegate={"factory": True})


def test_factory_charter_signs_the_budget(fws, fa, fh):
    eid, _ = _factory(fa, fh)
    d = epics.delegation(fws, store.load(fws, eid)[1])
    assert (d["factory"], d["max_children"], d["max_hours"], d["max_size"], d["active"]) == (True, 25, 72, "m", True)
    entry = epics.latest_charter(fws, eid)
    assert entry["delegate"] == {"max_children": 25, "max_size": "m", "factory": True, "max_hours": 72}


def test_ordinary_delegation_is_unchanged(ws):
    assert epics.normalize_delegate({}) == {"max_children": 10, "max_size": "m"}
    assert epics.normalize_delegate({"max_hours": 5}) == {"max_children": 10, "max_size": "m"}


def test_factory_children_stay_at_size_m():
    with pytest.raises(UsageError, match="size m"):
        epics.normalize_delegate({"factory": True, "max_size": "l"})


def test_agent_cannot_start_a_factory(fws, fa):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    with pytest.raises(HumanOnlyError):
        fa.approve(e.id, "requirements", expected_hash="sha256:x", delegate={"factory": True})


def test_time_budget_stops_auto_approval_and_grants(fws, fa, fh, monkeypatch):
    from orch import clock
    eid, cid = _factory(fa, fh)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    d = epics.delegation(fws, store.load(fws, eid)[1])
    assert d["expired"] and not d["active"]
    c2 = fa.new("late", epic=eid)
    _refine(fa, c2.id)
    with pytest.raises(ValidationError, match="budget"):
        fa.epic_auto_approve(c2.id)
    # the child auto-approved within the budget keeps its approval
    assert ledger.gate_verification(fws, store.load(fws, cid)[1], "requirements") == "delegated"


def test_agent_does_not_ask_in_a_factory_epic(fws, fa, fh, configure):
    _, cid = _factory(fa, fh)
    q = [{"text": "Which colour?", "options": [{"key": "a", "label": "red"}, {"key": "b", "label": "blue"}],
          "recommended": "a"}]
    with pytest.raises(ValidationError, match="questions are not asked"):
        fa.ask(cid, q)
    # switched off: the epic is an ordinary delegated epic again
    from orch.core.ops import Ops
    off = configure(factory={"enabled": False})
    Ops(off, fa.actor).ask(cid, q)


# -- requests, grants and the hook -------------------------------------------------------------------------------

def test_hook_is_silent_without_the_factory(ws):
    assert permits.hook_decision(ws, _payload("make deploy")) is None


def test_hook_is_silent_outside_a_factory_session(fws, fa, fh):
    _factory(fa, fh)
    assert permits.hook_decision(fws, _payload("make deploy", session="someone-else")) is None


def test_hook_parks_then_allows_once_after_a_human_grant(fws, fa, fh, human):
    eid, cid = _factory(fa, fh)
    out = permits.hook_decision(fws, _payload("make deploy-staging"))
    assert _behavior(out) == "deny" and "P-" in out["hookSpecificOutput"]["decision"]["message"]
    (r,) = permits.open_requests(fws)
    assert (r["epic"], r["ticket"], r["command"], r["source"]) == (eid, cid, "make deploy-staging", "harness")
    # the same prompt again: the same card, not a second one
    permits.hook_decision(fws, _payload("make deploy-staging"))
    assert len(permits.open_requests(fws)) == 1
    g = permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    assert g["kind"] == "grant" and g in ledger.entries(fws)
    assert not permits.open_requests(fws)
    assert _behavior(permits.hook_decision(fws, _payload("make deploy-staging"))) == "allow"
    # used up: the next prompt asks again
    assert _behavior(permits.hook_decision(fws, _payload("make deploy-staging"))) == "deny"
    assert len(permits.open_requests(fws)) == 1
    # another command is never covered by the grant
    assert _behavior(permits.hook_decision(fws, _payload("make deploy-staging && rm -rf x"))) == "deny"


def test_epic_grant_lasts_until_revoked_or_paused(fws, fa, fh, human):
    eid, _ = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    g = permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])
    for _ in range(2):
        assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "allow"
    permits.permit_revoke(fws, human, g["grant"])
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "deny"
    (r2,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r2["id"], "epic", expected_sha=r2["sha"])
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "allow"
    fh.epic_pause(eid)
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "deny"


def test_grant_ends_when_the_epic_text_changes(fws, fa, fh, human):
    eid, _ = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])
    fa.set_section(eid, "Requirements", "a different epic")
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "deny"


def test_only_the_human_answers(fws, fa, fh, agent):
    _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    for call in (lambda: permits.permit_grant(fws, agent, r["id"], "once", expected_sha=r["sha"]),
                 lambda: permits.permit_deny(fws, agent, r["id"], expected_sha=r["sha"])):
        with pytest.raises(HumanOnlyError):
            call()
    # a human process inside an agent harness is refused too
    import os
    os.environ["CLAUDECODE"] = "1"
    try:
        from orch.core.events import Actor
        with pytest.raises(HumanOnlyError):
            permits.permit_grant(fws, Actor("human", "you", "tty"), r["id"], "once", expected_sha=r["sha"])
    finally:
        del os.environ["CLAUDECODE"]


def test_grant_binds_the_command_shown(fws, fa, fh, human):
    _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    with pytest.raises(ValidationError, match="not the one you were shown"):
        permits.permit_grant(fws, human, r["id"], "once", expected_sha=permits.command_sha("make other"))


def test_deny_is_signed_and_an_agent_event_closes_no_card(fws, fa, fh, human, agent):
    from orch.core.events import append_event
    _, cid = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    append_event(fws, cid, "permit.denied", agent, {"request": r["id"]})
    assert len(permits.open_requests(fws)) == 1
    permits.permit_deny(fws, human, r["id"], expected_sha=r["sha"])
    assert not permits.open_requests(fws)
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "deny"


def test_forged_or_edited_grant_answers_nothing(fws, fa, fh, human):
    eid, _ = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])
    path = ledger.ledger_path(fws)
    lines = path.read_text(encoding="utf-8").splitlines()
    g = json.loads(lines[-1])
    g["command"] = g["command"] + " --force"  # edited after signing: the mac no longer checks out
    path.write_text("\n".join(lines[:-1] + [json.dumps(g)]) + "\n", encoding="utf-8")
    assert not [x for x in ledger.entries(fws) if x.get("kind") == "grant"]
    assert _behavior(permits.hook_decision(fws, _payload("make e2e --force"))) == "deny"
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "deny"


def test_a_once_grant_is_used_up_by_one_caller(fws, fa, fh, human, agent):
    eid, cid = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    g = permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    assert permits.use(fws, agent, g, cid) is True
    assert permits.use(fws, agent, g, cid) is False


@pytest.mark.parametrize("command", [
    "orch approve L-0001 requirements",
    "orch permit grant P-3",
    "git commit --no-verify -m x",
    "echo '{}' > .claude/settings.local.json",
    "orch serve",
])
def test_never_grantable(fws, fa, fh, command):
    _, cid = _factory(fa, fh)
    out = permits.hook_decision(fws, _payload(command))
    assert _behavior(out) == "deny" and "never granted" in out["hookSpecificOutput"]["decision"]["message"]
    assert not permits.open_requests(fws)  # no card is ever offered
    with pytest.raises(ValidationError, match="never be granted"):
        permits.request(fws, fa.actor, store.load(fws, cid)[1], command)


def test_hook_denies_other_tools_in_a_factory_session(fws, fa, fh):
    _factory(fa, fh)
    assert _behavior(permits.hook_decision(fws, _payload("x", tool="WebFetch"))) == "deny"


def test_hook_never_allows_on_an_error(fws, fa, fh, human, monkeypatch):
    _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])

    def boom(*a, **k):
        raise OSError("ledger unreadable")
    monkeypatch.setattr(permits, "find_live_grant", boom)
    out = permits.hook_decision(fws, _payload("make e2e"))
    assert _behavior(out) == "deny" and "nothing was allowed" in out["hookSpecificOutput"]["decision"]["message"]


def test_agent_request_files_a_card_after_an_auto_mode_denial(fws, fa, fh, human):
    """The open gap: a PermissionRequest hook is not consulted after an auto-mode classifier denial. The agent files
    the card itself; the grant then answers the next prompt the harness raises through the hook. Nothing is written
    into harness settings (owner decision D2 B)."""
    import os
    from pathlib import Path
    _, cid = _factory(fa, fh)
    r = permits.request(fws, fa.actor, store.load(fws, cid)[1], "make deploy-staging", reason="Denied by auto mode")
    assert r["source"] == "agent" and permits.open_requests(fws) == [r]
    before = {p for p in Path(os.environ["CLAUDE_CONFIG_DIR"]).rglob("*")} | set(fws.root.rglob(".claude*"))
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    assert _behavior(permits.hook_decision(fws, _payload("make deploy-staging"))) == "allow"
    after = {p for p in Path(os.environ["CLAUDE_CONFIG_DIR"]).rglob("*")} | set(fws.root.rglob(".claude*"))
    assert after == before


def test_request_outside_a_factory_is_refused(fws, fa, fh):
    t = fa.new("loose")
    with pytest.raises(ValidationError, match="not part of a factory epic"):
        permits.request(fws, fa.actor, store.load(fws, t.id)[1], "make x")


def test_grant_wakes_orch_wait(fws, fa, fh, human):
    from orch.core.wait import wait_for_human
    _, cid = _factory(fa, fh)
    permits.hook_decision(fws, _payload("make e2e"))
    (r,) = permits.open_requests(fws)
    cursor = read_events(fws)[-1].seq
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    ev = wait_for_human(fws, cid, after=cursor, timeout=1, poll=0.01)
    assert ev is not None and ev.kind == "permit.granted"


# -- the guard and the plugin hook -------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,allowed", [
    ("orch permit grant P-1", False), ("orch permit deny P-1", False), ("orch permit revoke abc", False),
    ("uv run orch permit --json grant P-1", False), ("echo P-1 | xargs orch permit grant", False),
    ("orch permit request 'make x' --ticket L-0002", True), ("orch permit list", True),
])
def test_guard_keeps_answers_with_the_human(ws, cmd, allowed):
    from orch.hooks.guard import evaluate
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow is allowed


def test_plugin_registers_the_permission_hook():
    from pathlib import Path
    hooks = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = hooks["hooks"]["PermissionRequest"]
    assert entry["hooks"][0]["command"] == '"${CLAUDE_PLUGIN_ROOT}/bin/orch" permit hook'


def test_hook_cli_is_silent_outside_a_workspace(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from orch.cli import app
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["permit", "hook"], input=json.dumps({"cwd": str(tmp_path), **_payload("ls")}))
    assert res.exit_code == 0 and res.stdout.strip() == ""


# -- security review round 1 ---------------------------------------------------------------------------------------

def _grant_once(fws, human, command="make e2e"):
    permits.hook_decision(fws, _payload(command))
    (r,) = permits.open_requests(fws)
    return permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])


def test_once_grant_is_used_up_for_every_checkout(fws, fa, fh, human, ws_root, tmp_path):
    """The use is recorded beside the ledger, not in this checkout's event log."""
    import shutil
    from orch.core.workspace import Workspace
    _factory(fa, fh)
    _grant_once(fws, human)
    copy = tmp_path / "copy"
    shutil.copytree(ws_root, copy)  # another checkout of the same workspace, before the grant is used
    assert _behavior(permits.hook_decision(fws, _payload("make e2e"))) == "allow"
    other = Workspace.open(copy)
    assert _behavior(permits.hook_decision(other, _payload("make e2e"))) == "deny"


def test_session_spanning_two_factory_epics_gets_no_answer(fws, fa, fh):
    _factory(fa, fh)
    _factory(fa, fh)  # the same agent session claims a child of a second factory epic
    assert permits.hook_decision(fws, _payload("make e2e")) is None


def test_factory_epic_is_never_taken_from_the_environment(fws, fa, fh, monkeypatch):
    eid, _ = _factory(fa, fh)
    monkeypatch.setenv("ORCH_FACTORY_EPIC", eid)
    assert permits.hook_decision(fws, _payload("make e2e", session="unclaimed")) is None


@pytest.mark.parametrize("command", ["make e2e‮", "make e2e", "make e2e\nrm x", "make e2e\r", "ma\tke"])
def test_text_outside_printable_ascii_is_never_grantable(fws, fa, fh, command):
    _, cid = _factory(fa, fh)
    out = permits.hook_decision(fws, _payload(command))
    assert _behavior(out) == "deny" and "never granted" in out["hookSpecificOutput"]["decision"]["message"]
    assert not permits.open_requests(fws)
    with pytest.raises(ValidationError, match="never be granted"):
        permits.request(fws, fa.actor, store.load(fws, cid)[1], command)


def test_cards_escape_what_could_hide():
    assert permits.shown("a‮b\nc") == "a\\u202eb\\nc"


@pytest.mark.parametrize("command", [
    "gh pr merge 12 --squash", "gh pr merge --auto 3", "git push --force origin main", "git push -f origin main",
    "git push origin +main", "git push --force-with-lease origin main", "cp x ~/.claude/plugins/a/b",
    "echo x > $CLAUDE_PLUGIN_ROOT/hooks/x", "echo '{}' > ~/.claude.json", "echo '{}' > orchestrator/config.json",
    "ORCH_STATE_DIR=/tmp/x make e2e", "export XDG_CONFIG_HOME=/tmp/x", "CLAUDE_CODE_SESSION_ID=abc orch claim L-1",
    "ORCH_SESSION=x make", "sudo make install", "rm -rf ~", "rm -rf /", "rm -r -f $HOME", "rm -rf .",
    "chmod 777 ~/.config/orch", "chown me $ORCH_STATE_DIR",
])
def test_more_never_grantable(fws, command):
    assert permits.never_grantable(fws, command)


def test_ordinary_commands_stay_grantable(fws):
    for command in ("make e2e", "npm run deploy-staging", "rm -rf build", "git fetch origin"):
        assert permits.never_grantable(fws, command) is None, command


def test_request_body_stays_outside_the_repository(fws, fa, fh):
    from orch.core.ledger import base_dir
    _, cid = _factory(fa, fh)
    r = permits.request(fws, fa.actor, store.load(fws, cid)[1], "make secret-target", reason="private reason")
    log = (fws.state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "secret-target" not in log and "private reason" not in log
    (ev,) = [e for e in read_events(fws) if e.kind == "permit.requested"]
    assert set(ev.data) == {"request", "epic", "command_sha"}
    import re
    assert re.fullmatch(r"P-[0-9A-F]{8}", r["id"])
    body = base_dir() / "permits" / "requests" / f"{r['id']}.json"
    assert json.loads(body.read_text(encoding="utf-8"))["command"] == "make secret-target"
    # a body changed after filing no longer matches its event: the request is gone, nothing to grant
    data = json.loads(body.read_text(encoding="utf-8"))
    data["command"] = "make other"
    body.write_text(json.dumps(data), encoding="utf-8")
    assert permits.open_requests(fws) == []


def test_non_string_command_is_denied_and_files_nothing(fws, fa, fh):
    _factory(fa, fh)
    p = _payload("x")
    p["tool_input"] = {"command": ["make", "e2e"]}
    assert _behavior(permits.hook_decision(fws, p)) == "deny"
    assert not permits.open_requests(fws) and not [e for e in read_events(fws) if e.kind == "permit.requested"]


def test_used_up_budget_stops_claims_and_task_starts_and_raises_a_card(fws, fa, fh, monkeypatch):
    from orch import clock
    eid, cid = _factory(fa, fh)
    fa.task_add(cid, ["do it"])
    late = fa.new("late", epic=eid)
    _refine(fa, late.id)
    fa.epic_auto_approve(late.id)
    assert permits.budget_cards(fws) == []
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    with pytest.raises(ValidationError, match="time budget"):
        fa.claim(late.id)
    with pytest.raises(ValidationError, match="time budget"):
        fa.task_start(cid, "T1")
    (card,) = permits.budget_cards(fws)
    assert card["epic"] == eid and "72 hours" in card["reason"]


def test_child_budget_raises_a_card(fws, fa, fh):
    eid, _ = _factory(fa, fh, max_children=1)
    (card,) = permits.budget_cards(fws)
    assert card["epic"] == eid and "child budget" in card["reason"]


def test_child_budget_survives_erased_events(fws, fa, fh):
    eid, cid = _factory(fa, fh, max_children=2)
    assert permits.budget_cards(fws) == []
    second = fa.new("second", epic=eid)
    _refine(fa, second.id)
    fa.epic_auto_approve(second.id)
    from orch.core.events import events_path
    p = events_path(fws)
    p.write_text("".join(l for l in p.read_text().splitlines(True) if "gate.delegated" not in l))
    (card,) = permits.budget_cards(fws)
    assert card["epic"] == eid and "child budget" in card["reason"]


def test_claims_and_task_starts_refused_past_the_child_budget(fws, fa, fh):
    eid, cid = _factory(fa, fh, max_children=1)
    fa.task_add(cid, ["do it"])
    fa.task_start(cid, "T1")  # an approved child goes on at the limit
    extra = fa.new("extra", epic=eid)
    _refine(fa, extra.id)
    with pytest.raises(ValidationError, match="auto-approved children"):
        fa.epic_auto_approve(extra.id)
    with pytest.raises(ValidationError, match="child budget"):
        permits.require_budget(fws, store.load(fws, extra.id)[1])
    fh.approve(extra.id, "requirements")  # the human's own approval is not counted against it
    permits.require_budget(fws, store.load(fws, extra.id)[1])
    fa.claim(extra.id)


def _switch_off(fws):
    fws.config["factory"] = {"enabled": False}  # what editing orchestrator/config.json does


def test_budget_stops_hold_with_the_switch_flipped_off(fws, fa, fh, monkeypatch):
    from orch import clock
    eid, cid = _factory(fa, fh)
    fa.task_add(cid, ["do it"])
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    _switch_off(fws)
    with pytest.raises(ValidationError, match="time budget"):
        fa.task_start(cid, "T1")


def test_child_budget_holds_with_the_switch_flipped_off(fws, fa, fh):
    eid, cid = _factory(fa, fh, max_children=1)
    extra = fa.new("extra", epic=eid)
    _refine(fa, extra.id)
    _switch_off(fws)
    with pytest.raises(ValidationError, match="child budget"):
        permits.require_budget(fws, store.load(fws, extra.id)[1])


def test_auto_approval_signs_nothing_into_the_ledger(fws, fa, fh):
    _, cid = _factory(fa, fh)
    assert not [e for e in ledger.entries(fws) if e.get("ticket") == cid]


def test_no_message_names_a_flag_that_does_not_exist():
    with pytest.raises(UsageError) as e:
        epics.normalize_delegate({"factory": True, "max_hours": 0})
    assert "--max-hours" not in str(e.value)


def test_hook_fast_path_reads_only_the_config(ws_root, monkeypatch):
    from typer.testing import CliRunner
    from orch.cli import app
    before = sorted(p.name for p in (ws_root / "orchestrator").iterdir())
    res = CliRunner().invoke(app, ["permit", "hook"], input=json.dumps({"cwd": str(ws_root), **_payload("ls")}))
    assert res.exit_code == 0 and res.stdout.strip() == ""
    assert sorted(p.name for p in (ws_root / "orchestrator").iterdir()) == before
    assert not permits.enabled_at(ws_root)
