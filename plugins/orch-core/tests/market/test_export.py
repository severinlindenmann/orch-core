"""V3 G3 (#921, C6): ``gitsafe.market_export_package`` on a local repository (no network): a hostile tree is judged
whole before any blob is read, the package comes out in memory, and nothing is written."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.addons import package

GIT_ENV = {
    "PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}  # fmt: skip
MANIFEST = b'{"schema": "orch.addon/3"}'


def git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, env=GIT_ENV, check=True)
    return done.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "--template=")
    return r


def blob(repo: Path, data: bytes) -> str:
    return git(repo, "hash-object", "-w", "--stdin", stdin=data)


def tree(repo: Path, rows: list[tuple[str, str, str, str]]) -> str:
    """rows = (mode, type, oid, name); ``mktree`` takes them as given, so a hostile name can be built."""
    text = "".join(f"{m} {t} {o}\t{n}\0" for m, t, o, n in rows)
    return git(repo, "mktree", "-z", "--missing", stdin=text.encode())


def commit(repo: Path, root_tree: str) -> str:
    return git(repo, "commit-tree", root_tree, "-m", "x")


def package_commit(repo: Path, files: dict[str, bytes]) -> str:
    """A commit whose tree holds ``files`` (nested paths become trees)."""

    def build(prefix: str) -> str:
        rows, seen = [], set()
        for path in sorted(files):
            if not path.startswith(prefix):
                continue
            head, _, rest = path[len(prefix) :].partition("/")
            if rest:
                if head not in seen:
                    seen.add(head)
                    rows.append(("040000", "tree", build(prefix + head + "/"), head))
            else:
                rows.append(("100644", "blob", blob(repo, files[path]), head))
        return tree(repo, rows)

    return commit(repo, build(""))


def test_exports_the_files_in_memory_and_writes_nothing(repo):
    files = {"orch-addon.json": MANIFEST, "main.py": b"print(1)\n", "lib/util.py": b"x = 1\n"}
    c = package_commit(repo, files)
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))
    got = gitsafe.market_export_package(str(repo), c, None)
    assert got == files
    assert package.from_files(got).digest == package.package_digest(files)
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before  # no object, no checkout, nothing written


def test_a_subdirectory_is_the_package_root(repo):
    inner = {"orch-addon.json": MANIFEST, "a.py": b"1"}
    sub = tree(repo, [(("100644", "blob", blob(repo, v), k)) for k, v in sorted(inner.items())])
    root = tree(repo, [("040000", "tree", sub, "pkg"), ("100644", "blob", blob(repo, b"readme"), "README.md")])
    c = commit(repo, root)
    assert gitsafe.market_export_package(str(repo), c, "pkg") == inner
    with pytest.raises(gitsafe.MarketGitError):  # no such directory
        gitsafe.market_export_package(str(repo), c, "nope")


def test_gitattributes_filters_are_not_applied(repo):
    attrs = b"*.txt filter=evil text eol=crlf export-subst\n* export-ignore\n"
    files = {"orch-addon.json": MANIFEST, ".gitattributes": b"", "a.txt": b"line\n$Format:%H$\n"}
    # a leading-dot name is refused by 8.1 anyway; the point here is the bytes of an ordinary file
    del files[".gitattributes"]
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_bytes(attrs)
    git(repo, "config", "filter.evil.smudge", "false")
    c = package_commit(repo, files)
    assert gitsafe.market_export_package(str(repo), c, None)["a.txt"] == b"line\n$Format:%H$\n"


@pytest.mark.parametrize(
    "rows",
    [
        [("120000", "blob", "LINK", "link"), ("100644", "blob", "M", "orch-addon.json")],  # a symbolic link
        [("160000", "commit", "a" * 40, "sub"), ("100644", "blob", "M", "orch-addon.json")],  # a submodule
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", ".git")],
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", ".hidden")],
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", "..")],
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", "café.py")],
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", "x.so")],
        [("100644", "blob", "M", "orch-addon.json"), ("100644", "blob", "M", "A"), ("100644", "blob", "M", "a")],
        [("100644", "blob", "M", "main.py")],  # no manifest
    ],
)
def test_a_hostile_listing_is_package_invalid_before_any_blob(repo, rows):
    m, link = blob(repo, MANIFEST), blob(repo, b"/etc/passwd")
    rows = [(a, b, {"M": m, "LINK": link}.get(c, c), d) for a, b, c, d in rows]
    c = commit(repo, tree(repo, rows))
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), c, None)
    assert e.value.kind == "package"


def test_a_name_that_is_both_file_and_directory_and_case_twins_in_subdirs(repo):
    m = blob(repo, MANIFEST)
    sub = tree(repo, [("100644", "blob", m, "x")])
    both = tree(repo, [("100644", "blob", m, "orch-addon.json"), ("040000", "tree", sub, "d"), ("100644", "blob", m, "d")])
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), commit(repo, both), None)
    assert e.value.kind == "package"
    twins = tree(repo, [("100644", "blob", m, "orch-addon.json"), ("040000", "tree", sub, "Lib"), ("040000", "tree", sub, "lib")])
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), commit(repo, twins), None)
    assert e.value.kind == "package"


def test_an_oversized_blob_and_too_many_entries_are_judged_from_the_listing(repo, monkeypatch):
    calls = []
    real = gitsafe._market_run

    def spy(argv, *a, **k):
        calls.append(argv[argv.index("-C") + 2 :][-3:] if "cat-file" not in argv else ["cat-file"])
        return real(argv, *a, **k)

    monkeypatch.setattr(gitsafe, "_market_run", spy)
    monkeypatch.setattr(package, "MAX_FILE_BYTES", 1000)  # the same rule, a small number: the test stays fast
    big = package_commit(repo, {"orch-addon.json": MANIFEST, "big.bin": b"x" * 1001})
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), big, None)
    assert e.value.kind == "package" and "cat-file" not in [c[0] for c in calls]  # no blob was read
    many = package_commit(repo, {"orch-addon.json": MANIFEST, **{f"f{i}.py": b"1" for i in range(package.MAX_FILES)}})
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), many, None)
    assert e.value.kind == "package"


def test_a_very_large_tree_is_cut_off_by_the_listing_cap(repo):
    m = blob(repo, b"1")
    rows = [("100644", "blob", m, f"f{i:06d}.py") for i in range(20000)]
    c = commit(repo, tree(repo, rows))
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe.market_export_package(str(repo), c, None)
    assert e.value.kind == "package"


def test_the_operands_are_closed(repo):
    for bad in ("main", "a" * 39, "A" * 40):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.market_export_package(str(repo), bad, None)
    c = "a" * 40
    for sub in ("..", "a/../b", ".git", "a b", "x" * 65, "/abs", "a/b/c/d/e/f/g"):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.market_export_package(str(repo), c, sub)
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.market_export_package("relative", c, None)


def test_the_repo_local_config_cannot_change_what_is_read(repo):
    git(repo, "config", "core.attributesFile", "/dev/null")
    git(repo, "config", "core.fsmonitor", "touch /tmp/should-not-exist-g3")
    git(repo, "config", "uploadpack.packObjectsHook", "touch /tmp/should-not-exist-g3")
    files = {"orch-addon.json": MANIFEST}
    c = package_commit(repo, files)
    assert gitsafe.market_export_package(str(repo), c, None) == files
    assert not Path("/tmp/should-not-exist-g3").exists()
