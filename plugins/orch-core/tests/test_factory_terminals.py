"""The AI Factory's sessions on the Terminals page and in the run view (docs/factory.md, "Watching the sessions"):
listed only from the runner's own bindings of this workspace, screens and keys on the runner's own tmux socket (never
orch's), no end from the page (the runner owns them), and a read-only peek in the run view while Terminals is off.
Fake tmux servers stand in for both sockets."""
import pytest

from orch.core import factory_sessions
from orch.dashboard import factory_runner, terminals
from test_factory_release import _not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote  # noqa: F401
from test_terminals import LOCAL, FakeTmux, _client

pytest.importorskip("fastapi")
EVIL = '<script>alert("x")</script> \x1b[31mred\x1b[0m'


@pytest.fixture
def two(monkeypatch, ws):
    """orch's own tmux (one scratch session of this workspace) and the factory runner's tmux (three sessions: two the
    runner bound in this workspace, one it did not), Terminals on."""
    orch = FakeTmux(sessions=[("scratch", str(ws.root))])
    fact = FakeTmux(sessions=[("fx-l-0002-a1", "/clones/a"), ("fx-l-0003-b2", "/clones/b"), ("fx-l-0009-zz", "/x")],
                    screen=EVIL)
    monkeypatch.setattr(terminals, "tmux", orch)
    monkeypatch.setattr(factory_runner, "_tmux", fact)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)
    binds = [{"name": "fx-l-0002-a1", "epic": "L-0001", "child": "L-0002", "session": "s1", "delegation": "d"},
             {"name": "fx-l-0003-b2", "epic": "L-0001", "child": "L-0003", "session": "s2", "delegation": "d"},
             {"name": "bad name;kill-server", "epic": "L-0001", "child": "L-0004", "session": "s3", "delegation": "d"}]
    monkeypatch.setattr(factory_sessions, "bindings", lambda w: binds)
    monkeypatch.setattr(factory_sessions, "is_planner", lambda b: False)
    factory_runner.HUMAN_KEYS.clear()
    return orch, fact


def test_the_grid_lists_factory_sessions_from_the_bindings_only_by_epic(ws, two):
    orch, fact = two
    html = _client(ws).get("/terminals").text
    assert 'id="factory-L-0001"' in html and "Factory · " in html
    assert 'data-factory-tile="fx-l-0002-a1"' in html and 'data-factory-tile="fx-l-0003-b2"' in html
    assert "fx-l-0009-zz" not in html  # on the runner's server, but not one of its bindings of this workspace
    assert "bad name" not in html  # a binding whose name the Terminals' rule refuses is never shown or asked for
    assert html.count("data-runner-owned") == 2 and 'href="/factory/L-0001"' in html
    assert "&lt;script&gt;" in html and "<script>alert" not in html  # the screen is escaped
    assert not any(c[0] == "list-sessions" for c in fact.calls)  # the factory server is never listed
    assert all("bad name;kill-server" not in " ".join(c) for c in fact.calls + orch.calls)


def test_a_factory_session_view_is_runner_owned_and_offers_no_end(ws, two):
    c = _client(ws)
    html = c.get("/factory-sessions/fx-l-0002-a1").text
    assert "data-runner-owned" in html and 'data-base="/factory-sessions/fx-l-0002-a1"' in html
    assert "End session" not in html and "/end" not in html and 'href="/factory/L-0001"' in html
    assert c.get("/factory-sessions/fx-l-0009-zz").status_code == 404  # not one of this workspace's bindings
    assert c.get("/factory-sessions/bad%20name").status_code == 404
    assert c.post("/factory-sessions/fx-l-0002-a1/end", headers={"origin": LOCAL}).status_code in (404, 405)


