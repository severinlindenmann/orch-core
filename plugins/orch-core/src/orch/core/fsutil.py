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


def agent_source(ws, actor, path) -> None:
    """Refuse a file an AI Factory session hands orch to read (`--file`, `--body-file`, `orch artifact add <file>`,
    ...) unless it lies inside the workspace, reached without a symbolic link, and outside orch's config dir: orch
    copies what it reads into tickets and artifacts, and in a Dark factory such a command runs without a prompt, so it
    must not carry other files out. A factory session is one with the runner's trusted binding (the one the permission
    hook trusts). A human, and an agent outside the factory (whose commands the harness asks about as usual, and who
    attaches screenshots from /tmp), pass any file, as before."""
    if actor is None or getattr(actor, "is_human", False) or not getattr(actor, "session", None):
        return
    from orch.core import factory_sessions
    if factory_sessions.trusted(ws, actor.session) is None:
        return
    from orch.core.ledger import base_dir
    from orch.errors import ValidationError
    a = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        r = a.resolve(strict=True)
        root, cfg = Path(ws.root).resolve(), base_dir().resolve()
    except (OSError, RuntimeError):
        r = root = cfg = None
    if r is None:
        why = "it cannot be read"
    elif r != a:
        why = "its path goes through a symbolic link"
    elif root not in r.parents:
        why = "it lies outside the workspace"
    elif r == cfg or cfg in r.parents:
        why = "it lies in orch's config dir"
    else:
        return
    raise ValidationError(f"an agent cannot hand orch the file {path}: {why}",
                          hint="write the text into a file inside the workspace (for example under "
                               "orchestrator/temporary) and pass that path")


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
