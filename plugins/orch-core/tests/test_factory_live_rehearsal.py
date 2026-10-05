"""The owner's live test (docs/factory-release-live-test.md), rehearsed: the example recipe and the example
release-step script run for real (no fake runner) against a bare git "remote" on disk. Nothing reaches the network."""
import json
import shutil
from pathlib import Path

import pytest

import orch
from orch.core import factory_close as fc, factory_release as fr, factory_report, store
from test_factory_release import _g, _ready_epic, _states
from test_factory_release import _not_stopping, bin_dir, fa, fh, fws, remote  # noqa: F401

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
DOCS = Path(orch.__file__).parents[2] / "docs"


@pytest.fixture
def live(fws, fa, fh, human, close_tasks, remote, bin_dir):
    """The example script on PATH (its state folder beside it, in the test's temporary folder) and the example
    recipe pointed at the test's bare remote."""
    shutil.copy(DOCS / "examples" / "release-step", bin_dir / "release-step")
    (bin_dir / "release-step").chmod(0o755)
    rec = json.loads((DOCS / "examples" / "factory-release-live-test.json").read_text(encoding="utf-8"))
    rec["remote"] = str(remote)
    state = bin_dir.parent / "state"  # beside the script's folder, which outlives one test: start empty
    shutil.rmtree(state, ignore_errors=True)

    def _make(charter=None):
        return _ready_epic(fws, fa, fh, human, close_tasks, release="prod", recipe=rec, kids=2,
                           charter={"rollback": True, "close": True} if charter is None else charter)
    yield _make, state
    shutil.rmtree(state, ignore_errors=True)


def test_the_example_recipe_is_valid_and_needs_its_script(ws):
    rec = json.loads((DOCS / "examples" / "factory-release-live-test.json").read_text(encoding="utf-8"))
    checked = fr.check_recipe(rec, ws, check_programs=False)
    assert [s["name"] for s in checked["stages"]] == ["merge", "dev", "production"]
    assert checked["stages"][2]["window_hours"] == 1 and checked["stages"][2]["rollback"] is not None
    assert fr.programs_of(checked) == ["release-step"]
    doc = (DOCS / "factory-release-live-test.md").read_text(encoding="utf-8")
    assert "examples/factory-release-live-test.json" in doc and "examples/release-step" in doc


def test_rehearsal_merges_deploys_releases_and_closes_for_real(fws, live, human, remote):
    from orch.dashboard.factory_runner import release_once
    make, state = live
    eid, kids, _ = make()
    lines = release_once(fws)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "proven"}, lines
    assert lines[-1].startswith(f"{eid}: closed by itself")
    head = _g(remote, "rev-parse", "refs/heads/main")
    for k in kids:  # both children's commits are on the remote's main
        sha = _g(fws.root, "rev-parse", f"feat/{k.lower()}-work")
        assert _g(remote, "merge-base", "--is-ancestor", sha, head) == ""
    dev = fr.unit_state(fws, eid, "dev", eid)["base_sha"]
    assert (state / "dev.version").read_text().strip() == dev == head
    assert (state / "production.version").read_text().strip() == dev
    assert store.load(fws, eid)[1].status == "done" and fc.closed_by_charter(fws, store.load(fws, eid)[1])
    assert "deploy production" in (state / "release.log").read_text()


def test_rehearsal_rolls_production_back_and_does_not_close(fws, live, human):
    from orch.dashboard.factory_runner import release_once
    make, state = live
    eid, _, _ = make()
    state.mkdir(parents=True, exist_ok=True)
    (state / "fail-production-check").write_text("", encoding="utf-8")
    release_once(fws)
    assert (state / "production.version").read_text().strip() == "rolled-back"
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["rolled-back"]
    assert store.load(fws, eid)[1].status == "open"


def test_rehearsal_rollback_that_fails_stops_and_holds_the_next_epic(fws, live, human):
    from orch.dashboard.factory_runner import release_once
    make, state = live
    eid, _, _ = make()
    state.mkdir(parents=True, exist_ok=True)
    for f in ("fail-production-check", "fail-rollback"):
        (state / f).write_text("", encoding="utf-8")
    release_once(fws)
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["rollback-failed"]
    other, _, _ = make()
    release_once(fws)
    st = fr.status(fws, store.load(fws, other)[1],
                   __import__("orch.core.permits", fromlist=["x"]).factory_delegation(fws, store.load(fws, other)[1]))
    prod = next(s for s in st["stages"] if s["name"] == "production")
    assert prod["state"] == "waiting" and prod["held"] == [eid]  # and its window is shut by the first attempt
    assert store.load(fws, other)[1].status == "open"


def test_rehearsal_without_a_signed_rollback_rolls_nothing_back(fws, live, human):
    from orch.dashboard.factory_runner import release_once
    make, state = live
    eid, _, _ = make(charter={"close": True})
    state.mkdir(parents=True, exist_ok=True)
    (state / "fail-production-check").write_text("", encoding="utf-8")
    release_once(fws)
    assert (state / "production.version").read_text().strip() != "rolled-back"
    assert " rollback production" not in (state / "release.log").read_text()
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["production-failed"]


def test_rehearsal_window_shut_then_reopen_after_the_close(fws, live, human):
    from orch.core.ops import Ops
    from orch.dashboard.factory_runner import release_once
    make, state = live
    first, _, _ = make()
    release_once(fws)
    assert store.load(fws, first)[1].status == "done"
    second, _, _ = make()
    release_once(fws)  # within the 1 hour window of the first release
    assert _states(fws, second) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    assert store.load(fws, second)[1].status == "open"  # no close while production waits
    Ops(fws, human).epic_pause(first)  # what the run view's Reopen does: stop the run, then reopen
    Ops(fws, human).reopen(first, "not done")
    assert store.load(fws, first)[1].status == "open"
    release_once(fws)
    assert store.load(fws, first)[1].status == "open"  # never closed again by itself
