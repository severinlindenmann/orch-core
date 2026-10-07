"""Quick tasks (orch.core.quick): the switch, who may do what, the size limit, picking and `near`."""
import json
import subprocess

import pytest

from orch.core import quick
from orch.core.quick import QuickOps
from orch.errors import ClaimError, HumanOnlyError, NotFoundError, TransitionError, UsageError, ValidationError
from conftest import init_repo


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _commit(root, files: dict, subject: str):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", subject)


@pytest.fixture
def repo(ws_root):
    init_repo(ws_root)
    _git(ws_root, "config", "user.email", "t@example.com")
    _git(ws_root, "config", "user.name", "t")
    _commit(ws_root, {"README.md": "x\n"}, "init")
    return ws_root


def turn_on(ws, **settings):
    """The human enables the quick-tasks default addon (Workspace & addons), with these addon settings."""
    from orch.addons import userfiles
    userfiles.set_enabled(ws.root, quick.ADDON, True)
    if settings:
        userfiles.save_addon_config(ws.root, quick.ADDON, settings)
    return ws


@pytest.fixture
def on(ws):
    return turn_on(ws)


@pytest.fixture
def hq(ws, human):
    return QuickOps(ws, human)


@pytest.fixture
def aq(ws, agent):
    return QuickOps(ws, agent)


# -- the switch -----------------------------------------------------------------------------------------------------

def test_off_by_default(ws, hq):
    assert quick.settings(ws)["state"] == "off"
    with pytest.raises(TransitionError, match="off"):
        hq.add("Fix a typo")


def test_config_alone_does_not_turn_it_on(configure, human):
    ws = configure(quick={"enabled": True, "agents_add": True})  # keys from before the addon: ignored
    s = quick.settings(ws)
    assert s["state"] == "off" and not s["enabled"] and not s["agents_add"]
    with pytest.raises(TransitionError, match="off") as e:
        QuickOps(ws, human).add("Fix a typo")
    assert "quick-tasks" in e.value.hint


def test_the_addon_is_the_switch_and_holds_the_settings(ws):
    from orch.addons import userfiles
    turn_on(ws, agents_add=True, max_commits="2", max_files="8")
    s = quick.settings(ws)
    assert s["enabled"] and s["agents_add"] and (s["max_commits"], s["max_files"]) == (2, 8)
    userfiles.set_enabled(ws.root, quick.ADDON, False)
    s = quick.settings(ws)
    assert not s["enabled"] and not s["agents_add"]


def test_bad_addon_settings_fall_back_to_the_defaults(ws):
    turn_on(ws, max_commits="lots", max_files="99")
    s = quick.settings(ws)
    assert (s["max_commits"], s["max_files"]) == (1, 3)


def test_prefix_never_equals_the_ticket_prefix(configure):
    ws = configure(id={"prefix": "Q", "pad": 4})
    assert quick.prefix(ws) == "QT"


# -- adding and the rules -------------------------------------------------------------------------------------------

def test_human_adds_agent_does_not_unless_allowed(on, hq, aq, human):
    t = hq.add("  Fix the   typo in README ", area="README.md")
    assert t["id"] == "Q-1" and t["title"] == "Fix the typo in README" and t["area"] == "README.md"
    with pytest.raises(HumanOnlyError):
        aq.add("Something")
    turn_on(on, agents_add=True)
    assert aq.add("Something")["id"] == "Q-2"


def test_title_rules(on, hq):
    with pytest.raises(UsageError):
        hq.add("   ")
    with pytest.raises(ValidationError):
        hq.add("x" * 201)
    with pytest.raises(ValidationError):
        hq.add("bad‮text")
    with pytest.raises(UsageError):
        hq.add("ok", area="../outside")


def test_claim_done_flow_and_events(on, hq, aq):
    qid = hq.add("Bump ruff")["id"]
    aq.claim(qid)
    with pytest.raises(UsageError):
        aq.done(qid, "")  # an agent's note is required
    t = aq.done(qid, "bumped, lint clean")
    assert t["status"] == "done" and t["done"]["note"] == "bumped, lint clean" and t["claim"] is None
    from orch.core.events import read_events
    kinds = [e.kind for e in read_events(on) if e.data.get("quick") == qid]
    assert kinds == ["quick.added", "quick.claimed", "quick.done"]


def test_agent_done_needs_its_claim(on, hq, aq, other_agent):
    qid = hq.add("Bump ruff")["id"]
    with pytest.raises(ClaimError):
        aq.done(qid, "did it")
    aq.claim(qid)
    with pytest.raises(ClaimError):
        QuickOps(on, other_agent).claim(qid)
    with pytest.raises(ClaimError):
        QuickOps(on, other_agent).done(qid, "did it")


