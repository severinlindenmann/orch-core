"""Dark AI Factory: the release, production, auto-close and audit review. Each test fails on the code before the fix
(the window lost after one clear-window, production on an outdated base, irreversible stages on lenient evidence,
submodules merged, a deleted production record run again, ...). As in test_factory_release.py, the runner's own git
runs for real against temporary repositories and a fake stands in for the recipe's commands."""
import pytest

from orch.core import factory_release as fr, permits, store
from test_factory_production import ProdFake, _at, _later, _prod_recipe, prod  # noqa: F401
from test_factory_release import Fake, _states, _stopped
from test_factory_release import (_not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote,  # noqa: F401
                                  switch)

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def _deploys(fake) -> int:
    return sum("deploy-prod" in x for x in fake.ran())


def _run(fws, eid):
    from orch.dashboard.data import factory as data
    return data.run_view(fws, store.load(fws, eid)[1])


# -- the window and the journal -----------------------------------------------------------------------------------

def test_after_clear_window_a_later_production_still_shuts_the_window(fws, human, monkeypatch):
    _at(fws, -500)  # a record 500 hours ahead
    fr.clear_window(fws, human)
    assert fr.window(fws, 20)["open"]
    _later(monkeypatch, 1)
    fr._record_production(fws, "X-1")  # a production an hour after the reset
    w = fr.window(fws, 20)
    assert w["open"] is False and not w["why"]
    _later(monkeypatch, 21)
    assert fr.window(fws, 20)["open"]


def test_a_deleted_production_record_with_the_journal_intact_never_runs_again(fws, prod, human, monkeypatch):
    eid, _, _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["production"] == "proven" and _deploys(fake) == 1
    d = fr._dir(fws, eid)
    for kind in ("intent", "outcome"):
        (d / fr._name("production", eid, 1, kind)).unlink()
    fr._window_path(fws).unlink()
    assert not fr.window(fws, 20)["open"]  # the journal still says when production ran
    _later(monkeypatch, 21)
    assert fr.tick(fws, human, fake) == [] and _deploys(fake) == 1
    assert _states(fws, eid)["production"] == "unknown" and "release-unknown" in _stopped(fws, eid)
    fr.retry(fws, human, eid, "production", eid)  # yours: the journal's attempt is put back, one more is allowed
    _later(monkeypatch, 21)  # a retry waits for the window too
    assert fr.tick(fws, human, fake) == [f"{eid}: production of {eid} proven"] and _deploys(fake) == 2


def test_a_removed_merge_record_with_the_journal_intact_is_unknown(fws, ready, human):
    eid, (c,), _ = ready()
    fake = Fake()
    fr.tick(fws, human, fake)
    d = fr._dir(fws, eid)
    for kind in ("intent", "outcome"):
        (d / fr._name("merge", c, 1, kind)).unlink()
    us = fr.unit_state(fws, eid, "merge", c)
    assert us["state"] == "unknown" and "journal records attempt 1" in us["why"]
    n = len(fake.calls)
    assert fr.tick(fws, human, fake) == [] and len(fake.calls) == n


def test_a_window_shut_by_a_future_record_needs_you(fws, prod, human):
    pytest.importorskip("fastapi")
    from orch.dashboard.data import factory as data
    _at(fws, -500)
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    r = _run(fws, eid)
    assert r["state"] == "windowlook" and r["state"] in data.NEEDS_YOU and "clear-window" in r["headline"]



# -- production never releases a commit older than what production already has -------------------------------------

def test_a_later_production_of_another_epic_makes_dev_out_of_date(fws, prod, human, remote, monkeypatch):
    from test_factory_release import _main_commit
    _at(fws, 1)  # the window is shut: A's merge and dev are proven, its production waits
    a, _, _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, a) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    _main_commit(fws.root, remote, {"later.txt": "x\n"})  # another epic's work reached the base since
    b, _, _ = prod()
    _later(monkeypatch, 21)
    fr._release(fws, human, fr.gate(fws, b), fake)  # B alone: merge, dev and production on the later base
    assert _states(fws, b)["production"] == "proven" and _deploys(fake) == 1
    assert fr.last_released(fws) == fr.unit_state(fws, b, "dev", b)["base_sha"]
    _later(monkeypatch, 21)
    lines = fr.tick(fws, human, fake)
    assert _deploys(fake) == 1, lines  # A's production would roll B's back: it does not run
    assert _states(fws, a)["dev"] == "stale" and "release-stale" in _stopped(fws, a)
    st = fr.status(fws, store.load(fws, a)[1], permits.factory_delegation(fws, store.load(fws, a)[1]))
    assert any("the base moved since dev was proven" in r["text"] for r in st["reasons"])
    fr.retry(fws, human, a, "dev", a)  # dev again, on the base as it is now; then production after the window
    fr.tick(fws, human, fake)
    _later(monkeypatch, 21)
    fr.tick(fws, human, fake)
    assert _states(fws, a)["production"] == "proven" and _deploys(fake) == 2


