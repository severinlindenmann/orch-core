import json

import pytest

from orch import actor
from orch.cli import run
from orch.errors import HumanOnlyError


@pytest.fixture
def agent_env(monkeypatch, ws_root):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "s-1")


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_new_list_show(agent_env, capsys):
    assert run(["new", "--title", "Back up config", "--size", "s", "--json"]) == 0
    created = out_json(capsys)
    assert created["id"] == "L-0001" and created["status"] == "backlog"
    assert run(["list", "--json"]) == 0
    assert [r["id"] for r in out_json(capsys)] == ["L-0001"]
    assert run(["show", "1"]) == 0
    out = capsys.readouterr().out
    assert "tickets/backlog/L-0001-back-up-config.md" in out and "## Log" in out  # no empty sections (anatomy v2)


def test_errors_have_exit_codes_and_json(agent_env, capsys):
    assert run(["show", "L-0404"]) == 2
    assert "no ticket" in capsys.readouterr().err
    assert run(["new", "--title", "x", "--type", "saga", "--json"]) == 2
    err = out_json(capsys)
    assert err["exit"] == 2 and err["error"] == "UsageError"


def test_claim_log_state_section_link(agent_env, capsys, tmp_path, put):
    tid = put("open")
    assert run(["claim", tid]) == 0
    assert run(["claim", tid]) == 0  # same session may re-claim
    plan = tmp_path / "plan.md"
    plan.write_text("1. do it", encoding="utf-8")
    assert run(["section", "set", tid, "Plan", "--file", str(plan)]) == 0
    assert run(["log", tid, "-m", "ran tests"]) == 0
    assert run(["state", tid, "-m", "halfway"]) == 0
    assert run(["state", tid]) == 2  # needs -m or --file
    assert run(["link", tid, "--repo", "hub", "--branch", "feature/x"]) == 0
    capsys.readouterr()
    assert run(["show", tid, "--json"]) == 0
    v = out_json(capsys)
    assert v["sections"]["Plan"] == "1. do it"
    assert v["meta"]["branches"] == {"hub": "feature/x"}
    assert v["meta"]["claim"]["harness"] == "test-agent"


def test_claim_conflict_exit_4(agent_env, monkeypatch, put):
    tid = put("open")
    assert run(["claim", tid]) == 0
    monkeypatch.setenv("ORCH_SESSION", "s-2")
    assert run(["claim", tid]) == 4


def test_next_search_path(agent_env, capsys, put):
    tid = put("open", sections={"Ask": "needle here"})
    capsys.readouterr()
    assert run(["next", "--json"]) == 0 and out_json(capsys)[0]["id"] == tid
    assert run(["search", "needle", "--json"]) == 0 and out_json(capsys)[0]["id"] == tid
    assert run(["path", tid]) == 0 and capsys.readouterr().out.strip().endswith(".md")


def test_actor_detection(monkeypatch):
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    assert actor.cli_actor().is_human
    monkeypatch.setenv("CLAUDECODE", "1")
    a = actor.cli_actor()
    assert a.name == "claude-code" and not a.is_human
    with pytest.raises(HumanOnlyError, match="agent harness"):
        actor.human_actor("L-0001")


def test_human_actor_needs_tty_and_confirmation(monkeypatch):
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    with pytest.raises(HumanOnlyError, match="interactive"):
        actor.human_actor("L-0001")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "L-0002")
    with pytest.raises(HumanOnlyError, match="did not match"):
        actor.human_actor("L-0001")
    monkeypatch.setattr("builtins.input", lambda prompt="": "l-0001")
    assert actor.human_actor("L-0001").via == "tty"


@pytest.mark.parametrize("args", [["rules"], ["show", "1"]])
def test_piped_output_survives_legacy_console_encoding(ws_root, put, args):
    """Windows pipes default to cp1252, which cannot encode '→' (rules text, Log lines)."""
    import os
    import subprocess
    import sys

    put("backlog", title="Move a → b", sections={"Log": "- 2026-09-30T09:00Z [you] moved backlog → open"})
    env = dict(os.environ, PYTHONIOENCODING="cp1252", ORCH_HARNESS="test-agent")
    res = subprocess.run([sys.executable, "-c", "from orch.cli import main; main()", *args],
                         cwd=ws_root, env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    assert "→".encode("utf-8") in res.stdout


@pytest.fixture
def no_uvicorn(monkeypatch):
    """Never actually start a server from these tests, even when the refusal is missing."""
    started = []
    try:
        import uvicorn
    except ImportError:
        return started
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    return started


def test_serve_refused_inside_agent_harness(ws_root, monkeypatch, capsys, no_uvicorn):
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["serve", "--no-open", "--port", "9999"]) == 3
    captured = capsys.readouterr()
    assert "?token=" not in captured.out and "your own terminal" in captured.err
    assert not no_uvicorn


def test_serve_refused_without_tty(ws_root, monkeypatch, capsys, no_uvicorn):
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    assert run(["serve", "--no-open", "--port", "9999"]) == 3
    assert "?token=" not in capsys.readouterr().out
    assert not no_uvicorn


def test_human_hints_name_no_agent_runnable_command():
    from orch.core.lifecycle import HUMAN_HINT
    for hint in (actor._HINT, HUMAN_HINT):
        assert "orch serve" not in hint and "ask the human" in hint


def test_version_command_and_flag_match_the_package_metadata(capsys):
    import tomllib
    from pathlib import Path

    from orch import __version__
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]   # a release bumps both: it printed 0.1.0 on 0.3.0
    assert run(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
