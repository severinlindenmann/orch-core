"""orch.core.receipts: run a verify line or a project's named check step by step and keep what happened."""
import subprocess

from orch.core.receipts import CUT, run_steps


def _git(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)


def _one(cmd):
    return [{"name": "verify", "run": cmd}]


def test_a_passing_step_in_a_clean_checkout(tmp_path):
    _git(tmp_path)
    r = run_steps(_one("echo hello"), tmp_path, timeout=30, max_bytes=10_000)
    assert r.exit == 0 and not r.timed_out
    assert len(r.commit) == 40 and r.dirty is False
    assert r.log.startswith(b"$ echo hello\n") and b"hello" in r.log
    assert [s["status"] for s in r.steps] == ["pass"]


def test_a_failing_step_and_a_dirty_tree(tmp_path):
    _git(tmp_path)
    (tmp_path / "a.txt").write_text("changed")
    r = run_steps(_one("exit 3"), tmp_path, timeout=30, max_bytes=10_000)
    assert r.exit == 3 and r.dirty is True and r.steps[0]["status"] == "fail"


def test_outside_git(tmp_path):
    r = run_steps(_one("true"), tmp_path, timeout=30, max_bytes=10_000)
    assert r.commit is None and r.dirty is False


def test_steps_stop_at_the_first_failure(tmp_path):
    steps = [{"name": "build", "run": "true"}, {"name": "test", "run": "exit 2"}, {"name": "lint", "run": "echo never"}]
    r = run_steps(steps, tmp_path, timeout=30, max_bytes=10_000)
    assert r.exit == 2
    assert [(s["name"], s["status"]) for s in r.steps] == [("build", "pass"), ("test", "fail"), ("lint", "skip")]
    assert b"never" not in r.log


def test_keep_going_runs_every_step(tmp_path):
    steps = [{"name": "a", "run": "exit 1"}, {"name": "b", "run": "echo ran"}]
    r = run_steps(steps, tmp_path, timeout=30, max_bytes=10_000, keep_going=True)
    assert r.exit == 1 and [s["status"] for s in r.steps] == ["fail", "pass"] and b"ran" in r.log


def test_a_timeout_kills_the_step_and_says_so(tmp_path):
    steps = [{"name": "slow", "run": "sleep 5"}, {"name": "after", "run": "true"}]
    r = run_steps(steps, tmp_path, timeout=1, max_bytes=10_000)
    assert r.timed_out and r.exit is None
    assert [(s["status"], s["timed_out"]) for s in r.steps] == [("fail", True), ("skip", False)]


def test_the_log_keeps_only_the_tail(tmp_path):
    r = run_steps(_one("yes x | head -c 50000"), tmp_path, timeout=30, max_bytes=1000)
    assert len(r.log) <= 1000 and r.log.startswith(CUT)


def test_the_record_names_steps_and_commands_but_holds_no_log_or_path(tmp_path):
    rec = run_steps(_one("true"), tmp_path, timeout=30, max_bytes=1000).record()
    assert set(rec) == {"exit", "timed_out", "commit", "dirty", "repo", "at", "seconds", "steps"}
    assert rec["steps"] == [{"name": "verify", "run": "true", "status": "pass", "exit": 0, "timed_out": False,
                             "seconds": 0}]
    assert str(tmp_path) not in repr(rec)
