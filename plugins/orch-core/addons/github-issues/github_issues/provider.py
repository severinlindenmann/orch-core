"""GitHub issues of the workspace's GitHub trackers (spec v2 §13.1): one `gh issue list` per tracker repo and interval.
GitHub has no sprints: a milestone is a sprint and its due date is the sprint's end (addons §7.2)."""
from __future__ import annotations

import re

from orch.addons.api import Snapshot

from .gh import GhFailure, Whoami, run_json

ISSUE_FIELDS = "number,title,state,stateReason,assignees,labels,milestone,url,createdAt,updatedAt,closedAt,id"
LIMIT = 300
RANKS = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
NO_RANK = 4
_ISSUES_URL = re.compile(r"https://(?P<host>[^/\s]+)/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/issues/\{(?:id|key)\}")
_IN_PROGRESS = ("status:in-progress", "status:in progress", "in progress", "in-progress")
_FAR = "9999-12-31"


def github_trackers(tracker_refs) -> dict:
    """{"owner/repo": TrackerRef} for trackers whose URL is a github.com issue URL; the first tracker per repo wins."""
    out = {}
    for t in tracker_refs:
        m = _ISSUES_URL.fullmatch(t.url)
        if m and m.group("host").lower() == "github.com":
            out.setdefault(f"{m.group('owner')}/{m.group('repo')}", t)
    return out


def _names(values, field: str) -> list[str]:
    return [v[field] for v in values or [] if isinstance(v, dict) and isinstance(v.get(field), str) and v[field]]


def label_value(labels, name: str) -> str | None:
    prefix = f"{name}:"
    for label in labels:
        if label.lower().startswith(prefix) and label[len(prefix):].strip():
            return label[len(prefix):].strip().lower()
    return None


def sprints(issues, today: str) -> dict:
    """{milestone number: {name, number, state, start, end}}; `today` is YYYY-MM-DD (UTC)."""
    found: dict[int, dict] = {}
    for issue in issues:
        ms = issue.get("milestone") if isinstance(issue, dict) else None
        if not isinstance(ms, dict) or not isinstance(ms.get("number"), int):
            continue
        s = found.setdefault(ms["number"], {"name": str(ms.get("title") or f"Milestone {ms['number']}"), "number": ms["number"],
                                            "end": ms.get("dueOn") if isinstance(ms.get("dueOn"), str) else None, "open": 0})
        if issue.get("state") != "CLOSED":
            s["open"] += 1
    ordered = sorted(found.values(), key=lambda s: ((s["end"] or _FAR)[:10], s["number"]))
    open_ones = [s for s in ordered if s["open"]]
    current = next((s for s in open_ones if s["end"] and s["end"][:10] >= today), None) or (open_ones[0] if open_ones else None)
    after = False
    out, previous_end = {}, None
    for s in ordered:
        if s is current:
            state, after = "active", True
        elif not s["open"]:
            state = "closed"
        else:
            state = "future" if after else "open"
        out[s["number"]] = {"name": s["name"], "number": s["number"], "state": state,
                            "start": previous_end if s["end"] else None, "end": s["end"]}
        previous_end = s["end"] or previous_end
    return out


def issue_item(issue: dict, tracker, me, sprint_by_number: dict) -> dict | None:
    number, url = issue.get("number"), issue.get("url")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(url, str) or not url.startswith("https://"):
        return None
    key = tracker.key_for(number)
    if key is None:
        return None
    labels = _names(issue.get("labels"), "name")
    assignees = _names(issue.get("assignees"), "login")
    closed = issue.get("state") == "CLOSED"
    priority = label_value(labels, "priority")
    ms = issue.get("milestone")
    sprint = sprint_by_number.get(ms.get("number")) if isinstance(ms, dict) else None
    item = {
        "tracker": tracker.prefix, "key": key, "native_id": issue.get("id"), "number": number, "url": url,
        "title": str(issue.get("title") or ""), "type": label_value(labels, "type"),
        "raw_status": "closed" if closed else "open",
        "category": "done" if closed else "in_progress" if any(lb.lower() in _IN_PROGRESS for lb in labels) else "todo",
        "resolution": (str(issue.get("stateReason") or "").lower() or None) if closed else None,
        "priority": priority, "rank": RANKS.get(priority, NO_RANK),
        "assignee": assignees[0] if assignees else None, "assignees": assignees,
        "assigned_to_me": bool(me) and me in assignees, "labels": labels,
        "sprint": dict(sprint) if sprint else None, "due": sprint["end"] if sprint else None,
        "closed_at": issue.get("closedAt") if isinstance(issue.get("closedAt"), str) else None,
    }
    for key_name, field in (("created_at", "createdAt"), ("updated_at", "updatedAt")):
        if isinstance(issue.get(field), str):  # never None: item_problems needs an ISO datetime with an offset
            item[key_name] = issue[field]
    return item


class IssuesProvider:
    id = "issues"
    kind = "issues"
    interval_s = 300

    def __init__(self):
        self.whoami = Whoami()

    def scopes(self, ctx):
        return list(github_trackers(ctx.trackers()))

    def fetch(self, ctx, scope, previous):
        at = ctx.now()
        tracker = github_trackers(ctx.trackers()).get(scope)
        if tracker is None:
            return Snapshot(self.id, scope, at, health="error", message=f"{scope} is no longer a GitHub tracker in external_trackers")
        if tracker.key_for(1) is None:
            return Snapshot(self.id, scope, at, health="error",
                            message=f"the {tracker.prefix} tracker pattern matches neither {tracker.prefix}-<number> nor <number>")
        try:
            me = self.whoami(ctx)
            data = run_json(ctx, ["gh", "issue", "list", "--repo", scope, "--state", "all", "--limit", str(LIMIT),
                                  "--json", ISSUE_FIELDS], timeout=30)
        except GhFailure as f:
            return Snapshot(self.id, scope, at, health=f.health, message=f.message, retry_after=f.retry_after)
        if not isinstance(data, list):
            return Snapshot(self.id, scope, at, health="error", message="gh issue list did not return a list")
        issues = [i for i in data if isinstance(i, dict)]
        by_number = sprints(issues, at.date().isoformat())
        items = tuple(x for x in (issue_item(i, tracker, me, by_number) for i in issues) if x is not None)
        full = len(data) >= LIMIT
        return Snapshot(self.id, scope, at, items=items, me=me, complete=not full,
                        message=f"showing the newest {LIMIT} issues" if full else "")
