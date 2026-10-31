import socket
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient

from orch_dashboard.__main__ import private_directory, safe_path, stale_socket, verify_probe
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.errors import HostError
from orch_dashboard.hostlink import check_uid


def _current(reply):
    """A control stub for a running server of THIS release: `hello` answers the launcher's own digest (so the
    server is reused, not restarted); every other command answers ``reply``."""
    from orch_dashboard import __main__ as launcher

    return lambda path, command, *rest: (
        {"release": launcher.code_digest()} if command == "hello" else reply(path, command, *rest)
    )


def test_exchange_atomic_single_use_and_restart():
    auth = Auth()
    code = auth.launch(PORT)["code"]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: auth.exchange(code, "local")[1], range(8)))
    assert results.count(200) == 1
    assert results.count(401) == 7
    assert Auth().exchange(code, "local")[1] == 401


def test_expiry_and_idle():
    now = [100.0]
    auth = Auth(lambda: now[0])
    code = auth.launch(PORT)["code"]
    now[0] += 30
    assert auth.exchange(code, "local")[1] == 401
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    now[0] += 43200
    assert not auth.valid(token)
    token = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
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
    assert auth.exchange(launch["code"], "local")[1] == 200  # probe does not consume launch


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
            assert delay == 30  # the redirect file lives exactly as long as its code (#542 item 3)
            self.fn = fn
            timers.append(self)

        def start(self):
            pass

    issued = auth.launch(PORT)
    opened = []
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    # a running server of this release: `hello` answers the same digest, so it is reused (not restarted)
    monkeypatch.setattr(
        launcher, "control", lambda _p, c, *_: {"release": launcher.code_digest()} if c == "hello" else issued
    )
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
    # one navigation only: the refresh is a no-JavaScript fallback (two loads would spend the single-use code twice)
    assert '<noscript><meta http-equiv="refresh"' in html and html.count("http-equiv") == 1
    assert auth.exchange(issued["code"], "local")[1] == 200
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
    # Never create a missing runtime directory or its parents.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing/deep"))
    assert redirect_directory(fallback) == fallback
    assert not (tmp_path / "missing").exists()


def test_launcher_uses_server_redirect_directory(tmp_path, monkeypatch):
    from orch_dashboard import __main__ as launcher

    server = tmp_path / "server-redirects"
    server.mkdir(mode=0o700)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "other"))
    opened = []
    monkeypatch.setattr(
        launcher,
        "control",
        _current(
            lambda *_: {
                "port": PORT,
                "code": "demo-not-a-real-code",
                "redirect": str(server),
                "expires_at": time.time() + 30,
            }
        ),
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    launcher.open_dashboard()
    assert len(list(server.glob("launch-*.html"))) == 1 and opened[0].startswith(server.as_uri())


def test_print_url_terminal_only(tmp_path, monkeypatch, capsys):
    from orch_dashboard import __main__ as launcher

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))

    def started(*_):
        pytest.fail("nothing may start or be minted without a terminal")

    monkeypatch.setattr(launcher, "control", started)
    monkeypatch.setattr(launcher.subprocess, "Popen", started)
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="terminal"):
        launcher.open_dashboard(print_url=True)
    assert not capsys.readouterr().out
    monkeypatch.setattr(launcher.sys, "argv", ["orch-dashboard", "open", "--print-url"])
    assert launcher.main() == 1  # the CLI: one line on stderr, no traceback, nothing on stdout
    seen = capsys.readouterr()
    assert not seen.out and seen.err == "orch-dashboard: --print-url requires a terminal\n"
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        launcher,
        "control",
        lambda _p, c, *_: {"release": launcher.code_digest()}
        if c == "hello"
        else {"port": PORT, "code": "demo-not-a-real-code", "expires_at": int(time.time()) + 30},
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("must print only"))
    launcher.open_dashboard(print_url=True)
    printed = capsys.readouterr().out
    assert "Single-use" in printed and "30 s" in printed and "#launch=" in printed


