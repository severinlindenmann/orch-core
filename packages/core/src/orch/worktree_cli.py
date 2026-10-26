"""``orch worktree`` in POSIX direct mode (H11d, D-P2-38, §12.7): the same worktrees as the host's ``worktree.*`` ops,
for a workspace no host serves.

    orch worktree list   [TICKET] [--check] [--json]
    orch worktree create TICKET [--repo NAME] [--json]
    orch worktree remove TICKET [--repo NAME] [--discard-untracked=TOKEN | --force] [--json]

The same safety contract as the host: one worktree per (ticket, linked repo) at ``<repo path>.worktrees/<WS8>-<KEY>`` on
the ticket's current ``links.branches`` branch (``worktree.no_branch`` without one). git runs only through the runner's
own helper :func:`~orch.runner.wtgit.run_git` (add and remove), behind the gate of :mod:`orch.gitsafe`: orch never
runs git against a repository whose local/worktree config or attributes could make git execute a program or read
config from elsewhere (such a repo is ``observe.unavailable``, git is not started); hooks, fsmonitor and network are
off on every command, the environment is built from scratch, 30 s, no symlink followed under ``<repo>.worktrees``.
Revisions, ``HEAD`` and the clean check are read with orch's own reader (:data:`orch.gitread.local.READER`, #565: no
git process), and git's ``worktree remove`` always runs ``--force`` after that check.
The new worktree is pinned in process
(:func:`orch.gitread.pinning.pin_repo`: ``O_NOFOLLOW`` walks, git's layout checked exactly) and registered with a new
generation in ``<state>/hosts/<ws>/git-pins.json``, the file a host reads when the workspace is registered later.
**Removal never depends on ignore rules** (#583 round 3): every file that is not tracked-and-unchanged blocks
``remove``, ignored or not (``node_modules``, ``.venv``, build output), listed with paths and, for untracked
directories, their file count and size. ``--discard-untracked=TOKEN`` deletes exactly that untracked set: the
refusal prints the set with a token (a fingerprint of the set); the command deletes only when the set it finds now has
that token, else it refuses and prints the new set with the new token (#723: what is deleted is what the person saw).
It prints the set, checks the worktree again right before deleting and refuses when anything changed in between;
modified tracked files still block it. ``--force`` skips the check (in direct mode the person is at the terminal and
could ``rm -rf``
it anyway); the branch is never deleted.

The ownership lock is held **shared** for the whole command and the registration is checked under it, so the tool never
works underneath a host (``host.required``), as the records tool (:mod:`orch.records.direct`). ``create`` and
``remove`` also hold an **exclusive** per-workspace ``flock`` (``<state>/hosts/<ws>/worktrees.lock``, 10 s, then
``lock.busy``) from loading the pin file through git and the write of the registration, so two direct commands never
lose each other's registrations (Codex #439). ``list --check`` observes as the host does: the pins verified again
(``moved``), ``HEAD`` read from the pinned gitdir (``stale_branch``), the tree through the reader. Windows answers
``unsupported.platform`` (exit 3): no reparse-point-safe resolution yet. A call under ``ORCH_GRANT`` is refused
(``op.not_allowed``): agents ask the host's ``worktree.*`` ops, never this person-side tool.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from orch import gitsafe
from orch.gitread.errors import Unavailable
from orch.gitread.local import READER

__all__ = ["main"]

LOCK_WAIT = 10.0
_USAGE = (
    "usage: orch worktree {list [TICKET] [--check] | create TICKET [--repo NAME] | remove TICKET [--repo NAME] "
    "[--discard-untracked=TOKEN | --force]} [--json]"
)


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
    a: dict[str, Any] = {"json": False, "check": False, "force": False, "discard": None, "repo": None, "ticket": None}
    it = iter(rest)
    for tok in it:
        if tok == "--json":
            a["json"] = True
        elif tok == "--check" and cmd == "list":
            a["check"] = True
        elif tok == "--force" and cmd == "remove":
            a["force"] = True
        elif (tok == "--discard-untracked" or tok.startswith("--discard-untracked=")) and cmd == "remove":
            a["discard"] = tok.partition("=")[2]  # "": asked without a token (refused with the list and its token)
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

    def __init__(
        self,
        root: Path,
        wid: str,
        state: Path,
        env: Mapping[str, str],
        clock: Any = time.time,
        *,
        ident: tuple[int, int] | None = None,
        config: bytes | None = None,
        store_pin: Any = None,
    ) -> None:
        self.root, self.wid, self.state, self.env, self.clock = Path(root), wid, Path(state), env, clock
        #: the root's identity when it was discovered: checked before the store opens below it
        self.ident = ident
        self.store_pin = store_pin
        #: the config.json bytes discovery read: the store's open-time check uses them, never a re-read by path
        self.config = config
        from orch.host.ownership import DirectGuard
        from orch.ops.caps import Git

        self.guard = DirectGuard(self.state, wid)
        self.git = Git(enabled=True)
        self._store: Any = None
        self._pins: Any = None

    # -- opening
    def served(self) -> bool:
        from orch.host.service import is_served

        return is_served(self.root)  # either layout; a layout it cannot place counts as served (fail closed)

    def store(self) -> Any:
        if self._store is None:
            from orch.discovery import same_root
            from orch.ops.errors import OrchError
            from orch.store import Store, StoreError

            if self.ident is not None and not same_root(self.root, self.ident):
                raise WtError("not_found", f"{self.root} is no longer the workspace directory that was found")
            try:
                self._store = Store.open(self.root, expected_workspace_id=self.wid, host=None,
                                         host_state_dir=self.state, load="lazy", guard=self.guard,
                                         config=self.config, ident=self.ident, store_pin=self.store_pin)  # fmt: skip
            except StoreError as exc:
                raise WtError(exc.code, exc.detail or exc.code) from None
            except OrchError as exc:
                raise WtError(exc.code, str(exc.message), hint=exc.hint) from None
        return self._store

    @contextlib.contextmanager
    def mutating(self, wait: float | None = None) -> Any:
        """The exclusive cross-process lock of a direct mutation: the pin file is (re)loaded under it and it is held
        through git and the write of the registration."""
        import fcntl

        d = self.state / "hosts" / self.wid
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(d / "worktrees.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise WtError(
                "observe.unavailable", f"the worktree lock cannot be opened ({exc.strerror or exc})"
            ) from None
        try:
            deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WtError("lock.busy", "another orch worktree command runs in this workspace", 4,
                                      hint="retry in a moment") from None  # fmt: skip
                    time.sleep(0.02)
            self._pins = None  # never a copy loaded before the lock
            yield
        finally:
            os.close(fd)  # closing releases the lock

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

        path = self.git.repo_path(self.store().layout, self.store().state.workspace.repos, repo)
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
        """The commit ``rev`` (``HEAD`` or ``refs/heads/<name>``) names, ``None`` when there is none. Read with orch's
        own reader (#565: no git process, so the repo's own config can run nothing); a repository the reader refuses
        is ``observe.unavailable``, never "no such branch"."""
        try:
            return READER.head(repo)[1] if rev == "HEAD" else READER.commit(repo, rev)
        except Unavailable as exc:
            raise WtError("observe.unavailable", exc.reason[:300]) from None

    def _dirty(self, repo: str) -> str | None:
        """Uncommitted work in the worktree (``""``: clean), ``None`` when it cannot be told: the reader, never git."""
        try:
            return READER.dirty(repo, self.env)
        except Unavailable:
            return None

    def _git(self, args: dict[str, Any]) -> None:
        from orch.runner.supervise import SpawnError
        from orch.runner.wtgit import run_git

        try:
            run_git(args, self._own())
        except SpawnError as exc:
            code = (
                exc.code
                if exc.code.startswith("worktree.") or exc.code in ("launch.cwd_changed", "observe.unavailable")
                else "worktree.git_failed"
            )
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
            rows.append(self.row(view, repo, slot, self._observe(view, repo, slot) if check else None))
        return rows

    def _observe(self, view: Any, repo: str, slot: dict[str, Any]) -> dict[str, Any]:
        """One check, as the host's ``worktree.list {check}``: the pins verified again in process (a re-pointed ``.git``
        file or a swapped directory is ``moved``), ``HEAD`` read from the pinned gitdir without git (another branch, or
        a relinked ticket, is ``stale_branch``), the tree through the reader (no git process, #565)."""
        from orch.gitread.errors import Unavailable
        from orch.gitread.fs import RepoPins, open_pin
        from orch.gitread.pinning import verify_worktree_pins
        from orch.gitread.refs import read_head

        reg = slot["reg"]
        out: dict[str, Any] = {"checked_at": _iso(self.clock())}
        if reg["pins"] is None:
            return out
        pin = reg["pins"]["worktree"]
        try:
            st = os.lstat(reg["path"])
        except FileNotFoundError:
            return {**out, "state": "missing"}
        except OSError:
            return {**out, "state": "moved"}
        if (st.st_dev, st.st_ino) != (pin["dev"], pin["ino"]):
            return {**out, "state": "moved"}
        try:
            pins = RepoPins.from_json(reg["pins"])
            verify_worktree_pins(reg["path"], pins)
            with open_pin(pins.gitdir) as gd:
                head_ref = read_head(gd)
        except Unavailable:
            return {**out, "state": "moved"}
        current = ((view.fields.get("links") or {}).get("branches") or {}).get(repo)
        if head_ref != f"refs/heads/{reg['branch']}" or current != reg["branch"]:
            out["state"] = "stale_branch"
        try:
            out["head"] = self._rev(reg["path"], "HEAD")
        except WtError:
            return out  # the reader refuses the repository: nothing is known
        d = self._dirty(reg["path"])
        out["dirty"] = None if d is None else len([ln for ln in d.splitlines() if ln])
        return out

    def create(self, ref: str, repo_arg: str | None) -> dict[str, Any]:
        self._gate()
        view = self._ticket(ref)
        repo = self._repo_of(view, repo_arg)
        with self.mutating():
            return self._create(view, repo)

    def _create(self, view: Any, repo: str) -> dict[str, Any]:
        from orch.gitread.errors import Unavailable
        from orch.gitread.fs import open_pin
        from orch.gitread.pinning import pin_repo
        from orch.gitread.refs import read_head
        from orch.host.worktrees import MAX_REGISTERED, worktree_path

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
        repos = self.store().state.workspace.repos
        declared_paths = {
            os.path.realpath(p) for n in repos if (p := self.git.repo_path(self.store().layout, repos, n)) is not None
        }
        if path in declared_paths or os.path.realpath(path) in declared_paths:  # parity with the host (Opus #439 F4)
            raise WtError("worktree.exists", "the worktree path is a declared repository")
        if os.path.lexists(path) and os.path.islink(path):
            raise WtError("worktree.exists", "the worktree path is a symlink")
        start = None
        if self._rev(declared, f"refs/heads/{branch}") is None:
            spec = self.store().state.workspace.repos.get(repo) or {}
            default = spec.get("default_branch") or "main"
            start = self._rev(declared, f"refs/heads/{default}")
            if not start:
                raise WtError(
                    "worktree.no_branch", f"{branch} does not exist and there is no default commit to start it at"
                )
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
            raise WtError(
                "worktree.unpinned", f"the worktree was created but could not be registered ({exc})"
            ) from None
        if reason is not None:
            raise WtError("worktree.unpinned", f"the worktree was created but could not be pinned: {reason}"[:400])
        return self.row(view, repo, pins.worktree(view.uid, repo))

    def remove(
        self, ref: str, repo_arg: str | None, force: bool, discard: str | None = None, say: Any = None
    ) -> dict[str, Any]:
        self._gate()
        view = self._ticket(ref)
        repo = self._repo_of(view, repo_arg)
        with self.mutating():
            return self._remove(view, repo, force, discard, say or (lambda text: None))

    def _removal_check(self, reg: dict[str, Any]) -> dict[str, Any]:
        """The removal check of a worktree (#583 round 3): the reader, NO ignore rule (an ignored file blocks too),
        untracked directories whole with their sizes, and the fingerprint of the untracked set; the registered pins
        when there are some. Unknown is ``observe.unavailable``, never clean."""
        from orch.gitread.fs import RepoPins

        try:
            pins = RepoPins.from_json(reg["pins"]) if reg.get("pins") else None
            return READER.tree_check(reg["path"], self.env, pins=pins, removal=True)
        except Unavailable as exc:  # a checked-out submodule, an index it cannot read, its time limit, ...
            raise WtError("observe.unavailable", f"the worktree cannot be checked: {exc.reason}"[:300]) from None

    @staticmethod
    def _entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
        from orch.gitread.treejob import escape_path

        sizes = {e["path_b64"]: (e["bytes"], e["files"]) for e in raw.get("sizes", [])}
        out = []
        for c in raw["changed"]:
            e = {"why": c["why"], "path": escape_path(base64.b64decode(c["path_b64"]), c["cut"])}
            if c["path_b64"] in sizes:
                e["bytes"], e["files"] = sizes[c["path_b64"]]
            out.append(e)
        return out

    @staticmethod
    def _lines(entries: list[dict[str, Any]], limit: int = 50) -> list[str]:
        from orch.gitread.treejob import render

        return render({"changed": entries, "more": len(entries) > limit}, limit).splitlines()

    def _token(self, raw: dict[str, Any]) -> str:
        """The token of an untracked set (#723): a fingerprint of what the listing shows (paths, kinds, sizes) and of
        the reader's fingerprint of the whole set; a person who passes it has read this set."""
        import hashlib

        doc = json.dumps([self._entries(raw), raw.get("fingerprint"), raw["changed"]], sort_keys=True)
        return hashlib.sha256(doc.encode()).hexdigest()[:12]

    def _between(self) -> None:
        """Between the listing a discard prints and the re-check right before it deletes (a test hook)."""

    def _remove(self, view: Any, repo: str, force: bool, discard: str | None, say: Any) -> dict[str, Any]:
        pins = self.pins()
        slot = pins.worktree(view.uid, repo)
        reg = slot["reg"]
        if reg is None:
            raise WtError("worktree.none", f"{view.key} has no worktree for {repo}")
        discarded: list[str] = []
        if os.path.lexists(reg["path"]) and not force:
            # #583 round 3, orch-07: removal never depends on ignore rules. git's own remove always runs --force
            # (its clean check would run the repo's clean filters, #565), so this check is the only guard: every file
            # that is not tracked-and-unchanged blocks, ignored or not, listed with sizes.
            first = self._removal_check(reg)
            entries = self._entries(first)
            tracked = [e for e in entries if e["why"] != "untracked"]
            untracked = [e for e in entries if e["why"] == "untracked"]
            token = self._token(first) if untracked else ""
            if untracked and discard and not tracked and not first["more"] and discard != token:
                raise WtError("worktree.dirty", ("the untracked files are not the ones that token was made for; "
                              "nothing was deleted. They are now: " + "; ".join(self._lines(untracked, 20))
                              + f". To delete exactly these, run `orch worktree remove {view.key} "
                              f"--discard-untracked={token}`")[:2000])  # fmt: skip
            if tracked or first["more"] or (untracked and not discard):
                hint = "commit them" if tracked else "commit or delete them"
                if untracked and not tracked and not first["more"]:
                    hint += (
                        f", or `orch worktree remove {view.key} --discard-untracked={token}` (deletes exactly these)"
                    )
                raise WtError("worktree.dirty", ("the worktree has files that are not committed (ignored files count "
                              "too: a removal never depends on ignore rules): " + "; ".join(self._lines(entries, 20))
                              + f". {hint[0].upper()}{hint[1:]}, or remove with --force")[:2000])  # fmt: skip
            if untracked:  # --discard-untracked=TOKEN: print exactly the set, re-check right before deleting
                discarded = [ln for ln in self._lines(untracked, len(untracked))]
                say("discarding exactly these untracked files:\n" + "\n".join(f"  {ln}" for ln in discarded) + "\n")
                self._between()
                second = self._removal_check(reg)
                if second["fingerprint"] != first["fingerprint"] or second["changed"] != first["changed"]:
                    raise WtError("worktree.dirty", "the worktree's files changed since they were listed; nothing was "
                                  "deleted. Look at the new list and run the command again")  # fmt: skip
        _declared, dpins = self._declared(repo)
        self._git({"op": "worktree.remove", "repo_pin": dpins.worktree.to_json(), "path": reg["path"],
                   "branch": reg["branch"], "base": None, "force": force, "generation": slot["gen"] + 1})  # fmt: skip
        try:
            pins.set_worktree(view.uid, repo, None, expect=slot["gen"])
        except (ValueError, OSError) as exc:
            raise WtError("internal", f"the worktree was removed but its registration could not be dropped ({exc})",
                          1) from None  # fmt: skip
        return {"removed": True, **({"discarded": discarded} if discarded else {})}


def _text(cmd: str, data: Any) -> str:
    if cmd == "remove":
        return "removed"
    rows = data if cmd == "list" else [data]
    if not rows:
        return "no worktrees"
    return "\n".join(
        f"{r['ticket']} {r['repo']} {r['branch']} {r['state']} gen={r['generation']} {r['path']}" for r in rows
    )


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
        from orch.ops.errors import OrchError
        from orch.ops.runtime import Workspace, state_dir

        files = Files.direct()
        ws = Workspace(env, cwd=os.getcwd(), files=files)
        cfg = ws.config  # as discovery read it, never again by path
        wid = ((cfg or {}).get("workspace") or {}).get("id")
        if ws.root is None or not isinstance(wid, str):
            raise WtError("not_found", "no orch workspace here")
        try:
            root = ws.verify_root()  # still the directory found, before anything under it is opened
        except OrchError as exc:
            raise WtError(exc.code, str(exc.message), hint=exc.hint) from None
        direct = Direct(
            Path(root),
            wid,
            state_dir(env, files),
            env,
            now,
            ident=ws.ident,
            config=ws.config_bytes,
            store_pin=ws.store_pin,
        )
        with direct.guarded():
            if cmd == "list":
                data: Any = direct.list(a["ticket"], a["check"])
            elif cmd == "create":
                data = direct.create(a["ticket"], a["repo"])
            else:
                say = (err if err is not None else sys.stderr).write
                data = direct.remove(a["ticket"], a["repo"], a["force"], a["discard"], say)
        out.write((json.dumps({"ok": True, "data": data}) if as_json else _text(cmd, data)) + "\n")
        return 0
    except WtError as exc:
        return fail(exc)
    except gitsafe.Unsafe as exc:  # the invariant of orch.gitsafe: git was not started
        return fail(WtError("observe.unavailable", exc.reason[:300]))
    except Exception as exc:  # the guard's host.required (an OrchError) when a host registered the workspace meanwhile
        from orch.ops.errors import OrchError

        if isinstance(exc, OrchError):
            return fail(WtError(exc.code, str(exc.message), hint=exc.hint))
        raise
    finally:
        if direct is not None:
            direct.close()
