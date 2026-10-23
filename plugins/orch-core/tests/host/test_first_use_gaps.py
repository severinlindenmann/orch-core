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
import sys
from pathlib import Path

import pytest

from orch.host import cli as host_cli
from orch.host import enable as svc
from tests.host.lifecycle_helpers import UID
from tests.host.test_host_mode_on import cli, fake, host, world  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def _no_exposure_mark(monkeypatch):
    """#1097 E3 needs a registered workspace and a running host (tests: test_key_exposure_custody.py)."""
    from orch.custody import exposure

    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: None)


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
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: False)
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}
    code, err, calls = _main(["open", "--print-url"], env, monkeypatch)
    assert code == 0 and calls == [([str(script), "open", "--print-url"], env)]
    assert str(script) in err and "PATH" in err  # the resolved path is printed when it came from PATH


def test_orch_dashboard_without_the_addon_names_how_to_install_it(tmp_path, monkeypatch):
    from orch.cli import dashboard

    monkeypatch.setattr(dashboard.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: False)
    code, err, calls = _main(["open"], {"PATH": str(tmp_path / "empty")}, monkeypatch)
    assert code == 3 and calls == []
    assert "not installed" in err and "orch-dashboard" in err and "install" in err


def test_orch_dashboard_falls_back_to_the_module(tmp_path, monkeypatch):
    from orch.cli import dashboard

    py = str(tmp_path / "nowhere" / "python")
    monkeypatch.setattr(dashboard.sys, "executable", py)
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: True)
    code, err, calls = _main(["serve"], {"PATH": str(tmp_path / "empty")}, monkeypatch)
    assert code == 0 and calls[0][0] == [py, "-P", "-m", "orch_dashboard", "serve"]  # -P: no cwd on sys.path


def test_the_module_probe_runs_with_dash_p_so_a_cwd_package_does_not_count(tmp_path, monkeypatch):
    from orch.cli import dashboard

    name = "orch_dashboard_cwd_only_probe"  # a name no interpreter has installed, unlike orch_dashboard itself
    monkeypatch.setattr(dashboard, "MODULE", name)
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("")
    monkeypatch.chdir(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    assert dashboard._module_installed(env) is False
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)
    assert dashboard._module_installed({"A": "1"}) is True
    assert seen["argv"][:3] == [sys.executable, "-P", "-c"] and seen["env"] == {"A": "1"}


def test_a_systemd_refreshing_unit_counts_as_active(tmp_path):
    env = {"HOME": str(tmp_path)}
    unit = svc.plan(env, tmp_path / "s", tmp_path / "s/run", tmp_path / "r", platform="linux", uid=501)
    mgr = svc.Manager(unit, run=lambda argv: subprocess.CompletedProcess([], 0, "refreshing\n", ""))
    assert mgr.active() is True


@pytest.mark.parametrize("entry", ["", ".", "rel/bin"])
def test_orch_dashboard_ignores_empty_and_relative_path_entries(tmp_path, monkeypatch, entry):
    from orch.cli import dashboard

    work = tmp_path / "work"
    (work / "rel" / "bin").mkdir(parents=True)
    for d in (work, work / "rel" / "bin"):
        planted = d / "orch-dashboard"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o755)
    monkeypatch.chdir(work)
    monkeypatch.setattr(dashboard.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: False)
    path = os.pathsep.join(["/usr/nonexistent", entry, "/also/nonexistent"])
    code, err, calls = _main(["open"], {"PATH": path}, monkeypatch)
    assert code == 3 and calls == [] and "not installed" in err


def test_orch_dashboard_prefers_the_module_over_path(tmp_path, monkeypatch):
    from orch.cli import dashboard

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "orch-dashboard").write_text("#!/bin/sh\n")
    (bindir / "orch-dashboard").chmod(0o755)
    py = str(tmp_path / "nowhere" / "python")
    monkeypatch.setattr(dashboard.sys, "executable", py)
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: True)
    code, err, calls = _main(["x"], {"PATH": str(bindir)}, monkeypatch)
    assert code == 0 and calls[0][0][:3] == [py, "-P", "-m"]


# --- 4. review round: running vs enabled, peer key presence, fd-based purge, runner guard --------------------------


def test_disable_stops_a_disabled_but_active_systemd_unit(world, monkeypatch):  # noqa: F811
    env, fk = world["env"], world["fake"]
    real_plan = svc.plan
    monkeypatch.setattr(svc, "plan", lambda *a, **k: real_plan(*a, **{**k, "platform": "linux"}))
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    fk.enabled, fk.active = False, True  # `systemctl disable` without --now: still running
    fk.ran.clear()
    code, out = host(["disable"], env)
    assert code == 0, out
    assert ["systemctl", "--user", "disable", "--now", "orch-host.service"] in fk.ran
    assert fk.active is None and not fk.host and not fk.plan.unit_path.exists()


