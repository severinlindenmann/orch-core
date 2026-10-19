"""Moving a workspace's store from the project root into ``<project>/.orch/`` (design B11-P2 section 3).

``migrate`` is the one engine; the host op ``host.store_migrate`` and (later) ``orch migrate`` in direct mode call it.

**Locks.** The caller holds ``owner.lock`` exclusively (``owner_lock``) from before planning until the engine returns,
and the engine itself holds the LEGACY ``.state/lock`` (the store lock) for the same time: openers that take only the
store lock (host-mode stores, custody and setup readers) wait on it, and the lock file moves with ``.state`` (the same
inode, still held), so a stale opener that gets the lock afterwards finds its layout changed
(:meth:`orch.store.store.Store._check_layout`) and refuses.

**Order.** plan (names with ``(dev, ino)``, byte digests of every log) -> journal ``planned`` -> ``mkdir
.orch-migrating`` (0700, ``EEXIST`` refused) -> journal ``moving`` -> ``renameat`` each name in (mode and owner are
kept: a rename) -> fsync -> ``renameat`` ``.orch-migrating`` to ``.orch`` -> verify (the same entries, the same log
digests, a full replay through ``Store.open``) -> journal ``committed`` -> ``after_commit`` (registry ``store``, the
``.gitignore`` line; idempotent) -> journal ``done``. Before ``committed`` any failure, and a crash found by
:func:`finish_interrupted`, rolls back by reverse renames (every ``(dev, ino)`` must match); after it, only
forward. No file is ever deleted, and nothing outside the project directory is touched.

Everything is done below the project directory fd (``O_NOFOLLOW``); an entry that is a link, or anything but a regular
file or a directory, refuses the whole migration before the first rename.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import layout as layout_mod

from .errors import StoreError
from .fsio import write_atomic
from .lock import FileLock, LockTimeout
from .paths import ULID

__all__ = ["JOURNAL", "MOVE_NAMES", "TMP_DIR", "MigrateError", "finish_interrupted", "journal_step", "migrate"]

#: what moves: every store record and directory of the legacy root layout (design section 1)
MOVE_NAMES = ("config.json", "keys.jsonl", "AGENTS.orch.md", "events", "tickets", "addons", ".state")
#: moved only when the caller found it to be THIS workspace's records repository (``records.tool.repo_is_ours``)
GIT_NAME = ".git"
_ALL_NAMES = (*MOVE_NAMES, GIT_NAME)
TMP_DIR = ".orch-migrating"
JOURNAL = "migrate.json"
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)

Fault = Callable[[str], None]


class MigrateError(Exception):
    """``code``: ``migrate.layout`` (not a legacy store, two stores, a link), ``migrate.busy`` (a lock),
    ``migrate.unsafe`` (an entry that must not be moved), ``migrate.verify`` (the check after the move failed; rolled
    back), ``migrate.stuck`` (a journal that cannot be completed or rolled back right now: files may be half way;
    nothing is guessed, nothing deleted, and a later run or the next host start continues from the journal)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


# ------------------------------------------------------------------------------------------------------ journal


