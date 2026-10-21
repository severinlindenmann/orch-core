"""this is the only place outside strings are cleaned; nothing downstream re-cleans or trusts.

Builders drop malformed identities and bound display fields before they enter host memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orch.canon.text import clean_line

from .client import ROUTES

_SHA = re.compile(r"[0-9a-fA-F]{40}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", re.ASCII)
CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)


def clean(text: object, cap: int) -> str:
    if type(text) is not str:
        return ""
    result = clean_line(text[:cap])
    end = cap * 4
    if len(result) > end:
        start = result.rfind("⟨", 0, end)
        if start >= 0 and result.find("⟩", start) >= end:
            end = start
    return result[:end]


def valid_repo(repo: object) -> bool:
    return (
        type(repo) is str
        and len(repo.split("/")) == 2
        and all(
            re.fullmatch(ROUTES["repo"].params[key], part) and part not in {".", ".."}
            for key, part in zip(("owner", "repo"), repo.split("/"), strict=True)
        )
    )


def obj(value):
    return value if type(value) is dict else {}


def integer(value, low=0, high=10**7):
    return type(value) is int and low <= value <= high


def date(value):
    return value if type(value) is str and len(value) <= 40 and _DATE.fullmatch(value) else ""


@dataclass(frozen=True)
class Pull:
    repo: str
    number: int
    title: str
    branch: str
    head_sha: str
    state: str
    draft: bool
    author: str
    updated_at: str
    fork: bool
    requested_review: bool
    additions: int
    deletions: int
    changed_files: int
    url: str


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    conclusion: str | None
    app: str


@dataclass(frozen=True)
class ReviewSummary:
    approved: int = 0
    changes_requested: int = 0
    commented: int = 0


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    title: str
    labels: tuple[str, ...]
    author: str
    updated_at: str
    url: str


def pull(raw: object, repo: str, detail: object = None) -> Pull | None:
    raw = obj(raw)
    if not valid_repo(repo) or not integer(raw.get("number"), 1) or raw.get("state") not in ("open", "closed"):
        return None
    if type(raw.get("draft", False)) is not bool:
        return None
    head, base, detail = obj(raw.get("head")), obj(raw.get("base")), obj(detail)
    sha = head.get("sha")
    head_repo, base_repo = obj(head.get("repo")), obj(base.get("repo"))
    same_repo = type(head_repo.get("full_name")) is str and head_repo.get("full_name") == base_repo.get("full_name")
    return Pull(
        repo,
        raw["number"],
        clean(raw.get("title"), 200),
        clean(head.get("ref"), 200),
        sha.lower() if type(sha) is str and _SHA.fullmatch(sha) else "",
        raw["state"],
        raw.get("draft", False),
        clean(obj(raw.get("user")).get("login"), 39),
        date(raw.get("updated_at")),
        not same_repo,
        any(type(raw.get(key)) is list and bool(raw[key]) for key in ("requested_reviewers", "requested_teams")),
        *(detail[key] if integer(detail.get(key)) else 0 for key in ("additions", "deletions", "changed_files")),
        f"https://github.com/{repo}/pull/{raw['number']}",
    )


def checks(raw: object) -> tuple[Check, ...]:
    items = obj(raw).get("check_runs")
    if type(items) is not list:
        return ()
    result = []
    for item in items:
        if type(item) is not dict or type(item.get("name")) is not str:
            continue
        status, conclusion = item.get("status"), item.get("conclusion")
        result.append(
            Check(
                clean(item["name"], 80),
                status if status in ("queued", "in_progress", "completed") else "unknown",
                conclusion if type(conclusion) is str and conclusion in CONCLUSIONS else None,
                clean(obj(item.get("app")).get("name"), 80),
            )
        )
        if len(result) == 20:
            break
    return tuple(result)


def reviews(raw: object) -> ReviewSummary:
    latest = {}
    if type(raw) is list:
        for item in raw:
            item = obj(item)
            login, state = obj(item.get("user")).get("login"), item.get("state")
            if (
                type(login) is str
                and login
                and state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING")
            ):
                latest[login] = state
    return ReviewSummary(
        *(
            sum(state == desired for state in latest.values())
            for desired in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
        )
    )


def combined_status(raw: object) -> str:
    state = obj(raw).get("state")
    return state if state in ("error", "failure", "pending", "success") else "unknown"


def issue(raw: object, repo: str) -> Issue | None:
    raw = obj(raw)
    if "pull_request" in raw or not valid_repo(repo) or not integer(raw.get("number"), 1):
        return None
    labels = raw.get("labels")
    labels = labels if type(labels) is list else []
    return Issue(
        repo,
        raw["number"],
        clean(raw.get("title"), 200),
        tuple(clean(label["name"], 50) for label in labels if type(label) is dict and type(label.get("name")) is str)[
            :10
        ],
        clean(obj(raw.get("user")).get("login"), 39),
        date(raw.get("updated_at")),
        f"https://github.com/{repo}/issues/{raw['number']}",
    )


def saved_text(value):
    """Undo our visible markers only at disk ingestion, so revalidation does not double escape them.

    Tampered marker text is still fed through the same cleaner as any other outside string.
    """
    if type(value) is not str:
        return value
    return re.sub(r"⟨U\+([0-9A-F]{4,6})⟩", lambda m: chr(min(int(m[1], 16), 0x10FFFF)), value)


def saved_pull(row):
    raw = {
        "number": row["number"],
        "title": saved_text(row["title"]),
        "state": row["state"],
        "draft": row["draft"],
        "user": {"login": saved_text(row["author"])},
        "updated_at": row["updated_at"],
        "head": {
            "sha": row["head_sha"],
            "ref": saved_text(row["branch"]),
            "repo": None if row["fork"] else {"full_name": row["repo"]},
        },
        "base": {"repo": {"full_name": row["repo"]}},
        "requested_reviewers": [{}] if row["requested_review"] else [],
    }
    return pull(raw, row["repo"], row)


def saved_issue(row):
    return issue(
        {
            "number": row["number"],
            "title": saved_text(row["title"]),
            "user": {"login": saved_text(row["author"])},
            "updated_at": row["updated_at"],
            "labels": [{"name": saved_text(label)} for label in row["labels"]],
        },
        row["repo"],
    )


def saved_reviews(row: object) -> ReviewSummary:
    """Validate cached counts directly to prevent small files from amplifying into millions of reviews."""
    row = obj(row)
    return ReviewSummary(
        *(row[key] if integer(row.get(key), 0, 10000) else 0 for key in ("approved", "changes_requested", "commented"))
    )
