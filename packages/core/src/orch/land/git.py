"""Landing's git layer (#596 task 2): the host-owned bare repository, the object import, the deterministic candidate.

This module is the only code that decides **which commit a landing would push**. It never pushes or fetches (that is
:mod:`orch.land.remote`, over ``land_push``/``land_fetch``) and never writes a ref an agent or a person owns.

* **Host-owned bare repository (I6).** Every git process runs through :mod:`orch.gitsafe` (``land_*`` operations of
  :data:`orch.gitsafe.OPS`, a closed set: full object ids only, no free argv; the gate plus
  :func:`orch.gitsafe.land_check` before each run: no alternates, no shallow file, no grafts, no hook, include,
  filter or merge-driver configuration, ``isolated_env``).
* **Objects come in through gitread (F5).** The source branch lives in a repository an agent can write. No git process
  ever runs there (#565): :func:`orch.gitread` reads commits, trees and blobs hash-checked, and this module writes them
  as loose objects into the bare repository, children before parents, so "an object exists" always means "its whole
  closure exists". Shallow sources, ``alternates``, SHA-256 repositories, gitlinks (submodules) and tree entries git
  itself would refuse to check out (``.git`` in any spelling, a symlinked ``.gitmodules``) are refused.
* **Deterministic candidate (I4).** :meth:`LandRepo.candidate` computes the one commit to check and push from
  ``(target_sha, source_sha, when, message)`` only: a fast-forward is the source itself, otherwise a merge commit with
  the target as **first parent**, the source as second, a fixed committer identity and the date of the attempt record.
  The same inputs give the same sha on any machine, in any HOME, timezone or git configuration. The push operation of
  task 4 re-runs it and compares.
* **Owner targets get no merge (F7).** For an owner target the candidate is the source itself and the source must
  descend from the target; anything else is ``land.changed`` (sign again). No rebase exists anywhere (N2).
* **Scope is what was approved (F6).** The target now must descend from the ``target_head`` recorded at enqueue (a
  rewound or force-pushed target moves the merge base back, so more history would land than the person saw), and
  ``target_head..source`` must still be the recorded number of commits.
* **Merges happen in the host repository, from the imported source objects (never in the agent's repository).** A
  conflict is a result (:class:`Conflict`), not an exception: the ticket gets a need, the queue moves on.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orch import gitsafe, oslock
from orch.gitread import limits
from orch.gitread.errors import Unavailable
from orch.gitread.fs import Dir, open_walk
from orch.gitread.objects import ObjectStore
from orch.gitread.pinning import pin_repo
from orch.gitread.refs import branch_ok
from orch.gitread.repo import Repo
from orch.land import key
from orch.land.key import LandKeyError
from orch.privdir import others_can_write

__all__ = [
    "BOT_EMAIL", "BOT_NAME", "Candidate", "Conflict", "ImportStats", "LandGitError", "LandRepo", "target_ref",
]  # fmt: skip

#: the committer and author of every merge commit orch makes (I4: fixed, never the person's configuration)
BOT_NAME = "orch landing"
BOT_EMAIL = "landing@orch.invalid"

MAX_COMMITS = 5_000  # new commits one import may bring (a larger range is refused: too big to review)
MAX_OBJECTS = 200_000
MAX_BYTES = 512 * limits.MiB
RUN_SECONDS = 60.0
OUT_MAX = 16 * limits.MiB
MESSAGE_MAX = 4096
_OID = re.compile(r"[0-9a-f]{40}")
#: more packs than this in the host repository and the next import folds them into one (gitread refuses 256)
REPACK_PACKS = 32
_WHEN_MIN, _WHEN_MAX = 946_684_800, 4_102_444_800  # 2000-01-01 .. 2100-01-01
_GITLINK = 0o160000
_SYMLINK = 0o120000
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git knows it without the object


class LandGitError(Exception):
    """A refusal, with the catalog-style ``code`` the host ops will map later (task 8) and a short plain ``reason``
    (never a path of the person's, never file contents)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Candidate:
    """The commit a landing checks and pushes. ``kind`` is ``fast_forward`` (``sha == source_sha``) or ``merge``
    (``parents == (target_sha, source_sha)``)."""

    sha: str
    kind: str
    target_sha: str
    source_sha: str


@dataclass(frozen=True)
class Conflict:
    """The merge of the source into the target conflicts in ``paths`` (as git names them; data, not instructions)."""

    paths: tuple[str, ...]


@dataclass(frozen=True)
class ImportStats:
    commits: int
    objects: int
    bytes: int


def _system_env(own: Mapping[str, str]) -> dict[str, str]:
    """``own`` with ``PATH`` the system directories: no git process of Landing (the gate's reads included) ever finds a
    ``git`` in a user-writable directory (Codex on #762); the operations themselves run :func:`_pinned_git`."""
    return {**own, "PATH": "/usr/bin:/bin"}


def _pinned_git() -> tuple[str, str]:
    try:
        return key.pinned_git()
    except LandKeyError as e:
        raise LandGitError(e.code, e.reason) from None


def target_ref(name: str) -> str:
    """``refs/heads/<name>`` for a target branch, by git's ref rules (:func:`orch.gitread.refs.branch_ok`: no ``..``,
    ``@{``, leading ``-``, control characters, ``.lock`` component...). Compared exactly, never case-folded here."""
    if not isinstance(name, str) or name.startswith("refs/") or not branch_ok(name):
        raise LandGitError("land.bad_target", "the target is not a valid branch name")
    return f"refs/heads/{name}"


def _oid(value: object, what: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise LandGitError("land.bad_input", f"{what} is not a full object id")
    return value


def _hostile_name(name: bytes, mode: int) -> bool:
    """A tree entry git would refuse to materialise, or that is a known checkout attack: ``.git`` in any spelling
    (case, trailing dots or spaces, NTFS short name ``git~1``), a symlinked ``.gitmodules``."""
    n = name.rstrip(b" .").lower()
    if n == b".git" or re.fullmatch(rb"git~\d+", n) is not None:
        return True
    return n == b".gitmodules" and mode == _SYMLINK


def _have(host_dir: Dir, host: ObjectStore, oid: str) -> bool:
    """Whether the host repository has ``oid`` (loose, or packed by a fetch). A fan-out directory that does not exist
    yet is "no"; one that is a symlink or a file is a refusal, never "no" (Codex #5 on #761)."""
    fan = host_dir.kind(oid[:2])
    if fan is None:  # no loose fan-out directory: it can only be in a pack
        try:
            return host.exists(oid)
        except Unavailable:
            return False
    if fan != "dir":
        raise LandGitError("land.unsafe", "the landing object store is not what git wrote")
    return host.exists(oid)


def _topo(new: Mapping[str, Sequence[str]]) -> list[str]:
    """Parents before children, over the commits in ``new`` (parents outside it are already present)."""
    out: list[str] = []
    done: set[str] = set()
    for root in sorted(new):
        if root in done:
            continue
        stack = [(root, False)]
        while stack:
            oid, expanded = stack.pop()
            if expanded:
                if oid not in done:
                    done.add(oid)
                    out.append(oid)
                continue
            if oid in done:
                continue
            stack.append((oid, True))
            stack.extend((p, False) for p in new[oid] if p in new and p not in done)
    return out


_PACK_TYPES = {"commit": 1, "tree": 2, "blob": 3}


class PackWriter:
    """The incoming objects as ONE undeltified pack v2 file in the private quarantine directory (written entry by entry,
    so memory stays flat). git's ``index-pack --strict`` is the only thing that turns it into objects."""

    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, "incoming.pack")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self._f = os.fdopen(fd, "wb")
        self._f.write(b"PACK\0\0\0\2\0\0\0\0")  # the object count is filled in at the end
        self._sha = hashlib.sha1()  # noqa: S324 - git's object format
        self.count = 0
        self._seen: set[str] = set()

    def add(self, oid: str, kind: str, data: bytes) -> bool:
        """Append one hash-checked object; ``False`` if this pack already has it."""
        if oid in self._seen:
            return False
        self._seen.add(oid)
        size = len(data)
        byte = (_PACK_TYPES[kind] << 4) | (size & 15)
        size >>= 4
        head = bytearray()
        while size:
            head.append(byte | 0x80)
            byte = size & 0x7F
            size >>= 7
        head.append(byte)
        self._f.write(bytes(head) + zlib.compress(data, 1))
        self.count += 1
        return True

    def finish(self) -> bytes:
        """The whole pack (header with the count, entries, SHA-1 trailer), for git's stdin."""
        self._f.flush()
        self._f.close()
        with open(self.path, "r+b") as f:
            f.seek(8)
            f.write(self.count.to_bytes(4, "big"))
        with open(self.path, "rb") as f:
            data = f.read()
        return data + hashlib.sha1(data).digest()  # noqa: S324

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._f.close()


class LandRepo:
    """The host-owned bare repository at ``path`` (absolute, a real path). ``own`` is the caller's environment, of which
    :func:`orch.gitsafe.isolated_env` keeps only ``PATH``, ``HOME`` and a few more (never a ``GIT_*`` variable)."""

    def __init__(self, path: str, own: Mapping[str, str] | None = None) -> None:
        if not path.startswith("/") or "\0" in path:
            raise LandGitError("land.bad_input", "the landing repository path is not absolute")
        self.path = path
        self.own: Mapping[str, str] = _system_env(os.environ if own is None else own)
        self.git, self.exec_path = _pinned_git()

    # -- creating
    @classmethod
    def create(cls, path: str, own: Mapping[str, str] | None = None) -> LandRepo:
        """Create (or reopen) the bare repository at ``path``; its parent directory must exist and be the person's."""
        env_own = _system_env(os.environ if own is None else own)
        if not path.startswith("/") or "\0" in path:
            raise LandGitError("land.bad_input", "the landing repository path is not absolute")
        if os.path.lexists(path):
            return cls(path, env_own)  # reopening never runs ``git init`` there; the first operation gates it
        try:
            parent = os.lstat(os.path.dirname(path))
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or others_can_write(parent, os.geteuid())
            ):
                raise LandGitError("land.unsafe", "the landing directory is not private to this user")
            os.mkdir(path, 0o700)
        except OSError:
            raise LandGitError("land.unavailable", "the landing repository cannot be created") from None
        repo = cls(path, env_own)
        try:
            with gitsafe.pinned(repo.git, repo.exec_path):
                argv, env = gitsafe.land_init(path, env_own)
        except gitsafe.Unsafe as e:
            raise LandGitError("land.unsafe", e.reason) from None
        code, _ = repo._spawn(argv, env, b"")
        if code != 0:
            raise LandGitError("land.unavailable", "the landing repository cannot be initialised")
        return repo

    # -- running git (only through gitsafe)
    def _spawn(self, argv: list[str], env: dict[str, str], stdin: bytes) -> tuple[int, bytes]:
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv built by gitsafe from a closed set
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd="/",
                start_new_session=True, close_fds=True,
            )  # fmt: skip
        except OSError:
            raise LandGitError("land.unavailable", "git cannot be started") from None
        try:
            out, _ = proc.communicate(stdin, timeout=RUN_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=5)
            raise LandGitError("land.unavailable", "git timed out") from None
        if len(out) > OUT_MAX:
            raise LandGitError("land.too_big", "git printed too much")
        return proc.returncode, out

    def _run(
        self, op: str, operand: str | tuple[str, ...] | None = None, *, stdin: bytes = b"",
        env_extra: Mapping[str, str] | None = None, ok: tuple[int, ...] = (0,), net: gitsafe.NetSpec | None = None,
    ) -> tuple[int, bytes]:  # fmt: skip
        try:
            argv, env = gitsafe.command(
                self.path,
                op,
                self.own,
                operand=operand,
                env_extra=env_extra,
                net=net,
                git=self.git,
                exec_path=self.exec_path,
            )
        except gitsafe.Unsafe as e:
            raise LandGitError("land.unsafe", e.reason) from None
        code, out = self._spawn(argv, env, stdin)
        if code not in ok:
            raise LandGitError("land.unavailable", f"git {op} failed")
        return code, out

    # -- questions about the repository's own history
    def has(self, oid: str) -> bool:
        return self._run("land_has", _oid(oid, "object"), ok=(0, 1))[0] == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ``ancestor`` is ``descendant`` or reachable from it. Both commits must be present."""
        return self._run("land_is_ancestor", (_oid(ancestor, "ancestor"), _oid(descendant, "descendant")),
                         ok=(0, 1))[0] == 0  # fmt: skip

    def merge_base(self, a: str, b: str) -> str | None:
        code, out = self._run("land_merge_base", (_oid(a, "commit"), _oid(b, "commit")), ok=(0, 1))
        return out.decode("ascii", "replace").strip() if code == 0 else None

    def count(self, base: str, head: str) -> int:
        """The number of commits ``base..head``."""
        _, out = self._run("land_count", (_oid(base, "base"), _oid(head, "head")))
        try:
            return int(out.strip())
        except ValueError:
            raise LandGitError("land.unavailable", "git printed no count") from None

    def parents(self, commit: str) -> tuple[str, ...]:
        _, out = self._run("land_commit", _oid(commit, "commit"))
        head = out.split(b"\n\n", 1)[0]
        return tuple(ln[7:].decode("ascii") for ln in head.split(b"\n") if ln.startswith(b"parent "))

    def pin(self, oid: str) -> None:
        """Keep ``oid`` reachable under ``refs/land/<oid>`` (so nothing prunes it before its push)."""
        self._run("land_update_ref", (f"refs/land/{_oid(oid, 'object')}", oid))

    # -- F5: object import through gitread
    def import_commits(self, source_path: str, tip: str, *, max_commits: int = MAX_COMMITS) -> ImportStats:
        """Bring commit ``tip`` of the repository at ``source_path`` (a real path of a working copy) and everything it
        needs that this repository does not have yet, into this repository. Walking stops at every commit already
        present (the target's history, put here by task 4's fetch). Nothing is imported unless the whole range passes
        the refusals (shallow, alternates, SHA-256, gitlinks are skipped here and refused by :meth:`candidate`'s
        diff check, hostile entry names), so an agent's repository can make this fail but never run a program."""
        tip = _oid(tip, "the source commit")
        if not isinstance(source_path, str) or not source_path.startswith("/"):
            raise LandGitError("land.bad_input", "the source path is not absolute")
        try:
            with self._lock():
                stats = self._import(os.path.realpath(source_path), tip, max_commits)
                self._maybe_repack()
                return stats
        except Unavailable as e:
            raise LandGitError("land.unavailable", e.reason) from None

    @contextlib.contextmanager
    def _lock(self):  # noqa: ANN202
        """One import (and the repack after it) at a time per host repository: ``flock`` on a file inside it, opened
        ``O_NOFOLLOW`` (kernel-released, so no stale lock)."""
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(os.path.join(self.path, "land-import.lock"), flags, 0o600)
            oslock.flock(fd, oslock.LOCK_EX)
        except OSError:
            raise LandGitError("land.unavailable", "the landing repository cannot be locked") from None
        try:
            yield
        finally:
            os.close(fd)

    def _maybe_repack(self) -> None:
        """Every import adds a pack; past :data:`REPACK_PACKS` fold them (objects git already accepted) into one, so the
        reader's pack limit is never reached."""
        try:
            packs = sum(1 for n in os.listdir(os.path.join(self.path, "objects", "pack")) if n.endswith(".idx"))
        except OSError:
            return
        if packs > REPACK_PACKS:
            self._run("land_repack", ok=(0,))

    def _import(self, real: str, tip: str, max_commits: int) -> ImportStats:
        host_dir, _ = open_walk(os.path.join(self.path, "objects"))
        host = ObjectStore(host_dir, "sha1")
        counts = {"objects": 0, "bytes": 0}
        quarantine = os.path.join(self.path, f"incoming-{secrets.token_hex(8)}")
        pack: PackWriter | None = None

        def put(src: Repo, oid: str, kind: str) -> None:
            _, data = src.objects.read(oid, kind)
            counts["bytes"] += len(data)
            if counts["bytes"] > MAX_BYTES or counts["objects"] >= MAX_OBJECTS:
                raise LandGitError("land.too_big", "the change is too large to land")
            if pack.add(oid, kind, data):  # type: ignore[union-attr]
                counts["objects"] += 1

        try:
            try:
                os.mkdir(quarantine, 0o700)
            except OSError:
                raise LandGitError("land.unavailable", "the import area cannot be created") from None
            pack = PackWriter(quarantine)
            with Repo(pin_repo(real, None)) as src:
                if src.algo != "sha1":
                    raise LandGitError("land.unavailable", "only SHA-1 repositories can land")
                for probe in ("shallow", "objects/info/alternates", "objects/info/http-alternates"):
                    try:
                        present = src.commondir.kind(probe) is not None
                    except Unavailable:
                        present = False
                    if present:
                        code = "land.shallow" if probe == "shallow" else "land.unavailable"
                        raise LandGitError(code, f"the source repository has {probe.rsplit('/', 1)[-1]}")
                new: dict[str, list[str]] = {}
                seen: set[str] = set()
                todo = [tip]
                while todo:
                    oid = todo.pop()
                    if oid in seen:
                        continue
                    seen.add(oid)
                    if _have(host_dir, host, oid):
                        continue
                    c = src.commit(oid)
                    new[oid] = list(c.parents)
                    if len(new) > max_commits:
                        raise LandGitError("land.too_big", "the change has too many commits to land")
                    todo.extend(c.parents)
                for oid in _topo(new):
                    self._import_tree(src, host, host_dir, src.commit(oid).tree, put)
                    put(src, oid, "commit")
            if pack.count:
                self._index(pack.finish(), quarantine)  # git's strict check of the incoming objects only
                self._promote(quarantine, os.path.join(self.path, "objects", "pack"))
        finally:
            # whatever happened (BaseException included) the quarantine goes, by its exact path: an object in objects/
            # is only ever one git accepted, with its whole closure
            if pack is not None:
                pack.close()
            shutil.rmtree(quarantine, ignore_errors=True)
            host.close()
            host_dir.close()
        return ImportStats(len(new), counts["objects"], counts["bytes"])

    def _import_tree(self, src: Repo, host: ObjectStore, host_dir: Dir, root: str, put) -> None:  # noqa: ANN001
        """Write ``root`` and everything below it that is missing, children before parents. A gitlink is skipped (it
        names a commit of another repository; any change of one is refused later)."""
        written: set[str] = set()
        stack = [(root, False)]
        while stack:
            oid, expanded = stack.pop()
            if expanded:
                if oid not in written:
                    put(src, oid, "tree")
                    written.add(oid)
                continue
            if oid in written or _have(host_dir, host, oid):
                continue
            stack.append((oid, True))
            for e in src.tree(oid):
                if _hostile_name(e.name, e.mode):
                    raise LandGitError("land.hostile_path", "the change has a path git must not check out")
                if e.mode == _GITLINK:
                    continue
                if e.is_tree:
                    stack.append((e.oid, False))
                elif e.oid not in written and not _have(host_dir, host, e.oid):
                    put(src, e.oid, "blob")
                    written.add(e.oid)

    def _index(self, pack: bytes, quarantine: str) -> None:
        """git's own strict check of the incoming objects (``index-pack --strict`` with every relevant fsck message an
        error: git's ``verify_path`` and fsck rules, not a spelling list of ours), reading the quarantine as its object
        directory and the host's own objects as its alternate, like git's receive quarantine. Only what is in the pack
        is checked, never the history the host already holds. ``land.hostile_path`` on any error."""
        env = {
            "GIT_OBJECT_DIRECTORY": quarantine,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(self.path, "objects"),
        }
        os.makedirs(os.path.join(quarantine, "pack"), mode=0o700, exist_ok=True)
        try:
            code, _ = self._run("land_index_pack", env_extra=env, stdin=pack, ok=tuple(range(256)))
        except LandGitError as e:
            if e.code == "land.unsafe":
                raise
            code = 1
        if code != 0:
            raise LandGitError("land.hostile_path", "git's own object check refuses the change")

    _PACK_FILE = re.compile(r"pack-[0-9a-f]{40}\.(pack|rev|idx)")

    @classmethod
    def _promote(cls, quarantine: str, dest: str) -> None:
        """Move the pack git wrote (and checked) from the quarantine into ``objects/pack``: ``.pack`` and ``.rev``
        first, ``.idx`` last (an index makes the pack visible), one rename each. On a failure what was moved is removed
        again, so ``objects/`` only ever gains complete, accepted packs."""
        src = os.path.join(quarantine, "pack")
        try:
            names = sorted(n for n in os.listdir(src) if not n.startswith("tmp_"))
        except OSError:
            raise LandGitError("land.unavailable", "git wrote no pack") from None
        if not names or any(cls._PACK_FILE.fullmatch(n) is None for n in names):
            raise LandGitError("land.unavailable", "git wrote an unexpected file")
        names.sort(key=lambda n: n.endswith(".idx"))
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        moved: list[str] = []
        sfd = dfd = -1
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(dest, 0o755)
            sfd = os.open(src, os.O_RDONLY | flags)
            dfd = os.open(dest, os.O_RDONLY | flags)
            for n in names:
                try:
                    os.stat(n, dir_fd=dfd, follow_symlinks=False)
                    continue  # an identical import already put this pack there: keep it, never replace or undo it
                except FileNotFoundError:
                    pass
                os.replace(n, n, src_dir_fd=sfd, dst_dir_fd=dfd)
                moved.append(n)
            os.fsync(dfd)
        except OSError:
            for n in moved:
                with contextlib.suppress(OSError):
                    os.unlink(n, dir_fd=dfd)
            raise LandGitError("land.unsafe", "the landing object store is not what git wrote") from None
        finally:
            for fd in (sfd, dfd):
                if fd >= 0:
                    os.close(fd)

    # -- submodules
    def _refuse_gitlinks(self, base: str | None, head: str) -> None:
        """``land.submodule`` when ``base..head`` adds, removes or moves a gitlink (v1 does not land submodules)."""
        _, out = self._run("land_diff_raw", (base if base is not None else _EMPTY_TREE, head))
        parts = out.split(b"\0")
        for i in range(0, len(parts) - 1, 2):
            meta = parts[i].split()  # b":<oldmode> <newmode> <oldsha> <newsha> <status>"
            if len(meta) >= 2 and (meta[0].lstrip(b":") == b"160000" or meta[1] == b"160000"):
                raise LandGitError("land.submodule", "the change touches a submodule")

    # -- I4 / F6 / F7: the candidate
    def candidate(
        self, *, target_sha: str, source_sha: str, target_head: str, commits: int, owner_target: bool, when: int,
        message: str,
    ) -> Candidate | Conflict:  # fmt: skip
        """The one commit this attempt checks and pushes. Both ``target_sha`` (the target now, fetched) and
        ``source_sha`` (imported with :meth:`import_commits`) must be present. Deterministic in its arguments (I4).

        Refusals (:class:`LandGitError`): ``land.changed`` when the target no longer descends from ``target_head`` (F6:
        rewound or replaced), when ``target_head..source_sha`` is no longer ``commits`` commits, or, for an owner
        target, when the source does not descend from the target (F7); ``land.submodule``; ``land.bad_input``."""
        target_sha, source_sha = _oid(target_sha, "the target"), _oid(source_sha, "the source")
        target_head = _oid(target_head, "the recorded target head")
        if not isinstance(commits, int) or isinstance(commits, bool) or commits < 0:
            raise LandGitError("land.bad_input", "the commit count is invalid")
        if not isinstance(when, int) or isinstance(when, bool) or not _WHEN_MIN <= when <= _WHEN_MAX:
            raise LandGitError("land.bad_input", "the attempt time is invalid")
        if not isinstance(message, str) or not message or len(message) > MESSAGE_MAX or "\0" in message:
            raise LandGitError("land.bad_input", "the commit message is invalid")
        for what, oid in (("target", target_sha), ("source", source_sha), ("recorded target head", target_head)):
            if not self.has(oid):
                raise LandGitError("land.unavailable", f"the {what} commit is not in the landing repository")
        if not self.is_ancestor(target_head, target_sha):
            raise LandGitError("land.changed", "the target is not where it was when the landing was approved")
        if self.count(target_head, source_sha) != commits:
            raise LandGitError("land.changed", "the approved range is not the range now")
        self._refuse_gitlinks(self.merge_base(target_sha, source_sha), source_sha)
        if self.is_ancestor(target_sha, source_sha):
            self.pin(source_sha)
            return Candidate(source_sha, "fast_forward", target_sha, source_sha)
        if owner_target:
            raise LandGitError("land.changed", "the target moved: an owner landing must be signed again")
        code, out = self._run("land_merge_tree", (target_sha, source_sha), ok=(0, 1))
        fields = [f for f in out.split(b"\0") if f]
        if code == 1:
            return Conflict(tuple(sorted(p.decode("utf-8", "replace") for p in fields[1:])))
        tree = fields[0].decode("ascii", "replace") if fields else ""
        if _OID.fullmatch(tree) is None:
            raise LandGitError("land.unavailable", "git printed no tree")
        stamp = f"@{when} +0000"
        ident = {
            "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL, "GIT_COMMITTER_DATE": stamp,
        }  # fmt: skip
        _, made = self._run("land_commit_tree", (target_sha, source_sha, tree), stdin=message.encode("utf-8"),
                            env_extra=ident)  # fmt: skip
        sha = made.decode("ascii", "replace").strip()
        if _OID.fullmatch(sha) is None:
            raise LandGitError("land.unavailable", "git printed no commit")
        self.pin(sha)
        return Candidate(sha, "merge", target_sha, source_sha)

    def check_push_shape(self, candidate: Candidate) -> None:
        """F2, called by the push operation right before it pushes (task 4): a fast-forward candidate is the source and
        contains the target; a merge candidate has exactly ``(target, source)`` as its parents. Anything else is
        ``land.changed`` (a source tip that is itself a merge with the target as 2nd parent is a valid fast-forward)."""
        target, source, sha = (_oid(x, "commit") for x in (candidate.target_sha, candidate.source_sha, candidate.sha))
        if candidate.kind == "fast_forward":
            ok = sha == source and self.is_ancestor(target, sha)
        elif candidate.kind == "merge":
            ok = self.parents(sha) == (target, source)
        else:
            ok = False
        if not ok:
            raise LandGitError("land.changed", "the candidate does not extend the target as recorded")
