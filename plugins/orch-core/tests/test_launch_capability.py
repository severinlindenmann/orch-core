"""The `launch` addon capability (API 2.7, issue #47): an enabled addon may add a model argument, process-only
environment variables and a prompt sentence to a Start agent preview and launch. With no such addon nothing changes."""
import pytest

from addon_fixtures import loaded
from orch.addons import launching
from orch.addons.api import LaunchPlan, LaunchRequest
from orch.addons.loader import AddonRegistry
from orch.addons.manifest import parse_manifest
from orch.core import store
from orch.dashboard import launch
from orch.dashboard.data import agent_start
from orch.errors import UsageError, ValidationError

ORIGIN = {"origin": "http://testserver"}
OVER = {"capabilities": ["launch"], "slots": [], "menu": None, "settings_schema": []}
KEY = "DEMO-0044"


class Router:
    """A launch addon that records what core asked and returns what the test set."""

    def __init__(self, plan=None, error=None):
        self.plan, self.error = plan, error
        self.asked, self.launched_with = [], []

    def launch(self, req, ctx):
        self.asked.append(req)
        if self.error:
            raise self.error
        return self.plan

    def launched(self, req, routing, ctx, session):
        self.launched_with.append((req, routing, session))


@pytest.fixture
def router(ws):
    obj = Router(LaunchPlan(model="opus", env={"ORCH_MODEL": "opus", "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
                            label="opus (Strong)", reason="refine runs on strong: Strong (opus)"))
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, obj, name="router", **OVER)})
    return obj


def _routing(ws, mode="work", harness="claude", strict=True):
    return launching.resolve(ws, LaunchRequest(KEY, mode, harness), strict=strict)


# -- off: nothing changes ---------------------------------------------------------------------------------------------

def test_no_launch_addon_means_no_routing_and_the_same_argv(ws):
    assert launching.active(ws) is False and _routing(ws) is None
    prompt, argv, shown = agent_start.build(ws, KEY, "work", "claude")
    assert argv == ["claude", prompt] and shown.endswith(f"&& claude '{prompt}'")
    assert agent_start.build(ws, KEY, "work", "claude", routing=None) == (prompt, argv, shown)


def test_an_addon_that_plans_nothing_leaves_the_argv_alone(ws):
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, Router(None), name="router", **OVER)})
    assert _routing(ws) is None
    assert agent_start.build(ws, KEY, "work", "claude", routing=_routing(ws))[1] == \
        agent_start.build(ws, KEY, "work", "claude")[1]


def test_a_plan_with_nothing_in_it_changes_nothing(ws):
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, Router(LaunchPlan(reason="no tier set")), name="router", **OVER)})
    r = _routing(ws)
    assert r.reason == "no tier set" and not r.active
    assert agent_start.build(ws, KEY, "work", "claude", routing=r)[1] == agent_start.build(ws, KEY, "work", "claude")[1]


# -- on: the argument group and the process-only environment ---------------------------------------------------------------

def test_model_becomes_an_argument_group_before_the_prompt_and_env_wraps_the_argv(ws, router):
    r = _routing(ws)
    prompt, argv, shown = agent_start.build(ws, KEY, "work", "claude", routing=r)
    plain = agent_start.build(ws, KEY, "work", "claude")[0]
    assert prompt == plain
    assert argv == ["env", "CLAUDE_CODE_SUBAGENT_MODEL=haiku", "ORCH_MODEL=opus", "claude", "--model", "opus", plain]
    assert "CLAUDE_CODE_SUBAGENT_MODEL=haiku" in shown and "--model opus" in shown


def test_the_group_goes_in_front_of_the_prompt_of_a_custom_template(ws, monkeypatch):
    monkeypatch.setattr(agent_start, "harnesses", lambda ws, settings=None: {"claude": ["claude", "-p", "{prompt}"]})
    r = launching.Routing(model="sonnet")
    assert agent_start.build(ws, KEY, "refine", "claude", routing=r)[1][:4] == ["claude", "-p", "--model", "sonnet"]


def test_a_harness_without_a_model_argument_refuses_a_model(ws):
    with pytest.raises(ValidationError, match="no model argument"):
        agent_start.build(ws, KEY, "work", "codex", routing=launching.Routing(model="opus"))
    # env alone is fine for any harness
    assert agent_start.build(ws, KEY, "work", "codex", routing=launching.Routing(env=(("ORCH_X", "1"),)))[1][:2] == \
        ["env", "ORCH_X=1"]


