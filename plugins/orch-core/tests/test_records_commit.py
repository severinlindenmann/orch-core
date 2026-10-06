"""#168: orch's own records can be committed before plan approval without weakening the code-commit gate."""
import json
import shlex
import shutil
import subprocess
import sys

import pytest

from orch.cli import run
from orch.core import gitfiles
from orch.hooks.commit_msg import check_message

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
GOOD_BODY = "\n\nWhat: add it\nWhy:  needed\nRisk: low\n"


def _git(root, *args, check=True):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _touch(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(ws_root, ws):
    _git(ws_root, "init", "-q", "-b", "main")
    _git(ws_root, "config", "user.email", "t@example.com")
    _git(ws_root, "config", "user.name", "t")
    gitfiles.write_ignore_block(ws)
    _touch(ws_root / "src" / "app.py", "print(1)\n")
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "base")
    return ws_root


def _hook(repo):
    """The real commit check as git runs it, in a fresh process (no orch on PATH needed)."""
    code = "import sys; from orch.cli import run; sys.exit(run(['hook', 'commit-msg', sys.argv[1]]))"
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} \"$1\"\n", encoding="utf-8")
    hook.chmod(0o755)


def _staged(repo):
    return set(_git(repo, "diff", "--cached", "--name-only").stdout.split())


@needs_git
def test_records_only_before_plan_approval_passes(repo, ws, put):
    tid = put("backlog", size="m")
    _git(repo, "add", "orchestrator")
    assert gitfiles.records_only(ws, repo)
    assert check_message(ws, "orch: records\n", repo) == []
    assert check_message(ws, f"orch: records {tid}\n\nRecords orch wrote:\n- added: x\n", repo) == []
    assert check_message(ws, f"{tid} Reconcile ticket{GOOD_BODY}", repo) == []  # keyed subject: no plan gate either


@needs_git
def test_mixed_commit_keeps_the_full_gate(repo, ws, put):
    tid = put("backlog", size="m")
    _touch(repo / "src" / "app.py", "print(2)\n")
    _git(repo, "add", "-A")
    assert not gitfiles.records_only(ws, repo)
    assert any("only orch records" in p for p in check_message(ws, "orch: records\n", repo))
    assert any("plan is not approved" in p for p in check_message(ws, f"{tid} Add thing{GOOD_BODY}", repo))


@needs_git
def test_caches_never_count_as_records(repo, ws):
    _touch(ws.home / ".state" / "index.json")
    _git(repo, "add", "-f", "orchestrator/.state/index.json")
    assert not gitfiles.records_only(ws, repo)
    assert check_message(ws, "orch: records\n", repo) != []


@needs_git
def test_attribution_still_refused_on_a_records_commit(repo, ws, put):
    put("backlog", size="m")
    _git(repo, "add", "orchestrator")
    problems = check_message(ws, "orch: records\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n", repo)
    assert any("attribution" in p for p in problems)


def test_records_subject_fails_closed_outside_git(ws):
    assert any("only orch records" in p for p in check_message(ws, "orch: records\n"))


@needs_git
@needs_sh
def test_partial_commit_reads_the_temporary_index(repo, ws, put):
    """`git commit <paths>` hands the hook a temporary index: code staged in the real index does not leak into it,
    and code named on the command line is seen."""
    _hook(repo)
    put("backlog", size="m")
    _touch(repo / "src" / "app.py", "print(2)\n")
    _git(repo, "add", "-A")
    r = _git(repo, "commit", "-q", "-m", "orch: records", "--", "orchestrator", check=False)
    assert r.returncode == 0, r.stderr
    assert _staged(repo) == {"src/app.py"}
    r = _git(repo, "commit", "-q", "-m", "orch: records", "--", "src", check=False)
    assert r.returncode != 0 and "only orch records" in r.stderr


@needs_git
@needs_sh
def test_records_commit_leaves_other_staged_files_alone(repo, ws, put, capsys):
    _hook(repo)
    t1, t2 = put("backlog", size="m"), put("backlog", size="m")
    _touch(ws.home / ".state" / "events.jsonl", '{"seq": 1}\n')
    _touch(ws.home / ".state" / "needs-count", "3\n")  # a cache: never committed
    _touch(repo / "src" / "app.py", "print(2)\n")
    _touch(repo / "src" / "new.py", "print(3)\n")
    _git(repo, "add", "src")
    assert run(["records", "commit", "--dry-run"]) == 0
    assert "would commit" in capsys.readouterr().out and _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert run(["records", "commit"]) == 0, capsys.readouterr().err
    assert f"orch: records {t1}, {t2}" in capsys.readouterr().out
    subject, _, body = _git(repo, "log", "-1", "--format=%B").stdout.partition("\n")
    assert subject == f"orch: records {t1}, {t2}"
    assert "- added: orchestrator/.state/events.jsonl" in body and "needs-count" not in body
    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
    assert "orchestrator/.state/events.jsonl" in committed and not any(p.startswith("src/") for p in committed)
    assert _staged(repo) == {"src/app.py", "src/new.py"}  # still staged, still uncommitted
    assert gitfiles.git_view(ws).uncommitted == []
    assert run(["records", "commit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["committed"] is False


@needs_git
def test_records_commit_respects_agent_may_commit(repo, ws, put, monkeypatch, configure):
    put("backlog", size="m")
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert run(["records", "commit"]) == 3
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    configure(git={"agent_may": {"commit": True}})
    assert run(["records", "commit"]) == 0
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"


@needs_git
def test_doctor_records_fix_points_to_records_commit(repo, ws, put):
    from orch.onboarding import doctor
    put("backlog", size="m")
    check = next(c for c in doctor(repo) if c.code == "records")
    assert check.ok is False and check.fix.startswith("orch records commit")
