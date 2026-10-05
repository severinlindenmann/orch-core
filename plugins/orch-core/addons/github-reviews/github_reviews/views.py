"""Code reviews widgets (spec v2 §12): the page, Today and the ticket's Code panel. Reads the cache through `view`
only and never runs commands (orch refuses ctx.run while a page renders). Item fields are read defensively."""
from __future__ import annotations

from urllib.parse import urlencode

from orch.addons.widgets import KV, Action, Badge, Callout, Card, Chips, Link, Table, Text, Tile

from .pending import pending

STATES = (("review", "Needs your review"), ("mine", "Yours"), ("failing", "Checks failing"), ("draft", "Drafts"),
          ("all", "All open"))
_STATE_LABEL = dict(STATES)
CHECKS = {"passed": ("ok", "checks passed"), "failed": ("err", "checks failing"), "pending": ("info", "checks running"),
          "cancelled": ("neu", "checks cancelled"), "none": ("neu", "no checks")}
REVIEWS = {"approved": ("ok", "approved"), "changes_requested": ("warn", "changes requested"),
           "required": ("info", "review required"), "none": ("neu", "review required")}
MERGE = {"conflict": ("warn", "merge conflict")}
SIZES = ((10, "XS"), (50, "S"), (250, "M"), (1000, "L"))
COLUMNS = ("PR", "Title", "Checks", "Review", "Next")  # DESIGN.md: at most 5 columns on a page
_NO_PROVIDER = ("no provider for this host", "no remote named origin")


def _refs(item) -> list[str]:
    refs = item.get("body_refs")
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def checks_state(item) -> str:
    checks = item.get("checks")
    state = checks.get("state") if isinstance(checks, dict) else None
    return state if state in CHECKS else "none"


def failed_runs(item) -> list[str]:
    checks = item.get("checks")
    runs = checks.get("failed_runs") if isinstance(checks, dict) else None
    return [r for r in runs if isinstance(r, str) and r.isdigit()] if isinstance(runs, list) else []


def size_label(item, files: bool = True) -> str:
    add, dele = _int(item.get("additions")), _int(item.get("deletions"))
    if add is None or dele is None:
        return "size unknown"
    chip = next((label for limit, label in SIZES if add + dele < limit), "XL")
    n = _int(item.get("changed_files"))
    return f"{chip} · +{add} −{dele}" + (f" · {n} file{'' if n == 1 else 's'}" if files and n is not None else "")


def matches(item, state: str, me) -> bool:
    if state == "review":
        reviewers = item.get("requested_reviewers")
        return bool(me) and isinstance(reviewers, list) and me in reviewers and item.get("author") != me
    if state == "mine":
        return bool(me) and item.get("author") == me
    if state == "failing":
        return checks_state(item) == "failed"
    if state == "draft":
        return item.get("draft") is True
    return True


def _text_of(item: dict):
    text = item.get("text")
    return text if isinstance(text, str) else None


def _badge(item: dict):
    role, text = item.get("role"), item.get("text")
    return Badge(role, text) if role in ("ok", "info", "warn", "err", "neu") and isinstance(text, str) else None


def _http(url) -> bool:
    return isinstance(url, str) and url.startswith("https://")


class Data:
    """The cached snapshots behind one render, by repo scope."""

    def __init__(self, view):
        self.view = view
        self.repos = view.repos()
        self.prs = {s.scope: s for s in view.snapshots("github")}
        self.local = {s.scope: s for s in view.snapshots("local-git")}
        self.me = next((s.me for s in self.prs.values() if s.me), None)
        self._links = None

    @property
    def links(self):
        """Built only when something needs it (e.g. the Today summary never does)."""
        if self._links is None:
            self._links = self.view.links()
        return self._links

    def fetched_at(self, item=None):
        """When the newest good fetch landed (the age of everything this page shows); None before the first one."""
        times = [s.fetched_at for s in self.prs.values() if s.health != "never_fetched" and s.fetched_at]
        return max(times) if times else None

    def label(self, scope: str) -> str:
        """The repo as a page names it: the workspace's own repo as "Harness (name)"."""
        repo = next((r for r in self.repos if r.name == scope), None)
        return repo.label if repo is not None else scope

    def scopes(self) -> list[str]:
        names = [r.name for r in self.repos]
        return names + sorted(s for s in set(self.prs) | set(self.local) if s not in names)

    def items(self, scope) -> list[dict]:
        snap = self.prs.get(scope)
        return [i for i in (snap.items if snap else ()) if isinstance(i, dict)]

    def all_items(self) -> list[tuple[str, dict]]:
        return [(s, i) for s in self.scopes() for i in self.items(s)]

    def tickets(self, item) -> list:
        return self.links.for_review(url=_str(item.get("url")), branch=_str(item.get("source_branch")),
                                     title=_str(item.get("title")), body=" ".join(_refs(item)))

    def local_item(self, scope, item_id) -> dict:
        snap = self.local.get(scope)
        return next((i for i in (snap.items if snap else ()) if isinstance(i, dict) and i.get("id") == item_id), {})


