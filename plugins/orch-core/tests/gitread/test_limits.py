"""Section 0.11 bounds, each enforced **before** the work it bounds: a 1 GiB zlib bomb (loose and packed), the delta
depth, the tree entries, the commits walked, the changed files and the patch text."""

from __future__ import annotations

import time
import zlib

import pytest

from orch.gitread import limits, local, objects, patchtext
from orch.gitread.errors import Unavailable
from orch.gitread.pinning import pin_repo
from orch.gitread.repo import Repo

from .packs import delta_chain, oid, write_pack, zlib_bomb

GIB = 1 << 30


class _Tally:
    """Wraps ``zlib.decompressobj`` in :mod:`orch.gitread.objects` and records the bytes each one produced."""

    def __init__(self, monkeypatch):
        self.outputs: list[int] = []
        real = zlib.decompressobj
        tally = self

        class Proxy:
            def __init__(self):
                self._d = real()
                self._i = len(tally.outputs)
                tally.outputs.append(0)

            def decompress(self, data, max_length=0):
                out = self._d.decompress(data, max_length)
                tally.outputs[self._i] += len(out)
                return out

            def __getattr__(self, name):
                return getattr(self._d, name)

        monkeypatch.setattr(objects.zlib, "decompressobj", Proxy)


@pytest.fixture
def repo(make_repo):
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    return r


def opened(r) -> Repo:
    return Repo(pin_repo(str(r.path), [str(r.path)]))


def _loose(r, hexid: str, data: bytes) -> None:
    d = r.gitdir / "objects" / hexid[:2]
    d.mkdir(exist_ok=True)
    (d / hexid[2:]).write_bytes(data)


def test_a_1gib_loose_bomb_with_an_honest_header_is_refused_before_inflating(repo, monkeypatch):
    bomb = zlib_bomb(b"blob %d\0" % GIB, GIB)
    assert len(bomb) < 2 << 20
    fake = "ab" * 20
    _loose(repo, fake, bomb)
    tally = _Tally(monkeypatch)
    t = time.monotonic()
    with opened(repo) as rp, pytest.raises(Unavailable, match="larger than the object size limit"):
        rp.objects.read(fake)
    assert time.monotonic() - t < 2
    assert sum(tally.outputs) <= 64  # only the header was inflated


def test_a_1gib_loose_bomb_with_a_lying_header_stops_one_byte_past_it(repo, monkeypatch):
    bomb = zlib_bomb(b"blob 10\0", GIB)
    fake = "cd" * 20
    _loose(repo, fake, bomb)
    tally = _Tally(monkeypatch)
    with opened(repo) as rp, pytest.raises(Unavailable, match="beyond its declared size"):
        rp.objects.read(fake)
    assert sum(tally.outputs) <= max(64, len(b"blob 10\0") + 10 + 1)  # the header probe, then one byte past


def test_a_1gib_pack_entry_bomb_stops_one_byte_past_its_header(repo, monkeypatch):
    bomb = zlib_bomb(b"", GIB)
    fake = "ef" * 20
    write_pack(repo.gitdir / "objects", [(fake, b"x" * 10, bomb, None)])
    tally = _Tally(monkeypatch)
    t = time.monotonic()
    with opened(repo) as rp, pytest.raises(Unavailable, match="beyond its declared size"):
        rp.objects.read(fake)
    assert time.monotonic() - t < 2
    assert max(tally.outputs) <= 11


def test_a_pack_entry_over_64_mib_is_refused_before_inflating(repo, monkeypatch):
    fake = "12" * 20
    write_pack(repo.gitdir / "objects", [(fake, b"", zlib_bomb(b"", 1 << 20), None)])
    # rewrite the entry header to claim 64 MiB + 1 (the size varint follows the pack header)
    pack = next((repo.gitdir / "objects" / "pack").glob("*.pack"))
    data = bytearray(pack.read_bytes())
    size = limits.OBJECT_MAX + 1
    hdr = bytearray([0x80 | (3 << 4) | (size & 15)])
    size >>= 4
    while size:
        hdr.append((size & 0x7F) | (0x80 if size >> 7 else 0))
        size >>= 7
    data[12:13] = hdr
    pack.write_bytes(bytes(data))
    tally = _Tally(monkeypatch)
    with opened(repo) as rp, pytest.raises(Unavailable, match="larger than the object size limit"):
        rp.objects.read(fake)
    assert tally.outputs == []


