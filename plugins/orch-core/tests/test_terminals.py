"""Terminals in Mission Control (issue #40): orch's tmux server, screens as escaped HTML, keys back in; all of it off
until the `terminals` default addon is enabled."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orch.dashboard import terminals
from orch.errors import ValidationError


class FakeTmux:
    """Stands in for `terminals.tmux`: answers list-sessions and capture-pane, records everything else."""

    def __init__(self, sessions=(), screen="hello"):
        self.sessions = list(sessions)  # (name, path)
        self.screen = screen
        self.calls = []

    def __call__(self, args, timeout=5):
        self.calls.append(args)
        out, rc = "", 0
        if args[0] == "list-sessions":
            fmt = args[args.index("-F") + 1]
            if not self.sessions:
                rc = 1  # tmux: "no server running"
            elif fmt == "#{session_name}":
                out = "\n".join(n for n, _ in self.sessions)
            else:
                out = "\n".join(f"{n}\t{p}\t100\t{200 + i}\t200\t50\t{4000 + i}" for i, (n, p) in enumerate(self.sessions))
        elif args[0] == "display-message":
            # capture_many: [display-message (header), capture-pane] per session, chained with ";"; tmux stops at the
            # first session that is gone
            cmds, cur = [], []
            for a in args:
                if a == ";":
                    cmds.append(cur)
                    cur = []
                else:
                    cur.append(a)
            cmds.append(cur)
            for cmd in cmds:
                name = cmd[cmd.index("-t") + 1][1:-1]
                if not any(n == name for n, _ in self.sessions):
                    rc = 1
                    break
                if cmd[0] == "display-message":
                    out += cmd[-1].replace("#{session_name}", name).replace("#{pane_width} #{pane_height}", "200 50") + "\n"
                else:
                    out += f"{self.screen}\n"
        elif args[0] == "capture-pane":
            name = args[args.index("-t") + 1][1:-1]
            rc = 0 if any(n == name for n, _ in self.sessions) else 1
            out = f"{self.screen}\n200 50\n" if rc == 0 else ""  # the screen, then display-message's size line
        return subprocess.CompletedProcess(args, rc, out, "")


LOCAL = "http://127.0.0.1:8765"


def _client(ws, base_url=LOCAL, client=("127.0.0.1", 50000)):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"), base_url=base_url, client=client)
    assert c.get("/?token=tok").status_code == 200
    return c


@pytest.fixture
def dash(ws):
    """The dashboard as the browser on this machine sees it: Terminals answer only to a local request."""
    pytest.importorskip("fastapi")
    return _client(ws)


@pytest.fixture
def fake(monkeypatch, ws):
    t = FakeTmux(sessions=[("DEMO-1", str(ws.root)), ("scratch", str(ws.root / "sub")), ("other", "/elsewhere")])
    monkeypatch.setattr(terminals, "tmux", t)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)  # the `terminals` addon is enabled
    return t


@pytest.fixture
def off(monkeypatch, ws):
    """tmux is installed and has this workspace's session, but the `terminals` addon is not enabled."""
    t = FakeTmux(sessions=[("DEMO-1", str(ws.root))])
    monkeypatch.setattr(terminals, "tmux", t)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    return t


# ---- the screen ----------------------------------------------------------------------------------------------------

def test_screen_text_is_escaped_and_only_spans_are_added():
    out = terminals.ansi_to_html('\x1b[31m<script>alert("x")</script>\x1b[0m & done')
    assert "<script>" not in out and "&lt;script&gt;" in out and "&amp; done" in out
    assert out.startswith('<span style="color:#ff6b6b">')


def test_sgr_colours_bold_reverse_256_and_truecolor():
    assert terminals.ansi_to_html("\x1b[1;92mok\x1b[22;39m.") == '<span style="color:#a9f7c0;font-weight:700">ok</span>.'
    assert 'color:#5fd7ff' in terminals.ansi_to_html("\x1b[38;5;81mx")
    assert 'background:#0a141e' in terminals.ansi_to_html("\x1b[48;2;10;20;30mx")
    assert terminals.ansi_to_html("\x1b[7mx") == '<span style="color:#15171a;background:#d6dae0">x</span>'


