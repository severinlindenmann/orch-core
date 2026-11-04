"""V3 G4 step 2 (design-g4 sections 3 and 4, C5, C6): step 15 at stage, the dependency block of the review, the signed
``runtime``, ``material["deps"]`` and the deps part of an update diff (``widened``)."""

# ruff: noqa: F811
from __future__ import annotations

import copy
import json

import pytest

from orch.host import signer
from tests.host import dep_helpers as D
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.dep_helpers import NODE, PY, dep_files, node_lock, py_lock
from tests.host.market_helpers import add_market, err, ok, person, stage

net = M.net
registry = D.registry
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def publish(served, net, version="1.0.0", lock=None, runtime=PY, **kw):
    net.add("standup", version, pkg=dep_files("standup", version, lock, runtime), **({"runtime": runtime} | kw))
    net.version += 1
    ok(person(served, "market.refresh"))


@pytest.fixture
def ready(served, net, registry):
    add_market(served, net)
    registry.IDNA = registry.py("idna", "3.10")
    registry.SIX = registry.py("six", "1.16.0")
    return served


def test_stage_lists_the_packages_and_the_signed_grant_names_runtime(served, human, net, ready, registry):
    publish(served, net, lock=py_lock(registry.IDNA, registry.SIX))
    st = ok(stage(served))["data"]
    d = st["deps"]
    assert (d["kind"], d["lock"], d["count"]) == ("python-requirements", "requirements.txt", 2)
    assert [(r["name"], r["version"], r["installs"], r["note"]) for r in d["rows"]] == [
        ("idna", "3.10", True, None),
        ("six", "1.16.0", True, None),
    ]
    assert (
        "dependencies Python from requirements.txt: 2 packages, installed after you sign; no package scripts run"
        in st["review"]
    )
    assert "dep idna 3.10" in st["review"] and "dep six 1.16.0" in st["review"]
    assert st["grant"]["runtime"] == {"kind": "python-requirements", "lock": "requirements.txt", "packages": 2}
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    assert prepared["events"][0]["runtime"] == st["grant"]["runtime"]
    assert prepared["material"]["deps"]["count"] == 2
    human.check(prepared)  # the signer accepts the material
    shown = signer.render(prepared, lambda w: ("Acme", "DEMO"))
    assert "dependencies (listed by this computer from the lockfile, covered by the digest" in shown
    assert "dep idna 3.10" in shown and "dep six 1.16.0" in shown and "runtime: " in shown


def test_a_package_without_runtime_has_no_deps_anywhere(served, human, net, ready, registry):
    net.add("standup", "1.0.0")
    net.version += 1
    ok(person(served, "market.refresh"))
    st = ok(stage(served))["data"]
    assert st["deps"] is None and "runtime" not in st["grant"]
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    assert "deps" not in prepared["material"]
    human.check(prepared)


@pytest.mark.parametrize(
    ("lock", "runtime", "code"),
    [
        (b"idna==3.10\n", PY, "market.lock_invalid"),  # no hash
        (b"--index-url https://evil.example\n", PY, "market.lock_invalid"),
        (b'{"lockfileVersion": 2, "packages": {}}', NODE, "market.lock_invalid"),
    ],
)
def test_step_15_refuses_a_bad_lock_before_anything_is_staged(served, net, ready, registry, lock, runtime, code):
    publish(served, net, lock=lock, runtime=runtime)
    assert err(stage(served)) == code
    assert M.mfiles(served).stages() == []


def test_the_index_copy_of_runtime_must_equal_the_manifest(served, net, ready, registry):
    net.add("standup", "1.0.0", pkg=dep_files("standup", "1.0.0"), runtime={**PY, "lock": "other.txt"})
    net.version += 1
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.manifest_mismatch"
    net.add("standup", "1.0.0", pkg=dep_files("standup", "1.0.0"))  # the manifest declares it, the index does not
    net.version += 1
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.manifest_mismatch"


def test_a_lock_that_this_computer_cannot_install_is_refused_at_stage(served, net, ready, registry):
    lock = node_lock(("fsevents", "2.3.3", b"x"), extra={"fsevents": {"os": ["win32"]}})
    publish(served, net, lock=lock, runtime=NODE)
    r = stage(served)
    assert err(r) == "market.env_failed" and r["error"]["detail"] == {"reason": "platform"}


