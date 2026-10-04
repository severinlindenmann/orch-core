"""`orch migrate`: every rule, the hash-safety rule, idempotency and the error for an unmigrated file. Workspaces live
in tmp_path only; nothing here reads the real config dir or a real workspace."""
import json
from pathlib import Path

import pytest

from orch.cli import run
from orch.core import gates, migrate, store
from orch.core.model import new_ticket, parse_ticket, render_ticket
from orch.errors import TicketParseError


@pytest.fixture
def make(ws_root):
    """Write a ticket file the way an older orch would have (any section, any status)."""
    home = ws_root / "orchestrator"
    n = [0]

    def _make(status="backlog", sections=None, **meta):
        n[0] += 1
        t = new_ticket(f"L-{n[0]:04d}", "Old ticket", type="feature", priority="normal", size="m", created="2026-01-01T09:00Z")
        t.meta["status"] = status
        t.meta.update(meta)
        t.sections.update({k: v for k, v in (sections or {}).items()})
        path = home / "tickets" / status / f"{t.id}-old-ticket.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_ticket(t), encoding="utf-8")
        return t.id, path

    return _make


def config(ws_root, **over):
    from conftest import make_config
    (ws_root / "orchestrator" / "config.json").write_text(json.dumps(make_config(**over), indent=2) + "\n", encoding="utf-8")


def read(path) -> "object":
    return parse_ticket(Path(path).read_text(encoding="utf-8"), "x", allow_old=True)


def snapshot(home: Path) -> dict:
    return {p.relative_to(home).as_posix(): p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file() and ".state" not in p.parts}


def migrate_all(ws_root):
    result = migrate.plan(ws_root / "orchestrator")
    migrate.apply(result)
    return result


def approve(path, gate, *, via="dashboard"):
    """What an approval left in the file: the gate record with the hash of the text as it stands."""
    t = read(path)
    t.meta["gates"][gate] = {"approved": "2026-01-02T09:00Z", "via": via, "hash": gates.gate_hash(t, gate), "hash_v": gates.HASH_VERSION}
    Path(path).write_text(render_ticket(t), encoding="utf-8")
    return t


# -- Proposal and Decisions -------------------------------------------------------------------------------

def test_proposal_and_decisions_move_into_context(ws_root, make):
    tid, path = make(sections={"Context": "Background.", "Proposal": "Use option B.", "Decisions": "- B, because it is cheaper",
                               "Plan": "1. do it"})
    result = migrate_all(ws_root)
    t = read(path)
    assert result.items[0].rules == ["proposal-decisions"]
    assert "Proposal" not in t.sections and "Decisions" not in t.sections
    assert t.section("Context") == ("Background.\n\n### Proposal (migrated)\n\nUse option B.\n\n"
                                    "### Decisions (migrated)\n\n- B, because it is cheaper")
    assert t.section("Plan") == "1. do it"
    parse_ticket(path.read_text(encoding="utf-8"), "x")  # loads normally now


def test_proposal_without_context_creates_it(ws_root, make):
    _, path = make(sections={"Proposal": "Use option B."})
    migrate_all(ws_root)
    assert read(path).section("Context") == "### Proposal (migrated)\n\nUse option B."


def test_proposal_with_a_widget_block_is_refused(ws_root, make):
    block = "```orch\n{\"id\": \"w1\", \"widget\": \"callout\"}\n```"
    _, path = make(sections={"Context": "x", "Proposal": block})
    before = path.read_bytes()
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items and result.refused[0][1] == "proposal-decisions" and "widget" in result.refused[0][2]
    assert path.read_bytes() == before


def test_an_unmigrated_ticket_fails_to_load_and_says_how_to_migrate(ws_root, make):
    _, path = make(sections={"Proposal": "Use option B."})
    with pytest.raises(TicketParseError, match="orch migrate"):
        parse_ticket(path.read_text(encoding="utf-8"), "tickets/backlog/x.md")
    entry = store.scan(__import__("orch.core.workspace", fromlist=["Workspace"]).Workspace.open(ws_root))[0]
    assert entry.meta is None and "orch migrate" in entry.error