def test_other_escapes_are_dropped():
    assert terminals.ansi_to_html("a\x1b[2K\x1b[?25lb\x1b]0;title\x07c\x1b(Bd") == "abcd"


# ---- sessions ------------------------------------------------------------------------------------------------------

def test_only_this_workspaces_sessions_are_listed(ws, fake):
    assert [s.name for s in terminals.sessions(ws)] == ["scratch", "DEMO-1"]  # most recent first, /elsewhere left out


def test_no_tmux_means_no_sessions(ws, monkeypatch):
    monkeypatch.setattr(terminals, "which", lambda name: None)
    monkeypatch.setattr(terminals, "tmux", lambda *a, **k: pytest.fail("tmux must not run"))
    assert terminals.sessions(ws) == [] and not terminals.available()


@pytest.mark.parametrize("name", ["other", "bad name", "-t", "x;y", ""])
def test_find_refuses_other_workspaces_and_bad_names(ws, fake, name):
    with pytest.raises(ValidationError):
        terminals.find(ws, name)


def test_free_name_counts_up(ws, fake):
    assert terminals.free_name(ws, "DEMO-1") == "DEMO-1-2"
    assert terminals.free_name(ws, "DEMO-9") == "DEMO-9"


# ---- keys ----------------------------------------------------------------------------------------------------------

def test_send_types_text_literally_then_keys_in_order(fake):
    terminals.send("DEMO-1", [{"text": "-n yes; rm"}, {"key": "Enter"}, {"key": "C-c"}])
    sent = [c for c in fake.calls if c[0] == "send-keys"]
    assert sent == [["send-keys", "-t", "=DEMO-1:", "-l", "--", "-n yes; rm"],
                    ["send-keys", "-t", "=DEMO-1:", "Enter"], ["send-keys", "-t", "=DEMO-1:", "C-c"]]


@pytest.mark.parametrize("seq", [[{"key": "run-shell"}], [{"text": "a\nb"}], [{"text": ""}], ["x"], "x",
                                 [{"text": "a", "key": "Enter"}], [{"key": "C-1"}]])
def test_send_refuses_anything_else(fake, seq):
    with pytest.raises(ValidationError):
        terminals.send("DEMO-1", seq)
    assert not [c for c in fake.calls if c[0] == "send-keys"]


# ---- launcher ------------------------------------------------------------------------------------------------------

def test_tmux_launcher_runs_detached_on_orchs_socket():
    from orch.dashboard.launch import argv_for
    a = argv_for("tmux", cwd="/w", command_argv=["claude", "x y"], name="DEMO-1", script_path=None, custom=[])
    assert a[:9] == ["tmux", "-L", "orch", "-f", "/dev/null", "new-session", "-d", "-s", "DEMO-1"]
    # inside the session a plain `tmux` must not reach orch's server: the agent runs without $TMUX
    assert a[-1] == "env -u TMUX -u TMUX_PANE claude 'x y'"


def test_orchs_tmux_server_never_reads_a_tmux_config(monkeypatch):
    """~/.tmux.conf is a file agents can write: the server orch talks to is never started with it."""
    from orch.dashboard import terminals as t
    seen = []
    monkeypatch.setattr(t.subprocess, "run", lambda argv, **k: seen.append(argv))
    t.tmux(["list-sessions"])
    assert seen[0][:5] == ["tmux", "-L", t.SOCKET, "-f", "/dev/null"]


def test_a_trailing_semicolon_survives_tmuxs_parser(fake):
    from orch.dashboard.launch import tmux_arg
    assert tmux_arg("ls;") == "ls\\;" and tmux_arg("a\\;") == "a\\\\;" and tmux_arg("x") == "x"
    terminals.send("DEMO-1", [{"text": "ls;"}])
    assert fake.calls[-1][-1] == "ls\\;"


