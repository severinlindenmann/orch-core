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
    headers = sorted(
        (k, ", ".join(sorted(v.split(", "))) if k == "allow" else v)
        for k, v in res.headers.multi_items()
        if k not in drop
    )
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
                lambda m=method, p=path, e=extra: _send(
                    client, m, p, HOSTS["good"], ORIGINS["same"], f"Bearer {token}", e, b"{}"
                ),
            )
    for ctype in (None, "application/json", "application/json; charset=utf-8", "text/plain", "multipart/form-data"):
        for path in ("/api/state", "/auth/session", "/auth/logout", "/api/w/x/records/auto-off", "/api/w/x/click/sign"):
            extra = [("content-type", ctype)] if ctype else []
            yield (
                f"ctype={ctype} POST {path}",
                lambda p=path, e=extra: _send(
                    client, "POST", p, HOSTS["good"], ORIGINS["same"], f"Bearer {token}", e, b"{}"
                ),
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
                    lambda p=path, e=extra, h=host: _send(
                        client,
                        "POST",
                        p,
                        HOSTS[h],
                        ORIGINS["same"],
                        None,
                        e + [("content-type", "application/json")],
                        b"{}",
                    ),
                )


def _websocket_cases(client):
    hosts = {"good": f"127.0.0.1:{PORT}", "localhost": f"localhost:{PORT}", "evil": "evil.test", "port": "127.0.0.1"}
    origins = {
        "same": ORIGIN,
        "none": None,
        "null": "null",
        "localhost": f"http://localhost:{PORT}",
        "evil": "http://evil.test",
    }
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


# ---- the remote policy (defined here, wired to no listener until R1-4) ----

from orch_dashboard.auth import Auth  # noqa: E402
from orch_dashboard.policy import clean_path, loopback_policy, remote_policy  # noqa: E402
from orch_dashboard.security import Security  # noqa: E402

NAME = "orch.example.com"
REMOTE = remote_policy(NAME)
LOOPBACK = loopback_policy(PORT)


