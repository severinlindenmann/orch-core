import json
from pathlib import Path

import pytest

import model_routing as M
from orch.addons.api import LaunchRequest, PendingDecision
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.testing import AddonContract, FakeRunner, ProviderContract
from orch.testing.workspace import fake_workspace

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
SETTINGS = {"tier_light": "haiku", "tier_standard": "sonnet", "tier_strong": "opus", "mode_refine": "strong",
            "mode_work": "standard", "mode_fix_checks": "light", "mode_continue": "same", "subagent_model": "haiku"}


class Ctx:
    """What launch() and launched() get (an AddonContext), without a workspace."""

    def __init__(self, tmp_path, settings=None, options=()):
        self.settings = {**MANIFEST.defaults(), **(settings if settings is not None else SETTINGS)}
        self.state_dir = tmp_path / "state"
        self.options = set(options)
        self.relayed = []

    def ticket_option(self, ref, option_id):
        return (ref, option_id) in self.options

    def ops(self):
        outer = self

        class Ops:
            def relay_ticket_option(self, ref, option_id, value):
                outer.relayed.append((ref, option_id, value))
                outer.options.discard((ref, option_id))
        return Ops()


def plan(tmp_path, mode="refine", harness="claude", settings=None, options=(), ticket="D-1"):
    ctx = Ctx(tmp_path, settings, options)
    return M.ModelRouting(ctx).launch(LaunchRequest(ticket, mode, harness), ctx), ctx


def test_manifest_is_off_by_default_and_empty():
    d = MANIFEST.defaults()
    assert all(d[k] == "" for k in ("tier_light", "tier_standard", "tier_strong", "subagent_model"))
    assert [d[k] for k in ("mode_refine", "mode_work", "mode_fix_checks")] == ["none"] * 3 and d["mode_continue"] == "same"
    assert MANIFEST.has("launch") and not MANIFEST.binaries and not MANIFEST.env


def test_a_freshly_enabled_addon_changes_nothing(tmp_path):
    p, _ = plan(tmp_path, settings={})
    assert p.model is None and p.env == {} and p.note == "" and p.label == ""


def test_each_mode_plans_its_alias_and_records_it_in_orch_model(tmp_path):
    for mode, model, tier in (("refine", "opus", "strong"), ("work", "sonnet", "standard"),
                              ("fix-checks", "haiku", "light")):
        p, _ = plan(tmp_path, mode)
        assert p.model == model and p.env["ORCH_MODEL"] == model and p.env["ORCH_MODEL_TIER"] == tier
        assert p.label.startswith(model) and mode in p.reason


def test_the_subagent_model_goes_in_the_environment_of_the_session(tmp_path):
    p, _ = plan(tmp_path)
    assert p.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku" and "subagents on haiku" in p.reason
    p, _ = plan(tmp_path, settings={**SETTINGS, "subagent_model": ""})
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in p.env


def test_only_the_subagent_model_set_still_passes_it(tmp_path):
    p, _ = plan(tmp_path, settings={"subagent_model": "haiku"})
    assert p.model is None and p.env == {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}


def test_other_harnesses_are_left_alone(tmp_path):
    p, _ = plan(tmp_path, harness="codex")
    assert p.model is None and p.env == {} and "Claude Code" in p.reason


def test_a_bad_name_stops_the_launch_with_a_sentence(tmp_path):
    with pytest.raises(ValueError, match="not a model name"):
        plan(tmp_path, settings={**SETTINGS, "tier_strong": "opus --x"})
    with pytest.raises(ValueError, match="Subagent model"):
        plan(tmp_path, settings={**SETTINGS, "subagent_model": "a b"})


