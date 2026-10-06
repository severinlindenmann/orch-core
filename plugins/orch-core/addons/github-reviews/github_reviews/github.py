"""Open pull requests per repo (spec v2 §12): one `gh pr list` per GitHub repo and interval; "me" once a day."""
from __future__ import annotations

import re

from orch.addons.api import Snapshot
from orch.addons.runner import AddonRunError

from .gh import GhFailure, Whoami, gh_user, run_json, token_env
from .localgit import remote_of

PR_FIELDS = ("number,title,author,headRefName,baseRefName,headRepository,headRefOid,isDraft,reviewDecision,"
             "reviewRequests,statusCheckRollup,mergeable,additions,deletions,changedFiles,labels,createdAt,updatedAt,url,id,"
             "body")
_KEY = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*-\d+(?![0-9])")
MAX_BODY_REFS = 20
NO_ACCESS = "no access:"  # message prefix of a repo gh's account cannot see; the page shows it on that repo only
LIMIT = 100
_RUN_ID = re.compile(r"/actions/runs/(\d+)(?:/|$)")
_PASSED = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_STATUS_FAILED = frozenset({"FAILURE", "ERROR"})
_REVIEW = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "REVIEW_REQUIRED": "required"}
_MERGEABLE = {"MERGEABLE": "yes", "CONFLICTING": "conflict"}


