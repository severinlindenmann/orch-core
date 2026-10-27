"""``orch addon list|grant|disable|purge`` end to end: the package, the signed event, the roles (ticket-format §8.1)."""

# ruff: noqa: F811
from __future__ import annotations

import os

from orch import canon
from orch.addons import install
from orch.addons.install import load_registry
from orch.addons.package import read_package
from tests.addons.helpers import installed_package, manifest, record_install
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
    installed_package(hws)
    r = me("addon", "list", "--json")
    assert r.doc["data"]["addons"] == [{"name": "echo", "version": "1.0.0", "enabled": False, "state": "not_granted"}]
    assert "echo 1.0.0 not granted" in me("addon", "list").out
    assert "orch addon grant NAME" in me("addon", "list").out


def test_the_owner_grants_a_package_and_the_event_carries_digest_capabilities_and_binds(hws, me):
    d = installed_package(hws)
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
    assert "binds" not in e and e["artifact_kinds"] == ["chart"]
    assert e["fields"]["mood"] == {
        "type": "enum",
        "values": ["good", "bad"],
        "set_by": ["owner"],
        "gate": ["plan", "verify"],
    }
    assert (
        e["fields"]["points"]["set_by"] == ["addon", "agent", "maintainer", "owner"]
        and "show" not in e["fields"]["points"]
    )
    assert e["sections"] == [
        {"id": "echo.extra", "types": ["feature"], "heading_key": "echoextra"},
        {"id": "echo.notes", "types": ["bug", "feature"], "gate": ["plan"], "heading_key": "echonotes"},
    ]
    assert e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase"  # a person signed it
    assert f"package {pkg.digest}" in r.out and "capabilities serve_http" in r.out
    a = addons(hws)["echo"]
    assert a.enabled and not a.purged and a.package_sha256 == pkg.digest
    assert hws.provider.shown and "echo" in hws.provider.shown[-1]  # the signing prompt names the grant
    lst = me("addon", "list", "--json").doc["data"]["addons"]
    assert lst[0]["fields"] == e["fields"]  # the grant's own fields (``orch describe echo.FIELD`` reads them)
    assert [{k: v for k, v in row.items() if k != "fields"} for row in lst] == [
        {"name": "echo", "version": "1.0.0", "enabled": True, "state": "active", "capabilities": ["serve_http"]}
    ]


def test_an_agent_cannot_grant_disable_or_purge(hws, me, agent):
    installed_package(hws)
    for verb in ("grant", "disable", "purge"):
        r = agent("addon", verb, "echo", "--json")
        assert r.code != 0 and r.err_code == "human_only", (verb, r.out, r.err)
    assert not ws_events(hws, "addon.granted", "addon.disabled", "addon.purged")


def test_only_an_owner_may_grant(hws, me):
    installed_package(hws)
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
    d = installed_package(hws)
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
    installed_package(hws)
    assert me("addon", "grant", "echo").code == 0
    r = me("addon", "disable", "echo")
    assert r.code == 0 and r.first.startswith("ok addon.disabled echo seq=")
    assert not addons(hws)["echo"].enabled
    assert "echo 1.0.0 disabled" in me("addon", "list").out
    assert me("addon", "list", "--json").doc["data"]["addons"][0]["enabled"] is False
    assert me("addon", "grant", "echo").code == 0
    assert addons(hws)["echo"].enabled


def test_purge_then_grant_starts_again(hws, me):
    installed_package(hws)
    me("addon", "grant", "echo")
    r = me("addon", "purge", "echo")
    assert r.code == 0 and r.first.startswith("ok addon.purged echo seq=")
    assert addons(hws)["echo"].purged
    assert "echo 1.0.0 purged" in me("addon", "list").out
    assert me("addon", "grant", "echo").code == 0
    a = addons(hws)["echo"]
    assert a.enabled and not a.purged


