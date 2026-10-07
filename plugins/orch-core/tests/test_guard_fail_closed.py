"""The guard and the permission hook stay closed when the workspace config is broken, missing or agent-written: an
agent that corrupts orchestrator/config.json must not switch every check off (review 18, F1)."""
import json

import pytest
from typer.testing import CliRunner

from orch.cli import app
from orch.hooks.guard import evaluate
from test_factory_commits import session, repo  # noqa: F401
from test_factory_runner import fake, _trusted_programs, _payload  # noqa: F401

MATCHERS = ("Bash", "Edit", "Write", "MultiEdit", "Read", "Grep", "Glob", "NotebookEdit")


def _cli(args, payload, monkeypatch, home=None):
    if home is not None:
        monkeypatch.setenv("ORCH_HOME", str(home))
    r = CliRunner().invoke(app, args, input=json.dumps(payload))
    return r.exit_code, r.output


def _denied(out: str) -> bool:
    return '"deny"' in out


def _input(tool, ws):
    target = str(ws.root / "README.md")
    return {"Bash": {"command": "ls"}, "Read": {"file_path": target}, "Grep": {"pattern": "x", "path": str(ws.root)},
            "Glob": {"pattern": "*"}, "NotebookEdit": {"notebook_path": str(ws.root / "n.ipynb"), "new_source": ""},
            "MultiEdit": {"file_path": target, "edits": []}}.get(tool, {"file_path": target, "content": "x"})


def test_a_bound_session_never_writes_the_config(session):  # noqa: F811
    ws, b = session["ws"], session["b"]
    cfg = str(ws.home / "config.json")
    for content in ("{", json.dumps({"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4}})):
        p = {"session_id": b["session"], "tool_name": "Write", "cwd": str(session["wt"]),
             "tool_input": {"file_path": cfg, "content": content}}
        assert not evaluate(ws, p).allow


def test_an_agent_cannot_break_the_config_or_touch_its_factory_settings(configure):
    ws = configure(factory={"enabled": True})
    cfg = ws.home / "config.json"
    raw = json.loads(cfg.read_text())

    def write(content):
        return evaluate(ws, {"session_id": "s-1", "tool_name": "Write", "cwd": str(ws.root),
                             "tool_input": {"file_path": str(cfg), "content": content}})
    assert not write("{").allow and not write("[1]").allow and not write("").allow
    assert not write(json.dumps({**raw, "factory": {"enabled": True, "max_concurrency": 9}})).allow
    assert not write(json.dumps({k: v for k, v in raw.items() if k != "factory"})).allow
    assert write(json.dumps({**raw, "repos": {}})).allow  # the rest stays the agent's
    edit = evaluate(ws, {"session_id": "s-1", "tool_name": "Edit", "cwd": str(ws.root),
                         "tool_input": {"file_path": str(cfg), "old_string": "{", "new_string": "", "replace_all": False}})
    assert not edit.allow
    assert not evaluate(ws, {"session_id": "s-1", "tool_name": "NotebookEdit", "cwd": str(ws.root),
                             "tool_input": {"notebook_path": str(cfg), "new_source": "{"}}).allow


@pytest.mark.parametrize("broken", ["{", "[]", None])
def test_the_real_guard_stays_closed_for_a_bound_session(session, monkeypatch, broken):  # noqa: F811
    ws, b = session["ws"], session["b"]
    cfg = ws.home / "config.json"
    if broken is None:
        cfg.unlink()
    else:
        cfg.write_text(broken, encoding="utf-8")
    for cmd in ("cat ~/.config/orch/ledger.key", "git push origin HEAD:main", "ls"):
        p = {**_payload(b["session"], cmd), "cwd": str(session["wt"])}
        code, out = _cli(["guard", "--hook-json"], p, monkeypatch, home=ws.home if broken is not None else None)
        assert code == 0 and _denied(out), (cmd, out)
    for tool in MATCHERS:
        p = {"session_id": b["session"], "tool_name": tool, "cwd": str(session["wt"]), "tool_input": _input(tool, ws)}
        code, out = _cli(["guard", "--hook-json"], p, monkeypatch)
        assert _denied(out), (tool, out)
    p = {**_payload(b["session"], "make e2e"), "cwd": str(session["wt"])}
    code, out = _cli(["permit", "hook"], p, monkeypatch)
    assert '"behavior": "deny"' in out


def test_an_unbound_agent_in_a_broken_workspace_is_refused_but_may_repair_the_config(configure, monkeypatch):
    ws = configure()
    cfg = ws.home / "config.json"
    good = cfg.read_text()
    cfg.write_text("{", encoding="utf-8")
    sid = "s-1"
    for tool in MATCHERS:
        p = {"session_id": sid, "tool_name": tool, "cwd": str(ws.root), "tool_input": _input(tool, ws)}
        assert _denied(_cli(["guard", "--hook-json"], p, monkeypatch)[1]), tool
    repair = {"session_id": sid, "tool_name": "Write", "cwd": str(ws.root),
              "tool_input": {"file_path": str(cfg), "content": good}}
    assert _cli(["guard", "--hook-json"], repair, monkeypatch)[1] == ""
    still = {**repair, "tool_input": {"file_path": str(cfg), "content": "{{"}}
    assert _denied(_cli(["guard", "--hook-json"], still, monkeypatch)[1])
    read = {**repair, "tool_name": "Read", "tool_input": {"file_path": str(cfg)}}
    assert _cli(["guard", "--hook-json"], read, monkeypatch)[1] == ""
    # the permission hook has no opinion for a session the runner never bound
    assert _cli(["permit", "hook"], _payload(sid), monkeypatch)[1] == ""


def test_outside_every_workspace_the_guard_has_no_opinion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = {"session_id": "s-1", "tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "ls"}}
    assert _cli(["guard", "--hook-json"], p, monkeypatch) == (0, "")