def test_start_agent_open_in_mission_control(dash, ws, put, fake, monkeypatch):
    from orch.dashboard import launch

    class Proc:
        def __init__(self, argv, **kw):
            launched.append(argv)

        def wait(self, timeout=None):
            return 0

    launched = []
    monkeypatch.setattr(launch.subprocess, "Popen", Proc)
    tid = put("open")
    assert "Open in Mission Control" in dash.get(f"/t/{tid}").text
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude", "where": "tmux"},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/terminals/{tid}")
    assert launched[0][:3] == ["tmux", "-L", "orch"]


# ---- pages and routes ----------------------------------------------------------------------------------------------

def test_menu_and_page_hidden_or_explained_without_tmux(dash, monkeypatch):
    monkeypatch.setattr(terminals, "which", lambda name: None)
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)  # enabled, but no tmux
    assert 'href="/terminals"' not in dash.get("/").text
    assert "tmux is not installed" in dash.get("/terminals").text


def test_grid_and_view_render_screens(dash, fake):
    fake.screen = "\x1b[32mready\x1b[0m <b>"
    grid = dash.get("/terminals").text
    assert 'href="/terminals"' in grid and 'data-screen="DEMO-1"' in grid and "&lt;b&gt;" in grid
    assert 'data-screen="other"' not in grid
    view = dash.get("/terminals/DEMO-1")
    assert view.status_code == 200 and 'data-term="DEMO-1"' in view.text and "ready" in view.text
    assert dash.get("/terminals/other").status_code == 404


ORIGIN = {"origin": LOCAL}


def _stream(ws, name, rounds=1):
    """The view's SSE generator for `rounds` rounds (TestClient waits for a body to end; an SSE body never does)."""
    import asyncio
    from types import SimpleNamespace
    from orch.dashboard import routes_terminals

    async def run():
        seen = {"n": 0}

        async def is_disconnected():
            seen["n"] += 1
            return seen["n"] > rounds

        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ws=ws)), is_disconnected=is_disconnected,
                              client=SimpleNamespace(host="127.0.0.1"), headers={"host": "127.0.0.1:8765"})
        resp = await routes_terminals.view_stream(req, name)
        return [chunk async for chunk in resp.body_iterator]

    return asyncio.run(run())


def test_view_stream_sends_the_screen_then_says_when_it_ended(ws, fake, monkeypatch):
    from orch.dashboard import routes_terminals
    monkeypatch.setattr(routes_terminals, "VIEW_SECONDS", 0)
    fake.screen = "<b>hi</b>"
    events = _stream(ws, "DEMO-1")
    head, data = events[1].split("\ndata: ", 1)
    screen = json.loads(data)
    assert head == "event: screen" and screen["cols"] == 200 and screen["rows"] == 50
    assert screen["html"] == screen["trim"] == screen["tail"] == "&lt;b&gt;hi&lt;/b&gt;"
    monkeypatch.setattr(terminals, "capture", lambda name: None)  # the program exited after the stream opened
    assert _stream(ws, "DEMO-1")[1] == 'event: gone\ndata: "DEMO-1"\n\n'


def test_keys_need_a_same_origin_header(dash, fake):
    body = {"seq": [{"text": "hi"}]}
    assert dash.post("/terminals/DEMO-1/keys", json=body).status_code == 403
    assert dash.post("/terminals/DEMO-1/keys", json=body, headers={"origin": "http://evil"}).status_code == 403
    assert dash.post("/terminals/DEMO-1/keys", json=body, headers=ORIGIN).status_code == 204
    assert ["send-keys", "-t", "=DEMO-1:", "-l", "--", "hi"] in fake.calls


def test_keys_to_another_workspaces_session_are_refused(dash, fake):
    assert dash.post("/terminals/other/keys", json={"seq": [{"text": "hi"}]}, headers=ORIGIN).status_code == 404
    assert not [c for c in fake.calls if c[0] == "send-keys"]


def test_bad_keys_are_a_400(dash, fake):
    assert dash.post("/terminals/DEMO-1/keys", json={"seq": [{"key": "kill-server"}]}, headers=ORIGIN).status_code == 400