def test_one_quick_task_at_a_time(on, hq, aq):
    a, b = hq.add("one")["id"], hq.add("two")["id"]
    aq.claim(a)
    with pytest.raises(ClaimError, match="one quick task at a time"):
        aq.claim(b)
    aq.release(a)
    aq.claim(b)


def test_no_quick_task_while_the_ticket_is_in_progress(on, hq, aq, working):
    qid = hq.add("Typo")["id"]
    with pytest.raises(ClaimError, match="finish it first"):
        aq.claim(qid)


def test_agent_does_not_pick_up_its_own(on, human, hq, aq, other_agent):
    turn_on(on, agents_add=True)
    qid = aq.add("Found a dead import")["id"]
    with pytest.raises(ClaimError, match="filed by this session"):
        aq.claim(qid)
    assert qid not in [t["id"] for t in quick.pickable(on, aq.actor)]
    QuickOps(on, other_agent).claim(qid)


def test_human_only_reopen_and_drop(on, hq, aq):
    qid = hq.add("Typo")["id"]
    aq.claim(qid)
    aq.done(qid, "fixed")
    with pytest.raises(HumanOnlyError):
        aq.reopen(qid, "no")
    with pytest.raises(HumanOnlyError):
        aq.drop(qid)
    t = hq.reopen(qid, "the light theme got too pale")
    assert t["status"] == "open" and t["notes"][0]["text"] == "the light theme got too pale"
    assert hq.drop(qid)["status"] == "dropped"


def test_promote_files_a_backlog_ticket(on, hq, aq):
    qid = hq.add("Rename the flag", area="src/cli")["id"]
    t, ticket = aq.promote(qid)
    assert t["status"] == "moved" and t["ticket"] == ticket.id
    assert ticket.status == "backlog" and ticket.title == "Rename the flag"
    assert f"From quick task {qid}" in ticket.sections["Ask"]
    with pytest.raises(TransitionError):
        aq.promote(qid)


def test_artifacts(on, hq, aq, tmp_path):
    qid = hq.add("Contrast")["id"]
    shot = tmp_path / "after.png"
    shot.write_bytes(b"\x89PNG\r\n")
    with pytest.raises(ClaimError):
        aq.artifact_add(qid, shot)
    aq.claim(qid)
    t = aq.artifact_add(qid, shot, label="After")
    assert t["artifacts"][0]["name"] == "after.png" and t["artifacts"][0]["label"] == "After"
    assert (on.artifacts_dir / qid / "after.png").read_bytes() == b"\x89PNG\r\n"
    aq.artifact_add(qid, url="https://example.com/run/1")
    with pytest.raises(ValidationError, match="already exists"):
        aq.artifact_add(qid, shot)
    with pytest.raises(UsageError):
        aq.artifact_add(qid, url="javascript:alert(1)")


def test_unknown_and_normalized_refs(on, hq):
    hq.add("one")
    assert quick.load(on, "1")["id"] == quick.load(on, "q-001")["id"] == "Q-1"
    with pytest.raises(NotFoundError):
        quick.load(on, "Q-9")
    with pytest.raises(NotFoundError):
        quick.load(on, "L-0001")


# -- the size limit -------------------------------------------------------------------------------------------------

def test_done_refuses_past_the_limit_and_marks_it_outgrown(repo, on, hq, aq):
    qid = hq.add("Rename the flag")["id"]
    aq.claim(qid)
    _commit(repo, {f"src/f{i}.py": "x\n" for i in range(5)}, f"{qid} rename the flag")
    with pytest.raises(ValidationError, match="outgrew"):
        aq.done(qid, "renamed")
    t = quick.load(on, qid)
    assert t["status"] == "open" and t["outgrew"]["files_n"] == 5 and t["claim"] is None
    with pytest.raises(TransitionError):
        aq.claim(qid)
    hq.reopen(qid)  # the human lets it finish
    aq.claim(qid)
    assert aq.done(qid, "renamed")["status"] == "done"


def test_orch_records_and_small_changes_pass(repo, on, hq, aq):
    qid = hq.add("Typo")["id"]
    aq.claim(qid)
    _commit(repo, {"README.md": "y\n", "orchestrator/.state/quick/x.json": "{}"}, f"{qid} fix typo")
    assert quick.size(on, qid) == {"commits": quick.size(on, qid)["commits"], "files": ["README.md"]}
    assert aq.done(qid, "fixed")["status"] == "done"


