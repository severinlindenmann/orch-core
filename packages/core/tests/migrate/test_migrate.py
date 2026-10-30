"""Data across updates: format.json, the open-time rule, the lock, the runner, backups, the journal, the command."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from orch.migrate import (
    CURRENT,
    Registry,
    Step,
    StepContext,
    apply,
    init_root,
    open_root,
    plan,
    read_format,
    write_format,
)
from orch.migrate import backup as bk
from orch.migrate import cli as mcli
from orch.migrate import journal as jr
from orch.migrate.format import lock_root
from orch.migrate.steps import DEFAULT, atomic_write, is_signed_path
from orch.ops.errors import OrchError


@pytest.fixture(autouse=True)
def _no_time_machine(monkeypatch):
    monkeypatch.setattr(bk, "_exclude_from_time_machine", lambda p: None)


def BK(root, env):
    """The journal's ``backups`` field: every backup of root, with the format it was taken at."""
    return {str(d.resolve()): 1 for d in bk.list_backups(env, root)}


def code_of(fn, *a, **k) -> str:
    with pytest.raises(OrchError) as e:
        fn(*a, **k)
    return e.value.code


@pytest.fixture
def env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    init_root(r, "workspace")
    return r


def two_steps(log=None, fail_at=None):
    log = [] if log is None else log

    def mk(n):
        def run(ctx: StepContext):
            if fail_at == n:
                raise RuntimeError("boom")
            log.append(n)
            ctx.write_file(f"made-{n}.txt", b"x")

        return run

    w = ("made-*.txt",)
    steps = [Step("workspace", 1, 2, "one", mk(2), writes=w), Step("workspace", 2, 3, "two", mk(3), writes=w)]
    return Registry(steps, {"host": 1, "workspace": 3}), log


# ------------------------------------------------------------------------------------------------- open_root


def test_open_at_the_current_format(root, env):
    with open_root(root, "workspace", env) as h:
        assert h.format == CURRENT["workspace"]


def test_open_refuses_older_and_newer_data(root, env):
    write_format(root, "workspace", CURRENT["workspace"] + 1)
    assert code_of(open_root, root, "workspace", env) == "format.newer"
    with pytest.raises(OrchError, match="newer orch \\(format 2\\): update orch"):
        open_root(root, "workspace", env)


def test_open_refuses_older_data(root, env, monkeypatch):
    monkeypatch.setitem(CURRENT, "workspace", 2)
    with pytest.raises(OrchError, match="older orch: run `orch update`") as e:
        open_root(root, "workspace", env)
    assert e.value.code == "format.older"


def test_open_refuses_a_folder_that_is_not_a_root(tmp_path, env):
    (tmp_path / "x").mkdir()
    assert code_of(open_root, tmp_path / "x", "workspace", env) == "format.missing"
    assert code_of(open_root, tmp_path / "nope", "workspace", env) == "format.missing"


def test_open_refuses_a_damaged_or_foreign_format_file(root, env):
    (root / "format.json").write_text("{not json")
    assert code_of(open_root, root, "workspace", env) == "format.missing"
    (root / "format.json").write_text(json.dumps({"orch": "host", "format": 1}))
    assert code_of(open_root, root, "workspace", env) == "format.missing"
    (root / "format.json").write_text(json.dumps({"orch": "workspace", "format": True}))
    assert code_of(open_root, root, "workspace", env) == "format.missing"


def test_init_root_refuses_an_existing_root(root):
    assert code_of(init_root, root, "workspace") == "format.exists"


def test_format_file_is_written_atomically(root):
    write_format(root, "workspace", 7)
    assert read_format(root, "workspace") == 7
    assert [p.name for p in root.iterdir() if ".tmp" in p.name] == []


# -------------------------------------------------------------------------------------------------- the lock


def test_migrate_is_refused_while_a_shared_holder_exists(root, env):
    reg, log = two_steps()
    with open_root(root, "workspace", env):
        with pytest.raises(OrchError, match="orch is running") as e:
            apply(root, "workspace", reg)
        assert e.value.code == "migrate.running"
    assert log == []
    assert apply(root, "workspace", reg) == ["one", "two"]  # the holder is gone


def test_open_is_refused_while_migrate_holds_the_lock(root, env):
    reg, _ = two_steps()
    seen = []

    def during(step):
        seen.append(code_of(open_root, root, "workspace", env))

    apply(root, "workspace", reg, after_step=during)
    assert seen == ["migrate.running", "migrate.running"]


# ------------------------------------------------------------------------------------------------- registry


def test_the_shipped_registry_is_contiguous_and_at_the_baseline():
    DEFAULT.check()
    assert DEFAULT.current == {"host": 1, "workspace": 1}


@pytest.mark.parametrize(
    "steps,current",
    [
        ([Step("workspace", 2, 3, "gap", lambda c: None)], {"workspace": 3}),  # does not start at the baseline
        (
            [Step("workspace", 1, 2, "a", lambda c: None), Step("workspace", 3, 4, "c", lambda c: None)],
            {"workspace": 4},
        ),
        ([Step("workspace", 1, 3, "jump", lambda c: None)], {"workspace": 3}),
        ([Step("workspace", 1, 2, "a", lambda c: None)], {"workspace": 3}),  # ends short of the code's format
        ([Step("mystery", 1, 2, "a", lambda c: None)], {"workspace": 1}),
    ],
)
def test_a_registry_with_a_gap_is_refused(steps, current):
    with pytest.raises(ValueError):
        Registry(steps, current)


# -------------------------------------------------------------------------------------------- runner / idempotency


