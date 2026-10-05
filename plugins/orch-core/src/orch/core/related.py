"""What else touches this code: tickets, files and links around a ticket or a set of paths, for an agent about to plan.

The commit-msg hook puts a ticket key into every commit subject, so `git log` already maps files to tickets; this
module joins that with the ticket links in frontmatter (parent, blocked_by, follow_ups, superseded_by). It only reads:
git history of the workspace repo and the configured `git.repos`, and the ticket scan. Nothing is written.

Groups, each ranked and capped so the result stays small enough for an agent's context:
- history:    done tickets whose commits changed these files (what was decided here before)
- in_flight:  open tickets whose commits (any local branch) changed these files (who else is in this code)
- co_changed: files often committed together with these files that are not among them (a missed companion)
- linked:     tickets linked to the ticket in frontmatter
"""
from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from orch.core import store
from orch.core.links import LinkIndex
from orch.core.query import resolution

MAX_COMMITS = 2000      # commits read per repo, newest first
BULK_FILES = 50         # a commit changing more files than this (a rename sweep, a format run) says nothing about coupling
MIN_CO_CHANGE = 2       # a co-changed file must share at least this many commits
_SEP_COMMIT, _SEP_FIELD = "\x1e", "\x1f"


@dataclass
class Commit:
    repo: str
    sha: str
    date: str
    keys: list[str]                 # local ticket ids named in the subject
    files: list[str] = field(default_factory=list)  # repo-qualified: "<repo>:<path>", or "<path>" in the workspace repo


def repos(ws) -> list[tuple[str, Path]]:
    """(name, directory) of each git checkout to read: the workspace repo (name "") and each configured git.repos."""
    out: list[tuple[str, Path]] = []
    if (ws.root / ".git").exists():
        out.append(("", ws.root))
    for name, spec in (ws.config["git"].get("repos") or {}).items():
        d = ws.root / str((spec or {}).get("path") or name).strip("/")
        if (d / ".git").exists() and d.resolve() != ws.root.resolve():
            out.append((str(name), d))
    return out


def _qualify(repo: str, path: str) -> str:
    return f"{repo}:{path}" if repo else path


