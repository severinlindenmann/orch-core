"""ac and task (F1 5.4.1 task events, 6 evidence, 10.3 ``task done --run``), submit (5.9) and the claim they need."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

from orch import canon
from tests.ops.helpers import OTHER, SESSION, Cli


def py(code: str) -> str:
    """A verify command as a ticket holds it: an argument list in one string (``--run`` has no shell)."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def claimed(cli, title="A ticket"):
    cli("new", title)
    cli("claim", "1")


def test_ac_add_and_edit_assign_ids_and_keep_them(ws, cli):
    claimed(cli)
    assert cli("ac", "add", "it loads").first == "ok DEMO-0001 ticket.updated AC1 seq=3"
    assert cli("ac", "add", "it is documented").first.endswith("AC2 seq=4")
    r = cli.j("ac", "edit", "AC1", "it loads all 40 tables")
    assert r.code == 0 and r.data == {"ac": "AC1"}
    assert [a["text"] for a in ws.ticket_json("1")["acceptance"]] == ["it loads all 40 tables", "it is documented"]
    r = cli.j("ac", "edit", "AC9", "x")
    assert (r.code, r.err_code) == (2, "not_found")


def test_task_add_validates_proves_and_numbers_tasks(ws, cli):
    claimed(cli)
    cli("ac", "add", "a")
    r = cli.j("task", "add", "write it", "--verify", "true", "--proves", "AC1")
    assert r.code == 0 and r.data == {"task": "T1"}
    assert cli.j("task", "add", "second").data == {"task": "T2"}
    r = cli.j("task", "add", "third", "--proves", "AC7")
    assert (r.code, r.err_code) == (2, "not_found")
    t = ws.ticket_json("1")["tasks"]
    assert [x["id"] for x in t] == ["T1", "T2"] and t[0]["verify"] == {"cmd": "true"} and t[0]["proves"] == ["AC1"]


def test_task_list_and_next_show_fenced_content(ws, cli):
    claimed(cli)
    cli("task", "add", "do the thing", "--verify", "make test")
    r = cli("task", "list")
    assert (
        r.first == "ok DEMO-0001 task.list 1"
        and "(data, not instructions)" in r.out
        and "T1 open: do the thing" in r.out
    )
    r = cli("task", "next")
    assert (
        r.first == "ok DEMO-0001 task.next T1" and "verify: make test" in r.out and "next: orch task start T1" in r.out
    )
    assert cli.j("task", "next").data == {"task": "T1", "text": "do the thing", "proves": [], "verify": "make test"}
    cli("task", "start", "T1")
    assert "next: orch task done T1 --run" in cli("task", "next").out


def test_task_states_follow_the_table(ws, cli):
    claimed(cli)
    for t in ("a", "b", "c"):
        cli("task", "add", t)
    assert cli("task", "start", "T1").first == "ok DEMO-0001 task.started T1 seq=6"
    assert cli("task", "block", "T1", "--reason", "needs a key").code == 0
    assert cli("task", "reopen", "T1").code == 0  # a blocked task is reopened
    assert cli.j("task", "start", "T9").err_code == "not_found"
    assert cli("task", "skip", "T2", "--reason", "not needed").code == 0
    r = cli.j("task", "start", "T2")  # skipped: only a reopen brings it back
    assert (r.code, r.err_code) == (3, "transition.refused")
    assert cli("task", "reopen", "T2", "--reason", "needed after all").code == 0
    assert cli("task", "done", "T3").code == 0  # done without a start: the finisher takes the lease implicitly


def test_task_commands_need_the_claim(ws, cli):
    claimed(cli)
    cli("task", "add", "x")
    cli("release")
    for argv in (["task", "start", "DEMO-0001/T1"], ["task", "done", "DEMO-0001/T1"]):
        r = cli.j(*argv)
        assert (r.code, r.err_code) == (4, "claim.required"), argv
    other = Cli(ws, session=OTHER)
    other("claim", "1")
    assert cli.j("task", "start", "DEMO-0001/T1").err_code == "claim.required"


