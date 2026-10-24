"""Opus security review of G4 (#1177): adversarial probes. Each test asserts the SAFE behaviour, so a failing test is a
finding. Not part of the PR (review-only)."""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
import zlib

import pytest

from orch.addons import lockfile
from orch.addons.envtree import Budget, TreeError
from orch.host import addonenv
from orch.market import MarketError, untar


# ------------------------------------------------------------------ F1: PAX sparse members bypass the Budget
def _sparse_tgz(realsize: int, n: int = 1) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as t:
        for i in range(n):
            ti = tarfile.TarInfo(f"package/GNUSparseFile.0/big{i}.bin")
            ti.size = 1
            ti.mode = 0o644
            ti.pax_headers = {
                "GNU.sparse.map": f"{realsize - 1},1",
                "GNU.sparse.size": str(realsize),
                "GNU.sparse.name": f"package/big{i}.bin",
            }
            t.addfile(ti, io.BytesIO(b"x"))
    return buf.getvalue()


def test_pax_sparse_member_is_refused_or_charged(tmp_path):
    blob = _sparse_tgz(8 << 20)
    assert len(blob) < 4096
    src = tmp_path / "t.tgz"
    src.write_bytes(blob)
    out = tmp_path / "out"
    out.mkdir()
    budget = Budget(bytes=1 << 20, entries=100)  # 1 MiB may come out of this archive
    fd = os.open(src, os.O_RDONLY)
    dfd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            untar.unpack(fd, dfd, budget)
        except TreeError:
            return  # safe: refused
    finally:
        os.close(fd)
        os.close(dfd)
    written = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    assert written <= 1 << 20, f"{written} bytes written from a {len(blob)}-byte tarball with a 1 MiB budget"