def test_keys_reach_the_factory_socket_and_quiet_the_nudge(ws, two):
    orch, fact = two
    c = _client(ws)
    r = c.post("/factory-sessions/fx-l-0002-a1/keys", json={"seq": [{"text": "1"}, {"key": "Enter"}]},
               headers={"origin": LOCAL})
    assert r.status_code == 204
    sent = [x for x in fact.calls if x[0] == "send-keys"]
    assert sent == [["send-keys", "-t", "=fx-l-0002-a1:", "-l", "--", "1"], ["send-keys", "-t", "=fx-l-0002-a1:",
                                                                           "Enter"]]
    assert not any(x[0] == "send-keys" for x in orch.calls)  # never orch's own server
    assert factory_runner.human_typed("fx-l-0002-a1") and not factory_runner.human_typed("fx-l-0003-b2")
    assert c.post("/factory-sessions/fx-l-0009-zz/keys", json={"seq": [{"text": "x"}]},
                  headers={"origin": LOCAL}).status_code == 404
    assert c.post("/factory-sessions/fx-l-0002-a1/keys", json={"seq": [{"text": "x"}]}).status_code == 403
    # orch's own end route never reaches a factory session either
    c.post("/terminals/fx-l-0002-a1/end", headers={"origin": LOCAL})
    assert not any(x[0] == "kill-session" for x in fact.calls + orch.calls)


def test_everything_is_off_with_the_addon_off_or_from_another_machine(ws, two, monkeypatch):
    c = _client(ws, base_url="http://192.168.1.5:8765", client=("192.168.1.9", 5000))
    assert c.get("/factory-sessions/fx-l-0002-a1").status_code == 404
    assert c.post("/factory-sessions/fx-l-0002-a1/keys", json={"seq": [{"text": "x"}]},
                  headers={"origin": "http://192.168.1.5:8765"}).status_code in (403, 404)
    monkeypatch.setattr(terminals, "addon_on", lambda ws: False)
    c = _client(ws)
    assert c.get("/factory-sessions/fx-l-0002-a1").status_code == 404
    assert "data-factory-tile" not in c.get("/terminals").text


def test_the_human_quiet_window_ends(monkeypatch):
    import time
    factory_runner.HUMAN_KEYS.clear()
    factory_runner.note_human_keys("fx-a")
    assert factory_runner.TmuxLauncher().human_typed("fx-a")
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + factory_runner.HUMAN_QUIET + 1)
    assert not factory_runner.human_typed("fx-a")


def test_the_run_view_links_to_the_sessions_or_peeks_while_terminals_is_off(ws, two, monkeypatch):
    from types import SimpleNamespace
    from orch.dashboard import routes_factory
    monkeypatch.setattr(factory_runner.TmuxLauncher, "capture", lambda self, name: f"line one\n{EVIL}\n")
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"host": "127.0.0.1:8765"})
    w = routes_factory._watch(req, ws, "L-0001")
    assert w == {"on": True, "n": 2, "peek": []}
    monkeypatch.setattr(terminals, "addon_on", lambda ws: False)
    w = routes_factory._watch(req, ws, "L-0001")
    assert not w["on"] and [p["name"] for p in w["peek"]] == ["fx-l-0002-a1", "fx-l-0003-b2"]
    assert "\x1b" not in w["peek"][0]["tail"] and "line one" in w["peek"][0]["tail"]  # escaped, never raw
    assert routes_factory._watch(req, ws, "L-0099") is None  # no session of that epic


def test_the_run_view_page_shows_the_link_or_the_escaped_peek(monkeypatch, fws, ready):
    eid, (c,), _ = ready(release="none")
    monkeypatch.setattr(factory_sessions, "bindings", lambda w: [
        {"name": f"fx-{c.lower()}-a1", "epic": eid, "child": c, "session": "s1", "delegation": "d"}])
    monkeypatch.setattr(factory_sessions, "is_planner", lambda b: False)
    monkeypatch.setattr(factory_runner.TmuxLauncher, "capture", lambda self, name: EVIL)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda ws: False)
    html = _client(fws).get(f"/factory/{eid}").text
    assert "data-peek-note" in html and f'data-peek="fx-{c.lower()}-a1"' in html and "Workspace &amp; addons" in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)
    html = _client(fws).get(f"/factory/{eid}").text
    assert f'href="/terminals#factory-{eid}"' in html and "Watch the sessions" in html and "data-peek=" not in html



@pytest.mark.parametrize("cmd", ["tmux -S {base}/permits/tmux/0123456789abcdef/factory send-keys -t fx-a y Enter",
                                 "tmux -S {base}/permits/tmux/0123456789abcdef/factory kill-server",
                                 "tmux -L orch send-keys -t scratch y", "cat {base}/permits/tmux.name"])
def test_the_guard_keeps_agents_off_both_tmux_servers(ws, cmd):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=base_dir())},
                      "cwd": str(ws.root)})
    assert not d.allow, cmd