def test_an_empty_old_heading_is_not_an_error(ws_root, make):
    _, path = make(sections={"Proposal": ""})
    path.write_text(path.read_text(encoding="utf-8") + "\n## Proposal\n", encoding="utf-8")
    parse_ticket(path.read_text(encoding="utf-8"), "x")


# -- Plan checklist -> tasks --------------------------------------------------------------------------------

PLAN = "Intro.\n- [x] Inventory `hub/jobs/` first\n- [ ] Migrate\n  the gold jobs\n- [ ] Compare costs"


def test_plan_checklist_becomes_tasks_and_the_plan_stays(ws_root, make):
    config(ws_root, git={"repos": {"hub": {"path": "hub"}}})
    _, path = make("in-progress", sections={"Plan": PLAN})
    migrate_all(ws_root)
    t = read(path)
    assert t.section("Plan") == PLAN
    assert t.section("Tasks") == ("- [x] T1 Inventory `hub/jobs/` first\n  - ref: file:hub/jobs/\n"
                                  "- [ ] T2 Migrate the gold jobs\n- [ ] T3 Compare costs")


def test_plan_checklist_only_for_tickets_being_worked(ws_root, make):
    paths = [make(s, sections={"Plan": PLAN})[1] for s in ("backlog", "open", "done")]
    before = [p.read_bytes() for p in paths]
    assert migrate.plan(ws_root / "orchestrator").empty()
    assert [p.read_bytes() for p in paths] == before


def test_plan_checklist_never_replaces_existing_tasks(ws_root, make):
    _, path = make("in-progress", sections={"Plan": PLAN, "Tasks": "- [ ] T1 mine"})
    assert migrate.plan(ws_root / "orchestrator").empty()


def test_testing_ticket_with_open_checklist_items_is_refused(ws_root, make):
    make("testing", sections={"Plan": PLAN})
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items and result.refused[0][1] == "plan-checklist" and "open items" in result.refused[0][2]


def test_testing_ticket_with_a_finished_checklist_gets_done_tasks(ws_root, make):
    _, path = make("testing", sections={"Plan": "- [x] a\n- [x] b"})
    migrate_all(ws_root)
    assert read(path).section("Tasks") == "- [x] T1 a\n- [x] T2 b"


def test_task_numbers_never_reuse_ones_the_event_log_named(ws_root, make):
    tid, path = make("in-progress", sections={"Plan": "- [ ] a"})
    from orch.core.events import Actor, append_event
    from orch.core.workspace import Workspace
    append_event(Workspace.open(ws_root), tid, "task.added", Actor("agent", "a", "cli"), {"tasks": ["T1", "T2"]})
    migrate_all(ws_root)
    assert read(path).section("Tasks").startswith("- [ ] T3 a")


# -- artifact paths -----------------------------------------------------------------------------------------

def test_old_artifact_paths_become_current_links(ws_root, make):
    text = ("![shot](../artifacts/L-0001/shot.png) and [report](./artifacts/L-0001/r%201.html?x=1#top) and "
            "[other](../artifacts/L-0009/o.csv)\n\n[ref]: ../artifacts/L-0001/ref.png\n\n```\n![code](../artifacts/L-0001/c.png)\n```")
    _, path = make("backlog", sections={"Context": text, "Log": "- 2026-01-01T09:00Z [a] saw ![x](../artifacts/L-0001/x.png)"})
    migrate_all(ws_root)
    t = read(path)
    assert t.section("Context") == ("![shot](artifact:shot.png) and [report](artifact:r%201.html?x=1#top) and "
                                    "[other](/a/L-0009/o.csv)\n\n[ref]: artifact:ref.png\n\n```\n![code](../artifacts/L-0001/c.png)\n```")
    assert "../artifacts/L-0001/x.png" in t.section("Log")  # history is not rewritten


def test_artifact_paths_in_an_approved_gate_are_refused_and_the_file_is_untouched(ws_root, make):
    _, path = make("open", sections={"Requirements": "see ![a](../artifacts/L-0001/a.png)", "Acceptance criteria": "- [ ] x"})
    approve(path, "requirements")
    before = path.read_bytes()
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items
    assert result.refused[0][:2] == (result.refused[0][0], "artifact-paths") and "re-approval" in result.refused[0][2]
    migrate.apply(result)
    assert path.read_bytes() == before


