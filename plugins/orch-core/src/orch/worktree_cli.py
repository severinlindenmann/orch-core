"""``orch worktree`` in POSIX direct mode (H11d, D-P2-38, §12.7): the same worktrees as the host's ``worktree.*`` ops,
for a workspace no host serves.

    orch worktree list   [TICKET] [--check] [--json]
    orch worktree create TICKET [--repo NAME] [--json]
    orch worktree remove TICKET [--repo NAME] [--force] [--json]

The same safety contract as the host: one worktree per (ticket, linked repo) at ``<repo path>.worktrees/<WS8>-<KEY>`` on
the ticket's current ``links.branches`` branch (``worktree.no_branch`` without one); git runs only through the runner's own helpers in :mod:`orch.runner.wtgit`
(:func:`~orch.runner.wtgit.run_git` for add and remove, :func:`~orch.runner.wtgit.read_git` for rev-parse and status:
hooks, fsmonitor and network off, a from-scratch environment, 30 s, no symlink followed under ``<repo>.worktrees``);
nothing here calls ``orch.store.observe``, so a repo's own config, hooks or fsmonitor never run; the new worktree is pinned in process (:func:`orch.gitread.pinning.pin_repo`: ``O_NOFOLLOW``
walks, git's layout checked exactly) and registered with a new generation in ``<state>/hosts/<ws>/git-pins.json``, the
file a host reads when the workspace is registered later. A clean tree is required to remove (``--force`` skips it; in
direct mode the person is at the terminal and could ``rm -rf`` it anyway); the branch is never deleted.

The ownership lock is held **shared** for the whole command and the registration is checked under it, so the tool never
works underneath a host (``host.required``), as the records tool (:mod:`orch.records.direct`). Windows answers
``unsupported.platform`` (exit 3): no reparse-point-safe resolution yet. A call under ``ORCH_GRANT`` is refused
(``op.not_allowed``): agents ask the host's ``worktree.*`` ops, never this person-side tool.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from orch.runner.wtgit import read_git, tree_dirty

__all__ = ["main"]

_USAGE = "usage: orch worktree {list [TICKET] [--check] | create TICKET [--repo NAME] | remove TICKET [--repo NAME] [--force]} [--json]"


class WtError(Exception):
    def __init__(self, code: str, message: str, exit_: int = 3, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.exit, self.hint = code, message, exit_, hint


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _parse(argv: Sequence[str]) -> tuple[str, dict[str, Any]]:
    if not argv or argv[0] not in ("list", "create", "remove"):
        raise WtError("invalid.input", _USAGE, 2)
    cmd, rest = argv[0], list(argv[1:])
    a: dict[str, Any] = {"json": False, "check": False, "force": False, "repo": None, "ticket": None}
    it = iter(rest)
    for tok in it:
        if tok == "--json":
            a["json"] = True
        elif tok == "--check" and cmd == "list":
            a["check"] = True
        elif tok == "--force" and cmd == "remove":
            a["force"] = True
        elif tok == "--repo" and cmd != "list":
            a["repo"] = next(it, None)
            if not a["repo"]:
                raise WtError("invalid.input", "--repo needs a name", 2)
        elif tok.startswith("-") or a["ticket"] is not None:
            raise WtError("invalid.input", _USAGE, 2)
        else:
            a["ticket"] = tok
    if cmd != "list" and a["ticket"] is None:
        raise WtError("invalid.input", _USAGE, 2)
    return cmd, a


class Direct:
    """One direct-mode workspace: the store (read-only, under the shared ownership lock), the pin file and the repos."""

    def __init__(self, root: Path, wid: str, state: Path, env: Mapping[str, str], clock: Any = time.time) -> None:
        self.root, self.wid, self.state, self.env, self.clock = Path(root), wid, Path(state), env, clock
        from orch.host.ownership import DirectGuard
        from orch.ops.caps import Git

        self.guard = DirectGuard(self.state, wid)
        self.git = Git(enabled=True)
        self._store: Any = None
        self._pins: Any = None

    # -- opening
    def served(self) -> bool:
        from orch.host.service import SERVED_MARKER

        return os.path.lexists(self.root / ".state" / SERVED_MARKER)

    def store(self) -> Any:
        if self._store is None:
            from orch.ops.errors import OrchError
            from orch.store import Store, StoreError

            try:
                self._store = Store.open(self.root, expected_workspace_id=self.wid, host=None,
                                         host_state_dir=self.state, load="lazy", guard=self.guard)  # fmt: skip
            except StoreError as exc:
                raise WtError(exc.code, exc.detail or exc.code) from None
            except OrchError as exc:
                raise WtError(exc.code, str(exc.message), hint=exc.hint) from None
        return self._store

    def pins(self) -> Any:
        if self._pins is None:
            from orch.gitread.client import PinStore

            d = self.state / "hosts" / self.wid
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self._pins = PinStore(d / "git-pins.json")
            except (ValueError, OSError) as exc:
                raise WtError("observe.unavailable", f"the pin file cannot be read ({exc})") from None
        return self._pins

    def close(self) -> None:
        if self._store is not None:
            with contextlib.suppress(Exception):
                self._store.close()
            self._store = None

    # -- the checks every command shares
    def guarded(self) -> contextlib.AbstractContextManager[None]:
        return self.guard()

    def _gate(self) -> None:
        from orch.ops.errors import OrchError

        if self.served():
            raise WtError("host.required", "a host serves this workspace; direct-mode worktrees are off",
                          hint="use the dashboard's Worktrees page, or orch host unregister")  # fmt: skip
        try:
            self.guard.check()
        except OrchError as exc:
            raise WtError(exc.code, str(exc.message), hint=exc.hint) from None

    def _ticket(self, ref: str) -> Any:
        view = self.store().ticket(ref)
        if view is None:
            raise WtError("not_found", f"no such ticket: {ref}")
        return view

    @staticmethod
    def _repo_of(view: Any, repo: str | None) -> str:
        linked = list((view.fields.get("links") or {}).get("repos") or [])
        if repo is None:
            if len(linked) != 1:
                raise WtError("invalid.input", "name the repo with --repo: the ticket links "
                              + ("no repo" if not linked else "several repos"))  # fmt: skip
            return linked[0]
        if repo not in linked:
            raise WtError("not_found", "no such repo on this ticket")
        return repo

    def _declared(self, repo: str) -> tuple[str, Any]:
        from orch.gitread.errors import Unavailable
        from orch.gitread.pinning import pin_repo

        path = self.git.repo_path(self.root, self.store().state.workspace.repos, repo)
        if path is None:
            raise WtError("not_found", f"{repo} is not declared in settings.repos")
        real = os.path.realpath(path)
        try:
            return real, pin_repo(real, None)
        except Unavailable as exc:
            raise WtError("observe.unavailable", f"{repo}: {exc.reason}"[:300]) from None

    def _own(self) -> dict[str, str]:
        return {k: v for k, v in self.env.items() if k in ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME")}

    def _rev(self, repo: str, rev: str) -> str | None:
        """A commit id through the hardened reader (never ``observe.git``: the repo's own config must run nothing)."""
        got = read_git(repo, ["rev-parse", "--verify", "-q", rev + "^{commit}"], self._own())
        return got if got and len(got) in (40, 64) else None

    def _git(self, args: dict[str, Any]) -> None:
        from orch.runner.supervise import SpawnError
        from orch.runner.wtgit import run_git

        try:
            run_git(args, self._own())
        except SpawnError as exc:
            code = exc.code if exc.code.startswith("worktree.") or exc.code == "launch.cwd_changed" else "worktree.git_failed"
            raise WtError(code, exc.message, 1 if code == "worktree.git_failed" else 3) from None

    # -- the commands
    def row(self, view: Any, repo: str, slot: dict[str, Any], observed: dict[str, Any] | None = None) -> dict[str, Any]:
        from orch.host.worktrees import _row

        return _row(view, repo, slot, observed)

    def list(self, ref: str | None, check: bool) -> list[dict[str, Any]]:
        self._gate()
        store, pins = self.store(), self.pins()
        wanted = self._ticket(ref).uid if ref else None
        rows = []
        for uid, repo, slot in pins.worktrees():
            if wanted is not None and uid != wanted:
                continue
            view = store.ticket(uid)
            if view is None:
                continue
            rows.append(self.row(view, repo, slot, self._observe(slot) if check else None))
        return rows

    def _observe(self, slot: dict[str, Any]) -> dict[str, Any]:
        reg = slot["reg"]
        out: dict[str, Any] = {"checked_at": _iso(self.clock())}
        try:
            st = os.lstat(reg["path"])
        except FileNotFoundError:
            return {**out, "state": "missing"}
        except OSError:
            return {**out, "state": "moved"}
        pin = (reg.get("pins") or {}).get("worktree")
        if pin is None:
            return out
        if (st.st_dev, st.st_ino) != (pin["dev"], pin["ino"]):
            return {**out, "state": "moved"}
        out["head"] = self._rev(reg["path"], "HEAD")
        d = tree_dirty(reg["path"], self._own())
        out["dirty"] = None if d is None else len([ln for ln in d.splitlines() if ln])
        return out

    def create(self, ref: str, repo_arg: str | None) -> dict[str, Any]:
        from orch.gitread.errors import Unavailable
        from orch.gitread.fs import open_pin
        from orch.gitread.pinning import pin_repo
        from orch.gitread.refs import read_head
        from orch.host.worktrees import MAX_REGISTERED, worktree_path
        self._gate()
        view = self._ticket(ref)
        repo = self._repo_of(view, repo_arg)
        pins = self.pins()
        slot = pins.worktree(view.uid, repo)
        if slot["reg"] is not None:
            raise WtError("worktree.exists", f"{view.key} already has a worktree for {repo}")
        branch = ((view.fields.get("links") or {}).get("branches") or {}).get(repo)
        if not branch:
            raise WtError("worktree.no_branch", f"{view.key} links no branch for {repo}; set links.branches first")
        if len(pins.worktrees()) >= MAX_REGISTERED:
            raise WtError("quota.host", f"at most {MAX_REGISTERED} worktrees are registered per workspace", 9)
        for uid, r, other in pins.worktrees():
            if r == repo and other["reg"]["branch"] == branch and uid != view.uid:
                raise WtError("worktree.branch_in_use", f"{branch} is checked out in another ticket's worktree")
        declared, dpins = self._declared(repo)
        path = worktree_path(declared, self.wid, view.key)
        if os.path.lexists(path) and os.path.islink(path):
            raise WtError("worktree.exists", "the worktree path is a symlink")
        start = None
        if self._rev(declared, f"refs/heads/{branch}") is None:
            spec = self.store().state.workspace.repos.get(repo) or {}
            default = spec.get("default_branch") or "main"
            start = self._rev(declared, f"refs/heads/{default}")
            if not start:
                raise WtError("worktree.no_branch", f"{branch} does not exist and there is no default commit to start it at")
        self._git({"op": "worktree.add", "repo_pin": dpins.worktree.to_json(), "path": path, "branch": branch,
                   "base": start, "force": False, "generation": slot["gen"] + 1})  # fmt: skip
        pinned, reason = None, None
        try:
            got = pin_repo(path, [path, declared])
            if (got.commondir.dev, got.commondir.ino) != (dpins.commondir.dev, dpins.commondir.ino):
                raise Unavailable("the worktree belongs to another repository than the declared one")
            with open_pin(got.gitdir) as gd:
                if read_head(gd) != "refs/heads/" + branch:
                    raise Unavailable("the worktree's HEAD is not the ticket's branch")
            pinned = got.to_json()
        except Unavailable as exc:
            reason = exc.reason
        reg = {"path": path, "branch": branch, "pins": pinned, "created_at": _iso(self.clock()),
               "created_by": {"kind": "person", "id": f"direct:{os.getuid()}"}, "idem": None}  # fmt: skip
        try:
            pins.set_worktree(view.uid, repo, reg, expect=slot["gen"])
        except (ValueError, OSError) as exc:
            raise WtError("worktree.unpinned", f"the worktree was created but could not be registered ({exc})") from None
        if reason is not None:
            raise WtError("worktree.unpinned", f"the worktree was created but could not be pinned: {reason}"[:400])
        return self.row(view, repo, pins.worktree(view.uid, repo))

    def remove(self, ref: str, repo_arg: str | None, force: bool) -> dict[str, bool]:
        self._gate()
        view = self._ticket(ref)
        repo = self._repo_of(view, repo_arg)
        pins = self.pins()
        slot = pins.worktree(view.uid, repo)
        reg = slot["reg"]
        if reg is None:
            raise WtError("worktree.none", f"{view.key} has no worktree for {repo}")
        if os.path.lexists(reg["path"]) and not force:
            dirty = tree_dirty(reg["path"], self._own())
            if dirty is None or dirty.strip():
                raise WtError("worktree.dirty", "the worktree has uncommitted changes (or cannot be checked); "
                              "commit them, or remove with --force")  # fmt: skip
        _declared, dpins = self._declared(repo)
        self._git({"op": "worktree.remove", "repo_pin": dpins.worktree.to_json(), "path": reg["path"],
                   "branch": reg["branch"], "base": None, "force": force, "generation": slot["gen"] + 1})  # fmt: skip
        try:
            pins.set_worktree(view.uid, repo, None, expect=slot["gen"])
        except (ValueError, OSError) as exc:
            raise WtError("internal", f"the worktree was removed but its registration could not be dropped ({exc})",
                          1) from None  # fmt: skip
        return {"removed": True}


def _text(cmd: str, data: Any) -> str:
    if cmd == "remove":
        return "removed"
    rows = data if cmd == "list" else [data]
    if not rows:
        return "no worktrees"
    return "\n".join(f"{r['ticket']} {r['repo']} {r['branch']} {r['state']} gen={r['generation']} {r['path']}" for r in rows)


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO | None = None, err: TextIO | None = None,
         now: Any = time.time) -> int:  # fmt: skip
    out = out if out is not None else sys.stdout
    as_json = "--json" in argv
    direct: Direct | None = None

    def fail(exc: WtError) -> int:
        if as_json:
            doc = {"ok": False, "code": exc.code, "message": exc.message}
            if exc.hint:
                doc["hint"] = exc.hint
            out.write(json.dumps(doc) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n" + (f"hint: {exc.hint}\n" if exc.hint else ""))
        return exc.exit

    try:
        if os.name == "nt":
            raise WtError("unsupported.platform", "orch worktree needs POSIX filesystem semantics (D-P2-38)")
        if env.get("ORCH_GRANT"):
            raise WtError("op.not_allowed", "agents use the host's worktree ops; this tool is for the person")
        cmd, a = _parse(argv)
        from orch.ops.caps import Files
        from orch.ops.runtime import _config, find_workspace, state_dir

        files = Files.direct()
        root = find_workspace(env, os.getcwd(), files)
        cfg = _config(root, files) if root is not None else None
        wid = ((cfg or {}).get("workspace") or {}).get("id")
        if root is None or not isinstance(wid, str):
            raise WtError("not_found", "no orch workspace here")
        direct = Direct(Path(root), wid, state_dir(env, files), env, now)
        with direct.guarded():
            if cmd == "list":
                data: Any = direct.list(a["ticket"], a["check"])
            elif cmd == "create":
                data = direct.create(a["ticket"], a["repo"])
            else:
                data = direct.remove(a["ticket"], a["repo"], a["force"])
        out.write((json.dumps({"ok": True, "data": data}) if as_json else _text(cmd, data)) + "\n")
        return 0
    except WtError as exc:
        return fail(exc)
    except Exception as exc:  # the guard's host.required (an OrchError) when a host registered the workspace meanwhile
        from orch.ops.errors import OrchError

        if isinstance(exc, OrchError):
            return fail(WtError(exc.code, str(exc.message), hint=exc.hint))
        raise
    finally:
        if direct is not None:
            direct.close()
