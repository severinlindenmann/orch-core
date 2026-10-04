"""Fix round 1 (C2b): approvals, verdicts and closes are signed into a per-user ledger outside the workspace, so
editing the ticket or the event log alone cannot fake one."""
import json
import os
import stat

import pytest

from orch.core import ledger, store
from orch.core.check import run_checks
from orch.core.events import Actor, append_event
from orch.errors import OrchError, ValidationError


def _ready(aops, size="m"):
    t = aops.new("x", size=size)
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    return t.id


def _codes(ws, tid):
    return {f.code: f.level for f in run_checks(ws, emit_events=False) if f.ticket == tid}


def _forge_approval(ws, tid, gate, *, status=None, approved="2026-10-05T09:00Z", hash_v=2):
    """What an agent could do by editing files in the repository: frontmatter plus a matching human event."""
    from orch.core.gates import gate_hash
    path, t = store.load(ws, tid)
    h = gate_hash(t, gate)
    t.meta["gates"][gate] = {"approved": approved, "via": "tty", "hash": h, **({"hash_v": hash_v} if hash_v else {})}
    if status:
        t.meta["status"] = status
    store.save(ws, t, path)
    append_event(ws, tid, "gate.approved", Actor("agent", "x", "cli"), {"gate": gate, "hash": h})
    # rewrite the event's actor as the human, as a hand edit of events.jsonl would
    p = ws.state_dir / "events.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"agent:x"', '"human:you"')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_approve_writes_a_signed_entry_outside_the_workspace(ws, aops, hops):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    path = ledger.ledger_path(ws)
    assert ws.root not in path.parents and path == ledger.base_dir() / "ledger.jsonl"
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["workspace"] == ledger.workspace_id(ws)
    t = store.load(ws, tid)[1]
    assert (entry["ticket"], entry["kind"], entry["gate"], entry["hash"], entry["hash_v"]) == (
        tid, "gate", "requirements", t.meta["gates"]["requirements"]["hash"], 3)
    assert entry["actor"] == "human:you" and entry["via"] == "tty" and "evidence" in entry and entry["mac"]
    if os.name != "nt":
        assert stat.S_IMODE(ledger.key_path().stat().st_mode) == 0o600
    assert ledger.gate_verification(ws, t, "requirements") == "verified"
    assert "unsigned-decision" not in _codes(ws, tid)


def test_forged_approval_is_reported_and_blocks_the_agent(ws, aops):
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    assert _codes(ws, tid).get("unsigned-decision") == "warning"
    with pytest.raises(ValidationError, match="not in the ledger on this machine") as e:
        aops.claim(tid)
    assert "orch ledger adopt" in e.value.hint and "request changes" in e.value.hint


def test_forged_plan_approval_blocks_tasks_and_testing(ws, aops, hops):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. do it")
    _, ids = aops.task_add(tid, [{"text": "work"}])
    _forge_approval(ws, tid, "plan")
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.task_start(tid, ids[0])
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.move(tid, "testing")


def test_a_tampered_entry_does_not_count(ws, aops, hops):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    path = ledger.ledger_path(ws)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["ticket"] = "L-9999"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert ledger.entries(ws) == []
    assert _codes(ws, tid).get("unsigned-decision") == "warning"


def test_an_entry_signed_with_another_key_does_not_count(ws, aops, hops):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    ledger.key_path().write_bytes(os.urandom(32))
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_back_dated_approval_without_a_hash_version_still_blocks_the_agent(ws, aops):
    """Nothing in the ticket file (an old stamp, no hash_v) makes an approval count as signed."""
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open", approved="2026-09-30T08:00Z", hash_v=None)
    findings = [f for f in run_checks(ws, emit_events=False) if f.ticket == tid and f.code == "unsigned-decision"]
    assert findings and "orch ledger adopt" in findings[0].message
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(tid)


def test_approve_does_not_re_sign_an_unsigned_approval(ws, aops, hops):
    from orch.errors import TransitionError
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    with pytest.raises(TransitionError):
        hops.approve(tid, "requirements")
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_verdict_and_close_are_signed(ws, aops, hops, put):
    tid = put("testing", sections={"Verification": "ok"})
    hops.verdict(tid, "done")
    kinds = [(e["kind"], e.get("verdict")) for e in ledger.entries(ws)]
    assert ("verdict", "done") in kinds
    assert "unsigned-decision" not in _codes(ws, tid)
    other = put("open")
    hops.close(other, "duplicate")
    assert any(e["kind"] == "close" and e["ticket"] == other for e in ledger.entries(ws))
    assert "unsigned-decision" not in _codes(ws, other)


def test_forged_done_verdict_is_reported(ws, put):
    tid = put("done", gates={"requirements": {"approved": None, "via": None, "hash": None},
                             "plan": {"approved": None, "via": None, "hash": None},
                             "verify": {"verdict": "done", "at": "2026-10-05T10:00Z", "via": "tty"}})
    assert _codes(ws, tid).get("unsigned-decision") == "warning"


def test_nothing_is_applied_when_the_ledger_cannot_be_written(ws, aops, hops, monkeypatch):
    tid = _ready(aops)

    def broken(*a, **k):
        raise OrchError("could not write the approval ledger (test); nothing was applied")
    monkeypatch.setattr(ledger, "record", broken)
    with pytest.raises(OrchError, match="approval ledger"):
        hops.approve(tid, "requirements")
    assert store.load(ws, tid)[1].status == "backlog"


