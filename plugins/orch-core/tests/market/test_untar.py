"""V3 G4 (design-g4 section 2, C7): orch unpacks npm tarballs itself: no links, no nested ``node_modules``, no
scripts, nothing outside the target."""

from __future__ import annotations

import io
import os
import stat
import tarfile

import pytest

from orch.addons.envtree import TreeError
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


def run(tmp_path, blob: bytes, dest=True):
    src = tmp_path / "t.tgz"
    src.write_bytes(blob)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    fd = os.open(src, os.O_RDONLY)
    dfd = os.open(out, os.O_RDONLY | os.O_DIRECTORY) if dest else None
    try:
        return untar.unpack(fd, dfd), out
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
        untar.unpack(fd, None)
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


def test_limits_and_broken_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(untar, "MAX_FILE", 5)
    assert reason(tmp_path, tgz(("package/big", b"123456", None))) == "too_large"
    monkeypatch.setattr(untar, "MAX_FILE", 1 << 20)
    monkeypatch.setattr(untar, "MAX_ENTRIES", 2)
    assert reason(tmp_path, tgz(*[(f"package/f{i}", b"", None) for i in range(3)])) == "too_large"
    monkeypatch.setattr(untar, "MAX_ENTRIES", 100)
    monkeypatch.setattr(untar, "MAX_BYTES", 4)
    assert reason(tmp_path, tgz(("package/a", b"123", None), ("package/b", b"123", None))) == "too_large"
    monkeypatch.setattr(untar, "MAX_BYTES", 1 << 20)
    assert reason(tmp_path, b"not a gzip") == "package"
    assert reason(tmp_path, tgz(("package/a", b"x" * 100, None))[:-20]) == "package"  # truncated


def test_the_output_directory_is_not_followed_through_a_planted_link(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, out / "lib")  # an agent planted a link where the tarball wants a directory
    src = tmp_path / "t.tgz"
    src.write_bytes(tgz(("package/lib/x.js", b"x", None)))
    fd = os.open(src, os.O_RDONLY)
    dfd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(TreeError):
            untar.unpack(fd, dfd)
    finally:
        os.close(fd)
        os.close(dfd)
    assert list(elsewhere.iterdir()) == []


def test_a_gzip_bomb_is_stopped_by_the_decompressed_byte_count(tmp_path, monkeypatch):
    """One 5 MiB file of zeros compresses to a few KiB; the cap is on what comes OUT of the decompressor."""
    monkeypatch.setattr(untar, "MAX_FILE", 1 << 30)  # the member cap alone would let it through
    monkeypatch.setattr(untar, "MAX_BYTES", 1 << 30)
    blob = tgz(("package/zeros", b"\0" * (5 << 20), None))
    assert len(blob) < 20_000
    monkeypatch.setattr(untar, "MAX_BYTES", 1 << 20)
    monkeypatch.setattr(untar, "MAX_ENTRIES", 10)  # the per-member allowance is 20 KiB
    assert reason(tmp_path, blob) == "too_large"
    # and a file whose header LIES about its size cannot make tarfile inflate more than the limit either
    monkeypatch.setattr(untar, "MAX_BYTES", 1 << 30)
    monkeypatch.setattr(untar, "MAX_ENTRIES", 20_000)
    info, _ = run(tmp_path, tgz(("package/ok", b"x" * 1000, None)))
    assert info.bytes == 1000


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


def test_too_many_members_including_empty_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(untar, "MAX_ENTRIES", 10)
    assert reason(tmp_path, tgz(*[(f"package/d{i}/", None, None) for i in range(25)])) == "too_large"


def test_a_sparse_member_is_refused(tmp_path):
    blob = tgz(("package/s", None, {"type": tarfile.GNUTYPE_SPARSE}))
    assert reason(tmp_path, blob) in ("link", "package")
