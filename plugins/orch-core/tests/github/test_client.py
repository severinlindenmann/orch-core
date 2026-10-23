"""Exercise the security boundary with hostile inputs and an in-process transport only."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import logging
import pickle
import re
import socket
import traceback
from pathlib import Path

import pytest

from orch.github import ROUTES, GitHubClient, GitHubError, RateInfo, Token
from orch.github import client as module
from orch.net.http import RelayConn, Response, TransportError

from .fake import FakeGitHub, busy_day, check_runs, issues, pulls, repo, reviews, user

FAKE_TOKEN = "github_pat_" + "A" * 40
PARAMS = {"owner": "team", "repo": "alpha", "number": 1, "sha": "a" * 40}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("GitHub tests must never open sockets or resolve names")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def client(fake):
    return GitHubClient(lambda: Token(FAKE_TOKEN), transport=fake, clock=lambda: 1000)


def test_route_table_and_only_get(client, fake):
    assert set(ROUTES) == {"repo", "pulls", "pull", "check_runs", "status", "reviews", "issues", "user", "rate_limit"}
    for name, route in ROUTES.items():
        params = {key: PARAMS[key] for key in route.params}
        query = {
            key: {"state": "open", "sort": "updated", "direction": "desc", "page": 1, "per_page": 100}[key]
            for key in route.query
        }
        target = route.template.format(**params)
        if query:
            target += "?" + "&".join(f"{key}={query[key]}" for key in sorted(query))
        fake.register(target, json_body=[] if route.response_type is list else {})
        client.get(name, params, query)
    assert client.requests_made == len(ROUTES)
    for seen in fake.seen:
        route = next(route for route in ROUTES.values() if route.template == seen.route)
        pattern = re.escape(route.template)
        for key, grammar in route.params.items():
            pattern = pattern.replace(re.escape("{" + key + "}"), f"(?:{grammar})")
        assert seen.method == "GET" and re.fullmatch(pattern, seen.target.split("?")[0])
        assert seen.auth_present
        assert seen.header_names == {"Accept", "X-GitHub-Api-Version", "User-Agent", "Authorization"}
    assert all(not hasattr(client, verb) for verb in ("post", "put", "delete", "patch"))
    assert repr(client) == "GitHubClient()"
    assert FAKE_TOKEN not in repr(fake.seen)


@pytest.mark.parametrize(
    "method,route",
    [("POST", "user"), ("DELETE", "user"), ("GET", "/unknown"), ("GET", "https://evil.example/"), ("GET", [])],
)
def test_bypassed_get_still_refused(client, fake, monkeypatch, method, route):
    monkeypatch.setattr(client, "get", lambda *a, **kw: client._request(method, route))
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == "bad_reply" and caught.value.status == 0
    assert fake.seen == []


@pytest.mark.parametrize("value", ["../", "%2e%2e", ".", "..", "é", "a\n", "a/b", "a?x", "", "a" * 101, None, 1])
@pytest.mark.parametrize("key", ["owner", "repo"])
def test_invalid_names(client, fake, key, value):
    params = {"owner": "team", "repo": "alpha", key: value}
    with pytest.raises(GitHubError):
        client.get("repo", params)
    assert fake.seen == []


@pytest.mark.parametrize("number", [True, False, 0, -1, 10_000_001, "1", 1.0, None])
def test_invalid_number(client, fake, number):
    with pytest.raises(GitHubError):
        client.get("pull", {"owner": "team", "repo": "alpha", "number": number})
    assert not fake.seen


@pytest.mark.parametrize("sha", ["a" * 41, "a" * 39, "A" * 40, "g" * 40, "../", "a" * 39 + "\n", 1])
def test_invalid_sha(client, fake, sha):
    with pytest.raises(GitHubError):
        client.get("status", {"owner": "team", "repo": "alpha", "sha": sha})
    assert not fake.seen


@pytest.mark.parametrize(
    "query",
    [
        {"labels": "bug"},
        {"url": "https://evil.example/"},
        {"state": "merged"},
        {"sort": "popularity"},
        {"direction": "up"},
        {"page": True},
        {"page": 0},
        {"page": 1001},
        {"per_page": 101},
        {"per_page": 0},
        {"per_page": "10"},
        {"state": []},
        {"state": "open\n"},
        [],
    ],
)
def test_invalid_queries(client, fake, query):
    with pytest.raises(GitHubError):
        client.get("pulls", {"owner": "team", "repo": "alpha"}, query)
    assert not fake.seen


@pytest.mark.parametrize("params", [{}, {"owner": "team"}, {"owner": "team", "repo": "a", "extra": "x"}, []])
def test_parameter_set_is_exact(client, fake, params):
    with pytest.raises(GitHubError):
        client.get("repo", params)
    assert not fake.seen


def test_parameter_boundaries(client, fake):
    for number in (1, 10_000_000):
        fake.register(f"/repos/A/{'b' * 100}/pulls/{number}")
        client.get("pull", {"owner": "A", "repo": "b" * 100, "number": number})
    fake.register("/repos/team/alpha/pulls?direction=asc&page=1000&per_page=100&sort=created&state=all", json_body=[])
    client.get(
        "pulls",
        {"owner": "team", "repo": "alpha"},
        {"direction": "asc", "page": 1000, "per_page": 100, "sort": "created", "state": "all"},
    )


def test_never_follows_response_urls(client, fake):
    fake.linked(
        "/repos/team/alpha/pulls?page=1&per_page=1",
        json_body=[
            {"url": "https://evil.example/", "html_url": "https://evil.example/", "Link": "https://evil.example/"}
        ],
    )
    fake.register("/repos/team/alpha/pulls?page=2&per_page=1", json_body=[])
    assert len(client.get_pages("pulls", {"owner": "team", "repo": "alpha"}, {}, max_pages=10, per_page=1)) == 1
    assert [s.target for s in fake.seen] == [f"/repos/team/alpha/pulls?page={p}&per_page=1" for p in (1, 2)]


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (429, "rate_limited"),
        (400, "bad_reply"),
        (500, "bad_reply"),
        (503, "bad_reply"),
        (301, "bad_reply"),
    ],
)
def test_http_errors(client, fake, status, kind):
    fake.register("/user", status=status)
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == kind
    assert caught.value.status == (0 if status == 301 else status)
    assert len(fake.seen) == client.requests_made == 1


@pytest.mark.parametrize("status", [300, 301, 302, 303, 305, 307, 308, 399])
def test_redirect_even_if_transport_returns_it(status):
    class ReturnsRedirect(FakeGitHub):
        def request(self, *args, **kwargs):
            return Response(status, {"location": "https://evil.example/"}, b"{}")

    with pytest.raises(GitHubError) as caught:
        GitHubClient(lambda: Token(FAKE_TOKEN), transport=ReturnsRedirect()).get("user")
    assert caught.value.kind == "bad_reply" and caught.value.status == status


@pytest.mark.parametrize(
    "transport_kind,kind",
    [(k, "network") for k in ("connect", "tls", "protocol", "truncated", "shutdown", "request")]
    + [("deadline", "timeout"), ("too_large", "too_large"), ("redirect", "bad_reply")],
)
def test_transport_errors(client, fake, transport_kind, kind):
    fake.fail("/user", transport_kind)
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == kind and caught.value.status == 0


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("retry-after", "-10", 0),
        ("retry-after", "0", 0),
        ("retry-after", "30", 30),
        ("retry-after", "9000", 3600),
        ("retry-after", "9" * 5000, 3600),
        ("retry-after", "bad", 60),
        ("retry-after", "1.5", 60),
        ("x-ratelimit-reset", "900", 0),
        ("x-ratelimit-reset", "1030", 30),
        ("x-ratelimit-reset", "99999999999999999999999", 3600),
        ("x-ratelimit-reset", "bad", 60),
    ],
)
def test_rate_limit_clamped(client, fake, key, value, expected):
    fake.register("/user", status=403, headers={"x-ratelimit-remaining": "0", key: value})
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == "rate_limited" and caught.value.retry_after_s == expected
    assert client.last_rate.remaining == 0


def test_retry_after_presence_and_precedence(client, fake):
    fake.register("/user", status=403, headers={"retry-after": "bad", "x-ratelimit-reset": "1001"})
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == "rate_limited" and caught.value.retry_after_s == 60


@pytest.mark.parametrize(
    "kind", ["network", "timeout", "too_large", "bad_reply", "unauthorized", "forbidden", "not_found", "rate_limited"]
)
def test_errors_carry_no_secret(kind):
    class Hostile(FakeGitHub):
        def request(self, method, target, **kwargs):
            secret = kwargs["headers"]["Authorization"]
            if kind in ("network", "timeout", "too_large"):
                error = TransportError(
                    {"network": "tls", "timeout": "deadline", "too_large": "too_large"}[kind], secret
                )
                raise error
            return Response(
                {"bad_reply": 200, "unauthorized": 401, "forbidden": 403, "not_found": 404, "rate_limited": 429}[kind],
                {"x-echo": secret},
                secret.encode(),
            )

    client = GitHubClient(lambda: Token(FAKE_TOKEN), transport=Hostile())
    with pytest.raises(GitHubError) as caught:
        client.get("pulls", {"owner": "team", "repo": "alpha"}, {"state": "open"})
    error = caught.value
    assert error.kind == kind
    assert error.route == "/repos/{owner}/{repo}/pulls"
    assert error.__cause__ is None and error.__context__ is None and error.__suppress_context__ is True
    for text in (str(error), repr(error), "".join(traceback.format_exception(error)), repr(error.args)):
        assert FAKE_TOKEN not in text and ("Bearer " + FAKE_TOKEN) not in text
        assert "state=open" not in text


def test_token_source_failure_has_no_context():
    def source():
        raise RuntimeError(FAKE_TOKEN)

    with pytest.raises(GitHubError) as caught:
        GitHubClient(source, transport=FakeGitHub()).get("user")
    assert caught.value.kind == "network" and caught.value.__context__ is None
    assert FAKE_TOKEN not in "".join(traceback.format_exception(caught.value))


def test_no_token_passes_through_as_its_own_error():
    from orch.github.token import NoToken

    def source():
        raise NoToken

    fake = FakeGitHub()
    with pytest.raises(NoToken):
        GitHubClient(source, transport=fake).get("user")
    assert fake.seen == []


def test_token_is_opaque():
    token = Token(FAKE_TOKEN)
    for text in (
        repr(token),
        str(token),
        format(token),
        f"{token}",
        f"{token!r}",
        "%s" % token,  # noqa: UP031 - legacy formatting must also hide credentials
        "%r" % token,  # noqa: UP031
        json.dumps(token, default=str),
        json.dumps(token, default=repr),
    ):
        assert FAKE_TOKEN not in text and "<github token>" in text
    for operation in (
        pickle.dumps,
        copy.copy,
        copy.deepcopy,
        hash,
        lambda t: t.__getstate__(),
        lambda t: t.__reduce__(),
    ):
        with pytest.raises(TypeError):
            operation(token)
    assert token == Token(FAKE_TOKEN) and token != Token("github_pat_" + "B" * 40)
    assert token != FAKE_TOKEN
    assert token.fingerprint() == hashlib.sha256(FAKE_TOKEN.encode()).hexdigest()


@pytest.mark.parametrize("value", [None, b"abc", "", "a" * 256, "a b", "a\t", "a\n", "é", "\x7f"])
def test_invalid_tokens(value):
    with pytest.raises(ValueError, match="^invalid github token$"):
        Token(value)


def test_rotation_per_request(fake):
    tokens = iter([Token(FAKE_TOKEN), Token("github_pat_" + "B" * 40)])
    fingerprints = []

    def source():
        token = next(tokens)
        fingerprints.append(token.fingerprint())
        return token

    class CheckRotation(FakeGitHub):
        def request(self, *args, **kwargs):
            value = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            assert hashlib.sha256(value.encode()).hexdigest() == fingerprints[-1]
            return super().request(*args, **kwargs)

    fake = CheckRotation().register("/user", headers={"etag": '"v1"'})
    client = GitHubClient(source, transport=fake)
    client.get("user")
    assert client.get("user", etag='"v1"').not_modified
    assert len(fingerprints) == 2 and fingerprints[0] != fingerprints[1]


def test_etag_and_304(client, fake):
    fake.register("/user", json_body=user(), headers={"etag": 'W/"abc"', "x-ratelimit-limit": "5000"})
    first = client.get("user")
    second = client.get("user", etag=first.etag)
    assert first.data == user() and first.not_modified is False
    assert second.status == 304 and second.data is None and second.not_modified
    assert second.etag == first.etag == 'W/"abc"'
    assert client.last_rate == second.rate == RateInfo(5000, None, None)
    assert fake.seen[-1].header_names == {
        "Accept",
        "X-GitHub-Api-Version",
        "User-Agent",
        "Authorization",
        "If-None-Match",
    }


@pytest.mark.parametrize("etag", ['""', '"x\n"', '"x"\r\nEvil: yes', '"' + "a" * 129 + '"', "unquoted", 5])
def test_etag_validation(client, fake, etag):
    with pytest.raises(GitHubError):
        client.get("user", etag=etag)
    assert not fake.seen
    fake.register("/user", headers={"etag": etag})
    assert client.get("user").etag is None


@pytest.mark.parametrize("max_pages,expected,requests", [(10, 5, 3), (2, 4, 2), (1, 2, 1)])
@pytest.mark.parametrize("route", ["pulls", "check_runs"])
def test_pages(client, fake, max_pages, expected, requests, route):
    params = {k: PARAMS[k] for k in ROUTES[route].params}
    base = ROUTES[route].template.format(**params)
    for page, items in enumerate(([1, 2], [3, 4], [5]), 1):
        fake.register(
            f"{base}?page={page}&per_page=2", json_body={"check_runs": items} if route == "check_runs" else items
        )
    query = {}
    result = client.get_pages(
        route, params, query, max_pages=max_pages, per_page=2, items_key="check_runs" if route == "check_runs" else None
    )
    assert result == list(range(1, expected + 1)) and client.requests_made == requests
    assert len(fake.seen) == requests and query == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_pages": 0},
        {"max_pages": True},
        {"max_pages": 1001},
        {"max_pages": 1, "per_page": False},
        {"max_pages": 1, "per_page": 101},
        {"max_pages": 1, "items_key": "url"},
    ],
)
def test_invalid_pagination(client, fake, kwargs):
    with pytest.raises(GitHubError):
        client.get_pages("pulls", {"owner": "team", "repo": "alpha"}, {}, **kwargs)
    assert not fake.seen


def test_invalid_page_shape(client, fake):
    fake.register(
        "/repos/team/alpha/commits/" + "a" * 40 + "/check-runs?page=1&per_page=100", json_body={"check_runs": {}}
    )
    with pytest.raises(GitHubError):
        client.get_pages("check_runs", {"owner": "team", "repo": "alpha", "sha": "a" * 40}, {}, max_pages=1)


@pytest.mark.parametrize(
    "body",
    [
        b"invalid",
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b'{"x": 1e9999}',
        b"[]",
        b"null",
        b"1",
        b'"string"',
        b'{"x":"\x00"}',
        b'{"x":"\xff"}',
        b"{} trailing",
        b"{" * 2000,
    ],
)
def test_invalid_json(client, fake, body):
    fake.register("/user", raw=body)
    with pytest.raises(GitHubError) as caught:
        client.get("user")
    assert caught.value.kind == "bad_reply" and caught.value.__context__ is None


def test_wrong_list_shape_and_oversized(client, fake):
    fake.register("/repos/team/alpha/pulls")
    with pytest.raises(GitHubError, match="bad_reply"):
        client.get("pulls", {"owner": "team", "repo": "alpha"})
    fake.oversized()
    with pytest.raises(GitHubError, match="too_large"):
        client.get("user")


@pytest.mark.parametrize("status", [200, 304, 401])
def test_rate_and_expiration(client, fake, status):
    expiration = "2026-10-21 10:00:00 UTC"
    fake.register(
        "/user",
        status=status,
        headers={
            "x-ratelimit-limit": "9" * 5000,
            "x-ratelimit-remaining": "-1",
            "x-ratelimit-reset": "bad",
            "github-authentication-token-expiration": expiration,
        },
    )
    if status == 401:
        with pytest.raises(GitHubError):
            client.get("user")
    else:
        reply = client.get("user")
        assert reply.token_expires == expiration
    assert client.last_rate == RateInfo(10**9, 0, None)
    fake.register("/user", headers={"github-authentication-token-expiration": "bad\nvalue"})
    assert client.get("user").token_expires is None
    assert client.last_rate == RateInfo(None, None, None)


def test_logging_contains_only_metadata(client, fake, caplog):
    fake.echoed_token(FAKE_TOKEN, "/repos/team/alpha/pulls?state=open", status=500)
    with caplog.at_level(logging.DEBUG, logger="orch.github.client"), pytest.raises(GitHubError):
        client.get("pulls", {"owner": "team", "repo": "alpha"}, {"state": "open"})
    assert FAKE_TOKEN not in caplog.text and "state=open" not in caplog.text
    assert [r.getMessage() for r in caplog.records] == [
        f"github /repos/{{owner}}/{{repo}}/pulls status=500 bytes={len(json.dumps({'message': FAKE_TOKEN}).encode())}"
    ]


def test_lazy_fixed_origin(monkeypatch):
    made = []
    fake = FakeGitHub().register("/user")

    def constructor(origin):
        made.append(origin)
        return fake

    monkeypatch.setattr(module, "RelayConn", constructor)
    client = GitHubClient(lambda: Token(FAKE_TOKEN))
    assert made == []
    client.get("user")
    client.get("user")
    assert made == ["https://api.github.com"]
    client.shutdown()
    assert fake.closed


def test_source_has_no_other_http_stack():
    for path in Path(module.__file__).parent.glob("*.py"):
        source = path.read_text()
        assert "graphql" not in source.lower()
        for node in ast.walk(ast.parse(source)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            assert not any(name == "http.client" or name.split(".")[0] in {"urllib", "requests"} for name in names)


def test_busy_day_is_reusable():
    fake = busy_day()
    client = GitHubClient(lambda: Token(FAKE_TOKEN), transport=fake)
    for name in ("alpha", "beta", "gamma"):
        params = {"owner": "team", "repo": name}
        assert client.get("repo", params).data == repo(name)
        assert client.get_pages("issues", params, {}, max_pages=1) == issues()
        assert client.get_pages("pulls", params, {}, max_pages=1) == pulls()
    assert check_runs("fail")["check_runs"][0]["conclusion"] == "failure"
    assert reviews()[0]["state"] == "CHANGES_REQUESTED"


def test_relay_304_without_sockets():
    """Exercise RelayConn's response branch with a stub connection, never a real socket."""

    class HTTPReply:
        status = 304
        will_close = False
        length = 0

        def getheaders(self):
            return [("ETag", '"cached"')]

        def read(self, count):
            return b""

    class HTTPConnection:
        sock = None

        def putrequest(self, *args, **kwargs):
            pass

        def putheader(self, *args):
            pass

        def endheaders(self, body):
            pass

        def getresponse(self):
            return HTTPReply()

        def close(self):
            pass

    transport = RelayConn(module.ORIGIN)
    transport._conn = HTTPConnection()
    transport._last_used = transport._mono()
    reply = GitHubClient(lambda: Token(FAKE_TOKEN), transport=transport).get("user", etag='"cached"')
    assert reply.not_modified and reply.etag == '"cached"'
