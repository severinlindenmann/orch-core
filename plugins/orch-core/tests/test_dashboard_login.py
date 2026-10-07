"""The dashboard sign-in: one cookie per port (dashboards on one host do not sign each other out), kept for 30 days,
and a token kept per workspace beside the ledger, so a restart of `orch serve` keeps open tabs signed in. Agents
cannot read the kept token, and only a human terminal prints the sign-in link."""
import os
import stat

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from orch.dashboard import serve_token
from orch.dashboard.app import create_app


def _bash(ws, cmd):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})


def _tool(ws, tool, **inp):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": tool, "tool_input": inp, "cwd": str(ws.root)})


def test_cookie_is_named_after_the_port_and_lasts_30_days(ws):
    c = TestClient(create_app(ws, "tok", port=8767))
    r = c.get("/a/INT-0001/visuals/index.html?token=tok", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/a/INT-0001/visuals/index.html"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("orch_token_8767=tok;") and "max-age=2592000" in cookie.lower()
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()


def test_another_ports_cookie_does_not_sign_in(ws):
    app = create_app(ws, "tok", port=8767)
    assert TestClient(app, cookies={"orch_token_8765": "tok"}).get("/").status_code == 401
    assert TestClient(app, cookies={"orch_token": "tok"}).get("/").status_code == 401
    assert TestClient(app, cookies={"orch_token_8767": "tok"}).get("/").status_code == 200


def test_locked_page_says_how_to_get_the_link(ws):
    r = TestClient(create_app(ws, "tok", port=8767)).get("/a/INT-0001/visuals/index.html")
    assert r.status_code == 401 and "Locked" in r.text and "orch serve --link" in r.text


def test_token_is_kept_across_starts(ws):
    first = serve_token.load_or_create(ws)
    assert serve_token.load_or_create(ws) == first == serve_token.read(ws)
    path = serve_token.token_path(ws)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.parent.parent.name == "permits"  # beside the ledger, where the guard keeps agents out


def test_new_token_replaces_the_kept_one(ws):
    first = serve_token.load_or_create(ws)
    second = serve_token.load_or_create(ws, new=True)
    assert second != first and serve_token.read(ws) == second


def test_a_malformed_token_file_gets_a_fresh_token(ws):
    path = serve_token.token_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("short\n", encoding="utf-8")
    assert serve_token.read(ws) is None
    token = serve_token.load_or_create(ws)
    assert token != "short" and serve_token.read(ws) == token


def test_guard_keeps_agents_from_the_kept_token(ws):
    path = serve_token.token_path(ws)
    serve_token.load_or_create(ws)
    assert not _tool(ws, "Read", file_path=str(path)).allow
    assert not _tool(ws, "Grep", pattern="x", path=str(path.parent)).allow
    assert not _bash(ws, f"cat {path}").allow
    assert not _bash(ws, "cat ~/.config/orch/permits/dashboard/*.token").allow


@pytest.fixture
def human(monkeypatch):
    import uvicorn
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: [s.close() for s in sockets])


def _printed_token(out):
    return out.split("?token=", 1)[1].split()[0]


def test_serve_keeps_the_token_across_restarts(ws_root, human, capsys):
    from orch.cli import run
    assert run(["serve", "--no-open", "--no-update", "--port", "9998"]) == 0
    first = _printed_token(capsys.readouterr().out)
    assert run(["serve", "--no-open", "--no-update", "--port", "9998"]) == 0
    assert _printed_token(capsys.readouterr().out) == first
    assert run(["serve", "--no-open", "--no-update", "--port", "9998", "--new-token"]) == 0
    assert _printed_token(capsys.readouterr().out) != first


def test_serve_link_prints_the_running_dashboards_link(ws_root, ws, human, monkeypatch, capsys):
    from orch.cli import run
    from orch.dashboard import switcher
    assert run(["serve", "--no-open", "--no-update", "--port", "9998"]) == 0
    token = _printed_token(capsys.readouterr().out)
    monkeypatch.setattr(switcher, "serves", lambda w, port: port == 9998)
    assert run(["serve", "--link"]) == 0
    assert capsys.readouterr().out.strip() == f"http://127.0.0.1:9998/?token={token}"


def test_serve_link_without_a_running_dashboard(ws_root, human, capsys):
    from orch.cli import run
    assert run(["serve", "--link"]) != 0
    captured = capsys.readouterr()
    assert "?token=" not in captured.out and "no dashboard is running" in captured.err


def test_serve_link_refused_to_agents(ws_root, ws, monkeypatch, capsys):
    from orch import actor
    from orch.cli import run
    serve_token.load_or_create(ws)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["serve", "--link"]) == 3
    captured = capsys.readouterr()
    assert "?token=" not in captured.out and "your own terminal" in captured.err
