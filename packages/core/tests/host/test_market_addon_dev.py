"""V3 G5 (#923): ``orch addon dev`` on the host (``market.dev_stage``). It works only in a dev sandbox workspace on a
dev host, never for an agent, installs the bytes of a COMMIT through the ordinary signed ``addon.grant {staged}`` and
leaves no install record. A real workspace, a normal host and an agent are refused before any path is read."""

# ruff: noqa: F811
from __future__ import annotations

import os
import subprocess

import pytest

from orch import gitsafe
from orch.addons import package
from orch.host import devmode
from orch.market import author
from tests.host.devgate_helpers import SbxLab
from tests.host.dispatch_helpers import Served
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import Lab
from tests.host.market_helpers import addon_dir, agent, err, mfiles, ok, person
from tests.market.test_author import commit_files, git

NAME = "changelog"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "addon-repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_files(r, author.scaffold(NAME))
    return r


@pytest.fixture
def reads(monkeypatch):
    """Every local repository read (an agent or a real workspace must cause none)."""
    calls: list[tuple] = []
    real = gitsafe.market_local_export

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(gitsafe, "market_local_export", spy)
    return calls


def dev_stage(served, repo, **more):
    return person(served, "market.dev_stage", {"path": str(repo), **more})


def sbx(tmp_path):
    lab = SbxLab(tmp_path)
    return lab, Served(lab, dev=lab.dev())


def test_in_a_dev_workspace_the_signed_grant_installs_the_commit_and_records_no_install(tmp_path, repo, reads):
    lab, served = sbx(tmp_path)
    try:
        st = ok(dev_stage(served, repo))["data"]
        packed = author.pack(str(repo))
        assert st["dev"] is True and st["name"] == NAME and st["package_sha256"] == packed.digest  # same as pack
        assert st["commit"] == packed.commit and not addon_dir(served, NAME).exists()  # staged is not installed
        h = Human(served)
        prepared = h.prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        assert prepared["material"]["market"]["class"] == "dev-folder"  # labelled, per host, not signed
        assert prepared["events"][0]["package_sha256"] == st["package_sha256"]
        ok(h.submit_res(prepared["request"]["id"], h.sign(prepared).events))
        assert package.read_package(addon_dir(served, NAME)).digest == packed.digest
        f = mfiles(served)
        assert f.installs() == {"addons": {}, "seen": {}, "keys": {}} and f.stages() == []
        assert ok(person(served, "market.installed"))["data"]["addons"] == []  # never an install
        assert err(person(served, "market.uninstall", {"name": NAME})) == "market.not_installed"
    finally:
        served.close()
        lab.close()


