"""#253: records commit guards (merge in progress), push rules, the signed automatic mode, records in a nested repo."""
import json
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.core import gitfiles, ledger
from orch.core.ops import Ops
from orch.core.workspace import Workspace
from orch.errors import HumanOnlyError, UsageError
from conftest import init_repo

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root, *args, check=True):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _identity(root):
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def _touch(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(ws_root, ws):
    init_repo(ws_root, "main")
    _identity(ws_root)
    gitfiles.write_ignore_block(ws)
    _touch(ws_root / "src" / "app.py", "print(1)\n")
    _git(ws_root, "add", "-A")
    _git(ws_root, "commit", "-qm", "base")
    return ws_root


@pytest.fixture
def remote(repo, tmp_path):
    """The repo with a bare origin it tracks, plus a second clone for someone else's pushes."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    _identity(other)
    return bare, other


def _subjects(root, rev="HEAD", n=5):
    return _git(root, "log", f"-{n}", "--format=%s", rev).stdout.split("\n")[:n]


# -- 1: merge in progress --------------------------------------------------------------------------------------------

@needs_git
@pytest.mark.parametrize("marker", ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge/head-name",
                                    "rebase-apply/applying"])
def test_commit_refused_while_git_is_mid_operation(repo, ws, put, marker):
    put("backlog", size="m")
    _touch(repo / ".git" / marker, "0" * 40 + "\n")
    with pytest.raises(UsageError, match="in progress"):
        gitfiles.commit_records(ws)
    assert _git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert gitfiles.push_records(ws)["pushed"] is False


@needs_git
def test_busy_check_follows_a_linked_worktree(repo, ws, tmp_path):
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    gitdir = _git(tmp_path / "wt", "rev-parse", "--absolute-git-dir").stdout.strip()
    assert gitfiles.busy_reason(tmp_path / "wt") is None
    _touch(__import__("pathlib").Path(gitdir) / "MERGE_HEAD", "0" * 40 + "\n")
    assert "merge" in gitfiles.busy_reason(tmp_path / "wt")
    assert gitfiles.busy_reason(repo) is None


# -- 2: push ---------------------------------------------------------------------------------------------------------

@needs_git
def test_push_sends_a_records_commit(repo, ws, put, remote, capsys):
    bare, _ = remote
    put("backlog", size="m")
    assert run(["records", "commit", "--push", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["committed"] and out["pushed"] and out["hash"]
    assert _subjects(bare, "main", 1)[0].startswith("orch: records")


@needs_git
def test_no_upstream_and_detached_head_do_not_push(repo, ws, put):
    put("backlog", size="m")
    result = gitfiles.sync_records(ws, push=True)
    assert result["committed"] and result["pushed"] is False and "no upstream" in result["reason"]
    _git(repo, "checkout", "-q", "--detach")
    assert "detached" in gitfiles.push_records(ws)["reason"]


@needs_git
def test_other_unpushed_commits_block_the_push(repo, ws, put, remote):
    bare, _ = remote
    _touch(repo / "src" / "app.py", "print(2)\n")
    _git(repo, "commit", "-qam", "code change")
    put("backlog", size="m")
    result = gitfiles.sync_records(ws, push=True)
    assert result["committed"] and not result["pushed"] and "1 other commit(s) wait" in result["reason"]
    assert _subjects(bare, "main", 1)[0] == "base"


@needs_git
def test_a_records_subject_does_not_make_code_pushable(repo, ws, put, remote):
    bare, _ = remote
    _touch(repo / "src" / "app.py", "print(3)\n")
    _git(repo, "commit", "-qam", "orch: records L-0001")  # the subject lies; the content is code
    result = gitfiles.push_records(ws)
    assert not result["pushed"] and "other commit" in result["reason"]
    assert _subjects(bare, "main", 1)[0] == "base"


@needs_git
def test_a_merge_commit_blocks_the_push(repo, ws, put, remote):
    bare, _ = remote
    _git(repo, "checkout", "-q", "-b", "side")
    _touch(repo / "orchestrator" / "side.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "checkout", "-q", "main")
    _touch(repo / "orchestrator" / "main.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "orch: records merge", "side")
    result = gitfiles.push_records(ws)
    assert not result["pushed"] and "other commit" in result["reason"]
    assert _subjects(bare, "main", 1)[0] == "base"


@needs_git
def test_rejected_push_rebases_records_commits_and_pushes(repo, ws, put, remote):
    bare, other = remote
    _touch(other / "src" / "elsewhere.py")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "someone else")
    _git(other, "push", "-q", "origin", "main")
    put("backlog", size="m")
    result = gitfiles.sync_records(ws, push=True)
    assert result["pushed"] and "rebased" in result["reason"]
    assert _subjects(bare, "main", 2)[0].startswith("orch: records") and _subjects(bare, "main", 2)[1] == "someone else"
    assert _git(repo, "rev-parse", "HEAD").stdout == _git(bare, "rev-parse", "main").stdout


@needs_git
def test_rebase_conflict_is_aborted_and_named(repo, ws, put, remote):
    bare, other = remote
    events = "orchestrator/.state/events.jsonl"
    _touch(other / events, '{"seq": 1, "who": "other"}\n')
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "orch: records elsewhere")
    _git(other, "push", "-q", "origin", "main")
    put("backlog", size="m")
    _touch(repo / events, '{"seq": 1, "who": "me"}\n')
    result = gitfiles.sync_records(ws, push=True)
    assert not result["pushed"] and result["failed"] and "conflicts" in result["reason"] and "events.jsonl" in result["reason"]
    assert not (repo / ".git" / "rebase-merge").exists() and not (repo / ".git" / "rebase-apply").exists()
    assert _git(bare, "log", "-1", "--format=%s", "main").stdout.strip() == "orch: records elsewhere"
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip().startswith("orch: records")  # our commit is kept


@needs_git
def test_agent_push_needs_agent_may_push(repo, ws, put, remote, monkeypatch, configure, capsys):
    bare, _ = remote
    configure(git={"agent_may": {"commit": True, "push": False}})
    put("backlog", size="m")
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert run(["records", "commit", "--push"]) == 3
    assert "git.agent_may.push is false" in capsys.readouterr().err
    assert _subjects(repo, "HEAD", 1)[0] == "base"  # nothing committed either
    configure(git={"agent_may": {"commit": True, "push": True}})
    assert run(["records", "commit", "--push"]) == 0
    assert _subjects(bare, "main", 1)[0].startswith("orch: records")


# -- 3: automatic mode -----------------------------------------------------------------------------------------------

def _reopen(ws):
    return Workspace.open(ws.root)


@needs_git
def test_auto_is_off_by_default_and_no_config_key_turns_it_on(repo, ws, configure, capsys):
    assert ledger.records_auto_state(ws) == "off"
    configure(records={"auto": True})
    assert run(["new", "--title", "One", "--size", "s"]) == 0
    assert _subjects(repo, "HEAD", 1)[0] == "base"
    assert ledger.records_auto_state(_reopen(ws)) == "off"


def test_agent_cannot_turn_auto_on_but_may_turn_it_off(ws, agent, human):
    with pytest.raises(HumanOnlyError):
        Ops(ws, agent).set_records_auto(True)
    assert not ledger.entries(ws)
    Ops(ws, human).set_records_auto(True)
    assert ledger.records_auto_state(ws) == "on"
    Ops(ws, agent).set_records_auto(False)
    assert ledger.records_auto_state(ws) == "off"


def test_cli_on_is_refused_in_a_harness_and_signed_in_a_terminal(ws, monkeypatch, capsys):
    from orch import actor
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    assert run(["records", "auto", "on"]) != 0
    assert "agent harness" in capsys.readouterr().err and ledger.records_auto_state(ws) == "off"
    monkeypatch.delenv("ORCH_HARNESS")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "L")
    assert run(["records", "auto", "on"]) == 0
    assert ledger.records_auto_state(_reopen(ws)) == "on"
    assert run(["records", "auto", "off"]) == 0
    assert ledger.records_auto_state(_reopen(ws)) == "off"


@needs_git
def test_auto_commits_after_a_writing_command_and_leaves_the_tree_clean(repo, ws, human, remote, capsys):
    bare, _ = remote
    Ops(ws, human).set_records_auto(True)
    capsys.readouterr()
    assert run(["new", "--title", "One", "--size", "s"]) == 0
    assert _subjects(repo, "HEAD", 1)[0].startswith("orch: records")
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _subjects(bare, "main", 1)[0].startswith("orch: records")  # pushed as well
    assert run(["list"]) == 0  # read-only: nothing new
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"


@needs_git
def test_auto_never_fails_the_command_and_warns_once(repo, ws, human, capsys):
    Ops(ws, human).set_records_auto(True)
    _touch(repo / ".git" / "MERGE_HEAD", "0" * 40 + "\n")
    capsys.readouterr()
    assert run(["new", "--title", "One", "--size", "s"]) == 0
    err = [x for x in capsys.readouterr().err.splitlines() if x.startswith("orch: ")]
    assert len(err) == 1 and "not committed automatically" in err[0]


@needs_git
def test_auto_respects_agent_may_commit(repo, ws, human, configure, monkeypatch):
    Ops(ws, human).set_records_auto(True)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert run(["new", "--title", "One", "--size", "s"]) == 0
    assert _subjects(repo, "HEAD", 1)[0] == "base"
    configure(git={"agent_may": {"commit": True}})
    assert run(["new", "--title", "Two", "--size", "s"]) == 0
    assert _subjects(repo, "HEAD", 1)[0].startswith("orch: records")


@needs_git
def test_auto_does_not_run_inside_its_own_commit(repo, ws, human, monkeypatch):
    Ops(ws, human).set_records_auto(True)
    monkeypatch.setenv("ORCH_AUTO_RECORDS", "1")
    assert run(["new", "--title", "One", "--size", "s"]) == 0
    assert _subjects(repo, "HEAD", 1)[0] == "base"


# -- 4: orchestrator/ as its own repository --------------------------------------------------------------------------

@needs_git
def test_orch_home_can_be_its_own_repository(repo, ws, put):
    (repo / ".gitignore").write_text("orchestrator/\n", encoding="utf-8")
    _git(repo, "rm", "-rq", "--cached", "orchestrator")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore orchestrator")
    init_repo(ws.home, "main")
    _identity(ws.home)
    put("backlog", size="m")
    view = gitfiles.git_view(ws)
    assert view is not None and view.root == ws.home.resolve()
    assert view.uncommitted and all(p.startswith("orchestrator/") for p in view.uncommitted)
    paths, subject = gitfiles.commit_records(ws)
    assert paths and subject.startswith("orch: records")
    assert _git(ws.home, "log", "-1", "--format=%s").stdout.strip() == subject
    assert _subjects(repo, "HEAD", 1)[0] == "ignore orchestrator"  # the code repository is untouched
    assert gitfiles.git_view(ws).uncommitted == []
