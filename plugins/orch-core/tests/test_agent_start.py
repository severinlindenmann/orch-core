import os
import stat
import time

import pytest


def test_build_command_quotes_and_validates(ws):
    from orch.dashboard.data.agent_start import build
    prompt, argv, shown = build(ws, "DEMO-0044", "work", "claude")
    assert argv == ["claude", "Work on ticket DEMO-0044 with the orch-work-on-ticket skill."]
    assert shown.startswith("cd ") and "&& claude 'Work on ticket DEMO-0044" in shown


@pytest.mark.parametrize("key,harness", [("DEMO-1; rm -rf ~", "claude"), ("demo-1", "claude"), ("DEMO-1", "bash")])
def test_start_agent_rejects_unsafe_key_and_harness(ws, key, harness):
    from orch.dashboard.data.agent_start import build
    from orch.errors import ValidationError
    with pytest.raises(ValidationError):
        build(ws, key, "work", harness)


def test_copilot_uses_interactive_flag(ws):
    from orch.dashboard.data.agent_start import build
    assert build(ws, "DEMO-1", "refine", "copilot")[1][:2] == ["copilot", "-i"]


@pytest.mark.parametrize("env,expected", [
    ({"CMUX_SOCKET_PATH": "/tmp/x"}, "cmux"), ({"TERM_PROGRAM": "iTerm.app"}, "iterm"),
    ({"TERM_PROGRAM": "ghostty"}, "ghostty"), ({}, "terminal")])
def test_choose_terminal(env, expected):
    from orch.dashboard.launch import choose
    assert choose(env, "auto", "darwin") == expected


def test_cmux_argv_has_no_shell():
    from orch.dashboard.launch import argv_for
    a = argv_for("cmux", cwd="/w", command_argv=["claude", "x y"], name="DEMO-1", script_path=None, custom=[])
    assert a[:2] == ["cmux", "new-workspace"] and "--command" in a and a[a.index("--command") + 1] == "claude 'x y'"


def test_route_spawns_launcher(dash, ws, put, monkeypatch):
    from orch.dashboard import launch
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, follow_redirects=False)
    assert r.status_code == 303 and "Opened" in r.headers["location"] and calls and calls[0][:3] == ["open", "-a", "Terminal"]
    script = calls[0][3]
    text = open(script).read()
    assert text.startswith("#!/bin/sh\ncd ") and "claude" in text


def test_disabled_while_human_has_the_move(dash, put):
    tid = put("testing", sections={"Verification": "ok"})
    html = dash.get(f"/t/{tid}").text
    assert "Waiting for your verdict" in html and "Open in terminal" in html


def _launch_json(data):
    """Write the user-level launch.json (in the test's temp config home)."""
    import json
    from orch.dashboard import launch
    path = launch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_terminal_none_hides_button(dash, put):
    # Fix round 1: the terminal is a per-user setting (launch.json), not dashboard.terminal.
    _launch_json({"terminal": "none"})
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert "Open in terminal" not in html and "Copy prompt" in html


# ---------- beyond the brief ----------

def _ticket(ws, tid):
    from orch.core import store
    return store.load(ws, tid)[1]


def _suggest(ws, tid, now=None):
    from orch.core import query
    from orch.dashboard.data.agent_start import suggest
    from orch.dashboard.data.agents import agent_rows
    return suggest(ws, _ticket(ws, tid), needs_items=query.needs_you(ws), rows=agent_rows(ws, now=now), now=now)


def test_suggest_modes_and_done(ws, put):
    assert _suggest(ws, put("backlog"))["mode"] == "refine"
    s = _suggest(ws, put("open"))
    assert s["mode"] == "work" and s["disabled"] is None and s["harness"] == "claude"
    assert s["prompt"] == "Work on ticket " + s["key"] + " with the orch-work-on-ticket skill."
    assert s["command"].startswith("cd ")
    assert _suggest(ws, put("done")) is None


def test_suggest_waits_for_requirements_approval(ws, put):
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    assert _suggest(ws, tid)["disabled"] == "Waiting for your requirements approval"


def test_suggest_waits_for_answer(ws, put):
    q = {"id": "Q1", "text": "Which?", "type": "text", "blocking": True, "answer": None}
    tid = put("waiting", questions=[q])
    assert _suggest(ws, tid)["disabled"] == "Waiting for your answer"


def test_suggest_continue_after_changes_requested(ws, put, hops):
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    hops.request_changes(tid, "requirements", "more detail")
    s = _suggest(ws, tid)
    assert s["mode"] == "continue" and s["disabled"] is None