# -- the review's gaps: resize, Enter after a human key, non-local peek, an untrusted tmux, a stale binding ------------

def test_a_resize_from_the_browser_is_clamped_and_counts_as_the_human(ws, two):
    orch, fact = two
    r = _client(ws).post("/factory-sessions/fx-l-0002-a1/size", json={"cols": 30, "rows": 8},
                         headers={"origin": LOCAL})
    assert r.status_code == 204
    (call,) = [x for x in fact.calls if x[0] == "resize-window"]
    assert call[-4:] == ["-x", "80", "-y", "24"] and factory_runner.human_typed("fx-l-0002-a1")
    from pathlib import Path
    js = (Path(terminals.__file__).parent / "static" / "terminal.js").read_text(encoding="utf-8")
    assert "if (term.dataset.base && !typing()) return;" in js  # watching a factory session never resizes it


@pytest.mark.parametrize("when", ["before Enter", "before C-u"])
def test_the_nudge_never_presses_enter_or_clears_after_a_human_key(monkeypatch, when):
    from orch.core import factory_runner as core
    factory_runner.HUMAN_KEYS.clear()
    sent = []
    nudge = core.NUDGES["answered"]
    screens = {"n": 0}

    def capture(self, name):
        screens["n"] += 1
        if screens["n"] == 1:
            return IDLE_SCREEN
        if when == "before Enter":  # the nudge sits on the input line, and the human types in that very moment
            factory_runner.note_human_keys("fx-a")
            return IDLE_SCREEN.replace("│ >" + " " * 38, "│ > " + nudge)
        if screens["n"] == 1 + factory_runner.TYPE_POLLS:  # the last poll: then the human types, before C-u
            factory_runner.note_human_keys("fx-a")
        return "busy"
    monkeypatch.setattr(factory_runner.TmuxLauncher, "capture", capture)
    monkeypatch.setattr(factory_runner, "_tmux", lambda args, timeout=10: sent.append(args) or
                        __import__("subprocess").CompletedProcess(args, 0, "160" if args[0] == "display-message"
                                                                  else "", ""))
    monkeypatch.setattr(factory_runner, "_sleep", lambda s: None)
    assert factory_runner.TmuxLauncher().type("fx-a", nudge) is False
    assert [a for a in sent if a[-1] in ("Enter", "C-u")] == []  # only the nudge text itself went in


IDLE_SCREEN = ("done.\n\n╭" + "─" * 40 + "╮\n│ >" + " " * 38 + "│\n╰" + "─" * 40 + "╯\n"
               "  ⏵⏵ accept edits on (shift+tab to cycle)\n")


def test_the_run_view_shows_no_screen_to_another_machine(ws, two, monkeypatch):
    from types import SimpleNamespace
    from orch.dashboard import routes_factory
    monkeypatch.setattr(terminals, "addon_on", lambda ws: False)
    monkeypatch.setattr(factory_runner.TmuxLauncher, "capture", lambda self, name: "secret on screen\n")
    far = SimpleNamespace(client=SimpleNamespace(host="192.168.1.9"), headers={"host": "192.168.1.5:8765"})
    assert routes_factory._watch(far, ws, "L-0001") is None


def test_an_untrusted_tmux_or_a_stale_binding_never_breaks_the_pages(ws, two, monkeypatch):
    from orch.errors import UsageError

    def untrusted(args, timeout=10):
        raise UsageError("tmux was not found at a trusted path")
    monkeypatch.setattr(factory_runner, "_tmux", untrusted)
    c = _client(ws)
    assert c.get("/terminals").status_code == 200
    assert c.get("/factory-sessions/fx-l-0002-a1").status_code == 200
    r = c.post("/factory-sessions/fx-l-0002-a1/keys", json={"seq": [{"text": "1"}]}, headers={"origin": LOCAL})
    assert r.status_code == 400
    assert c.post("/factory-sessions/fx-l-0002-a1/size", json={"cols": 100, "rows": 30},
                  headers={"origin": LOCAL}).status_code == 204
    monkeypatch.setattr(factory_runner, "_tmux", FakeTmux(sessions=[]))  # the runner's session is gone, its binding not
    assert c.get("/terminals").status_code == 200 and c.get("/factory-sessions/fx-l-0002-a1").status_code == 200
