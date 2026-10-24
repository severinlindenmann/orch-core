"""The click signer's private log (orch-v2-host §0.8.6 item 5): ``<person state>/person/click.jsonl`` (0600, rotated at
1 MiB, 4 files, symlink targets refused). It distinguishes review generation, signature production, submit attempt,
confirmed commit, refusal and uncertain outcome, with validated identifiers and static codes only: never key material,
a payload text or a secret. It is diagnostic evidence, not proof of human approval or a tamper-resistant audit trail
against the person's UID."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

__all__ = ["ClickLog", "WHAT"]

NAME = "click.jsonl"
MAX_BYTES = 1024 * 1024
KEEP = 4
WHAT = frozenset(
    {
        "prepared",
        "review",
        "signed",
        "submit",
        "committed",
        "refused",
        "uncertain",
        "cancelled",
        "adopted",
        "record_failed",
        "deposit",  # v1.6: a deposit grant's hand-over to the runner (the outcome code only, never a secret)
        "custody_on",  # §0.8.6 item 3a: the dashboard switched click-to-sign on (the workspace id, never a passphrase)
        "custody_off",  # and off
    }
)
_SAFE = {
    "workspace": re.compile(r"[0-9a-f]{32}"),
    "request": re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}"),
    "op": re.compile(r"[a-z][a-z_]*(\.[a-z_]+)*"),
    "code": re.compile(r"[a-z][a-z0-9_.]{0,79}"),
    "moved": re.compile(r"true"),  # #1198 C12: custody_off moved the key under a new passphrase (never the passphrase)
}


class ClickLog:
    def __init__(self, state_dir: Path, *, clock: Any = time.time) -> None:
        from orch.custody.convenience import person_root

        self.root = person_root(state_dir)
        self.clock = clock

    def _dir(self) -> int:
        from orch.custody.convenience import secure_dir

        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the person's state directory
        return secure_dir(self.root, (), create=True)

    def write(self, what: str, **fields: Any) -> None:
        if what not in WHAT:
            raise ValueError(what)
        line: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "what": what}
        for k, v in fields.items():
            pattern = _SAFE.get(k)
            if pattern is None or type(v) is not str or not pattern.fullmatch(v):
                continue  # only validated identifiers and static codes are written
            line[k] = v
        data = (json.dumps(line, sort_keys=True) + "\n").encode("ascii")
        try:
            fd = self._dir()
        except Exception:  # noqa: BLE001 - the log never stops a signature
            return
        try:
            self._rotate(fd)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            f = os.open(NAME, flags, 0o600, dir_fd=fd)
            try:
                st = os.fstat(f)
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077:
                    os.write(f, data)
            finally:
                os.close(f)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _rotate(fd: int) -> None:
        try:
            st = os.stat(NAME, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            os.unlink(NAME, dir_fd=fd)  # a symlink target is never written through
            return
        if st.st_size < MAX_BYTES:
            return
        for i in range(KEEP - 1, 0, -1):
            src = NAME if i == 1 else f"{NAME}.{i - 1}"
            try:
                os.rename(src, f"{NAME}.{i}", src_dir_fd=fd, dst_dir_fd=fd)
            except FileNotFoundError:
                pass
