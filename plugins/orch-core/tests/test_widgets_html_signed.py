"""#9: agent HTML in ticket widgets (`widgets.html`) is on only when a signed human decision in the ledger backs the
config; a hand-edited config, a tampered entry or another workspace's entry leaves it off, and `orch check` says so."""
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger
from orch.core.check import run_checks
from orch.core.ops import Ops
from orch.errors import HumanOnlyError
from orch.widgets import Ctx


def _reopen(ws):
    from orch.core.workspace import Workspace
    return Workspace.open(ws.root)


def _codes(ws):
    return {f.code: f for f in run_checks(ws, emit_events=False) if f.ticket is None}


def test_off_by_default(ws):
    assert ledger.widgets_html_state(ws) == "off" and Ctx.of(ws).html is False
    assert "unsigned-setting" not in _codes(ws)


def test_agent_cannot_turn_it_on(ws, agent):
    with pytest.raises(HumanOnlyError):
        Ops(ws, agent).set_widgets_html(True)
    assert not ledger.entries(ws) and Ctx.of(_reopen(ws)).html is False


def test_human_under_an_agent_harness_is_refused(ws, human, monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    with pytest.raises(HumanOnlyError, match="agent harness"):
        Ops(ws, human).set_widgets_html(True)
    assert not ledger.entries(ws)


def test_cli_on_is_refused_in_an_agent_harness(ws_root, ws, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    assert run(["widget", "html", "on"]) != 0
    assert "agent harness" in capsys.readouterr().err
    assert Ctx.of(_reopen(ws)).html is False


def test_human_turns_it_on_with_a_signed_entry(ws, human):
    Ops(ws, human).set_widgets_html(True)
    entry = [e for e in ledger.entries(ws) if e["kind"] == "setting"][-1]
    assert (entry["setting"], entry["value"], entry["workspace"], entry["actor"]) == (
        "widgets.html", True, ledger.workspace_id(ws), "human:you")
    assert json.loads((ws.home / "config.json").read_text())["widgets"] == {"html": True}
    again = _reopen(ws)
    assert ledger.widgets_html_state(again) == "on" and Ctx.of(again).html is True
    assert "unsigned-setting" not in _codes(again)


def test_cli_on_for_the_human_in_a_terminal(ws_root, ws, monkeypatch, capsys):
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "html")
    assert run(["widget", "html", "on"]) == 0
    assert Ctx.of(_reopen(ws)).html is True


def test_hand_edited_config_without_a_signature_is_off_and_reported(ws, configure):
    edited = configure(widgets={"html": True})
    assert ledger.widgets_html_state(edited) == "unsigned" and Ctx.of(edited).html is False
    finding = _codes(edited)["unsigned-setting"]
    assert finding.level == "warning" and "widgets.html" in finding.message


def test_tampered_entry_is_ignored(ws, human):
    Ops(ws, human).set_widgets_html(False)  # a signed "off" ...
    path = ledger.ledger_path(ws)
    path.write_text(path.read_text().replace('"value": false', '"value": true'))  # ... edited to "on"
    raw = json.loads((ws.home / "config.json").read_text())
    (ws.home / "config.json").write_text(json.dumps({**raw, "widgets": {"html": True}}))
    edited = _reopen(ws)
    assert ledger.widgets_html_state(edited) == "unsigned" and Ctx.of(edited).html is False
    assert "unsigned-setting" in _codes(edited)


def test_another_workspaces_signature_does_not_count(ws, human, configure):
    Ops(ws, human).set_widgets_html(True)
    other = configure(customer="other", widgets={"html": True})
    assert Ctx.of(other).html is False


def test_off_is_open_to_agents_and_recorded(ws, human, agent):
    from orch.core.events import read_events
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, agent).set_widgets_html(False)
    again = _reopen(ws)
    assert Ctx.of(again).html is False and "unsigned-setting" not in _codes(again)
    ev = [e for e in read_events(again) if e.kind == "setting.changed"][-1]
    assert ev.data == {"setting": "widgets.html", "value": False} and ev.actor.startswith("agent:")
    assert [e["value"] for e in ledger.entries(again) if e["kind"] == "setting"] == [True]  # an agent signs nothing


def test_human_off_is_signed_and_wins_over_an_earlier_on(ws, human, configure):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, human).set_widgets_html(False)
    assert [e["value"] for e in ledger.entries(ws) if e["kind"] == "setting"] == [True, False]
    assert Ctx.of(configure(widgets={"html": True})).html is False  # turning it back on by hand does not count


def test_dashboard_says_how_to_turn_it_on(dash):
    body = dash.get("/widgets").text
    assert "orch widget html on" in body