def test_size_is_clamped(dash, fake):
    assert dash.post("/terminals/DEMO-1/size", json={"cols": 9999, "rows": 1}, headers=ORIGIN).status_code == 204
    assert ["resize-window", "-t", "=DEMO-1:", "-x", "400", "-y", "5"] in fake.calls


def test_end_confirms_without_js_then_kills(dash, fake):
    r = dash.post("/terminals/DEMO-1/end", data={"ask": "1"}, headers=ORIGIN)
    assert r.status_code == 200 and "End session" in r.text
    assert not [c for c in fake.calls if c[0] == "kill-session"]
    r = dash.post("/terminals/DEMO-1/end", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and ["kill-session", "-t", "=DEMO-1"] in fake.calls


class Proc:
    """Stands in for launch's Popen: records the argv, tmux "returns" at once."""
    launched: list = []

    def __init__(self, argv, **kw):
        Proc.launched.append(argv)

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def launched(monkeypatch):
    from orch.dashboard import launch
    Proc.launched = []
    monkeypatch.setattr(launch.subprocess, "Popen", Proc)
    return Proc.launched


def test_new_scratch_session_runs_the_addons_harness_without_a_prompt(dash, fake, launched):
    r = dash.post("/terminals/new", data={"harness": "copilot"}, headers=ORIGIN, follow_redirects=False)  # ignored
    assert r.status_code == 303 and r.headers["location"].startswith("/terminals/scratch-2")
    assert launched[0][-1] == "env -u TMUX -u TMUX_PANE claude" and launched[0][launched[0].index("-s") + 1] == "scratch-2"
    assert "Runs Claude Code" in dash.get("/terminals").text


def test_mission_control_runs_only_the_addons_harness(dash, put, fake, launched):
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "codex", "where": "tmux"},
                  follow_redirects=False)
    assert "only+for+now" in r.headers["location"] and not launched


def test_mission_control_is_the_primary_button_unless_turned_off(dash, ws, put, fake):
    import re as _re
    from orch.addons import userfiles

    def primary(html):
        return _re.search(r'class="btn btn-primary" name="where" value="(\w*)"', html).group(1)

    tid = put("open")
    assert primary(dash.get(f"/t/{tid}").text) == "tmux"  # the addon's default: start here
    userfiles.save_addon_config(ws.root, "terminals", {"open_here": False})
    html = dash.get(f"/t/{tid}").text
    assert primary(html) == "" and 'name="where" value="tmux"' in html  # Open in terminal first, Mission Control second


def test_setup_checks_tmux_and_the_cli_only_while_the_addon_is_on(ws, monkeypatch):
    from orch import onboarding
    from orch.addons import userfiles
    codes = lambda: {c.code: c for c in onboarding.doctor(ws.root)}  # noqa: E731
    assert "tmux" not in codes() and "terminals-cli" not in codes()
    userfiles.set_enabled(ws.root, "terminals", True)
    monkeypatch.setattr(terminals, "which", lambda name: None)
    found = codes()
    assert not found["tmux"].ok and "brew install tmux" in found["tmux"].fix
    assert not found["terminals-cli"].ok and "Claude Code" in found["terminals-cli"].fix
    assert {"tmux", "terminals-cli"} <= {c.code for c in onboarding.open_setup_items(ws)}
    monkeypatch.setattr(terminals, "which", lambda name: f"/usr/bin/{name}")
    found = codes()
    assert found["tmux"].ok and found["terminals-cli"].ok


# ---- guard ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["tmux -L orch send-keys -t DEMO-1 'rm -rf ~' Enter", "tmux -Lorch capture-pane -p",
                                 "tmux -2 -L orch attach", "cd x && tmux -S /tmp/tmux-501/orch ls",
                                 'tmux -L "orch" ls', "tmux -L'orch' ls", "TMUX=/private/tmp/tmux-501/orch,1,0 tmux ls"])
