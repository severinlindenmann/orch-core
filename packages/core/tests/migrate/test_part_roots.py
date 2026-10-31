"""The data dir of each installed part (hub, relay, publish) is a migration root like the host and the workspaces."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import threading
from pathlib import Path

import pytest

from orch.install import cli as icli
from orch.install import parts as P
from orch.install.state import Layout, load, save
from orch.migrate import backup as bk
from orch.migrate import cli as mcli
from orch.migrate import init_root, read_format, write_format
from orch.migrate.format import lock_root
from orch.migrate.journal import journal_path, read_journal, unfinished, write_journal
from orch.migrate.runner import apply
from orch.migrate.steps import (
    CURRENT,
    DEFAULT,
    KINDS,
    NESTED_PART_ROOTS,
    PART_KINDS,
    Registry,
    Step,
    parts_snapshot,
    protected_snapshot,
)
from orch.ops.errors import OrchError
from orch.update.engine import Crash, take_update_lock
from orch.update.layout import InstallLayout
from tests.install.test_install_cli import make
from tests.update_cli.world import OLD, World, use_test_root

PARTS = ("hub", "relay", "publish")
NEW = "2.0.0.dev42"


@pytest.fixture(autouse=True)
def _no_time_machine(monkeypatch):
    monkeypatch.setattr(bk, "_exclude_from_time_machine", lambda p: None)


def install(ctx, out, err, part):
    return icli.main([part, "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx)


# ------------------------------------------------------------------------------------------------- the kinds


def test_current_has_the_five_kinds_at_the_baseline():
    assert CURRENT == {"host": 1, "workspace": 1, "hub": 1, "relay": 1, "publish": 1}
    assert KINDS == ("host", "workspace", "hub", "relay", "publish")
    DEFAULT.check()


@pytest.mark.parametrize("part", PARTS)
def test_every_kind_can_be_snapshotted_and_backed_up(tmp_path, part):
    root = tmp_path / part
    init_root(root, part)
    (root / "keys").mkdir()
    (root / "keys" / "k").write_text("secret\n")
    assert protected_snapshot(root)["keys/k"]
    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    b = bk.create_backup(root, part, 1, 1, env)
    assert (b / "data" / "keys" / "k").read_text() == "secret\n"


# ------------------------------------------------------------------------------------------------- install


@pytest.mark.parametrize("part", PARTS)
def test_install_writes_format_json_for_each_part(tmp_path, part):
    ctx, out, err, _ = make(tmp_path)
    assert install(ctx, out, err, part) == 0, err.getvalue()
    data = ctx.layout.data(part)
    assert json.loads((data / "format.json").read_text()) == {"orch": part, "format": 1}
    assert read_format(data, part) == CURRENT[part]


@pytest.mark.parametrize("part", PARTS)
def test_install_adopts_an_existing_dir_without_format_json(tmp_path, part):
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data(part)
    data.mkdir(parents=True)
    (data / "old.db").write_text("pre-2.0.0 data\n")
    assert read_format(data, part) is None
    assert install(ctx, out, err, part) == 0, err.getvalue()
    assert read_format(data, part) == 1
    assert (data / "old.db").read_text() == "pre-2.0.0 data\n"


@pytest.mark.parametrize("part", PARTS)
def test_install_leaves_an_existing_format_json(tmp_path, part, monkeypatch):
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data(part)
    data.mkdir(parents=True)
    write_format(data, part, 1)
    (data / "old.db").write_text("x\n")
    before = (data / "format.json").read_bytes()
    monkeypatch.setitem(CURRENT, part, 2)  # the code is ahead of the data: install must NOT rewrite it
    assert install(ctx, out, err, part) == 0, err.getvalue()
    assert (data / "format.json").read_bytes() == before and read_format(data, part) == 1


@pytest.mark.parametrize("part", PARTS)
def test_install_refuses_a_newer_format(tmp_path, part):
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data(part)
    data.mkdir(parents=True)
    write_format(data, part, 2)
    assert install(ctx, out, err, part) == 1
    assert "made by a newer orch (format 2): update orch" in err.getvalue()
    assert read_format(data, part) == 2
    assert not ctx.runner.started and not ctx.runner.stopped  # nothing was stopped or started
    assert part not in load(ctx.layout)["parts"]


def test_install_refuses_a_damaged_format_json(tmp_path):
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data("relay")
    data.mkdir(parents=True)
    (data / "format.json").write_text("{nope")
    assert install(ctx, out, err, "relay") == 1
    assert "not orch data" in err.getvalue() and not ctx.runner.started


@pytest.mark.parametrize("part", PARTS)
def test_the_unit_env_names_the_update_journal(tmp_path, part):
    ctx, out, err, _ = make(tmp_path)
    assert install(ctx, out, err, part) == 0, err.getvalue()
    argv, env = ctx.runner.started[part]  # what the runner really receives
    binary, data = str(ctx.layout.bin / P.PARTS[part].binary), str(ctx.layout.data(part))
    want = {
        "hub": [binary, "serve", "--state", data],
        "relay": [binary, "serve", "--host", "127.0.0.1"],
        "publish": [binary, "run", "--data", data],
    }
    assert argv[:4] == want[part]
    assert env["ORCH_UPDATE_JOURNAL"] == str(journal_path(ctx.env))
    assert env["ORCH_UPDATE_JOURNAL"] == str(Layout(ctx.env).root / "update.json")
    assert not any("QUARANTINE" in k for k in env)  # parts are never quarantine_ok


# ------------------------------------------------------------------------------------------------- the update


@pytest.fixture
def w(tmp_path):
    with use_test_root():
        world = World(tmp_path)
        world.source.publish("main", 42)
        yield world


def installed(world: World, *parts: str) -> None:
    lay = Layout(world.env)
    st = load(lay)
    for p in parts:
        st["parts"][p] = {"port": 1}
    save(lay, st)


def real_roots(world: World):
    seams = dataclasses.replace(world.seams(), roots=None)
    return type(world.updater())(world.env, seams, io.StringIO())._roots()


def test_roots_lists_the_installed_parts_that_have_data(w):
    lay = Layout(w.env)
    for p in ("hub", "relay"):
        lay.data(p).mkdir(parents=True, exist_ok=True)
        write_format(lay.data(p), p, 1)
    lay.data("publish").mkdir(parents=True)  # data, but never installed
    installed(w, "hub", "relay")
    roots, _notes = real_roots(w)
    assert roots == [
        ("this computer's orch data", "host", w.state),
        ("the hub's data", "hub", lay.data("hub")),
        ("the relay's data", "relay", lay.data("relay")),
    ]


def test_roots_skips_an_installed_part_whose_data_dir_is_gone(w):
    installed(w, "relay")
    roots, _ = real_roots(w)
    assert [r[1] for r in roots] == ["host"]


def test_parts_start_only_after_the_journal_is_cleared(w):
    """B2: a part refuses to start while the journal is unfinished. The quarantine start (journal unfinished) carries
    only the host's own services; every start of a part is after the commit (journal "committed")."""
    seen = record_starts(w, lambda: unfinished(w.env))
    w.services.up = ["host", "hub", "relay", "publish"]
    assert w.updater().update().ok
    new_bin = w.layout.tool_bin(NEW)  # the real start arguments: the new orch, the service names, the mode
    assert seen[0] == (new_bin, ["host"], True, True)  # quarantine: journal unfinished, only the host
    assert seen[-1] == (new_bin, ["host", "hub", "relay", "publish"], False, False)  # parts: journal finished
    for _bin, names, quarantine, open_ in seen:
        if open_:
            assert quarantine and not {"hub", "relay", "publish"} & set(names)


