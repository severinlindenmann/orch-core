"""#61: named checks are a signed human setting, a hand-written `receipt-t<n>` gates block shows as unverified, and the
receipt's temp file on disk is capped."""
import json
import subprocess

import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger, store
from orch.core.check import run_checks
from orch.core.ops import Ops
from orch.core.receipts import CUT, _Sink, run_steps
from orch.errors import HumanOnlyError
from orch.widgets import Ctx
from orch.widgets.blocks import parse_blocks, ticket_blocks
from orch.widgets.render import render_html

CHECKS = {"verify": {"steps": [{"name": "test", "run": "true"}]}}


@pytest.fixture
def ticket(working, plan_approved):
    plan_approved(working)
    return working


def set_checks(ws, checks):
    cfg = json.loads((ws.home / "config.json").read_text())
    cfg["checks"] = checks
    (ws.home / "config.json").write_text(json.dumps(cfg))
    ws.config["checks"] = json.loads(json.dumps(checks))
    return ws


def _reopen(ws):
    from orch.core.workspace import Workspace
    return Workspace.open(ws.root)


def _codes(ws):
    return {f.code for f in run_checks(ws, emit_events=False) if f.ticket is None}


def _done(aops, ticket, tmp_path, verify="check:verify"):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": verify}])
    aops.task_start(ticket, ids[0])
    return ids[0], aops.task_done_run(ticket, ids[0], cwd=tmp_path)


# -- signed checks --------------------------------------------------------------------------------------------------

def test_a_check_is_unsigned_until_the_human_signs(ws):
    set_checks(ws, CHECKS)
    assert ledger.check_state(ws, "verify") == "unsigned" and ledger.check_state(ws, "nope") == "missing"
    assert "unsigned-check" in _codes(ws)


def test_the_human_signs_and_the_check_is_signed(ws, human):
    set_checks(ws, CHECKS)
    digests = Ops(ws, human).sign_checks()
    assert set(digests) == {"verify"}
    entry = [e for e in ledger.entries(ws) if e["kind"] == "setting"][-1]
    assert entry["setting"] == "checks" and entry["value"] == digests and entry["actor"] == "human:you"
    again = _reopen(ws)
    assert ledger.check_state(again, "verify") == "signed" and "unsigned-check" not in _codes(again)


def test_an_agent_cannot_sign(ws, agent):
    set_checks(ws, CHECKS)
    with pytest.raises(HumanOnlyError):
        Ops(ws, agent).sign_checks()
    assert not ledger.entries(ws)


def test_the_cli_refuses_to_sign_in_an_agent_harness(ws, monkeypatch, capsys):
    set_checks(ws, CHECKS)
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    assert run(["checks", "sign"]) != 0
    assert "agent harness" in capsys.readouterr().err
    assert ledger.check_state(_reopen(ws), "verify") == "unsigned"


def test_a_check_edited_after_signing_is_changed_and_the_other_stays_signed(ws, human):
    two = {**CHECKS, "lint": {"steps": [{"name": "l", "run": "true"}]}}
    set_checks(ws, two)
    Ops(ws, human).sign_checks()
    cfg = json.loads((ws.home / "config.json").read_text())
    cfg["checks"]["verify"]["steps"][0]["run"] = "exit 0  # rewritten"
    (ws.home / "config.json").write_text(json.dumps(cfg))
    again = _reopen(ws)
    assert (ledger.check_state(again, "verify"), ledger.check_state(again, "lint")) == ("changed", "signed")
    assert "unsigned-check" in _codes(again)


def test_a_run_reports_an_unsigned_check_on_the_receipt(ws, aops, ticket, tmp_path):
    set_checks(ws, CHECKS)
    _, rec = _done(aops, ticket, tmp_path)
    assert rec["check_state"] == "unsigned"
    assert "check not signed by the human" in _block(ws, ticket)["source"]


def test_a_run_of_a_signed_check_says_nothing(ws, aops, human, ticket, tmp_path):
    set_checks(ws, CHECKS)
    Ops(ws, human).sign_checks()
    _, rec = _done(aops, ticket, tmp_path)
    assert rec["check_state"] == "signed" and "not signed" not in _block(ws, ticket)["source"]


