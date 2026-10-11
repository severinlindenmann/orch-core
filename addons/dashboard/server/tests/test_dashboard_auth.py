import socket
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient

from orch_dashboard.__main__ import private_directory, safe_path, stale_socket, verify_probe
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.errors import HostError
from orch_dashboard.hostlink import check_uid


def test_exchange_atomic_single_use_and_restart():
    auth = Auth()
    code = auth.launch(PORT)["code"]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: auth.exchange(code)[1], range(8)))
    assert results.count(200) == 1
    assert results.count(401) == 7
    assert Auth().exchange(code)[1] == 401


def test_expiry_and_idle():
    now = [100.0]
    auth = Auth(lambda: now[0])
    code = auth.launch(PORT)["code"]
    now[0] += 30
    assert auth.exchange(code)[1] == 401
    token = auth.exchange(auth.launch(PORT)["code"])[0]["token"]
    now[0] += 43200
    assert not auth.valid(token)
    token = auth.exchange(auth.launch(PORT)["code"])[0]["token"]
    for _ in range(3):
        now[0] += 25000
        assert auth.valid(token)
    now[0] += 11400
    assert not auth.valid(token)


@pytest.mark.parametrize(
    "host",
    ["localhost:43210", "127.0.0.1", "127.0.0.1:43211", "evil.test:43210", "127.0.0.1:43210,evil.test", "[::1]:43210"],
)
def test_host_before_routing(client, host):
    res = client.get("/api/no-such-route", headers={"host": host})
    assert res.status_code == 421
    assert "content-security-policy" in res.headers


def test_duplicate_host(client):
    assert client.get("/", headers=[("Host", "127.0.0.1:43210"), ("Host", "127.0.0.1:43210")]).status_code == 421


@pytest.mark.parametrize("origin", ["null", "http://localhost:43210", "http://127.0.0.1:43211", "https://evil.test"])
def test_foreign_origin(client, auth, origin):
    code = auth.launch(PORT)["code"]
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": origin}).status_code == 403
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 200