def _url(view, **query) -> str:
    q = urlencode({k: v for k, v in query.items() if v})
    return f"/addons/{view.addon}/" + (f"?{q}" if q else "")


def _ref(d: Data, scope, item) -> str:
    return f"{d.label(scope)} #{item.get('number')}"


def _provider(snap, remote: dict):
    message = snap.message if snap is not None else ""
    if message.startswith(_NO_PROVIDER):
        return Badge("neu", message)
    if remote.get("host") == "github.com" or (snap is not None and snap.health in ("ok", "stale")):
        return Badge("ok", "GitHub")
    return None


def _me_known(d: Data) -> bool:
    return any(s.health != "never_fetched" for s in d.prs.values())


def _me_unknown(d: Data) -> bool:
    return d.me is None and _me_known(d)


def _labelled(badge, label: str):
    """A local-git badge with what it is about ("Uncommitted: 2 files"), since a chip row has no labels."""
    return Badge(badge.role, f"{label}: {badge.text}") if badge is not None else None


def _repo_card(d: Data, scope: str) -> Card:
    """A compact card for the Repositories grid: who it is, its local state as chips, and three counts."""
    repo = next((r for r in d.repos if r.name == scope), None)
    snap = d.prs.get(scope)
    items = d.items(scope)
    known = snap is not None and snap.health != "never_fetched"
    failing = sum(1 for i in items if checks_state(i) == "failed") if known else None
    need_review = "unknown" if _me_unknown(d) else sum(1 for i in items if matches(i, "review", d.me))
    vs = d.local_item(scope, "vs-default")
    vs_text = _text_of(vs)
    vs_label = vs.get("label") if isinstance(vs.get("label"), str) else "vs default branch"
    branch = _text_of(d.local_item(scope, "branch"))
    who = (Text(repo.role if repo else "cached only"), _provider(snap, d.local_item(scope, "remote")),
           Text(f"branch {branch}") if branch else None)
    state = [_badge(d.local_item(scope, "state")), Text(f"{vs_text} {vs_label}") if vs_text else None,
             _labelled(_badge(d.local_item(scope, "changes")), "Uncommitted"),
             _labelled(_badge(d.local_item(scope, "commit-check")), "Commit check")]
    if snap is not None and snap.health not in ("ok", "never_fetched"):
        state.append(Badge("warn" if snap.health != "error" else "err", f"{snap.health}: {snap.message}"[:200]))
    counts = KV((("open PRs", len(items) if known else None),
                 ("need review", need_review if known else None),
                 ("failing", Badge("err", str(failing)) if failing else failing)), layout="stats")
    body = tuple(w for w in (Chips(tuple(x for x in who if x is not None), label=f"{d.label(scope)}: repository", show_label=False),
                             Chips(tuple(x for x in state if x is not None), label=f"{d.label(scope)}: local state", show_label=False),
                             counts) if not isinstance(w, Chips) or w.items)
    return Card(d.label(scope), body, role="warn" if snap is not None and snap.health not in ("ok", "never_fetched") else None)


def _login_help(d: Data) -> list:
    # Login needed is core's health callout (the Snapshot message "login needed: run gh auth login" gives it the
    # command to copy); drawing a second one here would say it twice.
    if any(s.health == "error" and "gh is not installed" in s.message for s in d.prs.values()):
        return [Callout("err", "GitHub CLI is not installed", "Install gh from https://cli.github.com, then press Refresh.")]
    if _me_unknown(d):
        return [Callout("warn", "GitHub login unknown", "GitHub login unknown, review counts unavailable.")]
    return []


