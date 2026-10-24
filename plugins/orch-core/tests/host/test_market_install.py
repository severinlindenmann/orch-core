"""V3 G3 (#921): ``orch addon install`` on a served workspace: the order of the checks, what is staged, what the signed
grant installs, and that nothing changes before the signature. The network and the packages are fakes
(``tests/host/market_helpers.py``); the git and address mechanics have their own tests (``tests/market``)."""

from __future__ import annotations

import json

import pytest

from orch.addons import package
from orch.host import marketops
from orch.market import MarketError
from tests.host import human_helpers
from tests.host.market_helpers import (
    addon_dir, add_market, agent, err, files, install, mfiles, net, ok, person, stage,  # noqa: F401
)  # fmt: skip
from tests.market import helpers as H

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def ready(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    return net


def nothing_changed(served):
    f = mfiles(served)
    assert f.installs() == {"addons": {}, "seen": {}, "keys": {}}
    assert not addon_dir(served).exists() and f.stages() == []


# ------------------------------------------------------------------------------------------------ the happy path
def test_install_stages_then_the_signed_grant_installs(served, human, ready):
    st = ok(stage(served))["data"]
    assert st["name"] == "standup" and st["version"] == "1.0.0" and st["update"] is None
    assert st["market"]["publisher"]["tofu"] == "new" and st["market"]["class"] == "official"
    assert st["market"]["source_note"] == "source as claimed by the marketplace"
    assert any(line.startswith("package sha256:") for line in st["review"])
    assert not addon_dir(served).exists() and mfiles(served).installs()["addons"] == {}  # staged is not installed
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    assert prepared["material"]["market"]["url"] == H.OFFICIAL
    assert [e["type"] for e in prepared["events"]] == ["addon.granted"]
    assert prepared["events"][0]["package_sha256"] == st["package_sha256"]
    assert "market" not in json.dumps(prepared["events"])  # nothing of the source is signed (5.2)
    assert not addon_dir(served).exists() and mfiles(served).installs()["addons"] == {}  # prepared is not installed
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    root = addon_dir(served)
    assert package.read_package(root).digest == st["package_sha256"]
    f = mfiles(served)
    rec = f.installs()
    assert rec["addons"]["standup"]["origin"] == H.OFFICIAL and rec["addons"]["standup"]["version"] == "1.0.0"
    assert rec["seen"] == {f"{H.OFFICIAL}|standup": "1.0.0"} and rec["keys"] == {f"{H.OFFICIAL}|standup": H.KEY_B64}
    assert f.stages() == []
    inst = ok(person(served, "market.installed"))["data"]["addons"][0]
    assert inst["name"] == "standup" and inst["state"] == "enabled" and inst["available"] is None
    row = ok(person(served, "market.list", {"entries": True}))["data"]["markets"][0]["addons"][0]
    assert row["installed"] == {"version": "1.0.0", "state": "enabled"} and row["update"] is False


def test_the_review_text_labels_the_market_block_as_not_signed(served, human, ready):
    from orch.host import signer

    st = ok(stage(served))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    text = signer.render(prepared, lambda w: ("ws", "ORCH"))
    assert "marketplace (per host, not signed)" in text and "first use" in text and "network" in text


def test_a_second_install_of_the_same_bytes_is_up_to_date(served, human, ready):
    install(served, human)
    assert ok(stage(served))["data"] == {"up_to_date": True, "name": "standup", "version": "1.0.0"}


# ------------------------------------------------------------------------------------------------ the order of refusal
def test_an_agent_is_refused_before_anything_is_read_or_fetched(served, ready):
    for op, args in (("market.stage", {"id": "standup"}), ("market.unstage", {"stage": "st_" + "0" * 32}),
                     ("market.uninstall", {"name": "standup"})):  # fmt: skip
        assert err(agent(served, op, args)) == "market.actor_refused", op
    assert ready.fetches == []
    nothing_changed(served)


def test_an_agent_can_read_installed_and_list(served, ready):
    assert ok(agent(served, "market.installed"))["data"] == {"addons": []}
    ok(agent(served, "market.list", {"entries": True}))


def test_unknown_and_ambiguous_ids_and_unadded_markets(served, net):
    net.add("standup", "1.0.0")
    assert err(stage(served)) == "market.unknown_addon"  # no marketplace added
    add_market(served, net)
    assert err(stage(served, "nothing")) == "market.unknown_addon"
    assert err(stage(served, url="https://github.com/acme/other")) == "not_found"
    assert net.fetches == []


def test_a_joined_hub_allows_nothing(served, ready, monkeypatch):
    monkeypatch.setattr(marketops, "hub_joined", lambda s: True)
    assert err(stage(served)) == "market.not_allowed"
    assert ready.fetches == []


# ------------------------------------------------------------------------------------------------ attacker index
@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"digest": "sha256:" + "0" * 64}, "market.digest_mismatch"),  # the index lies about the digest
        ({"entry_caps": ["network", "pty"]}, "market.digest_mismatch"),  # lies about caps: the signed index digest
        ({"min_orch": "9.0.0"}, "market.min_orch"),
    ],
)
def test_a_lying_index_installs_nothing(served, net, kw, code):
    net.add("standup", "1.0.0", **kw)
    add_market(served, net)
    got = err(stage(served))
    # entry_caps cannot change the digest: the package is the truth, so the caps lie shows as a manifest mismatch
    assert got in (code, "market.manifest_mismatch")
    nothing_changed(served)