def _journal_path(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / JOURNAL


def _read_journal(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, _FILE)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MigrateError("migrate.stuck", f"the migrate journal cannot be opened ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise MigrateError("migrate.stuck", "the migrate journal is not a private file of this user")
        doc = json.loads(os.read(fd, 1 << 22))
    except (OSError, ValueError):
        raise MigrateError("migrate.stuck", "the migrate journal is unreadable") from None
    finally:
        os.close(fd)
    if not isinstance(doc, dict) or doc.get("v") != 1 or doc.get("step") not in _STEPS:
        raise MigrateError("migrate.stuck", "the migrate journal has an unknown shape")
    names = doc.get("names") or {}
    if not isinstance(names, dict) or not set(names) <= set(_ALL_NAMES) or not all(_pair(v) for v in names.values()):
        raise MigrateError("migrate.stuck", "the migrate journal names an entry the migration does not move")
    if not all(v is None or _pair(v) for v in (doc.get("tmp"), doc.get("root"))):
        raise MigrateError("migrate.stuck", "the migrate journal has an unknown shape")
    return doc


def _pair(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(type(x) is int for x in value)


_STEPS = ("planned", "moving", "committed", "done", "rolled_back")


def _root_matches(doc: dict[str, Any], project: str | os.PathLike[str] | None) -> bool:
    """The journal belongs to the project at ``project`` (the recorded ``(dev, ino)`` of the project directory). A
    journal left behind by an unregistered workspace whose id is now registered at another root is not this root's."""
    if project is None:
        return True
    try:
        st = os.stat(project)
    except OSError:
        return False
    return doc.get("root") == [st.st_dev, st.st_ino]


def journal_step(state_dir: Path, workspace_id: str, project: str | os.PathLike[str] | None = None) -> str | None:
    """The step of the migrate journal (``None``: there is none, or it is for another project directory). Raises
    :class:`MigrateError` (``migrate.stuck``) for one that cannot be read."""
    doc = _read_journal(_journal_path(state_dir, workspace_id))
    if doc is None or not _root_matches(doc, project):
        return None
    return doc["step"]


def _write_journal(path: Path, doc: dict[str, Any]) -> None:
    """Replace the journal durably; a file system error is ``migrate.stuck`` (never a bare ``OSError``): at ``planned``
    nothing has moved, at ``committed`` the next run finishes forward."""
    try:
        write_atomic(path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), mode=0o600)
    except OSError as exc:
        raise MigrateError("migrate.stuck", f"the migrate journal cannot be written ({exc.strerror})") from None


# ------------------------------------------------------------------------------------------------------ helpers


def _lstat(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _ident(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino]


def _digests(root_fd: int) -> dict[str, str]:
    """sha256 of every event log below ``root_fd`` (``events/workspace.jsonl``, ``tickets/*/events.jsonl``): what a pure
    rename cannot change. Every component is opened ``O_NOFOLLOW`` below the fd."""
    out: dict[str, str] = {}

    def digest(dir_fd: int, name: str, key: str) -> None:
        try:
            fd = os.open(name, _FILE, dir_fd=dir_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MigrateError("migrate.unsafe", f"{key} cannot be read ({exc.strerror})") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MigrateError("migrate.unsafe", f"{key} is not a regular file")
            h = hashlib.sha256()
            while chunk := os.read(fd, 1 << 20):
                h.update(chunk)
            out[key] = h.hexdigest()
        finally:
            os.close(fd)

    def sub(parent: int, name: str) -> int | None:
        try:
            return os.open(name, _DIR, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MigrateError("migrate.unsafe", f"{name} is not a real directory ({exc.strerror})") from None

    events = sub(root_fd, "events")
    if events is not None:
        try:
            digest(events, "workspace.jsonl", "events/workspace.jsonl")
        finally:
            os.close(events)
    tickets = sub(root_fd, "tickets")
    if tickets is not None:
        try:
            for uid in sorted(os.listdir(tickets)):
                if not ULID.fullmatch(uid):
                    continue  # .DS_Store and the like: not a ticket, not read, not part of the comparison
                tdir = sub(tickets, uid)
                if tdir is None:
                    continue
                try:
                    digest(tdir, "events.jsonl", f"tickets/{uid}/events.jsonl")
                finally:
                    os.close(tdir)
        finally:
            os.close(tickets)
    return out


def _fsync(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(fd)


def _gitignore(pfd: int) -> bool:
    """Make sure the project's ``.gitignore`` has the line ``.orch/`` (idempotent, user lines kept). Only a plain file
    of this user with one link is touched (``O_NOFOLLOW``, ``O_NONBLOCK``): a link, a FIFO, a hard link or another
    owner's file is left alone. ``True``: the line is there."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(".gitignore", flags | getattr(os, "O_CLOEXEC", 0), 0o644, dir_fd=pfd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            return False
        text = os.read(fd, 1 << 20).decode("utf-8", "replace")
        if ".orch/" in text.split("\n"):
            return True
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, (("" if not text or text.endswith("\n") else "\n") + ".orch/\n").encode())
        _fsync(fd)
        return True
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------------ rollback


def _rollback(pfd: int, journal: dict[str, Any], jpath: Path) -> None:
    """:func:`_rollback_steps`, with a file system error as ``migrate.stuck`` (never an unhandled ``OSError``)."""
    base: int | None = None
    try:
        base = _state_base(pfd, journal)
        _rollback_steps(pfd, journal, jpath, base)
    except OSError as exc:
        raise MigrateError("migrate.stuck", f"the rollback could not finish ({exc.strerror}); run it again") from None
    finally:
        if base is not None:
            os.close(base)


def _rollback_steps(pfd: int, journal: dict[str, Any], jpath: Path, state_base: int | None) -> None:
    """Reverse the renames of an unfinished migration: every entry goes back only if it is the recorded one."""
    tmp_ident = journal.get("tmp")
    names: dict[str, list[int]] = journal.get("names") or {}
    here = _lstat(pfd, TMP_DIR)
    orch = _lstat(pfd, layout_mod.STORE_DIR)
    if tmp_ident is not None and here is None and orch is not None and _ident(orch) == tmp_ident:
        os.rename(layout_mod.STORE_DIR, TMP_DIR, src_dir_fd=pfd, dst_dir_fd=pfd)  # the last rename had happened
        here = _lstat(pfd, TMP_DIR)
    if here is not None and tmp_ident is None:
        # the crash was between ``mkdir`` and the journal write: an EMPTY directory of that name is ours to drop
        tfd = os.open(TMP_DIR, _DIR, dir_fd=pfd)
        try:
            empty = not os.listdir(tfd)
        finally:
            os.close(tfd)
        if empty:
            os.rmdir(TMP_DIR, dir_fd=pfd)
            here = None
    if here is not None:
        if tmp_ident is None or _ident(here) != tmp_ident:
            raise MigrateError("migrate.stuck", f"{TMP_DIR} is not the directory this migration made; left as it is")
        tfd = os.open(TMP_DIR, _DIR, dir_fd=pfd)
        try:
            for name in reversed(list(names)):
                st = _lstat(tfd, name)
                if st is None:
                    continue
                if _ident(st) != names[name] or _lstat(pfd, name) is not None:
                    raise MigrateError("migrate.stuck", f"{name} cannot be moved back; left as it is")
                os.rename(name, name, src_dir_fd=tfd, dst_dir_fd=pfd)
            _fsync(pfd)
            if os.listdir(tfd):
                raise MigrateError("migrate.stuck", f"{TMP_DIR} holds files this migration did not move")
        finally:
            os.close(tfd)
        os.rmdir(TMP_DIR, dir_fd=pfd)
        _fsync(pfd)
    _write_journal(jpath, {**journal, "step": "rolled_back"})


# ------------------------------------------------------------------------------------------------------ engine


def _finish(
    pfd: int, journal: dict[str, Any], jpath: Path, after_commit: Callable[[], None] | None, fault: Fault
) -> bool:
    """After ``committed``: only forward, every step idempotent. A failing step is ``migrate.stuck`` and the journal
    stays ``committed``: the next run (or host start) does it again. Returns whether ``.gitignore`` has the line."""
    try:
        if after_commit is not None:
            after_commit()
        ignored = _gitignore(pfd)
        fault("after_commit")
        _write_journal(jpath, {**journal, "step": "done"})
        fault("done")
        return ignored
    except MigrateError:
        raise
    except Exception as exc:  # noqa: BLE001 - the registry write, the journal, a fault: continue later, never half-done
        raise MigrateError(
            "migrate.stuck", f"finishing the migration failed ({type(exc).__name__}); run it again"
        ) from None


def _state_base(pfd: int, journal: dict[str, Any]) -> int | None:
    """An fd of the directory that holds the store's ``.state`` NOW, found by the journal's recorded ``(dev, ino)``: the
    project, ``.orch-migrating`` or ``.orch`` (the caller closes it); ``None`` when there is no ``.state`` anywhere."""
    want = (journal.get("names") or {}).get(".state")
    if want is None:
        return None
    candidates: list[int] = [os.dup(pfd)]
    for name in (TMP_DIR, layout_mod.STORE_DIR):
        try:
            candidates.append(os.open(name, _DIR, dir_fd=pfd))
        except OSError:
            continue
    found: int | None = None
    for fd in candidates:
        st = _lstat(fd, ".state")
        if found is None and st is not None and _ident(st) == want:
            found = fd
        else:
            os.close(fd)
    return found


def finish_interrupted(
    project: str | os.PathLike[str],
    *,
    state_dir: Path,
    workspace_id: str,
    after_commit: Callable[[], None] | None = None,
    fault: Fault | None = None,
) -> str:
    """Bring an interrupted migration to a terminal state (the caller holds ``owner.lock`` exclusively): ``planned`` or
    ``moving`` is rolled back, ``committed`` is finished forward. Returns the journal's step afterwards (``"none"``
    without a journal). The store lock is the ``.state/lock`` found by the journal's recorded identity, wherever
    ``.state`` sits now. A step that cannot finish is ``migrate.stuck`` and the journal keeps its step."""
    fault = fault or (lambda point: None)
    jpath = _journal_path(state_dir, workspace_id)
    journal = _read_journal(jpath)
    if journal is not None and not _root_matches(journal, project):
        return "none"  # another project directory's journal: not this root's to complete or undo
    if journal is None or journal["step"] in ("done", "rolled_back"):
        return "none" if journal is None else journal["step"]
    pfd = _open_project(project)
    base: int | None = None
    try:
        try:
            base = _state_base(pfd, journal)
            with _lock_in(base, 10.0):
                if journal["step"] == "committed":
                    _finish(pfd, journal, jpath, after_commit, fault)
                    return "done"
                _rollback_steps(pfd, journal, jpath, base)
                return "rolled_back"
        except OSError as exc:
            raise MigrateError(
                "migrate.stuck", f"the interrupted migration could not finish ({exc.strerror})"
            ) from None
    finally:
        if base is not None:
            os.close(base)
        os.close(pfd)


def _open_project(project: str | os.PathLike[str]) -> int:
    try:
        return os.open(project, _DIR)
    except OSError as exc:
        raise MigrateError("migrate.layout", f"the project directory cannot be opened ({exc.strerror})") from None


@contextlib.contextmanager
def _lock_in(base: int | None, timeout: float) -> Any:
    """The store lock ``.state/lock`` below the directory fd ``base`` (never a link; ``None``: there is no store lock to
    take, nothing can hold it)."""
    if base is None:
        yield
        return
    try:
        sfd = os.open(".state", _DIR, dir_fd=base)
    except FileNotFoundError:
        yield
        return
    except OSError as exc:
        raise MigrateError("migrate.unsafe", f".state is not a real directory ({exc.strerror})") from None
    try:

        def opener() -> int:
            return os.open("lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=sfd)

        try:
            with FileLock(".state/lock", timeout=timeout, opener=opener):
                yield
        except LockTimeout:
            raise MigrateError("migrate.busy", "the workspace store lock is held by another process") from None
    finally:
        os.close(sfd)


def migrate(
    project: str | os.PathLike[str],
    *,
    state_dir: Path,
    workspace_id: str,
    expected_genesis: str | None = None,
    lock_timeout: float = 10.0,
    after_commit: Callable[[], None] | None = None,
    fault: Fault | None = None,
    move_git: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Move the legacy store of ``project`` into ``.orch``. The caller holds ``owner.lock`` exclusively around this
    call. Returns ``{"migrated": bool, "names": [...]}`` (``migrated`` false: it was already done). Raises
    :class:`MigrateError`; on ``migrate.verify`` everything has been rolled back."""
    fault = fault or (lambda point: None)
    jpath = _journal_path(state_dir, workspace_id)
    if not jpath.parent.is_dir():
        raise MigrateError("migrate.stuck", "this workspace has no host state directory on this machine")
    pfd = _open_project(project)
    try:
        step = finish_interrupted(
            project, state_dir=state_dir, workspace_id=workspace_id, after_commit=after_commit, fault=fault
        )
        try:
            found = layout_mod.resolve(pfd, project)
        except layout_mod.LayoutError as exc:
            raise MigrateError("migrate.layout", str(exc)) from None
        if found is None:
            raise MigrateError("migrate.layout", "the directory holds no orch store")
        if not found.legacy:
            return {"migrated": False, "names": [], "recovered": step}
        with _lock_in(pfd, lock_timeout):
            return _move(pfd, project, jpath, state_dir, workspace_id, expected_genesis, after_commit, fault, move_git)
    finally:
        os.close(pfd)


def _move(
    pfd: int,
    project: str | os.PathLike[str],
    jpath: Path,
    state_dir: Path,
    workspace_id: str,
    expected_genesis: str | None,
    after_commit: Callable[[], None] | None,
    fault: Fault,
    move_git: tuple[int, int] | None = None,
) -> dict[str, Any]:
    # --- plan: every entry below the fd, nothing a link or odd; digests of the logs
    if _lstat(pfd, layout_mod.STORE_DIR) is not None or _lstat(pfd, TMP_DIR) is not None:
        raise MigrateError("migrate.layout", f"{layout_mod.STORE_DIR} or {TMP_DIR} already exists; nothing is moved")
    names: dict[str, list[int]] = {}
    for name in (*MOVE_NAMES, *((GIT_NAME,) if move_git is not None else ())):
        st = _lstat(pfd, name)
        if st is None:
            continue
        want_dir = name in ("events", "tickets", "addons", ".state", GIT_NAME)
        if (
            stat.S_ISLNK(st.st_mode)
            or (stat.S_ISDIR(st.st_mode) != want_dir)
            or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode))
        ):
            raise MigrateError(
                "migrate.unsafe", f"{name} is a link or not what the store keeps there; nothing is moved"
            )
        names[name] = _ident(st)
        if name == GIT_NAME and _ident(st) != list(move_git or ()):
            raise MigrateError("migrate.unsafe", ".git is not the directory that was identified; nothing is moved")
    if not {"config.json", "events", "tickets", ".state"} <= set(names):
        raise MigrateError(
            "migrate.layout", "the directory holds no complete orch store (config.json, events/, tickets/, .state/)"
        )
    before = _digests(pfd)
    pst = os.fstat(pfd)
    journal: dict[str, Any] = {
        "v": 1,
        "step": "planned",
        "names": names,
        "digests": before,
        "tmp": None,
        "root": [pst.st_dev, pst.st_ino],
    }
    _write_journal(jpath, journal)
    fault("planned")
    # --- move
    try:
        os.mkdir(TMP_DIR, 0o700, dir_fd=pfd)
        fault("mkdir")  # a crash here leaves an empty .orch-migrating and a journal without its identity
        tst = os.stat(TMP_DIR, dir_fd=pfd, follow_symlinks=False)
        journal = {**journal, "step": "moving", "tmp": _ident(tst)}
        _write_journal(jpath, journal)
        fault("tmp_made")
        tfd = os.open(TMP_DIR, _DIR, dir_fd=pfd)
        try:
            for name in names:
                os.rename(name, name, src_dir_fd=pfd, dst_dir_fd=tfd)
                fault(f"moved:{name}")
            _fsync(tfd)
        finally:
            os.close(tfd)
        fault("moved_all")
        _fsync(pfd)
        os.rename(TMP_DIR, layout_mod.STORE_DIR, src_dir_fd=pfd, dst_dir_fd=pfd)
        _fsync(pfd)
        fault("renamed")
        _verify(pfd, project, names, before, workspace_id, expected_genesis, journal["tmp"])
        fault("verified")
    except BaseException as exc:
        # nothing is committed yet: roll back (a crash instead of an exception is found by finish_interrupted())
        try:
            _rollback(pfd, journal, jpath)
        except MigrateError:
            raise
        if isinstance(exc, MigrateError):
            raise
        if isinstance(exc, (OSError, StoreError)):
            raise MigrateError("migrate.verify", f"{type(exc).__name__}: rolled back") from exc
        raise
    journal = {**journal, "step": "committed"}
    _write_journal(jpath, journal)
    fault("committed")
    ignored = _finish(pfd, journal, jpath, after_commit, fault)
    return {"migrated": True, "names": sorted(names), "gitignore": ignored}


def _verify(
    pfd: int,
    project: str | os.PathLike[str],
    names: dict[str, list[int]],
    before: dict[str, str],
    workspace_id: str,
    expected_genesis: str | None,
    tmp_ident: list[int],
) -> None:
    """After the move and before ``committed``: ``.orch`` is the directory made, holds the very entries that were
    planned, the log digests are unchanged and a full replay reads the new layout."""
    try:
        found = layout_mod.resolve(pfd, project)
    except layout_mod.LayoutError as exc:
        raise MigrateError("migrate.verify", str(exc)) from None
    if found is None or found.legacy:
        raise MigrateError("migrate.verify", "the moved store does not resolve to the new layout")
    sfd = layout_mod.open_store_dir(pfd)
    if sfd is None:
        raise MigrateError("migrate.verify", f"{layout_mod.STORE_DIR} is gone")
    try:
        if _ident(os.fstat(sfd)) != tmp_ident:
            raise MigrateError("migrate.verify", f"{layout_mod.STORE_DIR} is not the directory that was filled")
        for name, ident in names.items():
            st = _lstat(sfd, name)
            if st is None or _ident(st) != ident:
                raise MigrateError("migrate.verify", f"{name} is not the entry that was planned")
        if _digests(sfd) != before:
            raise MigrateError("migrate.verify", "an event log changed while it was moved")
    finally:
        os.close(sfd)
    from .store import Store  # a full replay through the real open: chain, genesis pin, projections

    try:
        s = Store.open(
            project, expected_workspace_id=workspace_id, expected_genesis=expected_genesis, load="all", scaffold=False
        )
    except StoreError as exc:
        raise MigrateError("migrate.verify", f"replay failed: {exc.code}") from None
    try:
        if s.layout.legacy:
            raise MigrateError("migrate.verify", "the replay opened the legacy layout")
    finally:
        s.close()