def _log(directory: Path, max_commits: int) -> str:
    args = ["git", "-C", str(directory), "log", "--all", "--no-merges", f"-n{max_commits}", "--name-only",
            f"--format={_SEP_COMMIT}%H{_SEP_FIELD}%cs{_SEP_FIELD}%s"]
    try:
        r = subprocess.run(args, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def commits(ws, index: LinkIndex, max_commits: int = MAX_COMMITS) -> list[Commit]:
    """Commits that name a known ticket, from every repo; `--all` so unmerged ticket branches count as in flight."""
    out: list[Commit] = []
    for repo, directory in repos(ws):
        for chunk in _log(directory, max_commits).split(_SEP_COMMIT)[1:]:
            head, _, names = chunk.partition("\n")
            parts = head.split(_SEP_FIELD, 2)
            if len(parts) != 3:
                continue
            keys = []
            for key in index.keys_in(parts[2]):
                t = index.ticket(key)
                if t is not None and t.id not in keys:
                    keys.append(t.id)
            if not keys:
                continue
            files = [_qualify(repo, n) for n in names.splitlines() if n.strip()]
            out.append(Commit(repo, parts[0], parts[1], keys, files))
    return out


def normalize_paths(ws, paths: list[str]) -> list[str]:
    """Paths as given (relative to the current directory, absolute, or already `<repo>:<path>`) in the qualified form
    commits use. A path outside every repo is kept as given, so it simply matches nothing."""
    checkouts = sorted(repos(ws), key=lambda r: len(str(r[1])), reverse=True)  # the innermost checkout wins
    out = []
    for raw in paths:
        p = raw.strip()
        if not p:
            continue
        if ":" in p and not Path(p).is_absolute() and p.split(":", 1)[0] in {n for n, _ in checkouts}:
            out.append(p.rstrip("/"))
            continue
        full = Path(p).expanduser().resolve()
        for repo, directory in checkouts:
            try:
                rel = full.relative_to(directory.resolve()).as_posix()
            except ValueError:
                continue
            out.append(_qualify(repo, "" if rel == "." else rel))
            break
        else:
            out.append(p.rstrip("/"))
    return out


def _matches(path: str, wanted: list[str]) -> bool:
    """A file matches a wanted path when it is that file or lies under that directory (a repo root matches all)."""
    for w in wanted:
        if w in ("", path) or path.startswith(w.rstrip("/") + "/") or (w.endswith(":") and path.startswith(w)):
            return True
    return False


def _ticket_item(entry, *, files: list[str] | None = None, commits_n: int = 0, last: str | None = None,
                 relation: str | None = None) -> dict:
    meta = entry.meta or {}
    item = {"id": entry.id, "title": meta.get("title"), "status": entry.status, "type": meta.get("type"),
            "resolution": resolution(meta, entry.status), "summary": entry.summary}
    if relation is not None:
        item["relation"] = relation
    if files is not None:
        item.update({"files": files[:5], "shared_files": len(files), "commits": commits_n, "last": last})
    return item


def _findings(entry) -> str | None:
    try:
        return store.first_line(store.read_ticket(entry.path, entry.id).section("Findings"))
    except Exception:  # a broken or vanished file: the item still shows without its findings line
        return None


def _linked(ticket_id: str, by_id: dict, entries: list) -> list[dict]:
    meta = by_id[ticket_id].meta or {}
    pairs: list[tuple[str, str]] = []
    if meta.get("parent"):
        pairs.append(("parent", str(meta["parent"])))
    pairs += [("blocked_by", str(r)) for r in meta.get("blocked_by") or []]
    pairs += [("follow_up", str(r)) for r in meta.get("follow_ups") or []]
    if meta.get("superseded_by"):
        pairs.append(("superseded_by", str(meta["superseded_by"])))
    for e in entries:
        m = e.meta or {}
        if e.id == ticket_id:
            continue
        if str(m.get("parent") or "").upper() == ticket_id.upper():
            pairs.append(("child", e.id))
        if ticket_id.upper() in {str(r).upper() for r in m.get("blocked_by") or []}:
            pairs.append(("blocks", e.id))
    out, seen = [], set()
    for relation, ref in pairs:
        e = by_id.get(ref.upper())
        if e is None or (relation, e.id) in seen:
            continue
        seen.add((relation, e.id))
        out.append(_ticket_item(e, relation=relation))
    return out


def related(ws, ref: str | None = None, paths: list[str] | None = None, *, limit: int = 8,
            max_commits: int = MAX_COMMITS) -> dict:
    """Everything around a ticket and/or paths. With a ticket and no paths, the paths are the files its commits changed."""
    entries = store.scan(ws)
    by_id = {e.id.upper(): e for e in entries}
    index = LinkIndex(ws, entries)
    ticket_id = store.resolve(ws, ref, entries).id if ref else None
    history = commits(ws, index, max_commits)

    wanted = normalize_paths(ws, paths or [])
    if not wanted and ticket_id:
        wanted = sorted({f for c in history if ticket_id in c.keys for f in c.files})

    touching = [c for c in history if any(_matches(f, wanted) for f in c.files)] if wanted else []
    shared: dict[str, set] = defaultdict(set)
    count: Counter = Counter()
    last: dict[str, str] = {}
    for c in touching:
        hit = {f for f in c.files if _matches(f, wanted)}
        for key in c.keys:
            if key == ticket_id:
                continue
            shared[key] |= hit
            count[key] += 1
            last[key] = max(last.get(key, ""), c.date)

    done, open_ = [], []
    for key in sorted(shared, key=lambda k: (-len(shared[k]), -count[k], k)):
        e = by_id.get(key.upper())
        if e is None:
            continue
        item = _ticket_item(e, files=sorted(shared[key]), commits_n=count[key], last=last[key] or None)
        (done if e.status == "done" else open_).append(item)
    for item in done[:limit]:
        item["findings"] = _findings(by_id[item["id"].upper()])

    co: Counter = Counter()
    base: Counter = Counter()
    for c in touching:
        if len(c.files) > BULK_FILES:
            continue
        base["*"] += 1
        for f in c.files:
            if not _matches(f, wanted):
                co[f] += 1
    co_changed = [{"file": f, "commits": n, "of": base["*"]} for f, n in co.most_common() if n >= MIN_CO_CHANGE][:limit]

    return {
        "ticket": _ticket_item(by_id[ticket_id.upper()]) if ticket_id else None,
        "paths": wanted[:50], "paths_total": len(wanted), "commits": len(touching),
        "repos": [name or "." for name, _ in repos(ws)],
        "history": done[:limit], "in_flight": open_[:limit], "co_changed": co_changed,
        "linked": _linked(ticket_id, by_id, entries) if ticket_id else [],
    }


def render(data: dict) -> str:
    """The text an agent reads: short lines, ticket text made visible (it is untrusted)."""
    from orch.textsafe import visible

    def tline(t: dict, extra: str = "") -> str:
        res = f" ({t['resolution']})" if t.get("resolution") not in (None, "completed") else ""
        return f"  {t['id']:<8} {t['status']:<12} {visible(t.get('title'))}{res}{extra}"

    out = []
    head = data["ticket"]
    where = f"{data['paths_total']} file(s) from {data['commits']} commit(s)"
    out.append(f"{head['id']} {visible(head.get('title'))}: {where}" if head else f"{where}")
    if not data["repos"]:
        out.append("  (no git checkout found: only frontmatter links are shown)")

    def files(t: dict) -> str:
        more = f" +{t['shared_files'] - len(t['files'])}" if t["shared_files"] > len(t["files"]) else ""
        return "      files: " + ", ".join(visible(f) for f in t["files"]) + more

    if data["history"]:
        out += ["", "history: done tickets that changed these files (read their decisions before planning)"]
        for t in data["history"]:
            out.append(tline(t, f"  · {t['commits']} commit(s), last {t['last']}"))
            if t.get("summary"):
                out.append(f"      summary: {visible(t['summary'])}")
            if t.get("findings"):
                out.append(f"      findings: {visible(t['findings'])}")
            out.append(files(t))
    if data["in_flight"]:
        out += ["", "in flight: open tickets changing the same files (conflict risk; coordinate or set blocked_by)"]
        for t in data["in_flight"]:
            out.append(tline(t))
            out.append(files(t))
    if data["co_changed"]:
        out += ["", "co-changed: files usually committed with these (check whether they need the change too)"]
        out += [f"  {visible(c['file'])}  {c['commits']} of {c['of']} commits" for c in data["co_changed"]]
    if data["linked"]:
        out += ["", "linked"]
        out += [tline(t, f"  [{t['relation']}]") for t in data["linked"]]
    if len(out) == 1:
        out.append("  nothing related found")
    return "\n".join(out)
