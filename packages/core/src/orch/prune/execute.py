"""``orch host prune``, step 2: remove what the inventory found, in a fixed order, stopping at the first error.

Order: stop services and take their units away; a workspace's own git worktrees; a workspace's own files (or its whole
folder, only where the person said so); Claude Code trust entries; keychain items; state and release folders;
accounts and system files (as root, or the exact commands to print); finally the orch tool itself.

Every removal goes through :class:`~orch.prune.model.Fence`: an exact recorded path below a root orch owns, no link on
the way, never the home folder. Folders are removed with ``shutil.rmtree`` (it does not follow links); there is no glob
and no pattern anywhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from .inventory import OS_ROOT_PATHS, edit_text
from .model import Ctx, Fence, Inventory, Item, PruneError

__all__ = ["Result", "build_steps", "execute", "rm_tree"]

TRUST_KEYS = (
    "hasTrustDialogAccepted",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)
_ORDER = {"launcher": 0, "part": 1, "unit": 2}


class Result:
    def __init__(self) -> None:
        self.done: list[Item] = []
        self.failed: Item | None = None
        self.error: str = ""
        self.left: list[Item] = []
        self.admin: list[str] = []  # commands the person must run with administrator rights
        self.notes: list[str] = []


def _onexc(func: Callable[..., object], p: str, exc: BaseException) -> None:
    if not isinstance(exc, PermissionError):
        raise exc
    # read-only release copies (0555): give the folder back its owner write bit, then retry once
    with contextlib.suppress(OSError):
        os.chmod(os.path.dirname(p), 0o700)
    with contextlib.suppress(OSError):
        if os.path.isdir(p) and not os.path.islink(p):
            os.chmod(p, 0o700)
    func(p)


def rm_tree(path: Path) -> None:
    """Remove a file, a link (itself) or a folder tree. A link is never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    shutil.rmtree(path, onexc=_onexc)


