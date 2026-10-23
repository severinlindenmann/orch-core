"""Exercise the real client against an in-process transport; never bind a port."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from orch.github import poller as module
from orch.github.client import GitHubClient, RateInfo
from orch.github.poller import GitHubSettings, Poller
from orch.github.token import NoToken, Token

from .fake import busy_day, pulls

SETTINGS = GitHubSettings({name: f"team/{name}" for name in ("alpha", "beta", "gamma")})
FIRST = "/repos/team/alpha/pulls?page=1&per_page=100&state=open"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def engine():
    fake, clock = busy_day(), Clock()
    for name in ("alpha", "beta", "gamma"):
        for route in ("pulls", "issues"):
            target = f"/repos/team/{name}/{route}?page=1&per_page=100"
            response = fake.responses[target]
            fake.responses[target + "&state=open"] = replace(response, headers={"etag": '"initial"'})
    tokens = [Token("github_pat_synthetic_only")]

    def source():
        if not tokens:
            raise NoToken()
        return tokens[0]

    client = GitHubClient(source, transport=fake, clock=clock)
    poller = Poller(client, source, clock=clock, rng=random.Random(1))
    return poller, fake, clock, tokens


def test_busy_day_and_conditional(engine):
    p, fake, _, _ = engine
    first = p.poll_once(SETTINGS)
    assert first.state == "ok" and first.verified and first.as_of == 1000
    assert len(first.pulls) == 12 and len(first.issues) == 75
    assert first.request_count == 54  # six lists plus four requests for each of twelve PRs
    second = p.poll_once(SETTINGS)
    assert second.pulls == first.pulls and second.checks == first.checks and second.reviews == first.reviews
    assert second.request_count == 6
    assert all("If-None-Match" in seen.header_names for seen in fake.seen[-6:])
    raw = pulls()
    raw[0]["head"]["sha"] = "f" * 40
    fake.register(FIRST, json_body=raw, headers={"etag": '"changed"'})
    for suffix, body in (("check-runs", {"check_runs": []}), ("status", {"state": "success"})):
        fake.register(f"/repos/team/alpha/commits/{'f' * 40}/{suffix}", json_body=body)
    changed = p.poll_once(SETTINGS)
    assert changed.request_count == 10
    assert changed.pulls[0].head_sha == "f" * 40


def test_budget_resumes_and_rate_floor(engine, monkeypatch):
    p, _, _, _ = engine
    monkeypatch.setattr(module, "MAX_REQUESTS_PER_POLL", 10)
    first = p.poll_once(SETTINGS)
    assert first.partial and first.state == "stale" and first.request_count == 10
    for _ in range(12):
        result = p.poll_once(SETTINGS)
        assert result.request_count <= 10
        if result.state == "ok":
            break
    assert result.state == "ok" and len(result.checks) == 12
    p.client.last_rate = RateInfo(5000, 100, 2000)
    assert p.poll_once(SETTINGS).request_count == 0


def test_rate_limit_and_bad_token(engine):
    p, fake, clock, tokens = engine
    good = p.poll_once(SETTINGS)
    fake.register(FIRST, status=429, headers={"retry-after": "999999"})
    paused = p.poll_once(SETTINGS)
    assert paused.state == "paused" and paused.paused_until == clock() + 3600
    count = p.client.requests_made
    assert p.poll_once(SETTINGS) is paused and p.client.requests_made == count
    clock.now += 3600
    fake.register(FIRST, status=401)
    bad = p.poll_once(SETTINGS)
    assert bad.state == "bad_token" and bad.pulls == good.pulls
    count = p.client.requests_made
    assert p.poll_once(SETTINGS) is bad and p.client.requests_made == count
    tokens[0] = Token("github_pat_different_synthetic")
    fake.register(FIRST, json_body=pulls())
    assert p.poll_once(SETTINGS).state == "ok"
    tokens.clear()
    missing = p.poll_once(SETTINGS)
    assert missing.state == "no_token" and not missing.pulls and p.etags.size_bytes == 0


def test_repo_404_and_network_keeps_data(engine):
    p, fake, clock, _ = engine
    good = p.poll_once(SETTINGS)
    fake.register(FIRST, status=404)
    result = p.poll_once(SETTINGS)
    assert result.repo_status["team/alpha"] == "not_found" and result.repo_status["team/beta"] == "ok"
    assert result.pulls == good.pulls
    fake.fail(FIRST)
    result = p.poll_once(SETTINGS)
    assert result.state == "stale" and result.last_error_kind == "network" and result.pulls == good.pulls
    first_delay = p.next_poll_at - clock()
    assert first_delay > 0 and result.as_of == good.as_of
    p.poll_once(SETTINGS)
    assert p.next_poll_at - clock() > first_delay


def test_304_without_body_retries(engine, monkeypatch):
    p, fake, _, _ = engine
    original = fake.request
    seen = []

    def once(*args, **kwargs):
        if args[1] == FIRST and not seen:
            seen.append(True)
            from orch.net.http import Response

            return Response(304, {}, b"")
        return original(*args, **kwargs)

    monkeypatch.setattr(fake, "request", once)
    assert p.poll_once(SETTINGS).request_count == 55
    assert "If-None-Match" not in fake.seen[0].header_names


def test_settings_wipe(engine):
    p, _, _, _ = engine
    p.poll_once(SETTINGS)
    result = p.poll_once(GitHubSettings({"a": "team/alpha"}))
    assert result.request_count == 18 and len(result.pulls) == 4
    assert p.etags.get(p.fingerprint, "/repos/team/beta/pulls?page=1&per_page=100&state=open") is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repos": {"a": "../x"}},
        {"repos": {}, "poll_minutes": True},
        {"repos": {}, "poll_minutes": 61},
        {"repos": {}, "link_prs": 1},
        {"repos": {str(n): "team/a" for n in range(21)}},
    ],
)
def test_settings_validation(kwargs):
    with pytest.raises(ValueError):
        GitHubSettings(**kwargs)


@pytest.mark.parametrize("budget", [1, 5])
def test_budget_smaller_than_list_phase_makes_progress(engine, monkeypatch, budget):
    p, _, _, _ = engine
    monkeypatch.setattr(module, "MAX_REQUESTS_PER_POLL", budget)
    for _ in range(60):
        result = p.poll_once(SETTINGS)
        assert result.request_count <= budget
        if result.state == "ok":
            break
    assert result.state == "ok" and len(result.checks) == 12


def test_priority_and_mid_detail_budget(engine, monkeypatch):
    p, fake, _, _ = engine
    p.priority = lambda repo, branch: repo == "team/gamma"
    monkeypatch.setattr(module, "MAX_REQUESTS_PER_POLL", 7)
    first = p.poll_once(SETTINGS)
    assert first.partial and fake.seen[-1].target == "/repos/team/gamma/pulls/1"
    assert not first.checks
    second = p.poll_once(SETTINGS)
    assert second.partial and second.checks[("team/gamma", 1)]
    assert sum(seen.target == "/repos/team/gamma/pulls/1" for seen in fake.seen) == 1


def test_page_caps_and_counts(engine, monkeypatch):
    p, fake, _, _ = engine
    raw = [{**pulls()[0], "number": n} for n in range(1, 101)]
    fake.register(FIRST, json_body=raw)
    fake.register(
        FIRST.replace("page=1&", "page=2&"), json_body=[{**row, "number": row["number"] + 100} for row in raw]
    )
    monkeypatch.setattr(module, "MAX_REQUESTS_PER_POLL", 3)
    result = p.poll_once(GitHubSettings({"a": "team/alpha"}))
    assert len(result.pulls) == 200 and result.totals["team/alpha"] == (200, 25)
    assert result.truncated["team/alpha"] and result.partial
    assert not any("page=3" in seen.target for seen in fake.seen)


def test_rotation_during_request_discards_everything(engine, monkeypatch):
    p, fake, _, tokens = engine
    p.poll_once(SETTINGS)
    original = fake.request

    def rotate(*args, **kwargs):
        reply = original(*args, **kwargs)
        tokens[0] = Token("github_pat_rotated_during_request")
        return reply

    monkeypatch.setattr(fake, "request", rotate)
    result = p.poll_once(SETTINGS)
    assert not result.pulls and p.etags.size_bytes == 0


def test_unheld_304_twice_is_bad_reply(engine):
    p, fake, _, _ = engine
    fake.register(FIRST, status=304)
    result = p.poll_once(SETTINGS)
    assert result.state == "stale" and result.last_error_kind == "bad_reply" and result.request_count == 2


def test_bad_token_remains_quarantined_after_settings_change(engine):
    p, fake, clock, _ = engine
    fake.register(FIRST, status=401)
    assert p.poll_once(SETTINGS).state == "bad_token"
    count = p.client.requests_made
    changed = GitHubSettings({"other": "team/beta"})
    assert p.poll_once(changed).state == "bad_token"
    assert p.client.requests_made == count and p.next_poll_at > clock()
