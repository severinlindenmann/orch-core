"""#36: which files orch writes belong in git (shared records) and which stay on one machine (caches, locks)."""
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.core import gitfiles

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _git_init(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def _touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("rel,kind", [
    ("config.json", "durable"), ("AGENTS.orch.md", "durable"), (".gitignore", "durable"),
    ("tickets/backlog/L-0001-x.md", "durable"), ("tickets/INDEX.md", "durable"),
    ("artifacts/L-0001/report.md", "durable"), ("static/logo.png", "durable"),
    (".state/counter.json", "durable"), (".state/events.jsonl", "durable"),
    (".state/gates/L-0001-plan.md", "durable"), (".state/remote/ledger.jsonl", "durable"),
    (".state/addons/orch-tix/records/decisions.json", "durable"),
    ("temporary/scratch.txt", "local"), (".state/locks/events.lock", "local"), (".state/index.json", "local"),
    (".state/addon-errors.log", "local"), (".state/guard-errors.log", "local"), (".state/needs-count", "local"),
    (".state/run/L-0001-x.command", "local"), (".state/remote/ledger.lock", "local"),
    (".state/addons/changed", "local"), (".state/addons/github-reviews/github.repo.json", "local"),
    (".state/addons/orch-tix/outbox.jsonl", "local"), (".state/addons/orch-tix/events.cursor", "local"),
    (".state/addons/orch-tix/inbox/1.json", "local"), (".state/addons/orch-tix/records/state.lock", "local"),
    (".state/.counter.json.abc.tmp", "local"),
    ("share/thing.txt", None), (".state/mystery.json", None), ("notes.md", None),
])
def test_classify(rel, kind):
    assert gitfiles.classify(rel) == kind


def test_block_is_inserted_first_and_replaced_in_place():
    mine = "# my own\n*.bak\n"
    once = gitfiles.apply_ignore_block(mine)
    assert once.startswith(gitfiles.BEGIN) and once.rstrip("\n").endswith("*.bak")
    assert gitfiles.apply_ignore_block(once) == once
    stale = once.replace("/.state/index.json\n", "")
    assert gitfiles.apply_ignore_block(stale) == once
    assert gitfiles.apply_ignore_block(None) == gitfiles.ignore_block() + "\n"


def test_block_state(ws):
    path = ws.home / ".gitignore"
    path.unlink(missing_ok=True)
    assert gitfiles.ignore_block_state(ws) == "missing"
    path.write_text("temporary/\n.state/locks/\n", encoding="utf-8")  # a workspace from before the block
    assert gitfiles.ignore_block_state(ws) == "missing"
    path.write_text(gitfiles.apply_ignore_block(path.read_text(encoding="utf-8")).replace("/.state/run/\n", ""),
                    encoding="utf-8")
    assert gitfiles.ignore_block_state(ws) == "outdated"
    assert gitfiles.write_ignore_block(ws) == "updated"
    assert gitfiles.ignore_block_state(ws) == "ok"
    assert path.read_text(encoding="utf-8").endswith(".state/locks/\n")  # the human's lines stay
    assert gitfiles.write_ignore_block(ws) == "unchanged"


def test_init_writes_the_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "acme", "--no-instructions"]) == 0
    text = (tmp_path / "orchestrator" / ".gitignore").read_text(encoding="utf-8")
    assert text == gitfiles.ignore_block() + "\n"


@needs_git
def test_git_view_after_a_day_of_use(ws_root, ws):
    """Only durable records show in git status once the block is in place; caches and locks never do."""
    _git_init(ws_root)
    gitfiles.write_ignore_block(ws)
    home = ws.home
    for rel in (".state/locks/events.lock", ".state/index.json", ".state/needs-count", ".state/run/a.command",
                ".state/addons/changed", ".state/addons/orch-tix/outbox.jsonl", ".state/addons/orch-tix/inbox/1.json",
                ".state/addons/gh/gh.repo.json", "temporary/x.txt", ".state/remote/ledger.lock"):
        _touch(home / rel)
    for rel in (".state/gates/L-0001-plan.md", ".state/events.jsonl", ".state/counter.json",
                ".state/addons/orch-tix/records/decisions.json", "tickets/backlog/L-0001-x.md"):
        _touch(home / rel)
    _touch(home / "share" / "odd.txt")
    view = gitfiles.git_view(ws)
    assert view is not None
    assert "orchestrator/.state/gates/L-0001-plan.md" in view.uncommitted
    assert "orchestrator/.state/addons/orch-tix/records/decisions.json" in view.uncommitted
    assert not any("/locks/" in p or "index.json" in p or "outbox" in p or "temporary" in p for p in view.uncommitted)
    assert view.unclassified == ["orchestrator/share/odd.txt"]
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "records")
    assert gitfiles.git_view(ws).uncommitted == []


