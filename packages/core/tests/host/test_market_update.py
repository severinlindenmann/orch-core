"""V3 G3 (#921): ``orch addon update``: the origin is pinned, the old version keeps running until the new grant lands,
a new grant is always needed, and a widened update says so."""

# ruff: noqa: F811
from __future__ import annotations

import json

import pytest

from orch.addons import install, package
from orch.market import constants
from tests.host import human_helpers
from tests.host import market_helpers as M  # noqa: E402
from tests.host.market_helpers import (  # noqa: F401
    add_market,
    addon_dir,
    err,
    files,
    mfiles,
    net,
    ok,
    on_worker,
    person,
    stage,
)  # fmt: skip
from tests.host.market_helpers import (
    install as do_install,
)
from tests.market import helpers as H

net = M.net  # the fake network (a fixture)
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def v1(served, human, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    st, _, _ = do_install(served, human)
    return st


def publish(served, net, version, caps=("network",), **kw):
    net.add("standup", version, caps, **kw)
    net.version += 1
    ok(person(served, "market.refresh"))


def state(served):
    s = served.service
    on_worker(served, s.store.refresh)
    return install.load_registry(s.store.layout.store, s.store.state.workspace.addons).state("standup")


def test_an_update_with_more_capabilities_is_widened_and_shows_what_is_new(served, human, net, v1):
    publish(served, net, "1.1.0", ("network", "spawn_agent"))
    st = ok(stage(served, update=True))["data"]
    u = st["update"]
    assert u["widened"] is True and u["added"]["capabilities"] == ["spawn_agent"] and u["removed"]["capabilities"] == []
    assert u["from"]["version"] == "1.0.0" and u["publisher"] == "same"
    assert "+ spawn_agent (capabilities, new)" in u["changes"] and "version 1.0.0 -> 1.1.0" in u["changes"]
    assert st["review"][-1].startswith("widened: yes")
    # the old version keeps running with its old grant until the new grant lands
    assert state(served) == "active" and package.read_package(addon_dir(served)).digest == v1["package_sha256"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    assert prepared["material"]["market"]["replaces"] == v1["package_sha256"]
    assert state(served) == "active" and package.read_package(addon_dir(served)).digest == v1["package_sha256"]
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert state(served) == "active"
    assert package.read_package(addon_dir(served)).digest == st["package_sha256"]
    assert mfiles(served).installs()["addons"]["standup"]["version"] == "1.1.0"
    assert mfiles(served).installs()["seen"][f"{H.OFFICIAL}|standup"] == "1.1.0"


def test_an_update_that_only_changes_code_is_not_widened(served, human, net, v1):
    publish(served, net, "1.0.1")
    u = ok(stage(served, update=True))["data"]["update"]
    assert u["widened"] is False and u["added"]["capabilities"] == [] and u["changes"] == ["version 1.0.0 -> 1.0.1"]


def test_a_declined_review_leaves_the_old_version_active(served, human, net, v1):
    publish(served, net, "1.1.0", ("network", "pty"))
    st = ok(stage(served, update=True))["data"]
    human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})  # prepared, never signed
    assert state(served) == "active"
    assert package.read_package(addon_dir(served)).digest == v1["package_sha256"]
    assert mfiles(served).installs()["addons"]["standup"]["version"] == "1.0.0"


def test_every_failure_keeps_the_old_version_installed_and_active(served, human, net, v1):
    publish(served, net, "1.1.0", digest="sha256:" + "0" * 64)
    assert err(stage(served, update=True)) == "market.digest_mismatch"
    publish(
        served, net, "1.0.0", pkg=files("standup", "1.0.0") | {"main.py": b"changed\n"}
    )  # same version, other bytes
    assert err(stage(served, update=True)) == "market.version_reused"
    publish(served, net, "0.9.0")
    assert err(stage(served, update=True)) == "market.rollback"
    assert state(served) == "active" and package.read_package(addon_dir(served)).digest == v1["package_sha256"]
    assert mfiles(served).stages() == []


def test_up_to_date_and_not_installed(served, human, net, v1):
    assert ok(stage(served, update=True))["data"] == {"up_to_date": True, "name": "standup", "version": "1.0.0"}
    assert err(stage(served, "nothing", update=True)) == "market.not_installed"


def test_the_origin_is_pinned(served, human, net, v1):
    assert err(stage(served, update=True, url="https://github.com/acme/market")) == "market.origin_changed"
    f = mfiles(served)
    with f.commit_installs() as write:  # a record that says another marketplace installed it
        cur = f.installs()
        cur["addons"]["standup"]["origin"] = "https://github.com/acme/market"
        write(cur)
    publish(served, net, "1.1.0")
    assert err(stage(served, url=H.OFFICIAL)) == "market.origin_changed"  # this marketplace is not where it came from
    assert net.fetches == ["standup"]  # only the install fetched


