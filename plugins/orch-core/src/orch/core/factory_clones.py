"""AI Factory: one separate clone per child, owned by the runner (docs/factory.md, "Per-child clones").

The runner (the dashboard process the human started) gives each child that has no linked worktree of its own a clone of
the workspace's repository, outside the workspace and outside the orch config dir, at
`<orch config dir>-clones/<workspace id>/<child id>/repo`, on a branch of its own (`fx/<child id>`). The child's
session starts there (in the clone's copy of the workspace folder, when the workspace is a subfolder of its
repository) and commits there; the release step fetches that branch from the clone.

- **Where it lives.** Not in the permits folder: the guard refuses every tool and command that reaches the orch config
  dir (a session started inside it could not run one command), and the clone's work tree must be the session's to write.
  Its `.git` stays protected by the guard's `.git` rules (file tools never write a `.git` component, shell writes into
  `.git` files are denied), as in every checkout.
- **What is recorded.** child -> (clone path, branch, base, source, the device and inode of the clone's folder and of
  its `.git`) in `permits/child-clones/` of the orch config dir, written only by the runner, guarded like the rest of
  the permits folder. The path is derived from the validated workspace and child ids, never from ticket text; a record
  whose path or branch is not the derived one does not count.
- **By descriptor.** The clones folder is agent-writable, so nothing there is created, written, renamed or removed by a
  path string: every folder is opened from the one above it with O_NOFOLLOW|O_DIRECTORY, the clone's folder is made
  exclusively (`mkdir` under its parent's descriptor) and pinned before git runs, and everything after is checked
  against the pinned inodes. Only git takes the clone's path by name (clone, checkout, fetch); the inodes are checked
  right after it, and a clone that moved is never used.
- **How it is made.** `git clone --local --no-hardlinks --no-checkout --no-recurse-submodules --template=` into that
  empty folder, by argv, with the release step's git isolation (no user or system git config, hooks and fsmonitor off,
  no replace objects, an empty home), then a config the runner writes (no includes, aliases, filters, hooks or
  fsmonitor; `origin` is the workspace path with a push URL that cannot work) and a checkout of the base's tip onto the
  child's branch, no submodules. `--local` copies the source's objects and refs (no upload-pack runs, no source config,
  hook or attribute is used); a git dir orch does not take (_odd_fd) is refused in the source and in the clone.
- **Lifecycle.** Made at the child's first launch, reused (its config written again) at every later one; never deleted
  by the runner. A folder at the clone's path that the runner has no record of is never touched: the child is not
  started and the run view says why. A clone attempt that fails removes only the folder that attempt made (by inode).
"""
from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path

from orch.core import factory_sessions as fs

CLONE_TIMEOUT = 300  # seconds for one clone (clone and checkout together) when no shorter budget is given
LOCK_TIMEOUT = 30  # seconds `clean` and the runner wait for one child's clone lock
_KEYS = {"workspace", "child", "path", "branch", "base", "source", "inode", "git_inode", "at"}
_FAIL_KEYS = {"workspace", "child", "why", "at"}
NO_PUSH = "/dev/null/orch-runner-never-pushes"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW


class CloneError(Exception):
    """The clone cannot be used or touched (a link, another inode, a git dir orch does not take): nothing is done."""


def root() -> Path:
    """The folder that holds every runner-owned clone: beside the orch config dir (`~/.config/orch-clones` for
    `~/.config/orch`), never inside it."""
    from orch.core.ledger import base_dir
    b = base_dir()
    return b.with_name(b.name + "-clones")


def in_root(p: Path) -> bool:
    """Whether `p` (already resolved) lies below the clones folder."""
    try:
        r = root().resolve()
    except (OSError, RuntimeError):
        return False
    return r in Path(p).parents


def _key_ok(child) -> bool:
    from orch.core.factory_release import KEY
    return isinstance(child, str) and bool(KEY.fullmatch(child))


def clone_dir(ws, child: str) -> Path:
    from orch.core.ledger import workspace_id
    return root() / workspace_id(ws) / fs._safe(child) / "repo"


def branch_for(child: str) -> str:
    """The branch the runner makes for the child: it names the child as a word (the release's branch rule)."""
    return f"fx/{child.lower()}"