def test_disable_stops_an_active_unit_whose_file_is_gone(world, monkeypatch):  # noqa: F811
    env, fk = world["env"], world["fake"]
    real_plan = svc.plan
    monkeypatch.setattr(svc, "plan", lambda *a, **k: real_plan(*a, **{**k, "platform": "linux"}))
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    fk.plan.unit_path.unlink()  # the file is gone, the unit still runs and is not enabled
    fk.enabled, fk.active = False, True
    code, out = host(["disable"], env)
    assert code == 4 and json.loads(out)["error"]["code"] == "host.busy"  # loaded from elsewhere: refused ...
    fk.ran.clear()
    code, out = host(["disable", "--force"], env)  # ... and stopped when forced
    assert code == 0, out
    assert any(a[:3] == ["systemctl", "--user", "stop"] or "--now" in a for a in fk.ran)
    assert fk.active is None and not fk.host


def test_a_stubborn_active_unit_is_an_error_and_keeps_the_unit_file(tmp_path):
    env = {"HOME": str(tmp_path)}
    unit = svc.plan(env, tmp_path / "s", tmp_path / "s/run", tmp_path / "r", platform="linux", uid=501)
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_bytes(unit.content)

    def run(argv):  # disabled, active, and nothing stops it
        if "is-active" in argv:
            return subprocess.CompletedProcess([], 0, "active\n", "")
        return subprocess.CompletedProcess([], 1 if "is-enabled" in argv else 5, "", "denied")

    with pytest.raises(OSError, match="failed"):
        svc.Manager(unit, run=run).stop()
    assert unit.unit_path.exists()


def test_the_peer_default_needs_the_key_itself_not_only_the_selection(world, monkeypatch):  # noqa: F811
    from orch.custody import convenience as conv
    from orch.custody import select
    from orch.custody.convenience import FakeStore

    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    state, wid = Path(env["ORCH_STATE_DIR"]), _wid(root)
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    select.convert_to_convenience(state, wid, store=store)
    assert list(host_cli.default_peers(state, root, env)) == [str(UID)]
    backend, _ = select.device_backend(state, wid)
    backend.delete("human")  # the key goes, custody.json (the selection) stays
    assert not backend.exists("human")
    with pytest.raises(host_cli.HostError) as e:
        host_cli.default_peers(state, root, env)
    assert "no device key" in e.value.message


def _tree(base: Path, link_to: Path | None = None) -> Path:
    releases = base / "releases"
    (releases / "v1" / "bin").mkdir(parents=True)
    if link_to is not None:
        (releases / "v1" / "link").symlink_to(link_to)
    (releases / "v1" / "bin" / "orch-host").write_text("x")
    (releases / "v1" / "bin" / "orch-host").chmod(0o555)
    (releases / "v1" / "bin").chmod(0o555)
    (releases / "v1").chmod(0o555)
    (releases / "current").symlink_to("v1")
    return releases


def test_purge_removes_a_read_only_tree_and_the_current_link(tmp_path):
    from orch.host.release import purge_releases

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    outside.chmod(0o500)
    releases = _tree(tmp_path, outside)
    assert purge_releases(releases) == ["current", "v1"]
    assert list(releases.iterdir()) == [] and (outside / "keep").read_text() == "k"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    outside.chmod(0o700)


def test_purge_refuses_a_symlink_swapped_in_for_a_subdirectory(tmp_path, monkeypatch):
    from orch.host import release
    from orch.host.release import ReleaseError, purge_releases

    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "keep").write_text("k")
    outside.chmod(0o500)
    releases = _tree(tmp_path)
    real_open = os.open

    def swap(path, flags, *a, **kw):  # the subdirectory is replaced by a link between the scan and the open
        if path == "v1" and kw.get("dir_fd") is not None:
            os.chmod(releases, 0o700)
            os.chmod(releases / "v1", 0o700)
            os.rename(releases / "v1", tmp_path / "moved")
            os.symlink(outside, releases / "v1")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(release.os, "open", swap)
    with pytest.raises(ReleaseError, match="changed while"):
        purge_releases(releases)
    assert (outside / "keep").read_text() == "k" and stat.S_IMODE(outside.stat().st_mode) == 0o500
    assert (outside / "inner").is_dir()
    outside.chmod(0o700)
    (tmp_path / "moved").chmod(0o700)
    (tmp_path / "moved" / "bin").chmod(0o700)


