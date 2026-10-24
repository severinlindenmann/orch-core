"""The trusted entry of ``orch-os-setup`` (H1 L2, spec 13.2). Standard library only.

Root must never run a file it does not own. The person's release lives in the person's home, so it is NOT what
root runs. The installer (as root) puts a root-owned copy of the few setup modules at ``TRUSTED_ROOT`` and this file as
``LAUNCHER_PATH`` (see ``osinstall``), with the shebang ``#!/usr/bin/python3 -I -S``: system interpreter, isolated, no
``site`` (so no ``.pth`` files and no site-packages). Before the first ``orch`` import this launcher checks that

- the interpreter and the standard library directory are root's and writable by root only, all the way up;
- everything under ``TRUSTED_ROOT`` and every directory above it is root's, no symlink, no group or other write;

and after the import that every ``orch`` module that was loaded comes from ``TRUSTED_ROOT``.
"""

from __future__ import annotations

import os
import stat
import sys

# /usr/lib, not /usr/local/lib: Debian ships /usr/local as 2775 root:staff, which this launcher must (and does) refuse.
TRUSTED_ROOT = "/usr/lib/orch/os-setup"
LAUNCHER_PATH = TRUSTED_ROOT + "/orch-os-setup"
SHEBANG = "#!/usr/bin/python3 -I -S\n"
# The only files the root process may import from orch (relative to TRUSTED_ROOT):
TRUSTED_FILES = (
    "orch/__init__.py",
    "orch/setup/__init__.py",
    "orch/setup/oslauncher.py",
    "orch/setup/osfacts.py",
    "orch/setup/osusers.py",
    "orch/setup/osapply.py",
)


class Untrusted(Exception):
    pass


def _root_only(path: str, info: os.stat_result, owners: tuple[int, ...] = (0,)) -> None:
    if info.st_uid not in owners or info.st_mode & 0o022:
        raise Untrusted(f"{path} is not owned by root or can be written by others")


def check_chain(path: str, owner: tuple[int, ...] = (0,)) -> None:
    """``path`` (resolved) and every directory above it: root's, no group or other write."""
    real = os.path.realpath(path)
    node = real
    while True:
        _root_only(node, os.lstat(node), owner)
        parent = os.path.dirname(node)
        if parent == node:
            return
        node = parent


def check_tree(root: str, allowed: tuple[str, ...] = TRUSTED_FILES, owner: tuple[int, ...] = (0,)) -> None:
    check_chain(root, owner)
    base = os.path.realpath(root)
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                raise Untrusted(f"{full} is a symlink")
            _root_only(full, info, owner)
            if not stat.S_ISDIR(info.st_mode):
                seen.add(os.path.relpath(full, base))
    extra = sorted(f for f in seen if f.endswith(".py") and f not in allowed and f != os.path.basename(LAUNCHER_PATH))
    if extra:
        raise Untrusted(f"unexpected code files in {root}: {', '.join(extra)}")
    for rel in allowed:
        if rel not in seen:
            raise Untrusted(f"{root} is missing {rel}")


def check_interpreter() -> None:
    if not (sys.flags.isolated and sys.flags.no_site):
        raise Untrusted("must run as 'python3 -I -S' (isolated, without site)")
    check_chain(sys.executable)
    check_chain(os.path.dirname(os.__file__))


def run(argv: list[str], root: str = TRUSTED_ROOT) -> int:
    try:
        check_interpreter()
        check_tree(root)
    except (Untrusted, OSError) as exc:
        sys.stderr.write(f"orch-os-setup will not run: {exc}. Nothing was changed.\n")
        return 2
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.realpath(root))
    from orch.setup import osapply  # noqa: PLC0415  (only after the checks)

    loaded = [m for n, m in sys.modules.items() if n == "orch" or n.startswith("orch.")]
    prefix = os.path.realpath(root) + os.sep
    for mod in loaded:
        if not os.path.realpath(getattr(mod, "__file__", "") or "").startswith(prefix):
            sys.stderr.write("orch-os-setup will not run: an orch module came from outside the trusted copy.\n")
            return 2
    return osapply.main(argv)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
