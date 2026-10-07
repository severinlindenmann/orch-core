"""Dark AI Factory, end to end with real per-child clones (not the workspace-branch fixture of test_factory_release):
a child commits in its runner-made clone, and the release merges it, deploys dev, releases production on dev's commit
and closes the epic by itself. A clone removed or swapped after the merge changes nothing for production, which runs
on the commit dev was proven on and fetches nothing. Real git, a fake runner for the recipe's commands."""
import os
import shutil

import pytest

from orch.core import epics, factory_clones as fc, factory_close, factory_release as fr, factory_sessions as fs, store
from orch.dashboard.factory_runner import release_once
from test_factory_clones import _g, _msg, _programs, fa, fh, fws, remote, bin_dir  # noqa: F401
from test_factory_production import ProdFake, _at, _later, _prod_recipe
from test_factory_release import _not_stopping, _refine, _states  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _closing_epic(fws, fa, fh, human, close_tasks, remote):
    """A Dark epic signed with release prod and close, naming a file its one child names, the child's work committed
    in its own clone and the child in testing with evidence the close rules take."""
    fr.set_recipe(fws, human, _prod_recipe(remote))
    e = fa.new("Elephants", type="epic")
    _refine(fa, e.id, plan=None)
    fa.set_section(e.id, "Requirements", "Build elephants.json")
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "prod", "close": True})
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    fs.arm(fws, human, d["id"])
    c = fa.new("data", epic=e.id)
    _refine(fa, c.id)
    fa.set_section(c.id, "Requirements", "Write elephants.json")
    fa.epic_auto_approve(c.id)
    clone, why = fc.ensure(fws, human, c.id)
    assert why == "", why
    (clone / "elephants.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "elephants.json")
    _g(clone, "commit", "-q", *_msg(c.id))
    fa.claim(c.id)
    close_tasks(fa, c.id)
    fa.set_section(c.id, "Verification", "- AC1: ran `pytest -q` on the branch, 12 passed")
    fa.move(c.id, "testing")
    return e.id, c.id, clone, _g(clone, "rev-parse", "HEAD")


def test_a_clone_child_is_merged_deployed_released_and_the_epic_closes_by_itself(fws, fa, fh, human, close_tasks,
                                                                                    remote):  # noqa: F811
    eid, cid, clone, sha = _closing_epic(fws, fa, fh, human, close_tasks, remote)
    fake = ProdFake()
    lines = release_once(fws, run=fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "proven"}, lines
    closed = [i for i, x in enumerate(lines) if x.startswith(f"{eid}: closed by itself")]
    proven = [i for i, x in enumerate(lines) if x.startswith(f"{eid}: production of") and x.endswith("proven")]
    assert closed and proven and proven[-1] < closed[0], lines  # the close follows the production proof
    assert store.load(fws, eid)[1].status == "done" and factory_close.closed_by_charter(fws, store.load(fws, eid)[1])
    merged = [a for a, *_ in fake.calls if "--match-head-commit" in a]
    assert merged and merged[0][merged[0].index("--match-head-commit") + 1] == sha  # the clone's own commit
    dev = fr.unit_state(fws, eid, "dev", eid)["base_sha"]
    assert any(a[1:] == ["deploy-prod", dev] for a, *_ in fake.calls)  # production on exactly dev's commit
    us = fr.unit_state(fws, eid, "merge", cid)
    assert fr.own_merge(us) and us["sha"] == sha
    assert sorted(os.listdir(fws.marks)) == []


@pytest.mark.parametrize("how", ["cleaned", "swapped"])
def test_production_runs_on_devs_commit_when_the_clone_goes_after_the_merge(fws, fa, fh, human, close_tasks, remote,
                                                                            monkeypatch, tmp_path, how):  # noqa: F811
    eid, cid, clone, sha = _closing_epic(fws, fa, fh, human, close_tasks, remote)
    _at(fws, 1)  # another production an hour ago: the window is shut, so this round stops after dev
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    dev = fr.unit_state(fws, eid, "dev", eid)["base_sha"]
    if how == "cleaned":
        assert fc.clean(fws, human, cid)
    else:  # an agent puts another repository where the clone was
        shutil.move(str(clone), str(tmp_path / "moved"))
        shutil.copytree(tmp_path / "moved", clone, symlinks=True)
    fetched = []
    real = fr.fetch_child
    monkeypatch.setattr(fr, "fetch_child", lambda *a, **k: fetched.append(a) or real(*a, **k))
    _later(monkeypatch, 30)
    lines = fr.tick(fws, human, fake)
    assert _states(fws, eid)["production"] == "proven", lines
    assert [a[1:] for a, *_ in fake.calls if "deploy-prod" in a] == [["deploy-prod", dev]]
    assert fetched == []  # production never fetches a child
