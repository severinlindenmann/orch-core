"""H3b2 review: a direct-mode receipt is labelled too (the agent's environment ran it, not orch), and the host-only
``receipt`` arguments are refused in direct mode."""

from __future__ import annotations

import pytest

from orch.cli import parser


@pytest.fixture(autouse=True)
def _plan_gate_off(ws):
    """r10-P02: an agent builds only after the plan is approved. These tests are about tasks and receipts, not the
    plan gate: the workspace turns it off (a signed policy change, as a person would make it)."""
    ws.plan_gate_off()


def test_a_direct_mode_run_receipt_is_labelled_attested_by_the_agent(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    cli("task", "add", "x", "--verify", "true")
    cli("task", "start", "T1")
    assert cli("task", "done", "T1", "--run").code == 0
    done = next(e for e in ws.events("1") if e["type"] == "task.done")
    assert done["receipt"]["attested_by"] == "agent" and done["receipt"]["exit"] == 0


def test_a_client_receipt_is_refused_in_direct_mode(ws, cli, monkeypatch):
    cli("new", "A")
    cli("claim", "1")
    cli("task", "add", "x", "--verify", "true")
    cli("task", "start", "T1")
    real = parser.parse

    def inject(argv):
        parsed = real(argv)
        parsed.args["receipt"] = {"cmd": "true", "exit": 0, "duration_ms": 1}
        parsed.args["receipt_output"] = "-"
        return parsed

    monkeypatch.setattr(parser, "parse", inject)
    r = cli.j("task", "done", "T1")
    assert (r.code, r.err_code) == (5, "invalid.input") and "direct mode" in r.out
    assert not [e for e in ws.events("1") if e["type"] == "task.done"]
