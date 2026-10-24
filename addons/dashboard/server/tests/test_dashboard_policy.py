"""#1198 R1-3: the listener policy object.

The loopback listener's behaviour is pinned by a golden recorded from the code BEFORE the policy object existed: the
same requests must still get the same status, headers and body. Regenerate only on purpose:
``ORCH_RECORD_GOLDEN=1 uv run pytest tests/test_dashboard_policy.py -k golden``."""

import itertools
import json
import os
import random
import urllib.parse
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT
from starlette.websockets import WebSocketDisconnect

GOLDEN = Path(__file__).parent / "data/loopback_golden.json"
HEX = "a" * 64
PATHS = [
    "/",
    "/assets/main-hash.js",
    "/assets/missing.js",
    "/favicon.svg",
    "/loading.css",
    "/setup",
    "/auth/setup",
    "/api/setup/status",
    "/api/no-such-route",
    "/api/state",
    "/auth/session",
    "/auth/logout",
    "/auth/launch",
    "/widget-frame/",
    f"/widget-frame/clock@1/{HEX}",
    "/term",
    "/api/w/x/click/sign",
    "/api/w/x/records/commit",
    "/api/w/x/records/init",
    "/api/testrun/x/images",
    "/other",
]
METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE"]
HOSTS = {
    "good": ["127.0.0.1:43210"],
    "localhost": ["localhost:43210"],
    "noport": ["127.0.0.1"],
    "wrongport": ["127.0.0.1:43211"],
    "evil": ["evil.test:43210"],
    "none": [],
    "dup": ["127.0.0.1:43210", "127.0.0.1:43210"],
}
ORIGINS = {
    "none": [],
    "same": [ORIGIN],
    "other": ["http://evil.test"],
    "null": ["null"],
    "dup": [ORIGIN, ORIGIN],
    "localhost": ["http://localhost:43210"],
}


def _record(res):
    drop = {"date", "server", "etag", "last-modified"}  # the fixture files are new on every run
    # `allow` lists a set of verbs in an arbitrary order
    headers = sorted((k, ", ".join(sorted(v.split(", "))) if k == "allow" else v) for k, v in res.headers.multi_items() if k not in drop)
    return {"status": res.status_code, "headers": headers, "body": res.text}


def _send(client, method, path, host, origins, auth=None, extra=(), content=None):
    headers = [("host", h) for h in host] + [("origin", o) for o in origins] + list(extra)
    if auth:
        headers.append(("authorization", auth))
    return client.request(method, path, headers=headers, content=content)