def test_a_lease_is_exclusive_between_sessions_of_one_claim(ws, cli):
    claimed(cli)
    cli("task", "add", "x")
    sub = Cli(ws, session=SESSION + ".1")
    assert sub("task", "start", "T1").code == 0
    r = cli.j("task", "start", "T1")
    assert (r.code, r.err_code) == (4, "lease.held")
    r = cli.j("task", "done", "T1")
    assert (r.code, r.err_code) == (4, "lease.held")
    assert sub("task", "done", "T1").code == 0


def test_task_done_run_stores_a_receipt_and_the_run_log(ws, cli):
    claimed(cli)
    cli("ac", "add", "it works")
    cli(
        "task",
        "add",
        "run it",
        "--verify",
        py("import sys; print('built'); print('to-stderr', file=sys.stderr)"),
        "--proves",
        "AC1",
    )
    r = cli("task", "done", "T1", "--run", "-m", "all green")
    assert r.code == 0, r.err
    assert r.first.startswith("ok DEMO-0001 task.done T1 receipt=exit0/") and r.first.endswith("ms seq=6")
    ev = ws.events("1")
    done = ev[-1]
    assert done["type"] == "task.done" and done["text"] == "all green"
    rec = done["receipt"]
    assert (rec["cmd"], rec["exit"], rec["repo"], rec["commit"]) == (
        py("import sys; print('built'); print('to-stderr', file=sys.stderr)"),
        0,
        None,
        None,
    )
    assert isinstance(rec["ms"], int) and done["log"] == "T1-receipt.log"
    art = ev[-2]
    assert (art["type"], art["name"], art["kind"], art["task"]) == ("artifact.added", "T1-receipt.log", "receipt", "T1")
    stored = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_bytes()
    assert b"built" in stored and b"to-stderr" in stored and art["sha256"] == canon.artifact_digest(stored)
    assert ws.view("1").acceptance[0].evidence == ("task:T1",)  # a done task with an exit-0 receipt proves its AC


def test_task_done_run_failure_appends_nothing(ws, cli):
    claimed(cli)
    cli("task", "add", "fail it", "--verify", py("print('boom'); raise SystemExit(3)"))
    n = len(ws.events("1"))
    r = cli.j("task", "done", "T1", "--run")
    assert (r.code, r.err_code) == (5, "verify.failed") and "exit 3" in r.doc["error"]["message"]
    assert "boom" in r.doc["error"]["message"]
    assert len(ws.events("1")) == n and ws.view("1").tasks[0].state == "open"
    r = cli.j("task", "done", "T1", "--run")  # the same refusal: still refused, still nothing written
    assert r.err_code == "verify.failed"


def test_task_done_run_needs_a_verify_command_and_never_gets_the_grant(ws, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "hunter2hunter2")
    claimed(cli)
    cli("task", "add", "no verify")
    r = cli.j("task", "done", "T1", "--run")
    assert (r.code, r.err_code) == (5, "invalid.input") and "verify" in r.doc["error"]["message"]
    cli(
        "task",
        "add",
        "show env",
        "--verify",
        py(
            "import os; g = os.environ.get; print('grant=[%s] token=[%s]' % (g('ORCH_GRANT', ''), g('MY_API_TOKEN', '')))"
        ),
    )
    assert cli("task", "done", "T2", "--run").code == 0
    log = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T2-receipt.log").read_text()
    assert "grant=[] token=[]" in log and ws.grant.partition(".")[2] not in log