@pytest.mark.parametrize(
    "named", ["elsewhere/arbitrary.txt", "elsewhere/launch-{h}.html", "orch-dashboard/arbitrary.txt"]
)
def test_launcher_refuses_an_unexpected_redirect_file(tmp_path, monkeypatch, named):
    """A reply naming any other file must not get the code written anywhere."""
    import hashlib

    from orch_dashboard import __main__ as launcher

    code = "demo-not-a-real-code"
    for name in ("elsewhere", "orch-dashboard"):
        (tmp_path / name).mkdir(mode=0o700)
    target = tmp_path / named.format(h=hashlib.sha256(code.encode()).hexdigest())
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        launcher, "control", _current(lambda *_: {"port": PORT, "code": code, "redirect_file": str(target)})
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("must not open"))
    with pytest.raises(SystemExit, match="unexpected launch file"):
        launcher.open_dashboard()
    assert not target.exists() and not any(code in p.read_text() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("xdg", ["valid", "unset", "invalid"])
def test_exchange_and_timer_delete_the_exact_handed_out_file(tmp_path, monkeypatch, xdg):
    """The server deletes the file it recorded for the code, whatever directory the launcher's env would resolve to."""
    from orch_dashboard import __main__ as launcher

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    fallback = tmp_path / "state" / "dashboard"
    fallback.mkdir(parents=True, mode=0o700)
    if xdg == "valid":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    elif xdg == "invalid":
        runtime.chmod(0o755)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    server_dir = launcher.redirect_directory(fallback)
    assert (server_dir == runtime / "orch-dashboard") == (xdg == "valid")
    timers = []

    class Timer:
        def __init__(self, delay, fn):
            self.fn = fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", Timer)
    auth = Auth(directory=server_dir)
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: True)
    created = []
    for mode in ("exchange", "timer"):
        issued = auth.launch(PORT)
        # The launcher's own environment resolves to a different directory than the server's (and the server's
        # "redirect" hint is absent): only the recorded file path may decide.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "launcher-sees-something-else"))
        monkeypatch.setattr(launcher, "control", _current(lambda *_, issued=issued: {k: v for k, v in issued.items()}))
        launcher.open_dashboard()
        redirect = auth.files[issued["code"]]
        assert redirect.parent == server_dir and redirect.is_file()
        created.append(redirect)
        if mode == "exchange":
            assert auth.exchange(issued["code"], "local")[1] == 200
        else:
            timers[-1].fn()
        assert not redirect.exists()
    assert not auth.files
    assert not list(server_dir.glob("launch-*.html"))


def test_setup_launcher_uses_the_login_link_path_with_a_setup_nonce(tmp_path, monkeypatch):
    """The launcher mints a SETUP-kind code and opens the same redirect file as `orch dashboard open`: the nonce is
    only ever in the file/fragment, never in argv or the request line; the page then POSTs it to /auth/setup."""
    from orch_dashboard import __main__ as launcher

    opened, asked = [], []
    (tmp_path / "dashboard").mkdir(mode=0o700)
    auth = Auth(directory=tmp_path / "dashboard")
    issued = {}

    def control(path, command, kind=None):
        if command == "hello":  # a running server of this release
            return {"release": launcher.code_digest()}
        asked.append((command, kind))
        issued.update(auth.launch(PORT, setup=kind == "setup"))
        return dict(issued)

    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "control", control)
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    launcher.open_dashboard("/setup")
    assert asked == [("launch", "setup")]
    assert len(opened) == 1 and opened[0].startswith("file://") and issued["code"] not in opened[0]
    page = auth.files[issued["code"]].read_text()
    assert f"/setup#launch={issued['code']}" in page and "to=" not in page  # loaded from /setup: setup CSP
    assert auth.exchange(issued["code"], "local")[0] is None and auth.exchange_setup(issued["code"]) is True