def test_suggest_continue_after_answer(ws, put, hops):
    q = {"id": "Q1", "text": "Which?", "type": "text", "blocking": True, "answer": None}
    tid = put("waiting", questions=[q])
    hops.answer(tid, "Q1", "this one")
    s = _suggest(ws, tid)
    assert s["mode"] == "continue" and s["disabled"] is None


def test_fresh_claim_disables_stale_claim_warns(ws, put, aops):
    from datetime import timedelta
    from orch.clock import now as clock_now
    tid = put("open")
    aops.claim(tid)
    s = _suggest(ws, tid)
    assert s["disabled"] == "claude-code is working on it"
    later = clock_now() + timedelta(hours=3)
    s = _suggest(ws, tid, now=later)
    assert s["disabled"] is None and s["warning"] == "Starting releases the stale claim first"


def test_route_releases_stale_claim_first(dash, ws, put, aops, monkeypatch):
    from orch.dashboard import launch
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    tid = put("open")
    aops.claim(tid)
    from datetime import timedelta
    import orch.dashboard.routes_agent_start as ras
    from orch.clock import now as clock_now
    monkeypatch.setattr(ras, "clock_now", lambda: clock_now() + timedelta(hours=3))
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "codex"}, follow_redirects=False)
    assert r.status_code == 303 and "Opened+codex+in+Ghostty" in r.headers["location"], r.headers["location"]
    assert calls[0][:5] == ["open", "-na", "Ghostty", "--args", "-e"]
    assert not (_ticket(ws, tid).meta.get("claim") or {}).get("session")


def test_route_refuses_fresh_claim(dash, ws, put, aops):
    tid = put("open")
    aops.claim(tid)
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"] and "working+on+it" in r.headers["location"]


def test_route_rejects_unknown_harness_without_spawning(dash, put):
    # the autouse fixture makes any real Popen fail the test
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "bash -c id"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_route_terminal_none_refuses(dash, put):
    _launch_json({"terminal": "none"})
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert "Open+in+terminal+is+turned+off" in r.headers["location"]


def test_route_refuses_cross_origin(dash, put):
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"},
                  headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_route_keeps_safe_next(dash, put, monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: None)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    tid = put("open")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude", "next": "/today"},
                  follow_redirects=False)
    assert r.headers["location"].startswith("/today?msg=Opened")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude", "next": "//evil"},
                  follow_redirects=False)
    assert r.headers["location"].startswith(f"/t/{tid}?")


def test_script_is_private_and_quoted(ws, monkeypatch):
    from orch.dashboard import launch
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    msg = launch.start(ws, "L-0001", ["claude", "it's here"], terminal="iterm", name="L-0001", harness="claude")
    argv, kw = calls[0]
    assert msg == "Opened claude in iTerm for L-0001"
    assert argv[:3] == ["open", "-a", "iTerm"] and kw.get("start_new_session") is True
    script = argv[3]
    assert script.startswith(str(ws.state_dir / "run")) and script.endswith(".command")
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o700
    text = open(script).read()
    assert text == f"#!/bin/sh\ncd '{ws.root}' && exec claude 'it'\"'\"'s here'\n"


def test_cmux_start_writes_no_script(ws, monkeypatch):
    from orch.dashboard import launch
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    launch.start(ws, "L-0001", ["claude", "x"], terminal="cmux", name="L-0001")
    assert calls[0] == ["cmux", "new-workspace", "--name", "L-0001", "--cwd", str(ws.root),
                        "--command", "claude x", "--focus", "true"]
    assert not (ws.state_dir / "run").exists() or not list((ws.state_dir / "run").iterdir())


def test_other_launchers_and_custom():
    from orch.dashboard.launch import argv_for, choose
    kw = dict(cwd="/w", command_argv=["claude", "p"], name="K-1", script_path="/s.command")
    assert argv_for("linux", custom=[], **kw) == ["x-terminal-emulator", "-e", "/s.command"]
    assert argv_for("windows", custom=[], **kw) == ["wt.exe", "-d", "/w", "claude", "p"]
    assert argv_for("custom", custom=["wezterm", "start", "--cwd", "{cwd}", "--", "sh", "{script}", "{name}"], **kw) == \
        ["wezterm", "start", "--cwd", "/w", "--", "sh", "/s.command", "K-1"]
    assert choose({}, "auto", "linux") == "linux" and choose({}, "auto", "win32") == "windows"
    assert choose({"TERM_PROGRAM": "Apple_Terminal"}, "auto", "darwin") == "terminal"
    assert choose({"CMUX_SOCKET_PATH": "x"}, "iterm", "darwin") == "iterm"
    assert choose({}, "none", "darwin") == "none"