@needs_git
def test_git_view_names_sync_targets_and_tracked_caches(ws_root, ws):
    _git_init(ws_root)
    _touch(ws.home / ".state" / "index.json")  # committed before the block existed
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "old")
    gitfiles.write_ignore_block(ws)
    _touch(ws_root / "AGENTS.md", "changed by sync\n")
    view = gitfiles.git_view(ws)
    assert "AGENTS.md" in view.uncommitted
    assert view.tracked_local == ["orchestrator/.state/index.json"]


def test_git_view_outside_git(ws):
    assert gitfiles.git_view(ws) is None


def test_sync_writes_and_updates_the_block(ws):
    from orch.instructions.sync import sync_instructions
    path = ws.home / ".gitignore"
    path.write_text("temporary/\n", encoding="utf-8")
    got = {r.path.relative_to(ws.root).as_posix(): r.action for r in sync_instructions(ws, dry_run=True)}
    assert got["orchestrator/.gitignore"] == "updated" and path.read_text(encoding="utf-8") == "temporary/\n"
    sync_instructions(ws)
    assert gitfiles.ignore_block_state(ws) == "ok"


@needs_git
def test_cli_sync_says_what_to_commit(ws_root, ws, capsys):
    _git_init(ws_root)
    assert run(["instructions", "sync"]) == 0
    out = capsys.readouterr().out
    assert "commit these files" in out and "AGENTS.md" in out.split("commit these files", 1)[1]
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "sync")
    assert run(["instructions", "sync"]) == 0
    assert "commit these files" not in capsys.readouterr().out


def test_cli_sync_outside_git_prints_no_commit_hint(ws_root, capsys):
    assert run(["instructions", "sync"]) == 0
    assert "commit these files" not in capsys.readouterr().out


@needs_git
def test_doctor_reports_block_records_and_unclassified(ws_root, ws):
    from orch.onboarding import doctor
    _git_init(ws_root)
    (ws.home / ".gitignore").write_text("temporary/\n", encoding="utf-8")
    _touch(ws.home / ".state" / "gates" / "L-0001-plan.md")
    _touch(ws.home / "notes.md")
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["gitignore"].ok is False and checks["gitignore"].fix == "orch doctor --fix"
    assert checks["records"].ok is False and "orchestrator/.state/gates/L-0001-plan.md" in checks["records"].message
    assert checks["records"].fix.startswith("commit them")
    assert "git add -- " in checks["records"].fix and "orchestrator/.state/gates/L-0001-plan.md" in checks["records"].fix
    assert "-A" not in checks["records"].fix
    assert checks["unclassified"].ok is False and "orchestrator/notes.md" in checks["unclassified"].message
    assert run(["doctor", "--fix"]) == 0
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "all")
    checks = {c.code: c.ok for c in doctor(ws_root)}
    assert checks["gitignore"] and checks["records"] and checks["unclassified"]


@needs_git
def test_doctor_names_tracked_caches(ws_root, ws):
    from orch.onboarding import doctor
    _git_init(ws_root)
    gitfiles.write_ignore_block(ws)
    _touch(ws.home / ".state" / "addons" / "gh" / "gh.repo.json")
    _git(ws_root, "add", "-f", "-A")
    _git(ws_root, "commit", "-qm", "oops")
    check = next(c for c in doctor(ws_root) if c.code == "gitignore")
    assert check.ok is False and "orchestrator/.state/addons/gh/gh.repo.json" in check.message
    assert "git rm --cached" in check.fix


def test_doctor_without_git_skips_git_checks(ws_root, ws):
    from orch.onboarding import doctor
    gitfiles.write_ignore_block(ws)
    codes = {c.code: c.ok for c in doctor(ws_root)}
    assert codes["gitignore"] is True and "records" not in codes and "unclassified" not in codes


def test_doctor_fix_writes_only_the_block(ws_root, ws, capsys):
    (ws.home / ".gitignore").unlink(missing_ok=True)
    before = (ws_root / "AGENTS.md").exists()
    assert run(["doctor", "--fix"]) == 0
    assert gitfiles.ignore_block_state(ws) == "ok"
    assert (ws_root / "AGENTS.md").exists() == before
    assert "orchestrator/.gitignore" in capsys.readouterr().out


@needs_git
def test_cli_check_names_uncommitted_records_as_info(ws_root, ws, capsys):
    import json
    _git_init(ws_root)
    gitfiles.write_ignore_block(ws)
    _touch(ws.home / ".state" / "gates" / "L-0001-plan.md")
    assert run(["check", "--json"]) == 0
    found = [f for f in json.loads(capsys.readouterr().out) if f["code"] == "uncommitted-records"]
    assert len(found) == 1 and found[0]["level"] == "info"
    assert "orchestrator/.state/gates/L-0001-plan.md" in found[0]["message"]


def test_addon_records_dir_is_the_durable_folder(ws):
    from orch.addons.api import AddonContext
    ctx = AddonContext(ws, "orch-tix")
    assert ctx.records_dir == ctx.state_dir / "records"
    assert gitfiles.classify(ctx.records_dir.relative_to(ws.home).as_posix() + "/links.json") == "durable"
    assert gitfiles.classify(ctx.state_dir.relative_to(ws.home).as_posix() + "/links.json") == "local"
