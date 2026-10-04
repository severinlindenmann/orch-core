import shutil
import subprocess

import pytest

from orch.core import store
from orch.core.check import run_checks
from orch.core.events import read_events


def codes(findings):
    return sorted({f.code for f in findings})


def _approved_open(aops, hops):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    return t.id


def test_clean_workspace(ws, aops, hops):
    _approved_open(aops, hops)
    assert run_checks(ws) == []


def test_structural_problems(ws, aops, hops):
    tid = _approved_open(aops, hops)
    path = store.resolve(ws, tid).path
    (ws.status_dir("done") / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (ws.status_dir("backlog") / "L-0077-broken.md").write_text("---\nid: [\n---\n", encoding="utf-8")
    (ws.artifacts_dir / "L-0999").mkdir(parents=True)
    got = set(codes(run_checks(ws)))
    assert {"duplicate", "status-mismatch", "parse", "orphan-artifacts"} <= got


def test_hand_edited_approval_and_answer_detected(ws, aops):
    tid = aops.new("x").id
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-09-30T09:00Z", "via": "dashboard", "hash": "sha256:bogus"}
    t.meta["questions"] = [{"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True, "answer": "sure", "via": "tty"}]
    store.save(ws, t, path)
    got = codes(run_checks(ws))
    assert "unverified-approval" in got and "unverified-answer" in got


def test_gate_invalidated_event_emitted_once(ws, aops, hops):
    tid = _approved_open(aops, hops)
    aops.set_section(tid, "Requirements", "changed after approval")
    assert "gate-invalidated" in codes(run_checks(ws))
    run_checks(ws)
    assert [e.kind for e in read_events(ws, tid)].count("gate.invalidated") == 1


def test_open_without_gate_and_done_without_verdict(ws, put):
    put("open")
    put("done")
    got = codes(run_checks(ws))
    assert "status-without-gate" in got and "unverified-verdict" in got


def test_config_and_artifact_mode(configure):
    ws = configure(dashboard={"port": "x"}, artifacts={"mode": "claude"})
    got = codes(run_checks(ws))
    assert "config" in got and "artifact-mode" in got


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commits_citing_unknown_tickets(configure, ws_root):
    repo = ws_root / "hub"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", "L-0099 Add thing")
    git("commit", "--allow-empty", "-q", "-m", "ABC-5 Fix thing")
    ws = configure(git={"repos": {"hub": {}}},
                   external_trackers=[{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "u/{key}"}])
    got = {(f.code, f.level) for f in run_checks(ws)}
    assert ("commit-unknown-ticket", "error") in got
    assert ("commit-unlinked-key", "warning") in got


# -- forged human actions (finding 1) and plan-less testing/done (finding 2) ---------------

def _to_testing(ws, aops, hops):
    tid = _approved_open(aops, hops)
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. do it")
    hops.approve(tid, "plan")
    aops.set_section(tid, "Verification", "ran it")
    aops.task_add(tid, [{"text": "the work"}])
    aops.task_start(tid, "T1")
    aops.task_done(tid, "T1")
    aops.move(tid, "testing")
    return tid


def _hand_edit(ws, tid, fn):
    path, t = store.load(ws, tid)
    fn(t)
    store.save(ws, t, path)


def test_full_legitimate_flow_is_clean(ws, aops, hops):
    tid = _to_testing(ws, aops, hops)
    hops.verdict(tid, "done")
    assert run_checks(ws) == []


def test_reapproval_after_invalidation_is_clean(ws, aops, hops):
    tid = _approved_open(aops, hops)
    aops.claim(tid)
    aops.set_section(tid, "Plan", "v1")
    hops.approve(tid, "plan")
    aops.set_section(tid, "Plan", "v2")
    hops.approve(tid, "plan")
    assert run_checks(ws) == []


def test_forged_plan_hash_detected(ws, aops, hops):
    from orch.core.gates import gate_hash
    tid = _approved_open(aops, hops)
    aops.claim(tid)
    aops.set_section(tid, "Plan", "the approved plan")
    hops.approve(tid, "plan")
    aops.set_section(tid, "Plan", "a different plan the human never saw")
    _hand_edit(ws, tid, lambda t: t.meta["gates"]["plan"].update(hash=gate_hash(t, "plan")))
    assert "unverified-approval" in codes(run_checks(ws))


def test_forged_verdict_after_reopen_detected(ws, aops, hops):
    tid = _to_testing(ws, aops, hops)
    hops.verdict(tid, "done")
    hops.move(tid, "backlog")
    _hand_edit(ws, tid, lambda t: t.meta.update(status="done"))
    assert "unverified-verdict" in codes(run_checks(ws))


def test_forged_answer_detected(ws, aops, hops):
    tid = _approved_open(aops, hops)
    aops.claim(tid)
    aops.ask(tid, [{"text": "A or B?", "options": ["a", "b"], "recommended": "A"}])
    hops.answer(tid, "Q1", "A")
    assert "unverified-answer" not in codes(run_checks(ws))
    _hand_edit(ws, tid, lambda t: t.meta["questions"][0].update(answer="B"))
    assert "unverified-answer" in codes(run_checks(ws))


@pytest.mark.parametrize("status", ["testing", "done"])
def test_testing_or_done_without_plan_approval(ws, put, status):
    put(status)
    put(status, size="xs")
    found = [f for f in run_checks(ws) if f.code == "status-without-plan"]
    assert [f.ticket for f in found] == ["L-0001"] and found[0].level == "error"


# -- remaining per-ticket codes (finding 7) ------------------------------------------------

def test_claim_expired(ws, put):
    put("in-progress", claim={"session": "s-old", "harness": "copilot", "at": "2020-01-01T00:00Z"})
    found = [f for f in run_checks(ws) if f.code == "claim-expired"]
    assert len(found) == 1 and found[0].level == "warning" and "copilot" in found[0].message


def test_fresh_claim_not_expired(ws, put):
    from orch.clock import stamp
    put("in-progress", claim={"session": "s-new", "harness": "copilot", "at": stamp()})
    assert "claim-expired" not in codes(run_checks(ws))


def test_waiting_without_question(ws, put):
    put("waiting")
    put("waiting", questions=[{"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True, "answer": None}])
    found = [f for f in run_checks(ws) if f.code == "waiting-without-question"]
    assert [f.ticket for f in found] == ["L-0001"] and found[0].level == "warning"


def test_blocking_question_open_while_in_progress(ws, put):
    put("in-progress", questions=[{"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True, "answer": None}])
    put("in-progress", questions=[{"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": False, "answer": None}])
    found = [f for f in run_checks(ws) if f.code == "blocking-question-open"]
    assert [f.ticket for f in found] == ["L-0001"] and "Q1" in found[0].message


def test_record_invalidations_writes_what_a_quiet_run_left_out(ws, aops, hops):
    from orch.core.check import record_invalidations
    tid = _approved_open(aops, hops)
    aops.set_section(tid, "Requirements", "changed after approval")
    findings = run_checks(ws, emit_events=False)
    assert "gate-invalidated" in codes(findings)
    assert "gate.invalidated" not in [e.kind for e in read_events(ws, tid)]
    record_invalidations(ws, findings)
    record_invalidations(ws, findings)
    run_checks(ws)
    assert [e.kind for e in read_events(ws, tid)].count("gate.invalidated") == 1
