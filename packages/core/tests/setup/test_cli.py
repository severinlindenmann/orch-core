"""B9 step 6: the command: refusals (ORCH_GRANT, no terminal, dev host), dispatch, a full run over the socket."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from orch.host import signer as host_signer
from orch.ops import workspace_init as wi
from orch.setup import flow as flow_mod
from orch.setup import server as server_mod
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, make_env
from .fakes import FakePorts
from .helpers import call


@pytest.fixture(autouse=True)
def tty(monkeypatch):
    monkeypatch.setattr(wi.TERMINAL, "available", lambda: True)
    monkeypatch.setattr(host_signer, "check", lambda *a, **k: None)
    monkeypatch.setattr(
        host_signer,
        "sign",
        lambda p, *, backend, key_id, workspace_id: host_signer.Signed([{**e, "sig": "x"} for e in p["events"]], None),
    )


def run_main(tmp, argv=(), env=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    ports = kw.pop("ports", None) or FakePorts()
    rc = flow_mod.main(
        list(argv),
        env=env if env is not None else {"USER": "ann"},
        out=out,
        err=err,
        make_env=lambda e: make_env(tmp, ports=lambda: ports, clock=lambda: ports.now),
        ensure_host=kw.pop("ensure_host", lambda e, err: None),
        run_dir=kw.pop("run_dir", tmp.root / "run"),
        cwd=kw.pop("cwd", str(tmp.work)),
        **kw,
    )
    return rc, out.getvalue(), err.getvalue()


def test_usage_and_dispatch(tmp):
    rc, _, err = run_main(tmp, ["extra"])
    assert rc == 2 and "usage: orch setup" in err
    from orch.cli.main import main as cli_main

    e = io.StringIO()
    assert cli_main(["setup", "extra"], env={"HOME": str(tmp.home)}, stdout=io.StringIO(), stderr=e) == 2
    assert "usage: orch setup" in e.getvalue()


@pytest.mark.parametrize("env", [{"ORCH_GRANT": "gr_x"}, {"ORCH_GRANT": ""}, {"ORCH_SESSION": "s_x"}])
def test_agents_are_refused_before_anything_starts(tmp, env):
    started = []
    rc, _, err = run_main(tmp, env=env, ensure_host=lambda e, err: started.append(1))
    assert rc == 3 and "an agent never does it" in err and started == []
    assert not (tmp.root / "run").exists()


def test_no_terminal_is_refused(tmp, monkeypatch):
    monkeypatch.setattr(wi.TERMINAL, "available", lambda: False)
    rc, _, err = run_main(tmp)
    assert rc == 3 and "terminal" in err


def test_a_dev_host_is_refused(tmp):
    def dev(env, err):
        raise SetupError("host.unavailable", "the orch host could not be started (dev checkout)", 500)

    rc, _, err = run_main(tmp, ensure_host=dev)
    assert rc == 1 and "dev checkout" in err


def test_a_refused_directory_stops_before_a_browser_opens(tmp):
    opened = []
    rc, _, err = run_main(tmp, cwd=str(tmp.home), open_browser=lambda e, er, launch: opened.append(1) or True)
    assert rc == 1 and "home directory" in err and opened == []


def test_no_browser_flag_is_passed_to_the_opener_and_other_args_are_refused(tmp):
    seen = {}

    def browser(env, err, launch, **kw):
        seen.update(kw)
        call(str(tmp.root / "run" / "setup.sock"), "POST", "/shutdown", {})
        return "link"

    rc, _, err = run_main(tmp, ["--no-browser"], open_browser=browser)
    assert seen == {"no_browser": True} and rc == 1 and "use the link above" in err and "is open" not in err
    assert run_main(tmp, ["--bogus"])[0] == 2


def test_an_existing_project_dir_is_fine(tmp):
    (tmp.work / "README.md").write_text("hi")
    seen = {}

    def browser(env, err, launch):
        seen.update(launch)
        call(str(tmp.root / "run" / "setup.sock"), "POST", "/shutdown", {})
        return True

    rc, _, err = run_main(tmp, open_browser=browser)
    assert seen["code"] and rc == 1  # shut down without finishing: not a success


def test_the_whole_command_over_the_socket(tmp):
    ports = FakePorts()
    sock = str(tmp.root / "run" / "setup.sock")
    result = {}

    def shutdown():
        import contextlib
        import http.client

        with contextlib.suppress(OSError, http.client.HTTPException):  # the command may already be gone
            call(sock, "POST", "/shutdown", {})

    def browser(env, err, launch):
        def drive():
            st, c, _ = call(sock, "POST", "/claim", {"code": launch["code"], "port": 8765})
            tok = c["token"]
            api = lambda m, r, b=None: call(sock, m, "/api/setup/" + r, b, token=tok)  # noqa: E731
            assert api("POST", "profile", {**PROFILE, "addons": ["dashboard", "quick"]})[0] == 200
            st, rev, _ = api("POST", "secrets/reveal", {})
            assert api("POST", "secrets/confirm", {"acknowledged": True})[1] == {"ok": True}
            assert api("POST", "secrets/reveal", {})[0] in (409, 410)
            st, rv, h = api("POST", "review", {})
            ports.expect(wait_flow().review.plan)
            st, created, _ = api("POST", "create", {"review_id": rv["review_id"], "digest": rv["digest"]})
            assert st == 200
            for _ in range(100):
                st, p, _ = api("GET", f"progress?run_id={created['run_id']}")
                if p["done"] or p["error"]:
                    break
                threading.Event().wait(0.05)
            result["progress"] = p
            st, fin, _ = api("POST", "finish", {})
            result["finish"] = (st, fin)
            shutdown()  # never leave the command waiting, whatever happened above

        def wait_flow():
            return holder["flow"]

        def safe():
            try:
                drive()
            except BaseException as e:  # noqa: BLE001 - a failed assertion must end the command, not hang it
                result["error"] = repr(e)
                shutdown()

        driver.append(threading.Thread(target=safe, daemon=True))
        driver[0].start()
        return True

    holder = {}
    driver: list[threading.Thread] = []
    real = flow_mod.SetupFlow

    class Capturing(real):
        def __init__(self, env):
            super().__init__(env)
            holder["flow"] = self

    flow_mod.SetupFlow = Capturing  # type: ignore[misc]
    try:
        rc, out, err = run_main(tmp, ports=ports, open_browser=browser)
    finally:
        flow_mod.SetupFlow = real  # type: ignore[misc]
    driver[0].join(10)  # the command exits once finish is answered; the driver still has to record it (#949)
    assert not driver[0].is_alive()
    assert "error" not in result, result
    assert rc == 0, (err, result)
    assert "browser connected" in err and "set up in" in out
    assert result["progress"]["done"] is True and result["finish"][0] == 200
    assert ports.passphrase_seen and ports.passphrase_seen not in out + err and ports.code_seen not in out + err
    assert not Path(sock).exists()  # the socket is gone with the process


def test_ctrl_c_before_the_run_starts_stops_between_steps(tmp):
    ports = FakePorts()
    f = SetupFlow(make_env(tmp, ports=lambda: ports, clock=lambda: ports.now))
    f.launch(str(tmp.work))
    f.dispatch("POST", "profile", {}, {**PROFILE, "addons": ["dashboard"]})
    from .test_secrets import reveal

    reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"acknowledged": True})
    rv = f.dispatch("POST", "review", {}, {})[1]
    ports.expect(f.review.plan)
    f.cancel_run()
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = f.dispatch("GET", "progress", {"run_id": out["run_id"]}, None)[1]
    assert p["error"]["code"] == "stop" and p["error"]["finishable"] is False and ports.log == []


def test_default_socket_directory_is_private_not_the_host_run_directory(tmp):
    """The host's run directory is 0711 (tier A); the setup socket and launch-code file must not live there."""
    (tmp.state / "run").mkdir(parents=True, mode=0o711)
    (tmp.state / "run").chmod(0o711)
    seen = {}

    def browser(env, err, launch):
        sock = tmp.state / "setup" / "setup.sock"
        seen["mode"] = (tmp.state / "setup").stat().st_mode & 0o777
        seen["sock"] = sock.exists()
        call(str(sock), "POST", "/shutdown", {})
        return True

    out, err = io.StringIO(), io.StringIO()
    rc = flow_mod.main(
        [],
        env={"USER": "ann"},
        out=out,
        err=err,
        make_env=lambda e: make_env(tmp, ports=lambda: FakePorts()),
        ensure_host=lambda e, er: None,
        open_browser=browser,
        cwd=str(tmp.work),
    )
    assert seen == {"mode": 0o700, "sock": True} and rc == 1, err.getvalue()
    assert not (tmp.state / "run" / "setup.sock").exists()