def test_the_signer_refuses_material_deps_that_disagree_with_the_signed_runtime(served, human, net, ready, registry):
    publish(served, net, lock=py_lock(registry.IDNA, registry.SIX))
    st = ok(stage(served))["data"]
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )

    def tweak(fn):
        p = copy.deepcopy(prepared)
        fn(p["material"]["deps"])
        return p

    bad = [
        lambda d: d.update(count=1),
        lambda d: d.update(lock="other.txt"),
        lambda d: d.update(kind="node-package-lock"),
        lambda d: d.update(extra=1),
        lambda d: d.update(bytes=-1),
        lambda d: d["rows"].pop(),
        lambda d: d["rows"][0].update(name="idna\nx"),
        lambda d: d["rows"][0].update(version="3.10; rm -rf"),
        lambda d: d["rows"][0].update(note="weird"),
        lambda d: d["rows"][0].update(installs=False),  # not installed needs a note that says why
        lambda d: d["rows"][0].update(extra=1),
    ]
    for fn in bad:
        with pytest.raises(signer.SignerRefused):
            human.check(tweak(fn))
    missing = copy.deepcopy(prepared)
    del missing["material"]["deps"]
    with pytest.raises(signer.SignerRefused):
        human.check(missing)
    stray = copy.deepcopy(prepared)
    stray["events"][0].pop("runtime")
    with pytest.raises(signer.SignerRefused):
        human.check(stray)  # deps without a signed runtime


# ---- the update diff (C6)


def install_v1(served, human, net, lock):
    publish(served, net, lock=lock)
    return M.install(served, human)[0]


def update(served, net, version, lock, runtime=PY, *, with_runtime=True, registry=None):
    if with_runtime:
        publish(served, net, version, lock, runtime)
    else:
        pkg = dep_files("standup", version, py_lock(registry.IDNA))
        m = json.loads(pkg["orch-addon.json"])
        del m["runtime"], pkg["requirements.txt"]
        pkg["orch-addon.json"] = json.dumps(m, sort_keys=True).encode()
        net.add("standup", version, pkg=pkg)
        net.version += 1
        ok(person(served, "market.refresh"))
    return ok(stage(served, update=True))["data"]


def test_an_update_that_adds_or_reversions_a_package_is_widened_and_shows_the_lines(
    served, human, net, ready, registry
):
    install_v1(served, human, net, py_lock(registry.IDNA))
    u = update(served, net, "1.1.0", py_lock(registry.py("idna", "3.11"), registry.SIX))["update"]
    assert u["widened"] is True
    assert {k: v for k, v in u["deps"].items() if k != "lines"} == {
        "runtime": None,
        "added": [{"name": "six", "version": "1.16.0"}],
        "removed": [],
        "changed": [{"name": "idna", "from": "3.10", "to": "3.11"}],
    }
    assert "+ dep six 1.16.0 (new)" in u["changes"] and "~ dep idna 3.10 -> 3.11" in u["changes"]


def test_an_update_that_only_removes_a_package_is_not_widened(served, human, net, ready, registry):
    install_v1(served, human, net, py_lock(registry.IDNA, registry.SIX))
    u = update(served, net, "1.0.1", py_lock(registry.IDNA))["update"]
    assert u["widened"] is False and u["deps"]["removed"] == [{"name": "six", "version": "1.16.0"}]
    assert "- dep six (removed)" in u["changes"]


def test_an_update_with_the_same_lock_is_not_widened_by_dependencies(served, human, net, ready, registry):
    install_v1(served, human, net, py_lock(registry.IDNA))
    u = update(served, net, "1.0.1", py_lock(registry.IDNA))["update"]
    assert u["widened"] is False and {k: v for k, v in u["deps"].items() if k != "lines"} == {
        "runtime": None,
        "added": [],
        "removed": [],
        "changed": [],
    }


def test_a_changed_hash_at_the_same_version_counts_as_changed(served, human, net, ready, registry):
    install_v1(served, human, net, py_lock(registry.IDNA))
    u = update(served, net, "1.0.1", py_lock(registry.py("idna", "3.10", {"idna/__init__.py": "x = 2\n"})))["update"]
    assert u["widened"] is True and u["deps"]["changed"] == [{"name": "idna", "from": "3.10", "to": "3.10"}]
    assert "~ dep idna 3.10 -> 3.10 (contents changed)" in u["changes"]


def test_runtime_added_is_widened_and_removed_is_not(served, human, net, ready, registry):
    net.add("standup", "1.0.0")  # no dependencies at first
    net.version += 1
    ok(person(served, "market.refresh"))
    M.install(served, human)
    u = update(served, net, "1.1.0", py_lock(registry.IDNA))["update"]
    assert (
        u["widened"] is True
        and u["deps"]["runtime"] == "added"
        and u["deps"]["added"] == [{"name": "idna", "version": "3.10"}]
    )
    assert "+ dependencies (Python, new)" in u["changes"]


def test_runtime_removed_and_a_changed_kind(served, human, net, ready, registry):
    install_v1(served, human, net, py_lock(registry.IDNA))
    u = update(served, net, "1.1.0", None, with_runtime=False, registry=registry)["update"]
    assert u["deps"]["runtime"] == "removed" and u["widened"] is False  # a removal alone does not widen
    assert "- dependencies (Python, removed)" in u["changes"]


# ---- the click view (one click in the browser: the signer's view is all the person sees)


def click_view(served, human, st):
    from orch.click import view as views

    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    return views.build(
        prepared, lambda w: ("Acme", "DEMO") if w == served.wid else None, device="d_" + "1" * 32,
        expires_at="2026-10-24T12:00:00Z", artifacts=None,
    )  # fmt: skip