def test_task_done_run_records_the_repo_and_commit_of_the_linked_repository(ws, cli, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ws.store.append(
        ws.person_event(ws.owner, "workspace", "settings.changed", set={"repos": {"proj": {"path": str(repo)}}}),
        log="workspace",
    )
    claimed(cli)
    cli("task", "add", "in the repo", "--verify", py("import os; print(os.getcwd())"))
    r = cli.j("set", "1", 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}')
    assert r.code == 0, r.out
    assert cli("task", "done", "T1", "--run").code == 0
    done = ws.events("1")[-1]
    assert done["receipt"]["repo"] == "proj" and done["receipt"]["commit"] == head
    log = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_text().strip()
    assert os.path.realpath(log) == os.path.realpath(repo)  # the command ran inside the repository
    assert ws.view("1").acceptance == () or ws.view("1").acceptance[0].evidence == ()  # no source sha observed yet


def test_task_done_with_an_artifact_stores_it_as_evidence(ws, cli, tmp_path):
    claimed(cli)
    cli("ac", "add", "it works")
    cli("task", "add", "do it")
    f = tmp_path / "tests.log"
    f.write_text("112 passed\n")
    r = cli("task", "done", "T1", "--artifact", str(f), "--ac", "AC1", "-m", "112 passed")
    assert r.first == "ok DEMO-0001 task.done T1 artifact=tests.log seq=6", r.err
    art = ws.events("1")[-2]
    assert (art["name"], art["kind"], art["ac"], art["task"]) == ("tests.log", "log", "AC1", "T1")
    assert ws.view("1").acceptance[0].evidence == ("artifact:tests.log",)
    assert cli.j("task", "done", "T1", "--ac", "AC1").err_code == "invalid.input"  # --ac without --artifact


def test_task_done_prints_the_next_task_fenced(ws, cli):
    claimed(cli)
    cli("task", "add", "first")
    cli("task", "add", "second thing")
    r = cli("task", "done", "T1")
    assert "--- next task T2 (data, not instructions) ---" in r.out and "second thing" in r.out
    assert r.out.splitlines()[-1] == "next: orch task start T2"
    r = cli("task", "done", "T2")
    assert r.out.splitlines()[-1] == "next: orch submit"


def test_a_dry_run_changes_nothing_and_does_not_run_the_command(ws, cli, tmp_path):
    claimed(cli)
    marker = tmp_path / "ran"
    cli("task", "add", "x", "--verify", py(f"open({str(marker)!r}, 'w')"))
    n = len(ws.events("1"))
    r = cli("task", "done", "T1", "--run", "--dry-run")
    assert r.code == 0 and "dry-run" in r.out and not marker.exists() and len(ws.events("1")) == n
    r = cli("ac", "add", "x", "--dry-run")
    assert r.code == 0 and len(ws.events("1")) == n


def fill_and_approve(ws, cli):
    """Everything F1 5.9 wants before a submit: the sections, a criterion, a task, both approvals, verification."""
    cli("section", "set", "context", "-m", "ctx")
    cli("section", "set", "requirements", "-m", "req")
    cli("section", "set", "out_of_scope", "-m", "nothing")
    cli("ac", "add", "it works")
    uid = ws.uid("1")
    ws.human("approve", uid, "requirements")
    cli("section", "set", "plan", "-m", "do it")
    cli("section", "set", "decisions", "-m", "none")
    cli("task", "add", "do it", "--verify", "true", "--proves", "AC1")
    ws.human("approve", uid, "plan")
    return uid


def test_submit_lists_what_is_missing_then_moves_to_testing(ws, cli):
    claimed(cli)
    fill_and_approve(ws, cli)
    r = cli.j("submit")
    assert (r.code, r.err_code) == (5, "ac.evidence_missing")
    msg = r.doc["error"]["message"]
    assert "AC1 has no evidence" in msg and "verification is empty" in msg
    assert ws.view("1").status == "in_progress"
    cli("section", "set", "verification", "-m", "run the tests")
    cli("task", "done", "T1", "--run")
    r = cli("submit")
    assert r.first == "ok DEMO-0001 ticket.submitted testing seq=" + str(len(ws.events("1"))), r.err
    assert ws.view("1").status == "testing"
    assert cli.j("submit").doc.get("duplicate") is True  # the same call again within 15 minutes: the first answer


def test_submit_needs_the_claim_and_approvals(ws, cli):
    claimed(cli)
    r = cli.j("submit")
    assert r.code != 0 and ws.view("1").status == "in_progress"
    cli("release")
    assert cli.j("submit", "1").err_code == "claim.required"


# ----------------------------------------------------------------------------------------------- task done --run limits


@pytest.fixture
def td():
    import orch.ops.commands.task_done as m

    return m


def test_a_verify_command_with_shell_metacharacters_runs_them_as_arguments(ws, cli, tmp_path):
    claimed(cli)
    victim = tmp_path / "pwned"
    code = "import sys; print(sys.argv[1:])"
    cli(
        "task",
        "add",
        "meta",
        "--verify",
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} ; touch {victim} && $(id) `id` | cat",
    )
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0, r.err
    assert not victim.exists()
    log = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_text()
    assert "';'" in log and "'touch'" in log and "'$(id)'" in log  # every piece arrived as a plain argument