def test_fix_checks_needs_valid_pr(ws):
    from orch.dashboard.data.agent_start import build
    from orch.errors import ValidationError
    prompt, argv, _ = build(ws, "L-1", "fix-checks", "claude", pr="https://github.com/a/b/pull/7")
    assert prompt == "Fix the failing checks on https://github.com/a/b/pull/7 for ticket L-1."
    for bad in (None, "x; rm -rf ~", "javascript:alert(1)"):
        with pytest.raises(ValidationError):
            build(ws, "L-1", "fix-checks", "claude", pr=bad)
    with pytest.raises(ValidationError):
        build(ws, "L-1", "deploy", "claude")


def test_defaults_validate_against_schema():
    from orch.config.load import DEFAULTS, deep_merge, validate_schema
    from orch.dashboard import launch
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x"})) == []
    assert "terminal" not in DEFAULTS["dashboard"] and "harnesses" not in DEFAULTS["agents"]
    s = launch.load_settings()
    assert s["terminal"] == "auto" and s["terminal_command"] == [] and s["error"] is None
    assert s["harnesses"]["copilot"] == ["copilot", "-i", "{prompt}"]


def test_tidy_removes_old_run_scripts(ws):
    from orch.core.maintenance import tidy
    run = ws.state_dir / "run"
    run.mkdir(parents=True)
    old, new = run / "L-1-1.command", run / "L-1-2.command"
    old.write_text("x"); new.write_text("y")
    past = time.time() - 2 * 86400
    os.utime(old, (past, past))
    removed = tidy(ws)
    assert old in removed and not old.exists() and new.exists()


def test_launch_is_blocked_by_default(ws):
    from orch.dashboard import launch
    with pytest.raises(AssertionError, match="unexpected launch"):
        launch.start(ws, "L-0001", ["claude", "x"], terminal="cmux", name="L-0001")