def _cases(client, token):
    for path, method, host, origin, auth in itertools.product(PATHS, METHODS, HOSTS, ORIGINS, ("none", "valid")):
        if method not in ("GET", "POST") and (host not in ("good", "localhost") or origin not in ("none", "same")):
            continue  # the combinations that matter for the other verbs; the rest only repeats the same refusals
        key = f"{method} {path} host={host} origin={origin} auth={auth}"
        extra = [("content-type", "application/json")] if method in ("POST", "PUT") else []
        yield (
            key,
            lambda p=path, m=method, h=host, o=origin, a=auth, e=extra: _send(
                client, m, p, HOSTS[h], ORIGINS[o], f"Bearer {token}" if a == "valid" else "Bearer nope", e, b"{}"
            ),
        )
    for site in ("same-origin", "cross-site", "same-site", "none"):
        for method, path in itertools.product(("GET", "POST"), ("/", "/api/state", "/auth/logout", "/setup")):
            extra = [("sec-fetch-site", site), ("content-type", "application/json")]
            yield (
                f"fetch-site={site} {method} {path}",
                lambda m=method, p=path, e=extra: _send(client, m, p, HOSTS["good"], ORIGINS["same"], f"Bearer {token}", e, b"{}"),
            )
    for ctype in (None, "application/json", "application/json; charset=utf-8", "text/plain", "multipart/form-data"):
        for path in ("/api/state", "/auth/session", "/auth/logout", "/api/w/x/records/auto-off", "/api/w/x/click/sign"):
            extra = [("content-type", ctype)] if ctype else []
            yield (
                f"ctype={ctype} POST {path}",
                lambda p=path, e=extra: _send(client, "POST", p, HOSTS["good"], ORIGINS["same"], f"Bearer {token}", e, b"{}"),
            )
    for path, size in (
        ("/auth/session", 1000),
        ("/auth/session", 1100),
        ("/api/w/x/click/sign", 16_000),
        ("/api/w/x/click/sign", 17_000),
        ("/api/w/x/records/commit", 1100),
        ("/api/w/x/records/init", 9000),
        ("/api/state", 1_048_000),
        ("/api/state", 1_049_000),
        ("/api/testrun/x/images", 1_049_000),
    ):
        yield (
            f"body {size} POST {path}",
            lambda p=path, n=size: _send(
                client,
                "POST",
                p,
                HOSTS["good"],
                ORIGINS["same"],
                f"Bearer {token}",
                [("content-type", "application/json")],
                b'{"a":"' + b"x" * n + b'"}',
            ),
        )
    for path in ("/setup", "/auth/setup", "/api/setup/status", "/api/setup/x"):
        for tok in (None, "short", "x" * 300):
            for host in ("good", "localhost", "evil"):
                extra = [("x-setup-token", tok)] if tok else []
                yield (
                    f"setup-token={tok and len(tok)} {path} host={host}",
                    lambda p=path, e=extra, h=host: _send(client, "POST", p, HOSTS[h], ORIGINS["same"], None, e + [("content-type", "application/json")], b"{}"),
                )


def _websocket_cases(client):
    hosts = {"good": f"127.0.0.1:{PORT}", "localhost": f"localhost:{PORT}", "evil": "evil.test", "port": "127.0.0.1"}
    origins = {"same": ORIGIN, "none": None, "null": "null", "localhost": f"http://localhost:{PORT}", "evil": "http://evil.test"}
    for path, h, o, proto, query in itertools.product(
        ("/term", "/term/", "/other", "/api/state"), hosts, origins, (None, "x"), ("", "a=1")
    ):
        yield f"ws {path} host={h} origin={o} proto={proto} query={query}", path, hosts[h], origins[o], proto, query


def _ws(client, path, host, origin, proto, query):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    url = path + (f"?{query}" if query else "")
    try:
        with client.websocket_connect(url, headers=headers, subprotocols=[proto] if proto else None) as sock:
            sock.close()
            return {"result": "accepted"}
    except WebSocketDisconnect as exc:
        return {"result": "closed", "code": exc.code}
    except Exception as exc:  # the app behind the check may raise for a non-terminal reply; the type is the contract
        return {"result": type(exc).__name__}


def _collect(client, token):
    responses, cases = {}, {}
    for key, send in _cases(client, token):
        text = json.dumps(_record(send()), sort_keys=True)
        cases[key] = responses.setdefault(text, f"r{len(responses)}")
    for key, *args in _websocket_cases(client):
        text = json.dumps(_ws(client, *args), sort_keys=True)
        cases[key] = responses.setdefault(text, f"r{len(responses)}")
    return {"responses": {v: json.loads(k) for k, v in responses.items()}, "cases": cases}


def test_loopback_golden(client, token):
    """Byte-identical loopback behaviour: every request above answers exactly as recorded before R1-3."""
    got = _collect(client, token)
    if os.environ.get("ORCH_RECORD_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=0, sort_keys=True) + "\n")
        pytest.skip("golden recorded")
    want = json.loads(GOLDEN.read_text())
    assert len(got["cases"]) == len(want["cases"])
    drift = [k for k in want["cases"] if got["responses"][got["cases"][k]] != want["responses"][want["cases"][k]]]
    assert not drift, drift[:10]
