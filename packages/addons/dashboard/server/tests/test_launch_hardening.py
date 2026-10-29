"""#542 items 1 and 3: no dashboard under an agent's environment; no launch code left at rest."""

import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

import orch_dashboard.__main__ as launcher
from orch_dashboard.auth import CODE_TTL, SETUP_CODE_TTL, TUNNEL_CODE_TTL, Auth
from orch_dashboard.hostlink import encode

PORT = 43210


# --- item 1 ----------------------------------------------------------------------------------------------------------


def _nothing(*_a, **_k):
    pytest.fail("nothing may be started, minted or served under an agent's environment")


@pytest.mark.parametrize("name", ["ORCH_GRANT", "ORCH_SESSION"])
@pytest.mark.parametrize(
    "argv", [[], ["open"], ["launch"], ["open", "/w/DEMO", "--print-url"], ["serve"], ["serve", "--launcher-lock"]]
)
def test_the_launcher_refuses_under_an_agents_environment(tmp_path, monkeypatch, capsys, name, argv):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(name, "x")
    for seam in ("control", "_serve", "run_dir", "verify_probe"):
        monkeypatch.setattr(launcher, seam, _nothing)
    monkeypatch.setattr(launcher.subprocess, "Popen", _nothing)
    monkeypatch.setattr(launcher.webbrowser, "open", _nothing)
    monkeypatch.setattr(launcher.sys, "argv", ["orch-dashboard", *argv])
    assert launcher.main() == 3
    seen = capsys.readouterr()
    assert not seen.out
    assert seen.err.startswith("orch-dashboard: human_only: ") and seen.err.count("\n") == 1
    assert not (tmp_path / "dashboard").exists()


@pytest.mark.parametrize("call", [lambda: launcher.open_dashboard(), lambda: launcher.serve()])
def test_the_functions_refuse_too(monkeypatch, call):
    monkeypatch.setenv("ORCH_GRANT", "x")
    monkeypatch.setattr(launcher, "run_dir", _nothing)
    with pytest.raises(launcher.AgentRefused, match="human_only"):
        call()


@pytest.mark.parametrize("argv", [["stop"], ["--stop"], ["serve", "--stop"]])
def test_stop_passes_under_an_agents_environment(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_GRANT", "x")
    monkeypatch.setenv("ORCH_SESSION", "s")
    sent = []
    monkeypatch.setattr(launcher, "control", lambda path, command: sent.append(command) or {"ok": True})
    monkeypatch.setattr(launcher.sys, "argv", ["orch-dashboard", *argv])
    assert launcher.main() == 0 and sent == ["stop"]


def test_empty_agent_variables_do_not_refuse(monkeypatch):
    launcher.refuse_agent({"ORCH_GRANT": "", "ORCH_SESSION": ""})
    with pytest.raises(launcher.AgentRefused):
        launcher.refuse_agent({"ORCH_SESSION": "s"})


def test_python_dash_m_refuses_as_well(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path), "ORCH_GRANT": "x"}
    res = subprocess.run(
        [sys.executable, "-m", "orch_dashboard", "open"], env=env, capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 3 and res.stderr.startswith("orch-dashboard: human_only: ") and not res.stdout
    assert not (tmp_path / "dashboard").exists()


# --- item 3 ----------------------------------------------------------------------------------------------------------


def _launch(tmp_path, monkeypatch, opens=True):
    """One open_dashboard run against an in-process Auth; every argv the launcher hands out is recorded."""
    auth = Auth(clock=lambda: time.time(), directory=tmp_path / "dashboard")  # looked up per call: a test may freeze it
    timers = []

    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", Timer)
    issued = auth.launch(PORT)
    argvs = []

    class Process:
        def poll(self):
            return None

    def popen(args, **kwargs):
        argvs.append(list(args))
        assert not any(issued["code"] in str(v) for v in (kwargs.get("env") or {}).values())
        return Process()

    def browser(url):
        argvs.append([url])
        return opens

    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        launcher,
        "control",
        lambda _path, command, *_: {"release": launcher.code_digest()} if command == "hello" else issued,
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.os, "execv", _nothing, raising=False)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    return auth, issued, timers, argvs