def test_the_commit_is_what_runs_not_the_working_tree(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        (repo / "main.py").write_bytes(b"print('uncommitted')\n")
        st = ok(dev_stage(served, repo))["data"]
        assert st["package_sha256"] == package.package_digest(author.scaffold(NAME))
    finally:
        served.close()
        lab.close()


def test_a_plain_grant_of_the_dev_folder_later_is_refused(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        st = ok(dev_stage(served, repo))["data"]
        h = Human(served)
        prepared = h.prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        ok(h.submit_res(prepared["request"]["id"], h.sign(prepared).events))
        assert err(h.prepare_res("addon.grant", {"name": NAME})) == "market.not_installed"
    finally:
        served.close()
        lab.close()


def test_a_real_workspace_refuses_dev_and_reads_nothing(tmp_path, repo, reads):
    real = Lab(tmp_path / "real")
    try:
        served = Served(real)
        try:
            assert served.service.dev is None
            res = dev_stage(served, repo)
            assert err(res) == "host.dev_refused" and res["error"]["detail"]["reason"] == "not_sandbox"
            assert reads == [] and mfiles(served).stages() == [] and not addon_dir(served, NAME).exists()
        finally:
            served.close()
    finally:
        real.close()


def test_a_real_workspace_refuses_it_even_when_the_host_object_is_a_dev_host(tmp_path, repo, reads):
    lab = SbxLab(tmp_path)
    real = Lab(tmp_path / "real")
    try:
        served = Served(real)
        try:
            served.service.dev = lab.dev()  # the worst case: a dev host object on a real workspace (S1 to S6 fail)
            assert devmode.dev_gate(served.service) is None
            assert err(dev_stage(served, repo)) == "host.dev_refused"
            assert reads == [] and not addon_dir(served, NAME).exists()
        finally:
            served.close()
    finally:
        real.close()
        lab.close()


def test_orch_no_convenience_turns_dev_off(tmp_path, repo, reads, monkeypatch):
    lab, served = sbx(tmp_path)
    try:
        monkeypatch.setenv("ORCH_NO_CONVENIENCE", "1")
        assert err(dev_stage(served, repo)) == "host.dev_refused" and reads == []
    finally:
        served.close()
        lab.close()


def test_an_agent_is_refused_before_any_path_is_read(tmp_path, repo, reads):
    lab, served = sbx(tmp_path)
    try:
        assert err(agent(served, "market.dev_stage", {"path": str(repo)})) == "market.actor_refused"
        assert reads == [] and mfiles(served).stages() == []
    finally:
        served.close()
        lab.close()


def test_the_dev_gate_is_asked_again_at_submit(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        st = ok(dev_stage(served, repo))["data"]
        h = Human(served)
        prepared = h.prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        served.service.dev = None  # the host stopped being a dev host between stage and submit
        res = h.submit_res(prepared["request"]["id"], h.sign(prepared).events)
        assert err(res) == "host.dev_refused" and not addon_dir(served, NAME).exists()
    finally:
        served.close()
        lab.close()


@pytest.mark.parametrize("name", ["wiki", "github", "dashboard"])
def test_a_dev_folder_cannot_take_a_built_in_or_official_name(tmp_path, name):
    lab, served = sbx(tmp_path)
    try:
        r = tmp_path / "r"
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        commit_files(r, author.scaffold(name))
        assert err(dev_stage(served, r)) == "market.reserved_id"
    finally:
        served.close()
        lab.close()


def test_a_repository_whose_config_could_run_a_program_is_refused(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        marker = tmp_path / "ran"
        git(repo, "config", "core.fsmonitor", f"touch {marker}")
        assert err(dev_stage(served, repo)) == "invalid.input" and not marker.exists()
    finally:
        served.close()
        lab.close()


def test_a_package_the_install_would_refuse_is_refused(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        os.symlink("/etc/passwd", repo / "link")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "link")
        assert err(dev_stage(served, repo)) == "market.package_invalid"
    finally:
        served.close()
        lab.close()


@pytest.mark.parametrize("path", ["relative/dir", "", "/x\0y"])
def test_the_path_must_be_absolute(tmp_path, path):
    lab, served = sbx(tmp_path)
    try:
        assert err(person(served, "market.dev_stage", {"path": path})) == "invalid.input"
    finally:
        served.close()
        lab.close()


def test_pack_and_dev_agree_on_the_digest_of_a_commit_with_crlf_and_filters(tmp_path, repo):
    lab, served = sbx(tmp_path)
    try:
        git(repo, "config", "core.autocrlf", "true")
        (repo / ".git" / "info" / "attributes").write_text("*.md text eol=crlf export-ignore\n")
        commit_files(repo, {"notes.md": b"a\nb\n"}, "notes")
        st = ok(dev_stage(served, repo))["data"]
        assert st["package_sha256"] == author.pack(str(repo)).digest
        assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True).stdout == b""
    finally:
        served.close()
        lab.close()


def test_the_dev_review_carries_a_per_host_not_signed_dev_folder_block(tmp_path, repo):
    from orch.host import signer

    lab, served = sbx(tmp_path)
    try:
        st = ok(dev_stage(served, repo))["data"]
        prepared = Human(served).prepare(
            "addon.grant",
            {"name": NAME, "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
        )
        block = prepared["material"]["market"]
        assert block["class"] == "dev-folder" and block["commit"] == st["commit"] and block["url"] == "local folder"
        text = signer.render(prepared, lambda w: ("Acme", "DEMO"))
        assert "DEV add-on from a local folder, not from a marketplace (per host, not signed)" in text
        assert st["commit"] in text and "marketplace commit" not in text
        # the strict shape: a dev-folder block with the marketplace note is refused
        bad = {**block, "source_note": "source as claimed by the marketplace"}
        with pytest.raises(signer.SignerRefused):
            signer._market({"market": bad}, prepared["events"])
        signer._market({"market": block}, prepared["events"])
    finally:
        served.close()
        lab.close()
