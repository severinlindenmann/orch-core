# ruff: noqa: F811
"""V3 G6 (#924) C4 at host level: the stored key goes with the grant (purge, a re-grant without ``credential``) and the
sweep at start is idempotent."""

from __future__ import annotations

import os

import pytest

from orch.addons import credential as cr
from orch.host import marketinstall
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.market_helpers import (  # noqa: F401
    add_market,
    err,
    files,
    mfiles,
    net,
    ok,
    person,
    stage,
)  # fmt: skip
from tests.host.market_helpers import install as do_install

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
UID = os.getuid()
API = {"api": ">=1.1,<2"}
KEY = cr.Secret("k-host-level-0123456789")


def publish(served, net, version, caps):
    net.add("standup", version, caps, pkg=files("standup", version, caps, **API))
    net.version += 1
    ok(person(served, "market.refresh"))


@pytest.fixture
def v1(served, human, net):
    net.add(
        "standup", "1.0.0", ("credential", "network"), pkg=files("standup", "1.0.0", ("credential", "network"), **API)
    )
    M.add_market(served, net)
    out = do_install(served, human)[0]
    cr.store(served.service.state_dir, served.service.workspace_id, UID, "standup", KEY, 1.0)
    return out


def stored(served) -> str:
    s = served.service
    return cr.status(s.state_dir, s.workspace_id, UID, "standup")


def sign(human, op, args):
    prepared = human.prepare(op, args)
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))


def test_a_purge_deletes_the_key_and_a_restart_sweep_is_idempotent(served, human, net, v1):
    assert stored(served) == "stored"
    sign(human, "addon.disable", {"name": "standup"})
    assert stored(served) == "stored"  # disabled: the grant still holds the capability, the key stays (not injected)
    sign(human, "addon.purge", {"name": "standup"})
    assert stored(served) == "missing"
    s = served.service
    for _ in range(2):  # restart: recover() sweeps again, nothing to do
        M.on_worker(served, lambda: marketinstall.recover(s))
        assert stored(served) == "missing"
    cr.store(
        s.state_dir, s.workspace_id, UID, "standup", KEY, 2.0
    )  # a key left behind (crash between append and sweep)
    M.on_worker(served, lambda: marketinstall.recover(s))
    assert stored(served) == "missing"


def test_a_regrant_without_credential_deletes_the_key(served, human, net, v1):
    publish(served, net, "1.1.0", ("network",))
    st = ok(stage(served, update=True))["data"]
    assert stored(served) == "stored"  # staging alone changes nothing
    sign(human, "addon.grant", {"name": "standup", "staged": st["stage"]})
    assert stored(served) == "missing"


def test_uninstall_deletes_the_key(served, human, net, v1):
    sign(human, "addon.disable", {"name": "standup"})
    assert ok(person(served, "market.uninstall", {"name": "standup"}))["data"]["removed"] is True
    assert stored(served) == "missing"
