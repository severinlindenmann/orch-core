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
    """The git repository orch's records belong to: the one around the orch home (so `orchestrator/` can be its own
    repository, ignored by the code repository around it), else the one around the workspace root."""
    for where in (ws.home, ws.root):
        out = _git(Path(where).resolve(), "rev-parse", "--show-toplevel")
        if out and out.strip():
            return Path(out.strip()).resolve()
    return None


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


def commit_records(ws, *, dry_run: bool = False) -> tuple[list[str], str]:
    """Commit exactly the records doctor lists, with a generated message, and return (paths, subject). The paths
    are staged and committed with `git commit --only`, so whatever else is staged stays staged and uncommitted."""
    from orch.errors import OrchError, UsageError
    view = git_view(ws)
    if view is None:
        raise UsageError("the workspace is not in a git repository (or git failed)")
    paths = list(view.uncommitted)
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


def _run(top: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    """(exit code, stdout + stderr) of a git call with a timeout; (-1, reason) when git cannot run."""
    try:
        r = subprocess.run(["git", "--literal-pathspecs", "-C", str(top), *args], capture_output=True, check=False,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()


def head_short(ws) -> str:
    top = repo_top(ws)
    out = _git(top, "rev-parse", "--short", "HEAD") if top else None
    return (out or "").strip()


def _unpushed(ws, top: Path) -> tuple[int, int] | None:
    """(commits in @{u}..HEAD, of those not purely orch records); None when git cannot tell. A commit counts as records
    only by CONTENT: not a merge, and every path it changes classifies as a durable record (the same rule as
    git_view). The subject is never trusted."""
    rc, out = _run(top, "rev-list", "--parents", "@{u}..HEAD")
    if rc != 0:
        return None
    specs = _record_specs(ws, top)
    total = other = 0
    for line in out.splitlines():
        shas = line.split()
        if not shas:
            continue
        total += 1
        if len(shas) != 2 and len(shas) != 1:  # a merge
            other += 1
            continue
        try:
            r = subprocess.run(["git", "--literal-pathspecs", "-C", str(top), "diff-tree", "--root", "--no-commit-id",
                                "--name-only", "-r", "-z", "--no-renames", shas[0]], capture_output=True, check=False,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        paths = [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
        if not paths or any(_record_kind(ws, top, specs, x) != "durable" for x in paths):
            other += 1
    return total, other


def push_records(ws) -> dict:
    """Push the current branch to its upstream, only when every commit it would send changes nothing but orch records (judged by content, never by subject).
    Never forces and names no branch but HEAD's own upstream. A rejected push fetches once and rebases records-only
    commits onto the upstream (aborted on any conflict), then pushes once more. Returns {"pushed": bool, "reason": str}."""
    def no(reason: str, failed: bool = True) -> dict:
        return {"pushed": False, "reason": reason, "failed": failed}  # failed: worth a warning, not just "nothing to do"
    top = repo_top(ws)
    if top is None:
        return no("the workspace is not in a git repository", False)
    busy = busy_reason(top)
    if busy:
        return no(busy)
    rc, branch = _run(top, "symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0 or not branch:
        return no("HEAD is detached; nothing pushed", False)
    rc, remote = _run(top, "config", f"branch.{branch}.remote")
    rc2, merge = _run(top, "config", f"branch.{branch}.merge")
    if rc != 0 or rc2 != 0 or not remote or not merge.startswith("refs/heads/"):
        return no(f"{branch} has no upstream; nothing pushed", False)
    if remote == ".":
        return no(f"the upstream of {branch} is a local branch; nothing pushed", False)
    counts = _unpushed(ws, top)
    if counts is None:
        return no(f"cannot compare {branch} with its upstream; nothing pushed")
    total, other = counts
    if total == 0:
        return no("nothing to push", False)
    if other:
        return no(f"{other} other commit(s) wait; push them yourself", False)
    push = ("push", remote, f"HEAD:{merge}")
    rc, out = _run(top, *push, timeout=120)
    if rc == 0:
        return {"pushed": True, "failed": False, "reason": f"pushed {total} records commit(s) to {remote}/{merge[11:]}"}
    if not any(w in out for w in ("rejected", "non-fast-forward", "fetch first")):
        return no(f"push failed: {out.splitlines()[-1] if out else 'unknown error'}")
    rc, out = _run(top, "fetch", remote, timeout=120)
    counts = _unpushed(ws, top) if rc == 0 else None
    if counts is None or counts[1] or not counts[0]:
        return no("push rejected and the branch holds other commits; fetch and rebase it yourself")
    rc, dirty = _run(top, "status", "--porcelain", "--untracked-files=no")
    if rc != 0 or dirty:
        return no("push rejected; tracked changes in the working tree block a rebase onto the upstream")
    rc, out = _run(top, "rebase", "@{u}", timeout=120)
    if rc != 0:
        _, conflicts = _run(top, "diff", "--name-only", "--diff-filter=U")
        _run(top, "rebase", "--abort")
        return no("rebase onto the upstream conflicts (aborted, nothing changed): " + (few(conflicts.splitlines()) or "see git status"))
    rc, out = _run(top, *push, timeout=120)
    if rc == 0:
        return {"pushed": True, "failed": False, "reason": f"rebased onto {remote}/{merge[11:]} and pushed"}
    return no(f"push failed after rebase: {out.splitlines()[-1] if out else 'unknown error'}")


def sync_records(ws, *, push: bool = False, dry_run: bool = False) -> dict:
    """Commit the uncommitted records and, with `push`, push them: the one call behind `orch records commit`, the
    dashboard button and `records.auto`. {"committed", "paths", "subject", "hash", "pushed", "reason"}."""
    paths, subject = commit_records(ws, dry_run=dry_run)
    out = {"committed": bool(paths) and not dry_run, "paths": paths, "subject": subject,
           "hash": head_short(ws) if paths and not dry_run else "", "pushed": False, "reason": "", "failed": False}
    if push and not dry_run:
        out.update(push_records(ws))
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
