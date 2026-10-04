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
    off = [e for e in ledger.entries(again) if e["kind"] == "setting"]
    assert [e["value"] for e in off] == [True, False] and off[-1]["actor"].startswith("agent:")  # an off is signed


def _hand_set(ws, on):
    """The config changed without orch (a hand edit, a pull or a merge)."""
    raw = json.loads((ws.home / "config.json").read_text())
    (ws.home / "config.json").write_text(json.dumps({**raw, "widgets": {"html": on}}))
    return _reopen(ws)


def test_an_agents_off_outranks_the_earlier_human_on(ws, human, agent):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, agent).set_widgets_html(False)
    pulled = _hand_set(ws, True)
    assert ledger.widgets_html_state(pulled) == "unsigned" and Ctx.of(pulled).html is False
    assert "unsigned-setting" in _codes(pulled)
    Ops(pulled, human).set_widgets_html(True)  # only a fresh human on counts
    assert Ctx.of(_reopen(ws)).html is True


def test_an_off_by_hand_leaves_the_signed_on_in_force_and_check_says_so(ws, human):
    Ops(ws, human).set_widgets_html(True)
    edited = _hand_set(ws, False)
    assert Ctx.of(edited).html is False and ledger.widgets_html_state(edited) == "stale"
    assert "orch widget html off" in _codes(edited)["stale-setting"].message


def test_a_truncated_off_still_counts_as_off(ws, human, agent):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, agent).set_widgets_html(False)
    path = ledger.ledger_path(ws)
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:-1]))  # the off entry removed
    assert Ctx.of(_hand_set(ws, True)).html is False  # the event log still records the off


def test_a_replayed_old_on_counts_as_no_decision(ws, human):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, human).set_widgets_html(False)
    path = ledger.ledger_path(ws)
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines) + lines[0])  # the old on appended again
    edited = _hand_set(ws, True)
    assert ledger.signed_setting(edited, "widgets.html") is None and Ctx.of(edited).html is False


def test_a_reordered_chain_counts_as_no_decision(ws, human):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, human).set_widgets_html(False)
    path = ledger.ledger_path(ws)
    on, off = path.read_text().splitlines(keepends=True)
    path.write_text(off + on)
    assert ledger.signed_setting(_reopen(ws), "widgets.html") is None


def test_same_customer_and_prefix_elsewhere_and_a_copy_stay_off(ws, human, tmp_path):
    import shutil
    from orch.core.workspace import Workspace
    Ops(ws, human).set_widgets_html(True)
    assert Ctx.of(_reopen(ws)).html is True
    copy = tmp_path / "copy"
    shutil.copytree(ws.root, copy)
    other = tmp_path / "other" / "orchestrator"
    other.mkdir(parents=True)
    (other / "config.json").write_text((ws.home / "config.json").read_text())
    for root in (copy, other.parent):
        moved = Workspace.open(root)
        assert ledger.workspace_id(moved) == ledger.workspace_id(ws)
        assert ledger.widgets_html_state(moved) == "unsigned" and Ctx.of(moved).html is False


def test_the_checkout_id_is_shared_by_worktrees_of_one_clone(tmp_path):
    import subprocess
    from types import SimpleNamespace
    main, tree = tmp_path / "main", tmp_path / "tree"
    main.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@e.test", "-C", str(main)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([*git, "worktree", "add", "-q", str(tree)], check=True)
    ids = [ledger.checkout_id(SimpleNamespace(home=r / "orchestrator", root=r)) for r in (main, tree, tmp_path)]
    assert ids[0] == ids[1] != ids[2]


def test_human_off_is_signed_and_wins_over_an_earlier_on(ws, human, configure):
    Ops(ws, human).set_widgets_html(True)
    Ops(ws, human).set_widgets_html(False)
    assert [e["value"] for e in ledger.entries(ws) if e["kind"] == "setting"] == [True, False]
    assert Ctx.of(configure(widgets={"html": True})).html is False  # turning it back on by hand does not count


def test_dashboard_says_how_to_turn_it_on(dash):
    body = dash.get("/widgets").text
    assert "orch widget html on" in body