def test_loopback_policy_values():
    assert LOOPBACK.hosts_for("/api/x", False) == (f"127.0.0.1:{PORT}",)
    assert LOOPBACK.hosts_for("/setup", True) == (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
    assert LOOPBACK.origin_for(f"127.0.0.1:{PORT}") == ORIGIN
    assert f"ws://127.0.0.1:{PORT}" in LOOPBACK.csp() and "frame-src http://127.0.0.1" in LOOPBACK.csp()
    assert LOOPBACK.pre_login("/anything") and LOOPBACK.launch_codes and LOOPBACK.widget_frames
    assert LOOPBACK.session_kind_ok("local") and not LOOPBACK.session_kind_ok("remote")
    assert LOOPBACK.websocket_ok("/term", [f"127.0.0.1:{PORT}"], [ORIGIN])
    assert not LOOPBACK.websocket_ok("/term", [f"localhost:{PORT}"], [f"http://localhost:{PORT}"])


def test_remote_policy_values():
    assert REMOTE.hosts_for("/setup", True) == (NAME,) == REMOTE.hosts_for("/api/x", False)
    assert REMOTE.origin_for(NAME) == f"https://{NAME}"
    csp = REMOTE.csp()
    assert f"connect-src 'self' wss://{NAME};" in csp and "frame-src 'none'" in csp and "127.0.0.1" not in csp
    assert "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert not REMOTE.launch_codes and not REMOTE.widget_frames
    assert REMOTE.session_kind_ok("remote") and not REMOTE.session_kind_ok("local")
    assert REMOTE.websocket_ok("/term", [NAME], [f"https://{NAME}"])
    for hosts, origins in (
        ([NAME], [f"http://{NAME}"]),
        ([NAME], []),
        ([NAME], ["null"]),
        ([f"{NAME}:443"], [f"https://{NAME}"]),
        ([f"127.0.0.1:{PORT}"], [ORIGIN]),
        ([NAME, NAME], [f"https://{NAME}"]),
    ):
        assert not REMOTE.websocket_ok("/term", hosts, origins)
    assert not REMOTE.websocket_ok("/other", [NAME], [f"https://{NAME}"])


@pytest.mark.parametrize(
    "name",
    [
        "localhost",
        "127.0.0.1",
        "Orch.Example.com",
        "orch.example.com:443",
        "a.b.",
        "x.local",
        "a.1",
        "",
        "a b.com",
        "-a.com",
    ],
)
def test_remote_policy_refuses_bad_names(name):
    with pytest.raises(ValueError):
        remote_policy(name)


ALLOWED = [
    "/",
    "/favicon.svg",
    "/loading.css",
    "/assets/main-hash.js",
    "/assets/index-AbC123_-.css",
    "/auth/passkey/login",
    "/auth/passkey/login/options",
    "/auth/enroll/AbC-123_x",
]
REFUSED = [
    "",
    "//",
    "/assets",
    "/assets/",
    "/assets//main.js",
    "/assets/../api/state",
    "/assets/./main.js",
    "/assets/.hidden",
    "/assets/a/b.js",
    "/Assets/main.js",
    "/ASSETS/main.js",
    "/AUTH/passkey/login",
    "/auth/Passkey/login",
    "/auth/passkey",
    "/auth/passkey/",
    "/auth/passkey//login",
    "/auth/passkey/login/",
    "/auth/passkey/a/b/c",
    "/auth/passkey/../session",
    "/auth/passkey/%2e%2e/session",
    "/auth/%70asskey/login",
    "/auth/passkey/login%2f",
    "/auth/enroll",
    "/auth/session",
    "/auth/logout",
    "/auth/setup",
    "/setup",
    "/api/state",
    "/api/setup/status",
    "/term",
    "/widget-frame/x",
    "/index.html",
    "/favicon.svg/",
    "/favicon.svg/x",
    "/loading.css;x",
    "/\\assets/main.js",
    "/assets/main.js\x00",
    "/assets/main.js\n",
    "/assets/maın.js",
    "/ ",
    "./",
    "/.",
    "/..",
    "/a/../",
]


@pytest.mark.parametrize("path", ALLOWED)
def test_prelogin_allowed(path):
    assert REMOTE.pre_login(path, path.encode())
    assert REMOTE.pre_login(path)


@pytest.mark.parametrize("path", REFUSED)
def test_prelogin_refused(path):
    assert not REMOTE.pre_login(path)
    assert not REMOTE.pre_login(path, path.encode("utf-8"))


def test_prelogin_raw_path_must_equal_decoded_path():
    assert not REMOTE.pre_login("/auth/passkey/login", b"/auth/passkey/%6cogin")
    assert not REMOTE.pre_login("/", b"/%2e")
    assert not REMOTE.pre_login("/assets/a.js", "/assets/ä.js".encode())
    assert not REMOTE.pre_login("/assets/a.js", b"/assets/a.js\xff")


def test_prelogin_fuzz_no_bypass():
    """Mutate allowed and sensitive paths with encodings, dots, slashes and case: the parser must never allow a path
    outside the allowlist's shape, and never raise."""
    rng = random.Random(1198)
    seeds = ALLOWED + ["/api/state", "/setup", "/term", "/auth/session", "/api/w/x/click/sign"]
    pieces = [
        "%2e",
        "%2E",
        "%2f",
        "%2F",
        "%5c",
        "%00",
        "..",
        ".",
        "/",
        "//",
        ";",
        "\\",
        "\x00",
        "\n",
        "∕",
        "．",
        "?",
        "#",
        " ",
    ]
    shape = [
        lambda s: s == "/",
        lambda s: s in ("/favicon.svg", "/loading.css"),
        lambda s: s.startswith("/assets/") and s.count("/") == 2,
        lambda s: s.startswith(("/auth/passkey/", "/auth/enroll/")),
    ]
    for _ in range(20_000):
        s = rng.choice(seeds)
        for _ in range(rng.randint(1, 3)):
            i = rng.randint(0, len(s))
            op = rng.random()
            if op < 0.6:
                s = s[:i] + rng.choice(pieces) + s[i:]
            elif op < 0.8 and s:
                j = min(len(s), i + rng.randint(1, 3))
                s = s[:i] + s[j:]
            else:
                s = s[:i] + s[i:].swapcase()
        for raw in (None, s.encode("utf-8", "surrogatepass"), urllib.parse.quote(s, safe="/").encode()):
            ok = REMOTE.pre_login(s, raw)
            if ok:
                assert any(f(s) for f in shape), s
                assert clean_path(s) is not None, s
                assert (
                    not any(c in s for c in "%\;?#\x00\n ") and "//" not in s and "/./" not in s and "/../" not in s
                ), s
                assert s == s.rstrip("/") or s == "/", s
                assert raw is None or raw.decode("ascii") == s, (s, raw)
    # the sensitive routes never pass whatever the case
    for s in ("/api/state", "/setup", "/term", "/auth/session"):
        for variant in (s.upper(), s.title(), s + "/", s + "/.", "/" + s, s.replace("/", "//")):
            assert not REMOTE.pre_login(variant)


# ---- Security driven by the remote policy (a bare ASGI app behind it; no listener involved) ----


async def _app(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _remote_client():
    auth = Auth()
    token = auth.exchange(auth.launch(PORT)["code"])[0]["token"]
    from fastapi.testclient import TestClient

    return TestClient(Security(_app, 443, auth, REMOTE), base_url=f"https://{NAME}"), token


def test_remote_listener_requests():
    client, token = _remote_client()
    bearer = {"authorization": f"Bearer {token}"}
    ok = client.get("/")
    assert ok.status_code == 200
    assert ok.headers["strict-transport-security"] == "max-age=31536000"
    assert f"wss://{NAME}" in ok.headers["content-security-policy"]
    assert client.get("/auth/passkey/login").status_code == 200
    # before login nothing else answers, and the answer does not tell what exists
    for path in ("/api/state", "/setup", "/term", "/assets/../api/state", "/AUTH/passkey/login", "/widget-frame/x"):
        assert client.get(path).status_code == 404, path
    assert client.get("/api/state", headers={"authorization": "Bearer nope"}).status_code == 404
    # a session of this listener passes the allowlist, then the usual checks apply
    assert client.get("/api/state", headers=bearer).status_code == 200
    # launch codes are a local credential: refused on the front, even with a session
    assert client.post("/auth/session", json={}, headers={"origin": f"https://{NAME}"}).status_code == 404
    # Host and Origin are exact; the loopback spelling is refused on the front
    assert client.get("/", headers={"host": f"127.0.0.1:{PORT}"}).status_code == 421
    assert client.get("/", headers={"host": f"{NAME}:443"}).status_code == 421
    assert client.get("/", headers={"host": "localhost"}).status_code == 421
    assert client.post("/auth/passkey/login", json={}, headers={"origin": f"http://{NAME}"}).status_code == 403
    assert client.post("/auth/passkey/login", json={}).status_code == 403  # a mutation needs its Origin
    assert client.post("/auth/passkey/login", json={}, headers={"origin": f"https://{NAME}"}).status_code == 200
    with client.websocket_connect("/term", headers={"host": NAME, "origin": f"https://{NAME}"}):
        pass
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/term", headers={"host": NAME, "origin": f"http://{NAME}"}):
            pass
    assert closed.value.code == 1008


def test_loopback_listener_refuses_the_public_name():
    client = None
    from fastapi.testclient import TestClient

    auth = Auth()
    client = TestClient(Security(_app, PORT, auth), base_url=ORIGIN)
    assert client.get("/", headers={"host": NAME}).status_code == 421
    assert client.get("/").status_code == 200
