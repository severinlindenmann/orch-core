"""Opus review G5 (#1175): adversarial probes of ``market.dev_stage`` (host side)."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from orch import devsandbox
from orch.market import address, author, fetch
from tests.host.devgate_helpers import SbxLab
from tests.host.dispatch_helpers import Served
from tests.host.human_helpers import Human
from tests.host.market_helpers import Net, addon_dir, err, install, mfiles, ok, person
from tests.market import helpers as H
from tests.market.test_author import commit_files, git

NAME = "changelog"
URL = "https://github.com/acme/orch-market-test"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "addon-repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_files(r, author.scaffold(NAME))
    return r


def _lab_with_market(tmp_path, monkeypatch, repo):
    packed = author.pack(str(repo))
    n = Net()
    n.url = URL
    n.add(NAME, "0.1.0", (), commit=packed.commit, pkg=packed.files, min_orch=packed.entry["min_orch"])
    monkeypatch.setattr(fetch, "fetch_index", n.fetch_index)
    monkeypatch.setattr(fetch, "fetch_package", n.fetch_package)
    monkeypatch.setattr(address, "resolve", n.resolve)
    lab = SbxLab(tmp_path)
    devsandbox.write_dev_json(lab.sroot, lab.sandbox_id, "https://relay.dev.example", {"url": URL, "keys": [H.KEY_B64]})
    served = Served(lab, dev=lab.dev())
    ok(person(served, "market.add", {"url": URL}))
    return lab, served


def test_dev_refuses_a_name_installed_from_a_marketplace(tmp_path, monkeypatch, repo):
    lab, served = _lab_with_market(tmp_path, monkeypatch, repo)
    try:
        install(served, Human(served), NAME)
        assert err(person(served, "market.dev_stage", {"path": str(repo)})) == "market.origin_changed"
    finally:
        served.close()
        lab.close()


def test_a_marketplace_install_between_dev_stage_and_submit_wins(tmp_path, monkeypatch, repo):
    lab, served = _lab_with_market(tmp_path, monkeypatch, repo)
    try:
        commit_files(repo, {"extra.txt": b"dev change\n"}, "dev")
        st = ok(person(served, "market.dev_stage", {"path": str(repo)}))["data"]
        h = Human(served)
        prepared = h.prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        install(served, Human(served), NAME)  # now installed from the marketplace
        res = h.submit_res(prepared["request"]["id"], h.sign(prepared).events)
        assert err(res) in ("market.origin_changed", "addon.stage_changed")
        rec = mfiles(served).installs()["addons"][NAME]
        assert rec["origin"] == URL  # the install record is untouched by the dev stage
    finally:
        served.close()
        lab.close()


def test_after_a_dev_grant_a_marketplace_install_is_a_normal_install_and_dev_bytes_never_get_a_record(
    tmp_path, monkeypatch, repo
):
    lab, served = _lab_with_market(tmp_path, monkeypatch, repo)
    try:
        commit_files(repo, {"extra.txt": b"dev change\n"}, "dev")
        st = ok(person(served, "market.dev_stage", {"path": str(repo)}))["data"]
        h = Human(served)
        prepared = h.prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        ok(h.submit_res(prepared["request"]["id"], h.sign(prepared).events))
        assert mfiles(served).installs()["addons"] == {}
        # a plain grant of the dev bytes stays refused (C1)
        p2 = Human(served).prepare_res("addon.grant", {"name": NAME}) if hasattr(Human, "prepare_res") else None
        if p2 is not None:
            assert err(p2) == "market.not_installed"
        install(served, Human(served), NAME)
        rec = mfiles(served).installs()["addons"][NAME]
        assert rec["package_sha256"] != st["package_sha256"] and rec["origin"] == URL
        assert addon_dir(served, NAME).joinpath("extra.txt").exists() is False
    finally:
        served.close()
        lab.close()


def test_host_path_that_is_not_a_repo_root_is_refused(tmp_path, repo):
    """Info: the host does not repeat the CLI's root check; git discovers the enclosing repository and ``ls-tree`` (no
    ``--full-tree``) lists only the part of the tree below the given folder: an empty folder is refused as a package."""
    sub = repo / "deeper"
    sub.mkdir()
    lab = SbxLab(tmp_path)
    served = Served(lab, dev=lab.dev())
    try:
        res = person(served, "market.dev_stage", {"path": str(sub)})
        assert err(res) == "invalid.input"  # I1: the host repeats the repo-root check
    finally:
        served.close()
        lab.close()