def test_k2_a_lost_page_prints_the_notice_and_enter_opens_a_new_link(tmp):
    """Fake clock: the page claims, then stops polling; the terminal says so; Enter mints a new link, opens it, and the
    old token is refused."""
    sock = str(tmp.root / "run" / "setup.sock")
    clock = {"t": 1000.0}
    seen: dict = {"opened": [], "enter": 0}
    result: dict = {}

    def browser(env, err, launch):
        seen["opened"].append(launch["code"])
        n = len(seen["opened"])

        def drive():
            st, c, _ = call(sock, "POST", "/claim", {"code": launch["code"], "port": 8765})
            assert st == 200
            tok = c["token"]
            if n == 1:
                seen["old"] = tok
                assert call(sock, "GET", "/api/setup/state", token=tok)[0] == 200
                clock["t"] += 151  # the tab was killed: silence past 150 s
                for _ in range(100):  # the watchdog notices
                    if "closed or reloaded" in err.getvalue():
                        break
                    threading.Event().wait(0.05)
                seen["noticed"] = "closed or reloaded" in err.getvalue()
                seen["enter"] = 1  # now the person presses Enter
            else:
                result["old_refused"] = call(sock, "GET", "/api/setup/state", token=seen["old"])[0] in (401, 403)
                result["new_ok"] = call(sock, "GET", "/api/setup/state", token=tok)[0] == 200
                call(sock, "POST", "/shutdown", {})

        def safe():
            try:
                drive()
            except BaseException as e:  # noqa: BLE001
                result["error"] = repr(e)
                call(sock, "POST", "/shutdown", {})

        threading.Thread(target=safe, daemon=True).start()
        return True

    def enter(timeout, flush=False):
        if seen["enter"] == 1 and not flush:
            seen["enter"] = 2
            return True
        threading.Event().wait(min(timeout, 0.05))
        return False

    rc, out, err = run_main(
        tmp, open_browser=browser, enter_pressed=enter, server_options={"clock": lambda: clock["t"]}
    )
    assert "error" not in result, result
    assert seen["noticed"] and len(seen["opened"]) == 2 and seen["opened"][0] != seen["opened"][1]
    assert result == {"old_refused": True, "new_ok": True}
    assert err.count(server_mod.LOST_LINE) == 1
    assert seen["opened"][0] not in out + err and seen["opened"][1] not in out + err  # codes never on stderr/stdout