def test_the_click_view_shows_the_runtime_the_packages_and_the_compiled_notes(served, human, net, ready, registry):
    wheel = registry.py(
        "six", "1.16.0", tag="cp314-cp314-" + __import__("packaging.tags", fromlist=["x"]).platform_tags().__next__()
    )
    publish(served, net, lock=py_lock(registry.IDNA, wheel))
    st = ok(stage(served))["data"]
    v = click_view(served, human, st)
    assert v["confirm"] == {"kind": "click"}
    full = "\n".join(v["lines"])
    assert "runtime: " in full and "dependencies (listed by this computer from the lockfile" in full
    assert "dep idna 3.10" in full and "dep six 1.16.0 (compiled code)" in full
    c = v["content"]
    assert v["headline"] == "Turn on an add-on" and v["risk_lines"]  # contract 4: signer words; a risk, two presses
    assert c["headline"] == "Grant the add-on standup"  # never the short "Turn on" view for an add-on with runtime
    assert any("Also installs 2 Python packages listed in requirements.txt after you sign" in n for n in c["notes"])
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert "idna 3.10" in rows["packages (listed by this computer from the lockfile; covered by the digest)"]
    assert (
        "six 1.16.0 (compiled code)"
        in rows["packages (listed by this computer from the lockfile; covered by the digest)"]
    )


def test_the_click_view_shows_the_update_diff_and_widened(served, human, net, ready, registry):
    publish(served, net, lock=py_lock(registry.IDNA))
    M.install(served, human)
    publish(served, net, "1.1.0", py_lock(registry.IDNA, registry.SIX))
    st = ok(stage(served, update=True))["data"]
    v = click_view(served, human, st)
    full = "\n".join(v["lines"])
    assert "update of the dependencies (host-derived, not signed): WIDENED" in full
    assert "dep change + dep six 1.16.0 (new)" in full
    rows = {r["field"]: r["after"] for r in v["content"]["fields_changed"]}
    assert rows["dependency changes (host-derived, not signed)"].startswith("WIDENED: + dep six 1.16.0 (new)")


def test_the_signer_checks_the_diff_block(served, human, net, ready, registry):
    publish(served, net, lock=py_lock(registry.IDNA))
    M.install(served, human)
    publish(served, net, "1.1.0", py_lock(registry.IDNA, registry.SIX))
    st = ok(stage(served, update=True))["data"]
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    human.check(prepared)
    for bad in ({"lines": ["x\ny"], "widened": True}, {"lines": ["x"], "widened": 1}, {"lines": ["x"]}, "x"):
        p = copy.deepcopy(prepared)
        p["material"]["deps"]["diff"] = bad
        with pytest.raises(signer.SignerRefused):
            human.check(p)


def test_a_big_update_keeps_every_line_and_puts_contents_changed_first(served, human, net, ready, registry):
    """201 added packages and one hash change at the same version: nothing is cut, the change comes first."""
    publish(served, net, lock=py_lock(registry.IDNA))
    M.install(served, human)
    many = [registry.py(f"pkg{i}", "1.0") for i in range(201)]
    changed = registry.py("idna", "3.10", {"idna/__init__.py": "x = 2\n"})
    publish(served, net, "1.1.0", py_lock(changed, *many))
    st = ok(stage(served, update=True))["data"]
    lines = st["deps"]["diff"]["lines"]
    assert len(lines) == 202 and lines[0] == "~ dep idna 3.10 -> 3.10 (contents changed)"
    v = click_view(served, human, st)
    assert any("dep change ~ dep idna 3.10 -> 3.10 (contents changed)" in x for x in v["lines"])
    assert any(
        "contents changed" in r["after"] for r in v["content"]["fields_changed"] if "dependency changes" in r["field"]
    )


def test_more_than_a_thousand_diff_lines_fall_back_to_the_full_review(served, human, net, ready, registry):
    from orch.host import signcontent

    publish(served, net, lock=py_lock(registry.IDNA))
    st = ok(stage(served))["data"]
    prepared = human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )
    prepared["material"]["deps"]["diff"] = {"lines": ["+ dep x 1 (new)"] * 1001, "widened": True}
    signer._deps(prepared["material"], prepared["events"])  # the signer accepts up to 2 x 1000 + 1 lines
    with pytest.raises(signcontent.PlainUnavailable):
        signcontent.build(prepared, verified=True)
    prepared["material"]["deps"]["diff"] = {"lines": ["+ dep x 1 (new)"] * 2002, "widened": True}
    with pytest.raises(signer.SignerRefused):
        signer._deps(prepared["material"], prepared["events"])
    prepared["material"]["deps"]["diff"] = {"lines": ["+ dep x\u202e1"], "widened": True}
    with pytest.raises(signer.SignerRefused):
        signer._deps(prepared["material"], prepared["events"])
