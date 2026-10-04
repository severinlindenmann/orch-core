import json

import pytest

from orch.cli import run
from orch.core.model import yaml_load
from orch.errors import ValidationError
from orch.instructions.settings import GUARD_COMMAND, SESSION_COMMAND
from orch.instructions.sync import SKILL_NAMES, sync_instructions


def actions(results, ws):
    return {r.path.relative_to(ws.root).as_posix(): r.action for r in results}


def test_first_sync_creates_claude_files_and_second_is_noop(ws):
    got = actions(sync_instructions(ws), ws)
    assert got["orchestrator/AGENTS.orch.md"] == "created"
    assert got["AGENTS.md"] == "created" and got["CLAUDE.md"] == "created"
    assert got[".claude/settings.json"] == "created"
    for name in SKILL_NAMES:
        assert got[f".claude/skills/{name}/SKILL.md"] == "created"
    assert not any(p.startswith(".agents/") or p.startswith(".github/") for p in got)
    settings = json.loads((ws.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["attribution"] == {"commit": "", "pr": ""}
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == GUARD_COMMAND
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == SESSION_COMMAND
    assert set(actions(sync_instructions(ws), ws).values()) == {"unchanged"}


def test_other_harnesses(configure):
    ws = configure(harnesses=["claude", "copilot", "codex"])
    got = actions(sync_instructions(ws), ws)
    assert got[".github/copilot-instructions.md"] == "created"
    assert got[".agents/skills/orch-tickets/SKILL.md"] == "created"


def test_customer_text_survives_policy_change(configure):
    ws = configure()
    sync_instructions(ws)
    agents = ws.root / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nRepo hub deploys via Jenkins.\n", encoding="utf-8")
    ws = configure(git={"agent_may": {"commit": True}})
    assert actions(sync_instructions(ws), ws)["AGENTS.md"] == "updated"
    text = agents.read_text(encoding="utf-8")
    assert "You may commit" in text and "Repo hub deploys via Jenkins." in text and "## Customer notes" in text


def test_existing_settings_are_preserved(ws):
    path = ws.root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    user = {"permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-check"}]}]}}
    path.write_text(json.dumps(user, indent=4), encoding="utf-8")
    sync_instructions(ws)
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert merged["permissions"] == user["permissions"]
    commands = [h["command"] for e in merged["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert commands == ["my-check", GUARD_COMMAND]
    before = path.read_bytes()
    sync_instructions(ws)
    assert path.read_bytes() == before  # semantically equal: untouched


def test_settings_already_merged_keeps_user_formatting(ws):
    from orch.instructions.settings import merge_settings
    path = ws.root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(merge_settings({"model": "opus"}, ws.config), indent=4), encoding="utf-8")
    before = path.read_bytes()
    sync_instructions(ws)
    assert path.read_bytes() == before


def test_invalid_settings_abort_before_any_write(ws):
    path = ws.root / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        sync_instructions(ws)
    assert not (ws.root / "AGENTS.md").exists()


def test_dry_run_writes_nothing(ws):
    got = actions(sync_instructions(ws, dry_run=True), ws)
    assert got["AGENTS.md"] == "created"
    assert not (ws.root / "AGENTS.md").exists()


def test_existing_files_need_adopt_and_stay_untouched(ws):
    claude = ws.root / "CLAUDE.md"
    agents = ws.root / "AGENTS.md"
    claude.write_text("# Initech harness\n\nRules only; facts live in skills.\n", encoding="utf-8")
    agents.write_text("@CLAUDE.md\n", encoding="utf-8")
    before = (claude.read_bytes(), agents.read_bytes())
    got = actions(sync_instructions(ws), ws)
    assert got["CLAUDE.md"] == "needs-adopt" and got["AGENTS.md"] == "needs-adopt"
    assert (claude.read_bytes(), agents.read_bytes()) == before
    assert (ws.home / "AGENTS.orch.md").exists()  # the owned rules file is still written


def test_adopt_appends_block_at_the_end_once(ws):
    claude = ws.root / "CLAUDE.md"
    original = "# Initech harness\n\nRules only; facts live in skills.\n"
    claude.write_text(original, encoding="utf-8")
    assert actions(sync_instructions(ws, adopt=True), ws)["CLAUDE.md"] == "updated"
    text = claude.read_text(encoding="utf-8")
    assert text.startswith(original) and text.rstrip().endswith("<!-- orch:end -->")
    assert "@orchestrator/AGENTS.orch.md" in text
    assert actions(sync_instructions(ws), ws)["CLAUDE.md"] == "unchanged"  # adopted: plain sync now manages it


def test_adopt_and_update_keep_a_crlf_file_crlf(configure):
    ws = configure()
    claude = ws.root / "CLAUDE.md"
    claude.write_bytes(b"# Initech harness\r\n\r\nWindows line endings.\r\n")
    assert actions(sync_instructions(ws, adopt=True), ws)["CLAUDE.md"] == "updated"
    data = claude.read_bytes()
    assert data.startswith(b"# Initech harness\r\n\r\nWindows line endings.\r\n") and b"orch:end" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert actions(sync_instructions(ws), ws)["CLAUDE.md"] == "unchanged"
    ws = configure(git={"agent_may": {"commit": True}})
    assert actions(sync_instructions(ws), ws)["CLAUDE.md"] in ("updated", "unchanged")
    assert b"\n" not in claude.read_bytes().replace(b"\r\n", b"")


def test_tickets_skill_questions_example_parses_as_written(ws):
    sync_instructions(ws)
    text = (ws.root / ".claude" / "skills" / "orch-tickets" / "SKILL.md").read_text(encoding="utf-8")
    example = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    options = yaml_load(example)["questions"][0]["options"]
    assert [o["cost"] for o in options] == ["simple, mixed permissions", "three repos to maintain"]


def test_cli_sync_columns_line_up(ws_root, capsys):
    (ws_root / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    assert run(["instructions", "sync"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.split(" ", 1)[0] in ("created", "updated", "unchanged", "needs-adopt")]
    offsets = {len(ln) - len(ln.split(" ", 1)[1].lstrip(" ")) for ln in lines}
    assert len(lines) > 2 and offsets == {12}


def test_skill_files_have_valid_frontmatter(ws):
    sync_instructions(ws)
    for name in SKILL_NAMES:
        text = (ws.root / ".claude" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        meta = yaml_load(text.split("---\n")[1])
        assert meta["name"] == name and len(meta["description"]) > 40
        assert "Generated by `orch instructions sync`" in text


def test_cli_sync(ws_root, capsys):
    assert run(["instructions", "sync", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"path": "AGENTS.md", "action": "created"} in rows


def test_cli_sync_hints_adopt(ws_root, capsys):
    (ws_root / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    assert run(["instructions", "sync"]) == 0
    out = capsys.readouterr().out
    assert "needs-adopt CLAUDE.md" in out and "--adopt" in out
    assert run(["instructions", "sync", "--adopt"]) == 0
    assert "@orchestrator/AGENTS.orch.md" in (ws_root / "CLAUDE.md").read_text(encoding="utf-8")
