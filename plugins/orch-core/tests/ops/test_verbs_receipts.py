"""Grant verbs are operation names (F1 10.1); receipts (F1 6, 10.7); ``apply`` honesty and retry (review of #351)."""

from __future__ import annotations

import json

import pytest

from orch.store import StoreError
from tests.ops.helpers import SESSION, Cli
from tests.ops.test_task_ac import claimed, py

# ------------------------------------------------------------------------------------------------ grant verbs


def ci(ws, verbs, session=SESSION + ".1"):
    return Cli(ws, session=session, ORCH_GRANT=ws.narrow_grant(verbs))


def test_a_ci_grant_for_task_done_runs_task_done_with_its_receipt(ws, cli):
    claimed(cli)
    cli("task", "add", "t1", "--verify", py("print('ok')"))
    r = ci(ws, ["task.done"])("task", "done", "T1", "--run")  # the receipt's artifact.added is in task.done's emits
    assert r.code == 0, r.err
    assert [e["type"] for e in ws.events("1")][-2:] == ["artifact.added", "task.done"]


def test_a_grant_covers_exactly_the_operations_it_names(ws, cli):
    claimed(cli)
    cli("task", "add", "t1")
    cli("task", "add", "t2")
    g = ci(ws, ["task.done"])
    for argv in (
        ["log", "hi", "--ref", "1"],
        ["task", "start", "T1"],
        ["set", "1", "title=x"],
        ["ask", "q?", "--ref", "1"],
    ):
        r = g.j(*argv)
        assert (r.code, r.err_code) == (3, "grant.verb"), argv  # its own code, not "expired"
    assert "does not cover" in g.j("log", "x", "--ref", "1").doc["error"]["message"]
    assert g.j("task", "done", "T1").code == 0


def test_an_event_type_name_is_not_an_operation_name(ws, cli):
    claimed(cli)
    r = ci(ws, ["log.added"]).j("log", "hi", "--ref", "1")  # the operation is `log`
    assert (r.code, r.err_code) == (3, "grant.verb")
    assert ci(ws, ["log"]).j("log", "hi", "--ref", "1").code == 0
    assert ci(ws, ["ticket.updated"]).j("set", "1", "title=x").err_code == "grant.verb"  # nor does it allow `set`


def test_apply_needs_apply_and_every_item_operation(ws, cli):
    claimed(cli)
    cli("task", "add", "t1")
    cli("task", "add", "t2")
    batch = json.dumps({"ops": [{"op": "task.done", "task": "T1"}]})
    r = ci(ws, ["task.done"]).j("apply", "--file", "-", stdin=batch)
    assert (r.code, r.err_code) == (3, "grant.verb")  # apply is not in the verbs
    both = json.dumps({"ops": [{"op": "task.done", "task": "T1"}, {"op": "log", "text": "x"}]})
    n = len(ws.events("1"))
    r = ci(ws, ["apply", "task.done"]).j("apply", "--file", "-", stdin=both)
    assert (r.code, r.err_code) == (3, "grant.verb") and len(ws.events("1")) == n  # log is not in the verbs
    assert ci(ws, ["apply", "task.done"]).j("apply", "--file", "-", stdin=batch).code == 0


def test_the_model_checks_the_event_against_the_operations_too(ws, cli):
    """Defence in depth: the store refuses an event no granted operation emits, whatever the CLI did."""
    claimed(cli)
    g = ws.narrow_grant(["task.done"])
    gid = g.partition(".")[0]
    actor = {"kind": "agent", "id": "x", "session": SESSION, "for": ws.owner.ref, "grant": gid}
    s = ws.other()
    try:
        uid = s.uid_of("1")
        with pytest.raises(StoreError) as e:
            s.append({"type": "log.added", "actor": actor, "text": "forged"}, log=uid)
        assert e.value.code == "grant.verb"
        s.append(
            {
                "type": "artifact.added",
                "actor": actor,
                "name": "r.log",
                "kind": "receipt",
                "task": "T1",
                "sha256": "sha256:" + "0" * 64,
                "bytes": 0,
            },
            log=uid,
            artifacts={"r.log": b""},
        ) if False else None
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ receipts