def _new_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from orch.crypto.encoding import b64u

    k = ec.generate_private_key(ec.SECP256R1())
    return k, b64u(k.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def test_an_official_key_rotation_updates_and_moves_the_remembered_key(served, human, net, v1, monkeypatch):
    """#1191: the official entry is re-keyed to the other pinned key; the update goes through (shown as a new key,
    widened) and the TOFU record moves to the new key. The version floor is untouched."""
    nxt = _new_key()
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64, nxt[1]))
    publish(served, net, "1.1.0", key=nxt)
    st = ok(stage(served, update=True))["data"]
    assert st["update"]["publisher"] == "first_use" and st["update"]["widened"] is True
    assert any("publisher key changed" in x for x in st["update"]["changes"])
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    assert prepared["material"]["market"]["publisher"]["tofu"] == "new"
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    rec = mfiles(served).installs()
    assert rec["keys"] == {f"{H.OFFICIAL}|standup": nxt[1]} and rec["addons"]["standup"]["publisher_key"] == nxt[1]
    assert rec["seen"][f"{H.OFFICIAL}|standup"] == "1.1.0"
    # the next release pins (next, new-next): an index still signed by the retired key is refused at refresh
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (nxt[1], _new_key()[1]))
    net.version += 1
    markets = ok(person(served, "market.refresh"))["data"]["markets"]
    assert markets == [{"url": H.OFFICIAL, "error": "market.index_unsigned"}]


def test_rotation_to_a_key_this_release_does_not_pin_is_refused(served, human, net, v1, monkeypatch):
    stranger = _new_key()
    publish(served, net, "1.1.0", key=stranger)  # OFFICIAL_KEYS is (H.KEY_B64,)
    assert err(stage(served, update=True)) == "market.official_unsigned"
    assert mfiles(served).installs()["keys"] == {f"{H.OFFICIAL}|standup": H.KEY_B64}  # the remembered key stays


def test_submit_rechecks_the_rotation_against_the_keys_pinned_now(served, human, net, v1, monkeypatch):
    """Staged under (current, next) as a rotation; the pins change before the grant lands: recheck refuses."""
    nxt = _new_key()
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64, nxt[1]))
    publish(served, net, "1.1.0", key=nxt)
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64,))  # the next key is no longer pinned
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.official_unsigned"  # C1
    assert mfiles(served).installs()["keys"] == {f"{H.OFFICIAL}|standup": H.KEY_B64}


def test_widened_is_not_fooled_by_a_tampered_installed_directory(served, human, net, v1):
    """Review F1: a same-user agent rewrites the installed manifest so the OLD package already "declares" spawn_agent.
    The old side of the diff is the signed grant: bytes that are not it make everything count as widened."""
    d = addon_dir(served)
    doc = json.loads((d / "orch-addon.json").read_bytes())
    doc["capabilities"] = sorted({*doc["capabilities"], "spawn_agent"})
    (d / "orch-addon.json").write_bytes(json.dumps(doc, sort_keys=True).encode())
    publish(served, net, "1.1.0", ("network", "spawn_agent"))
    assert ok(stage(served, update=True))["data"]["update"]["widened"] is True


def test_a_version_revoked_after_staging_cannot_be_granted(served, human, net, v1):
    """Review F2: the index is read again at submit."""
    publish(served, net, "1.1.0")
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    real = net.doc
    net.doc = lambda **more: real(revoked=["standup@1.1.0"], **more)
    net.version += 1
    ok(person(served, "market.refresh"))
    res = human.submit_res(prepared["request"]["id"], human.sign(prepared).events)
    assert err(res) == "market.revoked"
    assert mfiles(served).installs()["addons"]["standup"]["version"] == "1.0.0"


def test_a_stage_whose_class_is_gone_cannot_be_granted(served, human, net, v1):
    publish(served, net, "1.1.0")
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    f = mfiles(served)
    path = f.dir / "staged" / f"{st['stage']}.json"
    doc = f.read_stage(st["stage"])
    path.write_bytes(json.dumps({**doc, "class": "dev"}).encode())
    path.chmod(0o600)
    assert err(human.submit_res(prepared["request"]["id"], human.sign(prepared).events)) == "market.staged_changed"


def test_a_hub_joined_between_stage_and_submit_refuses_the_grant_and_changes_nothing(
    served, human, net, v1, monkeypatch
):
    from orch.host import marketops

    publish(served, net, "1.1.0")
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    before = mfiles(served).installs()
    monkeypatch.setattr(marketops, "hub_joined", lambda s: True)
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.not_allowed"
    assert mfiles(served).installs() == before  # TOFU key, seen and the record are untouched by a refused recheck
    assert package.read_package(addon_dir(served)).digest == v1["package_sha256"]


def test_the_index_must_still_list_the_staged_bytes_at_submit(served, human, net, v1):
    publish(served, net, "1.1.0")
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    publish(served, net, "1.2.0")  # the marketplace moved on: 1.1.0 is no longer what it lists
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.staged_changed"
    assert mfiles(served).installs()["addons"]["standup"]["version"] == "1.0.0"


def test_an_older_index_than_the_stage_saw_is_refused_at_submit(served, human, net, v1):
    publish(served, net, "1.1.0")
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    f = mfiles(served)
    doc = f.read_stage(st["stage"])
    path = f.dir / "staged" / f"{st['stage']}.json"
    path.write_bytes(json.dumps({**doc, "index_version": 99}).encode())
    path.chmod(0o600)
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.staged_changed"