def test_a_package_changed_after_the_grant_is_listed_as_changed_and_registers_nothing(hws, me):
    d = installed_package(hws)
    me("addon", "grant", "echo")
    (d / "addon.py").write_text("print('evil')\n")
    assert "echo 1.0.0 changed" in me("addon", "list").out
    reg = load_registry(hws.store_dir, view(hws))
    assert reg.state("echo") == "changed" and reg.active() == []
    assert reg.field_spec("echo", "points") is None
    (d / "orch-addon.json").write_text(__import__("json").dumps(manifest(version="2.0.0")))
    assert load_registry(hws.store_dir, view(hws)).state("echo") == "changed"


def test_a_dry_run_signs_nothing(hws, me):
    installed_package(hws)
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
    installed_package(hws)
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


def test_the_owner_reads_what_runs_before_the_passphrase(hws, me):
    man = manifest(
        agents_md="Estimates are hints only.",
        needs=[{"id": "big", "when": ["gt", ["field", "points"], 3], "who": ["owner"], "text": "large"}],
    )
    man["entry"] = {"cmd": ["/usr/bin/env-python", "{pkg}/addon.py", "--flag", "a b"]}
    installed_package(hws, man=man)
    assert me("addon", "grant", "echo").code == 0
    text = hws.reviews[-1]
    assert hws.expects[-1] == "echo"  # the owner types the addon's name
    pkg_digest = read_package(hws.store_dir / "addons" / "echo").digest
    for needle in (
        "addon echo 1.0.0",
        pkg_digest,
        "command /usr/bin/env-python '{pkg}/addon.py' --flag 'a b'",  # shlex-quoted: a space inside an argument shows
        "capabilities serve_http",
        "agents see: - echo: Estimates are hints only.",
        "needs rule big for owner: large",
        "field mood (enum) set by owner, bound to plan, verify",
        "section echo.notes for bug, feature",
        "artifact kinds chart",
    ):
        assert needle in text, needle


def test_a_declined_review_signs_nothing(hws, me):
    installed_package(hws)
    hws.confirm = False
    r = me("addon", "grant", "echo", "--json")
    assert r.code != 0 and "nothing was signed" in r.doc["error"]["message"]
    assert not ws_events(hws, "addon.granted")


def test_the_review_of_a_regrant_says_what_changed(hws, me):
    d = installed_package(hws)
    me("addon", "grant", "echo")
    (d / "addon.py").write_text((d / "addon.py").read_text() + "\n# changed\n")
    man = manifest(version="1.1.0", capabilities=["serve_http", "network"])
    man["fields"]["points"]["set_by"] = ["owner"]
    del man["fields"]["done"]
    (d / "orch-addon.json").write_text(__import__("json").dumps(man))
    record_install(hws)  # the update is installed (what a signed update leaves)
    assert me("addon", "grant", "echo").code == 0
    text = hws.reviews[-1]
    assert "previous grant: 1.0.0" in text
    for needle in (
        "version 1.0.0 -> 1.1.0",
        "capabilities ['serve_http'] -> ['network', 'serve_http']",
        "field points changed",
        "field done removed",
    ):
        assert needle in text, needle
    assert "changed: the package bytes only" not in text


def test_text_from_the_package_is_shown_as_escaped_ascii(hws, me):
    man = manifest()
    man["needs"] = [{"id": "r", "when": True, "who": ["owner"], "text": "caf\u00e9 \u0430 look-alike"}]
    man["entry"] = {"cmd": ["/usr/bin/env-python", "{pkg}/caf\u00e9"]}
    installed_package(hws, man=man)
    assert me("addon", "grant", "echo").code == 0
    text = hws.reviews[-1]
    assert text.isascii() and "caf\\xe9" in text and "\\u0430" in text


def test_a_control_character_in_the_manifest_never_reaches_the_prompt(hws, me):
    man = manifest()
    man["needs"] = [{"id": "r", "when": True, "who": ["owner"], "text": "evil \x1b[31m text"}]
    installed_package(hws, man=man)
    r = me("addon", "grant", "echo", "--json")
    assert r.code != 0 and r.err_code == "invalid.input" and not hws.reviews