def test_k2_a_failing_relink_ends_the_setup_with_one_plain_line(tmp, monkeypatch):
    sock = str(tmp.root / "run" / "setup.sock")
    clock = {"t": 1000.0}
    state = {"enter": 0}

    def browser(env, err, launch):
        def drive():
            assert call(sock, "POST", "/claim", {"code": launch["code"], "port": 8765})[0] == 200
            clock["t"] += 151
            for _ in range(100):
                if "closed or reloaded" in err.getvalue():
                    break
                threading.Event().wait(0.05)
            state["enter"] = 1

        threading.Thread(target=drive, daemon=True).start()
        return True

    def enter(timeout, flush=False):
        if state["enter"] == 1 and not flush:
            state["enter"] = 2
            return True
        threading.Event().wait(min(timeout, 0.05))
        return False

    def boom(self):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(server_mod.SetupServer, "relink", boom)
    rc, out, err = run_main(
        tmp, open_browser=browser, enter_pressed=enter, server_options={"clock": lambda: clock["t"]}
    )
    assert rc == 1  # returned instead of hanging
    assert "could not make a new link; run orch setup again" in err and "secret detail" not in err


def _install_ctx(tmp, out, err, parts_seen):
    from orch.install import cli as icli
    from tests.install.test_install_cli import FakeRunner

    def run(argv, env=None):
        parts_seen.append(argv)
        return 0, "orch-hub v0.1.0\n" if argv[:3] == ["uv", "tool", "list"] else ""

    return icli.Ctx(
        env={"HOME": str(tmp.home), "ORCH_STATE_DIR": str(tmp.state)},
        out=out,
        err=err,
        runner=FakeRunner(),
        run=run,
        resolve_ref=lambda u, r: "b" * 40,
        health=lambda h, p: True,
        detect=lambda: [("127.0.0.1", "x")],
        port_free=lambda p, h: True,
    )


def test_role_full_installs_the_parts_then_the_workspace_and_a_second_run_takes_the_next_port(tmp):
    from orch.install.state import load

    seen: list = []
    out, err = io.StringIO(), io.StringIO()
    ictx = _install_ctx(tmp, out, err, seen)

    def browser(env, e, launch, **kw):
        call(str(tmp.root / "run" / "setup.sock"), "POST", "/shutdown", {})
        return "link"

    rc, _, _ = run_main(tmp, ["--role", "full", "--address", "127.0.0.1"], open_browser=browser, install_ctx=ictx)
    assert rc == 1  # the workspace itself was not finished here; the parts were installed first
    assert set(load(ictx.layout)["parts"]) == {"hub", "relay", "publish"}
    # a server exists: no question, no second install; the next workspace is recorded on 8151, then 8152
    assert ictx.runner.started.keys() == {"hub", "relay", "publish"}
    from orch.install import cli as icli

    assert icli.add_workspace(ictx, "/w/a") == 8151 and icli.add_workspace(ictx, "/w/b") == 8152


def test_role_argument_is_checked(tmp):
    assert run_main(tmp, ["--role", "bogus"])[0] == 2
    assert run_main(tmp, ["--role"])[0] == 2


