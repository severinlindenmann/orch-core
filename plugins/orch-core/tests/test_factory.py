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