def _write_atomic(path: Path, text: str) -> None:
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    fd, tmp = tempfile.mkstemp(prefix=".orch-prune-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def build_steps(inv: Inventory, delete_folders: set[Path]) -> list[Item]:
    steps: list[Item] = []
    svc = [i for i in inv.items if i.kind in _ORDER]
    steps += sorted(svc, key=lambda i: _ORDER[i.kind])
    for ws in inv.workspaces:
        wt = [i for i in ws.items if i.kind == "worktree"]
        steps += wt
        if ws.root in delete_folders:
            steps.append(Item("workspace", "folder", "the whole workspace folder", str(ws.root), ws.files, ws.size))
        else:
            steps += [i for i in ws.items if i.kind not in ("worktree", "note")]
    steps += [i for i in inv.items if i.kind in ("claude-trust", "keychain")]
    steps += [i for i in inv.items if i.kind == "tree"]
    steps += [Item("", "rmdirs", "empty folders orch made")]
    if any(i.kind == "os" for i in inv.items):
        steps.append(Item("", "os-batch", "accounts and system files"))
    steps += [i for i in inv.items if i.kind == "tool"]
    return steps


def execute(ctx: Ctx, inv: Inventory, steps: list[Item]) -> Result:
    res = Result()
    fence = Fence(inv.roots, inv.home)
    for n, step in enumerate(steps):
        try:
            _do(ctx, inv, fence, step, res)
        except PruneError as e:
            res.failed, res.error, res.left = step, str(e), steps[n + 1 :]
            return res
        except OSError as e:
            res.failed, res.error, res.left = step, f"{step.label}: {e}", steps[n + 1 :]
            return res
        res.done.append(step)
    return res


def _do(ctx: Ctx, inv: Inventory, fence: Fence, it: Item, res: Result) -> None:
    k = it.kind
    if k == "unit":
        _unit(ctx, res, fence, it)
    elif k == "part":
        _part(ctx, it)
    elif k == "launcher":
        entry = ctx.dashboard_entry() if ctx.dashboard_entry else _dashboard_entry(ctx)
        if entry:
            ctx.run([*entry, "stop"])  # best effort: the dashboard may already be gone
    elif k in ("tree", "folder"):
        p = fence.check(it.path)
        if k == "folder":
            _folder_guard(p, inv)
        rm_tree(p)
    elif k == "skill":
        _skill(fence, it)
    elif k == "edit":
        _edit(ctx, fence, it)
    elif k == "worktree":
        _worktree(ctx, fence, it)
    elif k == "claude-trust":
        _claude(fence, it)
    elif k == "keychain":
        handle = ctx.keychain(it.path) if ctx.keychain else None
        if handle is None:
            raise PruneError("the keychain cannot be opened any more")
        handle.delete(it.data["account"])
    elif k == "rmdirs":
        for d in inv.rmdirs:
            _rmdir_if_empty(fence, d)
    elif k == "os-batch":
        _os_batch(ctx, inv, res)
    elif k == "tool":
        if ctx.platform.startswith("win"):
            res.admin.append(
                "uv tool uninstall orch-core   (Windows cannot remove a running program: run this yourself)"
            )
        else:
            rc, out = ctx.run(["uv", "tool", "uninstall", "orch-core"])
            if rc != 0:
                raise PruneError(f"`uv tool uninstall orch-core` failed: {out.strip()[-300:]}")
    else:
        raise PruneError(f"internal: unknown step {k}")


def _dashboard_entry(ctx: Ctx) -> list[str] | None:
    from orch.cli.dashboard import find_entry_point

    return find_entry_point(ctx.env)


def _rmdir_if_empty(fence: Fence, d: Path) -> None:
    p = fence.check(d)
    with contextlib.suppress(OSError):
        if not os.path.islink(p):
            os.rmdir(p)


def _folder_guard(p: Path, inv: Inventory) -> None:
    if not any(ws.root == p and not ws.folder_refusal for ws in inv.workspaces):
        raise PruneError(f"refusing to delete {p}: it is not a workspace folder prune may delete")


def _loaded_from(ctx: Ctx, kind: str, name: str) -> str | None:
    """The unit file the service manager loaded ``name`` from, or ``None`` (not loaded, or it does not say)."""
    if kind == "launchd":
        rc, out = ctx.run(["launchctl", "print", f"gui/{ctx.uid}/{name}"])
        if rc != 0:
            return None
        for line in out.splitlines():
            key, _, value = line.strip().partition(" = ")
            if key == "path" and value.startswith("/"):
                return value
        return None
    rc, out = ctx.run(["systemctl", "--user", "show", name, "-p", "FragmentPath", "--value"])
    value = out.strip()
    return value if rc == 0 and value.startswith("/") else None


def _unit(ctx: Ctx, res: Result, fence: Fence, it: Item) -> None:
    path = fence.check(it.path)
    kind, name = it.data["kind"], it.data["name"]
    other = _loaded_from(ctx, kind, name)
    if other is not None and os.path.realpath(other) != os.path.realpath(path):
        # the same label is loaded from ANOTHER file (another install on this computer): leave that one running
        res.notes.append(f"{name} is loaded from {other}, not from {path}: left running; only {path} was removed")
    elif kind == "launchd":
        ctx.run(["launchctl", "bootout", f"gui/{ctx.uid}/{name}"])  # fails when it is not loaded: that is fine
    else:
        ctx.run(["systemctl", "--user", "disable", "--now", name])
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    if kind == "systemd":
        ctx.run(["systemctl", "--user", "daemon-reload"])


def _part(ctx: Ctx, it: Item) -> None:
    from orch.install.state import Layout

    from .inventory import install_runner

    try:
        install_runner(ctx, Layout(ctx.env), it.data.get("runner")).stop(it.data["part"])
    except Exception as e:  # noqa: BLE001 - the runner may be launchctl/systemctl/os.kill
        raise PruneError(f"could not stop the {it.data['part']} service: {e}") from None


def _skill(fence: Fence, it: Item) -> None:
    root = Path(it.data["root"])
    for rel in it.data["files"]:
        p = fence.check(root / rel)
        if os.path.lexists(p) and not os.path.isdir(p) or os.path.islink(p):
            os.unlink(p)
    base = root / it.data["rel"]
    dirs = []
    if os.path.isdir(base) and not os.path.islink(base):
        for dp, dn, _ in os.walk(base, followlinks=False):
            dirs.extend(Path(dp) / d for d in dn)
        dirs.sort(key=lambda d: -len(d.parts))
    for d in [*dirs, base, base.parent, base.parent.parent]:
        if d == root:
            continue
        with contextlib.suppress(OSError):
            os.rmdir(fence.check(d))


def _edit(ctx: Ctx, fence: Fence, it: Item) -> None:
    root, rel, what = Path(it.data["root"]), it.data["rel"], it.data["what"]
    p = fence.check(root / rel)
    if os.path.islink(p) or not os.path.isfile(p):
        return
    have = p.read_text(encoding="utf-8")
    agents_gone = not os.path.lexists(root / "AGENTS.md")
    new = edit_text(what, have, agents_gone=agents_gone)
    if new is None:
        return
    if new == "":
        if what == "gitignore":
            rc, _ = ctx.run(["git", "-C", str(root), "ls-files", "--error-unmatch", rel])
            if rc == 0:  # tracked by git: keep the (now empty) file, a deletion would show as a change
                _write_atomic(p, "")
                return
        os.unlink(p)
    else:
        _write_atomic(p, new)


def _worktree(ctx: Ctx, fence: Fence, it: Item) -> None:
    path, declared = Path(it.data["path"]), it.data["declared"]
    p = fence.check(path)
    if os.path.lexists(p):
        rc, out = ctx.run(["git", "-C", declared, "worktree", "remove", "--force", str(p)])
        if rc != 0:
            if os.path.isdir(declared):
                raise PruneError(f"git could not remove the worktree {p}: {out.strip()[-200:]}")
            rm_tree(p)  # the repository is gone: nothing to unregister
    elif os.path.isdir(declared):
        ctx.run(["git", "-C", declared, "worktree", "prune"])
    parent = p.parent
    if parent.name.endswith(".worktrees"):
        with contextlib.suppress(OSError):
            os.rmdir(parent)  # only when empty


def _claude(fence: Fence, it: Item) -> None:
    cfg = fence.check(it.data["cfg"])
    if os.path.islink(cfg) or not os.path.isfile(cfg):
        return
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        return
    changed = False
    for key in it.data["keys"]:
        entry = projects.get(key)
        if not isinstance(entry, dict):
            continue
        for tk in TRUST_KEYS:
            if tk in entry:
                del entry[tk]
                changed = True
        if not entry:
            del projects[key]
    if changed:
        _write_atomic(cfg, json.dumps(doc, indent=2) + "\n")


_BIN = {"systemctl": "/usr/bin/systemctl", "userdel": "/usr/sbin/userdel", "groupdel": "/usr/sbin/groupdel"}


def _os_batch(ctx: Ctx, inv: Inventory, res: Result) -> None:
    if ctx.euid != 0:
        res.admin.extend(f"sudo {c}" for c in inv.root_commands)
        res.notes.append("accounts and system files need an administrator: run the commands printed below")
        return
    allowed = set(OS_ROOT_PATHS.get("linux", ()) + OS_ROOT_PATHS.get("darwin", ()))
    for cmd in inv.root_commands:
        words = cmd.split(" ", 1) if cmd.startswith("rm -rf ") else cmd.split()
        if cmd.startswith("rm -rf "):
            target = cmd[len("rm -rf ") :]
            if target not in allowed:
                raise PruneError(f"refusing to remove {target}: not a file orch's setup makes")
            rm_tree(Path(target))
            continue
        exe = _BIN.get(words[0])
        if exe is None:
            raise PruneError(f"internal: unknown command {cmd}")
        rc, out = ctx.run([exe, *words[1:]])
        if rc != 0 and words[0] != "groupdel" and "disable" not in cmd:
            raise PruneError(f"`{cmd}` failed: {out.strip()[-200:]}")
