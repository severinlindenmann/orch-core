"""#14: `orch link <id> --pr <number|url>`, a warning when work reaches testing without a branch or PR, and the
reviews addon finding the ticket key in a PR's title or body."""
import subprocess

import pytest

from orch.core import store
from orch.errors import UsageError


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(ws_root):
    _git(ws_root, "init", "-q")
    _git(ws_root, "remote", "add", "origin", "git@github.com:acme/energy-data.git")
    return ws_root


def test_a_pr_number_resolves_against_the_workspace_repo(ws, aops, repo):
    t = aops.new("x")
    aops.link(t.id, pr="22")
    prs = store.load(ws, t.id)[1].meta["prs"]
    assert prs == [{"repo": "harness", "url": "https://github.com/acme/energy-data/pull/22", "state": "draft"}]


def test_a_hash_number_and_a_named_repo(ws_root, configure, aops, repo):
    sub = ws_root / "infra"
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "remote", "add", "origin", "https://gitlab.example.com/acme/infra.git")
    ws2 = configure(git={"repos": {"infra": {"path": "infra"}}})
    from orch.core.ops import Ops
    ops = Ops(ws2, aops.actor)
    t = ops.new("x")
    ops.link(t.id, repo="infra", pr="#3")
    assert store.load(ws2, t.id)[1].meta["prs"][0]["url"] == "https://gitlab.example.com/acme/infra/-/merge_requests/3"


def test_a_pr_url_needs_no_repo(ws, aops, repo):
    t = aops.new("x")
    aops.link(t.id, pr="https://github.com/acme/energy-data/pull/9")
    assert store.load(ws, t.id)[1].meta["prs"][0]["repo"] == "harness"


def test_a_number_without_a_remote_says_what_to_do(ws, aops, ws_root):
    t = aops.new("x")
    with pytest.raises(UsageError) as e:
        aops.link(t.id, pr="22")
    assert "origin" in e.value.message and "URL" in (e.value.hint or "")


def test_an_unknown_repo_name_is_refused(ws, aops, repo):
    t = aops.new("x")
    with pytest.raises(UsageError):
        aops.link(t.id, repo="nope", pr="22")


def test_cli_link_pr_number(ws, aops, repo, capsys, monkeypatch):
    from orch.cli import run
    t = aops.new("x")
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    assert run(["link", t.id, "--pr", "22"]) == 0
    assert store.load(ws, t.id)[1].meta["prs"][0]["url"].endswith("/pull/22")


# -- handing over without code ------------------------------------------------------------------------------------

def _ready(aops, hops, working, close_tasks):
    aops.set_section(working, "Plan", "1. migrate")
    hops.approve(working, "plan")
    close_tasks(aops, working)
    aops.set_section(working, "Verification", "- AC1: 14 jobs on serverless\n- AC2: cost sheet attached")


def test_testing_without_a_linked_pr_warns_in_a_workspace_with_repos(aops, hops, working, close_tasks, repo):
    _ready(aops, hops, working, close_tasks)
    assert aops.move(working, "testing").status == "testing"
    assert any("orch link" in w for w in aops.warnings)


def test_a_linked_branch_is_enough(aops, hops, working, close_tasks, repo):
    _ready(aops, hops, working, close_tasks)
    aops.link(working, repo="harness", branch="feature/x")
    aops.move(working, "testing")
    assert aops.warnings == []


def test_no_warning_in_a_workspace_without_repos(aops, hops, working, close_tasks):
    _ready(aops, hops, working, close_tasks)
    aops.move(working, "testing")
    assert aops.warnings == []


def test_dashboard_move_shows_the_warning(ws, aops, hops, working, close_tasks, repo, dash):
    _ready(aops, hops, working, close_tasks)
    r = dash.post(f"/t/{working}/move", data={"to": "testing"}, headers={"origin": "http://testserver"},
                  follow_redirects=False)
    assert "orch+link" in r.headers["location"] or "orch%20link" in r.headers["location"]


# -- LinkIndex: keys in a PR body ---------------------------------------------------------------------------------

def test_for_review_finds_the_key_in_the_body(ws, aops):
    from orch.core.links import LinkIndex
    t = aops.new("x")
    hits = LinkIndex(ws).for_review(url="https://github.com/a/b/pull/1", branch="fix", title="Fix it",
                                    body=f"Closes {t.id}.")
    assert [h.id for h in hits] == [t.id]


# -- review fix round 1: no guessing ------------------------------------------------------------------------------

def _two_repos(ws_root, configure):
    sub = ws_root / "infra"
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "remote", "add", "origin", "git@github.com:acme/infra.git")
    return configure(git={"repos": {"infra": {"path": "infra"}}})


def test_a_bare_number_with_several_repos_names_them(ws_root, configure, aops, repo):
    from orch.core.ops import Ops
    ws2 = _two_repos(ws_root, configure)
    ops = Ops(ws2, aops.actor)
    t = ops.new("x")
    with pytest.raises(UsageError) as e:
        ops.link(t.id, pr="22")
    assert "several repos" in e.value.message and "harness" in e.value.hint and "infra" in e.value.hint


def test_a_url_picks_the_repo_whose_origin_it_is(ws_root, configure, aops, repo):
    from orch.core.ops import Ops
    ws2 = _two_repos(ws_root, configure)
    ops = Ops(ws2, aops.actor)
    t = ops.new("x")
    ops.link(t.id, pr="https://github.com/acme/infra/pull/5")
    assert store.load(ws2, t.id)[1].meta["prs"][0]["repo"] == "infra"
    with pytest.raises(UsageError):
        ops.link(t.id, pr="https://github.com/someone/else/pull/5")


def test_a_folder_inside_a_parent_repo_is_not_that_repo(ws_root, configure, aops, repo):
    """infra/ has no .git of its own: `git -C infra` would answer for the parent; orch must not use that."""
    (ws_root / "infra").mkdir()
    ws2 = configure(git={"repos": {"infra": {"path": "infra"}}})
    from orch.core.ops import Ops
    ops = Ops(ws2, aops.actor)
    t = ops.new("x")
    with pytest.raises(UsageError) as e:
        ops.link(t.id, repo="infra", pr="7")
    assert "no usable origin" in e.value.message
