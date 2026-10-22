"""release.yml: triggers, environment, secret handling and action pinning (parsed, not run)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SCRIPTS = ROOT / "scripts" / "release"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers(wf: dict) -> dict:
    return wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as True


def steps(wf: dict):
    for name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


def test_triggers(wf):
    on = triggers(wf)
    assert set(on) == {"push", "workflow_dispatch", "schedule"}
    assert on["push"]["branches"] == ["main", "develop"]
    assert "channel" in on["workflow_dispatch"]["inputs"]
    assert on["schedule"][0]["cron"].split()[4] != "*"  # weekly: a fixed weekday


def test_protected_environment_and_least_privilege(wf):
    jobs = wf["jobs"]
    assert "'release'" in jobs["publish"]["environment"]
    assert jobs["refresh"]["environment"] == "release"
    assert wf["permissions"] == {}
    for name, job in jobs.items():
        perms = job.get("permissions", {})
        if name in ("publish", "refresh"):
            assert perms == {"contents": "write"}
        else:
            assert perms.get("contents", "read") == "read", name
    # only the two signing jobs live in the environment
    assert {n for n, j in jobs.items() if "environment" in j} == {"publish", "refresh"}


def test_concurrency_per_channel_without_cancel(wf):
    for name in ("publish", "refresh"):
        c = wf["jobs"][name]["concurrency"]
        assert c["group"].startswith("release-")
        assert c["cancel-in-progress"] is False


def test_secret_only_in_sign_steps_and_never_echoed(wf):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.") == 2
    for job, step in steps(wf):
        run = step.get("run", "")
        env = step.get("env", {})
        if any("secrets." in str(v) for v in env.values()):
            assert step["name"].startswith("Sign"), (job, step)
            assert "echo" not in run and "set -x" not in run and "printenv" not in run
        assert "secrets." not in run
        assert "ORCH_RELEASE_SIGNING_KEY" not in run
    assert "set -x" not in text and "ACTIONS_STEP_DEBUG" not in text
    # job-level and workflow-level env carry no secret
    assert "secrets." not in json.dumps({k: v for k, v in wf.items() if k != "jobs"})
    for job in wf["jobs"].values():
        assert "secrets." not in json.dumps(job.get("env", {}))


def test_no_event_text_interpolated_into_scripts(wf):
    unsafe = re.compile(r"\$\{\{\s*(github\.event\.|github\.head_ref|inputs\.|github\.ref_name)")
    for job, step in steps(wf):
        assert not unsafe.search(step.get("run", "")), (job, step.get("name"))


def test_every_action_pinned_by_sha(wf):
    seen = 0
    for _job, step in steps(wf):
        if "uses" in step:
            seen += 1
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert seen >= 6


def test_build_order_and_release_steps(wf):
    build = "\n".join(s.get("run", "") for s in wf["jobs"]["build"]["steps"])
    order = ["npm run build", "stamp.py", "uv build", "check_wheels.py", "constraints.txt"]
    pos = [build.index(x) for x in order]
    assert pos == sorted(pos)
    assert "ORCH_DASHBOARD_REQUIRE_PREBUILT" in json.dumps(wf["jobs"]["build"])
    pub = "\n".join(s.get("run", "") for s in wf["jobs"]["publish"]["steps"])
    assert pub.index("manifest.py") < pub.index("sign.py") < pub.index("gh release create")
    assert "--clobber" in pub and "v${VERSION}" in pub and "channel-${CHANNEL}" in pub
    assert wf["jobs"]["plan"]["steps"][0]["env"]["RUN_NUMBER"] == "${{ github.run_number }}"
    assert "2.0.0.dev${RUN_NUMBER}" in WORKFLOW.read_text(encoding="utf-8")


def test_cert_check_warns_at_30_days_and_gates_release(wf):
    assert wf["env"]["WARN_DAYS"] == "30"
    for job in ("build", "refresh"):
        assert "cert-check" in wf["jobs"][job]["needs"]
    assert "::warning::" in json.dumps(wf["jobs"]["cert-check"])


def _cert(not_after: str) -> str:
    cert = base64.b64encode(json.dumps({"not_after": not_after}).encode()).decode()
    return json.dumps({"cert": cert, "root_key_id": "root-1", "sig": "x"})


def test_cert_expiry_script(tmp_path):
    f = tmp_path / "k.json"
    f.write_text(_cert("2026-12-01T00:00:00Z"))
    run = lambda now: subprocess.run(  # noqa: E731
        [sys.executable, "-I", str(SCRIPTS / "cert_expiry.py"), str(f), "--now", now], capture_output=True, text=True
    )
    ok = run("2026-10-22T00:00:00Z")
    assert ok.returncode == 0 and "days_left=40" in ok.stdout
    soon = run("2026-11-10T00:00:00Z")
    assert soon.returncode == 0 and "days_left=21" in soon.stdout
    assert run("2026-12-01T00:00:00Z").returncode == 1
    f.write_text("{}")
    assert run("2026-10-22T00:00:00Z").returncode == 1  # unreadable fails closed


def test_refresh_script_changes_only_times(tmp_path):
    doc = {
        "format": "orch.release/1",
        "channel": "develop",
        "seq": 7,
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-22T00:00:00Z",
        "version": "2.0.0.dev7",
    }
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    env = {"format": "orch.release-envelope/1", "payload": base64.b64encode(payload).decode()}
    (tmp_path / "e.json").write_text(json.dumps(env))
    args = [
        sys.executable,
        "-I",
        str(SCRIPTS / "refresh.py"),
        "--envelope",
        str(tmp_path / "e.json"),
        "--issued-at",
        "2026-02-01T00:00:00Z",
        "--expires-at",
        "2026-02-22T00:00:00Z",
        "--out",
        str(tmp_path / "o"),
    ]
    assert subprocess.run([*args, "--channel", "develop"]).returncode == 0
    new = json.loads((tmp_path / "o").read_bytes())
    assert new == {**doc, "issued_at": "2026-02-01T00:00:00Z", "expires_at": "2026-02-22T00:00:00Z"}
    assert subprocess.run([*args, "--channel", "main"], capture_output=True).returncode == 1
