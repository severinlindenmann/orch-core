"""Follow-ups on a REAL host and a REAL foreground runner (dev sandbox workspace, fake harness), no model called.

Scenarios (one bring-up, about 3 minutes, so ``slow``): set -> the host fires -> a fresh session starts and reads its brief
through its one-run grant (never argv) -> outcome ran; cancel; host restart between set and due (fires late); a chained
follow-up set by the follow-up run itself; the add-on turned off (derived cancel, nothing fires).

The dev shortcuts it relies on exist only in a dev sandbox workspace (signed genesis prefix SBX): a 30 s minimum lead, no
early-fire tolerance, a 2 s timer tick. ``test_dev_shortcuts_ignored_on_a_real_workspace`` pins that.

Run alone, in a clean environment (never with the kit's ORCH_* variables):
    env -i HOME=$H PATH=... USER=$USER LANG=en_US.UTF-8 .venv/bin/python -m pytest tests/host/test_followups_real_host.py -s
"""

# ruff: noqa: E501
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from orch.model import followups as fu

PACKAGES = Path(__file__).resolve().parents[3]
KIT_TESTS = PACKAGES / "addons" / "dashboard" / "server" / "tests"
DASH_BIN = PACKAGES / "addons" / "dashboard" / "server" / ".venv" / "bin" / "orch-dashboard"
SANDBOX_ID = "e2f00001"

# The fake claude-code: records how it was started, reads its brief through the one-run grant, claims the ticket (a write),
# and, when the brief says CHAIN, sets the next follow-up. @PY@ etc. are filled in by the test.
HARNESS = r"""#!@PY@
import json, os, re, subprocess, sys
OUT = "@OUT@"
os.environ.update(@ENV@)
argv = sys.argv[1:]
prompt = next((a for a in argv if "follow-up fu_" in a), "")
m = re.search(r"fu_[0-9A-Z]{26}", prompt)
fid = m.group(0) if m else None
key = os.environ.get("ORCH_TICKET")
def orch(*a):
    return subprocess.run([sys.executable, "-m", "orch", *a], capture_output=True, text=True, cwd=os.environ.get("ORCH_WORKSPACE") or None)
rec = {"argv": argv, "grant": bool(os.environ.get("ORCH_GRANT")), "session": os.environ.get("ORCH_SESSION"), "ticket": key, "fu": fid}
shown = orch("follow-up", "show", fid) if fid else None
rec["show_rc"] = shown.returncode if shown else None
brief = shown.stdout if shown else ""
rec["brief_seen"] = brief
claim = orch("claim", key) if key else None
rec["claim_rc"] = claim.returncode if claim else None
rec["claim_out"] = (claim.stdout + claim.stderr)[-300:] if claim else ""
if "CHAIN" in brief and key:
    nxt = orch("follow-up", key, "--in", "1m", "--brief", "second leg of the chain")
    rec["chain_rc"], rec["chain_out"] = nxt.returncode, (nxt.stdout + nxt.stderr)[-300:]
with open(os.path.join(OUT, "run-%s.json" % (fid or os.getpid())), "w") as f:
    json.dump(rec, f)
"""


def _kit():
    sys.path.insert(0, str(KIT_TESTS))
    import realhost_kit  # noqa: PLC0415

    return realhost_kit