def test_the_note_is_a_paragraph_after_the_prompt(ws):
    r = launching.Routing(note="The previous sessions failed task T3.")
    prompt, argv, _ = agent_start.build(ws, KEY, "work", "claude", routing=r)
    assert prompt.endswith("\n\nThe previous sessions failed task T3.") and argv == ["claude", prompt]


# -- a plan is checked ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"model": "--dangerously-skip-permissions"}, {"model": "opus; rm -rf ~"}, {"model": "a b"}, {"model": ""},
    {"env": {"PATH": "/tmp"}}, {"env": {"LD_PRELOAD": "x"}}, {"env": {"ORCH_STATE_DIR": "/tmp/x"}}, {"env": {"ORCH_HARNESS": "none"}},
    {"env": {"CLAUDE_CODE_SESSION_ID": "x"}}, {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "x"}}, {"env": {"ORCH_MODEL": "a b"}},
    {"env": {"ORCH_MODEL": "$(x)"}}, {"env": []}, {"note": "-x"}, {"note": "a\x07b"}, {"note": "x" * 401},
    {"label": "a\nb"}, {"reason": "x" * 201}, {"warnings": ("a\nb",)}])
def test_a_plan_with_anything_unsafe_is_refused(kw):
    with pytest.raises(ValueError):
        LaunchPlan(**kw)


@pytest.mark.parametrize("model", ["opus", "sonnet", "opusplan", "claude-opus-5-5", "sonnet[1m]", "us.anthropic.x:0"])
def test_model_names_and_aliases_pass(model):
    assert LaunchPlan(model=model).model == model


# -- the resolver ---------------------------------------------------------------------------------------------------

def test_resolve_merges_addons_first_by_name_wins(ws):
    a = Router(LaunchPlan(model="opus", env={"ORCH_MODEL": "opus"}, note="one", reason="a"))
    b = Router(LaunchPlan(model="haiku", env={"ORCH_MODEL": "haiku", "ORCH_B": "1"}, note="two", reason="b",
                          warnings=("w",)))
    ws._addons = AddonRegistry(ws, {"b-addon": loaded(ws, b, name="b-addon", **OVER),
                                    "a-addon": loaded(ws, a, name="a-addon", **OVER)})
    r = _routing(ws)
    assert r.model == "opus" and dict(r.env) == {"ORCH_MODEL": "opus", "ORCH_B": "1"}
    assert r.note == "one two" and r.reason == "a" and r.warnings == ("w",) and r.addons == ("a-addon", "b-addon")


def test_a_failing_addon_refuses_a_launch_but_only_warns_in_a_preview(ws):
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, Router(error=ValueError("Strong model 'x y' is bad")),
                                                     name="router", **OVER)})
    with pytest.raises(UsageError, match="Strong model 'x y' is bad"):
        _routing(ws, strict=True)
    r = _routing(ws, strict=False)
    assert r.warnings and "Strong model 'x y' is bad" in r.warnings[0] and not r.active


def test_a_wrong_return_type_is_a_failure_not_a_launch(ws):
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, Router({"model": "opus"}), name="router", **OVER)})
    with pytest.raises(UsageError):
        _routing(ws, strict=True)


def test_an_addon_without_the_capability_is_never_asked(ws):
    obj = Router(LaunchPlan(model="opus"))
    ws._addons = AddonRegistry(ws, {"router": loaded(ws, obj, name="router", capabilities=["panel"],
                                                     slots=["today.summary"], menu=None, settings_schema=[])})
    assert launching.active(ws) is False and _routing(ws) is None and obj.asked == []


def test_the_manifest_knows_the_capability():
    from orch.addons.manifest import CAPABILITIES
    assert "launch" in CAPABILITIES
    m = parse_manifest({**{"name": "x", "title": "X", "version": "0.1.0", "requires_api": "2", "kind": "in-process",
                           "capabilities": ["launch"], "entry": "x:create"}})
    assert m.has("launch")


# -- the box shows the plan, per mode ----------------------------------------------------------------------------------

def test_suggest_gives_every_option_its_own_plan_and_the_box_shows_it(dash, ws, put, router):
    tid = put("backlog")
    html = dash.get(f"/t/{tid}").text
    assert "refine runs on strong: Strong (opus)" in html
    assert "--model opus" in html and "CLAUDE_CODE_SUBAGENT_MODEL=haiku" in html
    assert {(r.mode, r.harness) for r in router.asked} >= {("refine", "claude"), ("work", "claude")}
    assert 'data-route="refine runs on strong: Strong (opus)"' in html


def test_the_box_is_unchanged_without_a_launch_addon(dash, put):
    tid = put("backlog")
    html = dash.get(f"/t/{tid}").text
    assert "data-route-text" not in html and "--model" not in html and "env " not in html.split("Command")[1][:300]


