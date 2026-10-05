"""AI Factory: one separate clone per child, owned by the runner (docs/factory.md, "Per-child clones").

The runner (the dashboard process the human started) gives each child that has no linked worktree of its own a clone of
the workspace's repository, outside the workspace and outside the orch config dir, at
`<orch config dir>-clones/<workspace id>/<child id>/repo`, on a branch of its own (`fx/<child id>`). The child's
session starts there and commits there; the release step fetches that branch from the clone.

- **Where it lives.** Not in the permits folder: the guard refuses every tool and command that reaches the orch config
  dir (a session started inside it could not run one command), and the clone's work tree must be the session's to write.
  Its `.git` stays protected by the guard's `.git` rules (file tools never write a `.git` component, shell writes into
  `.git` files are denied), as in every checkout.
- **What is recorded.** child -> (clone path, branch, base, source) in `permits/child-clones/` of the orch config dir,
  written only by the runner, guarded like the rest of the permits folder. The path is derived from the validated
  workspace and child ids, never from ticket text; a record whose path or branch is not the derived one does not count.
- **How it is made.** `git clone --local --no-hardlinks --no-checkout --no-recurse-submodules --template=` by argv, with
  the release step's git isolation (no user or system git config, hooks and fsmonitor off, no replace objects, an
  empty home), then a config the runner writes (no includes, aliases, filters, hooks or fsmonitor; `origin` is the
  workspace path with a push URL that cannot work) and a checkout of the base's tip onto the child's branch, no
  submodules. `--local` copies the source's objects and refs (no upload-pack runs, no source config, hook or attribute
  is used); a source with `objects/info/alternates` or reftable refs is refused, because the copy would keep reading
  objects from wherever they point.
- **Lifecycle.** Made at the child's first launch, reused (its config written again) at every later one; never deleted
  by the runner. A folder at the clone's path that the runner has no record of is never touched: the child is not
  started and the run view says why. A clone attempt that fails removes only what that attempt created.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from orch.core import factory_sessions as fs

CLONE_TIMEOUT = 300  # seconds for one git clone or checkout: a large repository fails clearly instead of hanging
_KEYS = {"workspace", "child", "path", "branch", "base", "source", "inode", "at"}
_FAIL_KEYS = {"workspace", "child", "why", "at"}
NO_PUSH = "/dev/null/orch-runner-never-pushes"


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


# -- paths below the clones folder: never through a link ------------------------------------------------------------
# The clones folder lies outside the guarded permits folder and agents can write it: a link planted anywhere from the
# clones folder down to a clone would point the runner's writes and deletes elsewhere. Every component is checked
# without following links, a clone is pinned by device and inode in the runner's record, and a removal walks the
# folders by descriptor (O_NOFOLLOW) and deletes relative to the last one.
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _parts(path: Path) -> list[str]:
    """The components of `path` below the clones folder; raises ValueError for a path outside it or one with `..`."""
    rel = Path(os.path.normpath(path)).relative_to(os.path.normpath(root()))
    if not rel.parts or any(p in ("..", ".", "") for p in rel.parts) or ".." in Path(path).parts:
        raise ValueError("not a path below the clones folder")
    return list(rel.parts)


def link_on_path(path: Path) -> str | None:
    """Why the clones folder or a component of `path` below it is a link or not a folder (a missing tail is fine),
    or None. Never follows a link."""
    import stat
    try:
        cur = Path(os.path.normpath(root()))
        for part in [None, *_parts(path)]:
            cur = cur if part is None else cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(st.st_mode):
                return f"{cur} is a link or not a folder: nothing is written or removed through it"
    except (OSError, ValueError) as e:
        return f"{path} cannot be checked ({type(e).__name__})"
    return None


def _open_parent(path: Path) -> int:
    """A descriptor of `path`'s parent folder, reached from the clones folder one component at a time, never through
    a link (O_NOFOLLOW): a link swapped in after a check makes the open fail."""
    parts = _parts(path)
    fd = os.open(os.path.normpath(root()), _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _inode(path: Path) -> str | None:
    """"device:inode" of the folder `path`, read through _open_parent (no link followed), or None."""
    import stat
    try:
        fd = _open_parent(path)
    except (OSError, ValueError):
        return None
    try:
        st = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(fd)
    return f"{st.st_dev}:{st.st_ino}" if stat.S_ISDIR(st.st_mode) else None


def _remove(path: Path, inode: str | None = None) -> None:
    """Delete the folder `path` below the clones folder: reached by descriptor without following a link, a folder
    (not a link) and, with `inode`, exactly the one recorded; removed relative to its parent's descriptor (rmtree
    with dir_fd never follows a link inside). Raises OSError (or ValueError) and removes nothing otherwise."""
    import stat
    fd = _open_parent(path)
    try:
        st = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{path} is not a folder")
        if inode is not None and f"{st.st_dev}:{st.st_ino}" != inode:
            raise OSError(f"{path} is not the clone the runner made")
        shutil.rmtree(Path(path).name, dir_fd=fd)
    finally:
        os.close(fd)


def _rec_path(ws, child: str) -> Path:
    return fs._root() / "child-clones" / f"{fs._key(ws, 'clone', child)}.json"


def _fail_path(ws, child: str) -> Path:
    return fs._root() / "child-clones" / f"{fs._key(ws, 'clone', child)}.failed"


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


def _write_config(dest: Path, source: str) -> None:
    from orch.core.factory_release import _atomic
    why = link_on_path(dest / ".git")
    if why:
        raise OSError(why)
    git = dest / ".git"
    _atomic(git / "config", config_text(source, os.path.exists(dest / ".GIT")))
    shutil.rmtree(git / "hooks", ignore_errors=True)


def _odd_repo(gitdir: Path) -> str | None:
    """Why a repository's git dir is one orch's clones do not take (objects from elsewhere, reftable refs), or None."""
    if os.path.islink(gitdir) or not gitdir.is_dir():
        return f"{gitdir} is not a plain .git folder (a linked worktree or a link)"
    if os.path.lexists(gitdir / "objects" / "info" / "alternates"):
        return f"{gitdir} borrows objects from another repository (objects/info/alternates)"
    if os.path.lexists(gitdir / "reftable"):
        return f"{gitdir} keeps its refs in reftable, which orch cannot read"
    return None


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


def _git(git: str, ws, *args: str, cwd: str) -> dict:
    from orch.core import factory_release as fr
    flags = [*fr._git_flags({}), "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true"]
    return fr.run_command([git, *flags, *args], cwd, fr._git_env(ws, git), CLONE_TIMEOUT)


def _failed(r: dict, what: str) -> str | None:
    from orch.core.factory_report import _text
    if r.get("timed_out"):
        return f"{what} did not finish within {CLONE_TIMEOUT} seconds (a large repository?)"
    if r.get("code") != 0:
        return f"{what} failed (exit {r.get('code')}): {_text(r.get('err') or r.get('out'), 300)}"
    return None


def ensure(ws, actor, child: str) -> tuple[Path | None, str]:
    """Human only (the runner): the child's clone, made at its first launch and reused (config written again) after;
    (None, why) when it cannot be had. Never deletes a clone or anything the runner did not just create."""
    from orch.clock import stamp_s
    from orch.core import factory_runner
    from orch.core.factory_release import ReleaseError, workspace_repo
    from orch.core.ledger import workspace_id
    fs.human_check(actor, "preparing a child's clone")
    if not _key_ok(child):
        return None, "not a ticket id"
    dest = clone_dir(ws, child)
    git = factory_runner.resolve_bin("git")
    if git is None or factory_runner.agent_writable(ws, git):
        return None, _fail(ws, child, "git was not found at a trusted path outside the workspace")
    rec = record(ws, child)
    try:
        if rec is not None:
            why = link_on_path(dest) or (
                (_odd_repo(dest / ".git") or (None if _inode(dest) == rec["inode"] else
                                              f"{dest} is not the folder the runner made (another device or inode)"))
                if os.path.isdir(dest) else f"its recorded clone {dest} is missing")
            if why:
                return None, _fail(ws, child, why + ": look at it, or remove the record's folder by hand")
            _write_config(dest, rec["source"])
            _clear(ws, child)
            return dest, ""
        why = link_on_path(dest)
        if why:
            return None, _fail(ws, child, why)
        if os.path.lexists(dest):
            return None, _fail(ws, child, f"{dest} exists but the runner has no record of it; nothing there was "
                                          "touched: move it away to let the runner make the clone")
        src = workspace_repo(ws)
        if src is None:
            return None, _fail(ws, child, "the workspace is not a git checkout")
        why = _odd_repo(src / ".git") or ("its path holds a line break" if "\n" in str(src) else None)
        if why:
            return None, _fail(ws, child, why)
        base, why = base_branch(ws, src)
        if base is None:
            return None, _fail(ws, child, why)
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        why = link_on_path(dest)
        if why:
            return None, _fail(ws, child, why)
        made = True
        try:
            why = _failed(_git(git, ws, "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout",
                               "--no-recurse-submodules", "--template=", "--", str(src), str(dest),
                               cwd=str(dest.parent)), "git clone")
            if not why:
                raw = (dest / ".git" / "config").read_text(encoding="utf-8", errors="replace").casefold()
                why = _odd_repo(dest / ".git") or (
                    "the repository uses sha256 objects or reftable refs, which orch's clones do not take"
                    if "objectformat" in raw or "refstorage" in raw else None)
            if not why:
                _write_config(dest, str(src))
                why = _failed(_git(git, ws, "-C", str(dest), "checkout", "--quiet", "--no-recurse-submodules", "-b",
                                   branch_for(child), f"refs/remotes/origin/{base}", cwd=str(dest)),
                              f"checking out {base}")
            inode = None if why or link_on_path(dest) else _inode(dest)
            if not why and inode is None:
                why = f"{dest} or a folder above it was replaced while the clone was made"
            if not why and not fs._create(_rec_path(ws, child), {
                    "workspace": workspace_id(ws), "child": child, "path": str(dest), "branch": branch_for(child),
                    "base": base, "source": str(src), "inode": inode, "at": stamp_s()}):
                why = "another runner recorded a clone for it meanwhile"
            made = bool(why)
        finally:
            if made:  # only what this attempt created, never through a link
                try:
                    _remove(dest)
                except (OSError, ValueError):
                    pass
        if why:
            return None, _fail(ws, child, why)
        _clear(ws, child)
        return dest, ""
    except (OSError, ReleaseError, UnicodeError) as e:
        return None, _fail(ws, child, f"the clone could not be prepared ({type(e).__name__})")


def _fail(ws, child: str, why: str) -> str:
    _note_failure(ws, child, why)
    return why


def _clear(ws, child: str) -> None:
    try:
        _fail_path(ws, child).unlink()
    except OSError:
        pass


def own_clone(ws, path, child: str) -> str | None:
    """Why `path` is not the child's own runner-made clone, or None: the folder the runner's record names for exactly
    this child, its `.git` a plain folder (no alternates, no reftable), HEAD on the branch the runner made for it, and
    that branch not a default branch (main, master, the recipe's base, what a remote's HEAD names, the base it was
    made from). Read from files only; anything unreadable is a reason."""
    from orch.core import permits
    from orch.core.factory_runner import default_branches
    from orch.core.fsutil import read_regular_file
    try:
        rec = record(ws, child)
        if rec is None:
            return f"it is not a clone the runner made for {child}"
        p = Path(str(path)).resolve()
        if p != Path(rec["path"]).resolve():
            return f"it is not {child}'s own clone"
        why = link_on_path(Path(rec["path"]))
        if why:
            return why
        if _inode(Path(rec["path"])) != rec["inode"]:
            return f"it is not the folder the runner made for {child} (another device or inode)"
        dot = p / ".git"
        why = _odd_repo(dot)
        if why:
            return why
        head = read_regular_file(dot / "HEAD", 4096)
        if head is None or not head.startswith(b"ref: refs/heads/"):
            return "its HEAD is detached or cannot be read"
        branch = head[16:].decode("utf-8", "replace").strip()
        if branch != rec["branch"]:
            return f"its branch {permits.shown(branch)} is not the one the runner made for {child}"
        defaults = default_branches(ws, dot)
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


def clean(ws, actor, child: str) -> bool:
    """Human only: delete the child's clone and its record. True when there was one. The runner never calls it. The
    folder is re-verified right before the deletion: reached from the clones folder without following a link, a
    folder, and the very one recorded (device and inode); otherwise nothing is removed and the record stays."""
    from orch.errors import ValidationError
    fs.human_check(actor, "removing a child's clone")
    rec = record(ws, child)
    if rec is None:
        return False
    path = Path(rec["path"])
    why = link_on_path(path)
    if why:
        raise ValidationError(f"{why}; nothing was removed")
    try:
        _remove(path, rec["inode"])
    except FileNotFoundError:
        pass  # removed by hand already: only the record goes
    except (OSError, ValueError) as e:
        raise ValidationError(f"the clone of {child} was not removed: {e}") from None
    _rec_path(ws, child).unlink()
    _clear(ws, child)
    return True
