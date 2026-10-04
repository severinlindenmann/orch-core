"""#19: an agent is recognised by its process ancestry, not only by environment variables it can unset."""
import os
import sys

import pytest

import orch.actor as actor
from orch.errors import HumanOnlyError
from conftest import human_ops


def _chain(*args_lines):
    return [(100 + i, line) for i, line in enumerate(args_lines)]


@pytest.mark.parametrize("args,harness", [
    ("/opt/homebrew/bin/claude --settings /tmp/x.json", "claude-code"),
    ("claude", "claude-code"),
    ("node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js", "claude-code"),
    ("/usr/bin/node /usr/local/bin/codex exec", "codex"),
    ("codex", "codex"),
    ("node /usr/lib/node_modules/@github/copilot/index.js", "copilot-cli"),
    ("copilot --allow-all-tools", "copilot-cli"),
    ("gemini -p hi", "gemini-cli"),
    ("/home/u/.local/bin/aider --model x", "aider"),
    ("python3 /home/u/.local/bin/aider", "aider"),
    ("cursor-agent", "cursor-agent"),
    ("opencode run", "opencode"),
])
def test_known_harness_processes(args, harness):
    assert actor.harness_of(args) == harness


@pytest.mark.parametrize("args", [
    "-/bin/zsh", "/usr/bin/login -flp user /bin/bash", "/Applications/cmux.app/Contents/MacOS/cmux",
    "/bin/zsh -c cd /tmp/claude-notes && ls", "vim claude.md", "tmux new -s claude", "sshd: user@pts/0",
    "/Applications/iTerm.app/Contents/MacOS/iTerm2", "python3 -m pytest tests/test_claude.py",
])
def test_ordinary_processes_are_not_harnesses(args):
    assert actor.harness_of(args) is None


def test_ancestor_harness_makes_an_agent_even_without_env(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("/bin/zsh -c orch approve", "/opt/homebrew/bin/claude",
                                                                "-/bin/zsh", "/usr/bin/login"))
    assert actor.agent_harness() == "claude-code"
    assert actor.cli_actor().kind == "agent"


def test_no_agent_anywhere_is_human_with_a_tty(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "/usr/bin/login"))
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert actor.agent_harness() is None
    assert actor.cli_actor().kind == "human"


@pytest.mark.parametrize("var,value", [("CLAUDECODE", "1"), ("CLAUDE_CODE_SESSION_ID", "abc"),
                                       ("CLAUDE_CODE_ENTRYPOINT", "cli"), ("ORCH_HARNESS", "copilot"),
                                       ("AI_AGENT", "claude-code_2_agent"), ("CODEX_SANDBOX", "seatbelt")])
def test_env_markers_still_count(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    assert actor.agent_harness()


def test_human_actor_refuses_under_an_agent_ancestor(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("script -q /dev/null orch approve L-1 plan",
                                                                "/usr/local/bin/codex"))
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must refuse before prompting"))
    with pytest.raises(HumanOnlyError, match="agent harness"):
        actor.human_actor("L-0001")


@pytest.mark.skipif(os.name == "nt", reason="unreadable process tree is tolerated on Windows")
def test_unreadable_process_tree_fails_closed(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: None)
    assert actor.agent_harness() == "unknown"
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    with pytest.raises(HumanOnlyError):
        actor.human_actor("L-0001")


def test_evidence_summarises_the_chain(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "/usr/bin/login -flp u", "/sbin/launchd"))
    ev = actor.process_evidence()
    assert ev == {"harness": None, "chain": ["zsh", "login", "launchd"]}
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("bash", "/opt/homebrew/bin/claude --x"))
    assert actor.process_evidence()["harness"] == "claude-code"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process tree")
def test_real_process_chain_starts_at_the_parent():
    chain = actor._read_chain()
    assert chain, "the process tree must be readable on POSIX"
    assert chain[0][0] == os.getppid()


def test_ops_refuse_a_tty_human_under_an_agent(ws, aops, hops, monkeypatch):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("python3 -c x", "claude"))
    with pytest.raises(HumanOnlyError, match="agent harness"):
        hops.approve(t.id, "requirements")


def test_human_events_record_process_evidence(ws, aops, hops, monkeypatch):
    from orch.core.events import read_events
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "/usr/bin/login"))
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    events = read_events(ws, t.id)
    approved = [e for e in events if e.kind == "gate.approved"][-1]
    assert approved.evidence == {"harness": None, "chain": ["zsh", "login"]}
    assert all(e.evidence is None for e in events if not e.actor.startswith("human:"))
    assert '"evidence"' not in (ws.state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]


