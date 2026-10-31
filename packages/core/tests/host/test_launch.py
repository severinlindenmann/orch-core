"""H11c: the closed harness registry and the launch argv (D-P2-28, §12.4.1), and ``runner.json`` (§12.4.3)."""

from __future__ import annotations

import json
import os

import pytest

from orch.host.errors import HostError
from orch.host.launch import LOGIN_EXEC, display, launch_argv, prompt
from orch.runner.config import ConfigError, RunnerConfig, parse, read_config

LOGIN = RunnerConfig()

REFINE = (
    "Run orch status, then refine ticket ORC-7. When the gates are complete, run orch wait until the person approves;"
    " then build the ticket in this session. Follow AGENTS.orch.md."
)


def test_codex_and_claude_argv_through_the_login_shell():
    argv, login, program = launch_argv("codex", "work", None, "ORC-7", LOGIN)
    assert argv == ["$SHELL", "-l", "-c", LOGIN_EXEC, "codex",
                    "Run orch status, then work on ticket ORC-7. Follow AGENTS.orch.md."]  # fmt: skip
    assert login is True and program is None
    argv, _, _ = launch_argv("claude-code", "refine", "opus", "ORC-7", LOGIN)  # not in a work folder: no extra flags
    assert argv[4:] == [
        "claude",
        "--model",
        "opus",
        "--session-id",
        "@orch-session-id",
        REFINE,
    ]
    argv, _, _ = launch_argv("claude-code", "refine", "opus", "ORC-7", LOGIN, workdir=True)
    assert argv[4:] == [
        "claude",
        "--model",
        "opus",
        "--session-id",
        "@orch-session-id",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--mcp-config",
        "@orch-mcp",
        "--append-system-prompt-file",
        "@orch-rules",
        REFINE.replace("Follow AGENTS.orch.md.", "Your rules are in your system prompt."),
    ]
    assert LOGIN_EXEC == 'exec "$0" "$@"'


def test_only_a_work_folder_agent_ignores_project_settings_and_mcp_files():
    flags = {"--setting-sources", "--strict-mcp-config", "--mcp-config", "@orch-mcp", "--append-system-prompt-file"}
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    head = ["claude", "--session-id", "@orch-session-id", "--setting-sources", "user", "--strict-mcp-config"]
    assert argv[4:10] == head
    assert flags <= set(argv)
    assert argv[10:14] == ["--mcp-config", "@orch-mcp", "--append-system-prompt-file", "@orch-rules"]
    # a repo, worktree or root launch (workdir False), a person-run harness and codex: Claude Code's normal behaviour
    assert not flags & set(launch_argv("claude-code", "work", None, "ORC-7", LOGIN)[0])
    assert not flags & set(launch_argv("claude-code", None, None, None, LOGIN, workdir=True)[0])
    assert not flags & set(launch_argv("codex", "work", None, "ORC-7", LOGIN, workdir=True)[0])
    assert launch_argv("claude-code", None, None, None, LOGIN)[0][4:] == ["claude", "--session-id", "@orch-session-id"]


def test_the_prompt_is_one_element_and_context_mode():
    argv, _, _ = launch_argv("codex", "context", None, "7", LOGIN)
    assert argv[-1] == "Run orch show 7 --section current_state, then wait for my instructions."
    assert len(argv) == 6
    assert prompt(None, "7") is None
    with pytest.raises(HostError) as e:
        prompt("work", None)
    assert e.value.code == "launch.invalid"
    with pytest.raises(HostError):
        prompt("work", "7; rm -rf /")  # a key is the host's lookup, never free text


def test_without_a_mode_a_harness_starts_interactive():
    argv, _, _ = launch_argv("claude-code", None, None, None, LOGIN)
    assert argv == ["$SHELL", "-l", "-c", LOGIN_EXEC, "claude", "--session-id", "@orch-session-id"]
    assert launch_argv("shell", None, None, None, LOGIN) == (["$SHELL", "-l"], False, None)


@pytest.mark.parametrize("model", ["-x", "--dangerously-skip-permissions", "a b", "", "x" * 65, "a;b", "a\nb"])
def test_a_model_starting_with_a_dash_or_with_a_space_is_launch_invalid(model):
    with pytest.raises(HostError) as e:
        launch_argv("codex", "work", model, "7", LOGIN)
    assert e.value.code == "launch.invalid" and "model" in e.value.message


