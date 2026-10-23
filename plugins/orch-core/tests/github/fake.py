"""Reusable GitHub scenarios without sockets or retained request credentials."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from orch.net.http import RedirectRefused, Response, TransportError


@dataclass(frozen=True)
class Seen:
    method: str
    target: str
    route: str
    header_names: frozenset[str]
    auth_present: bool


class FakeGitHub:
    """Record safe request metadata while exercising the real transport's failure contract."""

    def __init__(self):
        self.responses: dict[str, Response | str] = {}
        self.seen: list[Seen] = []
        self.closed = False

    def register(self, target, *, status=200, headers=None, json_body=None, raw=None):
        body = raw if raw is not None else json.dumps({} if json_body is None else json_body).encode()
        self.responses[target] = Response(status, {k.lower(): v for k, v in (headers or {}).items()}, body)
        return self

    def fail(self, target, kind="connect"):
        self.responses[target] = kind
        return self

    def request(self, method, target, *, route, headers: Mapping[str, str], body, deadline_s, cap):
        self.seen.append(Seen(method, target, route, frozenset(headers), "Authorization" in headers))
        assert body is None and deadline_s == 15 and cap == 2 * 1024 * 1024
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert headers["User-Agent"] == "orch-github"
        response = self.responses.get(target, Response(404, {}, b"{}"))
        if self.closed:
            raise TransportError("shutdown", route)
        if isinstance(response, str):
            raise TransportError(response, route)
        if 300 <= response.status < 400 and response.status != 304:
            raise RedirectRefused(route)
        if len(response.body) > cap:
            raise TransportError("too_large", route)
        if response.status == 200 and response.headers.get("etag") == headers.get("If-None-Match") is not None:
            return Response(304, response.headers, b"")
        return response

    def shutdown(self):
        self.closed = True

    def rate_limited(self, target, *, reset=2000):
        return self.register(
            target, status=403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
        )

    def bad_token(self, target="/user"):
        return self.register(target, status=401)

    def missing_repo(self, target="/repos/team/missing"):
        return self.register(target, status=404)

    def redirect(self, target="/user"):
        return self.register(target, status=301, headers={"location": "https://evil.example/"})

    def oversized(self, target="/user"):
        return self.register(target, raw=b"x" * (2 * 1024 * 1024 + 1))

    def linked(self, target="/user", *, json_body=None):
        return self.register(target, headers={"Link": '<https://evil.example/>; rel="next"'}, json_body=json_body)

    def echoed_token(self, token, target="/user", *, status=401):
        return self.register(target, status=status, json_body={"message": token})


def user(login="octocat"):
    return {"login": login, "id": 1, "name": "Test user"}


def repo(name="alpha", owner="team"):
    return {"id": len(name), "name": name, "full_name": f"{owner}/{name}", "private": True, "owner": user(owner)}


def pulls():
    return [
        {
            "number": i,
            "title": title,
            "draft": i == 1,
            "state": "open",
            "user": user(),
            "head": {"sha": f"{i:040x}"},
            "requested_reviewers": [user("reviewer")] if i == 2 else [],
        }
        for i, title in enumerate(["Draft change", "Ready for review", "hostile\x00\n\u202etitle", "x" * 10_000], 1)
    ]


def check_runs(outcome="pass"):
    return {
        "total_count": 1,
        "check_runs": [
            {
                "id": 1,
                "name": "tests",
                "status": "in_progress" if outcome == "pending" else "completed",
                "conclusion": {"pass": "success", "fail": "failure", "pending": None}[outcome],
            }
        ],
    }


def reviews():
    return [{"id": 1, "user": user("reviewer"), "state": "CHANGES_REQUESTED", "body": "Please add coverage"}]


def issues(count=25):
    return [{"number": i, "title": f"Issue {i}", "state": "open", "user": user()} for i in range(1, count + 1)]


def busy_day():
    """Three repos combine drafts, requested reviews, and mixed CI outcomes for future poller tests."""
    fake = FakeGitHub().register("/user", json_body=user())
    fake.register("/rate_limit", json_body={"resources": {"core": {"limit": 5000, "remaining": 4900, "reset": 2000}}})
    for name in ("alpha", "beta", "gamma"):
        base = f"/repos/team/{name}"
        fake.register(base, json_body=repo(name))
        for suffix, data in (("pulls", pulls()), ("issues", issues())):
            fake.register(f"{base}/{suffix}", json_body=data)
            fake.register(f"{base}/{suffix}?page=1&per_page=100", json_body=data)
        for pr, outcome in zip(pulls(), ("pending", "fail", "pass", "pass"), strict=True):
            number, sha = pr["number"], pr["head"]["sha"]
            fake.register(f"{base}/pulls/{number}", json_body=pr)
            fake.register(f"{base}/pulls/{number}/reviews", json_body=reviews())
            fake.register(f"{base}/pulls/{number}/reviews?page=1&per_page=100", json_body=reviews())
            fake.register(f"{base}/commits/{sha}/check-runs", json_body=check_runs(outcome))
            fake.register(f"{base}/commits/{sha}/check-runs?page=1&per_page=100", json_body=check_runs(outcome))
            fake.register(f"{base}/commits/{sha}/status", json_body={"state": "pending"})
    return fake