def test_a_run_reports_a_changed_check(ws, aops, human, ticket, tmp_path):
    set_checks(ws, CHECKS)
    Ops(ws, human).sign_checks()
    ws.config["checks"]["verify"]["steps"][0]["run"] = "true # other"
    _, rec = _done(aops, ticket, tmp_path)
    assert rec["check_state"] == "changed" and "changed since the human signed" in _block(ws, ticket)["source"]


def test_a_plain_command_has_no_check_state(ws, aops, ticket, tmp_path):
    _, rec = _done(aops, ticket, tmp_path, verify="cmd: true")
    assert rec["check_state"] is None


def _block(ws, tid):
    text = store.load(ws, tid)[1].section("Verification")
    return next(b.data for b in parse_blocks(text) if b.data.get("type") == "gates")


# -- cross-check ----------------------------------------------------------------------------------------------------

def _gates(ws, tid):
    t = store.load(ws, tid)[1]
    return t, next(b for b in ticket_blocks(t) if (b.data or {}).get("type") == "gates")


def test_a_real_receipt_block_is_not_unverified(ws, aops, ticket, tmp_path):
    _done(aops, ticket, tmp_path, verify="cmd: true")
    t, b = _gates(ws, ticket)
    assert "Unverified" not in render_html(b, Ctx.of(ws, t))


def test_a_hand_written_receipt_block_is_unverified(ws, aops, ticket):
    block = {"type": "gates", "id": "receipt-t1", "title": "T1 verify", "items": [{"name": "test", "status": "pass"}]}
    aops.set_section(ticket, "Verification", "```orch\n" + json.dumps(block) + "\n```")
    t, b = _gates(ws, ticket)
    assert "Unverified" in render_html(b, Ctx.of(ws, t))


def test_a_block_that_disagrees_with_the_receipt_is_unverified(ws, aops, ticket, tmp_path):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": "cmd: exit 1"}])
    aops.task_start(ticket, ids[0])
    with pytest.raises(Exception):
        aops.task_done_run(ticket, ids[0], cwd=tmp_path)
    t, b = _gates(ws, ticket)
    assert "Unverified" not in render_html(b, Ctx.of(ws, t))
    forged = {**b.data, "items": [{"name": "verify", "status": "pass"}]}
    aops.set_section(ticket, "Verification", "```orch\n" + json.dumps(forged) + "\n```")
    t, b = _gates(ws, ticket)
    assert "Unverified" in render_html(b, Ctx.of(ws, t))


def test_other_gates_blocks_are_not_judged(ws, aops, ticket):
    block = {"type": "gates", "id": "ci", "items": [{"name": "build", "status": "pass"}]}
    aops.set_section(ticket, "Verification", "```orch\n" + json.dumps(block) + "\n```")
    t, b = _gates(ws, ticket)
    assert "Unverified" not in render_html(b, Ctx.of(ws, t))


# -- temp file cap --------------------------------------------------------------------------------------------------

def test_the_sink_never_holds_more_than_its_cap_and_keeps_the_newest_output():
    s = _Sink(keep=100, cap=300)
    peak = 0
    for i in range(500):
        s.write(f"line {i:04d}\n".encode())
        peak = max(peak, s.size())
    assert peak <= 300 and s.dropped
    tail = s.tail()
    assert len(tail) <= 100 and tail.startswith(CUT) and tail.endswith(b"line 0499\n")
    s.close()


def test_a_small_output_is_kept_whole():
    s = _Sink(keep=100)
    s.write(b"hello\n")
    assert s.tail() == b"hello\n" and not s.dropped


def test_a_run_with_huge_output_keeps_a_bounded_file(tmp_path, monkeypatch):
    peaks = []
    orig = _Sink.write

    def spy(self, data):
        orig(self, data)
        peaks.append(self.size())
    monkeypatch.setattr(_Sink, "write", spy)
    r = run_steps([{"name": "v", "run": "yes line | head -c 3000000; echo END"}], tmp_path, timeout=60, max_bytes=1000)
    assert max(peaks) <= 2000 and len(r.log) <= 1000
    assert r.log.startswith(CUT) and b"END" in r.log
    assert r.ok
