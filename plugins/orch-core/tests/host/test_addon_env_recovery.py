"""V3 G4 (design-g4 C4): a crash at each point of ``market.env_build`` is converged by the next start
(``addonenv.recover``) and the next build; an environment is never adopted, never half-trusted."""

# ruff: noqa: F811
from __future__ import annotations

import os

import pytest

from orch.addons import envtree
from orch.host import addonenv
from tests.host import dep_helpers as D
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.dep_helpers import py_lock
from tests.host.market_helpers import ok, on_worker, person
from tests.host.test_addon_env import (
    PY_MAIN,
    build,
    build_state,
    env_path,
    location,
    publish_py,
    ready,  # noqa: F401
    run_addon,
)

net = M.net
registry = D.registry
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def recover(served):
    on_worker(served, lambda: addonenv.recover(served.service))


def builds(served):
    return [n for n in os.listdir(location(served).state + "/addon-env") if n.startswith(".build-")]


class Crash(RuntimeError):
    """Stands for the host process dying at this point."""


@pytest.fixture
def installed(served, human, net, ready, registry):
    publish_py(served, net, registry)
    M.install(served, human)
    return served


def test_a_crash_after_pip_leaves_a_build_directory_that_start_up_removes(installed, monkeypatch):
    served = installed

    def die(*a, **k):
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(addonenv, "_rm_name", lambda base, name: None)  # a dead process cleans nothing
        m.setattr(addonenv, "_install", die)
        assert build(served)["ok"] is False
    assert len(builds(served)) == 1 and not env_path(served).exists()
    recover(served)
    assert builds(served) == [] and build_state(served)["state"] == "needs_setup"
    ok(build(served))
    assert run_addon(served) == {"value": "from-the-dependency"}


def test_a_crash_after_the_rename_leaves_an_unstamped_target_that_is_removed_never_adopted(installed, monkeypatch):
    served = installed

    def die(*a, **k):
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(envtree, "stamp_body", die)
        assert build(served)["ok"] is False
    t = env_path(served)
    assert t.is_dir() and not envtree.stamp_path(t).exists()  # the tree is in place, the stamp is not
    assert build_state(served)["state"] == "needs_setup"  # unstamped: not an environment, the add-on does not run
    recover(served)
    assert not t.exists()
    ok(build(served))
    assert run_addon(served) == {"value": "from-the-dependency"}


def test_a_crash_after_the_stamp_keeps_the_new_environment_and_start_up_drops_the_old_digest(
    served, human, net, ready, registry, monkeypatch
):
    publish_py(served, net, registry)
    M.install(served, human)
    ok(build(served))
    old = env_path(served)
    row = registry.py("idna", "3.11", {"idna/__init__.py": "VALUE = 'v2'\n"})
    net.add("standup", "1.1.0", pkg=D.dep_files("standup", "1.1.0", py_lock(row), D.PY, main=PY_MAIN), runtime=D.PY)
    net.version += 1
    ok(person(served, "market.refresh"))
    M.install(served, human, update=True)
    real = addonenv.write_atomic

    def stamp_then_die(*a, **k):
        real(*a, **k)
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(addonenv, "write_atomic", stamp_then_die)
        assert build(served)["ok"] is False
    new = env_path(served)
    assert new.is_dir() and envtree.stamp_path(new).exists() and old.exists()  # the old digest was not cleaned yet
    recover(served)
    assert not old.exists() and not envtree.stamp_path(old).exists()
    assert run_addon(served) == {"value": "v2"}  # the stamped environment verifies and runs


def test_start_up_removes_environments_of_uninstalled_names_and_stale_cache_files(installed):
    served = installed
    state = location(served).state
    ws_dir = os.path.join(state, "addon-env", location(served).ws)
    os.makedirs(os.path.join(ws_dir, "ghost", "0123456789abcdef"), mode=0o700)
    dl = os.path.join(state, "addon-env", ".dl")
    os.makedirs(dl, mode=0o700, exist_ok=True)
    old, fresh, part = (os.path.join(dl, n) for n in ("sha256-old", "sha256-fresh", ".part-x"))
    for p in (old, fresh, part):
        open(p, "wb").write(b"x")
    os.utime(old, (1, 1))
    recover(served)
    assert not os.path.exists(os.path.join(ws_dir, "ghost"))
    assert not os.path.exists(old) and not os.path.exists(part) and os.path.exists(fresh)
