"""AI Factory per-child clones and the shape of the workspace's checkout: a workspace that is a subfolder of its
repository (the session starts in the clone's copy of that folder) and a workspace that is a linked worktree (its
children start in the workspace root and do not commit, as before clones, and readiness says why)."""
import shutil
import subprocess

import pytest

from orch.core import dark_profile, factory_clones as fc, factory_runner, factory_sessions as fs, permits, store
from test_factory_clones import COMMIT, _both, _child, _epic, _programs, _tick  # noqa: F401
from test_factory_runner import Fake, _behavior

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _g(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.hooksPath=",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _dark(configure, human):
    from orch.core.ops import Ops
    w = configure(factory={"enabled": True}, git={"agent_may": {"commit": True}})
    Ops(w, human).set_factory_dark(True)
    dark_profile.add_baseline(w, human, name="git-basic")
    return w


@pytest.fixture
def sub(configure, human, ws_root):
    """The workspace is the folder `my workspace` inside a repository one level up."""
    top = ws_root.parent
    _g(top, "init", "-q", "-b", "main")
    w = _dark(configure, human)
    (ws_root / ".claude").mkdir()
    (ws_root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    _g(top, "add", "my workspace/orchestrator/config.json", "my workspace/.claude/settings.json")
    _g(top, "commit", "-q", "-m", "init")
    return w


def _ops(ws, actor):
    from orch.core.ops import Ops
    return Ops(ws, actor)


def test_a_subfolder_workspace_starts_in_the_clones_copy_of_that_folder(sub, agent, human):
    from conftest import human_ops
    fa, fh = _ops(sub, agent), human_ops(sub, human)
    eid, d = _epic(fa, fh, human, sub)
    cid = _child(fa, eid)
    fake = Fake()
    lines = _tick(sub, human, fake)
    ((_, cwd, _),) = fake.started
    clone = fc.clone_dir(sub, cid)
    assert cwd == str((clone / "my workspace").resolve()), lines
    assert fc.workspace_rel(sub).as_posix() == "my workspace"
    assert fr_sensitive(sub)[0] == "my workspace/orchestrator"
    (b,) = fs.bindings(sub)
    guard, hook = _both(sub, b, COMMIT, clone / "my workspace")
    assert guard.allow and _behavior(hook) == "allow"
    guard, _ = _both(sub, b, COMMIT, clone)  # above the start folder
    assert not guard.allow


def fr_sensitive(ws):
    from orch.core import factory_release
    return factory_release.always_sensitive(ws)


def test_a_subfolder_clone_whose_harness_settings_differ_is_not_started(sub, agent, human):
    from conftest import human_ops
    fa, fh = _ops(sub, agent), human_ops(sub, human)
    eid, d = _epic(fa, fh, human, sub)
    cid = _child(fa, eid)
    (sub.root / ".claude" / "settings.json").write_text('{"changed": true}\n', encoding="utf-8")  # not committed
    fake = Fake()
    lines = _tick(sub, human, fake)
    assert not fake.started and any("harness settings" in x for x in lines), lines


@pytest.fixture
def linked(configure, human, tmp_path, ws_root):
    """The workspace is a linked worktree of a repository elsewhere."""
    main = tmp_path / "main-checkout"
    main.mkdir()
    _g(main, "init", "-q", "-b", "main")
    _g(main, "commit", "-q", "--allow-empty", "-m", "init")
    shutil.rmtree(ws_root)
    _g(main, "worktree", "add", "-q", "-b", "work", str(ws_root))
    (ws_root / "orchestrator").mkdir()
    from conftest import make_config
    import json
    (ws_root / "orchestrator" / "config.json").write_text(json.dumps(make_config()), encoding="utf-8")
    return _dark(configure, human)


def test_a_linked_worktree_workspace_falls_back_to_the_root_and_says_why(linked, agent, human):
    from conftest import human_ops
    fa, fh = _ops(linked, agent), human_ops(linked, human)
    eid, d = _epic(fa, fh, human, linked)
    cid = _child(fa, eid)
    can, why = fc.clonable(linked)
    assert can is None and "linked git worktree" in why
    fake = Fake()
    _tick(linked, human, fake)
    ((_, cwd, argv),) = fake.started
    assert cwd == str(linked.root.resolve()) and "Do not commit" in argv[-1] and fc.record(linked, cid) is None