def _count_label(pool, key: str, d: Data) -> str:
    if key in ("review", "mine") and _me_unknown(d):
        return "unknown"
    return str(sum(1 for _, i in pool if matches(i, key, d.me)))


def _filters(d: Data, state: str, repo: str) -> Card:
    """Two chip rows, state with counts and repository; the shown filter is the current chip."""
    pool = [(s, i) for s, i in d.all_items() if not repo or s == repo]
    states = tuple(Link(f"{label} {_count_label(pool, key, d)}", _url(d.view, state=key, repo=repo), current=key == state)
                   for key, label in STATES)
    repos = (Link("All repos", _url(d.view, state=state), current=not repo),) + tuple(
        Link(d.label(s), _url(d.view, state=state, repo=s), current=s == repo) for s in d.scopes())
    return Card(f"Showing: {_STATE_LABEL[state]}" + (f" in {d.label(repo)}" if repo else ""),
                (Chips(states, label="Filter by state"), Chips(repos, label="Filter by repository")))


def _row(d: Data, item: dict) -> tuple:
    url = item.get("url")
    tickets = d.tickets(item)
    first = tickets[0] if tickets else None
    state = checks_state(item)
    review = Badge("neu", "draft") if item.get("draft") is True else Badge(*REVIEWS.get(item.get("review"), REVIEWS["none"]))
    target = f"{_str(item.get('repo'))}#{item.get('number')}"
    what = f"{target} \"{_str(item.get('title'))[:80]}\""
    action = None
    if pending(d.view.state_dir, target, d.fetched_at(item)):
        action = Text("Rerun requested" if state == "failed" else "Marked ready")
    elif state == "failed" and failed_runs(item):
        action = Action("rerun_failed", "Rerun failed", target, confirm=f"Rerun the failed checks of {what}?",
                        detail="Asks GitHub to run the failed checks again. Nothing in your tickets changes.")
    elif item.get("draft") is True:
        action = Action("mark_ready", "Mark ready", target, confirm=f"Mark {what} ready for review?",
                        detail="Takes the pull request out of draft on GitHub and lets reviewers be notified.")
    merge = MERGE.get(item.get("mergeable"))
    number = f"#{item.get('number')}"
    author = _str(item.get("author"))
    big = size_label(item, files=False) if (_int(item.get("additions")) or 0) + (_int(item.get("deletions")) or 0) >= 1000 else ""
    title = " · ".join(x for x in (_str(item.get("title")), f"by {author}" if author and d.me and author != d.me else "",
                                   "merge conflict" if merge else "", big,
                                   first.id if first and first.id not in _str(item.get("title")) else "") if x)
    if action is None and state == "failed" and first:
        action = Link("Ask agent to fix", f"/t/{first.id}#start-agent-{first.id}")
    elif action is None and first:
        action = Link(first.id, f"/t/{first.id}")
    return (Link(number, url) if _http(url) else number, title, Badge(*CHECKS[state]), review, action)


def _lists(d: Data, state: str, repo: str) -> list:
    out = []
    for scope in d.scopes():
        if repo and scope != repo:
            continue
        rows = tuple(_row(d, i) for i in d.items(scope) if matches(i, state, d.me))
        if rows:
            out.append(Card(d.label(scope), (Table(COLUMNS, rows, empty="No pull requests match this filter. Pick another filter above."),)))
    if not out:  # an empty state is plain text, not a callout (DESIGN.md: one callout per page, for situations)
        out.append(Text(f"Nothing in {_STATE_LABEL[state]}. Pick another filter above, or wait for the next refresh."))
    return out


def _multi_repo(d: Data):
    by_ticket: dict[str, list[tuple[str, dict]]] = {}
    for scope, item in d.all_items():
        for t in d.tickets(item):
            by_ticket.setdefault(t.id, []).append((scope, item))
    multi = {tid: prs for tid, prs in by_ticket.items() if len({s for s, _ in prs}) > 1}
    if not multi:
        return None
    order = d.links.merge_order(list(multi))
    rows = tuple((Link(tid, f"/t/{tid}"), ", ".join(_ref(d, s, i) for s, i in prs)) for tid, prs in sorted(multi.items()))
    text = ("Suggested merge order: " + " → ".join(order)) if order else \
        "No merge order suggested: these tickets have no blocked_by links between them."
    return Card("One ticket, several repos", (Table(("Ticket", "Pull requests"), rows), Text(text)))


