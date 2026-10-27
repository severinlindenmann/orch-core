"""The recording stub: canned side effects, add-on availability, real help passthrough (fake "real" CLI here)."""
import json
import os
import subprocess
import sys

import pytest

STUB = os.path.join(os.path.dirname(__file__), "stub", "orch")
FAKE_REAL = [sys.executable, "-c", "import sys; print('REAL ' + ' '.join(sys.argv[1:])); sys.exit(0)"]


@pytest.fixture
def env(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "state.json").write_text(json.dumps({"active": ["quick", "schedules", "wiki", "changelog", "runbook"], "off": ["time-tracking"]}))
    cache = tmp_path / "cache"
    cache.mkdir()
    e = dict(os.environ, EVAL_META=str(meta), EVAL_LOG=str(tmp_path / "log"), EVAL_HELP_CACHE=str(cache),
             EVAL_REAL_ORCH=json.dumps(FAKE_REAL))
    e.pop("ORCH_STATE_DIR", None)
    return e, tmp_path


def run(env, *args):
    e, tmp = env
    p = subprocess.run([sys.executable, STUB, *args], env=e, capture_output=True, text=True, cwd=tmp)
    log = [json.loads(l) for l in open(tmp / "log")] if (tmp / "log").exists() else []
    return p, log


def test_calls_are_logged_with_rc(env):
    p, log = run(env, "quick", "add", "Bump lint config")
    assert p.returncode == 0 and "quick.created" in p.stdout
    assert log[-1]["argv"] == ["quick", "add", "Bump lint config"] and log[-1]["rc"] == 0


def test_off_addon_answers_addon_off(env):
    p, log = run(env, "time", "log", "90m")
    assert p.returncode == 5 and "addon.off" in p.stderr and "retry:false" in p.stderr
    assert log[-1]["rc"] == 5


def test_inactive_addon_is_unknown_command(env):
    p, _ = run(env, "estimate", "set", "DEMO-0007", "--hours", "3")
    assert p.returncode != 0 and "unknown.command" in p.stderr


def test_follow_up_validation(env):
    assert run(env, "follow-up", "--in", "20m", "--brief", "x")[0].returncode == 3
    p, _ = run(env, "follow-up", "--in", "20m", "--brief", "x")
    assert "followup.too_soon" in p.stderr and "retry:true" in p.stderr
    p, _ = run(env, "follow-up", "--in", "2d", "--brief", "x")
    assert p.returncode == 0 and "followup.set" in p.stdout


def test_set_field_rules(env):
    assert run(env, "set", "DEMO-0007", "changelog.line=Export CSV.")[0].returncode == 0
    assert run(env, "set", "DEMO-0007", "estimate.hours=3")[0].returncode != 0  # estimate not active
    assert run(env, "set", "DEMO-0007", "changelog.bogus=1")[0].returncode != 0


def test_runbook_show(env):
    p, _ = run(env, "runbook", "show", "db-failover")
    assert "dbctl promote" in p.stdout


def test_help_and_describe_come_from_the_real_cli(env):
    p, _ = run(env, "help")
    assert p.stdout.startswith("REAL help")
    assert "Add-ons active" in p.stdout and "runbook" in p.stdout and "estimate" not in p.stdout.split("Add-ons active")[1]
    assert run(env, "help", "work")[0].stdout.strip() == "REAL help work"
    assert run(env, "--help")[0].stdout.startswith("REAL --help")
    assert run(env, "describe", "quick")[0].stdout.strip() == "REAL describe quick"
    p, _ = run(env, "describe", "runbook")
    assert "orch runbook show" in p.stdout  # invented add-on, active
    p, _ = run(env, "describe", "estimate")
    assert p.returncode != 0  # invented add-on, not active


def test_help_result_is_cached(env):
    run(env, "describe", "set")
    e, tmp = env
    assert len(os.listdir(tmp / "cache")) == 1
    e["EVAL_REAL_ORCH"] = json.dumps([sys.executable, "-c", "import sys; sys.exit(9)"])
    p, _ = run(env, "describe", "set")
    assert p.stdout.strip() == "REAL describe set" and p.returncode == 0


def test_flaky_command_fails_once_then_works(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "state.json").write_text(json.dumps({"active": ["schedules"], "off": [], "flaky": ["follow-up"]}))
    e = dict(os.environ, EVAL_META=str(meta), EVAL_LOG=str(tmp_path / "log"))
    args = [sys.executable, STUB, "follow-up", "--in", "2d", "--brief", "x"]
    p1 = subprocess.run(args, env=e, capture_output=True, text=True)
    assert p1.returncode == 3 and "host.busy" in p1.stderr and "retry:true" in p1.stderr
    p2 = subprocess.run(args, env=e, capture_output=True, text=True)
    assert p2.returncode == 0 and "followup.set" in p2.stdout