# ------------------------------------------------------------------ F2: zip64 polyglot: scan_zip and zipfile disagree
def _lfh(name: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(data)
    return struct.pack("<4sHHHHHLLLHH", b"PK\x03\x04", 20, 0, 0, 0, 0, crc, len(data), len(data), len(name), 0) + name + data


def _cdh(name: bytes, data: bytes, off: int) -> bytes:
    crc = zlib.crc32(data)
    return (
        struct.pack(
            "<4sHHHHHHLLLHHHHHLL", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, crc, len(data), len(data), len(name), 0, 0, 0, 0,
            0o100644 << 16, off,
        )
        + name
    )  # fmt: skip


def polyglot() -> bytes:
    evil_n, evil_d = b"evil.pth", b"import os\n"
    ok_n, ok_d = b"pkg/__init__.py", b""
    a = _lfh(evil_n, evil_d)
    b = _lfh(ok_n, ok_d)
    cd_ok = _cdh(ok_n, ok_d, len(a))
    cd_evil = _cdh(evil_n, evil_d, 0)
    cd_ok_off = len(a) + len(b)
    cd_evil_off = cd_ok_off + len(cd_ok)
    z64_off = cd_evil_off + len(cd_evil)
    z64 = struct.pack("<4sQHHLLQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, len(cd_evil), cd_evil_off)
    loc = struct.pack("<4sLQL", b"PK\x06\x07", 0, z64_off, 1)
    eocd = struct.pack("<4sHHHHLLH", b"PK\x05\x06", 0, 0, 1, 1, len(cd_ok), cd_ok_off, 0)
    return a + b + cd_ok + cd_evil + z64 + loc + eocd


def test_scan_zip_sees_what_zipfile_and_so_pip_sees(tmp_path):
    p = tmp_path / "x-1.0-py3-none-any.whl"
    p.write_bytes(polyglot())
    fd = os.open(p, os.O_RDONLY)
    try:
        try:
            ours = [n for n, _ in addonenv.scan_zip(fd, Budget())]
        except TreeError:
            return  # safe: a zip64 record is refused
    finally:
        os.close(fd)
    theirs = zipfile.ZipFile(p).namelist()
    assert ours == theirs, f"orch judged {ours}, zipfile (pip) installs {theirs}"


# ------------------------------------------------------------------ F3: lockfile regex backtracking
def test_a_one_megabyte_python_line_is_judged_quickly():
    h = " --hash=sha256:" + "0" * 64
    line = "a==1" + h * ((1 << 20) // len(h) - 1) + " x"
    data = line.encode()[: (1 << 20) - 1]
    t = time.monotonic()
    with pytest.raises(MarketError):
        lockfile.check("python-requirements", data)
    assert time.monotonic() - t < 2.0, f"{time.monotonic() - t:.1f} s"


# ------------------------------------------------------------------ F4: one descriptor per directory, held to the end
def test_a_tarball_with_many_directories_does_not_need_a_descriptor_each(tmp_path):
    code = r"""
import io, os, resource, sys, tarfile
from orch.addons.envtree import Budget
from orch.market import untar
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as t:
    for i in range(400):
        ti = tarfile.TarInfo(f"package/d{i}/f.js"); ti.size = 1
        t.addfile(ti, io.BytesIO(b"x"))
src = sys.argv[1] + "/t.tgz"; open(src, "wb").write(buf.getvalue())
out = sys.argv[1] + "/out"; os.mkdir(out)
fd = os.open(src, os.O_RDONLY); dfd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
print(untar.unpack(fd, dfd, Budget()).files)
"""
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "400", r.stderr[-800:]


# ------------------------------------------------------------------ F2b: no zip64 at all: slack before the end record
def polyglot_slack() -> bytes:
    """No zip64. zipfile assumes the central directory ends right at the end record and shifts every offset by the
    slack ("concat"); scan_zip reads at the stated offset. Two directories of the same size, the evil one last."""
    evil_n, ok_n, d = b"evil.pth", b"ok_ab.py", b"import os\n"  # names of equal length
    concat = len(_cdh(ok_n, d, 0))  # = the size of one directory
    pre = b"\0" * concat
    a, b = _lfh(evil_n, d), _lfh(ok_n, d)
    cd_ok = _cdh(ok_n, d, len(pre) + len(a))
    cd_evil = _cdh(evil_n, d, 0)  # zipfile reads its header at 0 + concat == len(pre): the evil member
    cd_ok_off = len(pre) + len(a) + len(b)
    eocd = struct.pack("<4sHHHHLLH", b"PK\x05\x06", 0, 0, 1, 1, len(cd_ok), cd_ok_off, 0)
    assert len(cd_ok) == len(cd_evil) == concat
    return pre + a + b + cd_ok + cd_evil + eocd


def test_scan_zip_and_zipfile_agree_when_the_directory_does_not_end_at_the_end_record(tmp_path):
    p = tmp_path / "x-1.0-py3-none-any.whl"
    p.write_bytes(polyglot_slack())
    fd = os.open(p, os.O_RDONLY)
    try:
        try:
            ours = [n for n, _ in addonenv.scan_zip(fd, Budget())]
        except TreeError:
            return
    finally:
        os.close(fd)
    theirs = zipfile.ZipFile(p).namelist()
    assert ours == theirs, f"orch judged {ours}, zipfile (pip) installs {theirs}"


def test_the_walk_does_not_hold_a_descriptor_per_pending_directory(tmp_path):
    code = r"""
import os, resource, sys
from orch.addons import envtree
root = sys.argv[1] + "/env"
for i in range(400):
    os.makedirs(f"{root}/data/d{i}")
    open(f"{root}/data/d{i}/f.json", "w").write("{}")
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
print(len(envtree.walk(root).entries))
"""
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "400", r.stderr[-800:]


# ------------------------------------------------------------------ F5: the deadline does not bound one read
from tests.market import test_download as _td  # noqa: E402

server = _td.server  # the local TLS fixture


class _Drip(_td.Handler):
    def do_GET(self):  # noqa: N802
        if self.path != "/slow":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Length", "30")
        self.end_headers()
        for _ in range(30):  # one byte every 0.1 s: each read is well inside the per-operation timeout
            time.sleep(0.1)
            self.wfile.write(b"x")
            self.wfile.flush()
        return None


def test_the_whole_deadline_bounds_a_slow_drip(server, monkeypatch):
    monkeypatch.setattr(_td.Handler, "do_GET", _Drip.do_GET)
    t = time.monotonic()
    with pytest.raises(MarketError):
        _td.dl("https://pypi.org/slow", secs=0.3)
    assert time.monotonic() - t < 1.5, f"a 0.3 s deadline took {time.monotonic() - t:.1f} s"
