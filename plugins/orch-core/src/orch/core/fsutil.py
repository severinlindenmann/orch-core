from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def agent_source(ws, actor, path):
    """Refuse a file an AI Factory session hands orch to read (`--file`, `--body-file`, `orch artifact add <file>`,
    ...) unless it lies inside the workspace, reached without a symbolic link, and outside orch's config dir: orch
    copies what it reads into tickets and artifacts, and in a Dark factory such a command runs without a prompt, so it
    must not carry other files out. A factory session is one with the runner's trusted binding (the one the permission
    hook trusts). A human, and an agent outside the factory (whose commands the harness asks about as usual, and who
    attaches screenshots from /tmp), pass any file, as before.

    Fails closed for agents: a path that cannot be resolved (missing, a broken link, a loop) is refused for any agent,
    and so is every file while orch cannot tell whether the session is a factory session (a binding record that does
    not verify, or an error while looking: factory_sessions.session_state).

    Returns None (the caller reads the path as before) or, for a factory session, the file opened once, read-only and
    without following a link, checked on its descriptor (a regular file with one link, the very file the checked path
    names): the caller reads or copies from it and closes it, and never opens the path again."""
    if actor is None or getattr(actor, "is_human", False):
        return None
    from orch.errors import ValidationError

    def refuse(why: str):
        raise ValidationError(f"an agent cannot hand orch the file {path}: {why}",
                              hint="write the text into a file inside the workspace (for example under "
                                   "orchestrator/temporary) and pass that path")

    try:
        a = Path(os.path.abspath(os.path.expanduser(str(path))))
        r = a.resolve(strict=True)
        os.stat(r)
    except (OSError, RuntimeError, ValueError, TypeError):
        refuse("it cannot be resolved or read")
    from orch.core import factory_sessions
    state, _ = factory_sessions.session_state(ws, getattr(actor, "session", None))
    if state == "none":
        return None
    if state != "trusted":
        refuse("orch cannot tell whether this session is an AI Factory session (its binding does not verify)")
    from orch.core.ledger import base_dir
    try:
        root, cfg = Path(ws.root).resolve(), base_dir().resolve()
    except (OSError, RuntimeError):
        refuse("the workspace or orch's config dir cannot be resolved")
    if r != a:
        why = "its path goes through a symbolic link"
    elif root not in r.parents:
        why = "it lies outside the workspace"
    elif r == cfg or cfg in r.parents:
        why = "it lies in orch's config dir"
    else:
        return _open_checked(r, refuse)
    refuse(why)


def _open_checked(r: Path, refuse):
    """`r` opened once (no link followed, never blocking), as a binary file, when its descriptor is a regular file with
    exactly one link and the same file `r` named when it was checked; else refuse(). A hard link would make a file from
    anywhere on the volume look like a workspace file; a swap between the check and the open is caught by the identity."""
    try:
        before = os.stat(r, follow_symlinks=False)
        fd = os.open(r, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        refuse("it cannot be opened without following a link")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            refuse("it is not a regular file")
        if st.st_nlink != 1:
            refuse("it has more than one hard link, so it may be a file from outside the workspace")
        if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            refuse("it changed while orch checked it")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def read_regular_file(path, max_bytes: int) -> bytes | None:
    """The bytes of `path` if it is a regular file of at most `max_bytes`, else None (missing, unreadable, too
    large, or a FIFO/device: opened non-blocking, so it never hangs the caller). At most `max_bytes` are read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        chunks, left = [], max_bytes + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        return data if len(data) <= max_bytes else None
    except OSError:
        return None
    finally:
        os.close(fd)
