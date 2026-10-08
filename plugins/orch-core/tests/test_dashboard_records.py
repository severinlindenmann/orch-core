"""#253: the menu's "Not in git" row, the Records page and their human-only POSTs."""
import shutil
import subprocess

import pytest

pytest.importorskip("fastapi")

from orch.core import gitfiles, ledger  # noqa: E402
from orch.core.ops import Ops  # noqa: E402
from orch.dashboard import setup_state  # noqa: E402
from conftest import init_repo  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
SAME = {"origin": "http://testserver"}


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def waiting(ws_root, ws, put):
    """A git repository with one uncommitted ticket; returns its id."""
    init_repo(ws_root, "main")
    _git(ws_root, "config", "user.email", "t@example.com")
    _git(ws_root, "config", "user.name", "t")
    gitfiles.write_ignore_block(ws)
    (ws_root / "README.md").write_text("x\n", encoding="utf-8")
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "base")
    return put("backlog", size="m")


def _fresh(ws):
    setup_state.state(ws).refresh()


@needs_git
def test_menu_row_and_count(waiting, ws, dash):
    _fresh(ws)
    html = dash.get("/board").text
    assert "Not in git" in html and waiting in html and 'action="/records/commit"' in html
    assert "Commit and push" in html and 'class="nav-dot nav-dot-warn"' in html
    snap = setup_state.state(ws).snapshot
    assert any(c.code == "records" and not c.ok for c in snap.checks)  # the doctor check stays ...
    others = [c for c in snap.checks if not c.ok and c.code != "records"
              and c.code in __import__("orch.onboarding", fromlist=["x"]).OPEN_ITEM_CODES]
    assert setup_state.open_count(snap, ws) == len(others)  # ... but no longer counts toward the badge


def test_no_row_outside_git_or_when_clean(ws, dash):
    _fresh(ws)
    assert "Not in git" not in dash.get("/board").text


@needs_git
def test_page_lists_paths_message_and_auto_state(waiting, ws, dash):
    html = dash.get("/records").text
    assert f"orch: records {waiting}" in html and f'href="/t/{waiting}"' in html
    assert "orchestrator/.state/events.jsonl" in html or f"{waiting}" in html
    assert "orch records auto on" in html and "Turn off" not in html


@needs_git
def test_commit_button_commits_and_reports(waiting, ws, dash, ws_root):
    r = dash.post("/records/commit", data={"next": "/records"}, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and "Committed" in r.headers["location"].replace("+", " ")
    assert _git(ws_root, "log", "-1", "--format=%s").startswith("orch: records")
    assert gitfiles.git_view(ws).uncommitted == []
    assert "Not in git" not in dash.get("/board").text


@needs_git
def test_commit_post_is_human_only_and_same_origin(waiting, ws, dash, ws_root, monkeypatch):
    assert dash.post("/records/commit", headers={"origin": "http://evil.example"}, follow_redirects=False).status_code == 403
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    assert dash.post("/records/commit", headers=SAME, follow_redirects=False).status_code == 403
    assert _git(ws_root, "rev-list", "--count", "HEAD").strip() == "1"


@needs_git
def test_merge_in_progress_shows_the_reason_and_no_button(waiting, ws, dash, ws_root):
    (ws_root / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    _fresh(ws)
    html = dash.get("/board").text
    assert "Not in git" in html and "merge is in progress" in html and 'action="/records/commit"' not in html
    assert "merge is in progress" in dash.get("/records").text


@needs_git
def test_auto_row_has_no_button_and_turn_off_is_signed(waiting, ws, dash, human):
    Ops(ws, human).set_records_auto(True)
    _fresh(ws)
    html = dash.get("/board").text
    assert "commit and push automatically" in html and 'action="/records/commit"' not in html
    assert "Turn off" in dash.get("/records").text
    r = dash.post("/records/auto-off", data={"next": "/records"}, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and ledger.records_auto_state(ws) == "off"
