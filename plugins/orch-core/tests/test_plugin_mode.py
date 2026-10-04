import json

from orch.instructions.settings import GUARD_COMMAND, PLUGIN_ID, SESSION_COMMAND, merge_settings
from orch.instructions.sync import sync_instructions


def _commands(settings, event):
    return [h["command"] for e in settings.get("hooks", {}).get(event, []) for h in e["hooks"]]


def test_plugin_mode_sync_writes_no_skills_or_orch_hooks(configure):
    ws = configure(harnesses=["claude-plugin"])
    got = {r.path.relative_to(ws.root).as_posix(): r.action for r in sync_instructions(ws)}
    assert got["CLAUDE.md"] == "created" and got["AGENTS.md"] == "created"
    assert got["orchestrator/AGENTS.orch.md"] == "created" and got[".claude/settings.json"] == "created"
    assert not any("/skills/" in p for p in got)
    settings = json.loads((ws.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["attribution"] == {"commit": "", "pr": ""}
    assert settings["enabledPlugins"] == {PLUGIN_ID: True}
    assert settings["extraKnownMarketplaces"]["orch-core"]["source"] == {
        "source": "github", "repo": "severinlindenmann/orch-core"}
    assert "hooks" not in settings


def test_switching_to_plugin_mode_removes_only_orch_hooks(configure):
    cfg = configure().config
    old = merge_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-lint"}]}]}}, cfg)
    assert GUARD_COMMAND in _commands(old, "PreToolUse")
    new = merge_settings(old, cfg, plugin_mode=True)
    assert _commands(new, "PreToolUse") == ["my-lint"]
    assert SESSION_COMMAND not in _commands(new, "SessionStart")


def test_plugin_mode_respects_disabled_plugin(configure):
    cfg = configure().config
    merged = merge_settings({"enabledPlugins": {PLUGIN_ID: False}}, cfg, plugin_mode=True)
    assert merged["enabledPlugins"][PLUGIN_ID] is False


def test_plugin_mode_is_idempotent(configure):
    ws = configure(harnesses=["claude-plugin", "copilot"])
    sync_instructions(ws)
    got = {r.path.relative_to(ws.root).as_posix(): r.action for r in sync_instructions(ws)}
    assert set(got.values()) == {"unchanged"}
    assert ".agents/skills/orch-tickets/SKILL.md" in got  # copilot still gets skill copies


def test_an_old_guard_matcher_is_upgraded_to_read_and_grep():
    from orch.instructions.settings import GUARD_MATCHER
    cfg = {"commit": {"forbid_attribution": False}}
    old = {"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit",
                                     "hooks": [{"type": "command", "command": GUARD_COMMAND}]},
                                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-lint"}]}]}}
    pre = merge_settings(old, cfg)["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == GUARD_MATCHER and "Read" in GUARD_MATCHER and pre[1]["matcher"] == "Bash"


def test_a_read_and_grep_only_guard_matcher_is_upgraded_to_glob_and_notebookedit():
    from orch.instructions.settings import GUARD_MATCHER
    cfg = {"commit": {"forbid_attribution": False}}
    old = {"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|Read|Grep",
                                     "hooks": [{"type": "command", "command": GUARD_COMMAND}]}]}}
    pre = merge_settings(old, cfg)["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == GUARD_MATCHER and "Glob" in GUARD_MATCHER and "NotebookEdit" in GUARD_MATCHER