def test_guard_keeps_agents_off_orchs_tmux(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert not d.allow and "Mission Control" in d.reason


@pytest.mark.parametrize("cmd", ["tmux ls", "tmux -L work new -d", "tmux -L orchard ls"])
def test_guard_leaves_other_tmux_alone(ws, cmd):
    from orch.hooks.guard import evaluate
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


# ---- the switch: the `terminals` default addon --------------------------------------------------------------------

ADDON_DIR = Path(__file__).resolve().parents[1] / "addons" / "terminals"


def test_everything_is_off_until_the_addon_is_enabled(dash, put, off, monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may start"))
    assert 'href="/terminals"' not in dash.get("/").text
    page = dash.get("/terminals")
    assert page.status_code == 404 and "Terminals is off in this workspace" in page.text
    assert dash.get("/terminals/DEMO-1").status_code == 404
    assert dash.get("/terminals/stream").status_code == 404
    assert dash.post("/terminals/DEMO-1/keys", json={"seq": [{"text": "x"}]}, headers=ORIGIN).status_code == 404
    assert dash.post("/terminals/DEMO-1/size", json={"cols": 80, "rows": 24}, headers=ORIGIN).status_code == 404
    assert dash.post("/terminals/new", data={"harness": "claude"}, headers=ORIGIN).status_code == 404
    r = dash.post("/terminals/DEMO-1/end", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    tid = put("open")
    assert "Open in Mission Control" not in dash.get(f"/t/{tid}").text
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude", "where": "tmux"},
                  follow_redirects=False)
    assert "enable+it+in+Workspace" in r.headers["location"]
    assert not [c for c in off.calls if c[0] in ("send-keys", "resize-window", "kill-session")]


def test_launch_json_tmux_default_is_refused_while_off(dash, put, off, monkeypatch):
    from orch.dashboard import launch
    launch.config_path().parent.mkdir(parents=True, exist_ok=True)
    launch.config_path().write_text(json.dumps({"terminal": "tmux"}))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may start"))
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, follow_redirects=False)
    assert "enable+it+in+Workspace" in r.headers["location"]


def test_enabling_the_real_default_addon_turns_it_on(ws, off):
    from orch.addons import userfiles
    assert not terminals.addon_on(ws) and not terminals.enabled(ws)
    userfiles.set_enabled(ws.root, "terminals", True)
    ws._addons = None
    assert ws.addons.get("terminals") is not None, ws.addons.problems
    assert terminals.enabled(ws)


def test_the_addon_passes_orch_addon_check():
    from orch.addons.check import static_problems
    from orch.testing import FakeRunner, run_addon_contract
    recs = json.loads((ADDON_DIR / "tests" / "fixtures" / "list-sessions.json").read_text())
    assert static_problems(ADDON_DIR) == []
    assert run_addon_contract(ADDON_DIR, runner=FakeRunner(recs, strict=False)) == []


def test_the_addon_counts_only_this_workspace():
    sys.path.insert(0, str(ADDON_DIR))
    try:
        from orch_terminals import count
    finally:
        sys.path.remove(str(ADDON_DIR))
    assert count("/tmp/orch-demo\n/tmp/orch-demo/sub\n/elsewhere\n", "/tmp/orch-demo") == 2
    assert count("", "/tmp/orch-demo") == 0


# ---- local only: never over the network, never to a rebound Host ---------------------------------------------------

@pytest.mark.parametrize("base_url, client", [
    ("http://192.168.1.20:8765", ("192.168.1.30", 50000)),  # orch serve --lan, a phone on the network
    ("http://attacker.example:8765", ("127.0.0.1", 50000)),  # a name that resolves to this machine (DNS rebinding)
    ("http://127.0.0.1:8765", ("10.0.0.5", 50000)),  # a loopback Host header from another machine
])
def test_terminals_answer_only_to_a_local_request(ws, put, fake, launched, base_url, client):
    pytest.importorskip("fastapi")
    c = _client(ws, base_url=base_url, client=client)
    origin = {"origin": base_url}
    assert 'href="/terminals"' not in c.get("/").text
    page = c.get("/terminals")
    assert page.status_code == 404 and "only on this machine" in page.text and 'data-screen="DEMO-1"' not in page.text
    assert c.get("/terminals/DEMO-1").status_code == 404
    assert c.get("/terminals/stream").status_code == 404
    assert c.get("/terminals/DEMO-1/stream").status_code == 404
    assert c.post("/terminals/DEMO-1/keys", json={"seq": [{"text": "x"}]}, headers=origin).status_code == 404
    assert c.post("/terminals/DEMO-1/size", json={"cols": 80, "rows": 24}, headers=origin).status_code == 404
    assert c.post("/terminals/new", headers=origin).status_code == 404
    c.post("/terminals/DEMO-1/end", headers=origin, follow_redirects=False)
    tid = put("open")
    r = c.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude", "where": "tmux"},
               headers=origin, follow_redirects=False)
    assert "only+on+this+machine" in r.headers["location"]
    assert not launched
    assert not [x for x in fake.calls if x[0] in ("send-keys", "resize-window", "kill-session", "capture-pane")]