def test_a_heading_taken_by_another_granted_addon_is_refused(hws, me):
    installed_package(hws)
    assert me("addon", "grant", "echo").code == 0
    other = manifest(name="other", title="Other")
    other["fields"] = {}
    other["sections"] = [{"id": "n", "heading": "echo  notes".replace("  ", " "), "after": "plan", "types": ["bug"]}]
    other["sections"][0]["heading"] = "ECHO NOTES"
    other["needs"] = []
    installed_package(hws, "other", man=other)
    r = me("addon", "grant", "other", "--json")
    assert r.code != 0 and r.err_code == "invalid.input" and "already used" in r.doc["error"]["message"]
    other["sections"][0]["heading"] = "Other notes"
    (hws.store_dir / "addons" / "other" / "orch-addon.json").write_text(__import__("json").dumps(other))
    record_install(hws, "other")  # the corrected package is installed again
    assert me("addon", "grant", "other").code == 0


def test_disable_and_purge_are_idempotent_and_purge_says_what_it_removed(hws, me):
    installed_package(hws)
    me("addon", "grant", "echo")
    uid = hws.new_ticket("t")
    hws.store.refresh()
    hws.store.append(
        {
            "type": "ticket.updated",
            "actor": hws.agent,
            "base_rev": {"ticket.addons.echo.points": canon.value_hash(None)},
            "set": {"ticket.addons.echo.points": 5},
        },
        log=uid,
        body={},
    )
    n = len(hws.read_events("workspace"))
    assert me("addon", "disable", "echo").code == 0
    again = me("addon", "disable", "echo")
    assert again.code == 0 and "already disabled" in again.out and len(hws.read_events("workspace")) == n + 1
    r = me("addon", "purge", "echo")
    assert r.code == 0 and "removed 1 fields, 0 sections, 0 artifacts of 1 tickets" in r.out
    again = me("addon", "purge", "echo")
    assert again.code == 0 and "already purged" in again.out and len(hws.read_events("workspace")) == n + 2
    assert "already purged" in me("addon", "disable", "echo").out
    assert len(hws.read_events("workspace")) == n + 2


def test_list_says_the_state_and_what_to_do_about_a_changed_package(hws, me):
    d = installed_package(hws)
    me("addon", "grant", "echo")
    (d / "addon.py").write_text("# other\n")
    r = me("addon", "list", "--json")
    (row,) = r.doc["data"]["addons"]
    assert {k: v for k, v in row.items() if k != "fields"} == {
        "name": "echo", "version": "1.0.0", "enabled": False, "state": "changed",
    }  # fmt: skip
    assert "changed since the grant: orch addon grant echo" in me("addon", "list").out
    me("addon", "purge", "echo")
    assert me("addon", "list", "--json").doc["data"]["addons"][0]["state"] == "purged"


def test_the_typed_word_of_a_workspace_event_is_the_name_it_signs(monkeypatch):
    """A2: the caller cannot choose the word; a workspace event without a name is refused before any prompt."""
    import types

    import pytest

    from orch.ops import human as human_mod
    from orch.ops.errors import OrchError
    from orch.ops.human import Human

    asked = []
    monkeypatch.setattr(human_mod, "review_prompt", lambda text, word: asked.append(word) or True)
    h = Human.__new__(Human)
    h.ctx = types.SimpleNamespace(dry_run=False, prepare=None)
    h.precheck = lambda event, log: None
    h._sign_and_append = lambda *a: "done"
    assert h.run({"type": "addon.granted", "name": "echo"}, "workspace", "x", review=["r"]) == "done"
    assert asked == ["echo"]
    with pytest.raises(OrchError) as e:
        h.run({"type": "member.removed", "person": "p_x"}, "workspace", "x", review=["r"])
    assert e.value.code == "invalid.input" and asked == ["echo"]


def test_a_builtin_addon_never_installed_is_put_in_place_by_its_grant(hws, me):
    """Turning on terminals from the dashboard after setup left them off: no package on disk yet, the grant installs
    the shipped manifest and signs its digest."""
    assert not (hws.store_dir / "addons" / "terminals").exists()
    r = me("addon", "grant", "terminals")
    assert r.code == 0, r.err
    e = ws_events(hws, "addon.granted")[-1]
    assert e["name"] == "terminals"
    assert e["package_sha256"] == read_package(hws.store_dir / "addons" / "terminals").digest
    assert me("addon", "grant", "nonexistent").code != 0


