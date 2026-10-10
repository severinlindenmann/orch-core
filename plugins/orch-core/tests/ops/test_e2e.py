"""The real binary: a scripted agent session in a temp workspace, with dedup and the stop rule in effect.

Every call is ``python -m orch`` in a subprocess started in a subdirectory of the workspace (it is found by walking
up), with ``ORCH_STATE_DIR`` for the host state, ``ORCH_SESSION`` and ``ORCH_GRANT``. The human steps (approvals) are
signed in-process, as the person would on their own device."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time

import pytest

from tests.ops.helpers import OTHER, Ws
from tests.ops.test_task_ac import py

pytestmark = pytest.mark.slow


class Bin:
    def __init__(self, ws, session=None, grant=True):
        self.ws, self.session, self.grant = ws, session or "s_01J9ZP0000000000000000000S", grant
        self.cwd = ws.root / "tickets"  # anywhere below the workspace

    def __call__(self, *argv, stdin=None, env=None):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.ws.tmp), "ORCH_STATE_DIR": str(self.ws.host_state)}
        if self.session:
            e["ORCH_SESSION"] = self.session
        if self.grant:
            e["ORCH_GRANT"] = self.ws.grant
        e.update(env or {})
        p = subprocess.run(
            [sys.executable, "-m", "orch", *argv],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env=e,
            timeout=60,
        )
        p.doc = json.loads(p.stdout) if p.stdout.lstrip().startswith("{") else None
        return p


@pytest.fixture
def ws(tmp_path):
    w = Ws(tmp_path, live=True)  # the binary reads the wall clock, so the grant must be issued by it
    w.bootstrap()
    yield w
    w.store.close()


@pytest.fixture
def orch(ws):
    return Bin(ws)


def test_a_scripted_agent_session(ws, orch, tmp_path):
    r = orch("new", "Load tariff tables", "--priority", "high", "-m", "Load the tables.")
    assert r.returncode == 0 and r.stdout.startswith("ok DEMO-0001 ticket.created feature"), r.stderr
    assert orch("claim", "DEMO-0001").returncode == 0
    assert orch("status").stdout.splitlines()[0].startswith("ok status Owner cursor=")
    for sec in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        assert orch("section", "set", sec, "-m", f"text of {sec}").returncode == 0, sec
    assert orch("ac", "add", "the build is green").returncode == 0
    assert orch("task", "add", "run the build", "--verify", py("print('green')"), "--proves", "AC1").returncode == 0
    ws.human("approve", ws.uid("1"), "requirements")
    ws.human("approve", ws.uid("1"), "plan")
    r = orch("task", "next")
    assert r.returncode == 0 and "run the build" in r.stdout
    assert orch("task", "start", "T1").returncode == 0
    r = orch("task", "done", "T1", "--run", "-m", "green")
    assert r.returncode == 0 and "receipt=exit0/" in r.stdout and r.stdout.splitlines()[-1] == "next: orch submit", (
        r.stderr
    )
    log = tmp_path / "build.log"
    log.write_text("all green\n")
    assert orch("artifact", "add", str(log), "--ac", "AC1").returncode == 0
    r = orch("submit")
    assert r.returncode == 0 and "ticket.submitted testing" in r.stdout, r.stderr
    r = orch("ask", "Anything else?", "--options", "no,yes", "--rec", "no")
    assert r.returncode == 0 and "question.asked Q1" in r.stdout
    t = time.monotonic()
    r = orch("wait", "--timeout", "1", "--json")
    assert r.returncode == 0 and r.doc["data"]["kind"] == "timeout" and time.monotonic() - t < 15
    r = orch("wait", "--timeout", "1", "--strict-timeout", "--json")
    assert r.returncode == 7 and r.doc["error"]["code"] == "wait.timeout"
    r = orch("approve", "plan")  # a person's action: refused for an agent
    assert r.returncode == 3 and r.stderr.startswith("err human_only")
    # the log replays clean
    s = ws.other()
    try:
        s.load_all()
        assert s.chain_errors() == [] and s.state.workspace.invalid == ()
        v = s.ticket("DEMO-0001")
        assert v.status == "testing" and not v.frozen and all(a.evidence for a in v.acceptance)
        assert [e["type"] for e in ws.events("1")][-1] == "question.asked"
    finally:
        s.close()


def test_a_retry_is_answered_not_repeated_and_the_stop_rule_is_in_effect(ws, orch):
    orch("new", "A")
    orch("claim", "1")
    n = len(ws.events("1"))
    first = orch("log", "same note")
    again = orch("log", "same note")
    assert first.returncode == again.returncode == 0
    assert "duplicate" not in first.stdout and again.stdout.splitlines()[0].endswith(" duplicate")
    assert len(ws.events("1")) == n + 1
    other = orch("log", "same note", "--ref", "1", env={"ORCH_SESSION": OTHER})  # another session: its own call
    assert other.returncode == 0 and len(ws.events("1")) == n + 2
    for i in range(2):  # the same refusal ...
        r = orch("approve", "plan")
        assert r.returncode == 3 and r.stderr.startswith("err human_only"), i
    r = orch("approve", "plan")  # ... the third time is told to stop
    assert r.returncode == 3 and r.stderr.startswith("err stop STOP: report to the user")
    assert orch("approve", "requirements").stderr.startswith("err stop")  # human_only counts by operation
    assert orch("log", "a successful write resets it").returncode == 0
    assert orch("approve", "plan").stderr.startswith("err human_only")
    sessions = list((ws.root / ".state" / "sessions").glob("*.json"))
    assert sessions and not any(ws.grant.partition(".")[2] in p.read_text() for p in sessions)


def test_without_a_session_there_is_no_dedup_and_no_stop_rule(ws):
    o = Bin(ws)
    o.session = None
    r = o("new", "A")  # an agent event needs its session
    assert r.returncode == 5 and "ORCH_SESSION" in r.stderr
    for _ in range(5):
        assert o("approve", "plan").stderr.startswith("err human_only")


def test_the_workspace_is_found_by_walking_up_or_by_the_environment(ws, tmp_path):
    o = Bin(ws)
    o("new", "A")
    o.cwd = ws.root
    assert o("show", "1").returncode == 0
    o.cwd = tmp_path  # outside any workspace
    r = o("show", "1")
    assert r.returncode == 2 and r.stderr.startswith("err not_found no orch workspace here")
    assert o("show", "1", env={"ORCH_WORKSPACE": str(ws.root)}).returncode == 0
    assert o("show", "1", env={"ORCH_WORKSPACE": str(tmp_path)}).returncode == 2


def test_a_revoked_or_expired_grant_stops_every_write(ws, orch):
    orch("new", "A")
    ws.store.append(ws.person_event(ws.owner, "workspace", "grant.revoked", grant=ws.grant_id), log="workspace")
    r = orch("log", "x", "--ref", "1")
    assert r.returncode == 3 and r.stderr.startswith("err grant.expired")
    assert orch("show", "1").returncode == 0  # a read does not need one
    r = orch("claim", "1")
    assert r.returncode == 3


def test_help_needs_no_workspace_and_stays_fast(tmp_path, ws):
    o = Bin(ws)
    o.cwd = tmp_path
    t = time.monotonic()
    r = o("--help")
    assert r.returncode == 0 and time.monotonic() - t < 5
    assert o("describe", "claim").returncode == 0 and shlex.quote("x")
