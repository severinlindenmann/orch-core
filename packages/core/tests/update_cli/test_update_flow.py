"""``orch update`` end to end with a fake machine: happy path, every refusal, rollback, commit point, crashes, --use."""

from __future__ import annotations

import json

import pytest

from orch.migrate.journal import read_journal
from orch.ops.errors import OrchError
from orch.update import quarantine
from orch.update.engine import Crash
from orch.update.keys import embedded_roots
from orch.update.layout import read_record

from .world import OLD, World, record, use_test_root

NEW = "2.0.0.dev42"


@pytest.fixture(autouse=True)
def _root():
    with use_test_root():
        yield


@pytest.fixture
def w(tmp_path) -> World:
    world = World(tmp_path)
    world.source.publish("main", 42)
    return world


def code(fn, *a, **k) -> str:
    with pytest.raises(OrchError) as e:
        fn(*a, **k)
    return e.value.code


def untouched(w: World) -> None:
    assert w.layout.current_version() == OLD
    assert (w.state / "data.txt").read_text() == "before\n"
    assert read_journal(w.env) is None
    assert not quarantine.active(w.state)


def test_happy_path(w):
    o = w.updater().update()
    assert o.ok and o.action == "updated" and o.to_version == NEW
    assert w.layout.current_version() == NEW
    assert (w.state / "data.txt").read_text() == "migrated\n"
    assert read_journal(w.env)["phase"] == "committed"
    rec = read_record(w.layout)
    assert (rec.version, rec.seq("main"), rec.min_serial) == (NEW, 42, 2)
    assert (
        rec.uv["path"] == "/bin/true" and rec.release_key_id == "rk-3" and rec.trusted_roots == sorted(embedded_roots())
    )
    assert rec.root_key_id == "test-root-1" and rec.revocations_issued_at == "2026-10-20T00:00:00Z"
    assert {f["name"] for f in rec.files} == set(w.source.blobs) and rec.tool_dir.endswith(f"orch-{NEW}")
    # order: stop, quarantine start, stop, normal start; both versions kept
    assert w.services.events == ["stop", "start-q", "stop", "start"]
    assert w.services.started_with == [(f"orch-{NEW}", True), (f"orch-{NEW}", False)]
    assert w.layout.tool_dir(OLD).is_dir()
    assert w.phases[:3] == ["stopped", "backed-up", "installed"] and "committed" in w.phases
    assert not quarantine.active(w.state)
    assert (w.layout.cache_dir(NEW) / "release.json").is_file()
    again = w.updater().update()  # a second run is a no-op
    assert again.action == "current"


def test_the_backup_has_the_keys_at_0700(w):
    w.migrate_fail = True
    w.updater().update()
    backups = list((w.home / ".local/share/orch/backups").iterdir())
    assert len(backups) == 1
    assert (backups[0].stat().st_mode & 0o777) == 0o700
    assert (backups[0] / "data" / "keys" / "wsk").read_text() == "secret-key-material\n"


def test_verify_failure_changes_nothing(w):
    w.source.env["main"] = w.source.env["main"].replace(b'"sig":{', b'"sig":{"x":1,')
    assert code(w.updater().update) == "update.unverified"
    untouched(w)
    assert w.services.events == []


def test_a_tampered_file_changes_nothing(w):
    w.source.publish("main", 42, tamper_file=True)
    assert code(w.updater().update) == "update.unverified"
    untouched(w)
    assert w.services.events == []


def test_a_lower_seq_is_refused(w):
    from orch.update.layout import write_record

    write_record(w.layout, record(seq={"main": 50}))
    assert code(w.updater().update) == "update.stale"
    untouched(w)


def test_an_older_release_is_a_downgrade(w):
    from orch.update.layout import write_record

    write_record(w.layout, record(seq={}))
    w.source.publish("main", 40)
    assert code(w.updater().update) == "update.downgrade"
    untouched(w)


def test_low_disk_is_refused_before_anything_stops(w):
    w.free = 10
    assert code(w.updater().update) == "update.no_space"
    untouched(w)
    assert w.services.events == []


def test_active_agent_runs_are_refused(w):
    w.runs = [{"run": "r_1"}]
    assert code(w.updater().update) == "update.agents_running"
    untouched(w)
    assert w.services.events == []


def test_a_migrate_failure_rolls_back_code_and_data(w):
    w.migrate_fail = True
    o = w.updater().update()
    assert not o.ok and o.action == "rolled-back" and o.code == "update.rolled_back"
    assert str(o.log) in o.message
    untouched(w)
    assert (w.state / "keys" / "wsk").read_text() == "secret-key-material\n"
    assert w.services.events == ["stop", "stop", "start"]  # stopped for the update, started again on the old code
    assert w.services.started_with[-1] == (f"orch-{OLD}", False)
    assert read_record(w.layout).version == OLD and read_record(w.layout).seq("main") == 41