def test_setup_nonce_lives_five_minutes_and_is_single_use_while_login_stays_30s():
    from orch_dashboard.auth import CODE_TTL, SETUP_CODE_TTL

    assert (CODE_TTL, SETUP_CODE_TTL) == (30, 300)
    now = [1000.0]
    auth = Auth(clock=lambda: now[0])
    login = auth.launch(PORT)
    setup = auth.launch(PORT, setup=True)
    assert setup["expires_at"] - 1000 == 300 and login["expires_at"] - 1000 == 30
    now[0] += 31
    assert auth.exchange(login["code"], "local")[1] == 401  # a normal link is dead after 30 s
    now[0] += 250  # 281 s: the setup nonce is alive
    assert auth.exchange_setup(setup["code"]) is True
    assert auth.exchange_setup(setup["code"]) is False  # single use: a second browser is refused
    late = auth.launch(PORT, setup=True)
    now[0] += 301
    assert auth.exchange_setup(late["code"]) is False


def test_emit_url_fd_writes_to_a_pipe_only_and_no_browser_opens_nothing(tmp_path, monkeypatch, capsys):
    import os

    from orch_dashboard import __main__ as launcher

    now = [time.time()]
    (tmp_path / "dashboard").mkdir(mode=0o700)
    auth = Auth(clock=lambda: now[0], directory=tmp_path / "dashboard")
    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", lambda d, f: type("T", (), {"start": lambda s: None})())
    issued = auth.launch(PORT, setup=True)
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        launcher, "control", lambda p, c, k=None: issued if c == "launch" else {"release": launcher.code_digest()}
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u) or True)
    r, w = os.pipe()
    try:
        launcher.open_dashboard("/setup", emit_url_fd=w, no_browser=True)
        line = os.read(r, 4096).decode()
        assert line == f"http://127.0.0.1:{PORT}/setup#launch={issued['code']}\nno-browser\n"
        assert opened == [] and not list((tmp_path / "dashboard").glob("launch-*.html"))
        launcher.open_dashboard("/setup", emit_url_fd=w)  # with a browser: the argument is only the redirect file
        assert b"no-browser" not in os.read(r, 4096)  # the browser opened: no marker
        assert len(opened) == 1 and opened[0].startswith("file://") and issued["code"] not in opened[0]
        issued2 = auth.launch(PORT, setup=True)
        monkeypatch.setattr(
            launcher, "control", lambda p, c, k=None: issued2 if c == "launch" else {"release": launcher.code_digest()}
        )
        monkeypatch.setattr(launcher.webbrowser, "open", lambda u: False)  # a browser that cannot open
        launcher.open_dashboard("/setup", emit_url_fd=w)
        assert os.read(r, 4096).decode().endswith("\nno-browser\n")
        assert not __import__("pathlib").Path(issued2["redirect_file"]).exists()  # a failed open leaves no code on disk
    finally:
        os.close(r)
        os.close(w)
    with pytest.raises(RuntimeError):  # a tty / regular file is not a place for the code
        launcher.open_dashboard("/setup", emit_url_fd=1)
    assert issued["code"] not in capsys.readouterr().out


def test_an_older_server_without_expires_at_keeps_the_30s_bound(tmp_path, monkeypatch):
    from orch_dashboard import __main__ as launcher
    from orch_dashboard.auth import CODE_TTL

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, p):
            pass

        def sendall(self, b):
            pass

    monkeypatch.setattr(launcher.socket, "socket", lambda *a: FakeSock())
    monkeypatch.setattr(launcher, "check_uid", lambda *a: None)
    monkeypatch.setattr(launcher, "receive", lambda s: {"code": "c", "port": 1})
    monkeypatch.setattr(launcher, "refuse_agent", lambda: None)
    t0 = time.time()
    reply = launcher.control(tmp_path / "ctl.sock", "launch", "setup")
    assert t0 + CODE_TTL <= reply["expires_at"] <= time.time() + CODE_TTL


def test_session_reports_how_it_was_opened(client, auth):
    code = auth.launch(PORT)["code"]
    token = client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).json()["token"]
    head = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/session", headers=head).json()["login"] == "local"
    assert auth.login_kind(token) == "local"
    client.post("/auth/logout", headers={**head, "Origin": ORIGIN})
    assert auth.login_kind(token) is None
