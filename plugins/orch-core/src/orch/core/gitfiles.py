"""Which files orch writes belong in git (#36). Durable files are shared records every clone needs: tickets, gates,
the counter, the event log, the phone-decision ledger and what addons keep under `.state/addons/<name>/records/`.
Local files are caches, locks, spools and per-machine state that orch can rebuild or that belong to one machine;
the managed block in `orchestrator/.gitignore` keeps them out of git. orch commits nothing on its own: `orch doctor`
and `orch check` name what is left uncommitted, and `orch records commit` (#168) commits exactly those records when
the human (or an agent the workspace lets commit) runs it."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orch.core.fsutil import atomic_write_text

BEGIN = "# >>> orch: managed by `orch instructions sync` and `orch doctor --fix`; add your own lines outside this block"
END = "# <<< orch"

# (pattern in orchestrator/.gitignore, the same as a regex on the path relative to orchestrator/)
_LOCAL: tuple[tuple[str, str], ...] = (
    ("/temporary/", r"temporary/.*"),
    ("/.state/locks/", r"\.state/locks/.*"),
    ("/.state/index.json", r"\.state/index\.json"),
    ("/.state/addon-errors.log", r"\.state/addon-errors\.log"),
    ("/.state/guard-errors.log", r"\.state/guard-errors\.log"),
    ("/.state/needs-count", r"\.state/needs-count"),
    ("/.state/run/", r"\.state/run/.*"),
    ("/.state/**/*.lock", r"\.state/(.*/)?[^/]*\.lock"),
    ("**/.*.tmp", r"(.*/)?\.[^/]*\.tmp"),
)
# Addon folders are caches (snapshots, cursors, inbox/outbox spools) except their records/ folder.
_ADDON_LINES = ("/.state/addons/*", "!/.state/addons/*/", "/.state/addons/*/*", "!/.state/addons/*/records/")
_ADDON_LOCAL = r"\.state/addons/(?:[^/]+|[^/]+/(?!records/).*)"
_DURABLE = (r"config\.json", r"AGENTS\.orch\.md", r"\.gitignore", r"tickets/.*", r"artifacts/.*", r"static/.*",
            r"\.state/counter\.json", r"\.state/events\.jsonl", r"\.state/gates/.*", r"\.state/remote/ledger\.jsonl",
            r"\.state/addons/[^/]+/records/.*", r"\.state/quick-counter\.json", r"\.state/quick/[^/]+\.json")
_LOCAL_RE = re.compile("|".join(f"(?:{rx})" for _, rx in _LOCAL) + f"|(?:{_ADDON_LOCAL})")
_DURABLE_RE = re.compile("|".join(f"(?:{rx})" for rx in _DURABLE))
# Files outside orchestrator/ that `orch instructions sync` writes: shared, so they belong in git too.
_SYNC_TARGETS = ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", ".github/copilot-instructions.md",
                 ".claude/skills", ".agents/skills")


def classify(rel: str) -> str | None:
    """"durable", "local" or None (orch does not know the file) for a path relative to orchestrator/."""
    rel = rel.replace("\\", "/")
    if _LOCAL_RE.fullmatch(rel):
        return "local"
    if _DURABLE_RE.fullmatch(rel):
        return "durable"
    return None


def ignore_block() -> str:
    lines = [BEGIN, "# Local only: caches, locks, spools and per-machine state. Tickets, gates, counter.json,",
             "# events.jsonl, remote/ledger.jsonl and addon records/ are shared records: commit them."]
    lines += [p for p, _ in _LOCAL] + list(_ADDON_LINES) + [END]
    return "\n".join(lines)


def apply_ignore_block(text: str | None, block: str | None = None, begin: str = BEGIN, end: str = END) -> str:
    """The .gitignore text with the managed block replaced, or inserted first (lines after it can override it)."""
    block = ignore_block() if block is None else block
    if text is None or not text.strip():
        return block + "\n"
    crlf = "\r\n" in text
    lf = text.replace("\r\n", "\n")
    start = lf.find(begin)
    stop = lf.find(end, start) if start != -1 else -1
    if start != -1 and stop > start:
        out = lf[:start] + block + lf[stop + len(end):]
    else:
        out = block + "\n\n" + lf
    if not out.endswith("\n"):
        out += "\n"
    return out.replace("\n", "\r\n") if crlf else out


def _path(ws) -> Path:
    return ws.home / ".gitignore"


def _read(ws) -> str | None:
    try:
        return _path(ws).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def ignore_block_state(ws) -> str:
    """"ok", "missing" (no managed block yet) or "outdated" (a block from another orch version)."""
    text = _read(ws)
    if text is None or BEGIN not in text:
        return "missing"
    return "ok" if apply_ignore_block(text).replace("\r\n", "\n") == text.replace("\r\n", "\n") else "outdated"


def planned_ignore(ws) -> tuple[str, str]:
    """(new text, action) for orchestrator/.gitignore: action is "created", "updated" or "unchanged"."""
    old = _read(ws)
    new = apply_ignore_block(old)
    if old is None:
        return new, "created"
    return new, "unchanged" if old.replace("\r\n", "\n") == new.replace("\r\n", "\n") else "updated"


def write_ignore_block(ws) -> str:
    new, action = planned_ignore(ws)
    if action != "unchanged":
        atomic_write_text(_path(ws), new)
    return action


@dataclass
class GitView:
    root: Path
    uncommitted: list[str] = field(default_factory=list)  # durable records with changes git has not committed
    codes: dict[str, str] = field(default_factory=dict)  # porcelain status code of each uncommitted record
    unclassified: list[str] = field(default_factory=list)  # untracked, not ignored, and not an orch record
    tracked_local: list[str] = field(default_factory=list)  # local files (caches, locks) that git tracks anyway


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def repo_top(ws) -> Path | None:
    """The git repository orch's records belong to: the repository around the workspace root, or the orch home's own
    repository (`orchestrator/` ignored by the code repository around it). The home's own repository counts only when
    its top level IS the home and its git directory lies inside it: a gitfile pointing elsewhere is not trusted."""
    home = Path(ws.home).resolve()
    out = _git(Path(ws.root).resolve(), "rev-parse", "--show-toplevel")
    root_top = Path(out.strip()).resolve() if out and out.strip() else None
    out = _git(home, "rev-parse", "--show-toplevel")
    home_top = Path(out.strip()).resolve() if out and out.strip() else None
    if home_top is not None and (home_top == root_top or home_top == home):
        if home_top == root_top:
            return home_top
        common = _git(home, "rev-parse", "--git-common-dir")
        if common and common.strip() and (home / common.strip()).resolve().is_relative_to(home):
            return home_top
    return root_top


# What a push may carry or auto mode may commit: records under the orch home, except the files that steer agents or
# the workspace (config.json, static/, AGENTS.orch.md) and the sync targets outside the home. Those stay manual.
_MANUAL_ONLY = re.compile(r"config\.json|AGENTS\.orch\.md|static/.*")


def _auto_ok(ws, top: Path, git_rel: str) -> bool:
    in_home = _rel_home(ws, top, git_rel)
    return in_home is not None and classify(in_home) == "durable" and not _MANUAL_ONLY.fullmatch(in_home)


def _rel_home(ws, top: Path, git_rel: str) -> str | None:
    try:
        return (top / git_rel).relative_to(ws.home.resolve()).as_posix()
    except ValueError:
        return None


def git_view(ws) -> GitView | None:
    """What git says about orch's files, or None when the workspace is not in a git repository (or git fails)."""
    root = ws.root.resolve()
    top = repo_top(ws)
    if top is None:
        return None
    home = ws.home.resolve()
    if not home.is_relative_to(top):
        return None
    specs = _record_specs(ws, top)
    status = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *specs)
    tracked = _git(top, "ls-files", "-z", "--", home.relative_to(top).as_posix() or ".")
    if status is None or tracked is None:
        return None
    view = GitView(top)
    for entry in filter(None, status.split("\0")):
        code, rel = entry[:2], entry[3:]
        kind = _record_kind(ws, top, specs, rel)
        if kind == "durable":
            shown = _shown(top, root, rel)
            view.uncommitted.append(shown)
            view.codes[shown] = code
        elif kind is None and code == "??":
            view.unclassified.append(_shown(top, root, rel))
    for rel in filter(None, tracked.split("\0")):
        in_home = _rel_home(ws, top, rel)
        if in_home is not None and classify(in_home) == "local":
            view.tracked_local.append(_shown(top, root, rel))
    return view


