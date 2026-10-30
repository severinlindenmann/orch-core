"""#1463 (Opus F4): the launcher builds argv lists only, names units from hex alone, never passes --i-know-dev, hands the
remover an exact environment, and the pidfile runner starts a new session."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orch_hub.closing_launch import ENV_ALLOW, HEX32, Launcher, LaunchError

RID = "ab" * 16


class Rec:
    def __init__(self, rc=0):
        self.calls: list[tuple[list, dict]] = []
        self.rc = rc

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, "", "")

    def popen(self, argv, **kw):
        self.calls.append((argv, kw))


def _install(tmp_path: Path, runner: str) -> Path:
    data = tmp_path / "state" / "install" / "parts" / "hub" / "data"
    data.mkdir(parents=True, mode=0o700)
    (tmp_path / "state" / "install" / "install.json").write_text(
        json.dumps({"v": 1, "parts": {"hub": {"runner": runner}, "relay": {}, "dashboard": {}}})
    )
    return data


def _launcher(tmp_path, runner, rec, env=None):
    data = _install(tmp_path, runner)
    env = {"HOME": "/h", "PATH": "/bin", "SECRET": "x", "ORCH_STATE_DIR": "/s", **(env or {})}
    return Launcher(data, env=env, run=rec.run, popen=rec.popen, orch=Path("/opt/orch/bin/orch")), data


@pytest.mark.parametrize("runner", ["systemd", "launchd", "pidfile"])
def test_argv_is_a_list_with_no_shell_and_no_dev_flag(tmp_path, runner):
    rec = Rec()
    lc, data = _launcher(tmp_path, runner, rec)
    lc.launch(data, RID)
    argv, kw = rec.calls[0]
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert kw.get("shell") is not True and "--i-know-dev" not in " ".join(argv)
    assert argv[-3:] == ["host", "prune", "--hub-order"] and "/opt/orch/bin/orch" in argv


def test_unit_and_label_names_come_from_hex_only(tmp_path):
    rec = Rec()
    lc, data = _launcher(tmp_path, "systemd", rec)
    lc.launch(data, RID)
    assert f"orch-hub-remove-{RID}" in rec.calls[0][0]
    rec2 = Rec()
    lc2, data2 = _launcher(tmp_path / "b", "launchd", rec2)
    lc2.launch(data2, RID)
    assert f"io.orch.hub-remove.{RID}" in rec2.calls[0][0]
    for bad in ("../x", "AB" * 16, "ab" * 15, RID + "; reboot", ""):
        assert HEX32.fullmatch(bad) is None
        with pytest.raises(LaunchError):
            lc.launch(data, bad)


def test_the_pidfile_runner_uses_a_new_session_and_no_inherited_fds(tmp_path):
    rec = Rec()
    lc, data = _launcher(tmp_path, "pidfile", rec)
    lc.launch(data, RID)
    kw = rec.calls[0][1]
    assert kw["start_new_session"] is True and kw["close_fds"] is True and kw["cwd"] == "/"
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL


def test_the_environment_is_exactly_the_allow_list(tmp_path):
    rec = Rec()
    lc, data = _launcher(
        tmp_path, "pidfile", rec, env={"LANG": "C", "XDG_CONFIG_HOME": "/x", "ORCH_INSTALL_SERVICE": "pidfile"}
    )
    lc.launch(data, RID)
    env = rec.calls[0][1]["env"]
    assert set(env) <= set(ENV_ALLOW) and "SECRET" not in env and env["HOME"] == "/h"
    assert set(env) == {k for k in ENV_ALLOW if k in lc.env}


def test_the_dry_run_has_the_same_argv_plus_dry_run_and_needs_a_known_runner(tmp_path):
    rec = Rec()
    lc, data = _launcher(tmp_path, "systemd", rec)
    assert lc.dry_run(data) == (True, "")
    assert rec.calls[0][0][-4:] == ["host", "prune", "--hub-order", "--dry-run"]
    rec_bad = Rec(rc=3)
    lc2, d2 = _launcher(tmp_path / "c", "systemd", rec_bad)
    ok, plain = lc2.dry_run(d2)
    assert ok is False and plain
    lc3, d3 = _launcher(tmp_path / "d", "weird", Rec())
    assert lc3.dry_run(d3)[0] is False
