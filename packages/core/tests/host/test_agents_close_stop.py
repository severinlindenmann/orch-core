"""W12: when a ticket closes the host ends that ticket's live agent runs (polite stop, hard stop after a grace), and
only those. Same fixtures as ``test_agents`` (fake harness programs, a runner in this process)."""

# ruff: noqa: F811

from __future__ import annotations

import sys

import pytest

from orch.host import runners
from tests.host.dispatch_helpers import ok
from tests.host.test_agents import _main, ready, record, w  # noqa: F401
from tests.host.test_runner_socket import wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


def close(w, uid):
    w.person_event("ticket.closed", uid, resolution="wont_do")


def test_closing_the_ticket_ends_its_agent_run_and_notes_why(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket()
    _, run = _main(w, c, key)
    assert record(w, run["run"])["state"] == "running"
    close(w, uid)
    wait_for(lambda: record(w, run["run"])["state"] == "exited")
    rec = record(w, run["run"])
    assert rec["stop_cause"] == "ticket_closed" and rec["exit"]["reason"] == "stopped"
    live = ok(c.call("run.list", {"state": "live"}))["data"]["runs"]
    assert run["run"] not in [r["run"] for r in live]


def test_a_run_of_another_open_ticket_survives(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("one")
    _, other = w.ticket("two")
    _, run_a = _main(w, c, key)
    _, run_b = _main(w, c, other)
    close(w, uid)
    wait_for(lambda: record(w, run_a["run"])["state"] == "exited")
    assert record(w, run_b["run"])["state"] == "running"
    assert "stop_cause" not in record(w, run_b["run"])


def test_reopening_allows_a_fresh_start(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket()
    _, run = _main(w, c, key)
    close(w, uid)
    wait_for(lambda: record(w, run["run"])["state"] == "exited")
    w.person_event("ticket.reopened", uid)
    _, again = _main(w, c, key)
    assert again["run"] != run["run"] and record(w, again["run"])["state"] == "running"


def test_an_ignored_polite_stop_becomes_a_hard_stop_after_the_grace(ready, monkeypatch, tmp_path):
    """The grace is the runner's: SIGHUP + SIGTERM, wait ``grace_ms``, SIGKILL. The close passes CLOSE_GRACE_MS."""
    sent = []
    monkeypatch.setattr(runners, "CLOSE_GRACE_MS", 300)
    real = runners.RunnerHub.command

    async def spy(self, cmd, args, *, timeout=None):
        sent.append((cmd, dict(args)))
        return await real(self, cmd, args, timeout=timeout)

    monkeypatch.setattr(runners.RunnerHub, "command", spy)
    w = ready
    c = w.conn()
    uid, key = w.ticket()
    _, run = _main(w, c, key)
    close(w, uid)
    wait_for(lambda: record(w, run["run"])["state"] == "exited")
    assert ("stop", {"run": run["run"], "grace_ms": 300}) in sent
