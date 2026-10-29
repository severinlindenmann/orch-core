"""privdir.privatize: the setup-only repair removes group/other write from our own exact directories, nothing else."""

from __future__ import annotations

import os
import stat

from orch import privdir

UID = os.getuid()


def mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_removes_group_and_other_write_only(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    os.chmod(d, 0o777)
    assert privdir.privatize([d], UID) == [d]
    assert mode(d) == 0o755  # read/execute bits are kept, never added


def test_never_adds_a_bit(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    os.chmod(d, 0o700)
    assert privdir.privatize([d], UID) == []
    assert mode(d) == 0o700


def test_a_symlink_is_not_followed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o775)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert privdir.privatize([link], UID) == []
    assert mode(real) == 0o775


def test_another_owner_is_left_alone(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    os.chmod(d, 0o775)
    assert privdir.privatize([d], UID + 1) == []
    assert mode(d) == 0o775


def test_children_and_parents_are_untouched_and_missing_is_skipped(tmp_path):
    parent = tmp_path / "p"
    child = parent / "c"
    child.mkdir(parents=True)
    os.chmod(parent, 0o775)
    os.chmod(child, 0o775)
    assert privdir.privatize([child, tmp_path / "missing"], UID) == [child]
    assert mode(parent) == 0o775 and mode(child) == 0o755


def test_a_file_is_skipped(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(f, 0o666)
    assert privdir.privatize([f], UID) == []
    assert mode(f) == 0o666


def test_real_ports_touch_only_the_exact_setup_paths(tmp_path):
    from orch.setup.steps import RealPorts

    home = tmp_path / "home"
    root = home / "proj"
    paths = [
        root,
        root / ".orch",
        home / ".config",
        home / ".config" / "orch",
        home / ".local",
        home / ".local" / "share" / "orch",
    ]
    other = [home / ".config" / "other", root / "src"]
    for p in [*paths, *other]:
        p.mkdir(parents=True)
        os.chmod(p, 0o775)
    os.chmod(home, 0o775)
    RealPorts({"HOME": str(home)}, home / ".config" / "orch").privatize(root)
    assert all(mode(p) == 0o755 for p in paths)
    assert all(mode(p) == 0o775 for p in [*other, home])