class Env:
    """A dev-sandbox workspace with a --dev-runner host and a foreground runner running the fake harness."""

    def __init__(self, base: Path):
        from orch import devsandbox

        kit = _kit()
        self.kit, self.base = kit, base
        self.ws = ws = kit.Workspace(
            base / "ws", prefix="SBX", dirname="ws", owner=devsandbox.owner_name(SANDBOX_ID), state_name="state"
        )
        plain = ws.orch

        def orch(*a):
            if a[:3] != ("host", "run", "--foreground"):
                return plain(*a)
            # the host with INFO logging (the follow-up timer logs fired / outcome lines), as a dev-runner dev host
            boot = "import logging,sys;logging.basicConfig(level=logging.INFO);sys.argv[0]='orch';from orch.cli.main import entry;sys.exit(entry())"
            return [sys.executable, "-W", "ignore", "-c", boot, *a, "--dev-sim-convenience", "--dev-runner"]

        ws.orch = orch  # type: ignore[method-assign]
        self.out = base / "harness-out"
        self.runner: subprocess.Popen | None = None
        self.n = 0
        self.cancelled: str | None = None
        self.sessions: dict[str, str] = {}

    # -- bring-up --------------------------------------------------------------------------------------------------
    def up(self) -> None:
        from orch.runner import enablement

        ws, kit = self.ws, self.kit
        kit.sandbox_bring_up(ws, SANDBOX_ID)
        ws.grant()
        ws.human("addon", "grant", "schedules")
        self.out.mkdir(mode=0o700)
        bindir = self.base / "bin"
        bindir.mkdir(mode=0o700)
        env = {
            k: v
            for k, v in ws.env().items()
            if k in ("HOME", "XDG_CONFIG_HOME", "ORCH_STATE_DIR", "ORCH_DEV", "ORCH_HOST_RUN_DIR")
        }
        fake = bindir / "fu-fake-claude"
        fake.write_text(
            HARNESS.replace("@PY@", sys.executable).replace("@OUT@", str(self.out)).replace("@ENV@", repr(env))
        )
        fake.chmod(0o700)
        cfg = ws.state / "runner.json"
        cfg.write_text(json.dumps({"v": 1, "login_shell": False, "programs": {"claude-code": str(fake)}}))
        cfg.chmod(0o600)
        enablement.set_enabled(
            ws.state, ws.ws_id, True, os.getuid(), terminals=True, text=enablement.risk_text("SBX", ws.ws_id, True)
        )
        log = open(self.base / "runner.log", "ab")
        self.runner = subprocess.Popen(
            ws.orch("runner", "run", "--foreground"),
            env={**ws.env(), "ORCH_NO_SERVICE_MANAGER": "1", "SHELL": "/bin/sh"},
            cwd=ws.root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
        self.wait_runner()

    def wait_runner(self, timeout=60) -> None:
        """Until the host sees the runner connected and enabled (asked through the dashboard server's own venv gateway)."""
        probe = (
            "import json,sys\nfrom orch_dashboard.gateway import Gateway\n"
            "from pathlib import Path\ng=Gateway(Path(sys.argv[1]))\n"
            "print(json.dumps(g.link(sys.argv[2]).call('whoami').get('runner') or {}))\ng.close()\n"
        )
        deadline, who = time.time() + timeout, ""
        while time.time() < deadline:
            assert self.runner is not None and self.runner.poll() is None, (self.base / "runner.log").read_text()[
                -1500:
            ]
            done = subprocess.run(
                [str(DASH_BIN.parent / "python"), "-c", probe, str(self.ws.state), self.ws.ws_id],
                env=self.ws.env(), capture_output=True, text=True, timeout=30, check=False,
            )  # fmt: skip
            who = done.stdout.strip() or done.stderr[-300:]
            with contextlib.suppress(ValueError):
                info = json.loads(who)
                if info.get("connected") and info.get("enabled"):
                    return
            time.sleep(0.5)
        raise AssertionError(f"the runner never connected: {who}\n{(self.base / 'runner.log').read_text()[-1500:]}")

    def down(self) -> None:
        if self.runner is not None and self.runner.poll() is None:
            self.runner.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.runner.wait(timeout=10)
        self.ws.stop_host()
        with contextlib.suppress(Exception):
            self.ws.stop_dashboard()

    # -- tickets and follow-ups --------------------------------------------------------------------------------------
    def in_progress(self, title: str) -> str:
        """A ticket claimed by its own agent session (one session holds one claim)."""
        ws, kit = self.ws, self.kit
        self.n += 1
        key = kit.key_of(ws.agent("new", title, "--type", "feature"))
        ws.agent("ac", "add", "it is done", "--ref", key)
        kit.ready_requirements(ws, key, title)
        ws.human("approve", key, "requirements")
        kit.ready_plan(ws, key)
        ws.agent("task", "add", "do it", "--verify", "echo ok", "--proves", "AC1", "--ref", key)
        ws.human("approve", key, "plan")
        kit.new_session(ws, 10 + self.n)
        ws.agent("claim", key)
        self.sessions[key] = ws.session
        return key

    def as_session_of(self, key: str) -> None:
        self.ws.session = self.sessions[key]

    def set_fu(self, key: str, delay: str, brief: str) -> str:
        self.as_session_of(key)
        out = self.ws.agent("follow-up", key, "--in", delay, "--brief", brief)
        return re.search(r"followup\.set (fu_[0-9A-Z]{26})", out).group(1)

    def show(self, fid: str) -> dict:
        out = self.ws.unattended("follow-up", "show", fid, "--json")
        doc = json.loads(out[out.index("{") :])
        return doc.get("data", doc)

    def wait_state(self, fid: str, states, timeout=120) -> dict:
        deadline, rec = time.time() + timeout, {}
        while time.time() < deadline:
            rec = self.show(fid)
            if rec["state"] in states:
                return rec
            time.sleep(1)
        raise AssertionError(f"{fid} never reached {states}: {rec}\n{self.ws.log.read_text()[-2000:]}")

    def harness_record(self, fid: str) -> dict:
        return json.loads((self.out / f"run-{fid}.json").read_text())

    def restart_host(self, after: float = 0) -> None:
        self.ws.stop_host()
        time.sleep(after)
        self.ws.start_host()


def scenarios(e: Env, log=print) -> dict[str, str]:
    """Run every scenario; returns {name: 'PASS' | 'FAIL: why'}. A failure never hides the later scenarios."""
    results: dict[str, str] = {}

    only = os.environ.get("E2_ONLY", "").split()

    def run(name, fn):
        if only and name not in only:
            return
        try:
            fn()
            results[name] = "PASS"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"FAIL: {type(exc).__name__}: {str(exc)[:600]}"
        log(f"[E2] {name}: {results[name]}")

    started = {}
    started["ran"] = e.in_progress("follow-up ran")
    started["cancel"] = e.in_progress("follow-up cancelled")
    started["restart"] = e.in_progress("follow-up across a host restart")
    started["chain"] = e.in_progress("chained follow-up")
    started["off"] = e.in_progress("follow-up with the add-on turned off")
    t0 = time.time()
    ids = {
        "ran": e.set_fu(started["ran"], "1m", "SECRET-BRIEF-RAN do the first thing"),
        "cancel": e.set_fu(started["cancel"], "1m", "BRIEF-CANCEL never runs"),
        "chain": e.set_fu(started["chain"], "1m", "CHAIN first leg"),
        "restart": e.set_fu(started["restart"], "2m", "BRIEF-RESTART fires late"),
    }

    def ran():
        fid = ids["ran"]
        pending = e.show(fid)
        assert pending["state"] == "pending", pending
        rec = e.wait_state(fid, ("ran", "failed", "did_not_run"))
        assert rec["state"] == "ran" and rec["outcome"] == "ran", rec
        h = e.harness_record(fid)
        assert h["grant"] is True and h["show_rc"] == 0 and h["claim_rc"] == 0, h
        assert "SECRET-BRIEF-RAN" in h["brief_seen"], h
        assert not any("SECRET-BRIEF-RAN" in a for a in h["argv"]), h["argv"]  # the brief is never in argv
        assert rec["run"] and rec["run"].startswith("r_"), rec

    def cancel():
        fid = ids["cancel"]
        e.as_session_of(started["cancel"])
        out = e.ws.agent("follow-up", "cancel", fid)
        assert "cancelled" in out, out
        rec = e.show(fid)
        assert rec["state"] == "cancelled", rec
        e.cancelled = fid

    second: dict = {}

    def chain_first_leg():
        first = e.wait_state(ids["chain"], ("ran", "failed", "did_not_run"))
        assert first["state"] == "ran", first
        h = e.harness_record(ids["chain"])
        assert h["chain_rc"] == 0, h  # the follow-up run itself set the next one (its claim was the run's own)
        listing = json.loads(e.ws.unattended("follow-up", "list", started["chain"], "--json"))
        rows = listing["data"]["rows"]
        pending = [r for r in rows if r["id"] != ids["chain"] and r["state"] == "pending"]
        assert len(pending) == 1, rows
        second["id"] = pending[0]["id"]

    def restart():
        # the host is down across the due times of the restart ticket and the second chain leg: both fire late
        wait = max(0, 2 * 60 + 15 - (time.time() - t0))
        e.ws.stop_host()
        time.sleep(wait)
        e.ws.start_host()
        e.wait_runner()
        rec = e.wait_state(ids["restart"], ("ran", "failed", "did_not_run"), timeout=90)
        assert rec["state"] == "ran", rec
        assert (rec.get("late_by") or 0) > 0, rec

    def chain_second_leg():
        done = e.wait_state(second["id"], ("ran", "failed", "did_not_run"), timeout=90)
        assert done["state"] == "ran", done
        assert "second leg" in e.harness_record(done["id"])["brief_seen"]

    run("cancel", cancel)
    run("ran", ran)
    run("chain_first_leg", chain_first_leg)
    run("restart", restart)
    run("chain_second_leg", chain_second_leg)

    def cancel_held():
        assert e.cancelled and not (e.out / f"run-{e.cancelled}.json").exists(), "a cancelled follow-up ran"
        assert e.show(e.cancelled)["state"] == "cancelled"

    run("cancel_never_ran", cancel_held)

    def off():
        key = started["off"]
        fid = e.set_fu(key, "1m", "BRIEF-OFF never runs")
        t_off = time.time()
        e.ws.human("addon", "disable", "schedules")
        refused = e.ws.unattended("follow-up", "show", fid, ok=False)
        assert "addon.off" in refused, refused  # every follow-up verb says so while the add-on is off
        time.sleep(max(0, 60 + 10 - (time.time() - t_off)))  # past its due time, with the add-on off
        assert not (e.out / f"run-{fid}.json").exists(), "a follow-up fired with the add-on off"

    run("addon_off", off)
    return results


@pytest.fixture
def env():
    if not DASH_BIN.is_file():
        pytest.skip(
            "needs the dashboard server venv: cd packages/addons/dashboard/server; ORCH_DASHBOARD_SKIP_UI=1 uv sync --frozen"  # noqa: E501
        )
    base = Path(tempfile.mkdtemp(prefix=".te2-", dir=Path.home()))
    e = Env(base)
    try:
        e.up()
        yield e
    finally:
        e.down()
        if os.environ.get("E2_KEEP") != "1":
            shutil.rmtree(base, ignore_errors=True)


@pytest.mark.slow
def test_followups_on_a_real_host_and_runner(env):
    results = scenarios(env)
    assert all(v == "PASS" for v in results.values()), results


def test_dev_shortcuts_ignored_on_a_real_workspace():
    """The 30 s lead and the zero early tolerance are dev-sandbox-only: any other prefix keeps the code ceilings."""
    from orch.model.followups_wire import caps_of

    class Ws:
        settings: dict = {}

        def __init__(self, prefix):
            self.prefix = prefix

    real = caps_of(Ws("DEMO"))
    assert (real.min_lead_s, real.early_s) == (fu.FU_MIN_LEAD_S, fu.FU_TIME_EARLY_S)
    dev = caps_of(Ws("SBX"))
    assert (dev.min_lead_s, dev.early_s) == (fu.DEV_MIN_LEAD_S, fu.DEV_EARLY_S)
