"""B9 step 1: the socket server: session binding, single-use code, Host/Origin, idle rules."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from orch.setup.flow import Env, SetupFlow
from orch.setup.server import LOST_LINE, SetupServer

from .helpers import call


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def srv():
    with tempfile.TemporaryDirectory(prefix="hk", dir="/tmp") as d:
        run = Path(d) / "run"
        run.mkdir(mode=0o700)
        clock = Clock()
        events: list[str] = []
        wk = Path(d) / "work"
        wk.mkdir()
        env = Env(env={"USER": "ann"}, home=Path(d) / "home", state_dir=Path(d) / "state")
        s = SetupServer(run / "setup.sock", SetupFlow(env), clock=clock, notify=events.append)
        s.workdir = str(wk.resolve())  # type: ignore[attr-defined]
        s.start()
        s.clock_obj, s.events = clock, events  # type: ignore[attr-defined]
        yield s
        s.stop()


def claimed(s):
    path = str(s.path)
    st, out, _ = call(path, "POST", "/launch", {"dir": s.workdir})
    assert st == 200
    st, c, _ = call(path, "POST", "/claim", {"code": out["code"], "port": 8765})
    assert st == 200
    return path, c["token"], out


def test_socket_is_private(srv):
    mode = stat.S_IMODE(os.stat(srv.path).st_mode)
    assert mode == 0o600


def test_no_launch_no_token(srv):
    st, out, _ = call(str(srv.path), "GET", "/api/setup/state", token="x")
    assert st in (401, 403) and out["error"]["code"] in ("setup.token", "setup.host")


def test_claim_is_single_use_and_second_browser_refused(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    st, c, _ = call(path, "POST", "/claim", {"code": out["code"], "port": 8765})
    assert st == 200 and "browser connected" in srv.events
    st, e, _ = call(path, "POST", "/claim", {"code": out["code"], "port": 8765})
    assert st == 409 and e["error"]["code"] == "setup.claimed"
    assert any("second browser" in x for x in srv.events)


def test_code_is_alive_at_299_seconds_and_single_use(srv):
    path = str(srv.path)
    _, alive, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    srv.clock_obj.t += 299
    assert call(path, "POST", "/claim", {"code": alive["code"], "port": 8765})[0] == 200
    assert call(path, "POST", "/claim", {"code": alive["code"], "port": 8765})[0] == 409  # single use


def test_code_expires_after_5_minutes(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    srv.clock_obj.t += 301
    st, e, _ = call(path, "POST", "/claim", {"code": out["code"], "port": 8765})
    assert st == 403 and e["error"]["code"] == "setup.code_invalid"


def test_wrong_code_five_times_kills_the_code(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    for _ in range(5):
        assert call(path, "POST", "/claim", {"code": "nope", "port": 8765})[0] == 403
    assert call(path, "POST", "/claim", {"code": out["code"], "port": 8765})[0] == 403


def test_token_required_and_checked(srv):
    path, token, _ = claimed(srv)
    assert call(path, "GET", "/api/setup/state")[0] == 401
    assert call(path, "GET", "/api/setup/state", token="wrong")[0] == 401
    st, out, h = call(path, "GET", "/api/setup/state", token=token)
    assert st == 200 and out["phase"] == "profile"
    assert h["Cache-Control"] == "no-store"


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1", "127.0.0.1.evil.com:80", "localhost:99999"])
def test_host_header_checked(srv, host):
    path, token, _ = claimed(srv)
    st, out, _ = call(path, "GET", "/api/setup/state", token=token, host=host)
    assert st == 403 and out["error"]["code"] == "setup.host"


def test_localhost_host_accepted(srv):
    path, token, _ = claimed(srv)
    assert call(path, "GET", "/api/setup/state", token=token, host="localhost:8765")[0] == 200


def test_origin_and_content_type_on_post(srv):
    path, token, _ = claimed(srv)
    assert call(path, "POST", "/api/setup/cancel", {}, token=token, origin=None)[0] == 403
    assert call(path, "POST", "/api/setup/cancel", {}, token=token, origin="http://evil.example")[0] == 403
    assert call(path, "POST", "/api/setup/cancel", {}, token=token, ctype="text/plain")[0] == 415


def test_socket_only_routes_are_not_under_the_browser_prefix(srv):
    path, token, _ = claimed(srv)
    for r in ("/api/setup/launch", "/api/setup/claim", "/api/setup/shutdown"):
        assert call(path, "POST", r, {}, token=token)[0] == 404


def test_idle_session_dies_after_ten_minutes(srv):
    path, token, _ = claimed(srv)
    srv.clock_obj.t += 601
    srv.check_idle()
    st, out, _ = call(path, "GET", "/api/setup/state", token=token)
    assert st == 410 and out["error"]["code"] == "setup.ended"


def test_a_page_that_only_polls_state_still_idles_out(srv):
    path, token, _ = claimed(srv)
    for _ in range(55):  # the page asks for /state every second-ish; ten minutes pass
        srv.clock_obj.t += 10
        assert call(path, "GET", "/api/setup/state", token=token)[0] == 200
        srv.check_idle()
    srv.clock_obj.t += 60
    srv.check_idle()
    st, out, _ = call(path, "GET", "/api/setup/state", token=token)
    assert st == 410 and out["error"]["code"] == "setup.ended"


def test_idle_never_during_a_run(srv):
    path, token, _ = claimed(srv)
    srv.flow._running = True
    srv.clock_obj.t += 6000
    srv.check_idle()
    assert call(path, "GET", "/api/setup/state", token=token)[0] == 200


def test_cancel_ends_the_session(srv):
    path, token, _ = claimed(srv)
    assert call(path, "POST", "/api/setup/cancel", {}, token=token)[0] == 200
    assert call(path, "GET", "/api/setup/state", token=token)[0] == 410


def test_oversized_body_refused(srv):
    path, token, _ = claimed(srv)
    try:
        st, out, _ = call(path, "POST", "/api/setup/cancel", {"x": "a" * 70000}, token=token)
    except OSError:  # the server answers and closes before the body is sent: reset, broken pipe or (macOS) ENOTCONN
        return
    assert st == 413


def test_a_launch_while_claimed_is_refused(srv):
    path, _, _ = claimed(srv)
    assert call(path, "POST", "/launch", {"dir": srv.workdir})[0] == 409


def test_the_code_goes_to_a_0600_file_not_a_url(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    f = srv.code_path
    assert f.parent == srv.path.parent and stat.S_IMODE(os.stat(f).st_mode) == 0o600
    import json

    doc = json.loads(f.read_text())
    assert doc["code"] == out["code"] and doc["setup_id"] == out["setup_id"] and doc["expires_at"] > 0
    assert call(path, "POST", "/claim", {"code": out["code"], "port": 8765})[0] == 200
    assert not f.exists()  # deleted at the claim


def test_code_file_is_removed_on_expiry_wrong_tries_stop_and_a_new_launch(srv):
    path = str(srv.path)
    call(path, "POST", "/launch", {"dir": srv.workdir})
    srv.clock_obj.t += 301
    srv.check_idle()
    assert not srv.code_path.exists()
    call(path, "POST", "/launch", {"dir": srv.workdir})
    for _ in range(5):
        call(path, "POST", "/claim", {"code": "nope", "port": 8765})
    assert not srv.code_path.exists()
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    first = srv.code_path.read_text()
    _, out2, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    assert srv.code_path.read_text() != first and out2["code"] != out["code"]
    srv.stop()
    assert not srv.code_path.exists()


def test_claim_needs_the_dashboards_port_and_binds_the_browser_to_it(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    assert call(path, "POST", "/claim", {"code": out["code"]})[0] == 400  # no port
    assert call(path, "POST", "/claim", {"code": out["code"], "port": 0})[0] == 400
    st, c, _ = call(path, "POST", "/claim", {"code": out["code"], "port": 9000})
    assert st == 200
    assert call(path, "GET", "/api/setup/state", token=c["token"], host="127.0.0.1:8765")[0] == 403
    assert call(path, "GET", "/api/setup/state", token=c["token"], host="localhost:9000")[0] == 200


def test_a_launch_that_named_a_port_only_claims_with_that_port(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir, "port": 9000})
    assert call(path, "POST", "/claim", {"code": out["code"], "port": 9001})[0] == 403
    assert call(path, "POST", "/claim", {"code": out["code"], "port": 9000})[0] == 200


def _tty(monkeypatch):
    from orch.ops import workspace_init as wi

    shown: list[str] = []
    monkeypatch.setattr(wi.TERMINAL, "show", shown.append)
    return shown


CODE = "SECRETCODE0123456789abcdefgh"
URL = f"http://127.0.0.1:8123/#launch={CODE}&to=%2Fsetup"


def _fake_run(seen, url, rc=0, extra=b"", stderr=b""):
    import os
    import subprocess

    def run(argv, **kw):
        seen.setdefault("calls", []).append((argv, kw.get("env")))
        fd = int(argv[argv.index("--emit-url-fd") + 1])
        assert kw["pass_fds"] == (fd,)
        if rc == 0:
            os.write(fd, (url + "\n").encode() + extra)
        return subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=stderr)

    return run


def _setup(monkeypatch, run):
    import io
    import subprocess

    from orch.cli import dashboard as dash

    monkeypatch.setattr(dash, "find_entry_point", lambda env, err: ["orch-dashboard"])
    monkeypatch.setattr(subprocess, "run", run)
    return io.StringIO(), _tty(monkeypatch)


def test_the_opener_prints_one_link_and_puts_no_code_in_any_child_argument(monkeypatch):
    from orch.setup import flow as flow_mod

    seen = {}
    err, tty = _setup(monkeypatch, _fake_run(seen, URL))
    assert flow_mod._open_browser({"PATH": "/usr/bin"}, err, {"setup_id": "su_1", "code": CODE}) is True
    assert err.getvalue() == ""  # the code never reaches stderr/stdout
    ((argv, env),) = seen["calls"]
    assert argv[:3] == ["orch-dashboard", "open", "/setup"] and "--no-browser" not in argv
    assert CODE not in repr(argv) and CODE not in repr(env)
    lines = "".join(tty).splitlines()
    assert lines == [f"If no browser opened, use this link (single use, 5 minutes): {URL}"]
    assert "#launch=" in lines[0] and "?" not in lines[0]


def test_no_browser_passes_the_flag_and_always_prints_the_link(monkeypatch):
    from orch.setup import flow as flow_mod

    seen = {}
    err, tty = _setup(monkeypatch, _fake_run(seen, URL, extra=b"no-browser\n"))
    assert flow_mod._open_browser({}, err, {}, no_browser=True) == "link"
    assert "--no-browser" in seen["calls"][0][0] and CODE not in repr(seen["calls"][0][0])
    assert "".join(tty).startswith("Open this link in your browser (single use, 5 minutes): ") and err.getvalue() == ""


def test_a_failed_browser_open_is_reported_apart_from_a_success(monkeypatch):
    from orch.setup import flow as flow_mod

    err, tty = _setup(monkeypatch, _fake_run({}, URL, extra=b"no-browser\n"))
    assert flow_mod._open_browser({}, err, {}) == "link"
    assert "Open this link" in "".join(tty)


def test_without_a_writable_terminal_no_link_is_shown_anywhere(monkeypatch):
    from orch.custody import NoPrompt
    from orch.ops import workspace_init as wi
    from orch.setup import flow as flow_mod

    err, _ = _setup(monkeypatch, _fake_run({}, URL, extra=b"no-browser\n"))

    def broken(text):
        raise NoPrompt("no controlling terminal")

    monkeypatch.setattr(wi.TERMINAL, "show", broken)
    assert flow_mod._open_browser({}, err, {}, no_browser=True) is False
    assert CODE not in err.getvalue() and "your own terminal" in err.getvalue()


def test_a_failing_launcher_tail_never_contains_the_code(monkeypatch):
    from orch.setup import flow as flow_mod

    noisy = f"boom http://127.0.0.1:8123/#launch={CODE}&to=%2Fsetup".encode()
    err, tty = _setup(monkeypatch, _fake_run({}, URL, rc=1, stderr=noisy))
    assert flow_mod._open_browser({}, err, {}) is False
    assert "did not start" in err.getvalue() and CODE not in err.getvalue() and tty == []


def test_a_bad_line_from_the_launcher_prints_nothing(monkeypatch):
    from orch.setup import flow as flow_mod

    for bad in (
        "http://127.0.0.1:1/?launch=x#launch=" + CODE + "&to=%2Fsetup",
        "http://evil.example/#launch=" + CODE + "&to=%2Fsetup",
        "http://127.0.0.1:8123/#launch=short&to=%2Fsetup",
        URL + "\x1b[2J",
    ):
        err, tty = _setup(monkeypatch, _fake_run({}, bad))
        assert flow_mod._open_browser({}, err, {}) is False and CODE not in err.getvalue() and tty == []


# --- K2 (B12 run 3): a reload or a closed page is noticed, and Enter mints a fresh single-use link -----------------


def _poll(path, token, srv):
    st = call(path, "GET", "/api/setup/state", token=token)[0]
    srv.check_idle()
    return st


def test_a_polling_page_is_never_reported_lost(srv):
    path, token, _ = claimed(srv)
    for _ in range(30):
        srv.clock_obj.t += 1
        assert _poll(path, token, srv) == 200
    assert not srv.lost and "closed or reloaded" not in " ".join(srv.events)


def test_a_hidden_background_tab_polling_once_a_minute_is_never_reported_lost(srv):
    path, token, _ = claimed(srv)
    # 9 minutes, inside the idle limit: Chrome throttles a hidden tab to one timer a minute (not "left")
    for _ in range(9):
        srv.clock_obj.t += 60
        assert _poll(path, token, srv) == 200
    assert not srv.lost and not any("closed or reloaded" in e for e in srv.events)


def test_150_seconds_of_silence_is_reported_once(srv):
    path, token, _ = claimed(srv)
    srv.clock_obj.t += 150
    srv.check_idle()
    assert not srv.lost  # exactly 150 s is still within the limit
    srv.clock_obj.t += 0.5
    srv.check_idle()
    srv.check_idle()
    lines = [e for e in srv.events if "closed or reloaded" in e]
    assert lines == [
        "The setup page was closed or reloaded. Press Enter to open a fresh setup link, or Ctrl-C to stop."
    ]
    assert srv.lost


def test_a_leave_beacon_with_the_token_is_reported_at_once_and_once(srv):
    path, token, _ = claimed(srv)
    assert call(path, "POST", "/api/setup/left", {}, token=token)[0] == 200
    assert srv.lost and [e for e in srv.events if "closed or reloaded" in e] == [LOST_LINE]
    assert call(path, "POST", "/api/setup/left", {}, token=token)[0] in (200, 401, 403)
    assert [e for e in srv.events if "closed or reloaded" in e] == [LOST_LINE]


@pytest.mark.parametrize("kind", ["none", "wrong", "host", "origin"])
def test_a_leave_beacon_without_a_valid_token_host_or_origin_is_ignored(srv, kind):
    path, token, _ = claimed(srv)
    kw = {
        "none": {},
        "wrong": {"token": "x" * 43},
        "host": {"token": token, "host": "evil.example:8765"},
        "origin": {"token": token, "origin": "http://evil.example"},
    }[kind]
    assert call(path, "POST", "/api/setup/left", {}, **kw)[0] in (401, 403)
    assert not srv.lost and not any("closed or reloaded" in e for e in srv.events)


def test_a_leave_beacon_during_a_run_is_ignored(srv):
    path, token, _ = claimed(srv)
    srv.flow._running = True
    assert call(path, "POST", "/api/setup/left", {}, token=token)[0] == 200
    assert not srv.lost


def test_the_page_coming_back_clears_the_notice(srv):
    path, token, _ = claimed(srv)
    srv.clock_obj.t += 151
    srv.check_idle()
    assert srv.lost
    assert call(path, "GET", "/api/setup/state", token=token)[0] == 200
    assert not srv.lost and srv.events[-1].startswith("the setup page is back")
    assert srv.relink() is None  # nothing was lost: Enter does nothing


def test_relink_refuses_old_token_and_gives_new_words(srv):
    from .conftest import PROFILE
    from .test_secrets import words_for

    path, old, _ = claimed(srv)
    api = lambda m, r, b=None, t=old: call(path, m, "/api/setup/" + r, b, token=t)  # noqa: E731
    assert api("POST", "profile", PROFILE)[0] == 200
    shown = api("POST", "secrets/reveal", {})[1]
    assert api("POST", "secrets/reveal", {})[0] in (409, 410)  # shown once
    assert srv.relink() is None  # the page is not lost yet
    srv.clock_obj.t += 151
    srv.check_idle()
    fresh = srv.relink()
    assert fresh is not None and fresh["code"]
    # the old token is refused at once, on reads and on writes; nothing of the old session answers
    assert api("GET", "state")[0] in (401, 403) and api("POST", "secrets/reveal", {})[0] in (401, 403)
    # a second relink has nothing to replace until the new page is bound and lost in its turn
    assert srv.relink() is None
    st, c, _ = call(path, "POST", "/claim", {"code": fresh["code"], "port": 8765})
    assert st == 200 and c["token"] != old
    assert call(path, "POST", "/claim", {"code": fresh["code"], "port": 8765})[0] in (403, 409)  # single use
    new = lambda m, r, b=None: api(m, r, b, c["token"])  # noqa: E731
    assert new("GET", "state")[1]["phase"] == "profile"  # the flow started over
    assert new("POST", "secrets/reveal", {})[0] == 409  # no secrets carried over: the profile step comes first
    assert new("POST", "profile", PROFILE)[0] == 200
    again = new("POST", "secrets/reveal", {})[1]
    assert again["recovery_words"] != shown["recovery_words"]  # NEW words, never the old ones
    assert words_for(again)  # and the new page can go on


def test_relink_never_replaces_a_run_in_progress(srv):
    path, token, _ = claimed(srv)
    srv.clock_obj.t += 151
    srv.check_idle()
    assert srv.lost
    srv.flow._running = True
    assert srv.relink() is None
    assert call(path, "GET", "/api/setup/state", token=token)[0] == 200  # the old session is untouched


def test_a_leave_call_does_not_reset_the_idle_timer(srv):
    path, token, _ = claimed(srv)
    srv.clock_obj.t += 590
    assert call(path, "POST", "/api/setup/left", {}, token=token)[0] == 200
    srv.clock_obj.t += 20  # 610 s since the last real request: the leave call did not count as activity
    srv.check_idle()
    assert srv.ended == "idle"


def test_stop_waits_for_the_request_that_ended_the_session_to_be_answered(srv):
    """#949: ``die()`` runs inside the request that ends the session (finish, shutdown), before its response is
    written. The command's main loop wakes on ``stop_event`` and calls ``stop()``; ``stop()`` must not return (and the
    process exit) while that response is still being written, or the page loses its last answer.

    No timing: the request is held inside ``die`` until the test has seen ``stop()`` block on the in-flight count,
    then released; ``stop()`` may only return after the request is done. The waits below are failure guards only."""
    import threading

    path, _tok, _ = claimed(srv)
    # The handler thread of a request the client already has the answer to may still be on its way to
    # ``request_done`` (it runs after the response is written). Let those finish before the test counts, or their
    # late "request done" lands in ``order`` (#1047).
    with srv._idle:
        assert srv._idle.wait_for(lambda: srv._inflight == 0, timeout=10), "an earlier request never finished"
    real_die = srv.die
    in_die, release, stop_blocked = threading.Event(), threading.Event(), threading.Event()
    order: list[str] = []

    def held_die(why):
        real_die(why)  # stop_event is set from here on
        in_die.set()
        release.wait(10)  # ... and the response is not written until the test lets it go

    real_request_done, real_wait_for = srv.request_done, srv._idle.wait_for

    def request_done():
        order.append("request done")  # before the real call: that call wakes stop(), which then records its own return
        real_request_done()

    def wait_for(predicate, timeout=None):
        stop_blocked.set()  # stop() is now waiting for the in-flight request
        return real_wait_for(predicate, timeout)

    srv.die = held_die  # type: ignore[method-assign]
    srv.request_done = request_done  # type: ignore[method-assign]
    srv._idle.wait_for = wait_for  # type: ignore[method-assign]

    got: list = []

    def stopper():
        srv.stop()  # what the command does as soon as it sees the event
        order.append("stop returned")

    request = threading.Thread(target=lambda: got.append(call(path, "POST", "/shutdown", {})))
    request.start()
    assert in_die.wait(10) and srv.stop_event.wait(10)
    stopping = threading.Thread(target=stopper)
    stopping.start()
    try:
        assert stop_blocked.wait(10), "stop() did not wait for the in-flight request"
    finally:
        release.set()
    stopping.join(10)
    request.join(10)
    assert not stopping.is_alive() and not request.is_alive()
    assert got and got[0][0] == 200
    assert order == ["request done", "stop returned"], order