def workspace_rel(ws) -> Path | None:
    """The workspace root relative to the top of its git checkout (Path(".") at the top), or None (no checkout)."""
    from orch.core.factory_release import workspace_repo
    top = workspace_repo(ws)
    if top is None:
        return None
    try:
        return Path(ws.root).resolve().relative_to(top.resolve())
    except ValueError:
        return None


def start_in(ws, rec: dict) -> Path:
    """Where a session in this clone starts: the clone's copy of the workspace folder."""
    return Path(rec["path"]) / (workspace_rel(ws) or Path("."))


# -- by descriptor, never through a link ----------------------------------------------------------------------------

def _ino(st) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def _parent_parts(ws, child: str) -> list[str]:
    from orch.core.ledger import workspace_id
    wid = workspace_id(ws)
    if not _key_ok(child) or not re.fullmatch(r"[0-9a-f]{16}", wid):
        raise CloneError("not a valid workspace or child id")
    return [wid, fs._safe(child)]


def _walk(parts: list[str], create: bool = False) -> int:
    """A descriptor of the clones folder joined with `parts`, each folder opened from the one above it without
    following a link (and made, when `create`, with mode 0700). Raises OSError (ELOOP/ENOTDIR for a link)."""
    if create:
        root().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(os.path.normpath(root()), _DIR_FLAGS)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_clone(ws, child: str, rec: dict) -> tuple[int, int]:
    """(descriptor of the clone's folder, descriptor of its `.git`), both reached without following a link and both
    the very ones the record pins (device and inode). Raises CloneError with the reason."""
    try:
        pfd = _walk(_parent_parts(ws, child))
    except OSError as e:
        raise CloneError(f"a folder on the way to the clone of {child} is missing, a link or not a folder "
                         f"({type(e).__name__})") from None
    try:
        top = os.open("repo", _DIR_FLAGS, dir_fd=pfd)
    except FileNotFoundError:
        raise CloneError(f"the recorded clone of {child} is missing") from None
    except OSError as e:
        raise CloneError(f"the clone of {child} is a link or not a folder ({type(e).__name__})") from None
    finally:
        os.close(pfd)
    try:
        if _ino(os.fstat(top)) != rec["inode"]:
            raise CloneError(f"the clone of {child} is not the folder the runner made (another device or inode)")
        try:
            g = os.open(".git", _DIR_FLAGS, dir_fd=top)
        except OSError as e:
            raise CloneError(f"the clone's .git is a link or not a folder ({type(e).__name__})") from None
        if _ino(os.fstat(g)) != rec["git_inode"]:
            os.close(g)
            raise CloneError("the clone's .git is not the one the runner made (another device or inode)")
    except BaseException:
        os.close(top)
        raise
    return top, g


def _read_at(dfd: int, name: str, limit: int = 1 << 16) -> bytes | None:
    """The bytes of regular file `name` in folder `dfd`, opened without following a link; None otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dfd)
    except OSError:
        return None
    try:
        return os.read(fd, limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    finally:
        os.close(fd)


def _rmtree_fd(dfd: int, name: str, dev: int) -> None:
    """Remove folder `name` of folder `dfd` and everything in it, by descriptor: links are unlinked, never followed,
    and a folder on another device (a mount point inside) stops the removal with an error."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
    try:
        if os.fstat(fd).st_dev != dev:
            raise OSError(f"{name} is a mount point inside the clone: nothing below it is removed")
        with os.scandir(fd) as it:
            entries = list(it)
        for e in entries:
            if stat.S_ISDIR(e.stat(follow_symlinks=False).st_mode):
                _rmtree_fd(fd, e.name, dev)  # ponytail: recursion, fine for a repository's folder depth
            else:
                os.unlink(e.name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dfd)