def test_unknown_harness_and_shell_with_a_model_are_refused():
    for harness, mode, model in (("bash", None, None), ("shell", "work", None), ("shell", None, "m")):
        with pytest.raises(HostError) as e:
            launch_argv(harness, mode, model, "7", LOGIN)
        assert e.value.code == "launch.invalid"


def test_opt_out_uses_the_absolute_program_or_harness_not_found():
    cfg = RunnerConfig(login_shell=False, programs={"codex": "/opt/codex/bin/codex"})
    argv, login, program = launch_argv("codex", "fix", "o3", "7", cfg)
    assert argv[0] == "/opt/codex/bin/codex" and argv[1:3] == ["--model", "o3"] and login is False
    assert program == "/opt/codex/bin/codex"
    with pytest.raises(HostError) as e:
        launch_argv("claude-code", "fix", None, "7", cfg)
    assert e.value.code == "harness.not_found"


def test_display_quotes_every_argument():
    argv, _, _ = launch_argv("codex", "work", None, "7", LOGIN)
    shown = display(argv)
    assert shown.startswith('$SHELL -l -c \'exec "$0" "$@"\' codex \'Run orch status')


@pytest.mark.parametrize(
    "doc",
    [
        {"pass_env": ["LD_PRELOAD"]},
        {"pass_env": ["DYLD_INSERT_LIBRARIES"]},
        {"pass_env": ["PYTHONPATH"]},
        {"pass_env": ["GIT_SSH_COMMAND"]},
        {"pass_env": ["ORCH_GRANT"]},
        {"pass_env": ["BASH_ENV"]},
        {"pass_env": ["ENV"]},
        {"pass_env": ["ZDOTDIR"]},
        {"pass_env": ["PATH"]},
        {"pass_env": ["HOME"]},
        {"pass_env": ["SHELL"]},
        {"pass_env": ["IFS"]},
        {"pass_env": ["PS4"]},
        {"pass_env": ["NODE_OPTIONS"]},
        {"pass_env": ["PERL5OPT"]},
        {"pass_env": ["RUBYOPT"]},
        {"pass_env": ["a-b"]},
        {"programs": {"codex": "codex"}},
        {"programs": {"vim": "/usr/bin/vim"}},
        {"login_shell": "no"},
        {"extra": 1},
        [],
    ],
)
def test_runner_json_deny_list_and_shape_refuse_the_whole_file(doc):
    with pytest.raises(ConfigError):
        parse(doc)


def test_runner_json_reads_defaults_and_refuses_an_unsafe_file(tmp_path):
    assert read_config(tmp_path, os.getuid()) == RunnerConfig()
    path = tmp_path / "runner.json"
    path.write_text(json.dumps({"v": 1, "pass_env": ["ANTHROPIC_API_KEY"], "login_shell": False}))
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError):
        read_config(tmp_path, os.getuid())  # group/other readable: refused, never defaults
    os.chmod(path, 0o600)
    cfg = read_config(tmp_path, os.getuid())
    assert cfg.pass_env == ("ANTHROPIC_API_KEY",) and cfg.login_shell is False


@pytest.mark.parametrize("text", ["null", "[]", '"x"', "1", "true"])
def test_runner_json_that_is_not_an_object_refuses_never_defaults(tmp_path, text):
    """Codex P2 #4: only a missing file means defaults."""
    path = tmp_path / "runner.json"
    path.write_text(text)
    path.chmod(0o600)
    with pytest.raises(ConfigError):
        read_config(tmp_path, os.getuid())


def test_the_prompt_can_name_the_instructions_by_path():
    argv, _, _ = launch_argv("codex", "work", None, "ORC-7", LOGIN, instructions="/p/.orch/AGENTS.orch.md")
    assert argv[-1] == "Run orch status, then work on ticket ORC-7. Follow /p/.orch/AGENTS.orch.md."


def test_task_mode_is_one_prompt_element_and_needs_a_parent_session():
    task = {
        "session": "s_01J0000000000000000000000A",
        "title": "Review it",
        "brief": "Check X.",
        "expected": "A report",
    }
    argv, _, _ = launch_argv("codex", "task", None, "ORC-7", LOGIN, task=task)
    assert argv[-1] == (
        "Run orch status. You are a delegated task of session s_01J0000000000000000000000A on ticket ORC-7. "
        "Task: Review it. Brief: Check X. Expected output: A report Ask the parent, not the person; log your result "
        "with orch log, then stop."
    )
    assert len(argv) == 6  # login shell form: the whole prompt is the last element
    with pytest.raises(HostError):
        launch_argv("codex", "task", None, "ORC-7", LOGIN)


