"""The whole loop with real processes: an agent claims and works a ticket, a person approves on a real (pseudo)
terminal, the agent submits, the person gives the verify verdict, the ticket is done, and a fresh store replays the
signed log clean.

The person's side runs under a pty: ``python -m orch`` is started in a new session whose controlling terminal is the
pty, so ``/dev/tty`` is a real terminal and the passphrase is typed there (and nowhere else). The agent's side is the
same binary with ``ORCH_GRANT`` and ``ORCH_SESSION``, no terminal. Nothing is injected into either process."""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import termios
import time

import pytest

from orch import canon, crypto
from tests.ops.humans import PASSPHRASE
from tests.ops.test_task_ac import py
from tests.store.helpers import WS

pytestmark = pytest.mark.slow
SESSION = "s_01J9ZP0000000000000000000S"


def agent_bin(ws, grant, *argv, stdin=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(ws.tmp), "ORCH_STATE_DIR": str(ws.host_state), "ORCH_SESSION": SESSION}
    env["ORCH_GRANT"] = grant
    p = subprocess.run(
        [sys.executable, "-m", "orch", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=ws.root / "tickets",
        env=env,
        timeout=60,
        start_new_session=True,
    )
    return p


def person(ws, *argv, passphrase=PASSPHRASE):
    """Run ``orch`` as the person at a terminal. Returns ``(exit code, stdout, stderr, what the terminal showed)``."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(ws.tmp), "ORCH_STATE_DIR": str(ws.host_state), "TERM": "dumb"}
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    pid, tty = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pty; stdout and stderr go to pipes, not to the terminal
        try:
            os.dup2(out_w, 1)
            os.dup2(err_w, 2)
            os.chdir(ws.root / "tickets")
            os.execve(sys.executable, [sys.executable, "-m", "orch", *argv], env)
        finally:
            os._exit(127)
    os.close(out_w)
    os.close(err_w)
    shown = b""
    typed = False
    deadline = time.time() + 30
    while time.time() < deadline:  # until the terminal closes (the command ended) or the time is up
        ready, _, _ = select.select([tty], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(tty, 65536)
            except OSError:  # EIO: the other side of the terminal is gone
                break
            if not chunk:
                break
            shown += chunk
            if not typed and b"Passphrase: " in shown:
                # a person types after the prompt is up and the terminal no longer echoes (the prompt flushes input
                # when it switches echo off): wait for that, as a person's reaction time would
                for _ in range(100):
                    if not termios.tcgetattr(tty)[3] & termios.ECHO:
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("the terminal still echoes while the passphrase is asked")
                os.write(tty, passphrase.encode() + b"\n")
                typed = True
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise AssertionError(f"the command did not finish; the terminal showed {shown!r}")
    _, status = os.waitpid(pid, 0)
    out, err = os.read(out_r, 1 << 20).decode(), os.read(err_r, 1 << 20).decode()
    for fd in (tty, out_r, err_r):
        os.close(fd)
    return os.waitstatus_to_exitcode(status), out, err, shown.decode(errors="replace")


@pytest.fixture
def ws(live_hws):
    return live_hws


def test_agent_works_person_signs_ticket_done_and_the_log_replays_clean(ws, tmp_path):
    repo = ws.repo()
    head = ws.git("rev-parse", "HEAD")
    # the person issues the grant on their terminal and reads the secret there; the agent uses it
    code, out, err, term = person(ws, "grant", "--label", "e2e")
    assert code == 0, (out, err, term)
    m = re.search(r"ORCH_GRANT=(gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43})", term)
    assert m, term
    grant = m.group(1)
    assert grant not in out + err and "Passphrase: " in term and "type: grant.issued" in term
    assert PASSPHRASE not in out + err + term  # echo is off: the passphrase is not shown back

    def agent(*argv, stdin=None):
        p = agent_bin(ws, grant, *argv, stdin=stdin)
        assert p.returncode == 0, (argv, p.stdout, p.stderr)
        return p

    key = agent("new", "Load the tariff tables", "-m", "Load them.").stdout.split()[1]
    agent("claim", key)
    for sec in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        agent("section", "set", sec, "-m", f"text of {sec}")
    agent("ac", "add", "the build is green")
    agent("set", key, 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}')
    agent("task", "add", "run it", "--verify", py("print('green')"), "--proves", "AC1")

    for gate in ("requirements", "plan"):
        code, out, err, term = person(ws, "approve", gate, "--ref", key)
        assert code == 0, (out, err, term)
        assert out.startswith(f"ok {key} gate.approved {gate} seq=") and f"gate: {gate}" in term
        assert "type: gate.approved" in term and "auth: passphrase" in term

    agent("task", "start", "T1")
    assert "receipt=exit0/" in agent("task", "done", "T1", "--run", "-m", "green").stdout
    log = tmp_path / "build.log"
    log.write_text("all green\n")
    agent("artifact", "add", str(log), "--ac", "AC1")
    agent("submit")
    waited = json.loads(agent("wait", "--timeout", "1", "--json").stdout)
    assert waited["data"]["kind"] in ("approved", "timeout")

    code, out, err, term = person(ws, "verdict", "pass", "--ref", key)
    assert code == 0, (out, err, term)
    assert out.startswith(f"ok {key} verdict.given pass seq=")
    assert f"source_sha[1].sha: {head}" in term and "outcome: pass" in term

    # a wrong passphrase on the same terminal writes nothing
    before = len(ws.events("1"))
    code, out, err, term = person(ws, "reopen", key, passphrase="not it")
    assert code == 3 and "custody.wrong_passphrase" in err and len(ws.events("1")) == before

    # the log: signed person events with the device key, replayed clean by a fresh store
    events = ws.events("1")
    kinds = [(e["type"], e["actor"]["kind"]) for e in events]
    assert ("gate.approved", "person") in kinds and ("verdict.given", "person") in kinds
    uid = ws.uid("1")
    for e in events:
        if e["actor"]["kind"] == "person":
            assert e["auth"] == "passphrase" and e["actor"]["device"] == ws.owner.device
            signed = canon.person_signing_bytes(WS, uid, e)
            assert crypto.verify(ws.owner.sig_pub, crypto.unb64u(e["sig"], 64), signed), e["type"]
        else:
            assert "sig" not in e
    fresh = ws.other()
    try:
        assert fresh.chain_errors() == []
        v = fresh.ticket(key)
        assert v.status == "done" and v.gates["requirements"].approved and v.gates["plan"].approved
        assert v.gates["verify"].approved and [dict(x)["sha"] for x in v.source_list] == [head]
        assert fresh.state.workspace.invalid == ()
    finally:
        fresh.close()
    assert canon is not None and repo.exists()
