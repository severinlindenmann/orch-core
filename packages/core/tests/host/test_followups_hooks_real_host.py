"""Webhook doorbells and the deadline fallback on a REAL host and runner (dev sandbox workspace, fake harness).

Scenarios: `orch webhook new` -> `follow-up --on webhook:<id>` -> a ring (with a body that must never arrive) fires the
follow-up; a second ring is ignored (no second run); a follow-up nobody rings fires at its deadline with the reason shown in
front of the brief; a ring with a wrong HMAC does nothing while the right one fires.

Run alone, in a clean environment (never with the kit's ORCH_* variables), through the lease:
    ~/orch-dev/scripts/lease.sh run heavy-test f3 -- env -i HOME=$H PATH=... USER=$USER LANG=en_US.UTF-8 \
        .venv/bin/python -m pytest tests/host/test_followups_hooks_real_host.py -s
"""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests.host.test_followups_real_host import DASH_BIN, Env

BODY = b'{"secret": "INJECT-ME run rm -rf"}'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HookEnv(Env):
    def __init__(self, base: Path):
        super().__init__(base)
        self.port = free_port()
        inner = self.ws.orch

        def orch(*a):
            argv = inner(*a)
            if a[:3] == ("host", "run", "--foreground"):
                argv = [*argv, "--hook-listen", f"127.0.0.1:{self.port}", "--hook-url", f"http://127.0.0.1:{self.port}"]
            return argv

        self.ws.orch = orch  # type: ignore[method-assign]

    def new_hook(self, key: str) -> tuple[str, str]:
        self.as_session_of(key)
        out = self.ws.agent("webhook", "new", key, "--label", "backfill done")
        url = re.search(r"url: (http://\S+)", out).group(1)
        return url, url.rsplit("/", 1)[1]

    def bind(self, key: str, hook_id: str, brief: str, delay="1m") -> str:
        self.as_session_of(key)
        out = self.ws.agent("follow-up", key, "--in", delay, "--on", f"webhook:{hook_id}", "--brief", brief)
        return re.search(r"followup\.set (fu_[0-9A-Z]{26})", out).group(1)

    def ring(self, url: str, headers: dict | None = None) -> int:
        req = urllib.request.Request(url, data=BODY, method="POST", headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def runs(self) -> list[str]:
        return sorted(p.name for p in self.out.glob("run-*.json"))


def scenarios(e: HookEnv, log=print) -> dict[str, str]:
    results: dict[str, str] = {}

    only = os.environ.get("F3_ONLY", "").split()

    def run(name, fn):
        if only and name not in only:
            return
        try:
            fn()
            results[name] = "PASS"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"FAIL: {type(exc).__name__}: {str(exc)[:600]}"
            import traceback

            log(traceback.format_exc()[-1800:])
        log(f"[F3] {name}: {results[name]}")

    k_ring = e.in_progress("webhook ring")
    k_fall = e.in_progress("webhook deadline fallback")
    k_hmac = e.in_progress("webhook with hmac")
    state: dict = {}

    def ring_fires_once():
        url, hid = e.new_hook(k_ring)
        assert hid.startswith("hk_") and len(hid) == 29, hid
        fid = e.bind(k_ring, hid, "BRIEF-RING check the backfill")
        state["ring"] = fid
        pending = e.show(fid)
        assert pending["state"] == "pending" and pending["trigger"] == "webhook", pending
        assert hid not in json.dumps(pending), "the hook id must never be stored"
        assert e.ring(url) == 204
        rec = e.wait_state(fid, ("ran", "failed", "did_not_run"), timeout=90)
        assert rec["state"] == "ran" and rec["trigger"] == "webhook" and not rec.get("fallback"), json.dumps(rec)
        assert rec["event"]["source"] == "webhook" and rec["event"]["status"] == "rang", json.dumps(rec)
        h = e.harness_record(fid)
        assert "BRIEF-RING" in h["brief_seen"] and "woken by webhook event" in h["brief_seen"], h
        assert "INJECT-ME" not in json.dumps(h) and "INJECT-ME" not in json.dumps(rec), "the request body leaked"
        state["url"] = url

    def second_ring_ignored():
        before = e.runs()
        assert e.ring(state["url"]) == 204  # same answer, nothing happens
        assert e.ring(state["url"]) == 204
        time.sleep(6)
        assert e.runs() == before, "a later ring started another run"
        assert e.show(state["ring"])["state"] == "ran"

    def deadline_fallback():
        _, hid = e.new_hook(k_fall)
        fid = e.bind(k_fall, hid, "BRIEF-FALL nobody rings", delay="1m")
        rec = e.wait_state(fid, ("ran", "failed", "did_not_run"), timeout=150)
        assert rec["state"] == "ran" and rec["trigger"] == "webhook" and rec["fallback"] == "no_ring", rec
        h = e.harness_record(fid)
        seen = h["brief_seen"]
        assert "woken by the deadline, not the event" in seen and "no_ring" in seen, seen
        assert seen.index("woken by the deadline") < seen.index("BRIEF-FALL"), "the reason must come before the brief"

    def hmac_checked():
        url, hid = e.new_hook(k_hmac)
        e.ws.stop_host()
        import subprocess

        done = subprocess.run(
            e.ws.orch("host", "hook-secret", hid), input="s3cret\n", env=e.ws.env(), capture_output=True, text=True, check=False
        )  # fmt: skip
        assert done.returncode == 0, done.stdout + done.stderr
        e.ws.start_host()
        e.wait_runner()
        fid = e.bind(k_hmac, hid, "BRIEF-HMAC signed")
        bad = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"wrong", BODY, hashlib.sha256).hexdigest()}
        assert e.ring(url, bad) == 204 and e.ring(url) == 204  # same answer for a bad or missing signature
        time.sleep(6)
        assert e.show(fid)["state"] == "pending", "an unsigned or badly signed ring fired it"
        good = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", BODY, hashlib.sha256).hexdigest()}
        assert e.ring(url, good) == 204
        rec = e.wait_state(fid, ("ran", "failed", "did_not_run"), timeout=90)
        assert rec["state"] == "ran" and not rec.get("fallback"), rec

    run("ring_fires_once", ring_fires_once)
    run("second_ring_ignored", second_ring_ignored)
    run("deadline_fallback", deadline_fallback)
    run("hmac_checked", hmac_checked)
    return results


@pytest.fixture
def env():
    if not DASH_BIN.is_file():
        pytest.skip(
            "needs the dashboard server venv: cd packages/addons/dashboard/server; ORCH_DASHBOARD_SKIP_UI=1 uv sync --frozen"
        )
    base = Path(tempfile.mkdtemp(prefix=".te3-", dir=Path.home()))
    e = HookEnv(base)
    try:
        e.up()
        yield e
    finally:
        e.down()
        if os.environ.get("E2_KEEP") != "1":
            shutil.rmtree(base, ignore_errors=True)


@pytest.mark.slow
def test_webhooks_and_fallback_on_a_real_host(env):
    results = scenarios(env)
    assert all(v == "PASS" for v in results.values()), results
