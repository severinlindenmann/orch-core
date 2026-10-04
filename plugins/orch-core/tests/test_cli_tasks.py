import json

import pytest

from orch import actor
from orch.cli import run


@pytest.fixture
def cli(monkeypatch, ws_root, capsys):
    class Cli:
        def agent(self, session="s-1"):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", session)
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)

        def __call__(self, *args):
            capsys.readouterr()
            code = run(list(args))
            out = capsys.readouterr()
            return code, out.out, out.err

    c = Cli()
    c.agent()
    return c


@pytest.fixture
def claimed(cli, aops, hops):
    t = aops.new("Migrate jobs")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] all jobs on serverless")
    cli.human()
    hops.approve(t.id, "requirements")
    cli.agent()
    assert cli("claim", t.id)[0] == 0
    return t.id


def test_add_from_file_list_and_json(cli, claimed, tmp_path, plan_approved):
    f = tmp_path / "tasks.yaml"
    f.write_text("tasks:\n  - text: Inventory the jobs\n    refs: [ac:1]\n"
                 "  - text: Switch them\n    verify: databricks bundle deploy -t dev\n    needs: [T1]\n"
                 "  - text: Grant SELECT\n    owner: human\n", encoding="utf-8")
    assert cli("task", "add", claimed, "--file", str(f))[0] == 0
    plan_approved(claimed)
    assert cli("task", "start", claimed, "T1")[0] == 0
    code, out, _ = cli("task", "list", claimed)
    assert code == 0
    assert out.splitlines()[0] == f"{claimed} · Migrate jobs · in-progress · 0 of 3 closed (0 done, 0 skipped)"
    assert "◐ T1  Inventory the jobs   ← doing" in out
    assert 'ref     ac:1 "all jobs on serverless"' in out
    assert "○ T2  Switch them   needs T1" in out
    assert "○ T3  Grant SELECT   human" in out
    assert "next: T1 (doing) · testing needs T1, T2, T3 closed" in out
    data = json.loads(cli("task", "list", claimed, "--json")[1])
    assert (data["format"], data["doing"], data["open"]) == ("orch.tasks.v1", "T1", ["T1", "T2", "T3"])
    assert json.loads(cli("show", claimed, "--json")[1])["tasks"]["summary"]["total"] == 3


def test_exit_codes(cli, claimed, plan_approved):
    assert cli("task", "add", claimed, "a", "--verify", "pytest -q")[0] == 0
    plan_approved(claimed)
    assert cli("task", "add", claimed, "grant", "--owner", "human")[0] == 0
    assert cli("task", "done", claimed, "T9")[0] == 2               # unknown task
    assert cli("task", "start", claimed, "T1")[0] == 0
    assert cli("task", "done", claimed, "T1")[0] == 2               # verify needs -m
    assert cli("task", "done", claimed, "T2", "-m", "x")[0] == 3    # the human's task
    assert cli("task", "add", claimed, "b", "--needs", "T7")[0] == 5
    cli.agent(session="s-2")
    assert cli("task", "done", claimed, "T1", "-m", "ok")[0] == 4   # not this session's claim
    cli.human()
    assert cli("task", "done", claimed, "T1", "-m", "ok")[0] == 3   # decision 4
    assert cli("task", "skip", claimed, "T1", "-m", "done elsewhere")[0] == 0
    assert cli("task", "done", claimed, "T2", "-m", "granted")[0] == 0


def test_add_needs_text_or_file(cli, claimed):
    assert cli("task", "add", claimed)[0] == 2


def test_edit_block_reopen(cli, claimed):
    assert cli("task", "add", claimed, "a", "--ref", "ac:1")[0] == 0
    assert cli("task", "edit", claimed, "T1", "--text", "a2", "--drop-ref", "ac:1", "--ref", "file:hub/x.py",
               "--verify", "pytest")[0] == 0
    assert cli("task", "block", claimed, "T1", "-m", "waits", "--on", "Q9")[0] == 2
    assert cli("task", "block", claimed, "T1")[0] == 2
    assert cli("task", "block", claimed, "T1", "-m", "waits for access", "--on", "DEMO-0042")[0] == 0
    code, out, _ = cli("task", "list", claimed)
    assert "▲ T1  a2   blocked on DEMO-0042: waits for access" in out
    assert cli("task", "reopen", claimed, "T1", "-m", "access granted")[0] == 0
    t1 = json.loads(cli("task", "list", claimed, "--json")[1])["tasks"][0]
    assert (t1["text"], t1["state"], t1["verify"], [r["target"] for r in t1["refs"]]) == ("a2", "todo", "pytest", ["hub/x.py"])


def test_session_start_names_the_current_task(cli, claimed, ws, plan_approved):
    from orch.hooks.session_start import session_start_text
    assert cli("task", "add", claimed, "a")[0] == 0
    plan_approved(claimed)
    assert f"{claimed} (in-progress, no tasks yet)" not in session_start_text(ws, "s-1")
    assert cli("task", "start", claimed, "T1")[0] == 0
    assert f"{claimed} (in-progress, doing T1)" in session_start_text(ws, "s-1")


def test_list_on_a_broken_section_exits_6_with_the_json_error(cli, put):
    tid = put("in-progress", sections={"Tasks": "- [ ] T1 a\nfree text"})
    code, out, _ = cli("task", "list", tid, "--json")
    assert code == 6 and json.loads(out)["error"].startswith("Tasks line 2") and json.loads(out)["line"] == 2
    code, out, _ = cli("task", "list", tid)
    assert code == 6 and "Tasks line 2" in out


def test_cli_and_dashboard_share_the_skipped_glyph():
    pytest.importorskip("fastapi")
    from orch.cli_task import GLYPH
    from orch.dashboard.data import tasks as tasks_data
    assert GLYPH["skipped"] == tasks_data.GLYPH["skipped"] == "–" and GLYPH["todo"] == "○"
    assert GLYPH == tasks_data.GLYPH


def test_done_run_keeps_a_receipt(cli, claimed, plan_approved, ws_root, tmp_path, monkeypatch):
    assert cli("task", "add", claimed, "prove it", "--verify", "echo from-cli")[0] == 0
    assert cli("task", "add", claimed, "fails", "--verify", "exit 7")[0] == 0
    plan_approved(claimed)
    monkeypatch.setenv("ORCH_HOME", str(ws_root / "orchestrator"))
    elsewhere = tmp_path / "checkout"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the run happens where the agent stands, not in the workspace
    assert cli("task", "start", claimed, "T1")[0] == 0
    code, out, _ = cli("task", "done", claimed, "T1", "--run")
    assert code == 0 and "receipt receipt-T1-" in out
    assert cli("task", "start", claimed, "T2")[0] == 0
    code, out, err = cli("task", "done", claimed, "T2", "--run")
    assert code == 5 and "exit 7" in out + err  # a validation refusal, like any other
