"""H1 L2: the root-owned copy, its launcher and the descriptor-relative directory step (real files, no root needed)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from orch.setup import osapply, oslauncher
from orch.setup.osapply import DirSpec, secure_dir
from orch.setup.osinstall import install

ME = os.getuid()


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "os-setup"
    tmp_path.chmod(0o755)
    install(dest=str(dest))
    return dest


def test_install_copies_exactly_the_trusted_files_and_a_shebang_launcher(tmp_path) -> None:
    dest = _copy(tmp_path)
    found = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    assert found == {*oslauncher.TRUSTED_FILES, "orch-os-setup"}
    launcher = (dest / "orch-os-setup").read_text()
    assert launcher.startswith("#!/usr/bin/python3 -I -S\n")
    assert stat.S_IMODE((dest / "orch-os-setup").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "orch/setup/osapply.py").stat().st_mode) == 0o644
    oslauncher.check_tree(str(dest), owner=(0, ME))  # a fresh copy passes (owner = us instead of root here)


def test_the_copy_is_stdlib_only() -> None:
    import ast

    for rel in oslauncher.TRUSTED_FILES:
        tree = ast.parse((Path(oslauncher.__file__).parents[2] / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                tops = [(node.module or "").split(".")[0]]
            else:
                continue
            for top in tops:
                assert top in sys.stdlib_module_names or top == "orch", f"{rel} imports {top}"


def test_check_tree_refuses_an_extra_code_file(tmp_path) -> None:
    dest = _copy(tmp_path)
    (dest / "orch" / "setup" / "evil.py").write_text("x = 1\n")
    with pytest.raises(oslauncher.Untrusted, match="unexpected code files"):
        oslauncher.check_tree(str(dest), owner=(0, ME))


def test_check_tree_refuses_a_symlink_a_wide_mode_and_a_missing_file(tmp_path) -> None:
    dest = _copy(tmp_path)
    target = dest / "orch/setup/osapply.py"
    target.chmod(0o664)
    with pytest.raises(oslauncher.Untrusted, match="written by others"):
        oslauncher.check_tree(str(dest), owner=(0, ME))
    target.chmod(0o644)
    (dest / "orch/setup/link.txt").symlink_to("/etc/passwd")
    with pytest.raises(oslauncher.Untrusted, match="symlink"):
        oslauncher.check_tree(str(dest), owner=(0, ME))
    (dest / "orch/setup/link.txt").unlink()
    (dest / "orch/setup/osusers.py").unlink()
    with pytest.raises(oslauncher.Untrusted, match="missing"):
        oslauncher.check_tree(str(dest), owner=(0, ME))


def test_check_tree_refuses_files_not_owned_by_root(tmp_path) -> None:
    dest = _copy(tmp_path)
    if ME == 0:
        pytest.skip("running as root")
    with pytest.raises(oslauncher.Untrusted):
        oslauncher.check_tree(str(dest))  # owner defaults to root: our files fail


def test_a_writable_ancestor_is_refused(tmp_path) -> None:
    dest = _copy(tmp_path)
    tmp_path.chmod(0o775)
    try:
        with pytest.raises(oslauncher.Untrusted, match="written by others"):
            oslauncher.check_tree(str(dest), owner=(0, ME))
    finally:
        tmp_path.chmod(0o755)


def test_the_launcher_refuses_a_non_isolated_interpreter_and_an_untrusted_tree(tmp_path, capsys) -> None:
    with pytest.raises(oslauncher.Untrusted, match="-I -S"):
        oslauncher.check_interpreter()  # pytest runs without -I -S
    assert oslauncher.run(["--person", "x"], root=str(tmp_path)) == 2
    assert "will not run" in capsys.readouterr().err


def test_admin_command_never_names_the_release() -> None:
    from orch.setup.osusers import plan
    from tests.osfacts_fake import FakeOsFacts

    cmd = plan(FakeOsFacts(), "sev", 1000, release="/home/sev/.local/share/orch/releases/current").admin_command()
    assert cmd.startswith("sudo /usr/local/lib/orch/os-setup/orch-os-setup ")
    assert "/bin/orch-os-setup" not in cmd


# secure_dir: descriptor-relative, never follows ---------------------------------------------------------------------
def _spec(*chain: str, mode: int = 0o700, allowed: int = 0o700, fresh: bool = False, acl=None) -> DirSpec:
    return DirSpec(chain, ME, os.getgid(), mode, allowed, acl, acl, fresh)


def test_secure_dir_creates_and_then_leaves_alone(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir(mode=0o755)
    assert secure_dir(str(base), _spec("a"), base_uid=ME) == (True, None)
    assert secure_dir(str(base), _spec("a", "state"), base_uid=ME) == (True, None)
    assert stat.S_IMODE((base / "a/state").stat().st_mode) == 0o700
    assert secure_dir(str(base), _spec("a", "state"), base_uid=ME) == (False, None)


def test_secure_dir_does_not_follow_a_symlink_and_leaves_the_target_untouched(tmp_path) -> None:
    base = tmp_path / "base"
    (base / "a").mkdir(parents=True, mode=0o755)
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    (base / "a" / "repos").symlink_to(victim)
    for fresh in (False, True):
        changed, problem = secure_dir(
            str(base), _spec("a", "repos", mode=0o750, allowed=0o750, fresh=fresh), base_uid=ME
        )
        assert not changed and problem and "symlink" in problem
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755  # not chmodded through the link
    assert not list(victim.iterdir())


def test_secure_dir_refuses_a_symlinked_parent_component(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir(mode=0o755)
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    (base / "a").symlink_to(victim)
    changed, problem = secure_dir(str(base), _spec("a", "state"), base_uid=ME)
    assert not changed and problem
    assert not (victim / "state").exists()


def test_secure_dir_refuses_a_wider_existing_directory_and_never_loosens(tmp_path) -> None:
    base = tmp_path / "base"
    (base / "a").mkdir(parents=True, mode=0o755)
    (base / "a").chmod(0o755)
    changed, problem = secure_dir(str(base), _spec("a"), base_uid=ME)
    assert not changed and "more open" in problem
    assert stat.S_IMODE((base / "a").stat().st_mode) == 0o755  # untouched, not even tightened


def test_secure_dir_fresh_sets_the_exact_mode(tmp_path) -> None:
    base = tmp_path / "base"
    (base / "a").mkdir(parents=True, mode=0o755)
    assert secure_dir(str(base), _spec("a", fresh=True), base_uid=ME) == (True, None)
    assert stat.S_IMODE((base / "a").stat().st_mode) == 0o700


def test_secure_dir_refuses_a_base_others_can_write(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    base.chmod(0o777)
    changed, problem = secure_dir(str(base), _spec("a"), base_uid=ME)
    assert not changed and "written by others" in problem


def test_secure_dir_refuses_a_base_that_is_a_symlink(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    changed, problem = secure_dir(str(link), _spec("a"), base_uid=ME)
    assert not changed and problem and not (real / "a").exists()


def _acl_for(uid: int) -> bytes:
    o = osapply
    return o._acl(((o.USER_OBJ, 7), (o.USER, 1, uid), (o.GROUP_OBJ, 0), (o.MASK, 1), (o.OTHER, 0)))


def test_secure_dir_compares_exact_acl_entries(tmp_path) -> None:
    if not os.environ.get("CI") and not hasattr(os, "setxattr"):
        pytest.skip("no xattrs here")
    base = tmp_path / "base"
    base.mkdir(mode=0o755)
    want = _acl_for(4242)
    spec = _spec("a", allowed=0o710, acl=want)
    changed, problem = secure_dir(str(base), spec, base_uid=ME)
    if problem and "could not be set" in problem:
        if os.environ.get("CI"):
            pytest.fail(f"CI must support ACLs: {problem}")
        pytest.skip("filesystem without ACL support")
    assert problem is None and changed
    assert secure_dir(str(base), spec, base_uid=ME) == (False, None)
    other = _acl_for(4243)
    changed, problem = secure_dir(str(base), _spec("a", allowed=0o710, acl=other), base_uid=ME)
    assert problem and "other ACL entries" in problem