def test_plan_lists_the_pending_steps_without_changing_anything(root):
    reg, log = two_steps()
    p = plan(root, "workspace", reg)
    assert (p.at, p.target, [s.name for s in p.steps]) == (1, 3, ["one", "two"]) and log == []


def test_apply_runs_every_step_and_twice_is_a_no_op(root):
    reg, log = two_steps()
    assert apply(root, "workspace", reg) == ["one", "two"]
    assert read_format(root, "workspace") == 3 and log == [2, 3]
    assert apply(root, "workspace", reg) == []
    assert log == [2, 3] and not plan(root, "workspace", reg).pending


def test_a_crash_between_two_steps_resumes_at_the_next(root):
    reg, log = two_steps()

    def crash(step):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        apply(root, "workspace", reg, after_step=crash)
    assert read_format(root, "workspace") == 2 and log == [2]  # step one's format was written before the crash
    assert apply(root, "workspace", reg) == ["two"]  # resumes, step one does not run again
    assert read_format(root, "workspace") == 3 and log == [2, 3]


def test_a_failing_step_leaves_the_format_at_the_last_good_step(root):
    reg, log = two_steps(fail_at=3)
    with pytest.raises(RuntimeError):
        apply(root, "workspace", reg)
    assert read_format(root, "workspace") == 2
    reg2, log2 = two_steps()
    assert apply(root, "workspace", reg2) == ["two"] and log2 == [3]


def test_apply_refuses_newer_data(root):
    write_format(root, "workspace", 9)
    assert code_of(apply, root, "workspace") == "format.newer"


def test_the_verify_hook_failing_stops_before_the_format_moves(root):
    def bad(root_):
        raise RuntimeError("wrong")

    reg = Registry([Step("workspace", 1, 2, "v", lambda c: None, bad)], {"workspace": 2})
    with pytest.raises(RuntimeError):
        apply(root, "workspace", reg)
    assert read_format(root, "workspace") == 1


def test_sqlite_step_bumps_user_version_in_the_same_transaction(root):
    db = root / "x.sqlite3"
    sqlite3.connect(db).close()

    def good(ctx):
        ctx.sqlite("x.sqlite3", lambda c: c.execute("CREATE TABLE t(a)"), user_version=5)

    ctx = StepContext(root, ("x.sqlite3",))
    good(ctx)
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 5

    def bad(c):
        c.execute("CREATE TABLE u(a)")
        raise RuntimeError("half way")

    with pytest.raises(RuntimeError):
        ctx.sqlite("x.sqlite3", bad, user_version=6)
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 5
    assert [r[0] for r in c.execute("select name from sqlite_master")] == ["t"]


# ---------------------------------------------------------------------------------------- signed-path deny list


@pytest.mark.parametrize(
    "rel",
    [
        "events/2026.jsonl",
        "events/x.json",
        "tickets/T/events.jsonl",
        ".state/checkpoints/a.json",
        "hosts/w/revocations.jsonl",
        "cards/p_1.json",
        "membership-card.json",
        "remote/devices.json",
        "keys.jsonl",
    ],
)
def test_a_step_cannot_write_signed_data(root, rel):
    assert is_signed_path(rel)
    ctx = StepContext(root, ("*",))
    with pytest.raises(OrchError) as e:
        ctx.write_file(rel, b"x")
    assert e.value.code == "migrate.signed_path" and not (root / rel).exists()
    sql = "events.sqlite3" if "/" not in rel else rel + ".sqlite3"
    assert code_of(ctx.sqlite, sql, lambda c: None, user_version=2) == "migrate.signed_path"


@pytest.mark.parametrize("rel", ["../out.txt", "/etc/x", "a/../../b"])
def test_a_step_cannot_leave_the_root(root, rel):
    assert code_of(StepContext(root, ("*",)).write_file, rel, b"x") == "migrate.signed_path"


def test_a_step_may_write_an_ordinary_file(root):
    StepContext(root, ("tickets/*/ticket.json",)).write_file("tickets/T/ticket.json", b"{}")
    assert (root / "tickets/T/ticket.json").read_bytes() == b"{}"
    assert not is_signed_path("tickets/T/ticket.json")


# ------------------------------------------------------------------------------------------------------ backup


def make_data(root: Path) -> None:
    (root / "hosts/w/keys").mkdir(parents=True)
    (root / "hosts/w/keys/wsk").write_text("secret")
    os.chmod(root / "hosts/w/keys/wsk", 0o600)
    (root / "hosts/w/revocations.jsonl").write_text("r1\nr2\n")
    (root / ".state").mkdir()
    (root / ".state/index.sqlite").write_text("derived")
    (root / ".orch-work").mkdir()
    (root / ".orch-work/x").write_text("work")
    (root / "install/tools/orch-1").mkdir(parents=True)
    (root / "install/tools/orch-1/bin").write_text("tool")
    (root / "install/install.json").write_text("{}")
    (root / "events").mkdir()
    (root / "events/a.jsonl").write_text("e1\n")


def test_backup_is_private_and_leaves_out_derived_data(root, env):
    make_data(root)
    b = bk.create_backup(root, "workspace", 1, 2, env, ts="20261030T100000Z")
    assert b.name == "20261030T100000Z-workspace-1-2"
    assert b.parent == bk.backups_dir(env)
    assert stat.S_IMODE(b.stat().st_mode) == 0o700 and stat.S_IMODE(bk.backups_dir(env).stat().st_mode) == 0o700
    assert stat.S_IMODE((b / "data").stat().st_mode) == 0o700
    data = b / "data"
    assert (data / "hosts/w/keys/wsk").read_text() == "secret"
    assert stat.S_IMODE((data / "hosts/w/keys/wsk").stat().st_mode) == 0o600
    assert (data / "events/a.jsonl").read_text() == "e1\n" and (data / "install/install.json").exists()
    for gone in (".state/index.sqlite", ".orch-work", "install/tools", "format.lock"):
        assert not (data / gone).exists(), gone
    m = json.loads((b / "manifest.json").read_text())
    assert (m["kind"], m["from"], m["to"]) == ("workspace", 1, 2)
    assert m["method"] == ("clone" if sys.platform == "darwin" else "copy")