def test_delegate_false_adds_one_equals_flag_before_the_prompt_for_claude_only():
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, delegate=False)
    assert argv[-2] == "--disallowedTools=Agent,Task" and argv[-1].startswith("Run orch status")
    assert not any(
        a.startswith("--disallowedTools") for a in launch_argv("claude-code", "work", None, "ORC-7", LOGIN)[0]
    )
    assert not any(
        a.startswith("--disallowedTools") for a in launch_argv("codex", "work", None, "ORC-7", LOGIN, delegate=False)[0]
    )


def test_the_refine_prompt_keeps_the_session_for_the_build():
    # run-9 J04: "refine ticket X" read as "this session is only for refining"; the agent stopped before the approval
    from orch.host.launch import prompt

    text = prompt("refine", "ORC-7")
    assert text.startswith("Run orch status, then refine ticket ORC-7.")
    assert "orch wait" in text and "build" in text and "this session" in text
    assert text.endswith("Follow AGENTS.orch.md.")


# -- run 16 P01: a launch in a ticket work folder names no path --------------------------------------------------------

_FU = "fu_" + "0" * 26
_ALL_MODES = ["refine", "work", "fix", "continue", "followup"]


def _inline(mode):
    task = {"followup": _FU} if mode == "followup" else None
    return prompt(mode, "ORC-7", "/abs/outside/.orch/AGENTS.orch.md", task, rules_inline=True)


@pytest.mark.parametrize("mode", _ALL_MODES)
def test_a_claude_work_folder_prompt_names_no_path(mode):
    text = _inline(mode)
    assert text.endswith("Your rules are in your system prompt.")
    assert "AGENTS.orch.md" not in text and "/" not in text.replace("sc_", "")  # no absolute or any other path
    assert not any(tok.startswith(("/", "~", "..")) for tok in text.split())


def test_claude_in_a_work_folder_gets_the_inline_prompt_through_launch_argv():
    argv, _, _ = launch_argv(
        "claude-code", "work", None, "ORC-7", LOGIN, instructions="/abs/AGENTS.orch.md", workdir=True
    )
    assert argv[-1] == "Run orch status, then work on ticket ORC-7. Your rules are in your system prompt."
    assert "/abs/AGENTS.orch.md" not in argv and "--append-system-prompt-file" in argv


def test_other_launches_keep_the_pointer():
    # a worktree/repo Claude Code loads CLAUDE.md itself; codex has no appended system prompt
    for harness, workdir in (("claude-code", False), ("codex", True), ("codex", False)):
        argv, _, _ = launch_argv("claude-code" if harness == "claude-code" else harness, "work", None, "ORC-7", LOGIN,
                                 instructions="/p/.orch/AGENTS.orch.md", workdir=workdir)  # fmt: skip
        assert argv[-1].endswith("Follow /p/.orch/AGENTS.orch.md.")


def test_rule_map_every_rule_of_the_old_prompt_is_still_delivered():
    """The old prompt carried (1) the mode text, (2) ``orch status`` first, (3) for refine: wait for approval then
    build in this session, (4) "Follow <AGENTS.orch.md>". 1-3 stay in the prompt; 4 is the appended system prompt."""
    for mode in ("refine", "work", "fix", "continue"):
        old, new = prompt(mode, "ORC-7"), _inline(mode)
        assert old.removesuffix("Follow AGENTS.orch.md.") == new.removesuffix("Your rules are in your system prompt.")
        assert new.startswith("Run orch status, then")
    refine = _inline("refine")
    assert "orch wait" in refine and "build the ticket in this session" in refine
    from orch.host.launch import RULES_FLAG
    from orch.instructions.harness import AGENTS_FILE
    from orch.runner import rules

    assert RULES_FLAG == rules.FLAG  # delivery (4): the runner writes AGENTS.orch.md + AGENTS.md into this file
    assert AGENTS_FILE == "AGENTS.orch.md" and "AGENTS.orch.md first" in rules.HEADER
