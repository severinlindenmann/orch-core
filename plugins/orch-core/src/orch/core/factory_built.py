"""Dark AI Factory: whether the files an epic names exist in its children's commits, and whether a child left work
uncommitted in its clone (docs/factory.md, "What was built, not only mentioned").

The coverage block (factory_report.coverage) reads text only. This module looks at commits: each child's branch tip,
fetched into the runner's release repository with the release step's git isolation (from the child's recorded clone,
or for a child with a worktree of its own from the workspace; never a ticket field in place of a clone record), its
whole tree listed with `ls-tree`. A named file is there when a path of that tree is the name or ends in `/<name>`,
compared without case (the coverage rule). A near match by edit distance is shown to help the human ("found
similar: ..."); it never decides anything. A child's clone is read with `git status` under the clone's lock, its
config written again first (factory_clones.fetch_from): untracked files and changes not committed are named.

Used by the Ready report (shown), the release's merge stage (a named file in no child's commit blocks the merge) and
the auto-close (a named file absent from the merged commits, or uncommitted work, keeps the epic open)."""
from __future__ import annotations

import time

TTL = 30.0  # seconds a child's tree and clone state are reused by the Ready report (page views re-read it)
_CACHE: dict = {}  # ponytail: per-process memo by (workspace, child); a shared record if several dashboards read it
NS = "built-heads"  # the ref folder of these fetches in the release repository (never one a release stage compares)


def _git_rec(ws) -> dict:
    """The recipe for the runner's git flags, or {} without one (no credential helper or ssh command then)."""
    from orch.core import factory_release as fr
    rec, _ = fr.load(ws)
    return rec or {}


def tree(ws, rec: dict, sha: str) -> list[str] | None:
    """Every path of commit `sha`'s tree in the release repository, or None."""
    from orch.core import factory_release as fr
    r = fr._git(ws, rec, "ls-tree", "-r", "-z", "--name-only", "--full-tree", sha, limit=fr.GIT_OUT)
    if r.get("code") != 0 or r.get("out_size", 0) > fr.GIT_OUT:
        return None
    return [p for p in (r.get("out") or "").split("\x00") if p]


def child_tree(ws, t) -> tuple[str | None, list[str] | None, str]:
    """(the tip of child `t`'s branch, its tree's paths, "") or (None, None, why)."""
    from orch.core import factory_release as fr
    branch, src, why = fr.child_source(ws, t)
    if branch is None:
        return None, None, why
    rec = _git_rec(ws)
    try:
        fr.ensure_repo(ws, rec)
        sha = fr.fetch_child(ws, rec, branch, src, t.id, ns=NS)
    except fr.ReleaseError as e:
        return None, None, str(e)
    if sha is None:
        return None, None, f"{t.id}'s branch could not be fetched"
    paths = tree(ws, rec, sha)
    return (sha, paths, "") if paths is not None else (None, None, f"the tree of {t.id}'s commit could not be read")


UNREADABLE = {"ok": False, "lines": [], "submodules": []}


def uncommitted(ws, child: str) -> dict | None:
    """What the child's runner-made clone holds that is not committed: {ok: True, lines: [porcelain lines, at most
    20], submodules: [gitlink paths, never inspected]}; UNREADABLE ({ok: False}) when it has a clone record but its
    state cannot be read (never taken as clean); None when the runner made no clone for it.

    No program of the clone's runs: `git status` with `--ignore-submodules=all` (git never enters a gitlink, whose own
    config and attributes could name a filter), submodule recursion, the submodule summary and the untracked cache
    off (factory_clones._git adds these to the release isolation), under the clone's lock with its config written
    again first (factory_clones.fetch_from). Gitlinks are listed from the index (`ls-files -s`, mode 160000)."""
    from orch.core import factory_clones, factory_runner
    if factory_clones.record(ws, child) is None:
        return None
    git = factory_runner.resolve_bin("git")
    if git is None:
        return UNREADABLE

    def status(path):
        r = factory_clones._git(git, ws, "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all",
                                "--ignore-submodules=all", cwd=str(path), timeout=30)
        s = factory_clones._git(git, ws, "-C", str(path), "ls-files", "-s", "-z", cwd=str(path), timeout=30)
        if r.get("code") != 0 or s.get("code") != 0:
            return None
        subs = [x.split("\t", 1)[1] for x in (s.get("out") or "").split("\x00")
                if x.startswith("160000 ") and "\t" in x]
        return {"ok": True, "lines": [x for x in (r.get("out") or "").split("\x00") if x.strip()][:20],
                "submodules": subs[:20]}
    got = factory_clones.fetch_from(ws, child, status)
    return got if got is not None else UNREADABLE


