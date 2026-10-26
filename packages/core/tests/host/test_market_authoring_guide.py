"""V3 G5 (#923): the Changelog example of ``docs/addons/authoring.md`` (the files under ``docs/addons/examples``) really
goes the whole way: commit it, ``pack`` it, list it in a local test marketplace and install it in a dev workspace
through the signed grant. The guide's steps are these steps; if this breaks, the guide is wrong."""

# ruff: noqa: F811
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orch import devsandbox
from orch.addons import package
from orch.market import address, author, fetch
from tests.host.devgate_helpers import SbxLab
from tests.host.dispatch_helpers import Served
from tests.host.human_helpers import Human
from tests.host.market_helpers import Net, addon_dir, install, mfiles, ok, person
from tests.market import helpers as H
from tests.market.test_author import commit_files, git

EXAMPLE = Path(__file__).resolve().parents[4] / "docs" / "addons" / "examples" / "changelog"
URL = "https://github.com/acme/orch-market-test"
SOURCE = "https://github.com/acme/orch-addon-changelog"


def test_the_changelog_example_installs_from_a_local_marketplace_in_a_dev_workspace(tmp_path, monkeypatch):
    # 1 the add-on repo: the example files, one commit
    repo = tmp_path / "orch-addon-changelog"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_files(repo, {p.name: p.read_bytes() for p in EXAMPLE.iterdir()}, "Changelog 0.3.1")
    # 2 orch addon pack: the digest and the entry to paste
    packed = author.pack(str(repo), git=SOURCE)
    assert packed.entry["id"] == "changelog" and packed.entry["capabilities"] == []
    # 3 the test marketplace lists that entry (the marketplace owner signs it); the packages come from the real export
    n = Net()
    n.url = URL
    entry = n.add("changelog", "0.3.1", (), commit=packed.commit, pkg=packed.files, min_orch=packed.entry["min_orch"])
    listed = {k: v for k, v in entry.items() if k != "publisher"}
    assert listed == {**packed.entry, "source": {"git": "https://github.com/acme/changelog"}}  # the pasted entry
    assert entry["package_sha256"] == packed.digest
    monkeypatch.setattr(fetch, "fetch_index", n.fetch_index)
    monkeypatch.setattr(fetch, "fetch_package", n.fetch_package)
    monkeypatch.setattr(address, "resolve", n.resolve)
    # 4 a dev workspace whose host knows that marketplace (dev.json): add it, install, sign
    lab = SbxLab(tmp_path)
    devsandbox.write_dev_json(lab.sroot, lab.sandbox_id, "https://relay.dev.example", {"url": URL, "keys": [H.KEY_B64]})
    try:
        served = Served(lab, dev=lab.dev())
        try:
            assert ok(person(served, "market.add", {"url": URL}))["data"]["class"] == "dev"
            st, _, _ = install(served, Human(served), "changelog")
            assert st["package_sha256"] == packed.digest and st["market"]["class"] == "dev"
            assert package.read_package(addon_dir(served, "changelog")).digest == packed.digest
            rec = mfiles(served).installs()["addons"]["changelog"]
            assert rec["origin"] == URL and rec["version"] == "0.3.1"
            assert ok(person(served, "market.installed"))["data"]["addons"][0]["state"] == "enabled"
            # 5 it answers the runner's request the way the guide says
            request = {
                "jsonrpc": "2.0", "id": 1, "method": "propose",
                "params": {"ticket": {"key": "WEB-42", "type": "feature", "status": "done", "fields": {}}},
            }  # fmt: skip
            done = subprocess.run(
                [sys.executable, str(addon_dir(served, "changelog") / "changelog.py")],
                input=json.dumps(request) + "\n", capture_output=True, text=True, check=True,
            )  # fmt: skip
            assert json.loads(done.stdout)["result"] == {"set": {"line": "Shipped WEB-42."}}
        finally:
            served.close()
    finally:
        lab.close()


def test_the_example_is_what_the_guide_says():
    guide = (EXAMPLE.parents[1] / "authoring.md").read_text()
    for p in EXAMPLE.iterdir():
        if p.suffix in (".json", ".py"):
            assert p.read_text().strip() in guide, f"{p.name} in the guide differs from docs/addons/examples/changelog"


@pytest.mark.parametrize("cmd", ["orch addon new", "orch addon pack", "orch addon dev", "orch addon install"])
def test_the_guide_names_every_author_command(cmd):
    assert cmd in (EXAMPLE.parents[1] / "authoring.md").read_text()