def test_bootstrap_limits_headers_logout(client, auth):
    code = auth.launch(PORT)["code"]
    assert client.post("/auth/session", json={"code": code}).status_code == 403
    assert client.post("/auth/session", content="x", headers={"Origin": ORIGIN}).status_code == 400
    res = client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    token = res.json()["token"]
    assert "set-cookie" not in res.headers
    assert res.headers["cache-control"] == "no-store"
    assert not any(h.startswith("access-control-") for h in res.headers)
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 401
    assert (
        client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert (
        client.post("/auth/logout", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN}).status_code == 200
    )
    assert client.get("/api/session", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_body_bound_and_no_echo(client, auth):
    code = auth.launch(PORT)["code"]
    for extra in ({"path": "/etc/passwd"}, {"op": "human.submit"}, {"file": "-"}):
        res = client.post("/auth/session", json={"code": code, **extra}, headers={"Origin": ORIGIN})
        assert res.status_code == 400 and code not in res.text
    assert (
        client.post(
            "/auth/session", content=b"x" * 1025, headers={"Origin": ORIGIN, "content-type": "application/json"}
        ).status_code
        == 413
    )
    for i in range(20):
        assert client.post("/auth/session", json={"code": "x" * 43}, headers={"Origin": ORIGIN}).status_code == (
            401 if i < 10 else 429
        )
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 200


def test_all_api_routes_require_token_and_unknown_routes(tmp_path):
    app = create_app(PORT)
    with TestClient(app, base_url=ORIGIN) as client:
        for route in app.routes:
            if route.path.startswith("/api/") or route.path == "/auth/logout":
                path = route.path.replace("{ws}", "0" * 32)
                for method in route.methods:
                    assert client.request(method, path, headers={"Origin": ORIGIN}).status_code == 401, path
        for path in ["/api", "/api/unknown", "/api/dev/reset", "/api/w/" + "0" * 32 + "/handoff"]:
            assert client.get(path).status_code == 401
        assert client.options("/api/session", headers={"Origin": "https://evil.test"}).status_code == 403


def test_probe_squatter(client, auth):
    launch = auth.launch(PORT)
    verify_probe(launch, lambda url: client.get(url).text)
    with pytest.raises(RuntimeError, match="ownership"):
        verify_probe(launch, lambda _: "squatter")
    assert auth.exchange(launch["code"])[1] == 200  # probe does not consume launch


def test_directory_symlinks_and_non_socket(tmp_path):
    directory = tmp_path / "dashboard"
    with private_directory(directory):
        assert directory.stat().st_mode & 0o777 == 0o700
    alias = tmp_path / "alias"
    alias.symlink_to(directory)
    with pytest.raises(OSError):
        with private_directory(alias):
            pass
    path = directory / "ctl.sock"
    path.write_text("not a socket")
    with pytest.raises(RuntimeError):
        stale_socket(path)
    assert path.exists()
    directory.chmod(0o755)
    with pytest.raises(RuntimeError):
        with private_directory(directory):
            pass


def test_real_peer_credentials():
    import os

    a, b = socket.socketpair()
    try:
        check_uid(a, os.getuid())
        with pytest.raises(HostError):
            check_uid(a, os.getuid() + 1)
    finally:
        a.close()
        b.close()


def test_reopen_path():
    assert safe_path("http://127.0.0.1:1/w/DEMO/tickets?ws=123#old") == "/w/DEMO/tickets?ws=123"
    for path in ["//evil.test//bad", "/\\evil", "/\n"]:
        with pytest.raises(ValueError):
            safe_path(path)


def test_launcher_opens_only_after_probe(tmp_path, monkeypatch, capsys):
    import orch_dashboard.__main__ as launcher

    auth = Auth(directory=tmp_path / "dashboard")
    timers = []
    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", lambda delay, fn: Timer(delay, fn))

    class Timer:
        def __init__(self, delay, fn):
            assert delay == 60
            self.fn = fn
            timers.append(self)

        def start(self):
            pass

    issued = auth.launch(PORT)
    opened = []
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "control", lambda *_: issued)
    monkeypatch.setattr(launcher, "verify_probe", lambda _: opened.append("probe"))
    browser_args = []

    class BrowserProcess:
        def poll(self):
            return None

    def browser_process(args, **kwargs):
        browser_args.append(args)
        return BrowserProcess()

    def open_browser(url):
        opened.append(url)
        return launcher.webbrowser.BackgroundBrowser("demo-not-a-real-browser").open(url)

    monkeypatch.setattr(launcher.subprocess, "Popen", browser_process)
    monkeypatch.setattr(launcher.webbrowser, "open", open_browser)
    launcher.open_dashboard("/w/DEMO/tickets")
    assert opened[0] == "probe"
    assert opened[1].startswith("file://")
    assert "launch=" not in opened[1] and issued["code"] not in opened[1]
    assert browser_args == [["demo-not-a-real-browser", opened[1]]]
    assert all("launch=" not in arg and issued["code"] not in arg for arg in browser_args[0])
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    redirect = Path(unquote(urlsplit(opened[1]).path))
    assert redirect.stat().st_mode & 0o777 == 0o600
    assert redirect.parent.stat().st_mode & 0o777 == 0o700
    html = redirect.read_text()
    assert issued["code"] in html and "location.replace" in html and 'http-equiv="refresh"' in html
    assert auth.exchange(issued["code"])[1] == 200
    assert not redirect.exists()
    issued = auth.launch(PORT)
    launcher.open_dashboard()
    redirect = Path(unquote(urlsplit(opened[-1]).path))
    timers[-1].fn()
    assert not redirect.exists()
    assert not capsys.readouterr().out
    opened.clear()

    def squatter(_):
        raise RuntimeError("ownership proof failed")

    monkeypatch.setattr(launcher, "verify_probe", squatter)
    with pytest.raises(RuntimeError):
        launcher.open_dashboard()
    assert not opened


def test_runtime_redirect_and_stale_sweep(tmp_path, monkeypatch):
    import os

    from orch_dashboard.__main__ import redirect_directory, sweep_redirects

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    fallback = tmp_path / "state"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    directory = redirect_directory(fallback)
    assert directory == runtime / "orch-dashboard"
    assert directory.stat().st_mode & 0o777 == 0o700
    stale = directory / "launch-old.html"
    stale.write_text("old")
    os.utime(stale, (0, 0))
    fresh = directory / "launch-new.html"
    fresh.touch()
    symlink = directory / "launch-link.html"
    symlink.symlink_to(fresh)
    sweep_redirects(directory)
    assert not stale.exists() and fresh.exists() and symlink.is_symlink()
    runtime.chmod(0o755)
    assert redirect_directory(fallback) == fallback


def test_print_url_terminal_only(tmp_path, monkeypatch, capsys):
    from orch_dashboard import __main__ as launcher

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "control", lambda *_: {"port": PORT, "code": "demo-not-a-real-code"})
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="terminal"):
        launcher.open_dashboard(print_url=True)
    assert not capsys.readouterr().out
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("must print only"))
    launcher.open_dashboard(print_url=True)
    printed = capsys.readouterr().out
    assert "Single-use" in printed and "30 s" in printed and "#launch=" in printed