def has(paths, name: str) -> bool:
    n = name.casefold()
    return any(p.casefold() == n or p.casefold().endswith("/" + n) for p in paths)


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similar(paths, name: str, n: int = 3) -> list[str]:
    """Up to `n` paths whose file name is within a small edit distance of `name`'s (a help for the human only)."""
    base = name.casefold().rsplit("/", 1)[-1]
    limit = max(2, len(base) // 4)
    scored = sorted((d, p) for p in set(paths)
                    if 0 < (d := _distance(base, p.casefold().rsplit("/", 1)[-1])) <= limit)
    return [p for _, p in scored[:n]]


def missing(files, trees) -> list[dict]:
    """[{name, similar}] for each named file in none of `trees` (lists of paths)."""
    every = [p for t in trees for p in t]
    return [{"name": f, "similar": similar(every, f)} for f in files if not has(every, f)]


def missing_text(miss: list[dict], where: str = "any child's commit") -> str:
    """'Not in any child's commit: a.json (found similar: b.json), c.html'"""
    return f"Not in {where}: " + ", ".join(
        m["name"] + (f" (found similar: {', '.join(m['similar'])})" if m["similar"] else "") for m in miss)


def _cached(ws, t):
    key = (str(ws.root), t.id)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    val = (child_tree(ws, t), uncommitted(ws, t.id))
    _CACHE[key] = (time.monotonic(), val)
    return val


def built(ws, epic, files, kids) -> dict | None:
    """For the Ready report: {missing: [{name, similar}], dirty: [{id, lines}], unknown: [{id, why}], subs: [{id,
    paths}]} over the children `kids` ([(entry, Ticket)], those that count), or None outside a git checkout (nothing
    to look at). A clone whose state cannot be read is `unknown`, never clean."""
    from orch.core import factory_release as fr
    if fr.workspace_repo(ws) is None:
        return None
    trees, dirty, unknown, subs = [], [], [], []
    for _, t in kids:
        (sha, paths, why), status = _cached(ws, t)
        if paths is None:
            unknown.append({"id": t.id, "why": why})
        else:
            trees.append(paths)
        if status is not None and not status["ok"]:
            unknown.append({"id": t.id, "why": "the state of its clone could not be read"})
        elif status:
            if status["lines"]:
                dirty.append({"id": t.id, "lines": status["lines"]})
            if status["submodules"]:
                subs.append({"id": t.id, "paths": status["submodules"]})
    return {"missing": missing(files, trees) if files and not unknown else [], "dirty": dirty, "unknown": unknown,
            "subs": subs}


def epic_files(epic) -> list[str]:
    """The file names the epic's Requirements and Acceptance criteria name (factory_report.named_files)."""
    from orch.core.factory_report import named_files
    return named_files(f"{epic.section('Requirements')}\n{epic.section('Acceptance criteria')}")


def merge_refusal(ws, rec: dict, epic, kids: list[str], found: dict) -> tuple[str, str] | None:
    """(the child the merge block is recorded on, why) when the merge stage may not start: a file the epic names that
    is in no child's commit (the classified tips in `found`, and the commits already merged; a file already on the
    base counts, since a tip's whole tree is read), or commits that cannot be listed. The block goes to the child
    whose text names the missing file (factory_report.coverage), else the first child. None when the epic names no
    file."""
    from orch.core import factory_release as fr, factory_report
    files = epic_files(epic)
    if not files:
        return None
    trees = []
    for k in kids:
        sha = found[k][1] if k in found else fr.unit_state(ws, epic.id, "merge", k).get("sha")
        if not isinstance(sha, str):
            continue  # not checked this round and never merged: it fails its merge stage on its own
        paths = tree(ws, rec, sha)
        if paths is None:
            return k, f"the commit of {k} could not be listed to check the files the epic names"
        trees.append(paths)
    miss = missing(files, trees)
    if not miss:
        return None
    try:
        named = factory_report.coverage(ws, epic)["covered"].get(miss[0]["name"]) or []
    except Exception:
        named = []
    owner = next((k for k in named if k in kids), sorted(found)[0] if found else kids[0])
    return owner, (missing_text(miss) + f": the epic names it, so the merge does not start; Retry release once "
                   f"{owner} (or another child) commits it on its branch")


def close_blockers(ws, epic, d, units: list[str], testing: list[str]) -> list[str]:
    """Why the epic may not close by itself, read fresh (no cache): a named file absent from the merged commits (a
    charter that signs a release) or from the children's commits (one that does not), commits that cannot be read,
    and a child in testing with uncommitted work in its clone."""
    from orch.core import factory_release as fr
    out = []
    files = epic_files(epic)
    if files:
        trees, rec, later = [], _git_rec(ws), False
        for k in units:
            if d.get("release"):
                us = fr.unit_state(ws, epic.id, "merge", k)
                if us["state"] != "proven":
                    later = True  # its merge is still to run: the release blocker says so, checked once merged
                    continue
                paths = tree(ws, rec, us["sha"]) if isinstance(us.get("sha"), str) else None
            else:
                t = fr._ticket(ws, k)
                paths = child_tree(ws, t)[1] if t is not None else None
            if paths is None:
                out.append(f"the commit of {k} could not be read to check the files the epic names")
            else:
                trees.append(paths)
        miss = missing(files, trees) if not out and not later else []
        if miss:
            out.append(missing_text(miss, "any merged commit" if d.get("release") else "any child's commit"))
    for k in testing:
        st = uncommitted(ws, k)
        if st is None:
            continue
        if not st["ok"]:
            out.append(f"the state of {k}'s clone could not be read")
            continue
        if st["lines"]:
            out.append(f"{k} has uncommitted work in its clone")
        if st["submodules"]:
            out.append(f"{k}'s clone holds a submodule orch does not inspect ({', '.join(st['submodules'][:5])})")
    return out