def checks_of(rollup) -> dict:
    counts = {"passed": 0, "failed": 0, "pending": 0, "cancelled": 0}
    runs: list[str] = []
    url = None
    for c in rollup if isinstance(rollup, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("__typename") == "StatusContext":
            state = str(c.get("state") or "").upper()
            link = c.get("targetUrl")
            outcome = "passed" if state == "SUCCESS" else "failed" if state in _STATUS_FAILED else "pending"
        else:
            link = c.get("detailsUrl")
            conclusion = str(c.get("conclusion") or "").upper()
            if str(c.get("status") or "").upper() != "COMPLETED":
                outcome = "pending"
            else:
                outcome = "passed" if conclusion in _PASSED else "cancelled" if conclusion == "CANCELLED" else "failed"
        counts[outcome] += 1
        if outcome == "failed" and isinstance(link, str) and link.startswith("https://"):
            url = url or link
            m = _RUN_ID.search(link)
            if m and m.group(1) not in runs:
                runs.append(m.group(1))
    if not any(counts.values()):
        state = "none"
    elif counts["failed"]:
        state = "failed"
    elif counts["pending"]:
        state = "pending"
    elif counts["passed"]:
        state = "passed"
    else:
        state = "cancelled"
    return {"state": state, **counts, "url": url, "failed_runs": runs}


def body_refs(body) -> list[str]:
    """Ticket-key-like words in a PR description (#14), so the core can link the PR by a key named only there. The
    body itself is not kept; the core decides which of these are local tickets or tracker keys."""
    out: list[str] = []
    for m in _KEY.finditer(body if isinstance(body, str) else ""):
        key = m.group(0).upper()
        if key not in out:
            out.append(key)
        if len(out) == MAX_BODY_REFS:
            break
    return out


def _login(value) -> str | None:
    return value["login"] if isinstance(value, dict) and isinstance(value.get("login"), str) else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def pr_item(pr: dict, *, host: str, repo: str, me: str | None) -> dict | None:
    number, url = pr.get("number"), pr.get("url")
    if _count(number) is None or not isinstance(url, str) or not url.startswith("https://"):
        return None
    author = _login(pr.get("author"))
    requested: list[str] = []
    for r in pr.get("reviewRequests") or []:
        name = (r.get("login") or r.get("slug") or r.get("name")) if isinstance(r, dict) else None
        if isinstance(name, str) and name and name not in requested:
            requested.append(name)
    head = pr.get("headRepository") if isinstance(pr.get("headRepository"), dict) else {}
    item = {
        "provider": "github", "host": host, "repo": repo, "number": number, "native_id": pr.get("id"), "url": url,
        "title": str(pr.get("title") or ""), "author": author, "body_refs": body_refs(pr.get("body")),
        "source_branch": str(pr.get("headRefName") or ""), "target_branch": str(pr.get("baseRefName") or ""),
        "source_repo": head["nameWithOwner"] if isinstance(head.get("nameWithOwner"), str) else repo,
        "head_sha": pr["headRefOid"] if isinstance(pr.get("headRefOid"), str) else None,
        "state": "open", "raw_state": "OPEN", "draft": pr.get("isDraft") is True,
        "review": _REVIEW.get(str(pr.get("reviewDecision") or ""), "none"),
        "requested_reviewers": requested,
        "my_role": "author" if me and author == me else "reviewer" if me and me in requested else "none",
        "checks": checks_of(pr.get("statusCheckRollup")),
        "mergeable": _MERGEABLE.get(str(pr.get("mergeable") or ""), "unknown"),
        "additions": _count(pr.get("additions")), "deletions": _count(pr.get("deletions")),
        "changed_files": _count(pr.get("changedFiles")),
        "labels": [lb["name"] for lb in pr.get("labels") or [] if isinstance(lb, dict) and isinstance(lb.get("name"), str)],
    }
    for key, field in (("created_at", "createdAt"), ("updated_at", "updatedAt")):
        if isinstance(pr.get(field), str):  # never None: item_problems needs an ISO datetime with an offset
            item[key] = pr[field]
    return item


class GitHubProvider:
    id = "github"
    kind = "reviews"
    interval_s = 300

    def __init__(self):
        self.whoami = Whoami()

    def scopes(self, ctx):
        return [r.name for r in ctx.repos()]

    def fetch(self, ctx, scope, previous):
        at = ctx.now()
        repo = next((r for r in ctx.repos() if r.name == scope), None)
        if repo is None:
            return Snapshot(self.id, scope, at, health="error", message=f"{scope} is no longer in git.repos")
        try:
            remote = remote_of(ctx, repo)
        except AddonRunError as e:
            return Snapshot(self.id, scope, at, health="error", message=e.message)
        if remote is None:
            return Snapshot(self.id, scope, at, message="no remote named origin")
        host, full_name = remote
        kind = getattr(repo, "git_type", "github") or "github"
        if kind != "github":  # git.repos.<name>.type (#165)
            return Snapshot(self.id, scope, at, message=f"no provider for this host: {host} ({kind})")
        if host != "github.com":
            return Snapshot(self.id, scope, at, message=f"no provider for this host: {host}")
        listing = False
        try:
            env = token_env(ctx)
            me = self.whoami(ctx, env)
            listing = True
            data = run_json(ctx, ["gh", "pr", "list", "--repo", full_name, "--state", "open", "--limit", str(LIMIT),
                                  "--json", PR_FIELDS], timeout=30, env=env)
        except GhFailure as f:
            if listing and f.no_access:  # one repo the account cannot see is that repo's row, not a failed panel
                return Snapshot(self.id, scope, at, me=me, complete=False,
                                message=no_access_message(full_name, me, gh_user(ctx)))
            return Snapshot(self.id, scope, at, health=f.health, message=f.message, retry_after=f.retry_after)
        if not isinstance(data, list):
            return Snapshot(self.id, scope, at, health="error", message="gh pr list did not return a list")
        items = tuple(x for x in (pr_item(p, host=host, repo=full_name, me=me) for p in data if isinstance(p, dict)) if x)
        full = len(data) >= LIMIT
        return Snapshot(self.id, scope, at, items=items, me=me, complete=not full,
                        message=f"showing the first {LIMIT} open pull requests" if full else "")


def no_access_message(full_name: str, me: str | None, user: str) -> str:
    who = f"the gh account {me or user}" if user else f"the active gh account {me}" if me else "the active gh account"
    fix = ("check that account's access, or change the GitHub account in this addon's settings" if user else
           "run gh auth switch -u <account> with an account that can, or set the GitHub account in this addon's settings")
    return f"{NO_ACCESS} {full_name} is not visible to {who}; {fix}"
