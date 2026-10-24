"""V3 G5 (#923): the author tools' core. ``scaffold`` passes the validator; ``pack`` reads a commit (never the working
tree) of a local repository through the closed gitsafe operation and prints the digest an install computes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.addons import package
from orch.market import author, fetch
from tests.market.test_export import GIT_ENV, git, package_commit

NAME = "changelog"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    return r


def commit_files(repo: Path, files: dict[str, bytes], msg: str = "c") -> str:
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def test_scaffold_passes_the_validator_and_names_the_add_on():
    files = author.scaffold(NAME)
    pkg, manifest = author.check_files(files)
    assert manifest.name == NAME and manifest.version == "0.1.0" and manifest.capabilities == []
    assert pkg.digest == package.package_digest(files)
    assert json.loads(files["orch-addon.json"])["entry"]["cmd"] == ["python3", "{pkg}/main.py"]


@pytest.mark.parametrize("bad", ["", "Changelog", "1abc", "a_b", "a" * 41, "../x", "a b"])
def test_scaffold_refuses_a_bad_name(bad):
    with pytest.raises(author.AuthorError) as e:
        author.scaffold(bad)
    assert e.value.code == "invalid.input"


def test_pack_prints_the_digest_an_install_of_that_commit_computes(repo):
    files = author.scaffold(NAME)
    c = commit_files(repo, files)
    packed = author.pack(str(repo), git="https://github.com/acme/orch-addon-changelog")
    assert packed.commit == c and packed.digest == package.package_digest(files)
    assert packed.entry == {
        "id": NAME, "version": "0.1.0", "source": {"git": "https://github.com/acme/orch-addon-changelog"},
        "commit": c, "package_sha256": packed.digest, "capabilities": [], "min_orch": author.CORE_VERSION,
    }  # fmt: skip
    # the install side: the same export of that commit, the entry's digest check passes
    from orch.market import install as mi

    facts = mi.Facts(dict(packed.entry), "unsigned", None, None)
    exported = package.from_files(gitsafe.market_export_package(str(repo), c, None))
    assert mi.check_package(facts, exported, author.CORE_VERSION).name == NAME and exported.digest == packed.digest
    assert not packed.placeholder_git


def test_pack_uses_the_commit_not_the_working_tree(repo):
    files = author.scaffold(NAME)
    commit_files(repo, files)
    (repo / "main.py").write_bytes(b"print('edited after the commit')\n")
    (repo / "untracked.py").write_bytes(b"x = 1\n")
    packed = author.pack(str(repo))
    assert packed.digest == package.package_digest(files) and "untracked.py" not in packed.files


def test_pack_ignores_gitattributes_filters_and_eol_conversion(repo):
    files = author.scaffold(NAME)
    files["notes.txt"] = b"a\nb\n"
    git(repo, "config", "core.autocrlf", "true")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt text eol=crlf export-ignore\n")
    commit_files(repo, files)
    assert author.pack(str(repo)).digest == package.package_digest(files)


def test_pack_takes_a_revision_and_a_subdirectory(repo):
    old = commit_files(repo, {"pkg/" + k: v for k, v in author.scaffold(NAME).items()}, "one")
    (repo / "pkg" / "main.py").write_bytes(b"print(2)\n")
    git(repo, "commit", "-qam", "two")
    first = author.pack(str(repo), old, subdir="pkg")
    second = author.pack(str(repo), "main", subdir="pkg")
    assert first.commit == old != second.commit and first.digest != second.digest
    assert second.entry["source"]["subdir"] == "pkg"


def test_pack_without_git_flags_the_placeholder(repo):
    commit_files(repo, author.scaffold(NAME))
    p = author.pack(str(repo))
    assert p.placeholder_git and p.entry["source"]["git"] == author.PLACEHOLDER_GIT


@pytest.mark.parametrize("rev", ["--output=/x", "a..b", "x@{1}", "HEAD~1", "-x", "a b", "refs/../x", "x.lock"])
def test_pack_refuses_an_odd_revision(repo, rev):
    commit_files(repo, author.scaffold(NAME))
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo), rev)
    assert e.value.code == "invalid.input"


def test_pack_refuses_a_repo_whose_config_could_run_a_program(repo):
    commit_files(repo, author.scaffold(NAME))
    marker = repo.parent / "ran"
    git(repo, "config", "core.fsmonitor", f"touch {marker}")
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo))
    assert e.value.code == "invalid.input" and "allow-list" in e.value.message and not marker.exists()


def test_pack_names_the_broken_rule(repo):
    files = author.scaffold(NAME)
    files["lib.so"] = b"\x7fELF"
    commit_files(repo, files)
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo))
    assert e.value.code == "market.package_invalid"


def test_pack_refuses_a_link_and_a_missing_manifest(repo):
    (repo / "orch-addon.json").write_bytes(author.scaffold(NAME)["orch-addon.json"])
    os.symlink("/etc/passwd", repo / "link")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "l")
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo))
    assert e.value.code == "market.package_invalid"


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.update(schema="orch.addon/2"),
        lambda d: d.update(capabilities=["root"]),
        lambda d: d.update(min_core="99.0.0"),
        lambda d: d.update(runtime={"kind": "python-requirements", "lock": "requirements.txt"}),
        lambda d: d.pop("min_core"),
    ],
)
def test_pack_refuses_a_manifest_an_install_would_refuse(repo, edit):
    files = author.scaffold(NAME)
    doc = json.loads(files["orch-addon.json"])
    edit(doc)
    files["orch-addon.json"] = json.dumps(doc).encode()
    commit_files(repo, files)
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo))
    assert e.value.code in ("market.manifest_mismatch",)


def test_pack_writes_nothing_into_the_repository(repo):
    commit_files(repo, author.scaffold(NAME))
    before = sorted(
        str(p.relative_to(repo)) + str(p.stat().st_mtime_ns) for p in repo.rglob("*") if ".git/objects" not in str(p)
    )
    author.pack(str(repo))
    after = sorted(
        str(p.relative_to(repo)) + str(p.stat().st_mtime_ns) for p in repo.rglob("*") if ".git/objects" not in str(p)
    )
    assert before == after


def test_the_same_digest_through_the_install_fetch_path(repo, monkeypatch):
    """fetch_package's export of a staging repository and pack's export of the author's repo agree byte for byte."""
    files = author.scaffold(NAME)
    c = commit_files(repo, files)
    packed = author.pack(str(repo))
    monkeypatch.setattr(fetch.address, "resolve", lambda host, timeout: ["93.184.216.34"])
    monkeypatch.setattr(fetch.gitsafe, "market_fetch", lambda *a, **k: c)
    real = gitsafe.market_export_package
    monkeypatch.setattr(
        fetch.gitsafe,
        "market_export_package",
        lambda staging, commit, subdir: real(str(repo), commit, subdir),
    )
    pkg = fetch.fetch_package({"source": {"git": "https://github.com/acme/x"}, "commit": c})
    assert pkg.digest == packed.digest
    assert (
        subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, env=GIT_ENV).stdout
        == b""
    )
    _ = package_commit