def test_artifact_paths_in_unapproved_gated_text_are_rewritten(ws_root, make):
    _, path = make("backlog", sections={"Requirements": "see ![a](../artifacts/L-0001/a.png)"})
    migrate_all(ws_root)
    assert read(path).section("Requirements") == "see ![a](artifact:a.png)"


def test_artifact_paths_outside_the_bound_text_keep_approvals_valid(ws_root, make):
    _, path = make("open", sections={"Requirements": "r", "Acceptance criteria": "- [ ] x", "Plan": "p",
                                     "Context": "![a](../artifacts/L-0001/a.png)"})
    approve(path, "requirements")
    approve(path, "plan")
    migrate_all(ws_root)
    t = read(path)
    assert t.section("Context") == "![a](artifact:a.png)"
    assert gates.gate_state(t, "requirements") == "approved" and gates.gate_state(t, "plan") == "approved"


def test_a_verdict_text_of_a_testing_ticket_is_not_rewritten(ws_root, make):
    _, path = make("testing", sections={"Verification": "![run](../artifacts/L-0001/run.png)", "Context": "![c](../artifacts/L-0001/c.png)"})
    result = migrate.plan(ws_root / "orchestrator")
    assert [r for _, r, _ in result.refused] == ["artifact-paths"]
    assert not result.items  # the Context rewrite is part of the same rule: all or nothing per rule


def test_old_relative_artifact_paths_no_longer_resolve(ws_root):
    from orch.core import artifacts
    assert artifacts.artifact_path("../artifacts/L-0001/a.png") is None and artifacts.artifact_path("artifacts/L-0001/a.png") is None
    assert artifacts.artifact_path("/a/L-0001/a.png") == ("L-0001", "a.png")


# -- trackers -----------------------------------------------------------------------------------------------

def test_bare_tracker_gets_its_prefix_and_so_do_the_external_keys(ws_root, make):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "\\d+", "url": "https://example.test/issues/{key}"}])
    _, path = make(external=[{"key": "12", "url": "https://example.test/issues/12"}, {"key": "ABC-1", "url": None}])
    migrate_all(ws_root)
    cfg = json.loads((ws_root / "orchestrator" / "config.json").read_text(encoding="utf-8"))
    assert cfg["external_trackers"] == [{"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://example.test/issues/{id}"}]
    assert read(path).meta["external"] == [{"key": "GH-12", "url": "https://example.test/issues/12"}, {"key": "ABC-1", "url": None}]


def test_tracker_without_a_prefix_is_refused_and_nothing_changes(ws_root, make):
    config(ws_root, external_trackers=[{"pattern": "\\d+", "url": "https://example.test/{key}"}])
    _, path = make(external=[{"key": "12", "url": None}])
    snap = snapshot(ws_root / "orchestrator")
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items and result.refused[0][:2] == ("config.json", "trackers") and "prefix" in result.refused[0][2]
    migrate.apply(result)
    assert snapshot(ws_root / "orchestrator") == snap


def test_two_bare_trackers_make_a_bare_key_ambiguous(ws_root, make):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "\\d+", "url": "https://a.test/{key}"},
                                       {"prefix": "BB", "pattern": "[0-9]+", "url": "https://b.test/{key}"}])
    make(external=[{"key": "12", "url": None}])
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items and "GH or BB" in result.refused[0][2]


def test_unrecognised_bare_pattern_is_refused(ws_root):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "#?\\d+", "url": "https://a.test/{key}"}])
    result = migrate.plan(ws_root / "orchestrator")
    assert not result.items and "by hand" in result.refused[0][2]


def test_bare_keys_under_an_already_prefixed_tracker_are_prefixed(ws_root, make):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://a.test/issues/{id}"}])
    _, path = make(external=[{"key": "12", "url": None}])
    migrate_all(ws_root)
    assert read(path).meta["external"] == [{"key": "GH-12", "url": "https://a.test/issues/12"}]


def test_a_bare_key_two_prefixed_trackers_could_own_is_refused(ws_root, make):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://a.test/{id}"},
                                       {"prefix": "BB", "pattern": "BB-\\d+", "url": "https://b.test/{key}"}])
    make(external=[{"key": "12", "url": None}])
    assert migrate.plan(ws_root / "orchestrator").refused


