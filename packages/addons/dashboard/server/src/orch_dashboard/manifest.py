"""Export the packaged manifest without accessing host state or following workspace symlinks."""

import argparse
import os
import stat
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path


def manifest_bytes():
    return files("orch_dashboard").joinpath("orch-addon.json").read_bytes()


def write_manifest(workspace, data, force=False):
    """Write the manifest into the add-on folder of the workspace's store: ``<project>/.orch/addons/dashboard``. The
    store must exist (an old-version store in the project root is not one); nothing is created above ``addons``."""
    # Walk each component with pinned directory descriptors, including workspace ancestors.
    path = Path(os.path.abspath(workspace))
    with ExitStack() as stack:

        def directory(name, parent=None):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        fd = directory(path.anchor)
        for part in path.parts[1:]:
            fd = directory(part, fd)
        try:
            fd = directory(".orch", fd)
        except FileNotFoundError:
            raise ValueError("no orch store here (.orch is missing); run orch setup first") from None
        for part in ("addons", "dashboard"):
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            fd = directory(part, fd)
        name = "orch-addon.json"
        try:
            existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("manifest must be a regular file")
                if stream.read() == data:
                    return path / ".orch/addons/dashboard" / name
            if not force:
                raise ValueError("manifest differs; use --force to replace it")
        # A new inode avoids modifying hard-linked files; link() makes no-force creation exclusive.
        import secrets

        temporary = f".manifest-{secrets.token_hex(16)}"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(data)
            if force:
                # Refuse a symlink even with --force. replace itself never follows the leaf.
                try:
                    mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(mode):
                        raise ValueError("manifest must be a regular file, not a symlink")
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
    return path / ".orch/addons/dashboard" / name


def main(argv):
    parser = argparse.ArgumentParser(prog="orch-dashboard addon-manifest")
    parser.add_argument("--write", metavar="WORKSPACE")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.force and not args.write:
        parser.error("--force requires --write")
    try:
        data = manifest_bytes()
        if args.write:
            print(write_manifest(args.write, data, args.force))
        else:
            print(data.decode("utf-8"), end="")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