def _record_specs(ws, top: Path) -> list[str]:
    """The pathspecs (relative to the git top) where orch records live: orchestrator/ and the sync targets."""
    root, home = ws.root.resolve(), ws.home.resolve()
    targets = [home] + [root / t for t in _SYNC_TARGETS if (root / t).exists() or t in ("AGENTS.md", "CLAUDE.md")]
    return [p.relative_to(top).as_posix() or "." for p in targets if p.is_relative_to(top)]


def _record_kind(ws, top: Path, specs: list[str], git_rel: str) -> str | None:
    """"durable" for an orch record, "local" for a cache or lock, None for anything else (code)."""
    in_home = _rel_home(ws, top, git_rel)
    if in_home is not None:
        return classify(in_home)
    if any(spec != "." and (git_rel == spec or git_rel.startswith(spec + "/")) for spec in specs):
        return "durable"
    return None


def staged_paths(cwd: Path) -> tuple[Path, list[str]] | None:
    """(git top, staged paths relative to it) of the index this commit uses: git hands a hook GIT_INDEX_FILE, so a
    commit that names paths (a temporary index) is read correctly. None when git cannot tell."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top or not top.strip():
        return None
    root = Path(top.strip()).resolve()
    out = _git(root, "diff", "--cached", "--name-only", "-z", "--no-renames")
    if out is None:
        return None
    return root, [p for p in out.split("\0") if p]


def records_only(ws, cwd: Path) -> bool:
    """True when this commit stages something and every staged path is an orch record, the way doctor's `records`
    check counts them; caches and locks never count. Anything git cannot tell is False (fail closed)."""
    found = staged_paths(cwd)
    if found is None or not found[1]:
        return False
    top, paths = found
    try:
        if not ws.home.resolve().is_relative_to(top):
            return False
        specs = _record_specs(ws, top)
        return all(_record_kind(ws, top, specs, p) == "durable" for p in paths)
    except (OSError, ValueError):
        return False


RECORDS_SUBJECT = "orch: records"


def record_keys(ws, paths: list[str]) -> list[str]:
    """The ticket keys named in record paths, in first-seen order."""
    key = re.compile(rf"(?<![\w-])({re.escape(ws.config['id']['prefix'])}-\d+)(?!\d)")
    keys: list[str] = []
    for p in paths:
        for k in key.findall(p):
            if k not in keys:
                keys.append(k)
    return keys


def record_word(code: str) -> str:
    words = {"??": "added", "A": "added", "D": "deleted", "M": "modified", "R": "renamed", "T": "changed"}
    return next((w for c, w in words.items() if c in code), "changed")


def records_message(ws, view: GitView, paths: list[str]) -> tuple[str, str]:
    """(subject, body) for `orch records commit`: the ticket keys the records belong to, then each changed path."""
    keys = record_keys(ws, paths)
    subject = RECORDS_SUBJECT + (f" {few(sorted(keys), 8)}" if keys else "")
    lines = [f"- {record_word(view.codes.get(p, ''))}: {p}" for p in paths]
    return subject, "Records orch wrote:\n" + "\n".join(lines)


_BUSY = (("MERGE_HEAD", "a merge"), ("REBASE_HEAD", "a rebase"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
         ("REVERT_HEAD", "a revert"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase or an am"))


def busy_reason(top: Path) -> str | None:
    """Why a records commit or push must wait: a merge, rebase, cherry-pick or revert is in progress in this git
    directory (`git rev-parse --git-path` finds it in a worktree too). None when nothing is."""
    for name, what in _BUSY:
        out = _git(top, "rev-parse", "--git-path", name)
        if out and out.strip() and (top / out.strip()).exists():
            return f"{what} is in progress in this repository; finish or abort it first"
    return None


def commit_records(ws, *, dry_run: bool = False, auto: bool = False) -> tuple[list[str], str]:
    """Commit exactly the records doctor lists, with a generated message, and return (paths, subject). The paths
    are staged and committed with `git commit --only`, so whatever else is staged stays staged and uncommitted."""
    from orch.errors import OrchError, UsageError
    view = git_view(ws)
    if view is None:
        raise UsageError("the workspace is not in a git repository (or git failed)")
    paths = list(view.uncommitted)
    if auto:  # automatic mode commits only what it may also push
        root0 = ws.root.resolve()
        paths = [p for p in paths if _auto_ok(ws, view.root, (root0 / p).relative_to(view.root).as_posix())]
    if not paths:
        return [], ""
    busy = busy_reason(view.root)
    if busy and not dry_run:
        raise UsageError(f"records not committed: {busy}")
    subject, body = records_message(ws, view, paths)
    if dry_run:
        return paths, subject
    root = ws.root.resolve()
    full = [(root / p).as_posix() for p in paths]
    for args in (("add", "--", *full), ("commit", "-q", "--only", "-m", subject, "-m", body, "--", *full)):
        try:
            r = subprocess.run(["git", "--literal-pathspecs", "-C", str(view.root), *args], capture_output=True,
                               check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OrchError(f"git {args[0]} failed: {e}") from e
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).decode("utf-8", "replace").strip() or f"exit code {r.returncode}"
            hint = "the records are staged; fix the problem and run `orch records commit` again" if args[0] == "commit" else None
            raise OrchError(f"git {args[0]} failed: {detail}", hint=hint)
    return paths, subject


def _run(top: Path, *args: str, timeout: float = 10, env: dict | None = None) -> tuple[int, str]:
    """(exit code, stdout + stderr) of a git call: no stdin, no prompts, untranslated messages, a timeout; (-1, reason)
    when git cannot run."""
    import os
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})}
    try:
        r = subprocess.run(["git", "--literal-pathspecs", "-C", str(top), *args], capture_output=True, check=False,
                           timeout=max(1.0, timeout), stdin=subprocess.DEVNULL, env=e)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return -1, str(ex)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()


def head_short(ws) -> str:
    top = repo_top(ws)
    out = _git(top, "rev-parse", "--short", "HEAD") if top else None
    return (out or "").strip()


def _unpushed(ws, top: Path, base: str, head: str) -> tuple[int, int] | None:
    """(commits in base..head, of those not purely pushable records); None when git cannot tell. `base` is the real
    remote tip, never a local tracking ref. A commit counts only by CONTENT: not a merge, no gitlink or symlink, and
    every path it changes is a record auto mode may commit (_auto_ok). The subject is never trusted."""
    rc, out = _run(top, "rev-list", "--parents", f"{base}..{head}")
    if rc != 0:
        return None
    total = other = 0
    for line in out.splitlines():
        shas = line.split()
        if not shas:
            continue
        total += 1
        if len(shas) > 2:  # a merge
            other += 1
            continue
        try:
            r = subprocess.run(["git", "--literal-pathspecs", "-C", str(top), "diff-tree", "--root", "--no-commit-id",
                                "-r", "-z", "--raw", "--no-renames", shas[0]], capture_output=True, check=False,
                               timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        items = [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
        ok = bool(items) and len(items) % 2 == 0
        for meta, path in zip(items[0::2], items[1::2]):
            modes = meta.lstrip(":").split()[:2]
            if any(m in ("160000", "120000") for m in modes) or not _auto_ok(ws, top, path):
                ok = False
        other += not ok
    return total, other


_CONFIG_GUARD = (r"^(remote\..*\.(pushurl|proxy|uploadpack|receivepack|vcs)|url\..*\.(insteadof|pushinsteadof)"
                 r"|core\.(sshcommand|askpass|gitproxy)|http\.(.*\.)?(proxy|extraheader)|credential\..*helper)$")
# Transports orch's network calls may use; command-line -c beats repository config. Tests may add "file".
NET_PROTOCOLS = ("https", "ssh")
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")  # ls-remote, fetch and rebase need none; commit and push keep theirs


def _repo_config_redirects(top: Path) -> str | None:
    """A key set by the repository itself (local or worktree scope, includes too) that changes where a push goes or how
    it authenticates; None when there is none."""
    rc, out = _run(top, "config", "--show-origin", "--show-scope", "--get-regexp", _CONFIG_GUARD)
    if rc not in (0, 1):
        return "cannot read the repository git config"
    for line in out.splitlines():
        scope = line.split("\t", 1)[0]
        if scope in ("local", "worktree"):
            return line.split("\t")[-1].split()[0] if line.split("\t")[-1].split() else "a setting"
    return None


def push_records(ws, *, auto: bool = False) -> dict:
    """Push the current branch to its upstream, only when every commit it would send changes nothing but orch records
    (judged by content, never by subject). The remote's real tip is read with `git ls-remote` (a local tracking ref can
    be forged), must be an ancestor of HEAD, and is the lease: the push is `<checked HEAD sha>:refs/heads/<branch>` with
    --force-with-lease against that sha, so it can only fast-forward exactly what was checked. A remote that moved
    is fetched once and records-only commits are rebased onto its tip (aborted on any conflict), then pushed once
    more. Never creates a remote branch. Returns {"pushed": bool, "reason": str, "failed": bool}."""
    import os
    import time
    end = time.monotonic() + (45 if auto else 120)

    def no(reason: str, failed: bool = True) -> dict:
        return {"pushed": False, "reason": reason, "failed": failed}  # failed: worth a warning, not just "nothing to do"

    def run(*args: str, net: bool = False, env: dict | None = None) -> tuple[int, str]:
        left = end - time.monotonic()
        if left <= 0:
            return -1, "timed out"
        if net:
            proto = ["-c", "protocol.allow=never", *(x for p in NET_PROTOCOLS for x in ("-c", f"protocol.{p}.allow=always"))]
            args = (*proto, *(_NO_HOOKS if args[0] in ("ls-remote", "fetch") else ()), *args)
        elif args[0] == "rebase" or args[:1] == ("-c",):
            args = (*_NO_HOOKS, *args)
        return _run(top, *args, timeout=min(20 if net else 10, left), env=env)

    top = repo_top(ws)
    if top is None:
        return no("the workspace is not in a git repository", False)
    busy = busy_reason(top)
    if busy:
        return no(busy)
    rc, branch = run("symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0 or not branch:
        return no("HEAD is detached; nothing pushed", False)
    rc, remote = run("config", f"branch.{branch}.remote")
    rc2, merge = run("config", f"branch.{branch}.merge")
    if rc != 0 or rc2 != 0 or not remote or not merge.startswith("refs/heads/"):
        return no(f"{branch} has no upstream; nothing pushed", False)
    if not re.fullmatch(r"refs/heads/[\w./-]+", merge) or ".." in merge or not re.fullmatch(r"[\w.-]+", remote):
        return no(f"the upstream of {branch} is not one orch pushes to; nothing pushed")
    if remote == ".":
        return no(f"the upstream of {branch} is a local branch; nothing pushed", False)
    for key in (f"branch.{branch}.pushRemote", "remote.pushDefault"):  # a different push target than the upstream
        rc3, other_remote = run("config", key)
        if rc3 == 0 and other_remote and other_remote != remote:
            return no(f"{key} points at {other_remote}, not the upstream remote {remote}; nothing pushed")
    if _repo_config_redirects(top):
        return no("repository git config changes where pushes go or how they sign in; push by hand")
    rc, urls = run("config", "--get-all", f"remote.{remote}.url")
    if rc != 0 or len(urls.splitlines()) != 1:
        return no(f"remote {remote} must have exactly one URL; push by hand")
    env = {}
    if not os.environ.get("GIT_SSH_COMMAND") and run("config", "core.sshCommand")[1] == "":
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    name = merge[len("refs/heads/"):]

    def tip() -> str | tuple[None, str]:
        rc, url = run("remote", "get-url", "--push", remote)
        if rc != 0 or not url:
            return None, "cannot read the push URL of the upstream remote"
        rc, out = run("ls-remote", "--", url, merge, net=True, env=env)
        if rc != 0:
            return None, f"cannot reach {remote}: {out.splitlines()[-1] if out else 'unknown error'}"
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == merge and re.fullmatch(r"[0-9a-f]{40,64}", sha):
                return sha
        return None, f"{remote} has no branch {name}; orch does not create one"

    def attempt(allow_rebase: bool) -> dict:
        sha = tip()
        if isinstance(sha, tuple):
            return no(sha[1])
        rc, head = run("rev-parse", "--verify", "HEAD")
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            return no("cannot read HEAD; nothing pushed")
        have = run("cat-file", "-e", f"{sha}^{{commit}}")[0] == 0
        if have and run("merge-base", "--is-ancestor", sha, head)[0] == 0:
            counts = _unpushed(ws, top, sha, head)
            if counts is None:
                return no(f"cannot compare {branch} with {remote}; nothing pushed")
            total, other = counts
            if total == 0:
                return no("nothing to push", False)
            if other:
                return no(f"{other} other commit(s) wait; push them yourself", False)
            rc, out = run("push", "--no-follow-tags", f"--force-with-lease={merge}:{sha}", remote, f"{head}:{merge}",
                          net=True, env=env)
            if rc == 0:
                return {"pushed": True, "failed": False, "reason": f"pushed {total} records commit(s) to {remote}/{name}"}
            return no(f"push failed: {out.splitlines()[-1] if out else 'unknown error'}")
        if not allow_rebase:
            return no(f"{remote} moved again while pushing; try again")
        rc, out = run("fetch", "--no-tags", remote, merge, net=True, env=env)  # objects only; no tracking ref is trusted
        if rc != 0 or run("cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
            return no(f"{remote} has commits this clone lacks and they could not be fetched; fetch and rebase yourself")
        if run("rev-parse", "--verify", "HEAD") != (0, head):
            return no("HEAD changed while pushing; try again")
        counts = _unpushed(ws, top, sha, head)
        if counts is None or counts[1] or not counts[0]:
            return no("the remote moved and the branch holds other commits; fetch and rebase it yourself")
        rc, dirty = run("status", "--porcelain", "--untracked-files=no")
        if rc != 0 or dirty:
            return no("the remote moved; tracked changes in the working tree block a rebase onto it")
        rc, out = run("-c", "rebase.updateRefs=false", "-c", "rebase.autoSquash=false", "-c", "rebase.autoStash=false",
                      "rebase", sha)
        if rc != 0:
            _, conflicts = run("diff", "--name-only", "--diff-filter=U")
            run("rebase", "--abort")
            return no("rebase onto the remote conflicts (aborted, nothing changed): "
                      + (few(conflicts.splitlines()) or "see git status"))
        result = attempt(False)
        if result["pushed"]:
            result["reason"] = f"rebased onto {remote}/{name} and pushed"
        return result

    return attempt(True)


def sync_records(ws, *, push: bool = False, dry_run: bool = False, auto: bool = False) -> dict:
    """Commit the uncommitted records and, with `push`, push them: the one call behind `orch records commit`, the
    dashboard button and `records.auto`. {"committed", "paths", "subject", "hash", "pushed", "reason"}."""
    paths, subject = commit_records(ws, dry_run=dry_run, auto=auto)
    out = {"committed": bool(paths) and not dry_run, "paths": paths, "subject": subject,
           "hash": head_short(ws) if paths and not dry_run else "", "pushed": False, "reason": "", "failed": False}
    if push and not dry_run:
        out.update(push_records(ws, auto=auto))
    return out


def few(paths: list[str], n: int = 5) -> str:
    shown = ", ".join(paths[:n])
    return shown + (f" and {len(paths) - n} more" if len(paths) > n else "")


def _shown(top: Path, root: Path, git_rel: str) -> str:
    """A path as the human sees it from the workspace root (the usual place to run git)."""
    try:
        return (top / git_rel).relative_to(root).as_posix()
    except ValueError:
        return git_rel


def changed_and_uncommitted(ws, paths: list[Path]) -> list[str]:
    """Those of `paths` (files orch just wrote) that git shows as changed or new, relative to the workspace root."""
    view = git_view(ws)
    if view is None:
        return []
    root = ws.root.resolve()
    wanted = set()
    for p in paths:
        try:
            wanted.add(p.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return [p for p in view.uncommitted if p in wanted]


def stage_records(ws, cwd: Path, *, dry_run: bool = False) -> list[str]:
    """The pre-commit hook's work: when this commit stages a ticket file, the uncommitted durable records under
    the state folder (gate snapshots, events.jsonl, counter.json, ...) are returned and, unless dry_run, staged
    with `git add`. Nothing is committed; a commit in another repo than the workspace's is left alone."""
    view = git_view(ws)
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if view is None or not top or Path(top.strip()).resolve() != view.root:
        return []
    staged = _git(view.root, "diff", "--cached", "--name-only", "-z") or ""
    tickets = (ws.home.resolve() / "tickets").relative_to(view.root).as_posix() + "/"
    if not any(p.startswith(tickets) for p in staged.split("\0")):
        return []
    state = ws.state_dir.resolve().relative_to(ws.root.resolve()).as_posix() + "/"
    paths = [p for p in view.uncommitted if p.startswith(state)]
    if paths and not dry_run:
        _git(view.root, "add", "--", *[(ws.root.resolve() / p).as_posix() for p in paths])
    return paths
