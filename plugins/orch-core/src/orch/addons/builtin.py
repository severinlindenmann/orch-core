"""Manifests of the addons whose host code ships inside orch-core (start-agent, terminals, worktrees), so they install
without a clone: ``orch addon manifest <name> --write .`` puts ``addons/<name>/orch-addon.json`` into a workspace, then
``orch addon grant <name>`` (B5b). The package copies in ``manifests/`` equal ``addons/<name>/orch-addon.json`` of the
repo (a test pins that)."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from typing import TextIO

NAMES = ("start-agent", "terminals", "worktrees", "dashboard")


def manifest_bytes(name: str) -> bytes:
    if name not in NAMES:
        raise ValueError(f"no built-in manifest {name!r} (have: {', '.join(NAMES)})")
    return files("orch.addons").joinpath("manifests", f"{name}.json").read_bytes()


def write_manifest(workspace: str, name: str, data: bytes, force: bool = False) -> Path:
    """Write ``<workspace>/addons/<name>/orch-addon.json`` walking every component with ``O_NOFOLLOW``."""
    path = Path(os.path.abspath(workspace))
    with ExitStack() as stack:

        def directory(part: str, parent: int | None = None) -> int:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        fd = directory(path.anchor)
        for part in path.parts[1:]:
            fd = directory(part, fd)
        for part in ("addons", name):
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            fd = directory(part, fd)
        leaf = "orch-addon.json"
        try:
            existing = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("manifest must be a regular file")
                if stream.read() == data:
                    return path / "addons" / name / leaf
            if not force:
                raise ValueError("manifest differs; use --force to replace it")
        tmp = f".manifest-{secrets.token_hex(16)}"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(data)
            if force:
                try:
                    mode = os.stat(leaf, dir_fd=fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(mode):
                        raise ValueError("manifest must be a regular file, not a symlink")
                os.replace(tmp, leaf, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                os.link(tmp, leaf, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        finally:
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass
    return path / "addons" / name / leaf


def main(argv: list[str], *, out: TextIO, err: TextIO) -> int:
    parser = argparse.ArgumentParser(prog="orch addon manifest", exit_on_error=False)
    parser.add_argument("name", choices=NAMES)
    parser.add_argument("--write", metavar="WORKSPACE")
    parser.add_argument("--force", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.force and not args.write:
            raise argparse.ArgumentError(None, "--force requires --write")
        data = manifest_bytes(args.name)
        if args.write:
            out.write(f"{write_manifest(args.write, args.name, data, args.force)}\n")
        else:
            out.write(data.decode("utf-8"))
    except (argparse.ArgumentError, OSError, ValueError) as exc:
        err.write(f"orch addon manifest: {exc}\n")
        return 2
    return 0


def install_missing(workspace: str, name: str) -> None:
    """Write a built-in add-on's own manifest into ``<workspace>/addons/<name>``. On failure the empty directory this
    attempt made is removed (a package already there is never touched) and the error is raised."""
    directory = Path(os.path.abspath(workspace)) / "addons" / name
    try:
        write_manifest(workspace, name, manifest_bytes(name))
    except (OSError, ValueError):
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise
