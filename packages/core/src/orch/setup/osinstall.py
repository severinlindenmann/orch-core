"""Install the root-owned copy that ``orch-os-setup`` runs from (H1 L2). Standard library only.

Run ONLY as ``/usr/bin/python3 -I -S <staging>/orch/setup/osinstall.py`` by the installer, as root, where ``<staging>``
is a root-owned 0700 directory that holds the verified release's ``orch/`` tree. Never through the release venv and
never as ``-m orch...``: that would import the code from whatever site-packages the release has, which the person can
change.

TODO(#1198, installer): the signed-manifest part belongs to the installer task (the ``curl | sh`` installer and
``orch update``): it must create the staging directory (root, 0700, under a root-only parent), download the release
archive and its signed manifest INTO it, verify the signature and every file's sha256 against the bytes in the staging
directory, and only then run this script from there. This file does not fetch or verify signatures; it refuses a
source that is not root-owned and sealed, re-checks the tree it is about to copy, and re-hashes the copy.

It copies the modules listed in ``oslauncher.TRUSTED_FILES`` to ``TRUSTED_ROOT`` (root-owned, 0755 directories, 0644
files) and writes the launcher as ``LAUNCHER_PATH`` (0755). An existing copy is replaced atomically.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:  # run as a script under ``python3 -I -S``: the staging tree is the path
    _root = Path(os.path.realpath(__file__)).parents[2]
    _info = os.lstat(_root)
    if _info.st_uid != os.geteuid() or stat.S_IMODE(_info.st_mode) & 0o077:
        sys.stderr.write(f"{_root} is not a private (0700) directory of the installing user. Nothing was changed.\n")
        raise SystemExit(2)
    _node = str(_root)
    while True:  # every parent too: a directory others can write to could swap the staging tree before the import
        _p = os.lstat(_node)
        if _p.st_uid not in (0, os.geteuid()) or _p.st_mode & 0o022:
            sys.stderr.write(f"{_node} is not root's or can be written by others. Nothing was changed.\n")
            raise SystemExit(2)
        if os.path.dirname(_node) == _node:
            break
        _node = os.path.dirname(_node)
    sys.path.insert(0, str(_root))

from orch.setup.oslauncher import (  # noqa: E402
    LAUNCHER_PATH,
    SHEBANG,
    TRUSTED_FILES,
    TRUSTED_ROOT,
    Untrusted,
    check_chain,
    check_tree,
)

INSTALLER_FILE = "orch/setup/osinstall.py"


class InstallRefused(Exception):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_source(source: Path, owner: int = 0) -> None:
    """The staging directory must be sealed (``owner``'s, no group or other access) and hold only what we copy."""
    info = os.lstat(source)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(f"{source} is not a plain directory")
    if info.st_uid != owner or stat.S_IMODE(info.st_mode) & 0o077:
        raise InstallRefused(f"{source} must be owned by root and mode 0700 (it is the verified staging directory)")
    try:
        check_tree(str(source), allowed=(*TRUSTED_FILES, INSTALLER_FILE), owner=(0, owner))
    except Untrusted as exc:
        raise InstallRefused(str(exc)) from exc


def install(source: Path, dest: str = TRUSTED_ROOT, *, owner: int = 0) -> str:
    """Copy from the sealed ``source`` (``check_source``) to ``dest`` and verify the copy. Returns the launcher."""
    check_source(source, owner)
    parent = os.path.dirname(dest)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    try:
        check_chain(parent, (0, owner))
    except Untrusted as exc:
        raise InstallRefused(f"{exc}; the trusted copy cannot live there") from exc
    launcher_text = SHEBANG + (source / "orch/setup/oslauncher.py").read_text(encoding="utf-8")
    want = {rel: _digest(source / rel) for rel in TRUSTED_FILES}
    want[os.path.basename(LAUNCHER_PATH)] = hashlib.sha256(launcher_text.encode()).hexdigest()
    staging = Path(f"{dest}.new-{secrets.token_hex(4)}")
    old = f"{dest}.old-{secrets.token_hex(4)}"
    try:
        for rel in TRUSTED_FILES:
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, target)
        launcher = staging / os.path.basename(LAUNCHER_PATH)
        launcher.write_text(launcher_text, encoding="utf-8")
        for dirpath, dirnames, filenames in os.walk(staging):
            for name in dirnames:
                os.chmod(Path(dirpath, name), 0o755)
            for name in filenames:
                os.chmod(Path(dirpath, name), 0o644)
        os.chmod(staging, 0o755)
        os.chmod(launcher, 0o755)
        if owner == 0 and os.geteuid() == 0:
            for dirpath, dirnames, filenames in os.walk(staging):
                for name in [".", *dirnames, *filenames]:
                    os.chown(Path(dirpath, name), 0, 0, follow_symlinks=False)
        got = {str(p.relative_to(staging)): _digest(p) for p in staging.rglob("*") if p.is_file()}
        if got != want:
            raise InstallRefused("the copy does not match the verified source; nothing was installed")
        had_old = os.path.lexists(dest)
        if had_old:
            os.rename(dest, old)
        try:
            os.rename(staging, dest)
        except OSError:
            if had_old:
                os.rename(old, dest)  # put the previous copy back
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return os.path.join(dest, os.path.basename(LAUNCHER_PATH))


def main() -> int:
    if os.geteuid() != 0:
        sys.stderr.write("This must run as root (the installer does). Nothing was changed.\n")
        return 2
    try:
        print(install(Path(os.path.realpath(__file__)).parents[2]))
    except (InstallRefused, OSError) as exc:
        sys.stderr.write(f"orch-os-setup was not installed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
