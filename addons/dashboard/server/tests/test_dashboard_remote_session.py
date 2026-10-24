"""#1198 R1-4: the front socket, the remote session kind, lifetimes, per-source buckets.

What is pinned here:
- `Auth.exchange` takes the kind of the session as a required argument; a launch code opens a local session only.
- A remote session lives 12 h / 2 h idle, belongs to the front listener only, and gets every local-only refusal
  (runner on, One click off with a sealed passphrase, One click on again, device rotate, grant secret, hub backup key).
- Nothing a request carries (`X-Orch-Client` or any other header) makes a session local, picks the listener or sets the
  client address on the loopback server; the front reads that one header, after the peer-UID check of its socket.
- Passkey-login buckets: 20 per minute per source (IPv4 /32, IPv6 /64), 600 overall, apart from the launch limiter."""

# ruff: noqa: F811

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient

# fixtures and payload helpers of the click tests (a fixture imported here overrides the conftest one in this module)
from test_dashboard_click import (  # noqa: F401
    HASH,
    PASS_ENV,
    REQ,
    ROTATE_OFFER_FIELDS,
    agent_access,
    click_start,
    hub_join,
    rotate_body,
    rotate_offer,
    script,
)

from orch_dashboard import auth as auth_module
from orch_dashboard.app import create_app
from orch_dashboard.auth import (
    LIFETIMES,
    REMOTE_GLOBAL,
    REMOTE_MAX_SOURCES,
    REMOTE_PER_SOURCE,
    Auth,
    client_address,
    source_key,
)
from orch_dashboard.click import (
    REVEAL_SESSION_KINDS,
    ROTATE_LOCAL_ONLY,
    RUNNER_LOCAL_ONLY,
    SEALED_LOCAL_ONLY,
)
from orch_dashboard.front import Front, PeerChecked
from orch_dashboard.policy import remote_policy

NAME = "orch.example.com"
HTTPS = f"https://{NAME}"
SOURCE = "203.0.113.7"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


# ---- Auth: the kind is required, lifetimes, binding, buckets ----


def test_exchange_takes_the_kind_and_a_launch_code_opens_a_local_session_only():
    auth = Auth()
    with pytest.raises(TypeError):
        auth.exchange(auth.launch(PORT)["code"])  # no default: every caller says what it is opening
    for kind in ("remote", "hub", "passkey", "", None):
        with pytest.raises(ValueError):
            auth.exchange(auth.launch(PORT)["code"], kind)
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    assert auth.login_kind(token) == "local" and auth.listener_of(token) == "loopback"


def test_the_remote_kind_is_only_what_the_passkey_login_opens():
    auth = Auth()
    got = auth.open_remote()
    assert auth.login_kind(got["token"]) == "remote" and auth.listener_of(got["token"]) == "remote"
    assert auth.login_kind("unknown") is None and auth.listener_of("unknown") is None
    assert "remote" not in REVEAL_SESSION_KINDS and REVEAL_SESSION_KINDS == {"local"}


def test_remote_sessions_live_12_hours_and_2_idle_and_local_ones_keep_24_and_12():
    assert LIFETIMES == {"local": (86400, 43200), "remote": (43200, 7200)}
    clock = Clock()
    auth = Auth(clock=clock)
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]
    remote = auth.open_remote()
    assert (
        remote["expires_at"][:13]
        == auth_module.datetime.fromtimestamp(clock.now + 43200, auth_module.UTC).isoformat()[:13]
    )
    assert (
        local["expires_at"][:13]
        == auth_module.datetime.fromtimestamp(clock.now + 86400, auth_module.UTC).isoformat()[:13]
    )
    clock.now += 7200 - 1
    assert auth.valid(remote["token"])  # touched: the idle clock restarts
    clock.now += 7200 - 1
    assert auth.valid(remote["token"]) and auth.valid(local["token"])
    clock.now += 7200  # exactly 2 h without use
    assert not auth.valid(remote["token"]) and auth.valid(local["token"])
    assert auth.login_kind(remote["token"]) is None and auth.listener_of(remote["token"]) is None
    # absolute: a remote session in constant use still ends at 12 h
    busy = auth.open_remote()["token"]
    for _ in range(11):
        clock.now += 3600
        assert auth.valid(busy)
    clock.now += 3600
    assert not auth.valid(busy)