def test_backup_without_clonefile_copies(root, env, monkeypatch):
    make_data(root)
    monkeypatch.setattr(sys, "platform", "linux")
    b = bk.create_backup(root, "workspace", 1, 2, env, ts="20261030T100001Z")
    assert json.loads((b / "manifest.json").read_text())["method"] == "copy"
    assert (b / "data/hosts/w/keys/wsk").read_text() == "secret"


def test_backup_keeps_the_last_three_per_root(root, env, tmp_path):
    other = tmp_path / "other"
    init_root(other, "workspace")
    ob = bk.create_backup(other, "workspace", 1, 2, env, ts="20261030T090000Z")
    made = [bk.create_backup(root, "workspace", 1, 2, env, ts=f"20261030T10000{i}Z") for i in range(5)]
    assert len(bk.list_backups(env, root)) == 5
    bk.prune_backups(env, root)
    left = bk.list_backups(env, root)
    assert [p.name for p in left] == [p.name for p in reversed(made[2:])]
    assert not made[0].exists() and not made[1].exists() and ob.exists()  # the other root is not pruned


def test_two_backups_in_the_same_second_do_not_collide(root, env):
    a = bk.create_backup(root, "workspace", 1, 2, env, ts="20261030T100000Z")
    b = bk.create_backup(root, "workspace", 1, 2, env, ts="20261030T100000Z")
    assert a != b and a.exists() and b.exists()


@pytest.mark.parametrize(
    "parts",
    [
        ("Library", "Mobile Documents", "com~apple~CloudDocs", "backups"),
        ("Dropbox", "orch"),
        ("Google Drive", "orch"),
        ("Library", "CloudStorage", "GoogleDrive-me@example.com", "orch"),
        ("OneDrive", "x"),
    ],
)
def test_backup_refuses_a_synced_folder(root, tmp_path, parts):
    base = tmp_path / Path(*parts)
    env = {"XDG_DATA_HOME": str(base)}
    assert code_of(bk.create_backup, root, "workspace", 1, 2, env) == "migrate.backup_refused"
    assert not (base / "orch").exists()


def test_backup_refuses_a_folder_with_a_sync_marker(root, tmp_path):
    (tmp_path / "share").mkdir()
    (tmp_path / "share/.dropbox").write_text("")
    assert code_of(bk.create_backup, root, "workspace", 1, 2, {"XDG_DATA_HOME": str(tmp_path / "share/data")}) == (
        "migrate.backup_refused"
    )


def test_backup_refuses_a_backup_folder_inside_the_data(root):
    env = {"XDG_DATA_HOME": str(root / "data")}
    assert code_of(bk.create_backup, root, "workspace", 1, 2, env) == "migrate.backup_refused"


# ----------------------------------------------------------------------------------------------------- restore


def test_restore_puts_the_data_back_and_merges_revocations(root, env):
    make_data(root)
    b = bk.create_backup(root, "workspace", 1, 2, env)
    # after the backup: a newer revocation, a new file, a changed file, a new folder with its own list
    (root / "hosts/w/revocations.jsonl").write_text("r1\nr2\nr3\n")
    (root / "hosts/w/new.txt").write_text("new")
    (root / "install/install.json").write_text('{"changed": 1}')
    (root / "hosts/z").mkdir()
    (root / "hosts/z/revocations.jsonl").write_text("z1\n")
    (root / "format.json").write_text(json.dumps({"orch": "workspace", "format": 2}))
    (root / ".state/index.sqlite").write_text("kept")
    jr.write_journal("stopped", env, backups=BK(root, env))
    bk.restore(b, root, env)
    assert (root / "hosts/w/revocations.jsonl").read_text().splitlines() == ["r1", "r2", "r3"]  # r3 is never lost
    assert (root / "hosts/z/revocations.jsonl").read_text() == "z1\n"
    assert not (root / "hosts/w/new.txt").exists()
    assert (root / "install/install.json").read_text() == "{}"
    assert read_format(root, "workspace") == 1
    assert (root / "hosts/w/keys/wsk").read_text() == "secret"
    assert (root / ".state/index.sqlite").read_text() == "kept"  # derived data is not part of a backup


def test_restore_never_replaces_revocations_with_older_lines(root, env):
    (root / "revocations.jsonl").write_text("a\nb\n")
    b = bk.create_backup(root, "workspace", 1, 2, env)
    (root / "revocations.jsonl").write_text("b\nc\n")  # a differs from the backup: union, backup order first
    jr.write_journal("stopped", env, backups=BK(root, env))
    bk.restore(b, root, env)
    assert sorted((root / "revocations.jsonl").read_text().splitlines()) == ["a", "b", "c"]


def test_restore_is_refused_while_orch_runs_and_for_a_foreign_backup(root, env, tmp_path):
    b = bk.create_backup(root, "workspace", 1, 2, env)
    jr.write_journal("stopped", env, backups=BK(root, env))
    with lock_shared(root):
        assert code_of(bk.restore, b, root, env) == "migrate.running"
    other = tmp_path / "other"
    init_root(other, "workspace")
    assert code_of(bk.restore, b, other, env) == "migrate.backup_refused"
    assert code_of(bk.restore, tmp_path, root, env) == "migrate.backup_refused"