def _rm_entry(dfd: int, name: str) -> None:
    """Remove entry `name` of folder `dfd` (a folder by _rmtree_fd, anything else unlinked); missing is fine."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        _rmtree_fd(dfd, name, st.st_dev)
    else:
        os.unlink(name, dir_fd=dfd)


def _tombstones(pfd: int) -> list[str]:
    return sorted(n for n in os.listdir(pfd) if n.startswith(".removing-"))


def _remove(ws, child: str, inode: str) -> None:
    """Delete the child's clone folder, which must be exactly the pinned one: under its parent's descriptor it is
    renamed to a tombstone name the runner chooses, the tombstone is checked again, and only then removed by
    descriptor. A swap puts it back and raises; a failure part way leaves the tombstone and raises (the caller keeps
    the record). Raises FileNotFoundError only when the folder is gone and nothing of it remains."""
    import secrets
    pfd = _walk(_parent_parts(ws, child))
    try:
        try:
            st = os.stat("repo", dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            left = _tombstones(pfd)
            if left:
                raise CloneError(f"a partly removed clone of {child} remains ({', '.join(left)}): remove it by "
                                 "hand") from None
            raise
        if not stat.S_ISDIR(st.st_mode) or _ino(st) != inode:
            raise CloneError(f"the clone of {child} is not the folder the runner made: nothing was removed")
        tomb = f".removing-{secrets.token_hex(8)}"
        os.rename("repo", tomb, src_dir_fd=pfd, dst_dir_fd=pfd)
        st = os.stat(tomb, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or _ino(st) != inode:
            os.rename(tomb, "repo", src_dir_fd=pfd, dst_dir_fd=pfd)
            raise CloneError(f"the clone of {child} was swapped while it was removed: nothing was removed")
        try:
            _rmtree_fd(pfd, tomb, st.st_dev)
        except OSError as e:
            raise CloneError(f"the clone of {child} was only partly removed ({e}); what is left is in {tomb} next "
                             "to it: remove it by hand") from None
    finally:
        os.close(pfd)


def _clone_lock(ws, child: str):
    """One lock per child's clone, shared by the runner (making or reusing it) and `clean`."""
    from filelock import FileLock
    d = fs._root() / "child-clones"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(str(d / f".{fs._key(ws, 'clone', child)}.lock"), timeout=LOCK_TIMEOUT)


def _case_insensitive() -> bool:
    """Whether the file system is case-insensitive, probed in a temporary folder of the runner's own (the guarded
    permits folder), never in the agent-written work tree."""
    import tempfile
    d = fs._root() / "child-clones"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d) as t:
        (Path(t) / "a").touch()
        return os.path.exists(Path(t) / "A")


# -- records ------------------------------------------------------------------------------------------------------

def _rec_path(ws, child: str) -> Path:
    return fs._root() / "child-clones" / f"{fs._key(ws, 'clone', child)}.json"


def _fail_path(ws, child: str) -> Path:
    return fs._root() / "child-clones" / f"{fs._key(ws, 'clone', child)}.failed"


def _cleaned_path(ws, child: str) -> Path:
    return fs._root() / "child-clones" / f"{fs._key(ws, 'clone', child)}.cleaned"


def ever_had_clone(ws, child) -> bool:
    """Whether the runner ever recorded a clone for the child, tried to make one, or removed one (`clean`): its
    record file (readable or not), its failure file or the mark `clean` leaves. The release then never reads the
    agent-written ticket fields in place of the record."""
    if not _key_ok(child):
        return False
    return any(os.path.lexists(p(ws, child)) for p in (_rec_path, _fail_path, _cleaned_path))


def record(ws, child) -> dict | None:
    """The runner's record of the child's clone, or None (none, or one that does not read back exactly: another path
    or branch than the derived ones, another workspace, a damaged file)."""
    from orch.core.factory_release import valid_branch
    from orch.core.ledger import workspace_id
    if not _key_ok(child):
        return None
    body = fs._read_json(_rec_path(ws, child))
    if (body is None or set(body) != _KEYS or not all(isinstance(v, str) for v in body.values())
            or body["workspace"] != workspace_id(ws) or body["child"] != child
            or body["path"] != str(clone_dir(ws, child)) or body["branch"] != branch_for(child)
            or not valid_branch(body["base"])):
        return None
    return body


def failure(ws, child) -> dict | None:
    """Why the runner could not prepare the child's clone last time ({child, why, at}), or None."""
    from orch.core.ledger import workspace_id
    if not _key_ok(child):
        return None
    body = fs._read_json(_fail_path(ws, child))
    if (body is None or set(body) != _FAIL_KEYS or not all(isinstance(v, str) for v in body.values())
            or body["workspace"] != workspace_id(ws) or body["child"] != child):
        return None
    return body


