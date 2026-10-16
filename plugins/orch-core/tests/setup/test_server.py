"""B9 step 1: the socket server: session binding, single-use code, Host/Origin, idle rules."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from orch.setup.flow import Env, SetupFlow
from orch.setup.server import SetupServer

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


def test_code_expires_after_30_seconds(srv):
    path = str(srv.path)
    _, out, _ = call(path, "POST", "/launch", {"dir": srv.workdir})
    srv.clock_obj.t += 31
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
    except (BrokenPipeError, ConnectionResetError):  # the server answers and closes before the body is sent
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
    srv.clock_obj.t += 31
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


def test_the_opener_puts_no_code_in_the_path_or_the_arguments(monkeypatch):
    import subprocess

    from orch.cli import dashboard as dash
    from orch.setup import flow as flow_mod

    seen = {}
    monkeypatch.setattr(dash, "find_entry_point", lambda env, err: ["orch-dashboard"])

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert flow_mod._open_browser({"PATH": "/usr/bin"}, None, {"setup_id": "su_1", "code": "SECRETCODE"})
    assert seen["argv"] == ["orch-dashboard", "open", "/setup"]
    assert "SECRETCODE" not in repr(seen)