def test_a_dry_run_and_a_non_owner_install_nothing(hws, me):
    assert me("addon", "grant", "terminals", "--dry-run").code != 0
    assert not (hws.store_dir / "addons" / "terminals").exists()
    m = hws.add_member("maria", "maintainer")
    hws.act_as(m)
    r = me("addon", "grant", "terminals", "--json")
    assert r.code != 0 and r.err_code == "role.denied"
    assert not (hws.store_dir / "addons" / "terminals").exists()


def test_a_failed_builtin_install_removes_the_empty_directory_and_says_why(hws, me, monkeypatch):
    from orch.addons import builtin

    def broken(workspace, name, data, force=False):
        os.mkdir(os.path.join(workspace, "addons")) if not os.path.isdir(os.path.join(workspace, "addons")) else None
        os.mkdir(os.path.join(workspace, "addons", name))
        raise OSError("disk full")

    monkeypatch.setattr(builtin, "write_manifest", broken)
    r = me("addon", "grant", "terminals", "--json")
    assert r.code != 0 and r.err_code == "invalid.input"
    assert not (hws.store_dir / "addons" / "terminals").exists()


def test_a_builtin_that_needs_settings_is_not_granted_until_they_are_set(hws, me):
    """AE4 in direct mode too: the CLI reads the same rule as the host (addons/settings_needs.py)."""
    r = me("addon", "grant", "publish")
    assert r.code != 0 and "needs its settings first" in r.err + r.out
    assert not (hws.store_dir / "addons" / "publish").exists() and not ws_events(hws, "addon.granted")
    store = hws.other()
    hws.store = store
    try:
        store.append(
            hws.person_event(
                hws.owner,
                "workspace",
                "settings.changed",
                set={"publish": {"server": "https://files.example.com", "share": "https://share.example.com"}},
            ),
            log="workspace",
        )
    finally:
        store.close()
        hws.store = None
    r = me("addon", "grant", "publish")
    assert r.code == 0, r.err
    assert ws_events(hws, "addon.granted")[-1]["name"] == "publish"
    assert me("addon", "preview", "publish", "--json").doc["data"]["on"] is True


def test_preview_is_read_only_and_equals_the_grant_that_follows(hws, me):
    d = installed_package(hws)
    before = me("addon", "preview", "echo", "--json")
    assert before.code == 0 and before.doc["data"]["on"] is False and before.doc["data"]["needs"] is None
    assert not ws_events(hws, "addon.granted")
    assert me("addon", "grant", "echo").code == 0
    e = ws_events(hws, "addon.granted")[-1]
    assert before.doc["data"]["grant"] == {k: v for k, v in e.items() if k in before.doc["data"]["grant"]}
    assert set(before.doc["data"]["grant"]) == {
        "name", "version", "package_sha256", "capabilities", "fields", "sections", "artifact_kinds",
    }  # fmt: skip
    assert read_package(d).digest == before.doc["data"]["grant"]["package_sha256"]
    missing = me("addon", "preview", "nope", "--json")
    assert missing.code != 0 and missing.doc["error"]["code"] == "not_found"


def test_the_install_review_shows_the_skill_text(hws, me):
    from tests.addons.helpers import manifest as base_manifest

    text = "---\nname: orch-addon-echo\ndescription: Use when echo. Not for x.\n---\n# Echo\n\n1. Run `orch show`.\n"
    man = base_manifest(
        schema="orch.addon/3", api=">=1.2,<2", min_core="2.0.0.dev0", skill="skills/main", capabilities=[]
    )
    installed_package(hws, man=man, files={"skills/main/SKILL.md": text.encode()})
    assert me("addon", "grant", "echo").code == 0
    review = hws.reviews[-1]
    assert "skill orch-addon-echo (" in review and "  | 1. Run `orch show`." in review
    # direct mode: the signed grant brings the files along (design D2)
    assert (hws.root / ".agents/skills/orch-addon-echo/SKILL.md").read_text() == text
    assert "orch-addon-echo" in (hws.store_dir / "AGENTS.orch.md").read_text()
    assert me("addon", "disable", "echo").code == 0
    assert not (hws.root / ".agents/skills/orch-addon-echo").exists()