def test_the_redirect_file_is_gone_after_redemption_and_a_second_read_fails(tmp_path, monkeypatch):
    auth, issued, timers, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard("/w/DEMO")
    redirect = Path(unquote(urlsplit(argvs[-1][0]).path))
    page = redirect.read_text()  # the person's browser reads it once
    assert issued["code"] in page and "location.replace" in page and "Cache-Control" not in page
    assert [t.delay for t in timers] == [CODE_TTL] == [30]
    assert auth.exchange(issued["code"], "local")[1] == 200
    assert not redirect.exists()
    with pytest.raises(FileNotFoundError):
        redirect.read_text()
    assert auth.exchange(issued["code"], "local")[1] == 401  # and the code itself is spent
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))


def test_an_unredeemed_redirect_file_lives_only_as_long_as_its_code(tmp_path, monkeypatch):
    auth, issued, timers, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard()
    redirect = Path(unquote(urlsplit(argvs[-1][0]).path))
    assert redirect.exists()
    timers[-1].fn()  # fires at the code's expiry, CODE_TTL
    assert not redirect.exists()


def test_the_code_is_in_no_argv(tmp_path, monkeypatch):
    _, issued, _, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard("/w/DEMO/tickets")
    assert argvs and all(issued["code"] not in arg and "launch=" not in arg for argv in argvs for arg in argv)
    assert argvs[-1][0].startswith("file://")


def test_no_browser_leaves_no_file(tmp_path, monkeypatch):
    _, issued, _, argvs = _launch(tmp_path, monkeypatch, opens=False)
    with pytest.raises(RuntimeError, match="no browser"):
        launcher.open_dashboard()
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))


def test_the_startup_sweep_uses_the_longest_code_lifetime(tmp_path):
    import os

    directory = tmp_path / "d"
    directory.mkdir(mode=0o700)
    old, young = directory / "launch-old.html", directory / "launch-young.html"
    for p in (old, young):
        p.write_text("x")
    now = 1_000_000.0
    os.utime(old, (now - SETUP_CODE_TTL, now - SETUP_CODE_TTL))
    os.utime(young, (now - SETUP_CODE_TTL + 1, now - SETUP_CODE_TTL + 1))
    launcher.sweep_redirects(directory, now=now)
    assert not old.exists() and young.exists()


# --- review of #548: control() refuses, cleanup invalidates once, a paused launcher writes nothing -------------------


def test_control_refuses_to_mint_under_an_agents_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_SESSION", "s")

    def no_socket(*_a, **_k):
        pytest.fail("no socket may be opened to mint a code under an agent's environment")

    monkeypatch.setattr(launcher.socket, "socket", no_socket)
    with pytest.raises(launcher.AgentRefused):
        launcher.control(tmp_path / "ctl.sock", "launch")


def test_control_still_sends_stop_under_an_agents_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_GRANT", "x")
    sent = []

    class Sock:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _):
            pass

        def connect(self, _):
            pass

        def sendall(self, data):
            sent.append(data)

    monkeypatch.setattr(launcher.socket, "socket", Sock)
    monkeypatch.setattr(launcher, "check_uid", lambda *_: None)
    monkeypatch.setattr(launcher, "receive", lambda _: {"ok": True})
    assert launcher.control(tmp_path / "ctl.sock", "stop") == {"ok": True} and len(sent) == 1


class _Timer:
    made = []

    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        _Timer.made.append(self)

    def start(self):
        pass


def _auth(tmp_path, monkeypatch, clock):
    _Timer.made = []
    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", _Timer)
    directory = tmp_path / "dashboard"
    directory.mkdir(mode=0o700)
    return Auth(clock=clock, directory=directory)


