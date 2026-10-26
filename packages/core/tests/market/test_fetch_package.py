"""V3 G3 (#921): ``fetch.fetch_package`` against a local repository through the production argv: the pinned commit,
the package in memory, the staging directory gone, the refusal codes."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orch import gitsafe
from orch.addons import package
from orch.market import MarketError, fetch
from tests.market.test_core import local, make_repo  # noqa: F401  (the fixture swaps the transport only)

M = b'{"schema": "orch.addon/3"}'


def entry(commit: str, subdir: str | None = None) -> dict:
    src = {"git": "https://github.com/acme/standup"} | ({"subdir": subdir} if subdir else {})
    return {"source": src, "commit": commit}


def code_of(fn, *a):
    with pytest.raises(MarketError) as e:
        fn(*a)
    return e.value.code


def test_the_package_at_the_pinned_commit_comes_back_in_memory(local, tmp_path):  # noqa: F811
    repo, head = make_repo(tmp_path, {"orch-addon.json": M, "main.py": b"print(1)\n"})
    local["repo"] = str(repo)
    pkg = fetch.fetch_package(entry(head))
    assert pkg.files == {"orch-addon.json": M, "main.py": b"print(1)\n"}
    assert pkg.digest == package.package_digest(pkg.files)


def test_a_moved_branch_does_not_matter_only_the_commit_does(local, tmp_path):  # noqa: F811
    repo, first = make_repo(tmp_path, {"orch-addon.json": M, "main.py": b"1"})
    (repo / "main.py").write_bytes(b"evil")
    from tests.market.test_core import _git

    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "evil", cwd=repo)
    local["repo"] = str(repo)
    assert fetch.fetch_package(entry(first)).files["main.py"] == b"1"


def test_a_subdirectory(local, tmp_path):  # noqa: F811
    repo, _ = make_repo(tmp_path, {"README": b"r"})
    (repo / "pkg").mkdir()
    (repo / "pkg" / "orch-addon.json").write_bytes(M)
    from tests.market.test_core import _git

    _git("add", "-A", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "p", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    local["repo"] = str(repo)
    assert fetch.fetch_package(entry(head, "pkg")).files == {"orch-addon.json": M}
    assert code_of(fetch.fetch_package, entry(head, "missing")) == "market.fetch_failed"


def test_codes(local, tmp_path):  # noqa: F811
    repo, head = make_repo(tmp_path, {"orch-addon.json": M}, {"link": "/etc/passwd"})
    local["repo"] = str(repo)
    assert code_of(fetch.fetch_package, entry(head)) == "market.package_invalid"  # a link in the tree
    assert code_of(fetch.fetch_package, entry("c" * 40)) == "market.fetch_failed"  # the commit does not exist
    other = tmp_path / "o"
    other.mkdir()
    repo2, _ = make_repo(other, {"main.py": b"1"})
    local["repo"] = str(repo2)
    from tests.market.test_core import _git

    head2 = _git("rev-parse", "HEAD", cwd=repo2)
    assert code_of(fetch.fetch_package, entry(head2)) == "market.package_invalid"  # no manifest


def test_the_commit_mismatch_is_its_own_code(local, tmp_path, monkeypatch):  # noqa: F811
    repo, head = make_repo(tmp_path, {"orch-addon.json": M})
    local["repo"] = str(repo)
    real = gitsafe._market_run

    def lie(argv, *a, **k):
        out = real(argv, *a, **k)
        return b"d" * 40 + b"\n" if "rev-parse" in argv else out

    monkeypatch.setattr(gitsafe, "_market_run", lie)
    assert code_of(fetch.fetch_package, entry(head)) == "market.commit_mismatch"


def test_the_git_directory_is_deleted_on_every_path(local, tmp_path, monkeypatch):  # noqa: F811
    made: list[str] = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    repo, head = make_repo(tmp_path, {"orch-addon.json": M})
    local["repo"] = str(repo)
    fetch.fetch_package(entry(head))
    code_of(fetch.fetch_package, entry("c" * 40))
    assert made and not any(Path(d).exists() for d in made)


def test_the_package_pack_cap_is_its_own(local, tmp_path, monkeypatch):  # noqa: F811
    import os

    repo, head = make_repo(tmp_path, {"orch-addon.json": M, "big.bin": os.urandom(900_000)})
    local["repo"] = str(repo)
    monkeypatch.setattr(gitsafe, "MARKET_MAX_PACK", 200_000)  # the index's cap would refuse this pack...
    assert "big.bin" in fetch.fetch_package(entry(head)).files  # ...the package cap does not
    monkeypatch.setattr(gitsafe, "MARKET_PACKAGE_PACK", 200_000)
    assert code_of(fetch.fetch_package, entry(head)) == "market.fetch_failed"
