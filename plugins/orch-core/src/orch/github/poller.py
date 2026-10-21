"""Pure display polling; no gates, needs items, Landing checks or signed text derive from this data.

Landing must fetch its own SHA live. Disk warm starts never enter the poller's decision state.
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from . import ingest
from .client import ROUTES, GitHubClient, GitHubError
from .etags import EtagStore
from .token import NoToken

MAX_REQUESTS_PER_POLL = 300
ERROR_KINDS = frozenset(
    {"network", "timeout", "too_large", "bad_reply", "unauthorized", "forbidden", "not_found", "rate_limited"}
)


@dataclass(frozen=True)
class GitHubSettings:
    repos: Mapping[str, str]
    link_prs: bool = True
    poll_minutes: int = 5

    def __post_init__(self):
        if (
            not isinstance(self.repos, Mapping)
            or len(self.repos) > 20
            or any(type(key) is not str or not key or not ingest.valid_repo(value) for key, value in self.repos.items())
            or type(self.link_prs) is not bool
            or not ingest.integer(self.poll_minutes, 2, 60)
        ):
            raise ValueError("invalid github settings")
        object.__setattr__(self, "repos", MappingProxyType(dict(self.repos)))


@dataclass(frozen=True)
class Snapshot:
    """display only; Landing must fetch its own SHA live.

    Immutable containers let readers retain a coherent view while the next poll runs.
    """

    pulls: tuple[ingest.Pull, ...] = ()
    issues: tuple[ingest.Issue, ...] = ()
    checks: Mapping[tuple[str, int], tuple[ingest.Check, ...]] = field(default_factory=dict)
    reviews: Mapping[tuple[str, int], ingest.ReviewSummary] = field(default_factory=dict)
    statuses: Mapping[tuple[str, int], str] = field(default_factory=dict)
    as_of: float | None = None
    verified: bool = False
    state: str = "empty"
    paused_until: float | None = None
    last_error_kind: str | None = None
    repo_status: Mapping[str, str] = field(default_factory=dict)
    truncated: Mapping[str, bool] = field(default_factory=dict)
    totals: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    request_count: int = 0
    partial: bool = False

    def __post_init__(self):
        if self.state not in {"ok", "no_token", "bad_token", "paused", "stale", "empty"}:
            raise ValueError("invalid snapshot state")
        if self.last_error_kind is not None and self.last_error_kind not in ERROR_KINDS:
            raise ValueError("invalid error kind")
        if any(
            value not in {"ok", "not_found", "forbidden", "error", "skipped"} for value in self.repo_status.values()
        ):
            raise ValueError("invalid repo status")
        for name in ("checks", "reviews", "statuses", "repo_status", "truncated", "totals"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(self, "pulls", tuple(self.pulls))
        object.__setattr__(self, "issues", tuple(self.issues))


class _Budget(Exception):
    pass


class _Rotation(Exception):
    pass


class Poller:
    def __init__(
        self, client=None, token_source=None, *, client_factory=GitHubClient, clock=time.time, rng=None, priority=None
    ):
        if token_source is None:
            raise ValueError("token source required")
        self.token_source = token_source
        self.client = client if client is not None else client_factory(token_source)
        self.clock, self.rng, self.priority = clock, rng or random.Random(), priority
        self.etags = EtagStore()
        self.snapshot = Snapshot()
        self.next_poll_at = 0.0
        self._fp = None
        self._repos = None
        self._bad_fp = None
        self._failures = 0
        self._versions = {}
        self._pending = {}
        self._start = 0
        self._listed = set()
        self._candidates = {}
        self._list_replies = {}

    @property
    def fingerprint(self):
        return self._fp

    def clear(self):
        self.etags.clear()
        self.snapshot = Snapshot()
        self._versions.clear()
        self._pending.clear()
        self._fp = None
        self._failures = 0
        self.next_poll_at = 0
        self._listed = set()
        self._candidates = {}
        self._list_replies = {}
        self.client.last_rate = None

    def _budget(self):
        rate = self.client.last_rate
        if self.client.requests_made - self._start >= MAX_REQUESTS_PER_POLL:
            raise _Budget()
        if rate and rate.remaining is not None and rate.remaining <= 100:
            if rate.reset is None or rate.reset > self.clock():
                raise _Budget()

    def get(self, route_name, params=None, query=None):
        """Also serves as get_pages' adapter, so retries and every page obey the same budget."""
        params, query = params or {}, query or {}
        key = ROUTES[route_name].template.format(**params)
        if query:
            key += "?" + "&".join(f"{k}={query[k]}" for k in sorted(query))
        if key in self._list_replies:
            return self._list_replies[key]
        held = self.etags.get(self._fp, key)
        for attempt in range(2):
            self._budget()
            if self.token_source().fingerprint() != self._fp:
                raise _Rotation()
            reply = self.client.get(route_name, params, query, etag=held[0] if held else None)
            if self.token_source().fingerprint() != self._fp:
                raise _Rotation()
            if reply.not_modified:
                if held is not None:
                    reply = replace(reply, data=held[1])
                    if route_name in {"pulls", "issues"}:
                        self._list_replies[key] = reply
                    return reply
                self.etags.drop(self._fp, key)
                if attempt == 0:
                    continue
                raise GitHubError("bad_reply", 304, ROUTES[route_name].template)
            if reply.etag:
                self.etags.put(self._fp, key, reply.etag, reply.data)
            else:
                self.etags.drop(self._fp, key)
            if route_name in {"pulls", "issues"}:
                self._list_replies[key] = reply
            return reply
        raise AssertionError("unreachable")

    def _pages(self, route, params):
        return GitHubClient.get_pages(self, route, params, {"state": "open"}, max_pages=2)

    def poll_once(self, settings: GitHubSettings) -> Snapshot:
        now = self.clock()
        try:
            fp = self.token_source().fingerprint()
        except NoToken:
            self.clear()
            self.snapshot = Snapshot(state="no_token")
            return self.snapshot
        if fp != self._fp or dict(settings.repos) != self._repos:
            self.clear()
            self._fp, self._repos = fp, dict(settings.repos)
        if self._bad_fp == fp:
            if self.snapshot.state != "bad_token":
                self.snapshot = replace(self.snapshot, state="bad_token", last_error_kind="unauthorized")
            self.next_poll_at = now + settings.poll_minutes * 60 * self.rng.uniform(0.9, 1.1)
            return self.snapshot
        if self.snapshot.paused_until is not None:
            if now < self.snapshot.paused_until:
                return self.snapshot
            self.client.last_rate = None
        self._start = self.client.requests_made
        previous = self.snapshot
        pulls = {(p.repo, p.number): p for p in previous.pulls}
        issues = {(p.repo, p.number): p for p in previous.issues}
        checks, reviews, statuses = dict(previous.checks), dict(previous.reviews), dict(previous.statuses)
        repo_status, truncated, totals = {}, dict(previous.truncated), dict(previous.totals)
        state, error, paused, partial = "ok", None, None, False
        repos = list(dict.fromkeys(settings.repos.values()))
        candidates = list(self._candidates.values())
        budget_hit = False
        active_repo = None
        try:
            for repo in repos:
                active_repo = repo
                if repo in self._listed:
                    repo_status[repo] = previous.repo_status.get(repo, "ok")
                    if repo_status[repo] not in {"not_found", "forbidden"}:
                        repo_status[repo] = "ok"
                    partial |= repo_status[repo] != "ok"
                    continue
                owner, name = repo.split("/")
                params = {"owner": owner, "repo": name}
                try:
                    raw_pulls = self._pages("pulls", params)
                    raw_issues = self._pages("issues", params)
                except GitHubError as exc:
                    if exc.kind not in {"not_found", "forbidden"}:
                        raise
                    repo_status[repo] = exc.kind
                    self._listed.add(repo)
                    partial = True
                    continue
                new_pulls = [p for raw in raw_pulls[:200] if (p := ingest.pull(raw, repo)) is not None]
                new_issues = [p for raw in raw_issues[:200] if (p := ingest.issue(raw, repo)) is not None]
                alive = {(p.repo, p.number) for p in new_pulls}
                for key in list(pulls):
                    if key[0] == repo and key not in alive:
                        pulls.pop(key)
                        for mapping in (checks, reviews, statuses, self._versions, self._pending, self._candidates):
                            mapping.pop(key, None)
                issues = {key: value for key, value in issues.items() if key[0] != repo}
                issues.update({(p.repo, p.number): p for p in new_issues})
                for p in new_pulls:
                    key = (repo, p.number)
                    version = (p.head_sha, p.updated_at)
                    if self._versions.get(key) == version:
                        old = pulls[key]
                        pulls[key] = replace(
                            p, additions=old.additions, deletions=old.deletions, changed_files=old.changed_files
                        )
                    else:
                        candidates.append(p)
                        self._candidates[key] = p
                        pulls.setdefault(key, p)
                self._listed.add(repo)
                repo_status[repo] = "ok"
                totals[repo] = (len(raw_pulls), len(raw_issues))
                truncated[repo] = len(raw_pulls) >= 200 or len(raw_issues) >= 200
            candidates.sort(key=lambda p: not bool(self.priority and self.priority(p.repo, p.branch)))
            for p in candidates:
                active_repo = p.repo
                key, version = (p.repo, p.number), (p.head_sha, p.updated_at)
                owner, name = p.repo.split("/")
                params = {"owner": owner, "repo": name}
                if key not in self._pending or self._pending[key][0] != version:
                    self._pending[key] = (version, {})
                parts = self._pending[key][1]
                requests = [
                    ("pull", {**params, "number": p.number}),
                    ("check_runs", {**params, "sha": p.head_sha}),
                    ("status", {**params, "sha": p.head_sha}),
                    ("reviews", {**params, "number": p.number}),
                ]
                try:
                    for route, route_params in requests:
                        if route in parts:
                            continue
                        if route in {"check_runs", "status"} and not p.head_sha:
                            parts[route] = {}
                        else:
                            parts[route] = self.get(route, route_params).data
                except GitHubError as exc:
                    if exc.kind not in {"not_found", "forbidden"}:
                        raise
                    repo_status[p.repo] = exc.kind
                    partial = True
                    continue
                detail = ingest.pull(parts["pull"], p.repo, parts["pull"])
                pulls[key] = replace(
                    p,
                    **{field: getattr(detail, field, 0) for field in ("additions", "deletions", "changed_files")},
                )
                checks[key], reviews[key] = ingest.checks(parts["check_runs"]), ingest.reviews(parts["reviews"])
                statuses[key] = ingest.combined_status(parts["status"])
                self._versions[key] = version
                self._pending.pop(key)
                self._candidates.pop(key, None)
        except _Budget:
            state, partial = "stale", True
            budget_hit = True
            for pending in self._candidates.values():
                repo_status[pending.repo] = "skipped"
        except (NoToken, _Rotation) as exc:
            self.clear()
            self.snapshot = Snapshot(state="no_token" if isinstance(exc, NoToken) else "empty")
            return self.snapshot
        except GitHubError as exc:
            if active_repo is not None:
                repo_status[active_repo] = "error"
            error, partial = exc.kind, True
            if exc.kind == "unauthorized":
                state, self._bad_fp = "bad_token", fp
            elif exc.kind == "rate_limited":
                state, paused = "paused", now + max(0, min(3600, exc.retry_after_s))
            else:
                state = "stale"
        if not budget_hit and error is None:
            self._listed.clear()
            self._candidates.clear()
            self._list_replies.clear()
        if partial and state == "ok":
            state = "stale"
        for repo in repos:
            repo_status.setdefault(repo, "skipped")
        count = self.client.requests_made - self._start
        self.snapshot = Snapshot(
            tuple(pulls.values()),
            tuple(issues.values()),
            checks,
            reviews,
            statuses,
            now if state == "ok" else previous.as_of,
            True,
            state,
            paused,
            error,
            repo_status,
            truncated,
            totals,
            count,
            partial,
        )
        interval = settings.poll_minutes * 60
        if error in {"network", "timeout", "too_large", "bad_reply"}:
            self._failures = min(self._failures + 1, 12)
            delay = min(2**self._failures * 5 * self.rng.uniform(0.9, 1.1), interval * 4, 3600)
        else:
            self._failures = 0
            delay = interval * self.rng.uniform(0.9, 1.1)
        self.next_poll_at = paused if paused is not None else now + delay
        return self.snapshot
