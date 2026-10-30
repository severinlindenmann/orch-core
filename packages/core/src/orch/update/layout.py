"""The A/B install layout, the install record and the download cache: the one place that knows where orch lives on disk.

THIS DOCSTRING IS THE CONTRACT FOR ``install.sh`` (task P2-C). install.sh, ``orch update`` and the services
all follow it;
nobody else invents a path. ``<data>`` is ``$XDG_DATA_HOME/orch`` when that is an absolute path, else
``~/.local/share/orch`` (the same place as the backups: :func:`orch.migrate.backup.backups_dir`). Everything below is
mode 0700 (files 0600), owned by the person who runs orch, and never in a synced folder.

::

    <data>/install.json                      the install record (below), atomic write, 0600
    <data>/install/tools/orch-<ver>/         one directory per installed version = that install's UV_TOOL_DIR
    <data>/install/tools/orch-<ver>/bin/orch the entry point of that version (its UV_TOOL_BIN_DIR is the bin/ above)
    <data>/install/current                   a RELATIVE symlink to tools/orch-<ver>; flipped atomically (rename over it)
    <data>/install/bin/orch                  the shim on the person's PATH: a symlink to ../current/bin/orch
    <data>/install/cache/<ver>/              the verified download: the files the manifest lists + release.json
    <data>/install/logs/update-<ts>.log      one log per ``orch update`` run
    <data>/install/update.lock               held (flock) by a running ``orch update``/``--use``

``<ver>`` matches ``\\d+\\.\\d+\\.\\d+(\\.dev\\d+)?`` (the manifest's version, checked by the verifier). Any
other string is
refused before it becomes a path component.

How install.sh installs a version V (after it verified the envelope and the files, B1):

1. ``mkdir -p <data>/install/tools/orch-V``; run ``uv tool install --python 3.14 --require-hashes --constraints
   constraints.txt <core wheel> --with <dashboard wheel> [--with <hub|relay|publish wheel> ...]`` with
   ``UV_TOOL_DIR=<data>/install/tools/orch-V`` and ``UV_TOOL_BIN_DIR=<data>/install/tools/orch-V/bin``, using ONLY the
   files the verified manifest lists, by their verified names.
2. Copy the verified files and the envelope to ``<data>/install/cache/V/`` (``release.json`` is the envelope).
3. Flip ``current`` to ``tools/orch-V`` (:func:`flip_current`: create ``current.<pid>.tmp`` as a relative symlink,
   ``rename`` it over ``current``) and make the shim (:func:`ensure_shim`).
4. Write the install record (:func:`write_record`) with ``seq_seen`` and ``revocations_issued_at`` taken from the
   verified manifest and revocation list. install.sh never leaves a record that has a lower ``seq_seen`` than it
   verified: ``orch update`` passes the stored values back to the verifier and refuses a lower ``seq`` (rollback).
5. Put ``<data>/install/bin`` on PATH (``uv tool update-shell`` is not used: it would point at uv's own bin dir).

The install record ``<data>/install.json`` (exact key set, ``format`` 1)::

    {"format": 1, "channel": "main", "version": "2.0.0", "previous": null | "<ver>",
     "seq_seen": {"main": 17}, "revocations_issued_at": "2026-10-20T00:00:00Z", "root_key_id": "root-1",
     "files": {"<name>": "<sha256>"}, "installed_at": 1761800000}

It is written only by install.sh and ``orch update`` (never from workspace files, ``ORCH_*`` variables or agent-writable
config). ``version`` is informational: the truth for "what is installed" is where ``current`` points; ``orch update``
repairs a record that disagrees with a committed journal. A damaged record is refused (fail closed), never guessed.

Retention: :func:`prune_tools` keeps the CURRENT version, the PREVIOUS one and one more (the newest other), and
:func:`prune_cache` keeps the two newest cache directories.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.migrate.steps import atomic_write
from orch.ops.errors import OrchError

__all__ = [
    "InstallLayout",
    "Record",
    "ensure_shim",
    "flip_current",
    "prune_cache",
    "prune_tools",
    "read_record",
    "version_key",
    "write_record",
]

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?")
_RECORD_KEYS = {
    "format", "channel", "version", "previous", "seq_seen", "revocations_issued_at", "root_key_id", "files",
    "installed_at",
}  # fmt: skip
_CHANNEL = re.compile(r"main|develop|test-[a-z0-9][a-z0-9-]{0,30}")
_LIMIT = 1 << 20
KEEP_TOOLS_EXTRA = 1
KEEP_CACHE = 2


def check_version(ver: object) -> str:
    if not isinstance(ver, str) or VERSION_RE.fullmatch(ver) is None:
        raise OrchError("update.unverified", "the release names a version that is not valid")
    return ver


def version_key(ver: str) -> tuple[int, int, int, int, int]:
    """Sort key: a ``.devN`` build sorts before the release it leads to (2.0.0.dev9 < 2.0.0)."""
    m = VERSION_RE.fullmatch(ver)
    if m is None:
        raise ValueError(f"not a version: {ver!r}")
    major, minor, patch, dev = m.groups()
    return int(major), int(minor), int(patch), 0 if dev is not None else 1, int(dev or 0)


def data_home(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_DATA_HOME", "")
    if xdg.startswith("/"):
        return Path(xdg) / "orch"
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "share" / "orch"


@dataclass(frozen=True)
class InstallLayout:
    data: Path

    @classmethod
    def of(cls, env: Mapping[str, str]) -> InstallLayout:
        return cls(data_home(env))

    @property
    def root(self) -> Path:
        return self.data / "install"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def shim(self) -> Path:
        return self.bin / "orch"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def lock(self) -> Path:
        return self.root / "update.lock"

    @property
    def record(self) -> Path:
        return self.data / "install.json"

    def tool_dir(self, ver: str) -> Path:
        return self.tools / f"orch-{check_version(ver)}"

    def tool_bin(self, ver: str) -> Path:
        return self.tool_dir(ver) / "bin" / "orch"

    def cache_dir(self, ver: str) -> Path:
        return self.cache / check_version(ver)

    def ensure(self) -> None:
        old = os.umask(0o077)
        try:
            for d in (self.data, self.root, self.tools, self.bin, self.cache, self.logs):
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
        finally:
            os.umask(old)
        os.chmod(self.root, 0o700)

    def current_version(self) -> str | None:
        """The version ``current`` points at, or None (no install, or a link that does not name a tools dir)."""
        try:
            target = os.readlink(self.current)
        except OSError:
            return None
        name = Path(target).name
        if Path(target) != Path("tools") / name or not name.startswith("orch-"):
            return None
        ver = name.removeprefix("orch-")
        return ver if VERSION_RE.fullmatch(ver) else None

    def installed_versions(self) -> list[str]:
        """Versions that have a tools directory, newest first."""
        out = []
        try:
            names = [e.name for e in os.scandir(self.tools) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []
        for n in names:
            v = n.removeprefix("orch-")
            if n.startswith("orch-") and VERSION_RE.fullmatch(v):
                out.append(v)
        return sorted(out, key=version_key, reverse=True)


def flip_current(layout: InstallLayout, ver: str) -> None:
    """Point ``current`` at ``tools/orch-<ver>`` in one atomic rename. The tools directory must exist."""
    if not layout.tool_dir(ver).is_dir():
        raise OrchError("update.unverified", f"version {ver} is not installed")
    tmp = layout.root / f"current.{os.getpid()}.tmp"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(Path("tools") / f"orch-{ver}", tmp)
    os.replace(tmp, layout.current)
    _fsync_dir(layout.root)


def ensure_shim(layout: InstallLayout) -> None:
    """``<data>/install/bin/orch`` -> ``../current/bin/orch`` (idempotent)."""
    layout.bin.mkdir(parents=True, exist_ok=True, mode=0o700)
    want = Path("..") / "current" / "bin" / "orch"
    try:
        if os.readlink(layout.shim) == str(want):
            return
    except OSError:
        pass
    tmp = layout.bin / f"orch.{os.getpid()}.tmp"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(want, tmp)
    os.replace(tmp, layout.shim)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------------------------- install record


@dataclass(frozen=True)
class Record:
    channel: str
    version: str
    previous: str | None
    seq_seen: dict[str, int]
    revocations_issued_at: str | None
    root_key_id: str
    files: dict[str, str]
    installed_at: int

    def seq(self, channel: str) -> int | None:
        return self.seq_seen.get(channel)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": 1, "channel": self.channel, "version": self.version, "previous": self.previous,
            "seq_seen": dict(self.seq_seen), "revocations_issued_at": self.revocations_issued_at,
            "root_key_id": self.root_key_id, "files": dict(self.files), "installed_at": self.installed_at,
        }  # fmt: skip


def _no_record(why: str) -> OrchError:
    return OrchError(
        "update.no_record", f"orch was not installed with the installer, or its install record is damaged ({why})"
    )


def read_record(layout: InstallLayout) -> Record:
    """The install record. Missing or damaged raises ``update.no_record`` (fail closed)."""
    try:
        with open(layout.record, "rb") as f:
            raw = f.read(_LIMIT + 1)
    except FileNotFoundError:
        raise _no_record("there is none") from None
    except OSError as exc:
        raise _no_record(exc.strerror or str(exc)) from None
    try:
        doc = json.loads(raw) if len(raw) <= _LIMIT else None
    except ValueError:
        doc = None
    try:
        if not isinstance(doc, dict) or set(doc) != _RECORD_KEYS or doc["format"] != 1:
            raise ValueError("keys")
        seq = doc["seq_seen"]
        files = doc["files"]
        if not (
            isinstance(seq, dict) and all(_CHANNEL.fullmatch(k) and type(v) is int and v >= 1 for k, v in seq.items())
        ):
            raise ValueError("seq_seen")
        if not (isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
            raise ValueError("files")
        rev = doc["revocations_issued_at"]
        prev = doc["previous"]
        if rev is not None and not isinstance(rev, str):
            raise ValueError("revocations_issued_at")
        if prev is not None:
            check_version(prev)
        if not _CHANNEL.fullmatch(str(doc["channel"])) or not isinstance(doc["root_key_id"], str):
            raise ValueError("channel")
        if type(doc["installed_at"]) is not int:
            raise ValueError("installed_at")
        return Record(
            doc["channel"], check_version(doc["version"]), prev, dict(seq), rev, doc["root_key_id"], dict(files),
            doc["installed_at"],
        )  # fmt: skip
    except (ValueError, KeyError, OrchError):
        raise _no_record("it does not have the expected shape") from None


def write_record(layout: InstallLayout, rec: Record) -> None:
    layout.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(layout.record, (json.dumps(rec.to_json(), indent=2, sort_keys=True) + "\n").encode(), 0o600)


def new_record(
    old: Record | None,
    *,
    channel: str,
    version: str,
    previous: str | None,
    seq: int,
    revocations_issued_at: str,
    root_key_id: str,
    files: Mapping[str, str],
) -> Record:
    """The record after a verified install: ``seq_seen`` only ever goes up (per channel)."""
    seen = dict(old.seq_seen) if old else {}
    seen[channel] = max(seen.get(channel, 0), seq)
    return Record(channel, version, previous, seen, revocations_issued_at, root_key_id, dict(files), int(time.time()))


# ---------------------------------------------------------------------------------------------------------- retention


def _rm_under(parent: Path, child: Path) -> None:
    """Remove ``child`` (a directory) only if it is a direct, non-symlink child of ``parent``."""
    if child.parent != parent or child.is_symlink() or not child.is_dir():
        return
    shutil.rmtree(child, ignore_errors=True)


def prune_tools(layout: InstallLayout, current: str, previous: str | None) -> list[str]:
    """Keep current, previous and one more (the newest other); remove the rest. Returns the versions removed."""
    keep = {current}
    if previous:
        keep.add(previous)
    others = [v for v in layout.installed_versions() if v not in keep]
    keep.update(others[:KEEP_TOOLS_EXTRA])
    gone = [v for v in layout.installed_versions() if v not in keep]
    for v in gone:
        _rm_under(layout.tools, layout.tool_dir(v))
    return gone


def prune_cache(layout: InstallLayout, keep: Iterable[str]) -> None:
    """Keep the cache of every version in ``keep`` plus the :data:`KEEP_CACHE` newest; remove the rest."""
    try:
        dirs = [e.name for e in os.scandir(layout.cache) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    versions = sorted((d for d in dirs if VERSION_RE.fullmatch(d)), key=version_key, reverse=True)
    keepset = set(keep) | set(versions[:KEEP_CACHE])
    for v in versions:
        if v not in keepset:
            _rm_under(layout.cache, layout.cache_dir(v))
