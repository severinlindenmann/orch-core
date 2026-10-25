"""The versioned, immutable release the service runs (D-P2-4).

``orch host install`` copies the ``orch`` package to ``~/.local/share/orch/releases/<version>/`` (``$XDG_DATA_HOME``
when set), with the launchers ``bin/orch-host`` and ``bin/orch-runner`` (H11a) and a manifest ``RELEASE.json`` of
every file's SHA-256. Files are made read-only (0444, directories 0555). A version that is already installed is never
changed: installing the same bytes again is a no-op, different bytes under the same version are refused. An upgrade
is a new version, then ``host.restart`` (or the service manager's restart).

At start the host imports every ``orch`` module (:func:`preload`), so a later change on disk cannot change code the
running host has not loaded yet. ``releases/current`` points at the installed version.

Known limit: only ``orch/`` is copied. The dependencies (``jsonschema``, ``cryptography``) come from the
interpreter's environment, and ``python -I`` still runs that environment's ``site-packages/*.pth`` files, so the
interpreter's environment is trusted like any other code of the host UID (same-UID processes are trusted, §7).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib
import json
import os
import pkgutil
import secrets
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "ReleaseError",
    "code_digest",
    "default_version",
    "install_release",
    "preload",
    "purge_releases",
    "releases_dir",
]

LAUNCHER = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orch.host.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
"""

#: the runner's launcher (H11a, D-P2-24): the same release, its own entry point
RUNNER_LAUNCHER = LAUNCHER.replace("from orch.host.cli import main", "from orch.runner.cli import main")


class ReleaseError(Exception):
    pass


def releases_dir(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    data = env.get("XDG_DATA_HOME", "")
    base = Path(data) if data.startswith("/") else Path(env.get("HOME") or Path.home()) / ".local" / "share"
    return base / "orch" / "releases"


def _package_dir() -> Path:
    import orch

    return Path(orch.__file__).resolve().parent


def _files(source: Path) -> list[Path]:
    out = []
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".pyo"} or path.is_symlink():
            continue
        if path.is_file():
            out.append(rel)
    return out


def _manifest(source: Path) -> dict[str, str]:
    return {str(rel): hashlib.sha256((source / rel).read_bytes()).hexdigest() for rel in _files(source)}


@functools.lru_cache(maxsize=1)
def code_digest() -> str:
    """12 hex of the digest of the running code tree (the tail of :func:`default_version`). A host reports it in its
    welcome; a CLI with another digest knows the host runs a different release (B5b)."""
    tree = json.dumps(_manifest(_package_dir()), sort_keys=True).encode()
    return hashlib.sha256(tree).hexdigest()[:12]


