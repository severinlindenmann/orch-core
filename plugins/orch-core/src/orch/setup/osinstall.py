"""Install the root-owned copy that ``orch-os-setup`` runs from (H1 L2). Standard library only.

Called by the installer when it is root, from the release it has just downloaded and verified (not from a tree that
anyone else could have changed): ``python3 -I -m orch.setup.osinstall``. It copies the setup modules listed in
``oslauncher.TRUSTED_FILES`` to ``TRUSTED_ROOT`` (root-owned, 0755 directories and 0644 files) and writes the launcher
as ``LAUNCHER_PATH`` (0755). An existing copy is replaced atomically.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

from .oslauncher import LAUNCHER_PATH, SHEBANG, TRUSTED_FILES, TRUSTED_ROOT


def install(source_root: Path | None = None, dest: str = TRUSTED_ROOT) -> str:
    """``source_root``: the directory that contains the ``orch`` package. Returns the launcher path."""
    src = source_root or Path(__file__).resolve().parents[2]
    parent = os.path.dirname(dest)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    staging = f"{dest}.new-{secrets.token_hex(4)}"
    try:
        for rel in TRUSTED_FILES:
            target = Path(staging, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / rel, target)
        launcher = Path(staging, os.path.basename(LAUNCHER_PATH))
        launcher.write_text(SHEBANG + (src / "orch/setup/oslauncher.py").read_text(encoding="utf-8"), encoding="utf-8")
        for dirpath, dirnames, filenames in os.walk(staging):
            for name in dirnames:
                os.chmod(Path(dirpath, name), 0o755)
            for name in filenames:
                os.chmod(Path(dirpath, name), 0o644)
        os.chmod(staging, 0o755)
        os.chmod(launcher, 0o755)
        if os.geteuid() == 0:
            for dirpath, dirnames, filenames in os.walk(staging):
                for name in [".", *dirnames, *filenames]:
                    os.chown(Path(dirpath, name), 0, 0, follow_symlinks=False)
        old = f"{dest}.old-{secrets.token_hex(4)}"
        if os.path.lexists(dest):
            os.rename(dest, old)
        os.rename(staging, dest)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return os.path.join(dest, os.path.basename(LAUNCHER_PATH))


def main() -> int:
    if os.geteuid() != 0:
        sys.stderr.write("This must run as root (the installer does). Nothing was changed.\n")
        return 2
    print(install())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
