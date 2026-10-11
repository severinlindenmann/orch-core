"""Manifest loading, package reading and the package digest (ticket-format §8, §8.1)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from orch import schema
from orch.addons.manifest import ManifestError, derive_binds, load_manifest, load_package_manifest
from orch.addons.package import MAX_FILE_BYTES, PackageError, package_digest, read_package
from orch.store.render import HEADINGS
from tests.addons.helpers import MANIFEST, make_package, manifest


def raw(m):
    return json.dumps(m).encode()


def test_a_valid_manifest_loads_and_binds_are_derived():
    m = load_manifest(raw(MANIFEST))
    assert (m.name, m.version, m.capabilities) == ("echo", "1.0.0", ["serve_http"])
    assert derive_binds(m) == {
        "fields": {"mood": ["plan", "verify"], "points": ["plan"]},
        "sections": [{"id": "echo.notes", "gate": ["plan"], "types": ["bug", "feature"]}],
    }
    # the derived binds are what the event schema accepts
    ev = {"name": "echo", "binds": derive_binds(m)}
    assert all(s["id"].startswith("echo.") for s in ev["binds"]["sections"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(name="Echo"),
        lambda m: m.update(name="role"),  # a reserved event prefix
        lambda m: m.update(unknown=1),
        lambda m: m.update(capabilities=["root"]),
        lambda m: m.update(cli={"x": 1}),  # reserved for P2
        lambda m: m.update(skills=["a"]),
        lambda m: m["sections"][0].update(heading="Plan"),  # a core heading
        lambda m: m["sections"][0].update(heading="plan"),
        lambda m: m["sections"][1].update(heading="Echo notes"),  # a second section with the same heading
        lambda m: m["sections"][0].update(heading="A # B"),
        lambda m: m["sections"][0].update(heading="a​b"),  # zero-width space
        lambda m: m["sections"][0].update(heading=" Echo "),
    ],
)
def test_invalid_manifests_are_refused(mutate):
    m = manifest()
    mutate(m)
    with pytest.raises(ManifestError):
        load_manifest(raw(m))


def test_not_json_and_hostile_json_are_refused_without_echo():
    for data in (b"", b"{", b"[]", b'{"a": 1, "a": 2}', b"\xff\xfe", b"{" * 100000, b'{"schema": NaN}'):
        with pytest.raises(ManifestError):
            load_manifest(data)
    with pytest.raises(ManifestError) as e:
        load_manifest(raw(manifest(title="x\x1b[31m")))  # the message carries no raw escape
    assert "\x1b" not in str(e.value)


def test_core_headings_in_the_schema_equal_the_store_headings():
    assert schema._CORE_HEADINGS == {h.casefold() for h in HEADINGS.values()}


def test_agents_md_is_one_plain_line():
    load_manifest(raw(manifest(agents_md="Run `orch addon list` to see what is granted.")))
    load_manifest(raw(manifest(agents_md="")))
    for bad in ("two\nlines", "# heading", "```", "- item", "> quote", "| row", " lead", "x" * 201, "a\x00b", "a​b"):
        with pytest.raises(ManifestError):
            load_manifest(raw(manifest(agents_md=bad)))


def test_needs_rules_are_validated_when_the_manifest_loads():
    good = {
        "id": "stuck",
        "when": ["and", ["eq", ["var", "status"], "testing"], ["gt", ["field", "points"], 3]],
        "who": ["owner"],
        "text": "look at it",
    }
    load_manifest(raw(manifest(needs=[good])))
    bads = [
        {**good, "when": ["nope", 1]},
        {**good, "when": ["field", "missing"]},  # not a field of this addon
        {**good, "who": []},
        {**good, "who": ["root"]},
        {**good, "text": "x" * 121},
        {**good, "text": "a​b"},
        {**good, "id": "Bad Id"},
        {**good, "extra": 1},
    ]
    for bad in bads:
        with pytest.raises(ManifestError):
            load_manifest(raw(manifest(needs=[bad])))
    with pytest.raises(ManifestError):  # duplicate rule id
        load_manifest(raw(manifest(needs=[good, good])))
    with pytest.raises(ManifestError):  # more than 16 rules
        load_manifest(raw(manifest(needs=[{**good, "id": f"r{i}"} for i in range(17)])))


def test_the_directory_name_must_equal_the_manifest_name(tmp_path):
    make_package(tmp_path, "echo")
    pkg = read_package(tmp_path / "addons" / "echo")
    assert load_package_manifest(pkg, "echo").name == "echo"
    with pytest.raises(ManifestError):
        load_package_manifest(pkg, "other")


# ----------------------------------------------------------------------------------------------- package and digest


def test_the_digest_is_the_sha256sum_listing(tmp_path):
    d = make_package(tmp_path, files={"lib/util.py": b"x = 1\n", "B.txt": b"b"})
    pkg = read_package(d)
    assert set(pkg.files) == {"orch-addon.json", "addon.py", "lib/util.py", "B.txt"}
    # what the shell computes (§8.1), in the C locale
    listing = subprocess.run(
        "find . -type f | sed 's|^\\./||' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256",
        shell=True,
        cwd=d,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert pkg.digest == "sha256:" + listing
    assert pkg.digest == package_digest(pkg.files)


def test_any_changed_byte_name_or_extra_file_changes_the_digest(tmp_path):
    d = make_package(tmp_path)
    base = read_package(d).digest
    (d / "addon.py").write_bytes((d / "addon.py").read_bytes() + b"#")
    changed = read_package(d).digest
    (d / "more.txt").write_text("x")
    added = read_package(d).digest
    assert len({base, changed, added}) == 3
    (d / "more.txt").rename(d / "more2.txt")
    assert read_package(d).digest != added
    assert package_digest({"a": b"", "b": b"x"}) != package_digest({"a": b"x", "b": b""})  # path and content are bound


def test_empty_and_missing_packages_are_refused(tmp_path):
    with pytest.raises(PackageError):
        package_digest({})
    (tmp_path / "e").mkdir()
    with pytest.raises(PackageError, match="orch-addon.json"):
        read_package(tmp_path / "e")
    with pytest.raises(PackageError):
        read_package(tmp_path / "nope")


@pytest.mark.parametrize(
    "name", [".hidden", "a b", "naïve.py", "-x", "a" * 65, "__pycache__", "x\x1b[31m", "a\nb", "..a"[1:]]
)
def test_hostile_file_names_are_refused(tmp_path, name):
    d = make_package(tmp_path)
    try:
        (d / name).write_text("x")
    except OSError:
        pytest.skip("this file system cannot hold that name")
    with pytest.raises(PackageError) as e:
        read_package(d)
    assert "\x1b" not in str(e.value) and "\n" not in str(e.value)


def test_symlinks_are_refused_in_every_position(tmp_path):
    d = make_package(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(secret, d / "link.txt")
    with pytest.raises(PackageError, match="not a regular file"):
        read_package(d)
    os.unlink(d / "link.txt")
    os.symlink(tmp_path, d / "dirlink")  # a directory link out of the package
    with pytest.raises(PackageError):
        read_package(d)
    os.unlink(d / "dirlink")
    os.rename(d / "orch-addon.json", tmp_path / "m.json")  # the manifest itself a link
    os.symlink(tmp_path / "m.json", d / "orch-addon.json")
    with pytest.raises(PackageError):
        read_package(d)
    os.unlink(d / "orch-addon.json")
    os.rename(tmp_path / "m.json", d / "orch-addon.json")
    moved = tmp_path / "moved"
    os.rename(d, moved)
    os.symlink(moved, d)  # the package directory itself
    with pytest.raises(PackageError, match="symbolic link"):
        read_package(d)


def test_hard_links_fifos_and_sockets_are_refused(tmp_path):
    d = make_package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    os.link(outside, d / "hard.txt")
    with pytest.raises(PackageError, match="hard link"):
        read_package(d)
    os.unlink(d / "hard.txt")
    os.mkfifo(d / "pipe")
    with pytest.raises(PackageError):
        read_package(d)


def test_size_count_and_depth_limits(tmp_path):
    d = make_package(tmp_path)
    (d / "big.bin").write_bytes(b"\0" * (MAX_FILE_BYTES + 1))
    with pytest.raises(PackageError, match="larger"):
        read_package(d)
    (d / "big.bin").unlink()
    (d / "orch-addon.json").write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(PackageError, match="orch-addon.json"):
        read_package(d)
    (d / "orch-addon.json").write_text(json.dumps(MANIFEST))
    deep = d
    for i in range(6):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    with pytest.raises(PackageError, match="levels"):
        read_package(d)
    (d / "d0").rename(d.parent / "gone")
    for i in range(257):
        (d / f"f{i}.txt").write_text("")
    with pytest.raises(PackageError, match="files"):
        read_package(d)


def test_unreadable_files_are_a_package_error_not_a_crash(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    d = make_package(tmp_path)
    (d / "addon.py").chmod(0)
    try:
        with pytest.raises(PackageError):
            read_package(d)
    finally:
        (d / "addon.py").chmod(0o600)


def test_the_listing_digest_matches_hashlib(tmp_path):
    files = {"b": b"2", "a": b"1"}
    lines = f"{hashlib.sha256(b'1').hexdigest()}  a\n{hashlib.sha256(b'2').hexdigest()}  b\n"
    assert package_digest(files) == "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def test_a_symlinked_addons_directory_is_refused(tmp_path):
    from orch.addons.install import read_installed

    real = tmp_path / "elsewhere"
    make_package(real)
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(real / "addons", ws / "addons")
    with pytest.raises(PackageError, match="symbolic link"):
        read_installed(ws, "echo")