def _loading(d: Data) -> bool:
    """Before the first fetch there is nothing to count: dashes and "0" would read as a real, empty result."""
    snaps = list(d.prs.values())
    return all(s.health == "never_fetched" for s in snaps) if snaps else not d.local


def page(view) -> list:
    d = Data(view)
    if _loading(d):
        return [Card("Loading pull requests...", (Text("The first fetch from GitHub is running; this page fills in "
                                                       "when it finishes. Press Refresh to check again."),))]
    asked = view.params.get("state")
    notes = []
    state = asked if asked in _STATE_LABEL else "review"
    if asked and asked not in _STATE_LABEL:
        notes.append(Text(f"Unknown filter {asked!r}; showing {_STATE_LABEL[state]}."))
    repo = view.params.get("repo", "")
    if repo and repo not in d.scopes():
        notes.append(Text(f"Unknown repository {repo!r}; showing all repositories."))
        repo = ""
    if asked is None and not any(matches(i, state, d.me) for s, i in d.all_items() if not repo or s == repo):
        # the landing view must not be empty just because you opened every pull request yourself
        fallback = next((k for k in ("mine", "all") if any(matches(i, k, d.me) for s, i in d.all_items()
                                                          if not repo or s == repo)), None)
        if fallback is not None:
            notes.append(Text(f"Nothing needs your review right now; showing {_STATE_LABEL[fallback]}."))
            state = fallback
    out = [Card("Repositories", tuple(_repo_card(d, s) for s in d.scopes()), layout="grid"), *_login_help(d),
           *notes, _filters(d, state, repo), *_lists(d, state, repo)]
    multi = _multi_repo(d)
    if multi is not None:
        out.append(multi)
    return out


def summary(view) -> list:
    d = Data(view)
    if not d.prs:
        return []
    failing = [s for s, i in d.all_items() if checks_state(i) == "failed"]
    repos = len(set(failing))
    sub = f"in {repos} repo{'s' if repos != 1 else ''}"
    return [Tile("Checks failing", len(failing), "err", href=_url(view, state="failing"), sub=sub)] if failing else []


def today_items(view) -> list:
    d = Data(view)
    rows = []
    for scope, item in d.all_items():
        url = item.get("url")
        tickets = d.tickets(item)
        first = tickets[0] if tickets else None
        if matches(item, "review", d.me):
            rows.append((Link(f"Review requested on {_ref(d, scope, item)}", url) if _http(url) else f"Review requested on {_ref(d, scope, item)}",
                         Link(first.id, f"/t/{first.id}") if first else None))
        if checks_state(item) == "failed" and ((d.me and item.get("author") == d.me) or first is not None):
            rows.append((Link(f"Checks failing on {_ref(d, scope, item)}", url) if _http(url) else f"Checks failing on {_ref(d, scope, item)}",
                         Link("Ask agent to fix", f"/t/{first.id}#start-agent-{first.id}") if first else None))
    return [Card("Code reviews", (Table(("Item", "Next"), tuple(rows)),))] if rows else []


def ticket_prs(view) -> list:
    ticket = view.ticket
    if ticket is None:
        return []
    d = Data(view)
    rows = []
    for scope, item in d.all_items():
        if any(t.id == ticket.id for t in d.tickets(item)):
            url = item.get("url")
            review = Badge("neu", "draft") if item.get("draft") is True else Badge(*REVIEWS.get(item.get("review"), REVIEWS["none"]))
            rows.append((Link(_ref(d, scope, item), url) if _http(url) else _ref(d, scope, item), Badge(*CHECKS[checks_state(item)]), review))
    return [Table(("PR", "Checks", "Review"), tuple(rows))] if rows else []


def widgets(slot, view) -> list:
    if slot == f"page.{view.addon}":
        return page(view)
    if slot == "today.summary":
        return summary(view)
    if slot == "today.from_addons":
        return today_items(view)
    if slot == "ticket.code":
        return ticket_prs(view)
    return []