def test_override_of_an_open_question_line_is_recorded(ws, aops, hops):
    tid = _ready(aops)
    aops.set_section(tid, "Out of scope", "Open question: none for now, decided with the team")
    with pytest.raises(ValidationError, match="open question for you") as e:
        hops.approve(tid, "requirements")
    assert "--despite-open-question" in e.value.hint
    hops.approve(tid, "requirements", despite_open_question=True)
    from orch.core.events import read_events
    ev = [e for e in read_events(ws, tid) if e.kind == "gate.approved"][-1]
    assert ev.data["despite_open_question"] is True
    assert ledger.entries(ws)[-1]["despite_open_question"] is True


# -- the guard keeps agents' tools away from the ledger and its key --

@pytest.mark.parametrize("cmd", [
    "cat ~/.config/orch/ledger.key",
    "cat $XDG_CONFIG_HOME/orch/ledger/abc.jsonl",
    "echo x >> ~/.config/orch/ledger/abc.jsonl",
    "cp /tmp/x \"$ORCH_STATE_DIR/ledger.key\"",
    "python3 -c \"from orch.core import ledger; print(ledger.key_path().read_bytes())\"",
    "python3 -c \"import orch.core.ledger as l\"",
    "node -e \"require('fs').readFileSync(process.env.HOME + '/.config/orch/ledger.key')\"",
    "cat ~/.config/orch/led''ger.key",
])
def test_guard_denies_bash_access_to_the_ledger(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert not d.allow and "ledger" in d.reason, cmd


def test_guard_denies_file_tools_on_the_ledger(ws, aops, hops):
    from orch.hooks.guard import evaluate
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    for tool, key, value in [("Read", "file_path", str(ledger.key_path())),
                             ("Read", "file_path", str(ledger.ledger_path(ws))),
                             ("Edit", "file_path", str(ledger.ledger_path(ws))),
                             ("Write", "file_path", str(ledger.key_path())),
                             ("Grep", "path", str(ledger.ledger_path(ws).parent)),
                             ("Glob", "pattern", "*.jsonl", ),
                             ("Grep", "glob", "*.key")]:
        extra = {"path": str(ledger.base_dir())} if (tool, key) == ("Glob", "pattern") else {}
        extra = {"path": str(ledger.base_dir())} if (tool, key) == ("Grep", "glob") else extra
        d = evaluate(ws, {"tool_name": tool, "tool_input": {key: value, "content": "x", "old_string": "a",
                                                             "new_string": "b", **extra}})
        assert not d.allow, (tool, value)


def test_guard_allows_ordinary_mentions(ws):
    from orch.hooks.guard import evaluate
    for cmd in ("orch check", "grep -rn general_ledger src/", "cat docs/ledger-notes.md"):
        assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow, cmd


def test_check_help_explains_findings_in_clones(capsys):
    from orch.cli import run
    run(["check", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "clone" in out and "ledger" in out and "unverified-" in out


def test_config_dir_wide_reads_name_the_ledger(ws):
    from orch.hooks.guard import evaluate
    for payload in ({"tool_name": "Bash", "tool_input": {"command": "cat ~/.config/orch/*"}},
                    {"tool_name": "Read", "tool_input": {"file_path": str(ledger.base_dir())}}):
        d = evaluate(ws, payload)
        assert not d.allow and "ledger" in d.reason, payload


# -- final review C1: a signed approval of text that changed since does not count ----------------------------------

def _plan_approved(aops, hops, tid):
    hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. do it")
    _, ids = aops.task_add(tid, [{"text": "work"}, {"text": "more"}])
    hops.approve(tid, "plan")
    return ids


def test_edited_requirements_stop_task_work_and_testing(ws, aops, hops):
    tid = _ready(aops)
    ids = _plan_approved(aops, hops, tid)
    aops.task_start(tid, ids[0])
    aops.task_done(tid, ids[0])
    aops.task_skip(tid, ids[1], "not needed")
    aops.set_section(tid, "Verification", "- AC1: ran it")
    aops.set_section(tid, "Requirements", "r, and much more")
    with pytest.raises(ValidationError, match="requirements of .* changed since") as e:
        aops.move(tid, "testing")
    assert "approve" in e.value.hint
    aops.task_reopen(tid, ids[1])
    with pytest.raises(ValidationError, match="requirements of .* changed since"):
        aops.task_start(tid, ids[1])


def test_edited_plan_stops_testing(ws, aops, hops):
    tid = _ready(aops)
    ids = _plan_approved(aops, hops, tid)
    for i in ids:
        aops.task_start(tid, i)
        aops.task_done(tid, i)
    aops.set_section(tid, "Verification", "- AC1: ran it")
    aops.set_section(tid, "Plan", "1. something else")
    with pytest.raises(ValidationError, match="changed since|plan gate is invalidated"):
        aops.move(tid, "testing")


def test_edited_xs_requirements_stop_claim_and_testing(ws, aops, hops, close_tasks):
    tid = _ready(aops, size="xs")
    hops.approve(tid, "requirements")
    aops.set_section(tid, "Requirements", "r, widened")
    with pytest.raises(ValidationError, match="requirements of .* changed since"):
        aops.claim(tid)
    other = _ready(aops, size="xs")
    hops.approve(other, "requirements")
    aops.claim(other)
    close_tasks(aops, other)
    aops.set_section(other, "Verification", "- AC1: ran it")
    aops.set_section(other, "Acceptance criteria", "- [ ] a\n- [ ] b")
    with pytest.raises(ValidationError, match="requirements of .* changed since"):
        aops.move(other, "testing")
