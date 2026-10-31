"""The data dir of each installed part (hub, relay, publish) is a migration root like the host and the workspaces."""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

from orch.install import cli as icli
from orch.install.state import Layout, load, save
from orch.migrate import backup as bk
from orch.migrate import cli as mcli
from orch.migrate import init_root, read_format, write_format
from orch.migrate.journal import journal_path, read_journal, unfinished
from orch.migrate.steps import CURRENT, DEFAULT, KINDS, protected_snapshot
from tests.install.test_install_cli import make
from tests.update_cli.world import World, use_test_root

PARTS = ("hub", "relay", "publish")


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
    env = ctx.runner.started[part][1]
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
    seen: list[tuple[list[str], bool, bool]] = []
    inner = w.services.start

    def start(orch_bin, names, *, quarantine):
        seen.append((list(names), quarantine, unfinished(w.env)))
        inner(orch_bin, names, quarantine=quarantine)

    w.services.start = start
    w.services.up = ["host", "hub", "relay", "publish"]
    assert w.updater().update().ok
    assert seen[0] == (["host"], True, True)  # quarantine: journal unfinished, only the host
    assert seen[-1] == (["host", "hub", "relay", "publish"], False, False)  # parts: journal finished
    for names, quarantine, open_ in seen:
        if open_:
            assert quarantine and not {"hub", "relay", "publish"} & set(names)


def test_parts_start_after_a_rollback_cleared_the_journal(w):
    seen: list[tuple[list[str], bool, bool]] = []
    inner = w.services.start

    def start(orch_bin, names, *, quarantine):
        seen.append((list(names), quarantine, read_journal(w.env) is not None))
        inner(orch_bin, names, quarantine=quarantine)

    w.services.start = start
    w.services.up = ["host", "hub", "relay", "publish"]
    w.migrate_fail = True
    assert w.updater().update().action == "rolled-back"
    assert seen == [(["host", "hub", "relay", "publish"], False, False)]


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