def _note_failure(ws, child: str, why: str) -> None:
    from orch.clock import stamp_s
    from orch.core.ledger import workspace_id
    try:
        fs._write_json(_fail_path(ws, child), {"workspace": workspace_id(ws), "child": child, "why": why[:400],
                                               "at": stamp_s()})
    except OSError:
        pass


def _fail(ws, child: str, why: str) -> str:
    _note_failure(ws, child, why)
    return why


def _clear(ws, child: str) -> None:
    try:
        _fail_path(ws, child).unlink()
    except OSError:
        pass


# -- the clone's git dir ------------------------------------------------------------------------------------------

def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_text(source: str, case_insensitive: bool) -> str:
    """The clone's `.git/config`, written by the runner at every use: nothing that runs a program or reads other
    config, and an `origin` that cannot be pushed to."""
    extra = "\tignorecase = true\n\tprecomposeunicode = true\n" if case_insensitive else ""
    return ("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
            "\thooksPath = /dev/null\n\tfsmonitor = false\n\tsymlinks = true\n\tprotectHFS = true\n"
            f"\tprotectNTFS = true\n{extra}"
            f"[remote \"origin\"]\n\turl = {_quote(source)}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            f"\tpushurl = {NO_PUSH}\n")


def _write_config_fd(g: int, source: str) -> None:
    """Write the clone's config and remove its hooks and info folders, all relative to the descriptor `g` of its
    `.git`: a temporary file created exclusively without following a link, then renamed over `config`."""
    import secrets
    text = config_text(source, _case_insensitive())
    tmp = f".config.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=g)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, "config", src_dir_fd=g, dst_dir_fd=g)
    for name in ("hooks", "info"):
        _rm_entry(g, name)


_ODD = (("objects/info/alternates", "borrows objects from another repository (objects/info/alternates)"),
        ("reftable", "keeps its refs in reftable, which orch cannot read"),
        ("shallow", "is a shallow clone (.git/shallow): history is missing"),
        ("commondir", "names another git dir (commondir): it is a linked worktree's git dir"),
        ("gitdir", "names another git dir (gitdir)"),
        ("info/grafts", "rewrites history with info/grafts"))


def _odd_fd(g: int) -> str | None:
    """Why the git dir open at `g` is one orch's clones do not take, or None: `objects` or `refs` missing, a link or
    not a folder; alternates, reftable, shallow, commondir, gitdir or grafts present; sha256 objects or reftable named
    in its config. Read by descriptor, links never followed."""
    def mode(name):
        try:
            return os.stat(name, dir_fd=g, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return None
    for name in ("objects", "refs"):
        m = mode(name)
        if m is None or not stat.S_ISDIR(m):
            return f"its {name} is missing, a link or not a folder"
    for name in ("objects/info", "info"):
        m = mode(name)
        if m is not None and not stat.S_ISDIR(m):
            return f"its {name} is a link or not a folder"
    for name, why in _ODD:
        if mode(name) is not None:
            return why
    raw = _read_at(g, "config")
    if raw is None:
        return "its config cannot be read"
    low = raw.decode("utf-8", "replace").casefold()
    if "objectformat" in low or "refstorage" in low:
        return "it uses sha256 objects or reftable refs, which orch's clones do not take"
    return None


def odd_repo(gitdir: Path) -> str | None:
    """Why the git dir at `gitdir` (the workspace's) is one orch's clones do not take, or None (see _odd_fd)."""
    try:
        g = os.open(str(gitdir), _DIR_FLAGS)
    except OSError:
        return f"{gitdir} is not a plain .git folder (the workspace is a linked worktree, or it is a link)"
    try:
        why = _odd_fd(g)
    finally:
        os.close(g)
    return f"{gitdir} {why}" if why else None


def base_branch(ws, src: Path) -> tuple[str | None, str]:
    """(the branch a new clone starts from, ""): the release recipe's base when a recipe exists, else the branch the
    workspace checkout's HEAD names; (None, why) otherwise (a damaged recipe, a detached HEAD)."""
    from orch.core import factory_release
    from orch.core.factory_runner import _NO_RECIPE
    from orch.core.fsutil import read_regular_file
    rec, why = factory_release.load(ws)
    if rec is not None:
        return rec["base"], ""
    if why != _NO_RECIPE:
        return None, "the release recipe cannot be loaded, so the base branch is not known"
    head = read_regular_file(src / ".git" / "HEAD", 4096)
    name = head[16:].decode("utf-8", "replace").strip() if head and head.startswith(b"ref: refs/heads/") else ""
    if not factory_release.valid_branch(name):
        return None, "the workspace checkout's HEAD names no plain branch (detached?), so the base is not known"
    return name, ""


def clonable(ws) -> tuple[bool | None, str]:
    """Whether the workspace's children get clones: (True, ""); (None, why) for a workspace that is not a git checkout
    or is a linked worktree (its children start in the workspace root and do not commit, as before clones); (False,
    why) when it is a checkout the runner cannot clone (readiness blocks with the reason)."""
    from orch.core.factory_release import workspace_repo
    src = workspace_repo(ws)
    if src is None:
        return None, "the workspace is not a git checkout"
    if os.path.isfile(src / ".git") and not os.path.islink(src / ".git"):
        return None, ("the workspace is a linked git worktree: its children start in the workspace root and do not "
                      "commit (clones are made only from a plain checkout)")
    why = odd_repo(src / ".git") or ("its path holds a line break" if "\n" in str(src) else None)
    if why:
        return False, why
    base, why = base_branch(ws, src)
    return (False, why) if base is None else (True, "")


# -- making, reusing, checking, removing ------------------------------------------------------------------------

def _git(git: str, ws, *args: str, cwd: str, timeout: float) -> dict:
    from orch.core import factory_release as fr
    flags = [*fr._git_flags({}), "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true"]
    return fr.run_command([git, *flags, *args], cwd, fr._git_env(ws, git), max(1, int(timeout)))


def _failed(r: dict, what: str, budget: float) -> str | None:
    from orch.core.factory_report import _text
    if r.get("timed_out"):
        return f"{what} did not finish within {int(budget)} seconds (a large repository?)"
    if r.get("code") != 0:
        return f"{what} failed (exit {r.get('code')}): {_text(r.get('err') or r.get('out'), 300)}"
    return None


def _reuse(ws, child: str, rec: dict) -> None:
    """Check the recorded clone (pinned folder and `.git`, a git dir orch takes) and write its config again, by
    descriptor. Raises CloneError."""
    top, g = _open_clone(ws, child, rec)
    try:
        why = _odd_fd(g)
        if why:
            raise CloneError(f"the clone of {child} {why}")
        _write_config_fd(g, rec["source"])
    finally:
        os.close(g)
        os.close(top)


def ensure(ws, actor, child: str, budget: float = CLONE_TIMEOUT) -> tuple[Path | None, str]:
    """Human only (the runner): the child's clone, made at its first launch and reused (config written again) after;
    (None, why) when it cannot be had. `budget`: seconds the clone and checkout may take together. Never deletes a
    clone or anything the runner did not just create."""
    from filelock import Timeout
    from orch.core import factory_runner
    fs.human_check(actor, "preparing a child's clone")
    if not _key_ok(child):
        return None, "not a ticket id"
    git = factory_runner.resolve_bin("git")
    if git is None or factory_runner.agent_writable(ws, git):
        return None, _fail(ws, child, "git was not found at a trusted path outside the workspace")
    try:
        with _clone_lock(ws, child):
            return _ensure(ws, child, git, budget)
    except Timeout:
        return None, "another process holds the lock of this child's clone"
    except (OSError, CloneError, UnicodeError) as e:
        return None, _fail(ws, child, f"the clone could not be prepared ({type(e).__name__}: {e})")


def _ensure(ws, child: str, git: str, budget: float) -> tuple[Path | None, str]:
    from orch.clock import stamp_s
    from orch.core.factory_release import ReleaseError, workspace_repo
    from orch.core.ledger import workspace_id
    dest = clone_dir(ws, child)
    rec = record(ws, child)
    if rec is not None:
        try:
            _reuse(ws, child, rec)
        except CloneError as e:
            return None, _fail(ws, child, f"{e}: look at it, or remove it with `orch factory clones clean {child}`")
        _clear(ws, child)
        return dest, ""
    ok, why = clonable(ws)
    if not ok:
        return None, _fail(ws, child, why)
    src = workspace_repo(ws)
    base, _ = base_branch(ws, src)
    try:
        pfd = _walk(_parent_parts(ws, child), create=True)
    except OSError as e:
        return None, _fail(ws, child, f"a folder on the way to the clone is a link or not a folder "
                                      f"({type(e).__name__}): nothing is written through it")
    try:
        try:
            os.mkdir("repo", 0o700, dir_fd=pfd)  # exclusive: this attempt owns exactly this folder
        except FileExistsError:
            return None, _fail(ws, child, f"{dest} exists but the runner has no record of it; nothing there was "
                                          "touched: move it away to let the runner make the clone")
        inode = _ino(os.stat("repo", dir_fd=pfd, follow_symlinks=False))
    finally:
        os.close(pfd)
    deadline = time.monotonic() + budget
    why, keep = "", False
    try:
        why = _failed(_git(git, ws, "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout",
                           "--no-recurse-submodules", "--template=", "--", str(src), str(dest),
                           cwd=str(dest.parent), timeout=budget), "git clone", budget)
        git_inode = ""
        if not why:
            pfd = _walk(_parent_parts(ws, child))
            try:
                top = os.open("repo", _DIR_FLAGS, dir_fd=pfd)
            finally:
                os.close(pfd)
            try:
                if _ino(os.fstat(top)) != inode:
                    raise CloneError(f"{dest} was replaced while the clone was made")
                g = os.open(".git", _DIR_FLAGS, dir_fd=top)
                try:
                    git_inode = _ino(os.fstat(g))
                    odd = _odd_fd(g)
                    if odd:
                        raise CloneError(f"the clone {odd}")
                    _write_config_fd(g, str(src))
                finally:
                    os.close(g)
            finally:
                os.close(top)
            left = deadline - time.monotonic()
            why = _failed(_git(git, ws, "-C", str(dest), "checkout", "--quiet", "--no-recurse-submodules", "-b",
                               branch_for(child), f"refs/remotes/origin/{base}", cwd=str(dest), timeout=left),
                          f"checking out {base}", budget) if left > 0 else \
                f"the clone did not finish within {int(budget)} seconds (a large repository?)"
        if not why:
            pinned = {"inode": inode, "git_inode": git_inode}
            top, g = _open_clone(ws, child, pinned)  # after git ran by path: still the folders this attempt made
            os.close(g)
            os.close(top)
            body = {"workspace": workspace_id(ws), "child": child, "path": str(dest), "branch": branch_for(child),
                    "base": base, "source": str(src), "inode": inode, "git_inode": git_inode, "at": stamp_s()}
            if fs._create(_rec_path(ws, child), body):
                keep = True
            else:
                why = "another runner recorded a clone for it meanwhile"
                keep = True  # not ours to judge: leave both as they are for the human
    except (CloneError, ReleaseError, OSError) as e:
        why = str(e) or type(e).__name__
    finally:
        if not keep:  # only the folder this attempt made, by its inode, never through a link
            try:
                _remove(ws, child, inode)
            except (OSError, CloneError, ValueError):
                pass
    if why:
        return None, _fail(ws, child, why)
    _clear(ws, child)
    return dest, ""


def verify(ws, child: str) -> tuple[Path | None, str]:
    """(the child's clone path, "") when its record names a clone that is still the pinned one, reached without a
    link, with a git dir orch takes; (None, why) otherwise. What the release checks before it fetches from it."""
    rec = record(ws, child)
    if rec is None:
        return None, f"the runner has no clone of {child}"
    try:
        top, g = _open_clone(ws, child, rec)
    except CloneError as e:
        return None, str(e)
    try:
        why = _odd_fd(g)
    finally:
        os.close(g)
        os.close(top)
    return (None, f"the clone of {child} {why}") if why else (Path(rec["path"]), "")


def fetch_from(ws, child: str, fetch):
    """fetch(path) for the release, under the child's clone lock, right after the clone is checked again (the pinned
    folder and `.git`, a git dir orch takes) and its config written again by the runner, so the upload-pack git runs
    in the clone reads only the runner's config. None when the clone cannot be had (no record, moved, locked)."""
    from filelock import Timeout
    try:
        with _clone_lock(ws, child):
            rec = record(ws, child)
            if rec is None:
                return None
            _reuse(ws, child, rec)
            return fetch(Path(rec["path"]))
    except (Timeout, OSError, CloneError, UnicodeError):
        return None


def own_clone(ws, path, child: str) -> str | None:
    """Why `path` is not the child's own runner-made clone, or None: the clone's folder (or its copy of the workspace
    folder) the runner's record names for exactly this child, reached without a link and still the pinned folder and
    `.git` (device and inode), a git dir orch takes (_odd_fd: no commondir, alternates, ...), HEAD on the branch the
    runner made for it, and that branch not a default branch (main, master, the recipe's base, what a remote's HEAD
    names, the base it was made from). Anything unreadable is a reason."""
    from orch.core import permits
    from orch.core.factory_runner import default_branches
    try:
        rec = record(ws, child)
        if rec is None:
            return f"it is not a clone the runner made for {child}"
        p = Path(str(path)).resolve()
        top = Path(rec["path"])
        if p not in {top.resolve(), start_in(ws, rec).resolve()}:
            return f"it is not {child}'s own clone"
        try:
            tfd, g = _open_clone(ws, child, rec)
        except CloneError as e:
            return str(e)
        try:
            why = _odd_fd(g)
            head = _read_at(g, "HEAD", 4096)
        finally:
            os.close(g)
            os.close(tfd)
        if why:
            return f"the clone {why}"
        if head is None or not head.startswith(b"ref: refs/heads/"):
            return "its HEAD is detached or cannot be read"
        branch = head[16:].decode("utf-8", "replace").strip()
        if branch != rec["branch"]:
            return f"its branch {permits.shown(branch)} is not the one the runner made for {child}"
        defaults = default_branches(ws, top / ".git")
        if defaults is None:
            return "the release recipe cannot be loaded, so the default branch is not known"
        if branch.casefold() in defaults | {rec["base"].casefold()}:
            return f"its branch {permits.shown(branch)} is a default branch"
    except (OSError, RuntimeError, ValueError) as e:
        return f"it cannot be read ({type(e).__name__})"
    return None


def listing(ws) -> list[dict]:
    """Every clone the runner recorded for this workspace: [{child, path, branch, base, at}]."""
    from orch.core.ledger import workspace_id
    try:
        names = sorted(os.listdir(fs._root() / "child-clones"))
    except OSError:
        return []
    out = []
    for n in names:
        body = fs._read_json(fs._root() / "child-clones" / n) if n.endswith(".json") else None
        if body is not None and body.get("workspace") == workspace_id(ws):
            rec = record(ws, body.get("child"))
            if rec is not None:
                out.append({k: rec[k] for k in ("child", "path", "branch", "base", "at")})
    return out


def _delegation_of(ws, child: str) -> str | None:
    from orch.core import permits, store
    try:
        t = store.read_ticket(store.resolve(ws, child).path)
        parent = t.meta.get("parent")
        epic = store.read_ticket(store.resolve(ws, str(parent)).path) if parent else None
        d = permits.factory_delegation(ws, epic) if epic is not None else None
    except Exception:
        return None
    return d["id"] if d else None


def clean(ws, actor, child: str) -> bool:
    """Human only: delete the child's clone and its record. True when there was one. The runner never calls it.
    Under the child's clone lock and its delegation's lock (the runner binds a session under it), refused while a
    session of the child is bound; the folder is re-verified right before the deletion (_remove: no link, the pinned
    inode, renamed to a tombstone and checked again). Otherwise nothing is removed and the record stays."""
    from contextlib import nullcontext
    from filelock import Timeout
    from orch.core import epics
    from orch.errors import UsageError, ValidationError
    fs.human_check(actor, "removing a child's clone")
    rec = record(ws, child)
    if rec is None:
        return False
    did = _delegation_of(ws, child)
    try:
        with _clone_lock(ws, child), (epics.delegation_lock(did) if did else nullcontext()):
            if any(b["child"] == child for b in fs.bindings(ws)):
                raise UsageError(f"a session of {child} runs in its clone: stop the run first")
            try:
                _remove(ws, child, rec["inode"])
            except FileNotFoundError:
                pass  # removed by hand already, nothing of it left: only the record goes
            except (OSError, CloneError) as e:
                raise ValidationError(f"the clone of {child} was not removed: {e}; the record stays") from None
            fs._write_json(_cleaned_path(ws, child), {"child": child})  # the release never falls back to the ticket
            _rec_path(ws, child).unlink()
            _clear(ws, child)
    except Timeout:
        raise UsageError(f"the runner is working on the clone of {child}: try again in a moment") from None
    return True