# -- the irreversible stages start only on what an unattended close accepts ---------------------------------------

def test_a_doubting_evidence_line_keeps_production_from_starting(fws, prod, fa, human, monkeypatch):
    _at(fws, 1)
    eid, (c,), _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["production"] == "waiting"
    fa.set_section(c, "Verification", "- AC1: ran `pytest -q` on the branch, 12 passed\n"
                                      "- AC1: the live path could not be tested")
    _later(monkeypatch, 21)
    lines = fr.tick(fws, human, fake)
    assert _deploys(fake) == 0 and lines == [f"{eid}: production waits: the evidence of {c} for AC1 does not meet "
                                             "the close rules: a line about it says 'could not'"]
    p = next(s for s in fr.status(fws, store.load(fws, eid)[1],
                                  permits.factory_delegation(fws, store.load(fws, eid)[1]))["stages"]
             if s["name"] == "production")
    assert p["state"] == "waiting" and "could not" in p["waits"][0]
    pytest.importorskip("fastapi")
    r = _run(fws, eid)
    assert r["state"] == "relhold" and r["headline"].startswith("The production stage waits: the evidence of")
    fa.set_section(c, "Verification", "- AC1: ran `pytest -q` on the branch, 12 passed")
    fr.tick(fws, human, fake)
    assert _deploys(fake) == 1 and "waits" not in next(
        s for s in fr.status(fws, store.load(fws, eid)[1], permits.factory_delegation(fws, store.load(fws, eid)[1]))[
            "stages"] if s["name"] == "production")


# -- submodules are never merged by themselves --------------------------------------------------------------------

def test_a_child_branch_adding_a_submodule_stops_before_the_merge(fws, ready, human):
    from test_factory_release import _g, _msg, _wt
    eid, (c,), _ = ready()
    wt = fws.root / _wt(f"feat/{c.lower()}-work")
    _g(wt, "update-index", "--add", "--cacheinfo", f"160000,{_g(wt, 'rev-parse', 'HEAD')},vendor/lib")
    _g(wt, "commit", "-q", *_msg(f"feat/{c.lower()}-work"))
    fake = Fake()
    lines = fr.tick(fws, human, fake)
    assert fake.calls == [] and "sensitive path" in lines[-1]
    assert _stopped(fws, eid) == ["sensitive"]
    st = fr.status(fws, store.load(fws, eid)[1], permits.factory_delegation(fws, store.load(fws, eid)[1]))
    assert "vendor/lib (a submodule)" in st["reasons"][0]["text"]


def test_gitmodules_is_always_sensitive():
    assert fr.sensitive(".gitmodules", fr.HARNESS_SENSITIVE) and fr.sensitive("a/.gitmodules", fr.HARNESS_SENSITIVE)


def test_a_move_to_testing_with_a_submodule_in_the_clone_is_refused(fws, monkeypatch):
    from types import SimpleNamespace

    from orch.core import factory_built as fb, factory_clones as fc
    with monkeypatch.context() as m:
        m.setattr(fc, "record", lambda ws, cid: {"branch": "b"})
        m.setattr(fb, "uncommitted", lambda ws, cid: {"ok": True, "lines": [], "submodules": ["vendor/lib"]})
        why = fb.move_refusal(fws, SimpleNamespace(id="L-0009"))
    assert why and "submodule (vendor/lib)" in why and "does not merge" in why


# -- a production attempt that did not prove itself says to look at production -----------------------------------

def _reasons(fws, eid):
    e = store.load(fws, eid)[1]
    return fr.status(fws, e, permits.factory_delegation(fws, e))["reasons"]


def test_a_production_command_killed_midway_is_unknown_and_says_look_at_production(fws, prod, human):
    eid, _, _ = prod()
    fake = ProdFake()
    orig = fake.__call__

    def call(argv, cwd, env, timeout, started=None):
        r = orig(argv, cwd, env, timeout, started)
        return {**r, "code": None, "timed_out": True} if "deploy-prod" in argv else r
    fr.tick(fws, human, call)
    assert _states(fws, eid)["production"] == "unknown"
    (r,) = _reasons(fws, eid)
    assert r["code"] == "release-unknown" and "timed out" in r["text"] and "half-deployed" in r["text"]


def test_a_failed_production_command_says_look_at_production(fws, prod, human):
    eid, _, _ = prod()
    fake = ProdFake()
    fake.codes["deploy-prod"] = 3
    fr.tick(fws, human, fake)
    (r,) = _reasons(fws, eid)
    assert r["code"] == "production-failed" and "half-deployed" in r["text"] and "exit code 3" in r["text"]