def test_a_bare_tracker_is_a_config_error_and_never_links_numbers(ws, configure):
    from orch.core.check import run_checks
    ws = configure(external_trackers=[{"prefix": "GH", "pattern": "\\d+", "url": "https://a.test/{key}"}])
    assert any(f.code == "tracker-config" and "bare number" in f.message for f in run_checks(ws, emit_events=False))


# -- the whole run --------------------------------------------------------------------------------------------

def old_workspace(ws_root, make):
    config(ws_root, external_trackers=[{"prefix": "GH", "pattern": "\\d+", "url": "https://a.test/{key}"}])
    make(sections={"Proposal": "p", "Context": "![a](../artifacts/L-0001/a.png)"}, external=[{"key": "7", "url": None}])
    make("in-progress", sections={"Plan": "- [ ] a"})


def test_dry_run_is_the_default_and_writes_nothing(ws_root, make, capsys):
    old_workspace(ws_root, make)
    snap = snapshot(ws_root / "orchestrator")
    assert run(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "--- a/tickets/backlog/L-0001-old-ticket.md" in out and "would change" in out and "--apply" in out
    assert snapshot(ws_root / "orchestrator") == snap


def test_apply_then_a_second_run_is_a_no_op(ws_root, make, capsys):
    old_workspace(ws_root, make)
    assert run(["migrate", "--apply"]) == 0
    capsys.readouterr()
    snap = snapshot(ws_root / "orchestrator")
    assert run(["migrate", "--apply"]) == 0
    assert "nothing to migrate" in capsys.readouterr().out
    assert snapshot(ws_root / "orchestrator") == snap
    from orch.core.check import run_checks
    from orch.core.workspace import Workspace
    codes = {f.code for f in run_checks(Workspace.open(ws_root), emit_events=False)}
    assert not codes & {"needs-migration", "tracker-config", "parse"}


def test_check_reports_old_artifact_links_before_the_migration(ws_root, make):
    from orch.core.check import run_checks
    from orch.core.workspace import Workspace
    make(sections={"Context": "![a](../artifacts/L-0001/a.png)"})
    found = [f for f in run_checks(Workspace.open(ws_root), emit_events=False) if f.code == "needs-migration"]
    assert found and "orch migrate" in found[0].message


def test_a_workspace_with_nothing_old_is_a_no_op(ws_root, capsys):
    assert run(["migrate"]) == 0 and "nothing to migrate" in capsys.readouterr().out


def test_a_refusal_is_exit_5_and_the_safe_part_still_applies(ws_root, make, capsys):
    _, gated = make("open", sections={"Requirements": "![a](../artifacts/L-0001/a.png)"})
    approve(gated, "requirements")
    _, plain = make(sections={"Proposal": "p"})
    assert run(["migrate", "--apply"]) == 5
    assert "re-approval" in capsys.readouterr().out
    assert "Proposal" not in read(plain).sections and "../artifacts" in gated.read_text(encoding="utf-8")


def test_json_output(ws_root, make, capsys):
    old_workspace(ws_root, make)
    assert run(["migrate", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is False and {c["file"] for c in data["changes"]} >= {"config.json"}


def test_migration_never_touches_the_ledger_events_or_approved_snapshots(ws_root, make):
    _, path = make("open", sections={"Requirements": "r", "Acceptance criteria": "- [ ] x", "Context": "![a](../artifacts/L-0001/a.png)",
                                     "Proposal": "p"})
    approve(path, "requirements")
    state = ws_root / "orchestrator" / ".state"
    (state / "gates").mkdir(parents=True, exist_ok=True)
    (state / "gates" / "L-0001-requirements.md").write_text("snapshot\n", encoding="utf-8")
    (state / "events.jsonl").write_text("", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file() and p.name != "index.json"}
    migrate_all(ws_root)
    assert {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file() and p.name != "index.json"} == before
    assert "orch.core.ledger" not in Path(migrate.__file__).read_text(encoding="utf-8")


def test_the_old_import_command_is_gone(ws_root, capsys):
    assert run(["task", "import", "L-0001", "--from-plan"]) != 0
