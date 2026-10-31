"""The A/B install layout, the install record and the download cache: the one place that knows where orch lives on disk.

THIS DOCSTRING IS THE CONTRACT with ``install.sh`` (P2-C builds exactly this). ``<state>`` is the host state dir
(:func:`orch.ops.runtime.state_dir`: ``ORCH_STATE_DIR``, else ``$XDG_CONFIG_HOME/orch``, else ``~/.config/orch``).
``<data>`` is ``$XDG_DATA_HOME/orch`` when that is an absolute path, else ``~/.local/share/orch`` (where the backups
are). Directories are 0700, files 0600, never in a synced folder.

::

    <state>/install/tools/orch-<ver>/venv/        one environment per version (A/B); ``venv/bin/orch`` is its entry point
    <state>/install/tools/orch-<ver>/.complete    written LAST: a version without it is not installed
    <state>/install/current                       symlink ``tools/orch-<ver>`` (relative), flipped by rename
    ~/.local/bin/orch                             one-time symlink to <state>/install/current/venv/bin/orch; never changes
    <state>/install/update.json                   the update journal (orch.migrate.journal)
    <data>/install.json                           the install record (below), 0600, ``format`` ``orch.install/1``
    <data>/uv/uv-<uvver>/uv                       the pinned uv that install.sh fetched (the record names it)
    <data>/cache/<ver>/                           verified download of a release: its files + ``release.json`` (the envelope)
    <data>/logs/update-<ts>-<pid>.log             one log per ``orch update`` run
    <data>/update.lock                            held (flock) by a running ``orch update``

``<state>/install/tools`` is also where ``orch install hub|relay|publish`` keep their own tools; the update backup leaves
it out and a restore never touches it (:mod:`orch.migrate.backup`). Only ``orch-<ver>`` directories belong to this module.
``<ver>`` matches ``\\d+\\.\\d+\\.\\d+(\\.dev\\d+)?``; any other string is refused before it becomes a path component.

There is NO ``uv tool install``. A version is installed with the pinned uv, from the verified local files only::

    uv venv --no-config --python <3.14> <state>/install/tools/orch-V/venv
    uv pip install --no-config --require-hashes --no-deps --python <venv>/bin/python -r requirements.txt

where ``requirements.txt`` is the signed ``constraints`` file's lines plus one ``file:///<abs path> --hash=sha256:<hex>``
line per manifest file that is not the constraints file (:func:`requirements_text`). Then ``venv/bin/orch --version`` must
run, and only then ``.complete`` is written. install.sh then flips ``current`` (:func:`flip_current`), makes the PATH
link once, and writes the install record.

The install record ``<data>/install.json`` (exact key set)::

    {"format": "orch.install/1", "channel": "main", "version": "2.0.0", "seq_seen": {"main": 17},
     "revocations_issued_at": "2026-10-20T00:00:00Z", "min_serial": 2, "root_key_id": "root-1",
     "release_key_id": "rk-3", "trusted_roots": ["root-1"], "installed_at": "2026-10-30T10:00:00Z",
     "tool_dir": "<state>/install/tools/orch-V", "uv": {"path": "...", "version": "0.13.3"},
     "files": [{"role": "core", "name": "...", "sha256": "...", "size": 1}]}

Written only by install.sh and ``orch update``, never from workspace files, ``ORCH_*`` variables or agent-writable config.
``seq_seen[channel]`` only goes up; ``orch update`` passes the stored ``seq_seen``, ``revocations_issued_at`` and
``min_serial`` to the verifier and refuses anything lower. ``version`` is informational: what is installed is where
``current`` points, and ``orch update`` repairs a record that disagrees with a committed journal. A damaged record is
refused (fail closed), never guessed.

Retention: :func:`prune_tools` keeps the CURRENT version and the two newest others (the previous and one more);
:func:`prune_cache` keeps the two newest cache directories.
"""