def default_version(source: Path | None = None, python: str | None = None) -> str:
    """The package version plus 12 hex of the tree digest, so two dev builds never share a release directory."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        base = version("orch-core")
    except PackageNotFoundError:
        base = "0"
    tree = json.dumps(
        {"files": _manifest(source or _package_dir()), "python": python or sys.executable}, sort_keys=True
    ).encode()
    # the interpreter is part of the identity: the launchers' shebang names it
    return f"{base}+{hashlib.sha256(tree).hexdigest()[:12]}"


def _freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink():  # a venv links to its interpreter: never change what a link points at
            continue
        os.chmod(path, 0o555 if path.is_dir() else (0o555 if path.parent.name == "bin" else 0o444))
    os.chmod(root, 0o555)


def install_release(
    version: str | None = None,
    *,
    target: Path | None = None,
    source: Path | None = None,
    python: str | None = None,
    lock: Path | None = None,
    prepare: Callable[[Path], None] | None = None,
) -> Path:
    """``python``: the interpreter the launchers name (default: this one). Tier B passes the one in the share.

    ``lock`` (tier B): the release's hash-pinned requirements file. Its bytes are copied to ``requirements.lock``, its
    digest goes into ``RELEASE.json`` and into the version, and ``prepare(staging)`` (which builds ``staging/venv``
    from it) runs BEFORE the release is frozen; the launchers then name ``<release>/venv/bin/python``."""
    source = source or _package_dir()
    python = python or sys.executable
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest() if lock is not None else None
    version = version or default_version(source, python + (lock_digest or ""))
    if not version or "/" in version or version.startswith("."):
        raise ReleaseError(f"invalid version {version!r}")
    if any(ch.isspace() for ch in python) or "\x00" in python:
        raise ReleaseError(f"the interpreter path {python!r} has whitespace: a launcher cannot name it")
    releases = target or releases_dir()
    final = releases / version
    manifest = _manifest(source)
    if final.exists():
        try:
            have = json.loads((final / "RELEASE.json").read_text())
            on_disk = _manifest(final / "orch")  # re-hash: the manifest alone could hide an edited file
        except (OSError, ValueError) as exc:
            raise ReleaseError(f"{final} exists but has no readable manifest") from exc
        if have.get("files") != manifest or on_disk != manifest:
            raise ReleaseError(f"release {version} is already installed with other content; use a new version")
        for name in ("orch-host", "orch-runner"):  # the service runs under the launcher's interpreter: it must be ours
            try:
                first = (final / "bin" / name).read_text().split("\n", 1)[0]
            except OSError as exc:
                raise ReleaseError(f"release {version} has no readable launcher {name}") from exc
            want_python = f"{final}/venv/bin/python" if lock is not None else python
            if first != f"#!{want_python} -I":
                raise ReleaseError(
                    f"release {version} was installed for another interpreter ({first[2:]!r}); use a new version"
                )
        _point_current(releases, version)
        return final
    releases.mkdir(parents=True, exist_ok=True, mode=0o755)
    staging = releases / f".staging-{secrets.token_hex(6)}"
    try:
        (staging / "orch").mkdir(parents=True, mode=0o755)
        for rel in manifest:
            dest = staging / "orch" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, dest)
        (staging / "bin").mkdir()
        launcher_python = python
        doc: dict[str, object] = {"version": version, "files": manifest}
        if lock is not None:
            if prepare is None:
                raise ReleaseError("a lock needs a step that builds the environment from it")
            shutil.copyfile(lock, staging / "requirements.lock")
            prepare(staging)
            launcher_python = f"{final}/venv/bin/python"
            doc["lock_sha256"] = lock_digest
        (staging / "bin" / "orch-host").write_text(f"#!{launcher_python} -I\n" + LAUNCHER)
        (staging / "bin" / "orch-runner").write_text(f"#!{launcher_python} -I\n" + RUNNER_LAUNCHER)
        (staging / "RELEASE.json").write_text(json.dumps(doc, sort_keys=True))
        _freeze(staging)
        os.rename(staging, final)  # atomic: a release appears complete or not at all
        _point_current(releases, version)
    except BaseException:
        with contextlib.suppress(OSError):
            for path in [staging, *staging.rglob("*")]:
                os.chmod(path, stat.S_IRWXU)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


def purge_releases(releases: Path) -> list[str]:
    """Remove the release copies under ``releases`` (``orch host disable --purge-releases``). They are read-only, so
    each directory gets ``u+rwx`` before its entries go. The walk is by file descriptor: every directory is opened
    relative to its parent with ``O_NOFOLLOW | O_DIRECTORY`` and checked to be this user's before it is changed, so no
    link is followed at any level (a link, ``current`` included, is unlinked itself) and nothing outside ``releases``
    is touched. ``releases`` being a symlink or a file, or a directory of another user, is refused. Returns the
    top-level entries removed."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        top = os.open(releases, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:  # ELOOP (a link), ENOTDIR (a file), EACCES
        raise ReleaseError(f"{releases} is not a directory of yours (a link or a file): nothing removed") from exc

    def clear(fd: int, where: str) -> list[str]:
        if os.fstat(fd).st_uid != os.geteuid():
            raise ReleaseError(f"{where} is owned by another user: nothing more removed")
        os.fchmod(fd, stat.S_IRWXU)  # on the open directory itself, never on a path
        with os.scandir(fd) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for name, is_dir in entries:
            if is_dir:
                try:
                    sub = os.open(name, flags, dir_fd=fd)
                except OSError as exc:  # swapped for a link or a file since the scan
                    raise ReleaseError(f"{where}/{name} changed while it was removed: nothing more removed") from exc
                try:
                    clear(sub, f"{where}/{name}")
                finally:
                    os.close(sub)
                os.rmdir(name, dir_fd=fd)
            else:  # a file or a symlink (never followed)
                os.unlink(name, dir_fd=fd)
        return [n for n, _ in entries]

    try:
        return sorted(clear(top, str(releases)))
    finally:
        os.close(top)


def _point_current(releases: Path, version: str) -> None:
    """``releases/current`` → ``<version>``, replaced atomically (the units run ``current/bin/orch-host``)."""
    link = releases / f".current-{secrets.token_hex(4)}"
    os.symlink(version, link)
    try:
        os.replace(link, releases / "current")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(link)
        raise


#: modules that only import on another platform
_SKIP = ("orch.custody.windows",)


def preload() -> list[str]:
    """Import every ``orch`` module now; returns their names."""
    import orch

    names = []
    for info in pkgutil.walk_packages(orch.__path__, "orch."):
        if info.name.startswith(_SKIP) or info.name.endswith(".__main__"):
            continue
        importlib.import_module(info.name)
        names.append(info.name)
    return names
