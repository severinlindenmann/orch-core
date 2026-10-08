"""#166: the ticket-usage recorder in `orch doctor` and the human-run `orch addon setup ticket-usage`."""
import json
import os

import pytest

from orch import actor, cli, onboarding
from orch.addons import usage_recorder as rec
from orch.addons import userfiles
from orch.hooks.guard import evaluate

STATUS = {"type": "command", "command": "~/.claude/orch-usage/statusline.sh"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """HOME and Claude's dir are temp dirs: nothing here reads or writes the real ~/.claude."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h / ".claude"))
    return h


@pytest.fixture
def enabled(ws):
    userfiles.set_enabled(ws.root, rec.ADDON, True)
    return ws


def settings_file(home):
    return home / ".claude" / "settings.json"


def recorder_check(ws):
    return {c.code: c for c in onboarding.doctor(ws.root)}.get("usage-recorder")


def human(monkeypatch, typed="ticket-usage"):
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)


# ---- doctor --------------------------------------------------------------------------------------------------------

def test_no_check_while_ticket_usage_is_off(ws):
    assert recorder_check(ws) is None


def test_not_installed_points_at_the_setup_command(enabled):
    c = recorder_check(enabled)
    assert not c.ok and c.fix == "orch addon setup ticket-usage" and "every Claude Code session" in c.message
    assert "usage-recorder" in {i.code for i in onboarding.open_setup_items(enabled)}
    onboarding.validate_item_code("usage-recorder")  # it can be dismissed like the other items


def test_ok_when_the_limits_log_exists(enabled, home):
    (home / ".claude" / "orch-usage").mkdir()
    (home / ".claude" / "orch-usage" / "limits.jsonl").write_text("")
    assert recorder_check(enabled).ok


def test_ok_for_a_configured_log_path(enabled, tmp_path):
    log = tmp_path / "elsewhere.jsonl"
    log.write_text("")
    userfiles.save_addon_config(enabled.root, rec.ADDON, {"limits_log": str(log)})
    assert recorder_check(enabled).ok


def test_ok_when_the_status_line_runs_the_recorder(enabled, home, tmp_path):
    settings_file(home).write_text(json.dumps({"statusLine": STATUS}))
    assert recorder_check(enabled).ok
    script = tmp_path / "line.sh"  # a script of the user's that calls it
    script.write_text("input=$(cat)\nprintf '%s' \"$input\" | ~/.claude/orch-usage/statusline.sh >/dev/null\n")
    settings_file(home).write_text(json.dumps({"statusLine": {"type": "command", "command": str(script)}}))
    assert recorder_check(enabled).ok


def test_an_existing_status_line_gets_the_line_to_add_not_a_replacement(enabled, home):
    settings_file(home).write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    c = recorder_check(enabled)
    assert not c.ok and rec.add_line() in c.fix and "leaves your status line alone" in c.fix
    assert rec.add_line() == "printf '%s' \"$input\" | ~/.claude/orch-usage/statusline.sh >/dev/null"


def test_unreadable_settings_are_named(enabled, home):
    settings_file(home).write_text("{oops")
    c = recorder_check(enabled)
    assert not c.ok and "cannot be checked" in c.message and str(settings_file(home)) in c.fix


# ---- orch addon setup ticket-usage -----------------------------------------------------------------------------------

def test_agents_are_refused(ws, home, monkeypatch):
    monkeypatch.chdir(ws.root)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 3
    assert not rec.script_path().exists() and not settings_file(home).exists()


def test_non_tty_is_refused(ws, home, monkeypatch):
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 3
    assert not rec.script_path().exists() and not settings_file(home).exists()


def test_apply_refuses_an_agent_too(monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    with pytest.raises(Exception, match="agent harness"):
        rec.apply(rec.plan())
    assert not rec.script_path().exists()


def test_other_addons_have_no_setup(ws, monkeypatch):
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "github-issues"]) == 2


def test_human_setup_copies_the_script_and_adds_the_status_line(ws, home, monkeypatch, capsys):
    settings_file(home).write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}))
    os.chmod(settings_file(home), 0o644)
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    out = capsys.readouterr().out
    assert "user-global" in out and "added the status line" in out
    script = rec.script_path()
    assert script.read_bytes() == rec.source_script().read_bytes() and os.access(script, os.X_OK)
    data = json.loads(settings_file(home).read_text())
    assert data == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}, "statusLine": STATUS}
    assert oct(settings_file(home).stat().st_mode & 0o777) == "0o644"
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked again"))
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0  # a second run changes nothing and asks nothing
    assert "Nothing to change" in capsys.readouterr().out


def test_setup_creates_missing_settings(ws, home, monkeypatch):
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    assert json.loads(settings_file(home).read_text()) == {"statusLine": STATUS}


def test_an_existing_status_line_is_never_replaced(ws, home, monkeypatch, capsys):
    mine = {"statusLine": {"type": "command", "command": "~/bin/my-line.sh", "padding": 0}}
    settings_file(home).write_text(json.dumps(mine))
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    assert json.loads(settings_file(home).read_text()) == mine
    assert rec.script_path().is_file()
    assert rec.add_line() in capsys.readouterr().out


def test_wrong_confirmation_changes_nothing(ws, home, monkeypatch):
    monkeypatch.chdir(ws.root)
    human(monkeypatch, typed="yes")
    assert cli.run(["addon", "setup", "ticket-usage"]) == 3
    assert not rec.script_path().exists() and not settings_file(home).exists()


def test_a_symlinked_settings_file_stays_a_symlink(ws, home, tmp_path, monkeypatch):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    settings_file(home).symlink_to(real)
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    assert settings_file(home).is_symlink() and json.loads(real.read_text()) == {"statusLine": STATUS}


def test_invalid_settings_are_left_alone(ws, home, monkeypatch, capsys):
    settings_file(home).write_text("{oops")
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    assert settings_file(home).read_text() == "{oops" and rec.script_path().is_file()
    assert "once it is valid JSON" in capsys.readouterr().out


def test_a_status_line_added_while_the_human_types_is_kept(ws, home, monkeypatch):
    monkeypatch.chdir(ws.root)
    human(monkeypatch)
    p = rec.plan()
    settings_file(home).write_text(json.dumps({"statusLine": {"type": "command", "command": "theirs"}}))
    with pytest.raises(Exception, match="changed since it was read"):
        rec.apply(p)
    assert json.loads(settings_file(home).read_text())["statusLine"]["command"] == "theirs"


def test_doctor_is_ok_after_setup(enabled, home, monkeypatch):
    monkeypatch.chdir(enabled.root)
    human(monkeypatch)
    assert cli.run(["addon", "setup", "ticket-usage"]) == 0
    assert recorder_check(enabled).ok


# ---- guard ---------------------------------------------------------------------------------------------------------

def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.mark.parametrize("cmd", [
    "orch addon setup ticket-usage", "uv run orch addon setup ticket-usage", "python -m orch.cli addon setup ticket-usage",
    '"${CLAUDE_PLUGIN_ROOT}/bin/orch" addon setup ticket-usage', "echo ticket-usage | orch addon setup ticket-usage",
    "python -c 'from orch.addons import usage_recorder as r; r.apply(r.plan())'",
    "cp recorder/statusline.sh ~/.claude/orch-usage/statusline.sh",
    "echo x > ~/.claude/orch-usage/statusline.sh",
    "jq '.statusLine={\"type\":\"command\"}' ~/.claude/settings.json > /tmp/s && mv /tmp/s ~/.claude/settings.json",
])
def test_guard_refuses_agents(ws, cmd):
    assert not evaluate(ws, bash(cmd)).allow, cmd


@pytest.mark.parametrize("cmd", ["orch addon list", "orch doctor --json", "cat ~/.claude/settings.json",
                                 "cat ~/.claude/orch-usage/statusline.sh"])
def test_guard_keeps_reads_open(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


def test_guard_refuses_file_tools_on_the_recorder_and_the_status_line(ws, home):
    write = lambda path, content: evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}})  # noqa: E731
    assert not write(home / ".claude" / "orch-usage" / "statusline.sh", "x").allow
    s = settings_file(home)
    assert not write(s, json.dumps({"statusLine": STATUS})).allow
    s.write_text(json.dumps({"model": "opus", "statusLine": STATUS}, indent=2))
    edit = lambda old, new: evaluate(ws, {"tool_name": "Edit", "tool_input": {"file_path": str(s), "old_string": old, "new_string": new}})  # noqa: E731
    # the user-scope settings hold the hooks that guard every session, so no file-tool edit of them is the agent's
    # (guard._HARNESS_DENIED), whatever key it touches; the recorder's own keys stay refused for the same reason
    assert not edit('"opus"', '"sonnet"').allow
    assert not edit("orch-usage/statusline.sh", "mine.sh").allow
    s.write_text('{"model": "opus", // comment\n}')
    assert not edit('"model"', '"statusLine": {}, "model"').allow  # not JSON: judged by the edit's own text