# ruff: noqa: E501  (the contract tables in the module docstring are wider than the line limit)

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
    "RECORD_FORMAT",
    "InstallLayout",
    "Record",
    "flip_current",
    "new_record",
    "prune_cache",
    "prune_tools",
    "read_record",
    "requirements_text",
    "version_key",
    "write_record",
]

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?")
RECORD_FORMAT = "orch.install/1"
_RECORD_KEYS = {
    "format", "channel", "version", "seq_seen", "revocations_issued_at", "min_serial", "root_key_id", "release_key_id",
    "trusted_roots", "installed_at", "tool_dir", "uv", "files",
}  # fmt: skip
_CHANNEL = re.compile(r"main|develop|test-[a-z0-9][a-z0-9-]{0,30}")
_LIMIT = 1 << 20
KEEP_TOOLS_EXTRA = 2
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
    state: Path
    data: Path

    @classmethod
    def of(cls, env: Mapping[str, str]) -> InstallLayout:
        from orch.ops.runtime import state_dir

        return cls(state_dir(env), data_home(env))

    @property
    def root(self) -> Path:
        return self.state / "install"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def lock(self) -> Path:
        return self.data / "update.lock"

    @property
    def record(self) -> Path:
        return self.data / "install.json"

    def tool_dir(self, ver: str) -> Path:
        return self.tools / f"orch-{check_version(ver)}"

    def tool_bin(self, ver: str) -> Path:
        return self.tool_dir(ver) / "venv" / "bin" / "orch"

    def is_complete(self, ver: str) -> bool:
        return (self.tool_dir(ver) / ".complete").is_file()

    def cache_dir(self, ver: str) -> Path:
        return self.cache / check_version(ver)

    def ensure(self) -> None:
        old = os.umask(0o077)
        try:
            for d in (self.state, self.root, self.tools, self.data, self.cache, self.logs):
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
        finally:
            os.umask(old)

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
        """Complete versions (``.complete`` present), newest first."""
        out = []
        try:
            names = [e.name for e in os.scandir(self.tools) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []
        for n in names:
            v = n.removeprefix("orch-")
            if n.startswith("orch-") and VERSION_RE.fullmatch(v) and self.is_complete(v):
                out.append(v)
        return sorted(out, key=version_key, reverse=True)


def flip_current(layout: InstallLayout, ver: str) -> None:
    """Point ``current`` at ``tools/orch-<ver>`` in one atomic rename. The version must be complete."""
    if not layout.is_complete(ver):
        raise OrchError("update.unverified", f"version {ver} is not installed")
    tmp = layout.root / f"current.new-{os.getpid()}"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(Path("tools") / f"orch-{ver}", tmp)
    os.replace(tmp, layout.current)
    _fsync_dir(layout.root)


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


def requirements_text(constraints: str, files: Iterable[tuple[str, Path, str]]) -> str:
    """The ``requirements.txt`` install.sh feeds ``uv pip install --require-hashes``: the signed constraints, then one
    hash-pinned ``file://`` line per wheel. ``files`` is ``(role, path, sha256)``; the constraints role is not listed."""
    lines = [constraints.rstrip("\n")]
    for role, path, sha in files:
        if role != "constraints":
            lines.append(f"{Path(os.path.abspath(path)).as_uri()} --hash=sha256:{sha}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- install record


@dataclass(frozen=True)
class Record:
    channel: str
    version: str
    seq_seen: dict[str, int]
    revocations_issued_at: str | None
    min_serial: int
    root_key_id: str
    release_key_id: str
    trusted_roots: list[str]
    installed_at: str
    tool_dir: str
    uv: dict[str, str]
    files: list[dict[str, Any]]

    def seq(self, channel: str) -> int | None:
        return self.seq_seen.get(channel)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": RECORD_FORMAT, "channel": self.channel, "version": self.version, "seq_seen": dict(self.seq_seen),
            "revocations_issued_at": self.revocations_issued_at, "min_serial": self.min_serial,
            "root_key_id": self.root_key_id, "release_key_id": self.release_key_id,
            "trusted_roots": list(self.trusted_roots), "installed_at": self.installed_at, "tool_dir": self.tool_dir,
            "uv": dict(self.uv), "files": [dict(f) for f in self.files],
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
        if not isinstance(doc, dict) or set(doc) != _RECORD_KEYS or doc["format"] != RECORD_FORMAT:
            raise ValueError("keys")
        seq = doc["seq_seen"]
        if not (
            isinstance(seq, dict) and all(_CHANNEL.fullmatch(k) and type(v) is int and v >= 1 for k, v in seq.items())
        ):
            raise ValueError("seq_seen")
        rev = doc["revocations_issued_at"]
        if rev is not None and not isinstance(rev, str):
            raise ValueError("revocations_issued_at")
        if type(doc["min_serial"]) is not int or doc["min_serial"] < 1:
            raise ValueError("min_serial")
        for k in ("root_key_id", "release_key_id", "installed_at", "tool_dir"):
            if not isinstance(doc[k], str):
                raise ValueError(k)
        roots = doc["trusted_roots"]
        uv = doc["uv"]
        files = doc["files"]
        if not (isinstance(roots, list) and all(isinstance(r, str) for r in roots)):
            raise ValueError("trusted_roots")
        if not (isinstance(uv, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in uv.items())):
            raise ValueError("uv")
        if not (isinstance(files, list) and all(isinstance(f, dict) for f in files)):
            raise ValueError("files")
        if not _CHANNEL.fullmatch(str(doc["channel"])):
            raise ValueError("channel")
        return Record(
            doc["channel"], check_version(doc["version"]), dict(seq), rev, doc["min_serial"], doc["root_key_id"],
            doc["release_key_id"], list(roots), doc["installed_at"], doc["tool_dir"], dict(uv), list(files),
        )  # fmt: skip
    except (ValueError, KeyError, OrchError):
        raise _no_record("it does not have the expected shape") from None


def write_record(layout: InstallLayout, rec: Record) -> None:
    layout.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(layout.record, (json.dumps(rec.to_json(), indent=1, sort_keys=True) + "\n").encode(), 0o600)


def new_record(
    old: Record,
    *,
    channel: str,
    version: str,
    seq: int,
    revocations_issued_at: str,
    min_serial: int,
    root_key_id: str,
    release_key_id: str,
    trusted_roots: list[str],
    tool_dir: str,
    files: list[dict[str, Any]],
) -> Record:
    """The record after a verified update: ``seq_seen`` only goes up per channel, the revocation state never goes down,
    and the uv install.sh pinned is carried over."""
    seen = dict(old.seq_seen)
    seen[channel] = max(seen.get(channel, 0), seq)
    return Record(
        channel, version, seen, revocations_issued_at, max(min_serial, old.min_serial), root_key_id, release_key_id,
        trusted_roots, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), tool_dir, dict(old.uv), files,
    )  # fmt: skip


# ---------------------------------------------------------------------------------------------------------- retention


def _rm_under(parent: Path, child: Path) -> None:
    """Remove ``child`` (a directory) only if it is a direct, non-symlink child of ``parent``."""
    if child.parent != parent or child.is_symlink() or not child.is_dir():
        return
    shutil.rmtree(child, ignore_errors=True)


def prune_tools(layout: InstallLayout, current: str) -> list[str]:
    """Keep ``current`` and the two newest other complete versions; remove the rest. Returns the versions removed."""
    versions = layout.installed_versions()
    keep = {current, *[v for v in versions if v != current][:KEEP_TOOLS_EXTRA]}
    gone = [v for v in versions if v not in keep]
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