def test_cleanup_invalidates_the_code_and_unlinks_exactly_once(tmp_path, monkeypatch):
    now = [1000.0]
    auth = _auth(tmp_path, monkeypatch, lambda: now[0])
    issued = auth.launch(PORT)
    path = Path(issued["redirect_file"])
    path.write_text(issued["code"])
    unlinked = []
    real = Path.unlink

    def counting(self, missing_ok=False):
        unlinked.append(self)
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", counting)
    _Timer.made[-1].fn()  # the expiry timer, even before the clock says expired
    assert not path.exists() and unlinked == [path]
    assert auth.exchange(issued["code"], "local")[1] == 401  # invalidated, not merely its file forgotten
    _Timer.made[-1].fn()  # a second firing does nothing
    assert unlinked == [path]


def test_redemption_then_timer_unlinks_once(tmp_path, monkeypatch):
    auth = _auth(tmp_path, monkeypatch, lambda: 1000.0)
    issued = auth.launch(PORT)
    path = Path(issued["redirect_file"])
    path.write_text(issued["code"])
    unlinked = []
    real = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: unlinked.append(self) or real(self, missing_ok))
    assert auth.exchange(issued["code"], "local")[1] == 200
    _Timer.made[-1].fn()
    assert unlinked == [path] and not path.exists()


def test_an_expired_launch_removes_its_file_on_the_next_mint(tmp_path, monkeypatch):
    now = [1000.0]
    auth = _auth(tmp_path, monkeypatch, lambda: now[0])
    first = auth.launch(PORT)
    Path(first["redirect_file"]).write_text(first["code"])
    now[0] += CODE_TTL
    auth.launch(PORT)
    assert not Path(first["redirect_file"]).exists() and first["code"] not in auth.codes


def test_the_launch_reply_carries_the_expiry(tmp_path, monkeypatch):
    auth = _auth(tmp_path, monkeypatch, lambda: 1000.0)
    assert auth.launch(PORT)["expires_at"] == 1000.0 + CODE_TTL


def test_the_launch_reply_fits_a_frame(tmp_path, monkeypatch):
    # a frame carries no floats: a real clock's fraction made every `launch` fail (frame.invalid), so `open` timed out
    auth = _auth(tmp_path, monkeypatch, lambda: 1000.75)
    reply = auth.launch(PORT)
    assert reply["expires_at"] == 1000 + CODE_TTL and isinstance(reply["expires_at"], int)
    encode(reply)


def test_a_launcher_paused_past_expiry_writes_no_file(tmp_path, monkeypatch):
    clock = [1000.0]
    auth = _auth(tmp_path, monkeypatch, lambda: clock[0])
    issued = auth.launch(PORT)
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        launcher,
        "control",
        lambda _path, command, *_: {"release": launcher.code_digest()} if command == "hello" else issued,
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", _nothing)
    # The launcher was paused for the code's whole life: the server's timer already ran (nothing to unlink yet).
    clock[0] += CODE_TTL
    _Timer.made[-1].fn()
    monkeypatch.setattr(launcher.time, "time", lambda: 1000.0 + CODE_TTL)
    with pytest.raises(RuntimeError, match="expired"):
        launcher.open_dashboard()
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))
    assert auth.exchange(issued["code"], "local")[1] == 401


def test_a_launcher_paused_during_the_write_removes_the_file(tmp_path, monkeypatch):
    auth = _auth(tmp_path, monkeypatch, lambda: 1000.0)
    issued = auth.launch(PORT)
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        launcher,
        "control",
        lambda _path, command, *_: {"release": launcher.code_digest()} if command == "hello" else issued,
    )
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", _nothing)
    times = iter([1000.0, 1000.0 + CODE_TTL])  # before the write, then after a pause
    monkeypatch.setattr(launcher.time, "time", lambda: next(times))
    with pytest.raises(RuntimeError, match="expired"):
        launcher.open_dashboard()
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))