@contextlib.contextmanager
def lock_shared(root):
    fd = lock_root(root, exclusive=False)
    try:
        yield
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------------ journal


def test_an_interrupted_journal_refuses_open(root, env):
    jr.write_journal("stopped", env, from_version="1.0.0")
    with pytest.raises(OrchError, match="an update was interrupted: run `orch update` to finish or roll back") as e:
        open_root(root, "workspace", env)
    assert e.value.code == "update.interrupted"
    for phase in ("backed-up", "installed", "migrated:/some/ws"):
        jr.write_journal(phase, env, backups=BK(root, env))
        assert code_of(open_root, root, "workspace", env) == "update.interrupted"
    jr.write_journal("committed", env)
    open_root(root, "workspace", env).close()
    assert jr.read_journal(env)["from_version"] == "1.0.0"  # fields are kept across phases


def test_a_damaged_journal_fails_closed(root, env):
    p = jr.journal_path(env)
    p.parent.mkdir(parents=True)
    p.write_text("{garbage")
    assert code_of(open_root, root, "workspace", env) == "update.interrupted"
    with pytest.raises(OrchError):
        jr.write_journal("installed", env)  # only a new update replaces a damaged journal
    jr.write_journal("stopped", env)
    jr.write_journal("committed", env)
    open_root(root, "workspace", env).close()


def test_journal_rejects_an_unknown_phase_and_is_private(env):
    with pytest.raises(ValueError):
        jr.write_journal("whenever", env)
    jr.write_journal("stopped", env)
    assert stat.S_IMODE(jr.journal_path(env).stat().st_mode) == 0o600
    jr.clear_journal(env)
    assert jr.read_journal(env) is None and not jr.unfinished(env)


def test_atomic_write_leaves_no_tmp(tmp_path):
    atomic_write(tmp_path / "a" / "f", b"1")
    assert os.listdir(tmp_path / "a") == ["f"]


# ------------------------------------------------------------------------------------------------------ the CLI


