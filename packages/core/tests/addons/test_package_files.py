"""V3 G3: ``from_files`` (a package from an in-memory map, as a git export gives it) and ``read_package`` (a directory)
give the same verdict: the rules of 8.1 are one function."""

from __future__ import annotations

import pytest

from orch.addons import package

M = b'{"schema": "orch.addon/3"}'
CASES = {
    "ok": ({"orch-addon.json": M, "a.py": b"1", "lib/b.py": b"2"}, True),
    "empty": ({}, False),
    "no manifest": ({"a.py": b"1"}, False),
    "manifest too big": ({"orch-addon.json": b"x" * (package.MAX_MANIFEST_BYTES + 1)}, False),
    "dot name": ({"orch-addon.json": M, ".env": b"1"}, False),
    "so file": ({"orch-addon.json": M, "x.so": b"1"}, False),
    "pycache": ({"orch-addon.json": M, "__pycache__/x.py": b"1"}, False),
    "too deep": ({"orch-addon.json": M, "a/b/c/d/e/f/g.py": b"1"}, False),
    "non-ascii": ({"orch-addon.json": M, "café.py": b"1"}, False),
    "too many": ({"orch-addon.json": M, **{f"f{i}": b"1" for i in range(package.MAX_FILES)}}, False),
    "file and dir": ({"orch-addon.json": M, "d": b"1", "d/x": b"1"}, False),
}


@pytest.mark.parametrize("name", list(CASES))
def test_same_verdict_on_a_map_and_on_a_directory(tmp_path, name):
    files, ok = CASES[name]
    try:
        from_map = package.from_files(files)
    except package.PackageError:
        from_map = None
    root = tmp_path / "pkg"
    root.mkdir()
    made = True
    for rel, data in files.items():
        path = root / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:  # "d" as file and directory cannot exist on disk: the map-only verdict is the point
            made = False
    try:
        from_dir = package.read_package(root) if made else None
    except package.PackageError:
        from_dir = None
    assert (from_map is not None) is ok
    if made:
        assert (from_dir is not None) is ok
        if ok:
            assert from_dir.digest == from_map.digest and from_dir.files == from_map.files


def test_case_twins_in_one_directory_are_refused_on_a_map():
    with pytest.raises(package.PackageError):
        package.from_files({"orch-addon.json": M, "A.py": b"1", "a.py": b"2"})
    # the same name in two directories is not a twin
    package.from_files({"orch-addon.json": M, "a/X.py": b"1", "b/x.py": b"2"})


def test_the_map_is_copied():
    files = {"orch-addon.json": M}
    pkg = package.from_files(files)
    files["x"] = b"1"
    assert "x" not in pkg.files