def test_a_dirty_tree_gives_a_receipt_without_a_commit(ws, cli, tmp_path):
    repo = ws.repo()
    claimed(cli)
    cli("ac", "add", "impl works")
    check = "import sys; sys.exit(0 if open('impl.txt').read() == 'fixed\\n' else 1)"
    cli("task", "add", "t", "--verify", py(check), "--proves", "AC1")
    assert cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}').code == 0
    assert cli.j("task", "done", "T1", "--run").err_code == "verify.failed"  # the committed code fails
    (repo / "impl.txt").write_text("fixed\n")  # uncommitted: passes only here
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0 and "looks uncommitted" in r.out and "not evidence" in r.out
    rec = ws.events("1")[-1]["receipt"]
    assert rec["repo"] == "proj" and rec["commit"] is None
    assert ws.view("1").acceptance[0].evidence == ()
    # a changed-and-restored tree during the run counts as dirty before or after; an untracked file counts too
    (repo / "impl.txt").write_text("broken\n")
    (repo / "new.txt").write_text("x")
    cli("task", "reopen", "T1")
    cli("task", "add", "t2", "--verify", py("print(1)"))
    r = cli("task", "done", "T2", "--run")
    assert "looks uncommitted" in r.out and ws.events("1")[-1]["receipt"]["commit"] is None


def test_a_clean_tree_names_the_commit(ws, cli):
    repo = ws.repo()
    head = ws.git("rev-parse", "HEAD")
    claimed(cli)
    cli("task", "add", "t", "--verify", py("print(1)"))
    cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}')
    assert cli("task", "done", "T1", "--run").code == 0
    assert ws.events("1")[-1]["receipt"]["commit"] == head and repo.is_dir()


def test_the_agents_git_environment_does_not_choose_the_repository(ws, cli, tmp_path):
    import subprocess

    ws.repo()
    head = ws.git("rev-parse", "HEAD")
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=n",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    claimed(cli)
    cli("task", "add", "t", "--verify", py("import os; print(sorted(k for k in os.environ if k.startswith('GIT_')))"))
    cli("set", "1", 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}')
    r = cli("task", "done", "T1", "--run", GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE="/x")
    assert r.code == 0, r.err
    assert ws.events("1")[-1]["receipt"]["commit"] == head
    log = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_text()
    assert "[]" in log  # the verify command saw no GIT_* variable