# ---- polling: one tmux call per tick, back off while nothing changes ------------------------------------------------

def test_capture_many_is_one_tmux_call_and_a_screen_cannot_pose_as_another(ws, fake):
    fake.screen = "\x1e0000 other 1 1\nfake"  # looks like a header, but without this call's random marker
    fake.sessions.append(("third", str(ws.root)))
    screens = terminals.capture_many(["DEMO-1", "scratch", "third"])
    assert len(fake.calls) == 1 and set(screens) == {"DEMO-1", "scratch", "third"}
    assert all(v["cols"] == 200 and "fake" in v["html"] for v in screens.values())
    assert screens["DEMO-1"] == terminals.capture("DEMO-1")


def test_capture_many_falls_back_for_sessions_after_one_that_ended(ws, fake):
    screens = terminals.capture_many(["DEMO-1", "gone", "scratch"])
    assert screens["gone"] is None and screens["DEMO-1"] and screens["scratch"]


def test_the_view_backs_off_while_nothing_changes_and_a_key_wakes_it(ws, fake, monkeypatch):
    import asyncio
    from orch.dashboard import routes_terminals
    waits = []

    async def nap(name, seconds):
        waits.append(seconds)
        return len(waits) == 6  # a key POST arrives during the sixth nap

    with monkeypatch.context() as m:
        m.setattr(routes_terminals, "_nap", nap)
        _stream(ws, "DEMO-1", rounds=8)
    assert waits[:6] == [0.2, 0.4, 0.8, 1.6, 2.0, 2.0] and waits[6] == 0.4  # woken: back to the quick tick

    async def woken():
        task = asyncio.create_task(routes_terminals._nap("DEMO-1", 30))
        await asyncio.sleep(0)
        routes_terminals._wake("DEMO-1")
        return await task

    assert asyncio.run(woken()) is True and routes_terminals._WAKE == {}


def test_the_grid_backs_off_to_a_few_seconds_with_one_capture_call_per_tick(ws, fake, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from orch.dashboard import routes_terminals
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def run():
        seen = {"n": 0}

        async def is_disconnected():
            seen["n"] += 1
            return seen["n"] > 6

        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ws=ws)), is_disconnected=is_disconnected,
                              client=SimpleNamespace(host="127.0.0.1"), headers={"host": "127.0.0.1:8765"})
        resp = await routes_terminals.grid_stream(req)
        return [chunk async for chunk in resp.body_iterator]

    asyncio.run(run())
    assert waits == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    batches = [c for c in fake.calls if c[0] == "display-message"]
    assert len(batches) == 6 and not [c for c in fake.calls if c[0] == "capture-pane"]


def test_open_in_mission_control_is_not_offered_to_a_non_local_browser(ws, put, fake):
    pytest.importorskip("fastapi")
    tid = put("open")
    assert "Open in Mission Control" in _client(ws).get(f"/t/{tid}").text
    remote = _client(ws, base_url="http://192.168.1.20:8765", client=("192.168.1.30", 50000))
    assert "Open in Mission Control" not in remote.get(f"/t/{tid}").text
    assert "Open in Mission Control" not in remote.get(f"/t/{tid}/agent/panel").text
