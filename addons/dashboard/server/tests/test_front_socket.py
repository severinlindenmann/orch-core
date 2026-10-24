"""#1198 R1-4: the front socket (peer-UID check), the one header, remote.json, and the real server."""

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
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    RUN,
    SOURCE,
    Clock,
    app,
    bearer,
    echo,
    front,
    local,
    loop,
    remote,
    socket_dir,
    terminals_app,
)

from orch_dashboard.app import create_app
from orch_dashboard.front import Front, PeerChecked


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


def write_remote(directory, text, mode=0o600):
    path = directory / "remote.json"
    path.write_text(text)
    path.chmod(mode)
    return path


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


def test_remote_access_is_off_without_the_file_and_a_wrong_one_refuses_the_start(socket_dir):
    from orch_dashboard.__main__ import remote_config

    assert remote_config(socket_dir) is None
    mine = os.getuid()
    other = mine + 1
    write_remote(socket_dir, f'{{"name": "{NAME}", "front_uid": {other}}}')
    policy, uid = remote_config(socket_dir)
    assert (policy.host, policy.name, uid) == (NAME, "remote", other)
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


def test_a_bad_remote_json_keeps_loopback_serving_without_a_front(state):
    import orch_dashboard.__main__ as cli

    write_remote(state / "dashboard", f'{{"name": "127.0.0.1", "front_uid": {os.getuid() + 1}}}')
    ctl = state / "dashboard" / "ctl.sock"
    front_path = cli.run_dir(cli.state_dir()) / "dashboard" / "front.sock"
    thread = threading.Thread(target=cli.serve, daemon=True)
    thread.start()
    for _ in range(300):
        try:
            launch = cli.control(ctl, "launch")
            break
        except OSError:
            time.sleep(0.02)
    else:
        raise AssertionError("loopback did not start")
    assert not front_path.exists()  # the front is not opened
    import json as jsonlib
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{launch['port']}/auth/session",
        data=jsonlib.dumps({"code": launch["code"]}).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{launch['port']}"},
    )
    token = jsonlib.load(urllib.request.urlopen(req, timeout=5))["token"]
    assert token  # a normal login on loopback works
    cli.control(ctl, "stop")
    thread.join(timeout=15)
    assert not thread.is_alive()


def test_the_remote_problem_is_shown_on_the_session(gateway, auth, tmp_path, local):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    app = create_app(PORT, gateway, auth, static, remote_problem="remote.not_configured: x")
    got = TestClient(app, base_url=ORIGIN).get("/api/session", headers=bearer(local)).json()
    assert got["remote_problem"] == "remote.not_configured: x"
    app = create_app(PORT, gateway, auth, static)
    assert "remote_problem" not in TestClient(app, base_url=ORIGIN).get("/api/session", headers=bearer(local)).json()


def test_one_config_builder_pins_asyncio_for_both_servers_and_bounds_the_front():
    import asyncio

    import orch_dashboard.__main__ as cli

    plain, front = cli.server_config(echo([])), cli.server_config(echo([]), front=True)
    assert plain.loop == front.loop == "asyncio"
    assert plain.proxy_headers is False and front.proxy_headers is False
    assert front.lifespan == "off" and front.limit_concurrency == 64 and front.timeout_keep_alive == 5
    assert front.h11_max_incomplete_event_size == 16384
    assert plain.limit_concurrency is None
    loop = asyncio.new_event_loop()
    try:
        cli.require_selector_loop(loop)  # asyncio's own selector loop passes
    finally:
        loop.close()
    with pytest.raises(RuntimeError, match="selector"):
        cli.require_selector_loop(object())  # uvloop and the like: the peer check would be skipped


def test_the_front_server_refuses_to_start_on_a_foreign_loop(monkeypatch, socket_dir):
    import orch_dashboard.__main__ as cli

    def foreign(loop):
        raise RuntimeError("remote.not_configured: foreign loop")

    monkeypatch.setattr(cli, "require_selector_loop", foreign)
    sock = cli.bind_front(socket_dir / "f.sock", os.getuid())
    server = cli.FrontServer(cli.server_config(echo([]), front=True))
    with pytest.raises(RuntimeError, match="foreign loop"):
        server.run(sockets=[sock])
    sock.close()


def test_close_front_removes_the_socket_file_and_a_listener_taken_out_of_the_registry_refuses(socket_dir, app, front):
    import orch_dashboard.__main__ as cli
    from orch_dashboard.policy import REMOTE

    path = socket_dir / "f.sock"
    sock = cli.bind_front(path, os.getuid())
    cli.close_front(sock, path)
    assert not path.exists()
    cli.close_front(None, None)  # nothing to close
    # a front that failed to start: its policy leaves the shared registry and the listener answers nothing
    assert front.get("/").status_code == 200
    app.state.policies.pop(REMOTE)
    assert front.get("/").status_code == 404