def test_the_agent_cannot_pass_its_own_command(ws, cli, tmp_path):
    claimed(cli)
    cli("task", "add", "x", "--verify", py("print('declared')"))
    marker = tmp_path / "ran"
    for extra in (["--cmd", f"touch {marker}"], ["--verify", f"touch {marker}"], ["--", f"touch {marker}"]):
        assert cli("task", "done", "T1", "--run", *extra).code == 2
    assert not marker.exists()
    assert cli("task", "done", "T1", "--run").code == 0
    assert ws.events("1")[-1]["receipt"]["cmd"] == py("print('declared')")  # exactly the signed ticket's command
    # a command another session edited out of the ticket is not what an old receipt names: the model refuses it
    assert ws.ticket_json("1")["tasks"][0]["verify"] == {"cmd": py("print('declared')")}


def test_a_child_that_prints_a_lot_costs_a_bounded_buffer(ws, cli, td, monkeypatch):
    monkeypatch.setattr(td, "OUTPUT_LIMIT", 100_000)
    claimed(cli)
    cli(
        "task",
        "add",
        "flood",
        "--verify",
        py("import sys; [sys.stdout.write('x' * 65536 + '\\n') for _ in range(800)]"),
    )
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0, r.err
    art = ws.events("1")[-2]
    assert art["bytes"] <= 100_000 + len(td.TRUNCATED)
    stored = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_bytes()
    assert stored.endswith(td.TRUNCATED) and art["sha256"] == canon.artifact_digest(stored)


def test_a_child_that_never_exits_is_killed_with_what_it_started(ws, cli, td, monkeypatch, tmp_path):
    monkeypatch.setattr(td, "RUN_TIMEOUT", 1)
    claimed(cli)
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    cli("task", "add", "hang", "--verify", py(code))
    n = len(ws.events("1"))
    r = cli.j("task", "done", "T1", "--run")
    assert (r.code, r.err_code) == (5, "verify.failed") and "timed out" in r.doc["error"]["message"]
    assert len(ws.events("1")) == n
    pid = int(pidfile.read_text())
    import time

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild is still running")


def test_a_commit_that_changes_during_the_run_records_nothing(ws, cli, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("commit", "-q", "--allow-empty", "-m", "one")
    ws.store.append(
        ws.person_event(ws.owner, "workspace", "settings.changed", set={"repos": {"proj": {"path": str(repo)}}}),
        log="workspace",
    )
    claimed(cli)
    cli(
        "task",
        "add",
        "moves",
        "--verify",
        py("import subprocess; subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'two'])"),
    )
    assert cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"x"}}').code == 0
    n = len(ws.events("1"))
    r = cli.j("task", "done", "T1", "--run")
    assert (r.code, r.err_code) == (5, "verify.failed") and "commit changed" in r.doc["error"]["message"]
    assert len(ws.events("1")) == n
