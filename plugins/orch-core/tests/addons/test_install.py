"""``orch addon list|grant|disable|purge`` end to end: the package, the signed event, the roles (ticket-format §8.1)."""

# ruff: noqa: F811
from __future__ import annotations

import os

from orch.addons import install
from orch.addons.install import load_registry
from orch.addons.package import read_package
from tests.addons.helpers import make_package, manifest
from tests.ops.humans import agent, hws, me  # noqa: F401  (fixtures)


def ws_events(hws, *types):
    return [e for e in hws.read_events("workspace") if e["type"] in types]


def view(hws):
    s = hws.other()
    try:
        return dict(s.state.workspace.addons)
    finally:
        s.close()


def addons(hws):
    s = hws.other()
    try:
        return install.as_addons(s.state.workspace.addons)
    finally:
        s.close()


def test_list_shows_installed_packages_that_are_not_granted(hws, me):
    r = me("addon", "list")
    assert r.code == 0 and r.first == "ok addon.list 0"
    make_package(hws.root)
    r = me("addon", "list", "--json")
    assert r.doc["data"]["addons"] == [{"name": "echo", "version": "1.0.0", "enabled": False}]
    assert "echo 1.0.0 not granted" in me("addon", "list").out
    assert "orch addon grant NAME" in me("addon", "list").out


def test_the_owner_grants_a_package_and_the_event_carries_digest_capabilities_and_binds(hws, me):
    d = make_package(hws.root)
    r = me("addon", "grant", "echo")
    assert r.code == 0, r.err
    assert r.first.startswith("ok addon.granted echo seq=")
    e = ws_events(hws, "addon.granted")[-1]
    pkg = read_package(d)
    assert (e["name"], e["version"], e["package_sha256"], e["capabilities"]) == (
        "echo",
        "1.0.0",
        pkg.digest,
        ["serve_http"],
    )
    assert e["binds"] == {
        "fields": {"mood": ["plan", "verify"], "points": ["plan"]},
        "sections": [{"id": "echo.notes", "gate": ["plan"], "types": ["bug", "feature"]}],
    }
    assert e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase"  # a person signed it
    assert f"package {pkg.digest}" in r.out and "capabilities serve_http" in r.out
    a = addons(hws)["echo"]
    assert a.enabled and not a.purged and a.package_sha256 == pkg.digest
    assert hws.provider.shown and "echo" in hws.provider.shown[-1]  # the signing prompt names the grant
    lst = me("addon", "list", "--json").doc["data"]["addons"]
    assert lst == [{"name": "echo", "version": "1.0.0", "enabled": True}]


def test_an_agent_cannot_grant_disable_or_purge(hws, me, agent):
    make_package(hws.root)
    for verb in ("grant", "disable", "purge"):
        r = agent("addon", verb, "echo", "--json")
        assert r.code != 0 and r.err_code == "human_only", (verb, r.out, r.err)
    assert not ws_events(hws, "addon.granted", "addon.disabled", "addon.purged")


def test_only_an_owner_may_grant(hws, me):
    make_package(hws.root)
    m = hws.add_member("maria", "maintainer")
    hws.act_as(m)
    r = me("addon", "grant", "echo", "--json")
    assert r.code != 0 and r.err_code == "role.denied"
    assert not ws_events(hws, "addon.granted")


def test_a_missing_package_and_a_bad_name(hws, me):
    assert me("addon", "grant", "echo", "--json").err_code == "not_found"
    assert me("addon", "grant", "../x", "--json").err_code == "invalid.input"
    assert me("addon", "grant", "Echo", "--json").err_code == "invalid.input"
    assert me("addon", "disable", "echo", "--json").err_code == "not_found"
    assert me("addon", "purge", "echo", "--json").err_code == "not_found"


def test_a_package_that_breaks_a_rule_is_not_granted(hws, me, tmp_path):
    d = make_package(hws.root)
    secret = tmp_path / "secret"
    secret.write_text("s")
    os.symlink(secret, d / "link")
    r = me("addon", "grant", "echo", "--json")
    assert r.code != 0 and r.err_code == "invalid.input" and "link" in r.doc["error"]["message"]
    os.unlink(d / "link")
    (d / "orch-addon.json").write_text("{not json")
    assert me("addon", "grant", "echo", "--json").err_code == "invalid.input"
    bad = manifest(name="other")
    (d / "orch-addon.json").write_text(__import__("json").dumps(bad))
    r = me("addon", "grant", "echo", "--json")
    assert r.err_code == "invalid.input" and "other" in r.doc["error"]["message"]
    assert not ws_events(hws, "addon.granted")


def test_disable_keeps_the_grant_and_a_new_grant_enables_it_again(hws, me):
    make_package(hws.root)
    assert me("addon", "grant", "echo").code == 0
    r = me("addon", "disable", "echo")
    assert r.code == 0 and r.first.startswith("ok addon.disabled echo seq=")
    assert not addons(hws)["echo"].enabled
    assert "echo 1.0.0 disabled" in me("addon", "list").out
    assert me("addon", "list", "--json").doc["data"]["addons"][0]["enabled"] is False
    assert me("addon", "grant", "echo").code == 0
    assert addons(hws)["echo"].enabled


def test_purge_then_grant_starts_again(hws, me):
    make_package(hws.root)
    me("addon", "grant", "echo")
    r = me("addon", "purge", "echo")
    assert r.code == 0 and r.first.startswith("ok addon.purged echo seq=")
    assert addons(hws)["echo"].purged
    assert "echo 1.0.0 purged" in me("addon", "list").out
    assert me("addon", "grant", "echo").code == 0
    a = addons(hws)["echo"]
    assert a.enabled and not a.purged


def test_a_package_changed_after_the_grant_is_listed_as_changed_and_registers_nothing(hws, me):
    d = make_package(hws.root)
    me("addon", "grant", "echo")
    (d / "addon.py").write_text("print('evil')\n")
    assert "echo 1.0.0 changed" in me("addon", "list").out
    reg = load_registry(hws.root, view(hws))
    assert reg.state("echo") == "changed" and reg.active() == []
    assert reg.field_spec("echo", "points") is None
    (d / "orch-addon.json").write_text(__import__("json").dumps(manifest(version="2.0.0")))
    assert load_registry(hws.root, view(hws)).state("echo") == "changed"


def test_a_dry_run_signs_nothing(hws, me):
    make_package(hws.root)
    n = len(hws.read_events("workspace"))
    r = me("addon", "grant", "echo", "--dry-run")
    assert r.code == 0 and "dry-run" in r.out
    assert len(hws.read_events("workspace")) == n


def test_the_grant_changes_the_gate_hash_of_a_bound_gate_and_purge_restores_it(hws, me, agent):
    from tests.ops.humans import make_ticket

    key = make_ticket(agent)
    s = hws.other()
    uid = s.uid_of(key)
    before = s.ticket(uid).gates["plan"].hash
    s.close()
    make_package(hws.root)
    me("addon", "grant", "echo")
    s = hws.other()
    granted = s.ticket(uid).gates["plan"].hash
    s.close()
    assert granted != before  # addon_packages joined the hash input
    me("addon", "purge", "echo")
    s = hws.other()
    after = s.ticket(uid).gates["plan"].hash
    s.close()
    assert after == before