def test_ensure_host_retries_enable_and_names_the_reason(monkeypatch):
    import io

    from orch.host import cli as host_cli
    from orch.ops import workspace_init as wi
    from orch.setup import flow as F

    calls = []

    def fake_main(argv, env=None, out=None):
        calls.append(argv)
        out.write("host.busy: still settling\n")
        return 0 if len(calls) == 3 else 4

    class Conn:
        def close(self):
            pass

    state = {"up": False}

    def connect(_s):
        if state["up"] or len(calls) >= 3:
            return Conn()
        raise OSError("down")

    monkeypatch.setattr(host_cli, "main", fake_main)
    monkeypatch.setattr(wi, "connect_host", connect)
    monkeypatch.setattr(F, "ENABLE_RETRY_WAIT", 0)
    F._ensure_host({"ORCH_STATE_DIR": "/nonexistent-orch-state"}, io.StringIO())
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(
        host_cli, "main", lambda argv, env=None, out=None: (calls.append(1), out.write("nope: x\n"), 4)[2]
    )
    monkeypatch.setattr(wi, "connect_host", lambda s: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(F.SetupError) as e:
        F._ensure_host({"ORCH_STATE_DIR": "/nonexistent-orch-state"}, io.StringIO())
    assert "nope: x" in e.value.message and len(calls) == 3


def test_open_workspace_goes_to_the_dashboard_service_with_a_login(tmp, tmp_path):
    from orch.install import cli as icli
    from orch.install.state import load, save

    ictx = icli.Ctx(env={"HOME": str(tmp_path)}, out=io.StringIO(), err=io.StringIO())
    st = load(ictx.layout)
    st["dashboard"] = {"port": 8151, "bind": "127.0.0.1", "url": "http://127.0.0.1:8151/", "service": "x"}
    st["workspaces"] = [{"dir": "/w", "port": 8151}]
    ictx.layout.ensure()
    save(ictx.layout, st)
    seen = []
    ictx.health = lambda host, port, wait=0: True
    ictx.dashboard_entry = lambda: ["orch-dashboard"]
    ictx.mint_login = lambda argv, env: seen.append(argv) or "http://127.0.0.1:8151/#launch=abc&to=%2F"
    assert icli.workspace_login_url(ictx, "/w") == "http://127.0.0.1:8151/#launch=abc&to=%2F"
    assert seen == [["orch-dashboard"]]
    ictx.dashboard_entry = lambda: None
    assert icli.workspace_login_url(ictx, "/w") is None


def test_open_workspace_waits_for_the_service_and_restarts_a_dead_one(tmp, tmp_path):
    """r11-P01: the login link is minted only once the service answers; a service that never answers is started once
    more; still nothing -> LoginUnavailable (setup's Finish turns it into a retryable 503), never a raw exception."""
    from orch.install import cli as icli
    from orch.install.state import load, save

    ictx = icli.Ctx(env={"HOME": str(tmp_path)}, out=io.StringIO(), err=io.StringIO())
    st = load(ictx.layout)
    st["dashboard"] = {"port": 8151, "bind": "127.0.0.1", "url": "http://127.0.0.1:8151/", "service": "x"}
    st["workspaces"] = [{"dir": "/w", "port": 8151}]
    ictx.layout.ensure()
    save(ictx.layout, st)
    ictx.dashboard_entry = lambda: ["orch-dashboard"]
    minted: list = []
    ictx.mint_login = lambda argv, env: minted.append(1) or "http://127.0.0.1:8151/#launch=abc&to=%2F"
    up = {"n": 0}
    ictx.health = lambda host, port, wait=0: up["n"] >= 1
    started: list = []
    real_start = icli.start_dashboard
    icli_start = lambda ctx, bind, port: (started.append(port), up.__setitem__("n", 1))[0]  # noqa: E731
    try:
        icli.start_dashboard = icli_start
        assert icli.workspace_login_url(ictx, "/w", wait=0.05) == "http://127.0.0.1:8151/#launch=abc&to=%2F"
        assert started == [8151] and minted == [1]
        ictx.health = lambda host, port, wait=0: False
        started.clear()
        with pytest.raises(icli.LoginUnavailable):
            icli.workspace_login_url(ictx, "/w", wait=0.05)
        assert started == [8151] and minted == [1]  # no link minted on a service that does not answer
        ictx.health = lambda host, port, wait=0: True
        ictx.mint_login = lambda argv, env: None
        with pytest.raises(icli.LoginUnavailable):
            icli.workspace_login_url(ictx, "/w", wait=0.05)
        ictx.mint_login = lambda argv, env: (_ for _ in ()).throw(OSError("boom"))
        with pytest.raises(icli.LoginUnavailable):
            icli.workspace_login_url(ictx, "/w", wait=0.05)
    finally:
        icli.start_dashboard = real_start