def test_parts_start_after_a_rollback_cleared_the_journal(w):
    seen = record_starts(w, lambda: read_journal(w.env) is not None)
    w.services.up = ["host", "hub", "relay", "publish"]
    w.migrate_fail = True
    assert w.updater().update().action == "rolled-back"
    assert seen == [(w.layout.tool_bin(OLD), ["host", "hub", "relay", "publish"], False, False)]


def record_starts(w: World, journal_open) -> list[tuple[Path, list[str], bool, bool]]:
    """Wrap the services' start: (orch binary, names, quarantine, journal open at that moment) per start."""
    seen: list[tuple[Path, list[str], bool, bool]] = []
    inner = w.services.start

    def start(orch_bin, names, *, quarantine):
        seen.append((orch_bin, list(names), quarantine, journal_open()))
        inner(orch_bin, names, quarantine=quarantine)

    w.services.start = start
    return seen


def test_the_update_backs_up_the_part_roots(w):
    installed(w, "hub")
    write_format(Layout(w.env).data("hub"), "hub", 1)
    seams = dataclasses.replace(w.seams(), roots=None)
    o = type(w.updater())(w.env, seams, io.StringIO()).update()
    assert o.ok, o.message
    doc = read_journal(w.env)
    assert {b["kind"] for b in doc["backup_list"]} == {"host", "hub"}
    assert {b["label"] for b in doc["backup_list"]} == {"this computer's orch data", "the hub's data"}


def test_a_failed_update_puts_a_part_root_back(w):
    installed(w, "hub")
    data = Layout(w.env).data("hub")
    write_format(data, "hub", 1)
    w.migrate_fail = True
    seams = dataclasses.replace(w.seams(), roots=None)
    o = type(w.updater())(w.env, seams, io.StringIO()).update()
    assert o.action == "rolled-back"
    assert read_format(data, "hub") == 1 and (data / "hub.db").read_text() == "hub-state\n"
    assert read_journal(w.env) is None


# ------------------------------------------------------------------------------------------------- orch migrate