def test_check_flags_human_events_written_under_an_agent(ws, aops, hops, monkeypatch):
    from orch.core.check import run_checks
    from orch.core.events import Actor, append_event
    t = aops.new("x")
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("bash", "/usr/local/bin/claude"))
    # written past the Ops checks (as a script talking to the event log would)
    append_event(ws, t.id, "log.added", Actor("human", "you", "dashboard"), {"text": "x"})
    monkeypatch.setattr(actor, "process_chain", lambda: [])
    found = [f for f in run_checks(ws, emit_events=False) if f.code == "human-action-from-agent"]
    assert found and found[0].ticket == t.id and "claude-code" in found[0].message


@pytest.mark.parametrize("via", ["dashboard", "phone:pixel", "tty"])
def test_any_human_actor_is_refused_under_an_agent(ws, aops, monkeypatch, via):
    """A human Actor built in-process with any `via` gets no further than the CLI does."""
    from orch.core.events import Actor
    from orch.core.ops import Ops
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("python3 x.py", "codex"))
    with pytest.raises(HumanOnlyError, match="agent harness"):
        human_ops(ws, Actor("human", "you", via)).approve(t.id, "requirements")
    monkeypatch.setattr(actor, "process_chain", lambda: [])
    human_ops(ws, Actor("human", "you", via)).approve(t.id, "requirements")


def test_the_addon_test_kit_isolates_the_process_tree(tmp_path, monkeypatch):
    import inspect

    from orch.testing import pytest_plugin
    assert "process_chain" in inspect.getsource(pytest_plugin.orch_user_dir)


def test_serve_refuses_under_an_agent_ancestor(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("bash", "gemini"))
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    with pytest.raises(HumanOnlyError, match="agent harness"):
        actor.require_human_terminal("starting the dashboard")


def test_conftest_isolates_the_real_tree():
    # The suite runs under whatever started it (an agent, CI, a terminal): tests see a clean chain by default.
    assert actor.process_chain() == []
    assert sys.modules["orch.actor"] is actor


def test_pid_1_is_part_of_the_walk(monkeypatch):
    """In a container the harness can be PID 1 itself."""
    class R:
        returncode = 0
        stdout = "    1     0 claude --dangerously-skip-permissions\n   50     1 /bin/sh -c orch approve\n"

    monkeypatch.setattr(actor.subprocess, "run", lambda *a, **k: R())
    chain = actor._read_chain_ps(50)
    assert chain == [(50, "/bin/sh -c orch approve"), (1, "claude --dangerously-skip-permissions")]
    monkeypatch.setattr(actor, "process_chain", lambda: chain)
    assert actor.ancestor_harness() == "claude-code"


def test_doctor_shows_the_detected_harness_and_chain(ws, monkeypatch, capsys):
    from orch.cli import run
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "/opt/homebrew/bin/claude --x"))
    run(["doctor"])
    out = capsys.readouterr().out
    assert "actor: an agent harness is detected (claude-code)" in out and "zsh ← claude" in out
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "/usr/bin/login"))
    run(["doctor"])
    assert "no agent harness detected" in capsys.readouterr().out


# #40: Mission Control's terminals run in orch's own tmux server. A pane there is a child of that server, not of the
# agent that may have asked it for a new window, and it has a real TTY: it must not pass for the human's terminal.
@pytest.mark.parametrize("args", [
    "tmux -L orch new-session -d -s DEMO-1 -c /w -x 160 -y 45 env -u TMUX -u TMUX_PANE claude",
    "/opt/homebrew/bin/tmux -Lorch new-session -d -s scratch",
    "tmux -2 -L orch new -d", "tmux -S /tmp/tmux-501/orch new-session -d",
    "tmux: server (/tmp/tmux-1000/orch)",
])
def test_orchs_tmux_server_counts_as_a_harness(args):
    assert actor.harness_of(args) == "orch-terminals"


@pytest.mark.parametrize("args", ["tmux -L work new -d", "tmux -L orchard ls", "tmux new -s orch",
                                  "tmux: server (/tmp/tmux-1000/default)", "vim -L orch"])
def test_other_tmux_servers_are_not(args):
    assert actor.harness_of(args) is None


def test_a_shell_in_a_mission_control_pane_is_an_agent(monkeypatch):
    monkeypatch.setattr(actor, "process_chain", lambda: _chain("-/bin/zsh", "tmux -L orch new-window -d", "/sbin/launchd"))
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert actor.agent_harness() == "orch-terminals"
    with pytest.raises(HumanOnlyError):
        actor.require_human_terminal("approving")