def test_a_warning_from_the_plan_is_shown(dash, ws, put, router):
    router.plan = LaunchPlan(reason="r", warnings=("CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set",))
    tid = put("backlog")
    assert "Warning: CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set" in dash.get(f"/t/{tid}").text


# -- the launch ----------------------------------------------------------------------------------------------------

@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    return calls


def test_the_launch_runs_the_command_the_preview_showed(dash, ws, put, router, popen):
    tid = put("backlog")
    ticket = store.load(ws, tid)[1]
    shown = [o for o in agent_start.suggest(ws, ticket, needs_items=[])["options"]
             if o["mode"] == "refine" and o["harness"] == "claude"][0]["command"]
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, headers=ORIGIN,
                  follow_redirects=False)
    assert r.status_code == 303 and "Opened" in r.headers["location"] and "opus" in r.headers["location"]
    script = open(popen[0][3]).read()
    assert script.strip().split("exec ", 1)[1] == shown.split("&& ", 1)[1]
    assert "CLAUDE_CODE_SUBAGENT_MODEL=haiku" in script and "--model opus" in script
    (req, routing, session), = router.launched_with
    assert (req.ticket, req.mode, req.harness, session) == (tid, "refine", "claude", tid)
    assert launch.launch_notes(ws) == {tid: "opus (Strong)"}


def test_a_launch_without_an_addon_is_what_it_was(dash, ws, put, popen):
    tid = put("backlog")
    dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, headers=ORIGIN,
              follow_redirects=False)
    assert " env " not in open(popen[0][3]).read() and launch.launch_notes(ws) == {}


def test_a_failing_addon_stops_the_launch_with_its_sentence(dash, ws, put, router, popen):
    router.error = ValueError("Strong model 'x y' is not a model name")
    tid = put("backlog")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, headers=ORIGIN,
                  follow_redirects=False)
    assert r.status_code == 303 and "not+a+model+name" in r.headers["location"] and popen == []
    assert router.launched_with == []


def test_windows_terminal_cannot_carry_env_and_says_so(dash, ws, put, router, popen, monkeypatch):
    monkeypatch.setattr(launch, "load_settings", lambda: {**launch._defaults(), "terminal": "windows", "factory_command": []})
    tid = put("backlog")
    r = dash.post(f"/t/{tid}/agent/start", data={"mode": "refine", "harness": "claude"}, headers=ORIGIN,
                  follow_redirects=False)
    assert "Windows+Terminal+cannot+pass+environment" in r.headers["location"] and popen == []


# -- tmux: a session that ends at once ---------------------------------------------------------------------------------

def _start_tmux(ws, monkeypatch, alive, watch="opus"):
    class P:
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: P())
    monkeypatch.setattr(launch, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(launch, "_still_running", lambda name: alive)
    slept = []
    monkeypatch.setattr(launch.time, "sleep", slept.append)
    out = launch.start(ws, KEY, ["claude", "x"], terminal="tmux", name=KEY, harness="claude", settings={}, watch=watch)
    return out, slept


def test_a_routed_tmux_session_that_ends_at_once_is_a_failed_start(ws, monkeypatch):
    with pytest.raises(UsageError, match="ended right after it started on model opus"):
        _start_tmux(ws, monkeypatch, alive=False)


def test_a_routed_tmux_session_that_stays_is_a_good_start(ws, monkeypatch):
    out, slept = _start_tmux(ws, monkeypatch, alive=True)
    assert out.startswith("Opened claude in Mission Control") and slept == [launch.ALIVE_AFTER]


def test_an_unrouted_tmux_start_does_not_wait(ws, monkeypatch):
    out, slept = _start_tmux(ws, monkeypatch, alive=False, watch=None)
    assert out.startswith("Opened") and slept == []


# -- Terminals shows what the session was started on ---------------------------------------------------------------------

def test_launch_notes_are_kept_per_session_and_capped(ws):
    launch.record_launch(ws, "A-1", "opus (Strong)")
    launch.record_launch(ws, "B-1", "")  # nothing to remember
    for n in range(launch.MAX_LAUNCHES + 5):
        launch.record_launch(ws, f"S-{n}", "haiku")
    notes = launch.launch_notes(ws)
    assert len(notes) == launch.MAX_LAUNCHES and "A-1" not in notes and "S-204" in notes and "B-1" not in notes


def test_launch_notes_survive_a_broken_file(ws):
    p = ws.state_dir / "run" / launch.LAUNCHES
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{nope", encoding="utf-8")
    assert launch.launch_notes(ws) == {}