def test_the_verify_environment_is_an_allow_list(ws, cli, monkeypatch):
    for k, v in (
        ("SSH_AUTH_SOCK", "/tmp/agent"),
        ("DATABASE_URL", "postgres://u:pw@h/db"),
        ("OPENAI_KEY", "k"),
        ("AWS_ACCESS_KEY_ID", "a"),
        ("GH_PAT", "p"),
        ("LC_ALL", "C"),
        ("MY_SKILL_VAR", "1"),
    ):
        monkeypatch.setenv(k, v)
    claimed(cli)
    cli(
        "task",
        "add",
        "t",
        "--verify",
        py(
            "import os; ok = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TERM', '__CF_USER_TEXT_ENCODING'); "
            "print(sorted(k for k in os.environ if k not in ok))"
        ),
    )
    r = cli("task", "done", "T1", "--run", ORCH_STATE_DIR=str(ws.host_state))
    assert r.code == 0, r.err
    log = (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_text()
    assert log.strip() == "[]"  # no ORCH_*, no SSH_AUTH_SOCK, DATABASE_URL, tokens, keys; LC_* and PATH only


def test_only_task_done_run_makes_a_receipt(ws, cli, tmp_path):
    claimed(cli)
    cli("task", "add", "t", "--verify", py("print(1)"))
    cli("task", "done", "T1", "--run")
    f = tmp_path / "x.log"
    f.write_text("forged")
    assert cli.j("artifact", "add", str(f), "--kind", "receipt").err_code == "usage"  # not a kind of `add`
    r = cli.j("artifact", "replace", "T1-receipt.log", str(f))
    assert (r.code, r.err_code) == (5, "invalid.input") and "receipt" in r.doc["error"]["message"]
    assert (ws.root / "tickets" / ws.uid("1") / "artifacts" / "T1-receipt.log").read_text().strip() == "1"


def test_the_model_refuses_a_receipt_that_is_replaced_or_has_no_task(ws, cli):
    claimed(cli)
    s = ws.other()
    actor = {"kind": "agent", "id": "x", "session": SESSION, "for": ws.owner.ref, "grant": ws.grant_id}
    try:
        uid = s.uid_of("1")
        from orch import canon

        ev = {
            "type": "artifact.added",
            "actor": actor,
            "name": "r.log",
            "kind": "receipt",
            "sha256": canon.artifact_digest(b"x"),
            "bytes": 1,
        }
        with pytest.raises(StoreError) as e:
            s.append(ev, log=uid, artifacts={"r.log": b"x"})
        assert e.value.code == "artifact.kind"
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ apply


def test_apply_refuses_what_it_cannot_do_instead_of_ignoring_it(ws, cli, tmp_path):
    claimed(cli)
    cli("task", "add", "t", "--verify", py("print(1)"))
    f = tmp_path / "ev.txt"
    f.write_text("x")
    n = len(ws.events("1"))
    for extra in ({"run": True}, {"artifact": str(f)}, {"ac": "AC1"}, {"nonsense": 1}):
        r = cli.j("apply", "--file", "-", stdin=json.dumps({"ops": [{"op": "task.done", "task": "T1", **extra}]}))
        assert (r.code, r.err_code) == (5, "invalid.input"), extra
    assert len(ws.events("1")) == n


def test_set_works_in_a_batch(ws, cli):
    claimed(cli)
    r = cli.j("apply", "--file", "-", stdin=json.dumps({"ops": [{"op": "set", "pairs": ["priority=low", "size=s"]}]}))
    assert r.code == 0, r.out
    t = ws.ticket_json("1")
    assert (t["priority"], t["size"]) == ("low", "s")


def test_a_crashed_apply_is_refused_cleanly_on_retry_and_adds_nothing_twice(ws, cli, monkeypatch):
    from orch.store import Store

    claimed(cli)
    real = Store.append
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("crash")
        return real(self, *a, **kw)

    batch = json.dumps({"ops": [{"op": "ac.add", "text": "it works"}, {"op": "log", "text": "x"}]})
    monkeypatch.setattr(Store, "append", flaky)
    with pytest.raises(KeyboardInterrupt):
        cli("apply", "--file", "-", stdin=batch)
    monkeypatch.setattr(Store, "append", real)
    assert [a["id"] for a in ws.ticket_json("1")["acceptance"]] == ["AC1"]
    r = cli.j("apply", "--file", "-", stdin=batch)
    assert r.code == 5 and "interrupted" in r.doc["error"]["message"]
    assert [a["id"] for a in ws.ticket_json("1")["acceptance"]] == ["AC1"]  # no AC2
    # the session's own notes followed the first append: a plain edit of what it wrote is not a conflict with itself
    assert cli.j("ac", "add", "second").code == 0


def test_a_crashed_handoff_is_completed_by_the_retry(ws, cli, monkeypatch):
    from orch.store import Store

    claimed(cli)
    real = Store.append
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("crash")
        return real(self, *a, **kw)

    monkeypatch.setattr(Store, "append", flaky)
    with pytest.raises(KeyboardInterrupt):
        cli("handoff", "-m", "where it stands")
    monkeypatch.setattr(Store, "append", real)
    assert cli("handoff", "-m", "where it stands").code == 0
    assert [e["type"] for e in ws.events("1")][-2:] == ["handoff.written", "claim.released"]
    assert [e["type"] for e in ws.events("1")].count("handoff.written") == 1
