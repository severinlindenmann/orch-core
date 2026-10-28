"""Manual schedules on a REAL host and a REAL foreground runner (dev sandbox workspace, fake harness), no model called.

The bring-up is the one of ``test_followups_real_host`` (dev sandbox prefix SBX: 30 s minimum lead, no early-fire
tolerance, 2 s timer tick). A person signs a once-schedule about a minute ahead; the host fires it at the occurrence with
a FRESH one-run grant, the session reads its brief through that grant (never argv) and the outcome is ``ran``.

Run alone, in a clean environment (never with the kit's ORCH_* variables):
    env -i HOME=$H PATH=$PATH uv run pytest tests/host/test_schedules_real_host_subprocess.py -q
"""

# ruff: noqa: E501
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.host.test_followups_real_host import DASH_BIN, Env

# The fake claude-code for a schedule run: records how it was started and reads its brief through the one-run grant.
HARNESS = r"""#!@PY@
import json, os, re, subprocess, sys
OUT = "@OUT@"
os.environ.update(@ENV@)
argv = sys.argv[1:]
prompt = next((a for a in argv if "schedule sc_" in a), "")
m = re.search(r"sc_[0-9A-Z]{26}", prompt)
sid = m.group(0) if m else None
def orch(*a):
    return subprocess.run([sys.executable, "-m", "orch", *a], capture_output=True, text=True, cwd=os.environ.get("ORCH_WORKSPACE") or None)
rec = {"argv": argv, "grant": bool(os.environ.get("ORCH_GRANT")), "ticket": os.environ.get("ORCH_TICKET"), "sc": sid}
shown = orch("schedule", "show", sid) if sid else None
rec["show_rc"] = shown.returncode if shown else None
rec["brief_seen"] = (shown.stdout + shown.stderr) if shown else ""
claim = orch("claim", rec["ticket"]) if rec["ticket"] else None  # a write, so the run counts as ran
rec["claim_rc"] = claim.returncode if claim else None
rec["claim_out"] = (claim.stdout + claim.stderr)[-300:] if claim else ""
with open(os.path.join(OUT, "run-%s.json" % (sid or os.getpid())), "w") as f:
    json.dump(rec, f)
"""


class ScheduleEnv(Env):
    def up(self) -> None:
        super().up()
        env = {
            k: v
            for k, v in self.ws.env().items()
            if k in ("HOME", "XDG_CONFIG_HOME", "ORCH_STATE_DIR", "ORCH_DEV", "ORCH_HOST_RUN_DIR")
        }
        fake = self.base / "bin" / "fu-fake-claude"  # the program runner.json names; read again at every spawn
        fake.write_text(
            HARNESS.replace("@PY@", sys.executable).replace("@OUT@", str(self.out)).replace("@ENV@", repr(env))
        )
        fake.chmod(0o700)

    def schedule(self, sid: str) -> dict:
        out = self.ws.unattended("schedule", "show", sid, "--json")
        doc = json.loads(out[out.index("{") :])
        return doc.get("data", doc)


@pytest.fixture
def env():
    if not DASH_BIN.is_file():
        pytest.skip(
            "needs the dashboard server venv: cd packages/addons/dashboard/server; ORCH_DASHBOARD_SKIP_UI=1 uv sync --frozen"
        )
    base = Path(tempfile.mkdtemp(prefix=".tsc-", dir=Path.home()))
    e = ScheduleEnv(base)
    try:
        e.up()
        yield e
    finally:
        e.down()
        if os.environ.get("E2_KEEP") != "1":
            shutil.rmtree(base, ignore_errors=True)


@pytest.mark.slow
def test_once_schedule_fires_with_a_fresh_grant_on_a_real_host(env):
    ws, kit = env.ws, env.kit
    # an approved, unclaimed ticket (the scheduled session claims it: its write, so the outcome is ran)
    title = "scheduled once on a real host"
    key = kit.key_of(ws.agent("new", title, "--type", "feature"))
    ws.agent("ac", "add", "it is done", "--ref", key)
    kit.ready_requirements(ws, key, title)
    ws.human("approve", key, "requirements")
    kit.ready_plan(ws, key)
    ws.agent("task", "add", "do it", "--verify", "echo ok", "--proves", "AC1", "--ref", key)
    ws.human("approve", key, "plan")
    due = (int(time.time()) + 45) // 60 * 60 + 60  # the next whole minute at least 45 s away (the dev lead is 30 s)
    at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(due))
    out = ws.human(
        "schedule", "add", key, "--at", at, "--tz", "UTC", "--run-hours", "1", "--brief", "SECRET-SCHEDULE-BRIEF do it"
    )
    sid = re.search(r"schedule\.set (sc_[0-9A-Z]{26})", out).group(1)
    rec = env.schedule(sid)
    assert rec["state"] == "live" and not rec["occurrences"], rec

    occurrence = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(due))
    deadline, row = due + 150, {}
    while time.time() < deadline:
        row = env.schedule(sid)["occurrences"].get(occurrence) or {}
        if row.get("outcome"):
            break
        time.sleep(2)
    record = env.out / f"run-{sid}.json"
    seen = record.read_text() if record.exists() else "no harness record"
    assert row.get("outcome") == "ran", f"{row}\n{seen}\n{ws.log.read_text()[-1500:]}"
    # a fresh one-run grant minted at firing time, never the schedule's root (the set event)
    assert row["grant"] and row["grant"] != rec.get("set_event") and row["run"].startswith("r_"), row

    h = json.loads((env.out / f"run-{sid}.json").read_text())
    assert h["grant"] is True and h["ticket"] == key and h["show_rc"] == 0 and h["claim_rc"] == 0, h
    assert "SECRET-SCHEDULE-BRIEF" in h["brief_seen"], h
    assert not any("SECRET-SCHEDULE-BRIEF" in a for a in h["argv"]), h["argv"]  # the brief is never in argv
    # exactly one firing for the occurrence
    assert list(env.schedule(sid)["occurrences"]) == [occurrence]