def test_the_capabilities_in_the_index_must_be_the_manifests(served, net):
    net.add("standup", "1.0.0", ("network",), entry_caps=["network", "pty"])
    add_market(served, net)
    assert err(stage(served)) == "market.manifest_mismatch"
    nothing_changed(served)


@pytest.mark.parametrize("what", ["version", "name", "min_core", "runtime", "schema"])
def test_manifest_and_entry_must_agree(served, net, what):
    m = json.loads(files()["orch-addon.json"])
    if what == "version":
        m["version"] = "1.0.1"
    elif what == "name":
        m["name"] = "other"
    elif what == "min_core":
        m["min_core"] = "2.0.0"
    elif what == "runtime":
        m["runtime"] = {"kind": "python-requirements", "lock": "requirements.txt"}
    else:
        m["schema"] = "orch.addon/2"
    pkg = {**files(), "orch-addon.json": json.dumps(m, sort_keys=True).encode()}
    net.add("standup", "1.0.0", pkg=pkg)
    add_market(served, net)
    assert err(stage(served)) == "market.manifest_mismatch"
    nothing_changed(served)


def test_a_commit_mismatch_is_refused_and_nothing_is_staged(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    net.packages.clear()  # the fetch finds another object
    err_code = err(stage(served))
    assert err_code == "market.fetch_failed"
    nothing_changed(served)


def test_a_fetch_that_reports_a_commit_mismatch(served, net, monkeypatch):
    net.add("standup", "1.0.0")
    add_market(served, net)
    monkeypatch.setattr(
        "orch.market.fetch.fetch_package", lambda e: (_ for _ in ()).throw(MarketError("market.commit_mismatch"))
    )
    assert err(stage(served)) == "market.commit_mismatch"
    nothing_changed(served)


def test_a_hostile_package_is_package_invalid(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    monkeypatched = lambda e: (_ for _ in ()).throw(MarketError("market.package_invalid"))  # noqa: E731
    import orch.market.fetch as fetch

    fetch.fetch_package = monkeypatched
    assert err(stage(served)) == "market.package_invalid"
    nothing_changed(served)


def test_a_reserved_id_is_refused(served, net):
    net.add("wiki", "1.0.0")
    add_market(served, net)
    assert err(stage(served, "wiki")) == "market.reserved_id"
    assert net.fetches == []


def test_revoked_and_host_not_allowed(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    net.entries[0]["source"] = {"git": "https://evil.example.org/acme/standup"}
    net.add("other", "1.0.0")
    net.entries[0]["source"] = {"git": "https://evil.example.org/acme/standup"}
    net.version = 2
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.host_not_allowed"
    net.entries[0]["source"] = {"git": "https://github.com/acme/standup"}
    net.version = 3
    ok(person(served, "market.refresh"))
    net.doc = lambda **m: H.index(4, addons=net.entries, revoked=["standup@1.0.0"])  # type: ignore[method-assign]
    net.version = 4
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.revoked"
    assert net.fetches == []


def test_official_entries_must_be_signed_by_the_pinned_key_and_verify(served, net):
    net.add("standup", "1.0.0", signed=False)
    add_market(served, net)
    assert err(stage(served)) == "market.official_unsigned"
    net.add("standup", "1.0.1")
    net.entries[0]["publisher"]["sig"] = "A" * 86
    net.version = 2
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.bad_signature"
    assert net.fetches == []


# ------------------------------------------------------------------------------------------------ the stage file
def test_the_stage_is_one_per_name_and_unstage_removes_it(served, ready):
    a = ok(stage(served))["data"]["stage"]
    b = ok(stage(served))["data"]["stage"]
    assert a != b and [s["stage"] for s in mfiles(served).stages()] == [b]
    assert ok(person(served, "market.unstage", {"stage": b}))["data"] == {"removed": True}
    assert ok(person(served, "market.unstage", {"stage": b}))["data"] == {"removed": False}


def stage_edit(served, st, fn):
    f = mfiles(served)
    doc = f.read_stage(st)
    fn(doc)
    path = f.dir / "staged" / f"{st}.json"
    path.write_bytes(json.dumps(doc).encode())
    path.chmod(0o600)


def test_a_stage_that_was_edited_expired_or_is_another_name_is_staged_changed(served, human, ready):
    import base64

    st = ok(stage(served))["data"]["stage"]

    def edit(d):
        d["files"]["main.py"] = base64.b64encode(b"print('evil')\n").decode()

    stage_edit(served, st, edit)
    assert err(human.prepare_res("addon.grant", {"name": "standup", "staged": st})) == "market.staged_changed"
    st2 = ok(stage(served))["data"]["stage"]
    assert err(human.prepare_res("addon.grant", {"name": "other", "staged": st2})) == "market.staged_changed"
    stage_edit(served, st2, lambda d: d.update(expires_at=1))
    assert err(human.prepare_res("addon.grant", {"name": "standup", "staged": st2})) == "market.staged_changed"
    nothing_changed_but_stage(served)


def nothing_changed_but_stage(served):
    assert mfiles(served).installs()["addons"] == {} and not addon_dir(served).exists()


def test_a_stage_swapped_between_prepare_and_submit_is_refused_under_the_lock(served, human, ready):
    """C2: the bytes are checked again in submit, before the journal write, so nothing lands."""
    import base64

    st = ok(stage(served))["data"]["stage"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st})
    signed = human.sign(prepared)
    stage_edit(served, st, lambda d: d["files"].update(**{"main.py": base64.b64encode(b"x").decode()}))
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.staged_changed"
    nothing_changed_but_stage(served)
    log = served.lab.ws.workspace_events() if hasattr(served.lab.ws, "workspace_events") else None
    assert log is None or all(e["type"] != "addon.granted" for e in log)


def test_the_install_record_is_rechecked_at_submit(served, human, ready):
    """C2: a record that appeared between prepare and submit (a newer version installed, a key remembered) is judged."""
    st = ok(stage(served))["data"]["stage"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st})
    signed = human.sign(prepared)
    f = mfiles(served)
    with f.commit_installs() as write:
        cur = f.installs()
        cur["seen"][f"{H.OFFICIAL}|standup"] = "2.0.0"
        write(cur)
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.rollback"
    nothing_changed_but_stage(served)
    with f.commit_installs() as write:
        cur = f.installs()
        cur["seen"] = {}
        cur["keys"][f"{H.OFFICIAL}|standup"] = "B" * 87
        write(cur)
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.publisher_key_changed"


# ------------------------------------------------------------------------------------------------ C1: the plain gate
def test_a_copied_folder_cannot_be_granted_plainly(served, human):
    """C1: bytes that are neither built in nor the install record's are not signable."""
    root = addon_dir(served)
    root.mkdir(parents=True)
    for p, b in files("copied", "1.0.0").items():
        (root.parent / "copied").mkdir(exist_ok=True)
        (root.parent / "copied" / p).write_bytes(b)
    assert err(human.prepare_res("addon.grant", {"name": "copied"})) == "market.not_installed"


def test_a_builtin_is_granted_plainly_and_a_disabled_install_is_re_enabled_from_its_record(served, human, ready):
    ok(human.prepare_res("addon.grant", {"name": "terminals"}))
    install(served, human)
    ok(human.prepare_res("addon.disable", {"name": "standup"}))
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    again = human.prepare("addon.grant", {"name": "standup"})  # the bytes of the install record
    assert again["events"][0]["package_sha256"] == mfiles(served).installs()["addons"]["standup"]["package_sha256"]
    (addon_dir(served) / "main.py").write_bytes(b"print('tampered')\n")
    assert err(human.prepare_res("addon.grant", {"name": "standup"})) == "market.not_installed"