def run_cli(argv, env, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = mcli.main(argv, env=env, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("agent", [{"ORCH_GRANT": "g"}, {"ORCH_SESSION": "s"}])
@pytest.mark.parametrize("argv", [["--check"], ["--apply"], ["--apply", "--json"]])
def test_the_command_is_human_only(env, tmp_path, agent, argv):
    code, out, err = run_cli(argv, {**env, **agent}, tmp_path)
    assert code == 3 and "agent cannot" in (out + err) and not Path(env["ORCH_STATE_DIR"]).exists()
    assert not bk.backups_dir(env).exists()


def test_the_command_is_refused_through_the_main_cli_too(env, tmp_path):
    from orch.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    assert main(["migrate", "--apply"], env={**env, "ORCH_SESSION": "s"}, stdout=out, stderr=err) == 3


def test_usage(env, tmp_path):
    for argv in ([], ["--check", "--apply"], ["--workspace"], ["--check", "x"]):
        assert run_cli(argv, env, tmp_path)[0] == 2


def test_check_and_apply_on_a_workspace(env, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    store = project / ".orch"
    init_root(store, "workspace")
    reg, _ = two_steps()
    monkeypatch.setattr("orch.migrate.cli.plan", lambda r, k: plan(r, k, reg))
    monkeypatch.setattr("orch.migrate.cli.apply", lambda r, k, **kw: apply(r, k, reg, **kw))
    code, out, _ = run_cli(["--check", "--workspace", str(project)], env, tmp_path)
    assert code == 0 and "2 step(s) waiting (one, two)" in out
    assert read_format(store, "workspace") == 1 and not bk.backups_dir(env).exists()
    code, out, _ = run_cli(["--apply", "--workspace", str(project), "--json"], env, tmp_path)
    doc = json.loads(out)
    row = doc["roots"][0]
    assert code == 0 and row["applied"] == ["one", "two"] and Path(row["backup"]).is_dir()  # backed up first
    assert read_format(store, "workspace") == 3
    code, out, _ = run_cli(["--apply", "--workspace", str(project)], env, tmp_path)
    assert code == 0 and "up to date" in out
    assert len(bk.list_backups(env, store)) == 1  # nothing pending: no new backup


def test_a_failing_step_reports_the_backup(env, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    init_root(project / ".orch", "workspace")
    reg, _ = two_steps(fail_at=2)
    monkeypatch.setattr("orch.migrate.cli.plan", lambda r, k: plan(r, k, reg))
    monkeypatch.setattr("orch.migrate.cli.apply", lambda r, k, **kw: apply(r, k, reg, **kw))
    code, out, _ = run_cli(["--apply", "--workspace", str(project)], env, tmp_path)
    assert code == 5 and "put back as it was" in out and "boom" in out
    assert read_format(project / ".orch", "workspace") == 1  # restored to the state before the first step
    assert jr.read_journal(env) is None  # the restore made no lasting journal


def test_an_unreachable_workspace_is_skipped_and_listed(env, tmp_path):
    code, out, _ = run_cli(["--check", "--workspace", str(tmp_path / "gone")], env, tmp_path)
    assert code == 0 and "skipped" in out and "not orch data" in out


def test_the_host_and_registered_workspaces_are_checked(env, tmp_path):
    init_root(Path(env["ORCH_STATE_DIR"]), "host")
    code, out, _ = run_cli(["--check", "--json"], env, tmp_path)
    doc = json.loads(out)
    assert code == 0 and doc["roots"][0]["kind"] == "host" and doc["roots"][0]["steps"] == []


def test_a_git_project_that_does_not_ignore_the_store_warns(env, tmp_path):
    project = tmp_path / "proj"
    init_root(project / ".orch", "workspace")
    (project / ".git" / "info").mkdir(parents=True)
    code, out, _ = run_cli(["--check", "--workspace", str(project)], env, tmp_path)
    assert code == 0 and "not ignored by git" in out
    (project / ".gitignore").write_text("node_modules\n/.orch/\n")
    assert "not ignored by git" not in run_cli(["--check", "--workspace", str(project)], env, tmp_path)[1]


def test_migrate_never_runs_the_projects_git(env, tmp_path, monkeypatch):
    """A project's .git/config can name a program (core.fsmonitor): migrate must not run git at all, also with a
    pending step (so a backup and a migration really run)."""
    import subprocess

    project = tmp_path / "proj"
    init_root(project / ".orch", "workspace")
    marker = tmp_path / "marker"
    script = tmp_path / "fsm.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "core.fsmonitor", str(script)], check=True)
    reg, _ = two_steps()
    monkeypatch.setattr("orch.migrate.cli.plan", lambda r, k: plan(r, k, reg))
    monkeypatch.setattr("orch.migrate.cli.apply", lambda r, k, **kw: apply(r, k, reg, **kw))
    monkeypatch.setattr(sys, "platform", "linux")  # the clone path would legitimately spawn cp
    spawned = []

    def refuse(*a, **k):
        spawned.append(a)
        raise OSError("no spawning")

    for name in ("Popen",):
        monkeypatch.setattr(subprocess, name, refuse)
    for name in ("posix_spawn", "posix_spawnp", "system", "execv", "execve", "execvp", "execvpe"):
        monkeypatch.setattr(os, name, refuse)
    for mode in ("--check", "--apply"):
        assert run_cli([mode, "--workspace", str(project)], env, tmp_path)[0] == 0
    assert read_format(project / ".orch", "workspace") == 3
    assert not marker.exists() and spawned == []


# ------------------------------------------------------------------------------- review round: H1, H2, M1-M3, L


PROTECTED_CASES = [
    "events/a.json", "EVENTS/a.json", "membership/head.json", "Membership/Head.JSON", "membership/keys/k1",
    "keys/wsk", "hosts/w/keys/wsk", "hosts/w/Keys/WSK", "local-epochs.json", "hosts/w/local-epochs.json",
    "sign-ledger.json", "hosts/w/Sign-Ledger.json", "x/revocations.jsonl", "x/Revocations.JSONL", "remote/devices.json",
    "genesis.json", "hosts/w/Genesis-pin.json", "cards/p.json", ".state/checkpoints/a.json",
]  # fmt: skip


@pytest.mark.parametrize("rel", PROTECTED_CASES)
def test_protected_paths_are_refused_even_when_declared(root, rel):
    assert is_signed_path(rel)
    assert code_of(StepContext(root, ("*",)).write_file, rel, b"x") == "migrate.signed_path"


def test_a_step_may_write_only_what_it_declared(root):
    ctx = StepContext(root, ("made-*.txt",))
    ctx.write_file("made-1.txt", b"x")
    assert code_of(ctx.write_file, "other.txt", b"x") == "migrate.signed_path"
    assert code_of(StepContext(root).write_file, "made-1.txt", b"x") == "migrate.signed_path"  # nothing declared


def test_a_symlink_into_protected_data_is_refused(root):
    (root / "membership").mkdir()
    (root / "membership/head.json").write_text("h")
    (root / "alias.txt").symlink_to(root / "membership/head.json")
    assert code_of(StepContext(root, ("*",)).write_file, "alias.txt", b"x") == "migrate.signed_path"
    (root / "outside").symlink_to(root.parent)
    assert code_of(StepContext(root, ("*",)).write_file, "outside/f.txt", b"x") == "migrate.signed_path"


@pytest.mark.parametrize("rel", ["membership/head.json", "Membership/HEAD.json", "hosts/w/keys/wsk"])
def test_a_step_that_bypasses_the_helper_is_caught_and_the_format_stays(root, rel):
    target = root / rel

    def sneaky(ctx):
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w") as f:  # a plain open(), not the helper
            f.write("tampered")

    reg = Registry([Step("workspace", 1, 2, "sneaky", sneaky)], {"workspace": 2})
    with pytest.raises(OrchError) as e:
        apply(root, "workspace", reg)
    assert e.value.code == "migrate.signed_path" and read_format(root, "workspace") == 1


def test_a_step_that_edits_an_existing_signed_file_is_caught(root):
    (root / "events").mkdir()
    (root / "events/a.jsonl").write_text("e1\n")

    def sneaky(ctx):
        (root / "events/a.jsonl").write_text("e1\ne2\n")

    reg = Registry([Step("workspace", 1, 2, "sneaky", sneaky)], {"workspace": 2})
    with pytest.raises(OrchError):
        apply(root, "workspace", reg)
    assert read_format(root, "workspace") == 1


def committed(env, root, phase="stopped"):
    jr.write_journal(phase, env, backups=BK(root, env))


def test_restore_is_refused_after_the_commit_point_or_without_a_journal(root, env):
    b = bk.create_backup(root, "workspace", 1, 2, env)
    assert code_of(bk.restore, b, root, env) == "migrate.backup_refused"  # no journal
    jr.write_journal("stopped", env, backups=BK(root, env))
    jr.write_journal("committed", env)
    assert code_of(bk.restore, b, root, env) == "migrate.backup_refused"
    jr.write_journal("stopped", env, backups={"/elsewhere": 1})
    assert code_of(bk.restore, b, root, env) == "migrate.backup_refused"  # the journal names another root
    bk.restore(b, root, env, own_failure=True)  # orch migrate's own failure path needs no journal


def test_restore_keeps_what_the_backup_left_out(root, env):
    make_data(root)
    b = bk.create_backup(root, "workspace", 1, 2, env)
    (root / "install/tools/orch-2").mkdir()
    (root / "install/tools/orch-2/bin").write_text("new tool")
    (root / "install/update.json").write_text("journal")
    committed(env, root)
    (root / "install/update.json").unlink()
    bk.restore(b, root, env)
    assert (root / "install/tools/orch-1/bin").read_text() == "tool"
    assert (root / "install/tools/orch-2/bin").read_text() == "new tool"
    assert (root / ".orch-work/x").read_text() == "work" and (root / ".state/index.sqlite").read_text() == "derived"


def tree_digest(root):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.name == "format.lock":
            continue
        out[p.relative_to(root).as_posix()] = p.read_bytes() if p.is_file() and not p.is_symlink() else None
    return out


def test_an_error_during_restore_leaves_the_original_untouched(root, env, monkeypatch):
    make_data(root)
    b = bk.create_backup(root, "workspace", 1, 2, env)
    (root / "hosts/w/revocations.jsonl").write_text("r1\nr2\nr3\n")
    committed(env, root)
    before = tree_digest(root)

    def boom(src, dst, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(bk.shutil, "copytree", boom)
    with pytest.raises(OSError):
        bk.restore(b, root, env)
    assert tree_digest(root) == before
    assert [p.name for p in root.parent.iterdir() if p.name.startswith(".root.") and not p.name.endswith(".lock")] == []


def test_restore_rejects_a_backup_with_a_link(root, env):
    b = bk.create_backup(root, "workspace", 1, 2, env)
    (b / "data" / "evil").symlink_to("/etc")
    committed(env, root)
    assert code_of(bk.restore, b, root, env) == "migrate.backup_refused"


def test_apply_with_a_shared_holder_makes_no_backup(env, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    store = project / ".orch"
    init_root(store, "workspace")
    reg, _ = two_steps()
    monkeypatch.setattr("orch.migrate.cli.plan", lambda r, k: plan(r, k, reg))
    monkeypatch.setattr("orch.migrate.cli.apply", lambda r, k, **kw: apply(r, k, reg, **kw))
    with lock_shared(store):
        code, out, _ = run_cli(["--apply", "--workspace", str(project)], env, tmp_path)
    assert code == 4 and "orch is running" in out and not bk.backups_dir(env).exists()
    assert read_format(store, "workspace") == 1


def test_failed_attempts_never_prune_the_original(root, env):
    first = bk.create_backup(root, "workspace", 1, 2, env, ts="20261030T090000Z")
    for i in range(4):  # four failed attempts: backups only, nothing is pruned
        bk.create_backup(root, "workspace", 1, 2, env, ts=f"20261030T10000{i}Z")
    assert first.exists() and len(bk.list_backups(env, root)) == 5
    removed = bk.prune_backups(env, root, pin_from=1)
    assert first.exists() and first not in removed and len(bk.list_backups(env, root)) == 4  # 3 newest + the pin
    bk.prune_backups(env, root)
    assert not first.exists()  # no pin: plain keep-3


@pytest.mark.parametrize(
    "parts",
    [("Library", "CloudStorage", "OneDrive-x", "o"), ("Library", "Mobile Documents", "x")],
)
def test_more_synced_places_are_refused(root, tmp_path, parts):
    base = tmp_path / Path(*parts)
    assert code_of(
        bk.create_backup, root, "workspace", 1, 2, {"XDG_DATA_HOME": str(base)}
    ) == "migrate.backup_refused" or (parts[0] == "Sync")


def test_a_stfolder_marker_and_documents_on_macos_are_refused(root, tmp_path, monkeypatch):
    (tmp_path / "st").mkdir()
    (tmp_path / "st/.stfolder").mkdir()
    env = {"XDG_DATA_HOME": str(tmp_path / "st/d")}
    assert code_of(bk.create_backup, root, "workspace", 1, 2, env) == "migrate.backup_refused"
    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path / "h"
    for d in ("Documents", "Desktop"):
        e = {"HOME": str(home), "XDG_DATA_HOME": str(home / d / "x")}
        assert code_of(bk.create_backup, root, "workspace", 1, 2, e) == "migrate.backup_refused"


def test_backup_tree_has_no_group_or_other_access(root, env):
    make_data(root)
    os.chmod(root / "events/a.jsonl", 0o644)
    b = bk.create_backup(root, "workspace", 1, 2, env)
    for p in [b, *b.rglob("*")]:
        if not p.is_symlink():
            assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0, p


def test_backups_are_ordered_by_the_manifest_time(root, env):
    old = bk.create_backup(root, "workspace", 1, 2, env, ts="99990101T000000Z")  # a name that sorts last
    new = bk.create_backup(root, "workspace", 1, 2, env, ts="20000101T000000Z")
    assert bk.list_backups(env, root) == [new, old]


def test_journal_phases_only_move_forward(env):
    jr.write_journal("installed", env)
    with pytest.raises(ValueError):
        jr.write_journal("backed-up", env)
    jr.write_journal("migrated:/a", env)
    jr.write_journal("migrated:/b", env)
    jr.write_journal("committed", env)
    with pytest.raises(ValueError):
        jr.write_journal("installed", env)
    jr.write_journal("stopped", env)  # a new update


def test_the_lock_file_is_not_followed_through_a_link(root, tmp_path):
    (root / "format.lock").symlink_to(tmp_path / "victim")
    with pytest.raises(OSError):
        lock_root(root, exclusive=False)
    assert not (tmp_path / "victim").exists()


def test_state_checkpoints_are_backed_up_and_protected_but_the_index_is_not(root, env):
    (root / ".state/checkpoints").mkdir(parents=True)
    (root / ".state/checkpoints/1").write_text("signed")
    (root / ".state/abandoned").mkdir()
    (root / ".state/abandoned/a.jsonl").write_text("x")
    (root / ".state/pending").mkdir()
    (root / ".state/pending/p").write_text("p")
    (root / ".state/index.sqlite").write_text("derived")
    (root / ".state/index.sqlite-wal").write_text("derived")
    for rel in (".state/checkpoints/1", ".state/abandoned/x.jsonl", ".state/pending/p", "land/w/key", "known_hosts",
                "git-pins.json", "secret-pins.json", "remote/counters.json"):  # fmt: skip
        assert is_signed_path(rel), rel
    assert not is_signed_path(".state/index.sqlite")
    b = bk.create_backup(root, "workspace", 1, 2, env)
    assert (b / "data/.state/checkpoints/1").read_text() == "signed"
    assert (b / "data/.state/pending/p").exists() and not (b / "data/.state/index.sqlite").exists()
    assert not (b / "data/.state/index.sqlite-wal").exists()
    (root / ".state/checkpoints/1").write_text("tampered")
    jr.write_journal("stopped", env, backups=BK(root, env))
    bk.restore(b, root, env)
    assert (root / ".state/checkpoints/1").read_text() == "signed"
    assert (root / ".state/index.sqlite").read_text() == "derived"  # carried over


@pytest.mark.parametrize("mutate", ["rewrite", "create"])
def test_a_step_touching_state_checkpoints_or_abandoned_is_caught(root, mutate):
    (root / ".state/checkpoints").mkdir(parents=True)
    (root / ".state/checkpoints/1").write_text("signed")

    def sneaky(ctx):
        if mutate == "rewrite":
            (root / ".state/checkpoints/1").write_text("x")
        else:
            (root / ".state/abandoned").mkdir()
            (root / ".state/abandoned/x.jsonl").write_text("x")

    reg = Registry([Step("workspace", 1, 2, "s", sneaky)], {"workspace": 2})
    with pytest.raises(OrchError):
        apply(root, "workspace", reg)
    assert read_format(root, "workspace") == 1


def test_a_backup_from_an_earlier_update_is_not_restored(root, env):
    old = bk.create_backup(root, "workspace", 1, 2, env)
    jr.write_journal("stopped", env, backups=BK(root, env))
    jr.write_journal("committed", env)
    (root / "marker").write_text("after the first update")
    jr.write_journal("stopped", env, backups={})  # a new update, which made no backup yet
    assert code_of(bk.restore, old, root, env) == "migrate.backup_refused"
    new = bk.create_backup(root, "workspace", 2, 3, env)
    jr.write_journal("backed-up", env, backups={str(new.resolve()): 2})
    assert code_of(bk.restore, old, root, env) == "migrate.backup_refused"
    jr.write_journal("backed-up", env, backups={str(new.resolve()): 9})  # the format does not match
    assert code_of(bk.restore, new, root, env) == "migrate.backup_refused"
    jr.write_journal("backed-up", env, backups={str(new.resolve()): 2})
    bk.restore(new, root, env)


def test_a_failing_step_after_a_good_one_is_rolled_back_to_the_backup(env, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    store = project / ".orch"
    init_root(store, "workspace")
    reg, _ = two_steps(fail_at=3)  # step one succeeds and writes made-2.txt, step two fails
    monkeypatch.setattr("orch.migrate.cli.plan", lambda r, k: plan(r, k, reg))
    monkeypatch.setattr("orch.migrate.cli.apply", lambda r, k, **kw: apply(r, k, reg, **kw))
    code, out, _ = run_cli(["--apply", "--workspace", str(project)], env, tmp_path)
    assert code == 5 and read_format(store, "workspace") == 1 and not (store / "made-2.txt").exists()


def swap_setup(root, env):
    make_data(root)
    b = bk.create_backup(root, "workspace", 1, 2, env)
    (root / ".state/index.sqlite").write_text("kept-index")
    (root / "events/a.jsonl").write_text("e1\ne2\n")
    jr.write_journal("installed", env, backups={str(b.resolve()): 1})
    return b


@pytest.mark.parametrize("fallback", [False, True])
def test_restore_swaps_in_and_carries_what_was_left_out(root, env, monkeypatch, fallback):
    b = swap_setup(root, env)
    if fallback:
        monkeypatch.setattr(bk, "_exchange", lambda a, c: False)
    bk.restore(b, root, env)
    assert (root / "events/a.jsonl").read_text() == "e1\n"
    assert (root / ".state/index.sqlite").read_text() == "kept-index"
    assert (root / "install/tools/orch-1/bin").read_text() == "tool"
    assert [p.name for p in root.parent.iterdir() if p.name.startswith(".root.") and not p.name.endswith(".lock")] == []
    assert jr.read_journal(env)["phase"] == "installed" and not jr.read_journal(env).get("restoring")


@pytest.mark.parametrize("fail_at", ["carry", "swap", "second_rename"])
def test_a_crash_inside_restore_never_leaves_the_root_open_or_without_its_journal(root, env, monkeypatch, fail_at):
    b = swap_setup(root, env)
    monkeypatch.setattr(bk, "_exchange", lambda a, c: False)
    real = os.rename
    calls = []

    def flaky(src, dst, *a, **k):
        calls.append((str(src), str(dst)))
        n = len(calls)
        boom = (
            (fail_at == "carry" and ".restore-" in str(dst) and n >= 2)
            or (fail_at == "swap" and str(src) == str(root.resolve()) and ".replaced-" in str(dst))
            or (fail_at == "second_rename" and ".restore-" in str(src) and str(dst) == str(root.resolve()))
        )
        if boom:
            raise OSError("crash")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(bk.os, "rename", flaky)
    with pytest.raises(OSError):
        bk.restore(b, root, env)
    monkeypatch.setattr(bk.os, "rename", real)
    assert jr.unfinished(env)  # a crash fails closed
    assert code_of(open_root, root, "workspace", env) == "update.interrupted"
    assert root.exists() and (root / "format.json").exists() or fail_at == "carry"
    # the next run cleans up and can restore again
    bk.restore(b, root, env)
    assert (root / "events/a.jsonl").read_text() == "e1\n"
    assert [p.name for p in root.parent.iterdir() if p.name.startswith(".root.") and not p.name.endswith(".lock")] == []


def test_a_crash_between_the_two_renames_is_recovered_on_the_next_run(root, env, monkeypatch):
    b = swap_setup(root, env)
    aside = root.with_name(".root.replaced-dead")
    os.rename(root, aside)  # the state a crash between the renames leaves
    bk.restore(b, root, env)
    assert (root / "events/a.jsonl").read_text() == "e1\n" and not aside.exists()


@pytest.mark.parametrize(
    "rel",
    ["hosts/w/heads.sqlite", "hosts/w/heads.sqlite-wal", "hosts/w/transitions/t1"]
    + ["hosts/w/pending-transitions/p", ".state/applied", ".state/intents/i"],
)  # fmt: skip
def test_the_remaining_durable_host_files_are_protected(root, rel):
    assert is_signed_path(rel)
    assert not bk.is_derived(".state/heads.sqlite")


def test_a_step_that_wipes_heads_sqlite_is_caught(root):
    (root / "hosts/w").mkdir(parents=True)
    (root / "hosts/w/heads.sqlite").write_text("ancestry")

    def wipe(ctx):
        (root / "hosts/w/heads.sqlite").unlink()

    reg = Registry([Step("workspace", 1, 2, "wipe", wipe)], {"workspace": 2})
    with pytest.raises(OrchError):
        apply(root, "workspace", reg)
    assert read_format(root, "workspace") == 1 and (root / "hosts/w/heads.sqlite").exists() is False  # the step ran


@pytest.mark.parametrize("phase", ["committed", "installed"])
def test_restoring_always_counts_as_unfinished(root, env, phase):
    jr.write_journal("stopped", env)
    jr.write_journal(phase, env, restoring=str(root))
    assert jr.unfinished(env) and code_of(open_root, root, "workspace", env) == "update.interrupted"
    jr.write_journal(phase, env, restoring=None)
    assert jr.unfinished(env) is (phase != "committed")


def test_a_crash_in_restore_after_commit_marks_still_refuses_open(root, env, monkeypatch):
    b = swap_setup(root, env)
    jr.write_journal("committed", env, backups={str(b.resolve()): 1})
    assert code_of(bk.restore, b, root, env) == "migrate.backup_refused"  # allowed only before commit
    jr.write_journal("stopped", env, backups={str(b.resolve()): 1})
    monkeypatch.setattr(bk.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        bk.restore(b, root, env)
    assert jr.read_journal(env)["restoring"] and code_of(open_root, root, "workspace", env) == "update.interrupted"


def test_a_refused_second_restore_touches_nothing_of_the_first(root, env):
    b = swap_setup(root, env)
    leftover = root.with_name(".root.restore-other")
    leftover.mkdir()
    (leftover / "x").write_text("first run's staging")
    with lock_shared(root):
        assert code_of(bk.restore, b, root, env) == "migrate.running"
    assert (leftover / "x").read_text() == "first run's staging"


@pytest.mark.parametrize("fallback", [False, True])
def test_a_failed_restore_puts_carried_entries_back(root, env, monkeypatch, fallback):
    b = swap_setup(root, env)
    monkeypatch.setattr(bk, "_exchange", lambda a, c: False)
    real = os.rename

    def flaky(src, dst, *a, **k):
        if (fallback and str(src) == str(root.resolve()) and ".replaced-" in str(dst)) or (
            not fallback and ".restore-" in str(src) and "tools" in str(src) is False
        ):
            raise OSError("crash")
        if not fallback and str(dst) == str(root.resolve()) and ".restore-" in str(src):
            raise OSError("crash")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(bk.os, "rename", flaky)
    with pytest.raises(OSError):
        bk.restore(b, root, env)
    monkeypatch.setattr(bk.os, "rename", real)
    assert (root / "install/tools/orch-1/bin").read_text() == "tool"
    assert (root / ".state/index.sqlite").read_text() == "kept-index"


def test_state_rejected_and_body_are_data_not_derived(root, env):
    for rel in (".state/rejected/T-1.md", ".state/body/uid.md"):
        assert is_signed_path(rel) and not bk.is_derived(rel)
    (root / ".state/rejected").mkdir(parents=True)
    (root / ".state/rejected/a.md").write_text("prose")
    b = bk.create_backup(root, "workspace", 1, 2, env)
    assert (b / "data/.state/rejected/a.md").read_text() == "prose"