def test_a_quarantine_health_failure_rolls_back(w):
    w.services.problems_quarantine = ["the host does not answer"]
    o = w.updater().update()
    assert o.action == "rolled-back"
    untouched(w)
    assert w.layout.current_version() == OLD
    assert w.services.started_with[-1] == (f"orch-{OLD}", False)
    assert "does not answer" in w.out.getvalue() or "did not start cleanly" in o.message


def test_a_post_commit_failure_does_not_roll_back_the_data(w):
    w.services.problems_normal = ["the dashboard is not running"]
    o = w.updater().update()
    assert not o.ok and o.action == "updated" and o.code == "update.after_commit"
    assert f"orch update --use {OLD}" in o.message
    assert w.layout.current_version() == NEW
    assert (w.state / "data.txt").read_text() == "migrated\n"
    assert read_journal(w.env)["phase"] == "committed"
    assert read_record(w.layout).version == NEW


PHASES = ["stopped", "backed-up", "installed", "migrated", "health", "committed"]


@pytest.mark.parametrize("phase", PHASES)
def test_a_crash_after_each_phase_can_be_rolled_back(w, phase):
    w.crash_at = phase
    with pytest.raises(Crash):
        w.updater().update()
    w.crash_at = None
    if phase == "committed":
        assert code(w.updater().update, None, assume="rollback") == "update.after_commit"
        return
    assert code(w.updater().update) == "update.interrupted"  # no flag: it asks
    o = w.updater().update(assume="rollback")
    assert o.action == "rolled-back"
    untouched(w)
    assert (w.state / "keys" / "wsk").read_text() == "secret-key-material\n"
    assert w.services.started_with[-1] == (f"orch-{OLD}", False)


@pytest.mark.parametrize("phase", PHASES[:-1])
def test_a_crash_after_each_phase_can_be_finished(w, phase):
    w.crash_at = phase
    with pytest.raises(Crash):
        w.updater().update()
    w.crash_at = None
    o = w.updater().update(assume="finish")
    assert o.ok and o.action == "updated", o.message
    assert w.layout.current_version() == NEW
    assert (w.state / "data.txt").read_text() == "migrated\n"
    assert read_record(w.layout).version == NEW
    assert not quarantine.active(w.state)


def test_a_crash_at_the_commit_point_is_healed_by_the_next_update(w):
    w.crash_at = "committed"
    with pytest.raises(Crash):
        w.updater().update()
    w.crash_at = None
    assert read_record(w.layout).version == OLD  # the record write was lost
    w.services.up = []
    assert w.updater().update().action == "current"
    assert read_record(w.layout).version == NEW
    assert w.services.up == ["host", "runner", "dashboard", "hub"]  # L7: the services come back too
    assert w.services.started_with[-1] == (f"orch-{NEW}", False)


def test_rollback_after_the_flip_but_before_the_journal_flips_back(w):
    w.crash_at = "health"
    with pytest.raises(Crash):
        w.updater().update()
    from orch.update.layout import flip_current

    flip_current(w.layout, NEW)  # the crash happened between the flip and the committed journal
    w.crash_at = None
    w.updater().update(assume="rollback")
    untouched(w)


def test_use_selects_an_installed_version_and_keeps_the_data(w):
    w.updater().update()
    o = w.updater().use(OLD)
    assert o.ok and w.layout.current_version() == OLD
    assert (w.state / "data.txt").read_text() == "migrated\n"
    assert read_record(w.layout).version == OLD
    assert w.services.started_with[-1] == (f"orch-{OLD}", False)


def test_use_refuses_a_version_that_cannot_read_the_data(w):
    w.updater().update()
    w.old_cannot_read_new = True
    assert code(w.updater().use, OLD) == "format.newer"
    assert w.layout.current_version() == NEW


def test_use_refuses_a_version_that_is_not_installed(w):
    assert code(w.updater().use, "2.0.0.dev7") == "update.no_record"
    assert code(w.updater().use, "../../etc") == "update.unverified"


def test_a_second_update_at_the_same_time_is_refused(w):
    import os

    from orch import oslock

    fd = os.open(w.layout.lock, os.O_RDWR | os.O_CREAT, 0o600)
    oslock.flock(fd, oslock.LOCK_EX)
    try:
        assert code(w.updater().update) == "migrate.running"
    finally:
        os.close(fd)


