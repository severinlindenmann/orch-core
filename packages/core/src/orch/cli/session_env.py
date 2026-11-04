"""W28 (#950): a hand-started Claude Code session gets its own ``ORCH_SESSION`` at session start (the CLI side of
``orch instructions hook session-start``; the ops layer has no direct file access)."""

from __future__ import annotations

import re
from typing import Any

from orch.ops.errors import OrchError

_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def hand_session(env: Any, ws: Any, now: Any) -> str | None:
    """W28 (#950): a Claude Code started by hand has no ``ORCH_SESSION``. At its session start, inside a workspace this
    machine pinned, append ``export ORCH_SESSION=s_<ULID>`` to Claude Code's ``CLAUDE_ENV_FILE`` (it exports the file
    for every later command of that session) and return the id. Runs in the agent's own CLI process (never the host).
    Nothing when the session already has an id, outside a pinned workspace, or when the file is not an existing regular
    file (no symlink, never created here). A session id is a label, not a credential (the grant is)."""
    import os
    import stat

    from orch.identity.grants import new_ulid

    path = env.get("CLAUDE_ENV_FILE") or ""
    if env.get("ORCH_SESSION") or not os.path.isabs(path):
        return None
    # Opus W28 note (b): only a file in Claude Code's own per-session folder (<config>/session-env/<session>/<file>),
    # never a shell profile a hostile repository's settings could name
    if os.path.basename(os.path.dirname(os.path.dirname(path))) != "session-env":
        return None
    # Opus W28 delta: no ".." part and no symlinked folder on the way (O_NOFOLLOW guards only the last part)
    if ".." in path.split(os.sep) or os.path.realpath(path) != path:
        return None
    try:
        if ws.root is None:
            return None
        cfg = ws.config or {}
        wid = cfg.get("workspace", {}).get("id", "")
        if not isinstance(wid, str) or not _PLAIN_ID.fullmatch(wid):
            return None
        if not (ws.state_dir / "hosts" / wid / "genesis").is_file():
            return None
        # Opus W28 note (a): never block on a FIFO nobody reads, never adopt a terminal
        flags = os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
        fd = os.open(path, flags)
    except (OSError, ValueError, AttributeError, OrchError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        sid = "s_" + new_ulid(int(now() * 1000))
        os.write(fd, f"export ORCH_SESSION={sid}\n".encode())
        return sid
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