def test_ticket_page_shows_start_box(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert f'action="/t/{tid}/agent/start"' in html and "Open in terminal" in html
    assert "Copy command" in html and "with the orch-work-on-ticket skill." in html
    assert 'class="btn btn-primary"' in html


def test_ticket_text_never_enters_prompt(dash, put):
    tid = put("open", title="$(touch /tmp/pwn)", sections={"Requirements": "`rm -rf ~`"})
    html = dash.get(f"/t/{tid}").text
    box = html.split(f'id="start-agent-{tid}"', 1)[1].split("</section>", 1)[0]
    assert "pwn" not in box and "rm -rf" not in box


# ---------- fix round 1: launch settings are per user only ----------

def test_workspace_config_cannot_choose_the_launcher(ws, put, configure, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import launch
    from orch.dashboard.app import create_app
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    w = configure(dashboard={"terminal": "custom", "terminal_command": ["sh", "-c", "touch /tmp/pwn"]},
                  agents={"harnesses": {"claude": ["sh", "-c", "touch /tmp/pwn"], "evil": ["sh"]}})
    c = TestClient(create_app(w, "tok")); c.get("/?token=tok")
    tid = put("open")
    html = c.get(f"/t/{tid}").text
    assert "pwn" not in html and "Terminal: Terminal" in html and 'value="evil"' not in html
    r = c.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert "Opened" in r.headers["location"] and calls[0][:3] == ["open", "-a", "Terminal"]
    assert "touch" not in open(calls[0][3]).read()
    ws_html = c.get("/workspace").text
    assert "terminal settings in orchestrator/config.json are ignored" in ws_html


def test_user_custom_launcher_is_shown_in_the_box(dash, ws, put, monkeypatch):
    from orch.dashboard import launch
    _launch_json({"terminal": "custom", "terminal_command": ["wezterm", "start", "--cwd", "{cwd}", "--", "sh", "{script}"],
                  "harnesses": {"claude": ["claude", "--model", "opus", "{prompt}"]}})
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert f"Terminal: custom launcher · set in {launch.config_path()}" in html
    assert "Launcher preview" in html and "wezterm start --cwd" in html and "--model opus" in html
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert "Opened" in r.headers["location"]
    assert calls[0][:5] == ["wezterm", "start", "--cwd", str(ws.root), "--"] and calls[0][6].endswith(".command")


@pytest.mark.parametrize("content", ["{not json", "[1]", '{"terminal": "xterm"}', '{"harnesses": {"x": "sh -c id"}}',
                                     '{"surprise": 1}'])
def test_broken_launch_json_falls_back_with_warning(dash, put, content, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")  # "auto" -> Terminal on every CI platform
    _launch_json(content)
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert "Open in terminal" in html and "Terminal: Terminal" in html  # the defaults
    ws_html = dash.get("/workspace").text
    assert "launch.json is ignored" in ws_html and "using the defaults" in ws_html


def test_workspace_may_pick_known_default_harness_and_prompts(ws, put, configure):
    from orch.dashboard.data.agent_start import build, default_harness
    w = configure(agents={"default_harness": "codex", "prompts": {"work": "Do {key} now."}})
    assert default_harness(w) == "codex"
    assert build(w, "L-1", "work", "codex")[1] == ["codex", "Do L-1 now."]
    assert default_harness(configure(agents={"default_harness": "evil"})) == "claude"


def test_key_and_pr_with_trailing_newline_are_rejected(ws):
    from orch.dashboard.data.agent_start import build
    from orch.errors import ValidationError
    with pytest.raises(ValidationError):
        build(ws, "L-1\n", "work", "claude")
    with pytest.raises(ValidationError):
        build(ws, "L-1", "fix-checks", "claude", pr="https://github.com/a/b/pull/7\n")


def test_missing_launcher_binary_keeps_the_stale_claim(dash, ws, put, aops, monkeypatch):
    from datetime import timedelta
    import orch.dashboard.routes_agent_start as ras
    from orch.clock import now as clock_now
    from orch.dashboard import launch
    monkeypatch.setattr(launch, "which", lambda name: None)
    monkeypatch.setattr(ras, "clock_now", lambda: clock_now() + timedelta(hours=3))
    _launch_json({"terminal": "cmux"})
    tid = put("open")
    aops.claim(tid)
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert "cmux+was+not+found" in r.headers["location"]
    assert (_ticket(ws, tid).meta.get("claim") or {}).get("session")


def test_custom_quoted_placeholders():
    from orch.dashboard.launch import argv_for
    a = argv_for("custom", cwd="/my w", command_argv=["claude", "p"], name="K-1", script_path="/s s.command",
                 custom=["sh", "-c", "cd {cwd_q} && {command}", "{script_q}", "{name_q}", "{cwd}"])
    assert a == ["sh", "-c", "cd '/my w' && claude p", "'/s s.command'", "K-1", "/my w"]


def test_run_dir_is_private(ws, monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: None)
    launch.start(ws, "L-0001", ["claude", "x"], terminal="terminal", name="L-0001")
    assert stat.S_IMODE(os.stat(ws.state_dir / "run").st_mode) == 0o700


def test_box_id_is_per_ticket(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert f'id="start-agent-{tid}"' in html  # story page: the panel sits right under the status card
    assert html.count('class="start-agent"') == 1


# ---------- fix round 2: broken launch.json never crashes; prompts never become flags ----------

@pytest.mark.parametrize("content", [
    b'{"default_harness": []}', b'{"default_harness": 5}', b'\xff\xfe{"terminal": "none"}',
    b'{"terminal": []}', b'{"terminal": {"a": 1}}', b'{"terminal_command": "sh"}',
    b'{"harnesses": {"claude": ["claude"]}}', b'{"harnesses": {"claude": "claude {prompt}"}}',
    b'{"harnesses": []}', b'null', b'"text"'])
def test_odd_launch_json_gives_defaults_and_a_warning(dash, put, content, monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    path = launch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    s = launch.load_settings()
    assert s["error"] and s["terminal"] == "auto" and s["harnesses"] == launch.DEFAULT_HARNESSES
    tid = put("open")
    r = dash.get(f"/t/{tid}")
    assert r.status_code == 200 and "Terminal: Terminal" in r.text
    w = dash.get("/workspace")
    assert w.status_code == 200 and "launch.json is ignored" in w.text


def test_load_settings_never_raises(monkeypatch):
    from orch.dashboard import launch
    monkeypatch.setattr(launch, "_load_settings", lambda path: 1 / 0)
    s = launch.load_settings()
    assert s["terminal"] == "auto" and "ZeroDivisionError" in s["error"]


@pytest.mark.parametrize("template", ["--dangerously-skip-permissions", "  -p {key}", "Work on it.",
                                      "Work on {key}\x00", "Work on {key}\x1b[2J", 42])
def test_bad_workspace_prompt_falls_back_to_default(ws, configure, template):
    from orch.dashboard.data.agent_start import build
    w = configure(agents={"prompts": {"work": template}})
    prompt, argv, _ = build(w, "L-1", "work", "claude")
    assert prompt == "Work on ticket L-1 with the orch-work-on-ticket skill." and argv[1] == prompt


def test_good_workspace_prompt_with_newline_is_used(ws, configure):
    from orch.dashboard.data.agent_start import build
    w = configure(agents={"prompts": {"refine": "Refine {key}.\nBe brief."}})
    assert build(w, "L-1", "refine", "claude")[0] == "Refine L-1.\nBe brief."


def test_bad_prompt_warning_on_workspace_page(ws, configure):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    w = configure(agents={"prompts": {"work": "--dangerously-skip-permissions", "continue": "Go on."}})
    c = TestClient(create_app(w, "tok")); c.get("/?token=tok")
    html = c.get("/workspace").text
    assert "agents.prompts.work in orchestrator/config.json is ignored" in html
    assert "agents.prompts.continue in orchestrator/config.json is ignored" in html
    assert "agents.prompts.refine" not in html


def test_prompt_argument_never_starts_with_dash(ws, configure, put):
    from orch.core import query
    from orch.dashboard.data.agent_start import MODES, build, harnesses
    w = configure(agents={"prompts": {m: "-x {key}" for m in MODES}})
    for h, template in harnesses(w).items():
        i = template.index("{prompt}")
        for m in MODES:
            argv = build(w, "L-1", m, h, pr="https://github.com/a/b/pull/7")[1]
            assert not argv[i].startswith("-"), (h, m, argv)
            if h == "claude":
                assert not argv[1].startswith("-")


# ---------- final review ----------

def test_start_agent_footer_shows_the_real_launch_json_path(dash, put):
    from orch.dashboard import launch
    html = dash.get(f"/t/{put('open')}").text
    assert f"set in {launch.config_path()}</span>" in html and "~/.config/orch/launch.json" not in html


def test_continue_mode_label_matches_spec():
    from orch.dashboard.data.agent_start import MODE_LABELS
    assert MODE_LABELS["continue"] == "Continue after feedback"


def test_disabled_reason_for_changed_requirements_is_the_in_place_reapproval(ws, put):
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("open", sections={"Requirements": "- r", "Acceptance criteria": "- a"})
    _, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-09-30T09:00Z", "via": "dashboard",
                                       "hash": gate_hash(t, "requirements")}
    t.set_section("Requirements", "- r changed")
    store.save(ws, t)
    reason = _suggest(ws, tid)["disabled"]
    assert reason == "Waiting for your requirements approval"  # re-approved in place (#208): no move back to backlog


def test_an_epic_is_never_offered_work_on_ticket(ws, aops, hops):
    """Final review M5: an epic is not worked (its children are): the Start agent panel offers refining it, never
    "Work on ticket", in backlog and once approved."""
    e = aops.new("Billing", type="epic")
    aops.set_section(e.id, "Requirements", "the epic")
    aops.set_section(e.id, "Acceptance criteria", "- [ ] all done")
    for round_ in range(2):
        if round_:
            hops.approve(e.id, "requirements")
        s = _suggest(ws, e.id)
        values = [m["value"] for m in s["modes"]]
        assert "work" not in values and "fix-checks" not in values and s["mode"] == "refine", values
        assert {m["value"]: m["label"] for m in s["modes"]}["refine"] == "Refine epic"
        assert all(o["mode"] != "work" for o in s["options"])


def test_the_epic_stepper_has_no_plan_step(ws, aops):
    from orch.dashboard.data.steps import steps
    e = aops.new("Billing", type="epic")
    assert "Plan" not in [s["name"] for s in steps(_ticket(ws, e.id), plan_skip_sizes=())]
    t = aops.new("Child", epic=e.id)
    assert "Plan" in [s["name"] for s in steps(_ticket(ws, t.id), plan_skip_sizes=())]


def test_route_refuses_work_on_an_epic(dash, ws, aops, monkeypatch):
    from orch.dashboard import launch
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    e = aops.new("Billing", type="epic")
    r = dash.post(f"/t/{e.id}/agent/start", data={"mode": "work", "harness": "claude"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "not+offered" in r.headers["location"] and not calls