def test_uncommitted_work_counts(repo, on, hq, aq):
    qid = hq.add("Typo")["id"]
    aq.claim(qid)
    for i in range(4):
        (repo / f"n{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="outgrew"):
        aq.done(qid, "fixed")


# -- picking: orch next and near -------------------------------------------------------------------------------------

def test_pickable_order_human_first_then_oldest(on, human, hq, aq, other_agent):
    turn_on(on, agents_add=True)
    a = QuickOps(on, other_agent).add("agent idea")["id"]
    b = hq.add("human one")["id"]
    c = hq.add("human two")["id"]
    assert [t["id"] for t in quick.pickable(on, aq.actor)] == [b, c, a]


def test_near_lists_tasks_in_the_files_of_the_ticket(repo, on, hq, aq, working):
    _commit(repo, {"dashboard/static/graph.css": "a\n", "docs/x.md": "b\n"}, f"{working} legend")
    hit = hq.add("Legend contrast", area="dashboard/static")["id"]
    hq.add("Unrelated", area="src/remote")
    hq.add("No area")
    data = quick.near(on, working, actor=aq.actor)
    assert [t["id"] for t in data["tasks"]] == [hit]
    assert "dashboard/static/graph.css" in data["files"]


# -- commit messages -------------------------------------------------------------------------------------------------

def test_commit_msg_accepts_an_open_quick_task(on, hq):
    from orch.hooks.commit_msg import check_message
    qid = hq.add("Typo")["id"]
    body = "\n\nWhat: typo\nWhy: readers\nRisk: none\n"
    assert check_message(on, f"{qid} fix the typo{body}") == []
    assert any("no quick task" in p for p in check_message(on, f"Q-77 fix{body}"))
    hq.drop(qid)
    assert any("dropped" in p for p in check_message(on, f"{qid} fix{body}"))


def test_commit_msg_without_quick_tasks_is_unchanged(ws):
    from orch.hooks.commit_msg import check_message
    assert any("subject must match" in p for p in check_message(ws, "Q-1 fix\n\nWhat: a\nWhy: b\nRisk: c\n"))


# -- the CLI ---------------------------------------------------------------------------------------------------------

def test_cli_next_falls_through_to_quick_tasks(on, hq, monkeypatch, capsys):
    from orch import cli
    monkeypatch.setattr(cli, "_workspace", None)
    hq.add("Typo")
    assert cli.run(["next", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == "Q-1" and rows[0]["quick"] is True


def test_cli_next_idle_prefers_tickets(on, hq, aops, hops, monkeypatch, capsys):
    from orch import cli
    monkeypatch.setattr(cli, "_workspace", None)
    t = aops.new("A ticket")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    hq.add("Typo")
    assert cli.run(["next", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(not r.get("quick") for r in rows)


def test_cli_list_and_human_only_commands_refuse_agents(on, hq, monkeypatch, capsys):
    from orch import cli
    monkeypatch.setattr(cli, "_workspace", None)
    hq.add("Typo")
    assert cli.run(["quick"]) == 0
    assert "Q-1" in capsys.readouterr().out
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert cli.run(["quick", "drop", "Q-1"]) != 0


# -- the guard -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["orch quick reopen Q-1", "uv run orch quick drop 3", "orch addon enable quick-tasks",
                                 "sh -c 'orch quick reopen Q-2'"])
def test_guard_refuses_human_only_quick_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert not d.allow


@pytest.mark.parametrize("cmd", ["orch quick", "orch quick claim Q-1", "orch quick done Q-1 -m 'fixed'",
                                 "orch quick near L-0001"])
def test_guard_lets_agent_quick_commands_through(ws, cmd):
    from orch.hooks.guard import evaluate
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


def test_guard_refuses_hand_edits_of_quick_task_files(on, hq):
    from orch.hooks.guard import evaluate
    qid = hq.add("Typo")["id"]
    path = quick.directory(on) / f"{qid}.json"
    d = evaluate(on, {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "open", "new_string": "done"}})
    assert not d.allow


def test_session_start_mentions_quick_tasks(on, hq):
    from orch.hooks.session_start import session_start_text
    hq.add("Typo")
    assert "Quick tasks: 1 open" in session_start_text(on, "abc")


def test_quick_records_are_durable_for_git():
    from orch.core.gitfiles import classify
    assert classify(".state/quick/Q-1.json") == "durable"
    assert classify(".state/quick-counter.json") == "durable"


def test_orch_check_counts_quick_task_artifacts(on, hq, aq, tmp_path):
    from orch.core.check import _check_orphan_artifacts
    qid = hq.add("Contrast")["id"]
    aq.claim(qid)
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    aq.artifact_add(qid, f)
    (on.artifacts_dir / "Z-9").mkdir()
    assert [x.ticket for x in _check_orphan_artifacts(on, [])] == ["Z-9"]


def test_activity_phrases_quick_events(on, hq):
    from orch.core.events import read_events
    from orch.dashboard.data.timeline import describe
    hq.add("Some <b>title</b>")
    (e,) = [e for e in read_events(on) if e.kind == "quick.added"]
    assert describe(e) == "added quick task Q-1"