def test_the_check_changes_nothing_and_reports(w):
    from orch.cli import update as cli

    out = __import__("io").StringIO()
    rc = cli.main(["--check", "--json"], env=w.env, out=out, err=out, seams=w.seams())
    doc = json.loads(out.getvalue())
    assert rc == 0 and doc["installed"] == OLD and doc["latest"] == NEW and doc["update_available"] is True
    assert "TEST release" in doc["changelog"]
    untouched(w)
    assert w.services.events == []


def test_only_the_host_runs_in_quarantine_and_parts_data_is_never_rolled_back(w):
    w.services.problems_quarantine = ["the host does not answer"]
    hub_db = w.state / "install" / "parts" / "hub" / "data" / "hub.db"
    o = w.updater().update()
    assert o.action == "rolled-back"
    quarantined = [names for names, q in w.services.started_names if q]
    assert quarantined == [["host"]]  # no hub, relay, publish or dashboard before the commit
    assert [names for names, q in w.services.started_names if not q][-1] == ["host", "runner", "dashboard", "hub"]
    backup = next((w.home / ".local/share/orch/backups").iterdir())
    assert not (backup / "data" / "install" / "parts").exists()
    assert hub_db.read_text() == "hub-state-changed\n"  # the rollback did not touch it either way


def test_use_refuses_a_version_newer_than_the_record(w):
    w.migrate_fail = True
    w.updater().update()  # rolled back; the new tools dir stays
    assert w.layout.is_complete(NEW)
    assert code(w.updater().use, NEW) == "format.newer"
    assert w.layout.current_version() == OLD


def test_use_refuses_while_an_update_is_unfinished_or_quarantined(w):
    w.updater().update()
    from orch.migrate.journal import write_journal

    write_journal("stopped", w.env, backups={})
    assert code(w.updater().use, OLD) == "update.interrupted"
    from orch.migrate.journal import clear_journal

    clear_journal(w.env)
    quarantine.activate(w.state, "ab" * 16)
    assert code(w.updater().use, OLD) == "update.interrupted"


@pytest.mark.parametrize(
    "out",
    [
        '{"ok": true, "roots": [{"steps": ["x"]}], "skipped": []}',
        '{"ok": true, "roots": [], "skipped": [{"root": "r", "why": "newer"}]}',
        "not json",
        '{"ok": false, "roots": []}',
    ],
)
def test_use_needs_a_clean_migrate_check(w, out):
    w.updater().update()
    w.check_out = out
    assert code(w.updater().use, OLD) == "format.newer"
    assert w.layout.current_version() == NEW


def test_a_run_that_starts_after_the_check_stops_the_update(w):
    calls = []

    def live(state):
        calls.append(1)
        return [] if len(calls) == 1 else [{"run": "r_late"}]

    w.runs = []
    seams = w.seams()
    seams.live_runs = live
    from orch.update.engine import Updater

    assert code(Updater(w.env, seams, w.out).update) == "update.agents_running"
    assert w.services.events == ["stop", "start"]  # stopped, then put back on the old code
    assert w.layout.current_version() == OLD and (w.state / "data.txt").read_text() == "before\n"


def test_free_space_counts_three_times_the_data(w):
    from orch.update.engine import dir_size

    data = dir_size(w.state)
    wheels = sum(len(b) for b in w.source.blobs.values())
    w.free = 3 * data + 2 * wheels - 1
    assert code(w.updater().update) == "update.no_space"
    w.free = 3 * data + 2 * wheels
    assert w.updater().update().ok


class Kill(BaseException):
    """A SIGKILL: no finally block runs."""


def test_resume_recovers_an_interrupted_restore_first_and_never_migrates(w, monkeypatch):
    from orch.migrate import backup as bk
    from orch.migrate.journal import restore_pending

    w.migrate_fail = True  # the update fails, so a rollback (a restore) starts ...
    real_exchange = bk._exchange

    def killed_after_swap(a, b):  # ... and is killed right after the swap, with no cleanup
        assert real_exchange(a, b)
        raise Kill()

    with monkeypatch.context() as m:
        m.setattr(bk, "_exchange", killed_after_swap)
        m.setattr(bk.shutil, "rmtree", lambda *a, **k: None)
        m.setattr(bk, "_put_back", lambda *a, **k: None)
        with pytest.raises(Kill):
            w.updater().update()
    assert restore_pending(w.state) is not None  # the sidecar record of the killed restore is there
    calls_before, installs = w.migrate_calls, []
    real_install = w.installer
    w.installer = lambda *a: installs.append(a) or real_install(*a)
    o = w.updater().update(assume="finish")
    assert o.action == "rolled-back" and not o.ok  # recovered, then the rollback was finished
    assert w.migrate_calls == calls_before and installs == []  # nothing migrated or installed meanwhile
    assert restore_pending(w.state) is None
    untouched(w)
    assert (w.state / "keys" / "wsk").read_text() == "secret-key-material\n"
