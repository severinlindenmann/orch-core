"""The person's side of the records tool: its state file, its log, its per-workspace lock (H11e part 2).

* ``<state>/records.json`` (0600): per workspace the validated tip per branch (``validated``), the tip last scanned
  clean of convenience events per branch (``clean``: ``{sha, scanner}``, #441) and the auto-mode
  setting (``auto``: ``{on, every_min, push, root?}``; ``root`` only for a direct-mode workspace, so the timer finds
  it). Written atomically (``mkstemp``, fsync, rename), read with ``O_NOFOLLOW``; another owner, a group or other
  readable mode or a corrupt file is ``records.unsafe_config``.
* ``<state>/person/records.jsonl`` (0600, 1 MiB × 4): one line per run start, commit, push result and refusal code;
  never file contents or credentials (a URL is stored redacted; URLs with credentials are refused before anyway).
* ``<state>/person/records-<ws>.lock``: one run per workspace at a time (``records.busy``, retryable).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import RecordsError

__all__ = ["LOG_FILES", "LOG_MAX", "PersonState", "RunLog", "run_lock"]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
LOG_MAX = 1024 * 1024
#: the log and its rotated copies (records.jsonl, .1, .2, .3)
LOG_FILES = 4
EVERY_MIN, EVERY_MAX = 5, 1440


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or st.st_uid != os.getuid():
        raise RecordsError("records.unsafe_config", "the records state directory is not a private directory")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


class PersonState:
    """``<state>/records.json``: validated tips and the auto setting."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _doc(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            return {"v": 1, "validated": {}, "auto": {}}
        except OSError:
            raise RecordsError("records.unsafe_config", "the records state file is not a regular file") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise RecordsError("records.unsafe_config", "the records state file is not private to you")
            try:
                doc = json.loads(fh.read(1 << 20) or b"{}")
            except ValueError:
                raise RecordsError("records.unsafe_config", "the records state file is corrupt") from None
        if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("validated"), dict):
            raise RecordsError("records.unsafe_config", "the records state file is corrupt")
        if not isinstance(doc.setdefault("auto", {}), dict):
            raise RecordsError("records.unsafe_config", "the records state file is corrupt")
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        _private_dir(self.path.parent)
        fd, tmp = tempfile.mkstemp(prefix=".records.", suffix=".tmp", dir=self.path.parent)  # 0600, O_EXCL
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # -- validated tips
    def get(self, workspace_id: str, branch: str) -> str | None:
        v = (self._doc()["validated"].get(workspace_id) or {}).get(branch)
        return v if isinstance(v, str) else None

    def set(self, workspace_id: str, branch: str, sha: str) -> None:
        doc = self._doc()
        doc["validated"].setdefault(workspace_id, {})[branch] = sha
        self._save(doc)

    # -- tips scanned clean of convenience events (#441): a later scan covers only the objects added since
    def clean(self, workspace_id: str, branch: str, scanner: int) -> str | None:
        """The tip last scanned clean, if it was scanned under the scanner rules ``scanner`` (another version: a full
        rescan)."""
        clean = self._doc().get("clean")
        v = ((clean if isinstance(clean, dict) else {}).get(workspace_id) or {}).get(branch)
        if not isinstance(v, dict) or v.get("scanner") != scanner or not isinstance(v.get("sha"), str):
            return None
        return v["sha"]

    def set_clean(self, workspace_id: str, branch: str, sha: str, scanner: int) -> None:
        doc = self._doc()
        if not isinstance(doc.get("clean"), dict):
            doc["clean"] = {}
        doc["clean"].setdefault(workspace_id, {})[branch] = {"sha": sha, "scanner": scanner}
        self._save(doc)

    # -- auto mode
    def auto(self, workspace_id: str) -> dict[str, Any] | None:
        entry = self._doc()["auto"].get(workspace_id)
        return dict(entry) if isinstance(entry, dict) and entry.get("on") is True else None

    def autos(self) -> dict[str, dict[str, Any]]:
        return {w: dict(e) for w, e in self._doc()["auto"].items() if isinstance(e, dict) and e.get("on") is True}

    def ran(self, workspace_id: str, at: float) -> None:
        """The timer's own clock (Opus #433 finding 1): when it last committed this workspace. The host's notes are
        never read for this decision."""
        doc = self._doc()
        entry = doc["auto"].get(workspace_id)
        if isinstance(entry, dict):
            entry["last_run"] = int(at)
            self._save(doc)

    def set_auto(self, workspace_id: str, entry: dict[str, Any] | None) -> None:
        doc = self._doc()
        if entry is None:
            doc["auto"].pop(workspace_id, None)
        else:
            every = entry.get("every_min")
            if type(every) is not int or not EVERY_MIN <= every <= EVERY_MAX:
                raise RecordsError("records.invalid", f"--every is {EVERY_MIN} to {EVERY_MAX} minutes")
            doc["auto"][workspace_id] = entry
        self._save(doc)


def rotate(path: Path, limit: int | None = None) -> None:
    """Shift ``path`` to ``path.1`` … (keeping :data:`LOG_FILES` files); with ``limit``, only when it is bigger."""
    if limit is not None:
        try:
            if os.lstat(path).st_size <= limit:
                return
        except FileNotFoundError:
            return
    for i in range(LOG_FILES - 1, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        with contextlib.suppress(FileNotFoundError):
            os.replace(src, path.with_name(f"{path.name}.{i}"))


class RunLog:
    """``<state>/person/records.jsonl``: append-only, rotated at :data:`LOG_MAX`, :data:`LOG_FILES` files."""

    def __init__(self, path: Path, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    def _rotate(self) -> None:
        rotate(self.path)

    def write(self, workspace_id: str, event: str, **fields: Any) -> None:
        """One line; never raises (the log is a record of the run, never a reason for it to fail)."""
        from .git import redact

        try:
            _private_dir(self.path.parent)
            entry: dict[str, Any] = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(self.clock()))),
                "ws": workspace_id,
                "event": event,
            }
            for k, v in fields.items():
                if v is None:
                    continue
                entry[k] = redact(v)[:300] if isinstance(v, str) else v
            line = (json.dumps(entry, sort_keys=True) + "\n").encode()
            try:
                if os.lstat(self.path).st_size + len(line) > LOG_MAX:
                    self._rotate()
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o600)
            try:
                st = os.fstat(fd)
                if st.st_uid != os.getuid():
                    return
                if st.st_mode & 0o077:  # an existing log with a wider mode is tightened before it is written
                    os.fchmod(fd, 0o600)
                os.write(fd, line)
            finally:
                os.close(fd)
        except (OSError, RecordsError):
            pass


@contextlib.contextmanager
def run_lock(state: Path, workspace_id: str) -> Iterator[None]:
    """One records run per workspace at a time (``records.busy`` otherwise; it is retryable)."""
    d = _private_dir(Path(state) / "person")
    fd = os.open(d / f"records-{workspace_id}.lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecordsError("records.busy", "another records run for this workspace is in progress") from None
        yield
    finally:
        os.close(fd)
