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
