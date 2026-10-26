"""V3 G4 (resource-cap invariant): ``addonenv.scan_zip`` walks a wheel's central directory itself, drawing every entry
and declared byte from the shared ``envtree.Budget`` before it keeps anything; ``zipfile`` never lists a file that has
not been walked here."""

from __future__ import annotations

import io
import os
import struct
import zipfile

import pytest

from orch.addons.envtree import Budget, TreeError
from orch.host import addonenv


def zip_bytes(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in names.items():
            z.writestr(n, b)
    return buf.getvalue()


def scan(tmp_path, blob: bytes, budget: Budget):
    p = tmp_path / "w.whl"
    p.write_bytes(blob)
    fd = os.open(p, os.O_RDONLY)
    try:
        return addonenv.scan_zip(fd, budget)
    finally:
        os.close(fd)


def set_count(blob: bytes, n: int) -> bytes:
    at = blob.rfind(b"PK\x05\x06")
    return blob[: at + 8] + struct.pack("<HH", n, n) + blob[at + 12 :]


def test_an_honest_zip_lists_names_and_declared_sizes(tmp_path):
    got = scan(tmp_path, zip_bytes({"a/b.py": b"123", "c.txt": b""}), Budget(bytes=100, entries=10))
    assert got == [("a/b.py", 3), ("c.txt", 0)]


def test_an_end_record_that_lies_about_its_count_stops_at_the_second_entry(tmp_path):
    blob = set_count(zip_bytes({f"f{i}": b"" for i in range(5000)}), 1)  # says one entry, holds 5000
    b = Budget(bytes=10_000, entries=100)
    with pytest.raises(TreeError) as e:
        scan(tmp_path, blob, b)
    assert e.value.reason == "package" and b.entries >= 98  # at most the first entries were ever drawn
    assert zipfile.ZipFile(io.BytesIO(blob)) is not None  # (python's own parser would list them all)


@pytest.mark.slow
def test_a_hundred_thousand_entries_with_a_lying_count_never_get_listed(tmp_path):
    blob = set_count(zip_bytes({f"f{i}": b"" for i in range(100_000)}), 1)
    b = Budget(bytes=10_000, entries=100)
    with pytest.raises(TreeError):
        scan(tmp_path, blob, b)
    assert b.entries >= 98


def test_an_honest_count_over_the_budget_is_too_large_after_the_budget(tmp_path):
    b = Budget(bytes=10_000, entries=50)
    with pytest.raises(TreeError) as e:
        scan(tmp_path, zip_bytes({f"f{i}": b"" for i in range(2000)}), b)
    assert e.value.reason == "too_large" and b.entries == -1  # stopped at the first entry past the budget


def test_declared_bytes_are_drawn_before_the_entry_is_kept(tmp_path):
    blob = zip_bytes({"zeros": b"\0" * (5 << 20)})  # a few KiB on disk, 5 MiB when written
    assert len(blob) < 20_000
    with pytest.raises(TreeError) as e:
        scan(tmp_path, blob, Budget(bytes=1 << 20, entries=10))
    assert e.value.reason == "too_large"


def test_the_budget_is_shared_between_wheels(tmp_path):
    shared = Budget(bytes=1000, entries=10)
    blob = zip_bytes({"a": b"x" * 600})
    scan(tmp_path, blob, shared)
    with pytest.raises(TreeError):
        scan(tmp_path, blob, shared)


@pytest.mark.parametrize("which", ["zip64_count", "directory_past_end", "no_end_record", "non_utf8", "short"])
def test_odd_zips_are_refused(tmp_path, which):
    blob = zip_bytes({"a": b"1", "b": b"2"})
    at = blob.rfind(b"PK\x05\x06")
    if which == "zip64_count":
        blob = set_count(blob, 0xFFFF)
    elif which == "directory_past_end":
        blob = blob[: at + 12] + struct.pack("<LL", 1 << 20, 0) + blob[at + 20 :]
    elif which == "no_end_record":
        blob = blob[:at]
    elif which == "non_utf8":
        blob = blob.replace(b"PK\x01\x02", b"PK\x01\x02", 1)
        i = blob.find(b"PK\x01\x02") + 46
        blob = blob[:i] + b"\xff" + blob[i + 1 :]
    elif which == "short":
        blob = blob[:10]
    with pytest.raises(TreeError):
        scan(tmp_path, blob, Budget(bytes=100, entries=10))
