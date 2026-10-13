"""First-use gaps (dashboard PR #450 runbook): ``orch host register`` without ``--peer`` in either custody,
``orch host disable`` (the reverse of ``enable``) and ``orch dashboard``.

The service manager is the fake of ``test_host_mode_on`` (``cli.SERVICE_MANAGER``): no launchctl or systemctl runs, and
every file lands under a temporary HOME."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from orch.host import cli as host_cli
from orch.host import enable as svc
from tests.host.lifecycle_helpers import UID
from tests.host.test_host_mode_on import cli, fake, host, world  # noqa: F401 - fixtures


def _wid(root: Path) -> str:
    from orch.ops.caps import Files
    from orch.ops.runtime import _config

    return _config(root, Files.direct())["workspace"]["id"]


# --- 1. the peer default in either custody -------------------------------------------------------------------------


def test_the_peer_default_finds_a_passphrase_key(world):  # noqa: F811
    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert list(host_cli.default_peers(Path(env["ORCH_STATE_DIR"]), root, env)) == [str(UID)]


def test_the_peer_default_finds_a_convenience_key(world, monkeypatch):  # noqa: F811
    from orch.custody import convenience as conv
    from orch.custody import select
    from orch.custody.convenience import FakeStore
    from orch.ops import human

    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    state, wid = Path(env["ORCH_STATE_DIR"]), _wid(root)
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    select.convert_to_convenience(state, wid, store=store)
    assert not (human.keys_dir(state, wid) / human.KEY_FILE).exists()  # the passphrase file is gone
    assert list(host_cli.default_peers(state, root, env)) == [str(UID)]


def test_the_peer_default_without_any_key_says_so(world):  # noqa: F811
    from orch.ops import human

    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    state, wid = Path(env["ORCH_STATE_DIR"]), _wid(root)
    (human.keys_dir(state, wid) / human.KEY_FILE).unlink()  # no passphrase key, no convenience selection
    with pytest.raises(host_cli.HostError) as e:
        host_cli.default_peers(state, root, env)
    assert "no device key" in e.value.message and "--peer" in e.value.message


# --- 2. orch host disable ------------------------------------------------------------------------------------------


def test_disable_reverses_enable_and_keeps_the_state(world):  # noqa: F811
    env, fk = world["env"], world["fake"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    unit = fk.plan
    assert unit is not None and unit.unit_path.exists() and fk.loaded
    state = Path(env["ORCH_STATE_DIR"])
    releases = Path(env["HOME"]) / ".local/share/orch/releases"
    assert releases.is_dir()
    fk.ran.clear()
    code, out = host(["disable"], env)
    doc = json.loads(out)
    assert code == 0 and doc["ok"], out
    data = doc["result"]["data"]
    assert data["removed"] is True and data["purged_releases"] is None
    assert not unit.unit_path.exists() and not fk.loaded
    assert any(a[1] == "bootout" or a[2:3] == ["disable"] for a in fk.ran), fk.ran
    assert state.is_dir() and (state / "host").is_dir()  # the state dir stays
    assert (releases / "current").is_symlink()  # releases stay without --purge-releases
    # again: nothing installed, nothing to do, still ok
    code, out = host(["disable"], env)
    assert code == 0 and json.loads(out)["result"]["data"]["removed"] is False


def test_disable_purges_the_read_only_releases_only_inside_the_release_dir(world):  # noqa: F811
    env = world["env"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    releases = Path(env["HOME"]) / ".local/share/orch/releases"
    outside = Path(env["HOME"]) / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    outside.chmod(0o500)
    (releases / "evil").symlink_to(outside)
    versions = [p for p in releases.iterdir() if p.is_dir() and not p.is_symlink()]
    assert versions and not os.access(versions[0] / "bin", os.W_OK)  # read-only copies
    code, out = host(["disable", "--purge-releases"], env)
    assert code == 0, out
    purged = json.loads(out)["result"]["data"]["purged_releases"]
    assert "current" in purged and "evil" in purged
    assert list(releases.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "keep" and stat.S_IMODE(outside.stat().st_mode) == 0o500
    outside.chmod(0o700)


def test_disable_refuses_another_setup_unless_forced(world):  # noqa: F811
    env, fk = world["env"], world["fake"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    unit = fk.plan
    assert unit is not None
    before = unit.unit_path.read_bytes()
    other = {**env, "ORCH_STATE_DIR": env["ORCH_STATE_DIR"] + "-b"}
    fk.ran.clear()
    code, out = host(["disable"], other)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (4, "host.busy") and "state" in doc["differs"], doc
    assert "--force" in doc["error"]["message"]
    assert unit.unit_path.read_bytes() == before and not any(a[1] == "bootout" for a in fk.ran if len(a) > 1)
    assert not Path(other["ORCH_STATE_DIR"]).exists()  # nothing created for the other state
    code, out = host(["disable", "--force"], other)
    assert code == 0, out
    assert not unit.unit_path.exists()


def test_purge_never_follows_a_link_for_the_release_dir_itself(tmp_path):
    from orch.host.release import ReleaseError, purge_releases

    target = tmp_path / "real"
    target.mkdir()
    (target / "x").write_text("x")
    link = tmp_path / "releases"
    link.symlink_to(target)
    with pytest.raises(ReleaseError):
        purge_releases(link)
    assert (target / "x").exists()
    assert purge_releases(tmp_path / "missing") == []


@pytest.mark.parametrize(
    ("platform", "first"),
    [
        ("darwin", ["launchctl", "bootout", "gui/501/io.orch.host"]),
        ("linux", ["systemctl", "--user", "disable", "--now", "orch-host.service"]),
    ],
)
def test_the_manager_stop_commands(tmp_path, platform, first):
    env = {"HOME": str(tmp_path / platform)}
    unit = svc.plan(env, tmp_path / "s", tmp_path / "s/run", tmp_path / "r", platform=platform, uid=501)
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_bytes(unit.content)
    ran: list[list[str]] = []

    def run(argv):
        ran.append(list(argv))
        return subprocess.CompletedProcess([], 0, "", "")

    assert svc.Manager(unit, run=run).stop()[0] == first
    assert not unit.unit_path.exists()


# --- 3. orch dashboard ---------------------------------------------------------------------------------------------


def _main(args, env, monkeypatch):
    from orch.cli import dashboard
    from orch.cli.main import main

    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(dashboard, "_exec", lambda argv, e: calls.append((argv, dict(e))))
    err = io.StringIO()
    code = main(["dashboard", *args], env=env, stdout=io.StringIO(), stderr=err)
    return code, err.getvalue(), calls


def test_orch_dashboard_execs_the_addon_entry_point_with_the_args(tmp_path, monkeypatch):
    from orch.cli import dashboard

    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "orch-dashboard"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(dashboard.sys, "executable", str(tmp_path / "nowhere" / "python"))
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}
    code, err, calls = _main(["open", "--print-url"], env, monkeypatch)
    assert code == 0 and err == ""
    assert calls == [([str(script), "open", "--print-url"], env)]


def test_orch_dashboard_without_the_addon_names_how_to_install_it(tmp_path, monkeypatch):
    from orch.cli import dashboard

    monkeypatch.setattr(dashboard.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(dashboard.importlib.util, "find_spec", lambda name: None)
    code, err, calls = _main(["open"], {"PATH": str(tmp_path / "empty")}, monkeypatch)
    assert code == 3 and calls == []
    assert "not installed" in err and "orch-dashboard" in err and "install" in err


def test_orch_dashboard_falls_back_to_the_module(tmp_path, monkeypatch):
    from orch.cli import dashboard

    py = str(tmp_path / "nowhere" / "python")
    monkeypatch.setattr(dashboard.sys, "executable", py)
    monkeypatch.setattr(
        dashboard.importlib.util, "find_spec", lambda name: object() if name == "orch_dashboard" else None
    )
    code, err, calls = _main(["serve"], {"PATH": str(tmp_path / "empty")}, monkeypatch)
    assert code == 0 and calls[0][0] == [py, "-m", "orch_dashboard", "serve"]