@pytest.mark.parametrize("var", ["CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "ANTHROPIC_DEFAULT_OPUS_MODEL"])
def test_the_variables_that_change_what_a_tier_means_warn(tmp_path, monkeypatch, var):
    monkeypatch.setenv(var, "x")
    p, _ = plan(tmp_path)
    assert any(var in w for w in p.warnings)
    monkeypatch.delenv(var)
    assert plan(tmp_path)[0].warnings == ()


def test_strong_next_applies_once_and_is_turned_off_after_the_start(tmp_path):
    p, ctx = plan(tmp_path, "work", options={("D-1", "strong_next")})
    assert p.model == "opus" and "You marked" in p.reason
    # another ticket is not affected
    assert plan(tmp_path, "work", ticket="D-2", options={("D-1", "strong_next")})[0].model == "sonnet"
    obj = M.ModelRouting(ctx)
    routing = type("R", (), {"env": tuple(p.env.items())})()
    obj.launched(LaunchRequest("D-1", "work", "claude"), routing, ctx, "D-1")
    assert ctx.relayed == [("D-1", "strong_next", False)]
    assert plan(tmp_path, "work", options=ctx.options)[0].model == "sonnet"  # back to the mode's tier


def test_strong_next_stays_on_when_the_start_used_no_model(tmp_path):
    p, ctx = plan(tmp_path, "work", settings={**SETTINGS, "tier_strong": ""}, options={("D-1", "strong_next")})
    assert p.model is None and "no model set" in p.reason
    routing = type("R", (), {"env": tuple(p.env.items())})()
    M.ModelRouting(ctx).launched(LaunchRequest("D-1", "work", "claude"), routing, ctx, "D-1")
    assert ctx.relayed == []


def test_continue_follows_the_tier_of_the_last_start(tmp_path):
    ctx = Ctx(tmp_path)
    obj = M.ModelRouting(ctx)
    refine = obj.launch(LaunchRequest("D-1", "refine", "claude"), ctx)
    obj.launched(LaunchRequest("D-1", "refine", "claude"), type("R", (), {"env": tuple(refine.env.items())})(), ctx, "D-1")
    assert obj.launch(LaunchRequest("D-1", "continue", "claude"), ctx).model == "opus"
    assert obj.launch(LaunchRequest("D-2", "continue", "claude"), ctx).model == "sonnet"  # no earlier start there


# -- escalation ------------------------------------------------------------------------------------------------------

def receipt(name, by, exit=1, task="T1"):
    return {"name": name, "kind": "receipt", "task": task, "by": by, "added": "2026-10-05T10:00Z",
            "run": {"exit": exit, "timed_out": False, "at": "2026-10-05T10:00Z", "seconds": 1, "dirty": False,
                    "commit": None, "steps": []}}


def ticket(arts, tasks="- [ ] T1 build it\n  - verify: cmd: false\n", status="in-progress"):
    return {"title": "Flaky", "status": status, "sections": {"Tasks": tasks}, "meta": {"artifacts": arts}}


@pytest.fixture
def fw(tmp_path):
    return fake_workspace(tmp_path / "w", tickets=[
        ticket([receipt("receipt-T1-a.log", "agent:claude:aaaaaaaa"), receipt("receipt-T1-b.log", "agent:claude:bbbbbbbb")]),
        ticket([receipt("receipt-T1-a.log", "agent:claude:aaaaaaaa"), receipt("receipt-T1-b.log", "agent:claude:aaaaaaaa")]),
        ticket([receipt("receipt-T1-a.log", "agent:claude:aaaaaaaa"), receipt("receipt-T1-b.log", "agent:claude:bbbbbbbb", exit=0)]),
        ticket([receipt("receipt-T1-a.log", "agent:claude:aaaaaaaa"), receipt("receipt-T1-b.log", "agent:claude:bbbbbbbb")],
               tasks="- [x] T1 build it\n  - note: fixed\n"),
        ticket([receipt("receipt-T1-a.log", "human:you"), receipt("receipt-T1-b.log", "agent:claude")]),
    ])


def test_only_a_task_that_failed_in_two_separate_sessions_is_listed(fw):
    ids = fw.tickets
    items = M.failures_items(fw.context(MANIFEST))
    assert [(i["ticket"], i["task"], i["sessions"]) for i in items] == [(ids[0], "T1", 2)]


def test_the_item_carries_the_tail_of_the_latest_failing_log(fw):
    d = fw.ws.artifacts_dir / fw.tickets[0]
    d.mkdir(parents=True)
    (d / "receipt-T1-b.log").write_text("$ pytest\nFAILED test_x\x1b[31m boom\n[verify: exit 1 after 1s]\n")
    (item,) = M.failures_items(fw.context(MANIFEST))
    assert item["receipt"] == "receipt-T1-b.log" and "FAILED test_x" in item["tail"] and "\x1b" not in item["tail"]


def test_a_symlinked_log_is_not_read(fw, tmp_path):
    d = fw.ws.artifacts_dir / fw.tickets[0]
    d.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (d / "receipt-T1-b.log").symlink_to(secret)
    (item,) = M.failures_items(fw.context(MANIFEST))
    assert item["tail"] == ""


def view_of(fw, loaded_obj, ctx):
    from orch.addons.loader import LoadedAddon
    la = LoadedAddon(MANIFEST.name, "custom", MANIFEST, ADDON, loaded_obj, ctx, "t")
    return SlotView(fw.ws, la, "today.from_addons")


@pytest.fixture
def card(fw):
    ctx = fw.context(MANIFEST)
    obj = M.create(ctx)
    fw.cache(MANIFEST.name, obj.providers[0].fetch(ctx.provider_context(), "workspace", None))
    return fw, ctx, obj


def test_one_card_per_task_and_it_asks_for_one_tier_up(card):
    fw, ctx, obj = card
    (d,) = obj.decisions(view_of(fw, obj, ctx))
    assert isinstance(d, PendingDecision) and d.id == f"escalate-{fw.tickets[0]}-T1" and d.ticket == fw.tickets[0]
    assert "2 sessions" in d.title and d.choices[0][1] == "Next start on Strong"  # nothing recorded: standard, one up
    assert obj.decisions(view_of(fw, obj, ctx)) == obj.decisions(view_of(fw, obj, ctx))  # stable, not duplicated


def test_accepting_arranges_the_next_start_and_the_card_goes(card):
    fw, ctx, obj = card
    (d,) = obj.decisions(view_of(fw, obj, ctx))
    msg = obj.resolve(d.id, "escalate", ctx.provider_context())
    assert "Strong" in msg and "Start agent" in msg
    assert obj.decisions(view_of(fw, obj, ctx)) == []
    # the next start on that ticket is one tier up and its prompt names the receipt
    ctx2 = Ctx(Path(ctx.state_dir).parent.parent / "x")
    ctx2.state_dir = ctx.state_dir
    p = obj.launch(LaunchRequest(fw.tickets[0], "work", "claude"), ctx2)
    assert p.model == "opus" and fw.tickets[0] in d.id and "T1" in p.note and "Escalated" in p.reason
    obj.launched(LaunchRequest(fw.tickets[0], "work", "claude"), type("R", (), {"env": tuple(p.env.items())})(), ctx2, "s")
    assert obj.launch(LaunchRequest(fw.tickets[0], "work", "claude"), ctx2).note == ""  # one start only


def test_not_now_hides_the_card_until_another_session_fails(card, fw):
    fw_, ctx, obj = card
    (d,) = obj.decisions(view_of(fw_, obj, ctx))
    obj.resolve(d.id, "ignore", ctx.provider_context())
    assert obj.decisions(view_of(fw_, obj, ctx)) == []
    assert obj.launch(LaunchRequest(fw_.tickets[0], "work", "claude"), Ctx(Path(ctx.state_dir).parent)).model == "sonnet"


def test_no_card_when_the_last_start_was_already_strong(card):
    fw, ctx, obj = card
    M.State(ctx.state_dir).record_start(fw.tickets[0], "strong", "work", "s")
    assert obj.decisions(view_of(fw, obj, ctx)) == []


def test_a_stale_card_is_refused_not_applied(card):
    fw, ctx, obj = card
    (d,) = obj.decisions(view_of(fw, obj, ctx))
    obj.resolve(d.id, "ignore", ctx.provider_context())
    assert "no longer current" in obj.resolve(d.id, "escalate", ctx.provider_context())
    assert obj.resolve("escalate-nope", "escalate", ctx.provider_context()) is None
    assert obj.resolve(d.id, "bad", ctx.provider_context()) is None


# -- the Workspace page ----------------------------------------------------------------------------------------------

def texts(widgets):
    return json.dumps(widgets, default=lambda o: getattr(o, "__dict__", str(o)))


def test_the_workspace_panel_shows_each_mode_and_its_model(fw, monkeypatch):
    class V:
        settings = {**MANIFEST.defaults(), **SETTINGS}
    out = texts(M.create(fw.context(MANIFEST)).widgets("workspace.settings", V))
    assert "Strong: opus" in out and "Standard: sonnet" in out and "Same as the last start" in out and "haiku" in out
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "1")
    assert "CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set" in texts(M.create(fw.context(MANIFEST)).widgets("workspace.settings", V))


def test_the_empty_panel_says_nothing_is_routed(fw):
    class V:
        settings = MANIFEST.defaults()
    out = texts(M.create(fw.context(MANIFEST)).widgets("workspace.settings", V))
    assert "No tier has a model yet" in out and "Not routed" in out


# -- the platform's own checks -------------------------------------------------------------------------------------------

class TestAddon(AddonContract):
    addon_dir = ADDON


class TestFailuresContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return M.FailuresProvider(None)

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        return orch_workspace.provider_context(MANIFEST, runner=FakeRunner(strict=False))