def test_a_local_session_is_unchanged_by_the_remote_lifetimes():
    clock = Clock()
    auth = Auth(clock=clock)
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    for _ in range(23):
        clock.now += 3600
        assert auth.valid(token)
    clock.now += 3600
    assert not auth.valid(token)  # 24 h absolute


def test_logout_and_log_out_everywhere_end_remote_sessions_only():
    auth = Auth()
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    a, b, c = (auth.open_remote()["token"] for _ in range(3))
    auth.logout(a)
    assert not auth.valid(a) and auth.valid(b)
    auth.logout_everywhere()
    assert not auth.valid(b) and not auth.valid(c) and auth.valid(local)
    assert auth.listeners.keys() == {local}


def test_sources_are_ipv4_32_and_ipv6_64_and_anything_else_is_refused():
    assert source_key("203.0.113.7") == "203.0.113.7"
    assert source_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == source_key("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert source_key("2001:db8:1:3::1") != source_key("2001:db8:1:2::1")
    assert source_key("::ffff:203.0.113.7") == "203.0.113.7" == client_address("::ffff:203.0.113.7")
    for bad in (
        "",
        "nope",
        "203.0.113.7, 198.51.100.1",
        "203.0.113.7:80",
        "1.2.3",
        " 1.2.3.4",
        "1.2.3.4\n",
        "１.２.３.４",
        None,
        7,
    ):
        assert client_address(bad) is None and source_key(bad) is None, bad


def test_login_buckets_are_per_source_and_a_refused_try_is_not_counted():
    clock = Clock()
    auth = Auth(clock=clock)
    for _ in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt(SOURCE)
    assert not auth.remote_attempt(SOURCE) and not auth.remote_attempt("::ffff:" + SOURCE)
    assert auth.remote_attempt("203.0.113.8")  # another source has its own budget (the owner's is not the attacker's)
    assert len(auth.remote_all) == REMOTE_PER_SOURCE + 1  # the refused tries took nothing from the global bucket
    clock.now += 59
    assert not auth.remote_attempt(SOURCE)
    clock.now += 2
    assert auth.remote_attempt(SOURCE)  # a minute later the bucket has drained
    assert not auth.remote_attempt("not an address")


def test_a_whole_ipv6_64_is_one_source():
    auth = Auth(clock=Clock())
    for n in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt(f"2001:db8:5:6::{n + 1:x}")
    assert not auth.remote_attempt("2001:db8:5:6:ffff::1")
    assert auth.remote_attempt("2001:db8:5:7::1")


def test_the_global_bucket_guards_the_cpu_far_above_one_source():
    assert REMOTE_GLOBAL == 600 and REMOTE_GLOBAL > 10 * REMOTE_PER_SOURCE
    auth = Auth(clock=Clock())
    for n in range(REMOTE_GLOBAL):
        assert auth.remote_attempt(f"198.51.{n // 250}.{n % 250}")
    assert not auth.remote_attempt("192.0.2.1")  # a distributed flood: the global cap holds
    assert len(auth.remote_all) == REMOTE_GLOBAL


def test_internet_traffic_never_uses_the_launch_limiter(monkeypatch):
    auth = Auth(clock=Clock())
    for n in range(3 * REMOTE_PER_SOURCE):
        auth.remote_attempt(f"198.51.100.{n % 200}")
    assert not auth.attempts  # the launch limiter saw nothing
    for _ in range(15):
        auth.exchange("wrong", "local")
    assert auth.exchange(auth.launch(PORT)["code"], "local")[1] in (200, 429)
    # and the other way: a spent launch limiter does not stop a passkey login
    assert auth.remote_attempt("192.0.2.9")


def test_tracked_sources_are_bounded_and_the_overflow_shares_one_bucket():
    auth = Auth(clock=Clock())
    for n in range(REMOTE_MAX_SOURCES):
        auth.remote_sources[f"10.{n >> 16}.{(n >> 8) & 255}.{n & 255}"] = auth_module.deque([auth.clock()])
    for _ in range(REMOTE_PER_SOURCE):
        assert auth.remote_attempt("192.0.2.200")
    assert not auth.remote_attempt("192.0.2.201")  # new sources past the cap share one (stricter) bucket
    assert len(auth.remote_sources) == REMOTE_MAX_SOURCES + 1


# ---- the front: the peer check and the one header ----


def echo(seen):
    async def app(scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        seen.append(scope)
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_the_front_reads_exactly_one_valid_address_and_hides_the_header_from_the_app():
    seen = []
    client = TestClient(Front(echo(seen)), base_url=HTTPS)
    assert client.get("/", headers={"X-Orch-Client": "::ffff:203.0.113.7"}).status_code == 200
    scope = seen[-1]
    assert scope["orch_listener"] == "remote" and scope["state"]["client"] == "203.0.113.7"
    assert b"x-orch-client" not in [k.lower() for k, _ in scope["headers"]]
    bad = [
        {},
        {"X-Orch-Client": ""},
        {"X-Orch-Client": "not-an-ip"},
        {"X-Orch-Client": "203.0.113.7, 198.51.100.1"},  # a chain is the front's job to flatten
        {"X-Orch-Client": "203.0.113.7:4444"},
    ]
    for headers in bad:
        seen.clear()
        res = client.get("/", headers=headers)
        assert res.status_code == 400 and not seen, headers
    seen.clear()
    twice = client.get("/", headers=[("X-Orch-Client", "203.0.113.7"), ("x-orch-client", "198.51.100.1")])
    assert twice.status_code == 400 and not seen  # two values: refuse, never pick one
    # other proxy headers are not read at all
    seen.clear()
    client.get("/", headers={"X-Orch-Client": SOURCE, "X-Forwarded-For": "198.51.100.1", "Forwarded": "for=1.1.1.1"})
    assert seen[-1]["state"]["client"] == SOURCE


def test_a_websocket_on_the_front_without_the_address_is_closed_before_accept():
    client = TestClient(Front(echo([])), base_url=HTTPS)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/term"):
        pass
    with client.websocket_connect("/term", headers={"X-Orch-Client": SOURCE}):
        pass


@pytest.fixture
def socket_dir():
    path = tempfile.mkdtemp(prefix="r14")  # short: a Unix socket path is limited to ~104 bytes
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


def serve_front(path, front_uid, seen):
    """A real uvicorn server on a real PeerChecked Unix socket, as `__main__` starts it."""
    from orch_dashboard.__main__ import bind_front

    sock = bind_front(path, front_uid)
    server = uvicorn.Server(
        uvicorn.Config(Front(echo(seen)), loop="asyncio", lifespan="off", log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    return server, thread, sock


def stop_front(server, thread, sock):
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def raw_get(path, headers):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(3)
        conn.connect(str(path))
        try:
            conn.sendall(
                ("GET / HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode()
            )
            return conn.recv(4096)
        except (BrokenPipeError, ConnectionResetError):
            return b""  # closed by the server before it read anything


def test_the_front_socket_answers_the_front_uid_and_drops_every_other_peer(socket_dir):
    seen = []
    # the configured front is this very user: the connection passes the peer check and is a remote request
    server, thread, sock = serve_front(socket_dir / "front.sock", os.getuid(), seen)
    try:
        assert raw_get(socket_dir / "front.sock", {"Host": NAME, "X-Orch-Client": SOURCE}).startswith(b"HTTP/1.1 200")
        assert seen[-1]["state"]["client"] == SOURCE
        assert (socket_dir / "front.sock").stat().st_mode & 0o777 == 0o666  # the peer check decides, not the mode
    finally:
        stop_front(server, thread, sock)
    seen.clear()
    # the configured front is somebody else: this user's connection is closed and the app never sees a byte
    server, thread, sock = serve_front(socket_dir / "other.sock", os.getuid() + 1, seen)
    try:
        assert raw_get(socket_dir / "other.sock", {"Host": NAME, "X-Orch-Client": SOURCE}) == b""
        assert seen == []
    finally:
        stop_front(server, thread, sock)


def test_a_peer_whose_uid_cannot_be_read_is_dropped(monkeypatch, socket_dir):
    import orch_dashboard.front as front

    def broken(sock):
        raise OSError("no peer credentials")

    monkeypatch.setattr(front, "peer_uid", broken)
    listener = PeerChecked(os.getuid())
    listener.bind(str(socket_dir / "p.sock"))
    listener.listen(1)
    with socket.socket(socket.AF_UNIX) as conn:
        conn.connect(str(socket_dir / "p.sock"))
        with pytest.raises(ConnectionAbortedError):
            listener.accept()
    listener.close()


# ---- remote.json: remote access is off unless configured, and a bad file refuses the start ----


def write_remote(directory, text, mode=0o600):
    path = directory / "remote.json"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_remote_access_is_off_without_the_file_and_a_wrong_one_refuses_the_start(socket_dir):
    from orch_dashboard.__main__ import remote_config

    assert remote_config(socket_dir) is None
    mine = os.getuid()
    other = mine + 1
    write_remote(socket_dir, f'{{"name": "{NAME}", "front_uid": {other}}}')
    assert remote_config(socket_dir) == (NAME, other)
    wrong = [
        f'{{"name": "{NAME}", "front_uid": {mine}}}',  # a front that is the person is no boundary
        f'{{"name": "{NAME}", "front_uid": 0}}',
        f'{{"name": "{NAME}", "front_uid": "7"}}',
        f'{{"name": "{NAME}", "front_uid": true}}',
        f'{{"name": "127.0.0.1", "front_uid": {other}}}',
        f'{{"name": "localhost", "front_uid": {other}}}',
        f'{{"name": "{NAME}:443", "front_uid": {other}}}',
        f'{{"name": "{NAME}", "front_uid": {other}, "extra": 1}}',
        f'{{"name": "{NAME}"}}',
        "not json",
        "[]",
    ]
    for text in wrong:
        write_remote(socket_dir, text)
        with pytest.raises(RuntimeError, match="remote.not_configured"):
            remote_config(socket_dir)
    write_remote(socket_dir, f'{{"name": "{NAME}", "front_uid": {other}}}', mode=0o664)  # others could choose the front
    with pytest.raises(RuntimeError, match="remote.not_configured"):
        remote_config(socket_dir)
    (socket_dir / "remote.json").unlink()
    (socket_dir / "real.json").write_text("{}")
    (socket_dir / "remote.json").symlink_to(socket_dir / "real.json")
    with pytest.raises(RuntimeError, match="remote.not_configured"):
        remote_config(socket_dir)


def test_the_front_socket_directory_must_be_ours_and_closed_to_writers(socket_dir):
    from orch_dashboard.__main__ import front_socket_path

    path = front_socket_path(socket_dir)
    assert (
        path == socket_dir / "dashboard" / "front.sock" and (socket_dir / "dashboard").stat().st_mode & 0o777 == 0o711
    )
    (socket_dir / "dashboard").chmod(0o777)
    with pytest.raises(RuntimeError, match="remote.not_configured"):
        front_socket_path(socket_dir)


# ---- one app, two listeners: the listener opens the session and the session stays on it ----


@pytest.fixture
def app(gateway, auth, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<html><head><script src="/assets/main-hash.js"></script></head></html>')
    (static / "assets/main-hash.js").write_text("x")
    return create_app(PORT, gateway, auth, static, remote=remote_policy(NAME))


@pytest.fixture
def loop(app):
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def front(app):
    return TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE})


@pytest.fixture
def local(auth):
    return auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]


@pytest.fixture
def remote(auth):
    return auth.open_remote()["token"]


def bearer(token, origin=None):
    return {"Authorization": f"Bearer {token}", **({"Origin": origin} if origin else {})}


def test_each_session_works_on_its_own_listener_and_nowhere_else(loop, front, local, remote, auth):
    assert loop.get("/api/session", headers=bearer(local)).json()["login"] == "local"
    assert front.get("/api/session", headers=bearer(remote)).json()["login"] == "remote"
    # a remote bearer on loopback (also with the public name, also with the header the front would set): unknown
    assert loop.get("/api/session", headers=bearer(remote)).status_code == 401
    assert loop.get("/api/session", headers={**bearer(remote), "X-Orch-Client": SOURCE}).status_code == 401
    assert loop.post("/auth/logout", headers=bearer(remote, ORIGIN)).status_code == 401
    # and a local bearer on the front
    assert front.get("/api/session", headers=bearer(local)).status_code == 404
    assert front.post("/auth/logout", headers=bearer(local, HTTPS)).status_code == 404
    # refused is not burned: both are still alive on their own listener
    assert auth.valid(remote) and auth.valid(local)
    assert front.get("/api/session", headers=bearer(remote)).status_code == 200
    assert loop.get("/api/session", headers=bearer(local)).status_code == 200


def test_a_header_never_makes_a_request_local_or_picks_the_listener(loop, front, remote, local):
    # loopback ignores X-Orch-Client: the same answers with it, nothing about the session changes
    plain = loop.get("/api/session", headers=bearer(local))
    spoofed = loop.get("/api/session", headers={**bearer(local), "X-Orch-Client": "198.51.100.99"})
    assert plain.status_code == spoofed.status_code == 200 and plain.json() == spoofed.json()
    # the name of the listener is not a header: the only way to name it is the scope the accepting server sets
    for header in ("X-Orch-Listener", "orch_listener", "X-Orch-Kind", "X-Orch-Login"):
        got = loop.get("/api/session", headers={**bearer(remote), header: "remote"})
        assert got.status_code == 401, header
    # the public name on loopback and the loopback name on the front are refused whatever else is sent
    assert loop.get("/", headers={"Host": NAME, "X-Orch-Client": SOURCE}).status_code == 421
    assert front.get("/", headers={"Host": f"127.0.0.1:{PORT}"}).status_code == 421
    # a remote client on the front who calls a launch-code exchange gets nothing
    assert front.post("/auth/session", json={"code": "x"}, headers={"Origin": HTTPS}).status_code == 404


def test_the_pre_login_surface_of_the_front_gets_the_address_for_the_login_bucket(auth):
    """The seam for R1-5: a passkey route reads `request.state.client` and asks `auth.remote_attempt` with it."""
    from orch_dashboard.policy import loopback_policy
    from orch_dashboard.security import Security

    seen = []
    stack = Front(Security(echo(seen), auth, loopback_policy(PORT), others=(remote_policy(NAME),)))
    front = TestClient(stack, base_url=HTTPS, headers={"X-Orch-Client": "2001:db8::7"})
    assert front.get("/auth/passkey/login").status_code == 200
    assert seen[-1]["state"]["client"] == "2001:db8::7" and auth.remote_attempt(seen[-1]["state"]["client"])
    assert front.get("/api/session").status_code == 404  # nothing else answers without a remote session
    # a listener name that is not configured is refused, not served by the default policy
    bare = TestClient(Security(echo(seen), auth, loopback_policy(PORT)), base_url=HTTPS)
    odd = Security(echo(seen), auth, loopback_policy(PORT))
    assert odd.policy_of({"orch_listener": "remote"}) is None and odd.policy_of({}).name == "loopback"
    assert bare.get("/", headers={"host": f"127.0.0.1:{PORT}"}).status_code == 200


# ---- the local-only gates on a remote session (until the step-up of R1-6 exists) ----

GATES = [
    ("runner/review", {"on": True}, RUNNER_LOCAL_ONLY),
    ("runner", {"on": True, "view_digest": HASH}, RUNNER_LOCAL_ONLY),
    ("custody/seal", {}, SEALED_LOCAL_ONLY),
    (
        "custody",
        {"on": False, "request_id": REQ, "view_digest": HASH, "sealed": {"custody.passphrase": PASS_ENV}},
        SEALED_LOCAL_ONLY,
    ),
    ("rotate/seal", {}, ROTATE_LOCAL_ONLY),
    ("rotate", rotate_body(), ROTATE_LOCAL_ONLY),
    ("custody", {"on": True, "resume": True}, "needs a dashboard opened on this computer"),
    ("prepare", agent_access(), "needs a dashboard opened on this computer"),
    ("prepare", hub_join(), "needs a dashboard opened on this computer"),
    ("sign", {"request_id": REQ, "view_digest": HASH, "sealed": {"hub.backup_key": PASS_ENV}}, "needs a dashboard"),
]
FEATURES = ["custody", "device_rotate", "runner_switch"]


def step(client, stub, path, body, token, origin):
    return client.post(f"/api/w/{stub.ws}/click/{path}", json=body, headers=bearer(token, origin))


@pytest.mark.parametrize(("path", "body", "message"), GATES)
def test_a_remote_session_on_the_front_gets_each_local_only_refusal(front, remote, stub, script, path, body, message):
    script.features = FEATURES
    res = step(front, stub, path, body, remote, HTTPS)
    assert res.status_code == 409, res.text
    error = res.json()["error"]
    assert error.get("reason") == "check_failed" or error["code"] == "custody.terminal_needed"
    assert message in res.json()["error"]["message"]
    assert [
        c for cl in script.clients for c in cl.calls if c[0] in ("runner", "rotate", "custody_on", "custody_off")
    ] == []
    assert not [c for cl in script.clients for c in cl.calls if c[0] in ("prepare", "sign")]


@pytest.mark.parametrize(("path", "body", "message"), GATES)
@pytest.mark.parametrize("kind", ["remote", "passkey", "hub", "", None])
def test_any_session_kind_that_is_not_local_is_refused_by_the_route_itself(
    loop, local, auth, stub, script, path, body, message, kind
):
    """Defence in depth: the route does not rely on the listener binding. A session of another kind that reached it
    (the binding broken, a new kind added) is refused, fail closed."""
    script.features = FEATURES
    auth.kinds[local] = kind
    res = step(loop, stub, path, body, local, ORIGIN)
    assert res.status_code == 409, (kind, res.text)
    assert message in res.json()["error"]["message"]
    assert not [
        c for cl in script.clients for c in cl.calls if c[0] in ("runner", "rotate", "custody_on", "custody_off")
    ]


def test_the_same_requests_pass_the_gates_for_a_local_session(loop, local, stub, script):
    script.features = FEATURES
    script.rotation = rotate_offer(stub.ws)
    assert step(loop, stub, "runner/review", {"on": True}, local, ORIGIN).status_code == 200
    assert step(loop, stub, "rotate/seal", {}, local, ORIGIN).status_code == 200
    script.enabled = script.available = False
    assert step(loop, stub, "custody", {"on": True, "resume": True}, local, ORIGIN).status_code == 200


def test_turning_things_off_stays_a_plain_click_on_a_remote_session(front, remote, stub, script):
    script.features = FEATURES
    res = step(front, stub, "runner/review", {"on": False}, remote, HTTPS)
    assert res.status_code == 200
    assert step(front, stub, "runner", {"on": False, "view_digest": HASH}, remote, HTTPS).status_code == 200
    assert step(front, stub, "custody", {"on": False}, remote, HTTPS).status_code == 200


def test_the_front_cannot_forge_local_through_the_listener_header_on_the_real_stack(front, remote, stub, script):
    """The strongest forgery a front client can try: headers that name a local session or the loopback listener."""
    script.features = FEATURES
    forged = {
        **bearer(remote, HTTPS),
        "X-Orch-Client": "127.0.0.1",
        "X-Orch-Listener": "loopback",
        "X-Orch-Login": "local",
    }
    res = front.post(f"/api/w/{stub.ws}/click/runner", json={"on": True, "view_digest": HASH}, headers=forged)
    assert res.status_code == 409 and RUNNER_LOCAL_ONLY in res.json()["error"]["message"]


# ---- the real server with remote.json ----


@pytest.fixture
def state(monkeypatch):
    root = Path(
        tempfile.mkdtemp(prefix="od-", dir=Path.home() / ".cache" if (Path.home() / ".cache").is_dir() else None)
    )
    os.chmod(root, 0o700)
    monkeypatch.setenv("ORCH_STATE_DIR", str(root))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_RUN_DIR", raising=False)
    (root / "dashboard").mkdir(mode=0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_the_server_opens_the_front_socket_only_when_configured_and_removes_it_at_stop(state):
    import orch_dashboard.__main__ as cli

    ctl = state / "dashboard" / "ctl.sock"
    front_path = cli.run_dir(cli.state_dir()) / "dashboard" / "front.sock"
    errors = []

    def run():
        try:
            cli.serve()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def wait_up():
        for _ in range(300):
            try:
                return cli.control(ctl, "hello")
            except OSError:
                time.sleep(0.02)
        raise AssertionError(errors or "the server did not start")

    # off: no remote.json, no front socket
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_up()
    assert not front_path.exists()
    cli.control(ctl, "stop")
    thread.join(timeout=10)
    assert not thread.is_alive()
    # on: the front socket is there, mode 0666 in a 0711 directory, and this user (not the front) is dropped
    write_remote(state / "dashboard", f'{{"name": "{NAME}", "front_uid": {os.getuid() + 1}}}')
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_up()
    assert front_path.is_socket() and front_path.stat().st_mode & 0o777 == 0o666
    assert front_path.parent.stat().st_mode & 0o777 == 0o711
    assert raw_get(front_path, {"Host": NAME, "X-Orch-Client": SOURCE}) == b""
    cli.control(ctl, "stop")
    thread.join(timeout=15)
    assert not thread.is_alive() and not errors
    assert not front_path.exists()  # gone with the server


def test_a_bad_remote_json_stops_the_server_before_anything_listens(state):
    import orch_dashboard.__main__ as cli

    write_remote(state / "dashboard", f'{{"name": "127.0.0.1", "front_uid": {os.getuid() + 1}}}')
    with pytest.raises(RuntimeError, match="remote.not_configured"):
        cli.serve()
    assert not (state / "dashboard" / "ctl.sock").exists()
