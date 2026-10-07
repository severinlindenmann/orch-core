"""#214: `orch link --branch/--worktree` defaults --repo with one repo, refuses with the names when there are
several, and the handover hint names --repo for a multi-repo workspace."""
import pytest

from orch.core import store
from orch.core.ops import Ops
from orch.errors import UsageError


def _several(configure, ws_root):
    (ws_root / "infra").mkdir()
    return configure(git={"repos": {"infra": {"path": "infra"}}})


def test_branch_defaults_to_the_only_repo(ws, aops):
    t = aops.new("x")
    aops.link(t.id, branch="feat/x")
    m = store.load(ws, t.id)[1].meta
    assert m["repos"] == ["harness"] and m["branches"] == {"harness": "feat/x"}


def test_worktree_defaults_to_the_only_repo(ws, aops):
    t = aops.new("x")
    aops.link(t.id, worktree="/tmp/wt")
    assert store.load(ws, t.id)[1].meta["worktrees"] == {"harness": "/tmp/wt"}


def test_several_repos_refuse_and_list_the_names(configure, ws_root, agent):
    ws = _several(configure, ws_root)
    ops = Ops(ws, agent)
    t = ops.new("x")
    with pytest.raises(UsageError) as e:
        ops.link(t.id, branch="feat/x")
    assert "harness" in e.value.hint and "infra" in e.value.hint
    assert not store.load(ws, t.id)[1].meta.get("repos")
    ops.link(t.id, repo="infra", branch="feat/x")
    assert store.load(ws, t.id)[1].meta["branches"] == {"infra": "feat/x"}


def test_the_epic_repo_is_not_inherited(configure, ws_root, agent):
    ws = _several(configure, ws_root)
    ops = Ops(ws, agent)
    e = ops.new("E", type="epic")
    ops.link(e.id, repo="infra")
    c = ops.new("c", epic=e.id)
    with pytest.raises(UsageError):
        ops.link(c.id, branch="feat/x")


def _hint(ops, ticket_id):
    t = store.load(ops.ws, ticket_id)[1]
    return next(w for w in ops._handover_warnings(t) if "no branch or PR is linked" in w)


def test_handover_hint_mentions_repo_only_with_several(configure, ws_root, agent):
    ws = _several(configure, ws_root)
    ops = Ops(ws, agent)
    assert "--repo" in _hint(ops, ops.new("x").id)
    ws1 = configure(git={"repos": {"harness": {"path": "."}}})
    ops1 = Ops(ws1, agent)
    assert "--repo" not in _hint(ops1, ops1.new("y").id)