def test_purge_refuses_a_directory_of_another_user(tmp_path, monkeypatch):
    from orch.host import release
    from orch.host.release import ReleaseError, purge_releases

    releases = _tree(tmp_path)
    real_fstat = os.fstat
    seen = []

    def fstat(fd):
        st = real_fstat(fd)
        seen.append(fd)
        if len(seen) == 2:  # the first subdirectory (v1) belongs to someone else
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.geteuid() + 1, *st[5:]))
        return st

    monkeypatch.setattr(release.os, "fstat", fstat)
    with pytest.raises(ReleaseError, match="another user"):
        purge_releases(releases)
    assert (releases / "v1" / "bin" / "orch-host").exists()


def test_purge_refuses_a_releases_dir_of_another_user(tmp_path, monkeypatch):
    from orch.host import release
    from orch.host.release import ReleaseError, purge_releases

    releases = _tree(tmp_path)
    monkeypatch.setattr(release.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ReleaseError, match="another user"):
        purge_releases(releases)
    assert (releases / "current").is_symlink()


def test_purge_releases_is_refused_while_the_runner_service_is_installed(world, monkeypatch):  # noqa: F811
    from orch.runner import service as runner_service

    env, fk = world["env"], world["fake"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["enable", "--dev"], env)[0] == 0
    releases = Path(env["HOME"]) / ".local/share/orch/releases"
    rplan = runner_service.plan(
        env, Path(env["ORCH_STATE_DIR"]), Path(env["ORCH_STATE_DIR"]) / "run", releases / "current"
    )
    rplan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    rplan.unit_path.write_bytes(rplan.content)
    code, out = host(["disable", "--purge-releases"], env)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (4, "host.busy") and "orch runner disable" in doc["error"]["message"]
    assert (releases / "current").is_symlink() and fk.loaded  # nothing stopped, nothing removed
    rplan.unit_path.unlink()
    # a loaded runner without its unit file here counts too
    monkeypatch.setattr(
        host_cli,
        "RUNNER_SERVICE_MANAGER",
        lambda u: svc.Manager(u, run=lambda a: subprocess.CompletedProcess(a, 0, "state = running\n", "")),
    )
    assert host(["disable", "--purge-releases"], env)[0] == 4
    code, out = host(["disable", "--purge-releases", "--force"], env)
    assert code == 0 and list(releases.iterdir()) == [], out


def test_the_launch_environment_drops_empty_and_relative_pythonpath_entries():
    from orch.cli import dashboard

    env = dashboard._launch_env({"PYTHONPATH": ":rel:/abs/lib", "A": "1"})
    assert env == {"PYTHONPATH": "/abs/lib", "A": "1"}
    assert "PYTHONPATH" not in dashboard._launch_env({"PYTHONPATH": ":."})


# --- #542 item 1: refused under an agent's environment ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["ORCH_GRANT", "ORCH_SESSION"])
@pytest.mark.parametrize("args", [[], ["open"], ["launch"], ["serve"], ["open", "--print-url"], ["stop", "x"]])
def test_orch_dashboard_refuses_under_an_agents_environment(tmp_path, monkeypatch, name, args):
    from orch.cli import dashboard

    def found(*_a, **_k):
        raise AssertionError("nothing may be looked up or started under an agent's environment")

    monkeypatch.setattr(dashboard, "find_entry_point", found)
    code, err, calls = _main(args, {"PATH": str(tmp_path), name: "x"}, monkeypatch)
    assert code == 3 and calls == []
    assert err.startswith("orch dashboard: human_only: ") and err.count("\n") == 1


@pytest.mark.parametrize("args", [["stop"], ["--stop"]])
def test_orch_dashboard_stop_passes_under_an_agents_environment(tmp_path, monkeypatch, args):
    from orch.cli import dashboard

    monkeypatch.setattr(dashboard, "find_entry_point", lambda env, err=None: ["/demo/orch-dashboard"])
    env = {"PATH": str(tmp_path), "ORCH_GRANT": "x", "ORCH_SESSION": "s"}
    code, _, calls = _main(args, env, monkeypatch)
    assert code == 0 and calls == [(["/demo/orch-dashboard", *args], env)]


def test_orch_dashboard_empty_agent_variables_do_not_refuse(tmp_path, monkeypatch):
    from orch.cli import dashboard

    monkeypatch.setattr(dashboard, "find_entry_point", lambda env, err=None: ["/demo/orch-dashboard"])
    code, _, calls = _main(["open"], {"PATH": str(tmp_path), "ORCH_GRANT": "", "ORCH_SESSION": ""}, monkeypatch)
    assert code == 0 and len(calls) == 1
