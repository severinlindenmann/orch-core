"""V3 G3 (#921, C3): a crash at any point of an install leaves nothing running that was not granted, and the next start
converges (idempotent). The registry fails closed meanwhile: a directory whose digest is not the grant's is
``changed``."""

# ruff: noqa: F811
from __future__ import annotations

import os

import pytest

from orch.addons import install, package
from orch.host import human as host_human
from orch.host import marketinstall
from tests.host import human_helpers
from tests.host import market_helpers as M  # noqa: E402
from tests.host.market_helpers import (  # noqa: F401
    add_market,
    addon_dir,
    err,
    mfiles,
    net,
    ok,
    on_worker,
    person,
    stage,
)  # fmt: skip
from tests.market import helpers as H

net = M.net  # the fake network (a fixture)
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def ready(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    return net


def registry_state(served, name="standup"):
    s = served.service
    on_worker(served, s.store.refresh)
    return install.load_registry(s.store.layout.store, s.store.state.workspace.addons).state(name)


def sign_and_submit(served, human, name="standup"):
    st = ok(stage(served, name))["data"]
    prepared = human.prepare("addon.grant", {"name": name, "staged": st["stage"]})
    signed = human.sign(prepared)
    return st, prepared, signed


def converged(served, st):
    assert package.read_package(addon_dir(served)).digest == st["package_sha256"]
    rec = mfiles(served).installs()
    assert rec["addons"]["standup"]["package_sha256"] == st["package_sha256"]
    assert rec["seen"] == {f"{H.OFFICIAL}|standup": st["version"]} and rec["keys"]
    assert mfiles(served).stages() == []
    assert not [n for n in os.listdir(addon_dir(served).parent) if n.startswith(".standup.")]
    assert registry_state(served) == "active"


def test_crash_after_the_append_before_the_swap(served, human, ready, monkeypatch):
    st, prepared, signed = sign_and_submit(served, human)
    monkeypatch.setattr(marketinstall, "after_grant", lambda service, record, signed: None)  # the host dies right here
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    M.turn_on(human)  # 1237: an install is signed Off; these tests are about a granted AND on add-on
    assert not addon_dir(served).exists() and mfiles(served).installs()["addons"] == {}
    assert registry_state(served) != "active"  # granted, but nothing granted runs
    on_worker(served, lambda: marketinstall.recover(served.service))
    converged(served, st)
    on_worker(served, lambda: marketinstall.recover(served.service))  # idempotent
    converged(served, st)


def test_crash_between_the_two_renames(served, human, ready, monkeypatch):
    st, prepared, signed = sign_and_submit(served, human)
    real = os.rename
    calls = []

    def dying(src, dst, **kw):
        calls.append(src)
        if str(src) == "standup" or calls.count(src) > 0 and ".new-" in str(src):
            raise OSError("power cut")
        return real(src, dst, **kw)

    monkeypatch.setattr(marketinstall.os, "rename", dying)
    ok(human.submit_res(prepared["request"]["id"], signed.events))  # after_grant logs the failure, never raises
    M.turn_on(human)  # 1237: an install is signed Off; these tests are about a granted AND on add-on
    monkeypatch.undo()
    assert not addon_dir(served).exists()  # new was materialized, never renamed into place
    assert [n for n in os.listdir(addon_dir(served).parent) if n.startswith(".standup.new-")]
    assert registry_state(served) != "active"
    on_worker(served, lambda: marketinstall.recover(served.service))
    converged(served, st)


def test_crash_after_the_swap_before_the_record(served, human, ready, monkeypatch):
    st, prepared, signed = sign_and_submit(served, human)
    f = type(mfiles(served))
    monkeypatch.setattr(f, "commit_installs", lambda self: (_ for _ in ()).throw(OSError("disk")))
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    M.turn_on(human)  # 1237: an install is signed Off; these tests are about a granted AND on add-on
    monkeypatch.undo()
    assert package.read_package(addon_dir(served)).digest == st["package_sha256"]
    assert mfiles(served).installs()["addons"] == {}
    on_worker(served, lambda: marketinstall.recover(served.service))
    converged(served, st)


def test_crash_after_the_journal_before_the_append(served, human, ready, monkeypatch):
    st, prepared, signed = sign_and_submit(served, human)
    real = host_human.append_signed

    def dies(*a, **k):
        raise RuntimeError("killed")

    monkeypatch.setattr(host_human, "append_signed", dies)
    with pytest.raises(AssertionError):
        ok(human.submit_res(prepared["request"]["id"], signed.events))
    monkeypatch.setattr(host_human, "append_signed", real)
    assert mfiles(served).installs()["addons"] == {} and not addon_dir(served).exists()
    on_worker(
        served, lambda: host_human.recover(served.service)
    )  # start-up: the journalled submission is rolled forward, finish() swaps
    M.turn_on(human)  # 1237: it was signed Off
    converged(served, st)


def test_an_update_crashed_between_the_renames_keeps_no_old_code_running(served, human, ready, monkeypatch):
    st, prepared, signed = sign_and_submit(served, human)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    M.turn_on(human)  # 1237: an install is signed Off; these tests are about a granted AND on add-on
    ready.add("standup", "1.1.0")
    ready.version = 2
    ok(person(served, "market.refresh"))
    st2 = ok(stage(served, "standup", update=True))["data"]
    p2 = human.prepare("addon.grant", {"name": "standup", "staged": st2["stage"]})
    s2 = human.sign(p2)
    real = os.rename
    seen = []

    def dying(src, dst, **kw):
        seen.append(str(src))
        if ".new-" in str(src):  # the old one moved aside, the new one not yet in place
            raise OSError("power cut")
        return real(src, dst, **kw)

    monkeypatch.setattr(marketinstall.os, "rename", dying)
    ok(human.submit_res(p2["request"]["id"], s2.events))
    monkeypatch.undo()
    assert registry_state(served) != "active"  # the 1.0.0 bytes (or none) do not match the 1.1.0 grant
    on_worker(served, lambda: marketinstall.recover(served.service))
    assert package.read_package(addon_dir(served)).digest == st2["package_sha256"]
    assert registry_state(served) == "active"
    assert mfiles(served).installs()["addons"]["standup"]["version"] == "1.1.0"


def test_debris_that_is_not_the_grant_is_removed_and_a_planted_one_is_never_adopted(served, human, ready):
    st, prepared, signed = sign_and_submit(served, human)
    base = addon_dir(served).parent
    base.mkdir(parents=True, exist_ok=True)
    evil = base / ".standup.new-planted"
    evil.mkdir()
    (evil / "orch-addon.json").write_bytes(b"{}")
    (evil / "main.py").write_bytes(b"import os; os.system('x')")
    os.symlink("/etc", base / ".standup.old-link")
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    M.turn_on(human)  # 1237: an install is signed Off; these tests are about a granted AND on add-on
    converged(served, st)
    assert b"os.system" not in (addon_dir(served) / "main.py").read_bytes()


def test_an_expired_stage_with_no_grant_is_dropped_at_start(served, ready):
    st = ok(stage(served))["data"]
    f = mfiles(served)
    doc = f.read_stage(st["stage"])
    path = f.dir / "staged" / f"{st['stage']}.json"
    import json

    path.write_bytes(json.dumps({**doc, "expires_at": 1}).encode())
    path.chmod(0o600)
    on_worker(served, lambda: marketinstall.recover(served.service))
    assert f.stages() == []
