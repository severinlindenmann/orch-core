"""The rules file of a Claude Code agent that runs in a ticket work folder (run 6, #1066).

Such an agent is launched with ``--setting-sources user --strict-mcp-config`` (:func:`orch.host.launch.launch_argv`).
That also keeps Claude Code from loading the project's and its ancestors' ``CLAUDE.md`` files, so the ``@AGENTS.md`` and
``@.orch/AGENTS.orch.md`` imports that carry orch's rules never reach it. The rules arrive another way: the host puts
the placeholder :data:`PLACEHOLDER` after ``--append-system-prompt-file`` in the argv, and the runner (under the
person's uid) writes the file and swaps in its path.

* **Not in the work folder.** The file lives in the runner's own state folder (``<state>/runner/agent-rules/<run>.md``,
  0600, folder 0700), which nobody who can write the workspace controls. It is removed when the run exits.
* **Plain text, bounded.** It holds the workspace's ``AGENTS.orch.md`` and ``AGENTS.md``, read from the resolved
  workspace root without following a link, at most :data:`LIMIT` bytes together. ``@imports`` inside are not expanded:
  the agent gets them as text.
* **Strict placeholder.** It is accepted once, directly after the flag, and only with an absolute ``ORCH_WORKSPACE``.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from orch.layout import STORE_DIR

from .supervise import SpawnError

__all__ = ["FLAG", "LIMIT", "PLACEHOLDER", "build", "prepare", "remove"]

FLAG = "--append-system-prompt-file"
PLACEHOLDER = "@orch-rules"
LIMIT = 64 << 10
HEADER = (
    "These are the rules of the orch workspace this ticket belongs to, passed as plain text (AGENTS.orch.md first,"
    " then AGENTS.md). Imports written inside them are not followed.\n"
)


def _read(root: str, rel: str, room: int) -> str | None:
    from orch.instructions.harness import UnsafePath, safe_read

    try:
        text = safe_read(Path(root), rel)
    except (UnsafePath, OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    return text if len(text.encode()) <= room else text.encode()[:room].decode(errors="ignore") + "\n[cut: too long]\n"


def build(root: str) -> str:
    """The rules text for the workspace at ``root`` (resolved first)."""
    from orch.instructions.harness import AGENTS_FILE

    real = os.path.realpath(root)
    out = [HEADER]
    left = LIMIT - len(HEADER)
    wanted = (("AGENTS.orch.md", (os.path.join(STORE_DIR, AGENTS_FILE), AGENTS_FILE)), ("AGENTS.md", ("AGENTS.md",)))
    for title, rels in wanted:
        for rel in rels:
            text = _read(real, rel, max(left, 0))
            if text is not None:
                out.append(f"\n## {title}\n\n{text}")
                left -= len(text.encode())
                break
    return "".join(out)


def _dir(base: Path) -> Path:
    d = base / "agent-rules"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SpawnError("run.spawn_failed", "the runner's rules folder is not usable")
    os.chmod(d, 0o700)
    return d


def prepare(base: Path, run: str, argv: list[str], env: Mapping[str, str]) -> list[str]:
    """``argv`` with the placeholder replaced by the path of the rules file written for ``run``; ``argv`` itself when it
    has none. Raises :class:`SpawnError` for a misplaced placeholder or a file that cannot be written (the run then does
    not start: it would run without its rules)."""
    if PLACEHOLDER not in argv:
        return argv
    at = argv.index(PLACEHOLDER)
    if argv.count(PLACEHOLDER) != 1 or at == 0 or argv[at - 1] != FLAG:
        raise SpawnError("invalid.input", "spawn: the rules placeholder is misplaced")
    root = env.get("ORCH_WORKSPACE") or ""
    if not root.startswith("/"):
        raise SpawnError("invalid.input", "spawn: the rules need the workspace")
    try:
        path = _dir(base) / f"{run}.md"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(build(root))
    except OSError:
        raise SpawnError("run.spawn_failed", "the rules file for the run could not be written") from None
    return [*argv[:at], str(path), *argv[at + 1 :]]


def remove(base: Path, run: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(base / "agent-rules" / f"{run}.md")