def test_migrate_with_part_roots_at_format_1_is_a_no_op(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    init_root(Path(env["ORCH_STATE_DIR"]), "host")
    lay = Layout(env)
    save(lay, {"v": 1, "address": None, "domain": None, "parts": {p: {} for p in PARTS}, "workspaces": []})
    for p in PARTS:
        init_root(lay.data(p), p)
    for mode in ("--check", "--apply"):
        out, err = io.StringIO(), io.StringIO()
        assert mcli.main([mode, "--json"], env=env, out=out, err=err) == 0, out.getvalue() + err.getvalue()
        doc = json.loads(out.getvalue())
        assert doc["ok"] and doc["skipped"] == []
        assert [r["kind"] for r in doc["roots"]] == ["host", *PARTS]
        assert all(r["steps"] == [] and r["at"] == r["target"] == 1 and "backup" not in r for r in doc["roots"])
    assert not bk.backups_dir(env).exists()
    assert all(read_format(lay.data(p), p) == 1 for p in PARTS)


def test_migrate_ignores_a_part_that_is_not_installed(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    init_root(Path(env["ORCH_STATE_DIR"]), "host")
    init_root(Layout(env).data("hub"), "hub")  # a data dir the install record does not know
    out, err = io.StringIO(), io.StringIO()
    assert mcli.main(["--check", "--json"], env=env, out=out, err=err) == 0
    assert [r["kind"] for r in json.loads(out.getvalue())["roots"]] == ["host"]


# ------------------------------------------------------------------------------- 1: a part dir without format.json

NO_FORMAT = "the hub's data has no format yet: run `orch install hub` again"


def real_updater(world: World):
    seams = dataclasses.replace(world.seams(), roots=None)  # the engine's own root list
    return type(world.updater())(world.env, seams, world.out)


def test_an_unmarked_part_dir_is_no_root_and_the_update_goes_through(w):
    data = Layout(w.env).data("hub")  # the world made it: hub.db, no format.json
    installed(w, "hub")
    assert not (data / "format.json").exists()
    roots, notes = real_roots(w)
    assert [r[1] for r in roots] == ["host"] and notes == [NO_FORMAT]
    o = real_updater(w).update()
    assert o.ok and o.action == "updated", o.message
    assert NO_FORMAT in w.out.getvalue()  # shown to the person
    assert {b["kind"] for b in read_journal(w.env)["backup_list"]} == {"host"}  # never backed up as a root
    assert not (data / "format.json").exists() and not (data / "format.lock").exists()  # never locked or marked
    assert not list(bk.backups_dir(w.env).glob("*-hub-*"))


def test_migrate_notes_an_unmarked_part_dir_and_the_old_version_still_fits(tmp_path):
    from orch.update.engine import Updater

    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    init_root(Path(env["ORCH_STATE_DIR"]), "host")
    lay = Layout(env)
    save(lay, {"v": 1, "address": None, "domain": None, "parts": {p: {} for p in PARTS}, "workspaces": []})
    init_root(lay.data("relay"), "relay")
    lay.data("hub").mkdir(parents=True)
    (lay.data("hub") / "hub.db").write_text("x\n")
    # publish is installed but has no data dir at all: no root, no note
    for mode in ("--check", "--apply"):
        out, err = io.StringIO(), io.StringIO()
        assert mcli.main([mode, "--json"], env=env, out=out, err=err) == 0
        doc = json.loads(out.getvalue())
        assert [r["kind"] for r in doc["roots"]] == ["host", "relay"] and doc["skipped"] == []
        assert doc["warnings"] == [NO_FORMAT]
        assert Updater._data_fits(0, out.getvalue())  # what `orch update --use` asks of the old version
    out, err = io.StringIO(), io.StringIO()
    assert mcli.main(["--check"], env=env, out=out, err=err) == 0
    assert f"warning: {NO_FORMAT}" in out.getvalue()
    assert not (lay.data("hub") / "format.json").exists() and not bk.backups_dir(env).exists()


# ------------------------------------------------------------------------------------ 2: disjoint roots


def fail_migrate(world: World, mutate) -> None:
    """The migrate seam changes data (``mutate``) and then fails: the rollback must really restore it."""
    inner = world.run_orch

    def run_orch(orch_bin, args):
        if list(args)[:1] == ["migrate"] and "--apply" in args:
            mutate()
            return 5, json.dumps({"ok": False})
        return inner(orch_bin, args)

    world.run_orch = run_orch


def spy_restores(monkeypatch, own) -> list[Path]:
    """Record the roots in the order they are restored; ``own(root)`` runs right after that root's own restore."""
    seen: list[Path] = []
    real = bk.restore

    def restore(backup, root, *a, **k):
        real(backup, root, *a, **k)
        seen.append(Path(root))
        own(Path(root))

    monkeypatch.setattr(bk, "restore", restore)
    return seen


def test_the_host_snapshot_and_backup_leave_the_nested_part_roots_out(w):
    hub = Layout(w.env).data("hub")
    write_format(hub, "hub", 1)
    (hub / "keys").mkdir()
    (hub / "keys" / "hub.key").write_text("K")
    (hub / "revocations.jsonl").write_text("r1\n")
    (hub.parent / "pid").write_text("4242")  # the runner's own files beside the data dir belong to the host root
    (hub.parent / "cmd.json").write_text("{}")
    snap = protected_snapshot(w.state)
    assert snap["keys/wsk"] and not [k for k in snap if k.startswith("install/parts/hub/data")]
    assert protected_snapshot(hub)["keys/hub.key"]  # the part root has its own
    b = bk.create_backup(w.state, "host", 1, 1, w.env)
    assert (b / "data" / "data.txt").exists() and not (b / "data" / "install" / "parts" / "hub" / "data").exists()
    assert (b / "data" / "install" / "parts" / "hub" / "pid").read_text() == "4242"  # only the data dirs are left out
    assert (b / "data" / "install" / "parts" / "hub" / "cmd.json").exists()
    pb = bk.create_backup(hub, "hub", 1, 1, w.env)  # and is backed up on its own
    assert (pb / "data" / "keys" / "hub.key").read_text() == "K" and (pb / "data" / "format.json").exists()


def test_the_rollback_restores_the_parts_first_then_the_host():
    from orch.update.engine import restore_order

    entries = [{"kind": k} for k in ("host", "hub", "workspace", "relay", "publish")]
    assert [e["kind"] for e in restore_order(entries)] == ["hub", "relay", "publish", "host", "workspace"]


def test_a_host_restore_after_a_failed_update_leaves_the_part_root_as_its_own_restore_made_it(w, monkeypatch):
    installed(w, "hub")
    hub = Layout(w.env).data("hub")
    write_format(hub, "hub", 1)
    (hub / "revocations.jsonl").write_text("r1\n")

    def mutate():  # inside the migrate seam: the root really changed before the failure
        (w.state / "data.txt").write_text("host-mutated\n")
        (hub / "hub.db").write_text("hub-mutated\n")
        (hub / "revocations.jsonl").write_text("r1\nr2\n")

    fail_migrate(w, mutate)
    restored = spy_restores(monkeypatch, lambda root: (hub / "after-own-restore").write_text("x") if root == hub else 0)
    o = real_updater(w).update()
    assert o.action == "rolled-back", o.message
    assert restored == [hub, w.state]  # pinned: the part first, then the host
    assert (hub / "hub.db").read_text() == "hub-state\n"  # the part's own restore happened
    assert (hub / "revocations.jsonl").read_text().splitlines() == ["r1", "r2"]  # merged, never taken away
    assert (hub / "after-own-restore").read_text() == "x"  # the host's restore did not touch the part dir
    assert (w.state / "data.txt").read_text() == "before\n" and read_format(hub, "hub") == 1
    assert read_journal(w.env) is None


def test_a_crash_then_rollback_restores_the_part_first_too(w, monkeypatch):
    installed(w, "hub")
    hub = Layout(w.env).data("hub")
    write_format(hub, "hub", 1)
    w.crash_at = "migrated"  # the world's migrate seam has changed host and hub data by now
    with pytest.raises(Crash):
        real_updater(w).update()
    w.crash_at = None
    assert (hub / "hub.db").read_text() == "hub-state-changed\n"
    restored = spy_restores(monkeypatch, lambda root: None)
    o = real_updater(w).update(assume="rollback")  # resume after the crash: the engine's own root list again
    assert o.action == "rolled-back", o.message
    assert restored == [hub, w.state]
    assert (hub / "hub.db").read_text() == "hub-state\n" and (w.state / "data.txt").read_text() == "before\n"


def test_a_crash_then_finish_migrates_the_part_root_too(w):
    installed(w, "hub")
    hub = Layout(w.env).data("hub")
    write_format(hub, "hub", 1)
    w.crash_at = "installed"
    with pytest.raises(Crash):
        real_updater(w).update()
    w.crash_at = None
    o = real_updater(w).update(assume="finish")
    assert o.ok and o.action == "updated", o.message
    assert f"migrated:{hub}" in w.phases and f"migrated:{w.state}" in w.phases
    assert {b["kind"] for b in read_journal(w.env)["backup_list"]} == {"host", "hub"}


# ------------------------------------------------------------------------------------ 3: the adoption gate


def test_adoption_is_refused_once_the_format_is_raised(tmp_path, monkeypatch):
    ctx, out, err, calls = make(tmp_path)
    data = ctx.layout.data("relay")
    data.mkdir(parents=True)
    (data / "old.db").write_text("pre-2.0.0 data\n")
    monkeypatch.setitem(CURRENT, "relay", 2)
    assert install(ctx, out, err, "relay") == 1
    assert "this relay data has no format.json: restore it from a backup or remove it" in err.getvalue()
    assert read_format(data, "relay") is None and (data / "old.db").read_text() == "pre-2.0.0 data\n"
    assert not ctx.runner.started and not ctx.runner.stopped
    assert not any(c[:3] == ["uv", "tool", "install"] for c in calls)
    assert "relay" not in load(ctx.layout)["parts"]
    with pytest.raises(icli.InstallError):  # and the helper itself is gated, not only the early check
        icli._mark_root(data, "relay", born=False)
    assert icli.ADOPT_BASELINE_ONLY_AT == 1


def test_a_fresh_dir_is_born_at_the_code_format_even_when_it_is_raised(tmp_path, monkeypatch):
    ctx, out, err, _ = make(tmp_path)
    monkeypatch.setitem(CURRENT, "publish", 2)
    assert install(ctx, out, err, "publish") == 0, err.getvalue()
    assert read_format(ctx.layout.data("publish"), "publish") == 2


class Died(BaseException):
    """The machine went down: nothing after this point ran."""


def test_a_crash_between_the_hubs_key_init_and_its_marker_is_picked_up_by_the_next_install(tmp_path, monkeypatch):
    ctx, out, err, calls = make(tmp_path)
    data = ctx.layout.data("hub")
    inner = ctx.run

    def run(argv, env=None):
        if argv[1:2] == ["keys"] and "init" in argv:
            (data / "keys").mkdir(parents=True)  # the real init fills the folder
            (data / "keys" / "hub.key").write_text("K")
        return inner(argv, env)

    ctx.run = run
    real_write = icli.write_format
    monkeypatch.setattr(icli, "write_format", lambda *a: (_ for _ in ()).throw(Died()))
    with pytest.raises(Died):
        install(ctx, out, err, "hub")
    assert (data / "keys" / "hub.key").exists() and read_format(data, "hub") is None  # keys, no marker
    assert not ctx.runner.started and "hub" not in load(ctx.layout)["parts"]
    inits = [c for c in calls if c[1:2] == ["keys"] and "init" in c]
    assert len(inits) == 1  # the marker is written only after the init reported success (rc 0)
    monkeypatch.setattr(icli, "write_format", real_write)
    assert install(ctx, out, err, "hub") == 0, err.getvalue()  # next install: the baseline adopts it
    assert read_format(data, "hub") == 1 and (data / "keys" / "hub.key").read_text() == "K"


def test_a_failed_key_init_writes_no_marker_and_leaves_no_dir(tmp_path):
    ctx, out, err, _ = make(tmp_path)
    ctx.run = lambda argv, env=None: (1, "boom") if argv[1:2] == ["keys"] else (0, "")
    assert install(ctx, out, err, "hub") == 1 and "keys init failed" in err.getvalue()
    assert not ctx.layout.data("hub").exists()


def test_a_crash_between_them_is_refused_not_guessed_once_the_format_is_raised(tmp_path, monkeypatch):
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data("hub")
    data.mkdir(parents=True)
    (data / "hub.key").write_text("K")  # what the crash left: keys, no marker
    monkeypatch.setitem(CURRENT, "hub", 2)
    assert install(ctx, out, err, "hub") == 1
    assert "this hub data has no format.json: restore it from a backup or remove it" in err.getvalue()
    assert not ctx.runner.started and not ctx.runner.stopped


# ------------------------------------------------------------------------------- 4: an unfinished update, a busy root


def with_running_relay(tmp_path):
    ctx, out, err, calls = make(tmp_path)
    assert install(ctx, out, err, "relay") == 0, err.getvalue()
    before = (dict(ctx.runner.started), load(ctx.layout))
    calls.clear()
    return ctx, out, err, calls, before


WAY_OUT = "an update was interrupted: run `orch update` to finish or roll back"


def assert_nothing_changed(ctx, calls, before):
    assert ctx.runner.stopped == [] and ctx.runner.started == before[0]  # nothing was stopped or started
    assert not [c for c in calls if c[:2] == ["uv", "tool"]]  # nothing was installed
    assert load(ctx.layout) == before[1]


def test_install_part_refuses_while_an_update_is_unfinished(tmp_path):
    ctx, out, err, calls, before = with_running_relay(tmp_path)
    write_journal("backed-up", ctx.env)
    for part in PARTS:
        err.truncate(0), err.seek(0)
        assert install(ctx, out, err, part) == 1
        assert WAY_OUT in err.getvalue()
    assert_nothing_changed(ctx, calls, before)
    assert read_journal(ctx.env)["phase"] == "backed-up"


def test_install_server_refuses_while_an_update_is_unfinished(tmp_path):
    ctx, out, err, calls, before = with_running_relay(tmp_path)
    write_journal("installed", ctx.env)
    rc = icli.main(["server", "--role", "edge"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert rc == 1 and WAY_OUT in err.getvalue()
    assert_nothing_changed(ctx, calls, before)


def test_setup_server_parts_refuse_while_an_update_is_unfinished(tmp_path):
    ctx, out, err, calls, before = with_running_relay(tmp_path)
    write_journal("stopped", ctx.env)
    with pytest.raises(icli.InstallError, match="an update was interrupted"):
        icli.full_server(ctx, ["publish", "relay", "hub"])
    assert_nothing_changed(ctx, calls, before)
    assert icli.full_server(ctx, []) == {}  # a workspace-only setup installs no part: nothing to refuse


def test_a_damaged_journal_counts_as_unfinished(tmp_path):
    ctx, out, err, calls, before = with_running_relay(tmp_path)
    journal_path(ctx.env).write_text("{nope")
    assert install(ctx, out, err, "relay") == 1 and WAY_OUT in err.getvalue()
    assert_nothing_changed(ctx, calls, before)


def test_a_committed_journal_does_not_block_install(tmp_path):
    ctx, out, err, _calls, _before = with_running_relay(tmp_path)
    write_journal("committed", ctx.env)
    assert install(ctx, out, err, "relay") == 0, err.getvalue()


@pytest.mark.parametrize("held_exclusive", [False, True])
def test_format_json_is_written_under_the_exclusive_lock_and_a_busy_root_refuses(tmp_path, held_exclusive, monkeypatch):
    monkeypatch.setattr(icli, "MARKER_WAIT", 0.0)  # no waiting: the lock stays held
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data("relay")
    data.mkdir(parents=True)
    (data / "old.db").write_text("x\n")
    fd = lock_root(data, exclusive=held_exclusive)  # a service holds the root open (shared), or a migration (exclusive)
    try:
        assert install(ctx, out, err, "relay") == 1
        assert "the relay's data is in use" in err.getvalue()
        assert read_format(data, "relay") is None and "relay" not in load(ctx.layout)["parts"]
    finally:
        os.close(fd)
    assert install(ctx, out, err, "relay") == 0, err.getvalue()
    assert read_format(data, "relay") == 1
    os.close(lock_root(data, exclusive=True))  # the install let go of the lock again


def test_a_marker_lock_that_is_let_go_of_in_time_is_waited_for(tmp_path, monkeypatch):
    monkeypatch.setattr(icli, "MARKER_WAIT", 5.0)
    ctx, out, err, _ = make(tmp_path)
    data = ctx.layout.data("relay")
    data.mkdir(parents=True)
    (data / "old.db").write_text("x\n")
    fd = lock_root(data, exclusive=False)  # a part that was just stopped, still letting go
    timer = threading.Timer(0.4, os.close, args=(fd,))
    timer.start()
    try:
        assert install(ctx, out, err, "relay") == 0, err.getvalue()
    finally:
        timer.join()
    assert read_format(data, "relay") == 1


@pytest.mark.parametrize("part", PARTS)
def test_a_marker_refusal_after_the_stop_puts_the_old_part_back(tmp_path, monkeypatch, part):
    monkeypatch.setattr(icli, "MARKER_WAIT", 0.0)
    ctx, out, err, calls = make(tmp_path)
    assert install(ctx, out, err, part) == 0, err.getvalue()
    data = ctx.layout.data(part)
    (data / "format.json").unlink()  # an unmarked dir at the baseline: the install adopts it, which needs the lock
    resumed: list[str] = []
    ctx.runner.resume = resumed.append
    fd = lock_root(data, exclusive=False)  # a stale part process holds the root
    try:
        assert install(ctx, out, err, part) == 1
    finally:
        os.close(fd)
    assert f"the {part}'s data is in use" in err.getvalue()
    assert ctx.runner.stopped == [part]  # it WAS stopped (the refusal came after) ...
    assert resumed == [part]  # ... and is started again, never left stopped, new code, no marker
    assert read_format(data, part) is None


# ------------------------------------------------------------------- 5: install and update are serialized by one lock


def update_lock_of(ctx):
    return take_update_lock(InstallLayout.of(ctx.env))


@pytest.mark.parametrize("part", PARTS)
def test_install_refuses_before_it_stops_anything_while_the_update_lock_is_held(tmp_path, part):
    ctx, out, err, calls, before = with_running_relay(tmp_path)
    fd = update_lock_of(ctx)  # an `orch update` is running
    try:
        assert install(ctx, out, err, part) == 1
        assert "an update is running" in err.getvalue()
        assert_nothing_changed(ctx, calls, before)
        write_journal("backed-up", ctx.env)  # the lock comes first: the same answer, still nothing touched
        err.truncate(0), err.seek(0)
        assert install(ctx, out, err, part) == 1 and "an update is running" in err.getvalue()
        assert_nothing_changed(ctx, calls, before)
    finally:
        os.close(fd)
    assert read_format(ctx.layout.data("relay"), "relay") == 1


def test_the_lock_is_held_for_the_whole_install_and_let_go_of_after_it(tmp_path):
    ctx, out, err, _ = make(tmp_path)
    seen: dict[str, str] = {}

    def update_is_refused(at: str) -> None:
        try:
            os.close(update_lock_of(ctx))
            seen[at] = "free"
        except OrchError as exc:
            seen[at] = exc.code

    inner_run, inner_health = ctx.run, ctx.health

    def run(argv, env=None):
        update_is_refused("uv " + argv[1] if argv[0] == "uv" else "keys")
        return inner_run(argv, env)

    def health(h, p):
        update_is_refused("ready")  # long after the marker is written
        return inner_health(h, p)

    ctx.run, ctx.health = run, health
    assert install(ctx, out, err, "hub") == 0, err.getvalue()
    assert read_format(ctx.layout.data("hub"), "hub") == 1
    assert seen and set(seen.values()) == {"migrate.running"}, seen
    assert {"uv tool", "keys", "ready"} <= set(seen)
    os.close(update_lock_of(ctx))  # released again


def test_the_lock_is_released_after_a_refused_install(tmp_path):
    ctx, out, err, _, _ = with_running_relay(tmp_path)
    write_journal("backed-up", ctx.env)
    assert install(ctx, out, err, "relay") == 1
    os.close(update_lock_of(ctx))


def test_the_update_uses_the_same_lock_file(w):
    fd = take_update_lock(InstallLayout.of(w.env))
    try:
        with pytest.raises(OrchError) as e:
            w.updater().update()
        assert e.value.code == "migrate.running"
    finally:
        os.close(fd)
    assert w.updater().update().ok


# ------------------------------------------------------------------ 6: an unmarked part dir, an empty one


def test_an_unmarked_part_dir_refuses_the_update_before_anything_is_stopped_once_the_format_is_raised(w, monkeypatch):
    data = Layout(w.env).data("hub")  # the world made it: hub.db, no format.json
    installed(w, "hub")
    monkeypatch.setitem(CURRENT, "hub", 2)
    with pytest.raises(OrchError) as e:
        real_roots(w)
    assert e.value.code == "format.missing" and "no format.json" in e.value.message
    with pytest.raises(OrchError) as e:
        real_updater(w).update()
    assert e.value.code == "format.missing"
    assert w.services.events == [] and read_journal(w.env) is None  # nothing stopped, nothing journalled
    assert not (data / "format.json").exists() and (data / "hub.db").read_text() == "hub-state\n"


def test_an_empty_unmarked_part_dir_is_only_a_note_even_when_the_format_is_raised(w, monkeypatch):
    data = Layout(w.env).data("relay")
    data.mkdir(parents=True)
    installed(w, "relay")
    monkeypatch.setitem(CURRENT, "relay", 2)
    roots, notes = real_roots(w)
    assert [r[1] for r in roots] == ["host"] and notes == [
        "the relay's data has no format yet: run `orch install relay` again"
    ]


def test_migrate_refuses_an_unmarked_part_dir_once_the_format_is_raised_and_migrates_nothing(tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    init_root(Path(env["ORCH_STATE_DIR"]), "host")
    lay = Layout(env)
    save(lay, {"v": 1, "address": None, "domain": None, "parts": {"hub": {}}, "workspaces": []})
    lay.data("hub").mkdir(parents=True)
    (lay.data("hub") / "hub.db").write_text("x\n")
    monkeypatch.setitem(CURRENT, "hub", 2)
    for mode in ("--check", "--apply"):
        out, err = io.StringIO(), io.StringIO()
        assert mcli.main([mode, "--json"], env=env, out=out, err=err) == 3
        assert json.loads(out.getvalue())["code"] == "format.missing"
    assert not bk.backups_dir(env).exists()


@pytest.mark.parametrize("part", PARTS)
def test_an_empty_existing_dir_is_born_at_the_raised_format_and_never_refused(tmp_path, monkeypatch, part):
    ctx, out, err, _ = make(tmp_path)
    ctx.layout.data(part).mkdir(parents=True)  # exists, empty
    monkeypatch.setitem(CURRENT, part, 2)
    icli._check_root(ctx.layout.data(part), part)  # the early check lets it through ...
    assert install(ctx, out, err, part) == 0, err.getvalue()  # ... and the marker write agrees
    assert read_format(ctx.layout.data(part), part) == 2


# ------------------------------------------------------------ 7: only the data dirs are the part roots' own


def test_the_nested_roots_are_exactly_the_layouts_data_dirs_and_nothing_else_of_install_parts_is_left_out(tmp_path):
    lay = Layout({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "state")})
    assert NESTED_PART_ROOTS == tuple(lay.data(p).relative_to(lay.root.parent).as_posix() for p in PART_KINDS)
    assert set(bk._SKIP_REL) >= set(NESTED_PART_ROOTS) and "install/parts" not in bk._SKIP_REL
    # what lives under install/parts/<part>/: data (the root), and the runner's pid and cmd.json (see install.service)
    from orch.install.service import PidfileRunner

    runner = PidfileRunner(lay)
    for p in PART_KINDS:
        assert runner._pidfile(p).parent == lay.data(p).parent and runner._cmdfile(p).parent == lay.data(p).parent


@pytest.fixture
def host_root(tmp_path):
    root = tmp_path / "state"
    init_root(root, "host")
    (root / "data.txt").write_text("before\n")
    for p in ("hub", "relay"):
        d = root / "install" / "parts" / p / "data"
        d.mkdir(parents=True)
        (d / "db").write_text(f"{p}\n")
        (root / "install" / "parts" / p / "pid").write_text("1")
    return root


def host_registry(step):
    return Registry([step], {"host": 2})


@pytest.mark.parametrize(
    "rel",
    [
        "install/parts/hub/data/db",  # a part's data, rewritten
        "install/parts/hub/data/new.txt",  # a new file in a part's data
        "install/parts/relay/pid",  # the runner's pid, outside the data dir
        "install/parts/publish/cmd.json",  # a file in a part's folder that did not exist
    ],
)
def test_a_host_step_that_touches_install_parts_is_refused(host_root, rel):
    def touch(ctx):
        (host_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (host_root / rel).write_text("touched")

    with pytest.raises(OrchError) as e:
        apply(host_root, "host", host_registry(Step("host", 1, 2, "touch", touch)))
    assert e.value.code == "migrate.signed_path" and "install/parts" in e.value.message
    assert read_format(host_root, "host") == 1  # the format was not moved


def test_a_host_step_that_removes_a_part_dir_is_refused(host_root):
    import shutil

    def wipe(ctx):
        shutil.rmtree(host_root / "install" / "parts" / "hub")

    with pytest.raises(OrchError) as e:
        apply(host_root, "host", host_registry(Step("host", 1, 2, "wipe", wipe)))
    assert e.value.code == "migrate.signed_path" and read_format(host_root, "host") == 1


def test_a_host_step_may_not_declare_a_write_under_install_parts(host_root):
    def write(ctx):
        ctx.write_file("install/parts/hub/data/db", b"x")

    step = Step("host", 1, 2, "write", write, writes=("install/parts/*",))
    with pytest.raises(OrchError) as e:
        apply(host_root, "host", host_registry(step))
    assert e.value.code == "migrate.signed_path" and "parts' folders" in e.value.message
    assert (host_root / "install/parts/hub/data/db").read_text() == "hub\n"


def test_a_host_step_that_leaves_the_parts_alone_goes_through(host_root):
    before = parts_snapshot(host_root)
    assert "hub/data/db" in before and "relay/pid" in before

    def fine(ctx):
        ctx.write_file("data.txt", b"after\n")

    apply(host_root, "host", host_registry(Step("host", 1, 2, "fine", fine, writes=("data.txt",))))
    assert read_format(host_root, "host") == 2 and (host_root / "data.txt").read_text() == "after\n"
    assert parts_snapshot(host_root) == before


def test_the_parts_snapshot_is_for_the_host_only(tmp_path):
    root = tmp_path / "hub"
    init_root(root, "hub")

    def touch(ctx):  # a part root has no install/parts of its own to guard: nothing special happens
        (root / "install" / "parts").mkdir(parents=True)

    apply(root, "hub", Registry([Step("hub", 1, 2, "touch", touch)], {"hub": 2}))
    assert read_format(root, "hub") == 2


# ------------------------------------------------- 8: a crash in the host's swap after the parts were restored


class Kill(BaseException):
    """A SIGKILL: no finally block runs."""


def kill_the_host_swap(mp, state: Path, hub: Path, where: str) -> None:
    """Every restore is real, except the host's: it dies in its swap (``between``: the root is renamed aside and the
    staged one is not in place yet; ``swapped``: the exchange is done, nothing after it ran). A part's restore is real
    and leaves a file in the part root afterwards: what the host's swap and recovery must carry over."""
    real_restore, real_exchange, real_rename = bk.restore, bk._exchange, os.rename

    def rename(s, d, *a, **k):
        if (".restore-" in Path(s).name or ".replaced-" in Path(s).name) and Path(d) == state.resolve():
            raise Kill()
        return real_rename(s, d, *a, **k)

    def exchange(a, c):
        assert real_exchange(a, c)
        raise Kill()

    def restore(backup, root, *a, **k):
        if Path(root).resolve() != state.resolve():
            real_restore(backup, root, *a, **k)
            (hub / "after-own-restore").write_text("x")
            return
        with mp.context() as m:  # a SIGKILL runs no cleanup
            m.setattr(bk, "_put_back", lambda s, r: None)
            m.setattr(bk.shutil, "rmtree", lambda *a, **k: None)
            if where == "between":
                m.setattr(bk, "_exchange", lambda a, c: False)
                m.setattr(bk.os, "rename", rename)
            else:
                m.setattr(bk, "_exchange", exchange)
            real_restore(backup, root, *a, **k)

    mp.setattr(bk, "restore", restore)


@pytest.mark.parametrize("assume", ["rollback", "finish"])
@pytest.mark.parametrize("where, host_after_recovery", [("between", "host-mutated\n"), ("swapped", "before\n")])
def test_a_crash_in_the_host_swap_after_the_parts_were_restored_keeps_the_part_data(
    w, monkeypatch, where, host_after_recovery, assume
):
    """``between`` is recovery that rolls the swap BACK (the old host root returns), ``swapped`` is recovery that rolls
    it FORWARD (the restored host root stays). Either way the part root the update restored is carried through in
    place."""
    installed(w, "hub")
    hub = Layout(w.env).data("hub")
    write_format(hub, "hub", 1)

    def mutate():
        (w.state / "data.txt").write_text("host-mutated\n")
        (hub / "hub.db").write_text("hub-mutated\n")

    fail_migrate(w, mutate)
    with monkeypatch.context() as mp:
        kill_the_host_swap(mp, w.state, hub, where)
        with pytest.raises(Kill):
            real_updater(w).update()
    assert bk.recover_interrupted(w.state) is True  # what `orch migrate --apply` or the next update does first
    assert (hub / "hub.db").read_text() == "hub-state\n"  # the part's own restore survived the host's crash ...
    assert (hub / "after-own-restore").read_text() == "x"  # ... and nothing replaced the part's folder
    assert read_format(hub, "hub") == 1
    assert (w.state / "data.txt").read_text() == host_after_recovery
    o = real_updater(w).update(assume=assume)  # `finish` goes forward, hits the failing migrate and rolls back again
    assert o.action == "rolled-back", o.message
    assert (hub / "hub.db").read_text() == "hub-state\n" and (w.state / "data.txt").read_text() == "before\n"
    assert read_format(hub, "hub") == 1 and read_journal(w.env) is None
