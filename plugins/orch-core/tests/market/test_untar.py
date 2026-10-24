"""V3 G4 (design-g4 section 2, C7): orch unpacks npm tarballs itself: no links, no nested ``node_modules``, no
scripts, nothing outside the target."""

from __future__ import annotations

import io
import os
import stat
import tarfile

import pytest

from orch.addons.envtree import Budget, TreeError
from orch.market import untar


def tgz(*members, raw: bytes | None = None) -> bytes:
    """``members``: ``(name, data | None for a dir, extra)``; ``extra`` is a dict of TarInfo attributes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, extra in members:
            ti = tarfile.TarInfo(name)
            for k, v in (extra or {}).items():
                setattr(ti, k, v)
            if data is None and "type" not in (extra or {}):
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                t.addfile(ti)
            elif data is None:
                t.addfile(ti)
            else:
                ti.size = len(data)
                ti.mode = (extra or {}).get("mode", 0o644)
                t.addfile(ti, io.BytesIO(data))
    return buf.getvalue() if raw is None else raw


def run(tmp_path, blob: bytes, dest=True, budget=None):
    src = tmp_path / "t.tgz"
    src.write_bytes(blob)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    fd = os.open(src, os.O_RDONLY)
    dfd = os.open(out, os.O_RDONLY | os.O_DIRECTORY) if dest else None
    try:
        return untar.unpack(fd, dfd, budget or Budget()), out
    finally:
        os.close(fd)
        if dfd is not None:
            os.close(dfd)


def reason(tmp_path, blob) -> str:
    with pytest.raises(TreeError) as e:
        run(tmp_path, blob)
    return e.value.reason


def test_a_normal_package_is_unpacked_with_normalized_modes(tmp_path):
    blob = tgz(
        ("package/", None, None),
        ("package/package.json", b'{"name":"ms"}', {"mode": 0o600}),
        ("package/lib/index.js", b"module.exports=1\n", {"mode": 0o777}),
        ("package/lib/", None, None),
    )
    info, out = run(tmp_path, blob)
    assert (info.files, info.compiled) == (2, False)
    assert (out / "package.json").read_bytes() == b'{"name":"ms"}'
    assert stat.S_IMODE((out / "package.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((out / "lib/index.js").stat().st_mode) == 0o755
    assert stat.S_IMODE((out / "lib").stat().st_mode) == 0o755


def test_a_dry_run_writes_nothing_but_judges_the_same(tmp_path):
    info, out = run(tmp_path, tgz(("package/a.node", b"\x7fELF", None)), dest=False)
    assert info.compiled is True and list(out.iterdir()) == []
    src = tmp_path / "t.tgz"
    src.write_bytes(tgz(("package/x", None, {"type": tarfile.SYMTYPE, "linkname": "/etc/hosts"})))
    fd = os.open(src, os.O_RDONLY)
    with pytest.raises(TreeError):
        untar.unpack(fd, None, Budget())
    os.close(fd)


@pytest.mark.parametrize(
    ("members", "why"),
    [
        ([("package/l", None, {"type": tarfile.SYMTYPE, "linkname": "/etc/hosts"})], "link"),
        ([("package/a", b"x", None), ("package/h", None, {"type": tarfile.LNKTYPE, "linkname": "package/a"})], "link"),
        ([("package/p", None, {"type": tarfile.FIFOTYPE})], "link"),
        ([("package/d", None, {"type": tarfile.CHRTYPE})], "link"),
        ([("package/../../evil", b"x", None)], "path_escape"),
        ([("/etc/cron.d/x", b"x", None)], "path_escape"),
        ([("package/./x", b"x", None)], "path_escape"),
        ([("package//x", b"x", None)], "path_escape"),
        ([("package/a\\b", b"x", None)], "path_escape"),
        ([("package/a\nb", b"x", None)], "path_escape"),
        ([("package/node_modules/evil/index.js", b"x", None)], "package"),
        ([("package/Node_Modules/evil/index.js", b"x", None)], "package"),
        ([("package/sub/node_modules/", None, None)], "package"),
        ([("package/Readme", b"1", None), ("package/README", b"2", None)], "package"),
        ([("package/a", b"1", None), ("package/a/b", b"2", None)], "package"),
        ([("package/a/b", b"1", None), ("package/a", b"2", None)], "package"),
        ([("package/a", b"1", None), ("package/a", b"2", None)], "package"),
        ([("package/a/b", b"1", None), ("package/A/c", b"2", None)], "package"),
    ],
)
def test_hostile_members_are_refused_and_nothing_escapes(tmp_path, members, why):
    assert reason(tmp_path, tgz(*members)) == why
    assert not (tmp_path / "evil").exists() and not (tmp_path.parent / "evil").exists()


def too_large(tmp_path, blob, budget):
    with pytest.raises(TreeError) as e:
        run(tmp_path, blob, budget=budget)
    return e.value.reason


def test_limits_and_broken_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(untar, "MAX_FILE", 5)
    assert reason(tmp_path, tgz(("package/big", b"123456", None))) == "too_large"
    monkeypatch.setattr(untar, "MAX_FILE", 1 << 20)
    assert too_large(tmp_path, tgz(*[(f"package/f{i}", b"", None) for i in range(3)]), Budget(entries=2)) == "too_large"
    assert (
        too_large(tmp_path, tgz(("package/a", b"123", None), ("package/b", b"123", None)), Budget(bytes=1200))
        == "too_large"
    )
    assert reason(tmp_path, b"not a gzip") == "package"
    assert reason(tmp_path, tgz(("package/a", b"x" * 100, None))[:-20]) == "package"  # truncated


def test_a_gzip_bomb_is_stopped_by_the_decompressed_byte_count(tmp_path, monkeypatch):
    """One 5 MiB file of zeros compresses to a few KiB; the cap is on what comes OUT of the decompressor."""
    monkeypatch.setattr(untar, "MAX_FILE", 1 << 30)  # the member cap alone would let it through
    blob = tgz(("package/zeros", b"\0" * (5 << 20), None))
    assert len(blob) < 20_000
    assert too_large(tmp_path, blob, Budget(bytes=1 << 20, entries=10)) == "too_large"
    info, _ = run(tmp_path, tgz(("package/ok", b"x" * 1000, None)))
    assert info.bytes == 1000


def test_many_tarballs_each_under_the_cap_are_over_it_together(tmp_path):
    """The budget is shared by every archive of one environment: one archive streams ~10 KiB (a tar record), so a 15 KiB
    budget lets the first through and stops the second, though each alone is far under it."""
    blob = tgz(("package/data", b"x" * 2000, None))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    run(tmp_path / "a", blob, budget=Budget(bytes=15_000, entries=100))  # a single archive of this size fits
    shared = Budget(bytes=15_000, entries=100)
    run(tmp_path / "b", blob, budget=shared)
    assert 0 < shared.bytes < 15_000 - 10_000 + 1
    d = tmp_path / "second"
    d.mkdir()
    with pytest.raises(TreeError) as e:
        run(d, blob, budget=shared)
    assert e.value.reason == "too_large"
    many = Budget(entries=5)  # and the member count is aggregate too
    with pytest.raises(TreeError):
        for i in range(4):
            d = tmp_path / f"m{i}"
            d.mkdir()
            run(d, tgz(("package/a", b"1", None), ("package/b", b"2", None)), budget=many)


def test_an_oversized_pax_or_long_name_header_is_refused_before_it_is_read(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as t:
        ti = tarfile.TarInfo("package/a")
        ti.pax_headers = {"comment": "x" * 50_000}
        ti.size = 1
        t.addfile(ti, io.BytesIO(b"1"))
    assert reason(tmp_path, buf.getvalue()) == "too_large"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as t:
        ti = tarfile.TarInfo("package/" + "d/" * 20_000 + "f")  # a long-name header of 40 KB
        ti.size = 1
        t.addfile(ti, io.BytesIO(b"1"))
    assert reason(tmp_path, buf.getvalue()) == "too_large"


def test_too_many_members_including_empty_directories(tmp_path):
    blob = tgz(*[(f"package/d{i}/", None, None) for i in range(25)])
    assert too_large(tmp_path, blob, Budget(entries=10)) == "too_large"


def test_a_sparse_member_is_refused(tmp_path):
    blob = tgz(("package/s", None, {"type": tarfile.GNUTYPE_SPARSE}))
    assert reason(tmp_path, blob) in ("link", "package")


def sparse_tgz(realsize, key="GNU.sparse.size"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as t:
        ti = tarfile.TarInfo("package/big.bin")
        ti.size = 1
        ti.pax_headers = {key: str(realsize), "GNU.sparse.map": f"{realsize - 1},1"}
        t.addfile(ti, io.BytesIO(b"x"))
    return buf.getvalue()


@pytest.mark.parametrize("key", ["GNU.sparse.size", "GNU.sparse.realsize", "GNU.sparse.major"])
def test_a_pax_sparse_member_is_refused_before_a_byte_is_written(tmp_path, key):
    blob = sparse_tgz(8 << 20, key)
    assert reason(tmp_path, blob) == "link"
    assert [p for p in (tmp_path / "out").rglob("*") if p.is_file()] == []


def test_members_in_interleaved_directories_each_land_in_their_own(tmp_path):
    members = [
        (f"package/{d}/{n}", b"x", None) for d, n in (("a", "1"), ("b", "1"), ("a", "2"), ("c/d", "1"), ("a", "3"))
    ]
    info, out = run(tmp_path, tgz(*members))
    assert info.files == 5
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == [
        "a/1", "a/2", "a/3", "b/1", "c/d/1",
    ]  # fmt: skip