def test_delta_depth_64_is_read_and_65_is_refused_before_the_last_inflate(repo, monkeypatch):
    ids = delta_chain(repo.gitdir / "objects", 65)
    with opened(repo) as rp:
        kind, data = rp.objects.read(ids[64])
        assert kind == "blob" and data == b"base\n" + b"x" * 64
    # git agrees about the 64-deep object
    assert repo.git("cat-file", "-p", ids[64]) == "base\n" + "x" * 64
    calls = []
    real = objects.ObjectStore._inflate_at

    def spy(self, k, start, size):
        calls.append(start)
        return real(self, k, start, size)

    monkeypatch.setattr(objects.ObjectStore, "_inflate_at", spy)
    with opened(repo) as rp, pytest.raises(Unavailable, match="deeper than 64"):
        rp.objects.read(ids[65])
    assert len(calls) == 64  # the 65th delta was never inflated


def test_tree_entries_are_capped_while_parsing(repo, monkeypatch):
    for i in range(50):
        repo.write(f"d/f{i}", f"{i}\n")
    repo.commit()
    built = []
    from orch.gitread import repo as repomod

    orig = repomod.TreeEntry.__init__

    def spy(self, *a):
        built.append(1)
        orig(self, *a)

    monkeypatch.setattr(repomod.TreeEntry, "__init__", spy)
    with opened(repo) as rp:
        tree = rp.commit(repo.sha("HEAD")).tree
        sub = next(e for e in rp.tree(tree) if e.name == b"d")
        built.clear()
        with pytest.raises(Unavailable, match="too many paths"):
            rp.tree(sub.oid, max_entries=10)
    assert len(built) == 10  # the 11th entry was refused before it was built


def test_changed_files_are_capped_while_walking(repo, monkeypatch):
    repo.git("checkout", "-q", "-b", "feat")
    for i in range(30):
        repo.write(f"n{i:02d}", "x\n")
    repo.commit()
    monkeypatch.setattr(limits, "DIFF_FILES_MAX", 5)
    rendered = []
    real = patchtext._one

    def spy(*a, **k):
        rendered.append(a[1])
        return real(*a, **k)

    monkeypatch.setattr(patchtext, "_one", spy)
    got = local.diff(repo.path, branch="feat", default_branch=None)
    assert got["truncated"] is True and len(got["files"]) == 5 and len(rendered) == 5


def test_patch_text_is_not_kept_past_max_bytes(repo, monkeypatch):
    repo.git("checkout", "-q", "-b", "feat")
    for i in range(40):
        repo.write(f"n{i:02d}", "".join(f"{j}\n" for j in range(100)))
    repo.commit()
    kept = []
    real = patchtext.render

    def spy(*a, **k):
        text, files, cut = real(*a, **k)
        kept.append(len(text))
        return text, files, cut

    monkeypatch.setattr("orch.gitread.ops.render", spy)
    got = local.diff(repo.path, branch="feat", default_branch=None, max_bytes=2048)
    one_file = len(repo.git("diff", "main", "feat", "--", "n00", raw=True))
    assert got["truncated"] is True and kept[0] <= 2048 + one_file
    assert len(got["files"]) == 40 and all(f["add"] == 100 for f in got["files"])


def test_commits_walked_are_capped_before_reading(repo, monkeypatch):
    for i in range(12):
        repo.write("a.txt", f"{i}\n")
        repo.commit()
    monkeypatch.setattr(limits, "COMMITS_MAX", 5)
    reads = []
    real = objects.ObjectStore.read

    def spy(self, o, expect=None):
        if expect == "commit":
            reads.append(o)
        return real(self, o, expect)

    monkeypatch.setattr(objects.ObjectStore, "read", spy)
    with opened(repo) as rp, pytest.raises(Unavailable, match="too many commits"):
        rp.merge_base(repo.sha("HEAD"), "0" * 40)
    assert len(reads) == 5


def test_the_bomb_helper_is_a_real_1gib_stream():
    bomb = zlib_bomb(b"", 4 << 20)
    assert zlib.decompress(bomb) == b"\0" * (4 << 20)
    assert oid("blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_the_request_line_budget_marks_later_files_too_large(repo, monkeypatch):
    repo.git("checkout", "-q", "-b", "feat")
    for i in range(3):
        repo.write(f"f{i}", "".join(f"{j}\n" for j in range(40)))
    repo.commit()
    monkeypatch.setattr(limits, "REQUEST_LINES_MAX", 90)
    got = local.diff(repo.path, branch="feat", default_branch=None)
    assert [f["too_large"] for f in got["files"]] == [False, False, True]  # 41 + 41 lines fit, the third does not