# --- #1198 R1-1: tunnel path (fixed port, 5 min print-url code on headless machines, the ssh -L line) ---


@pytest.mark.parametrize(
    "environ,platform,expected",
    [
        ({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "linux", True),
        ({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "darwin", True),
        ({}, "linux", True),
        ({"DISPLAY": ":0"}, "linux", False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", False),
        ({}, "darwin", False),
        ({"SSH_CONNECTION": "x"}, "win32", False),
    ],
)
def test_port_headless_detection(environ, platform, expected):
    assert launcher.is_headless(environ, platform) is expected


def test_port_is_fixed_on_headless_and_random_on_a_desktop(tmp_path):
    assert launcher.listen_port(tmp_path, headless=True) == launcher.HEADLESS_PORT
    assert launcher.listen_port(tmp_path, headless=False) == 0


def test_port_file_and_flag_win(tmp_path):
    (tmp_path / "port").write_text("51234\n")
    (tmp_path / "port").chmod(0o600)
    assert launcher.listen_port(tmp_path, headless=True) == 51234
    assert launcher.listen_port(tmp_path, headless=False) == 51234
    assert launcher.listen_port(tmp_path, headless=True, port=40000) == 40000


@pytest.mark.parametrize("text", ["abc", "80", "70000", ""])
def test_port_file_bad_content_is_an_error(tmp_path, text):
    (tmp_path / "port").write_text(text)
    (tmp_path / "port").chmod(0o600)
    with pytest.raises(RuntimeError):
        launcher.listen_port(tmp_path, headless=False)


def test_port_file_writable_by_others_or_a_symlink_is_refused(tmp_path):
    (tmp_path / "real").write_text("51234")
    (tmp_path / "real").chmod(0o600)
    (tmp_path / "port").symlink_to(tmp_path / "real")
    with pytest.raises(RuntimeError):
        launcher.listen_port(tmp_path, headless=False)
    (tmp_path / "port").unlink()
    (tmp_path / "port").write_text("51234")
    (tmp_path / "port").chmod(0o666)
    with pytest.raises(RuntimeError):
        launcher.listen_port(tmp_path, headless=False)


def test_port_in_use_is_named(tmp_path, monkeypatch):
    import socket as _socket

    busy = _socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        monkeypatch.setattr(launcher, "stale_socket", lambda _p: None)
        monkeypatch.setattr(launcher, "redirect_directory", lambda d: d)
        with pytest.raises(RuntimeError, match="already in use"):
            launcher._serve(tmp_path, port=busy.getsockname()[1])
    finally:
        busy.close()


def test_print_url_tunnel_line_uses_the_real_user_and_the_ssh_address():
    env = {"SSH_CONNECTION": "1.2.3.4 5555 203.0.113.9 22"}
    assert (
        launcher.tunnel_line(47821, env, user="alice")
        == "ssh -o ExitOnForwardFailure=yes -L 47821:127.0.0.1:47821 alice@203.0.113.9"
    )


def test_print_url_tunnel_line_falls_back_to_the_host_name(monkeypatch):
    monkeypatch.setattr(launcher.socket, "getfqdn", lambda: "box.example.org")
    assert (
        launcher.tunnel_line(1234, {}, user="bob")
        == "ssh -o ExitOnForwardFailure=yes -L 1234:127.0.0.1:1234 bob@box.example.org"
    )


def test_print_url_tunnel_line_refuses_odd_names():
    with pytest.raises(RuntimeError):
        launcher.tunnel_line(1234, {}, user="bob", host="x; rm -rf ~")


def test_print_url_tunnel_code_lives_5_minutes_and_stays_single_use():
    now = [1000.0]
    auth = Auth(clock=lambda: now[0])
    tunnel = auth.launch(PORT, tunnel=True)
    desktop = auth.launch(PORT)
    assert tunnel["expires_at"] == 1000 + TUNNEL_CODE_TTL == 1300
    assert desktop["expires_at"] == 1000 + CODE_TTL == 1030
    now[0] += 31
    assert auth.exchange(desktop["code"], "local")[1] == 401
    assert auth.exchange(tunnel["code"], "local")[1] == 200
    assert auth.exchange(tunnel["code"], "local")[1] == 401  # single use


def _print_url_run(tmp_path, monkeypatch, capsys, environ, tunnel=False):
    # One frozen instant for the issue and the print: the server stores a whole-second expiry, so on the wall clock a
    # code issued at x.95 and printed at x+1.0 honestly says "29 s" (CI flake 29 Oct, "expires in 30 s" not in out).
    monkeypatch.setattr(time, "time", lambda: 1000.5)
    kinds = []
    for name in ("SSH_CONNECTION", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)
    auth, issued, _t, _a = _launch(tmp_path, monkeypatch)
    monkeypatch.setattr(
        launcher,
        "control",
        lambda _p, command, kind=None: (
            {"release": launcher.code_digest()} if command == "hello" else kinds.append(kind) or issued
        ),
    )
    if tunnel:  # the server's answer decides the printed time, not the kind asked for
        issued["expires_at"] = int(time.time()) + 300
    launcher.open_dashboard("/", print_url=True)
    return kinds, capsys.readouterr().out


def test_print_url_headless_asks_for_a_tunnel_code_and_prints_the_ssh_line(tmp_path, monkeypatch, capsys):
    kinds, out = _print_url_run(
        tmp_path, monkeypatch, capsys, {"SSH_CONNECTION": "1.2.3.4 5 203.0.113.9 22"}, tunnel=True
    )
    assert kinds == ["tunnel"]
    assert "expires in 5 min" in out
    assert f"ssh -o ExitOnForwardFailure=yes -L {PORT}:127.0.0.1:{PORT} alice@203.0.113.9" in out


def test_print_url_on_a_desktop_keeps_30_seconds_and_no_ssh_line(tmp_path, monkeypatch, capsys):
    kinds, out = _print_url_run(tmp_path, monkeypatch, capsys, {"DISPLAY": ":0"})
    assert kinds == [None]
    assert "expires in 30 s" in out and "ssh -L" not in out


def test_print_url_text_follows_the_servers_expiry_not_the_kind(tmp_path, monkeypatch, capsys):
    # an older server answers "tunnel" with a 30 s code: the text must say 30 s
    _kinds, out = _print_url_run(tmp_path, monkeypatch, capsys, {"SSH_CONNECTION": "1.2.3.4 5 203.0.113.9 22"})
    assert "expires in 30 s" in out and "5 min" not in out


@pytest.mark.parametrize("user,host", [("-oProxyCommand=x", "box"), ("bob", "-oFoo"), ("bob", "-x.example")])
def test_print_url_tunnel_line_rejects_a_leading_dash(user, host):
    with pytest.raises(RuntimeError):
        launcher.tunnel_line(1234, {}, user=user, host=host)


def test_port_listener_can_rebind_after_a_connection_closed():
    import socket as _socket

    first = launcher.bind_listener(0)
    first.listen(1)
    port = first.getsockname()[1]
    client = _socket.create_connection(("127.0.0.1", port))
    conn, _ = first.accept()
    conn.close()
    client.close()
    first.close()
    second = launcher.bind_listener(port)  # without SO_REUSEADDR the closed connection's TIME_WAIT can refuse this
    second.close()


def test_port_a_test_server_never_lands_on_the_headless_port(tmp_path, monkeypatch):
    # the autouse fixture makes even a Linux machine without a display (CI) a desktop for the server under test
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher.listen_port(tmp_path) == 0
    with pytest.raises(AssertionError, match="fixed headless port"):
        launcher.bind_listener(launcher.HEADLESS_PORT)
