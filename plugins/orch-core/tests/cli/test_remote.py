"""#1198 R1-7: ``orch remote enable|disable|reset`` (the person's side). Nothing here touches the real machine: the
resolver, the interface and port probes, sudo and the dashboard command are replaced, and the root side is covered in
tests/setup/test_osfront.py."""

from __future__ import annotations

import io
import stat
import subprocess
from pathlib import Path

import pytest

from orch.cli import remote
from orch.click.passkey_store import PasskeyStore
from orch.setup import osfront
from orch.setup.osapply import DryRunExecutor, RunResult
from tests.host.passkey_helpers import Authenticator
from tests.osfacts_fake import FakeOsFacts

NAME, PERSON, UID, FRONT_UID = "orch.example.com", "sev", 1000, 990
ENROLL_LINE = f"https://{NAME}/#enroll=abc123-SECRETSECRETSECRETSECRET"


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.env = {"ORCH_STATE_DIR": str(self.state), "HOME": str(tmp_path)}
        self.calls: list[list[str]] = []
        self.asked: list[str] = []
        self.answer = "y"
        self.rc: dict[str, int] = {}
        self.resolves: set[str] | OSError = {"203.0.113.5"}
        self.local = {"203.0.113.5"}
        self.busy: set[int] = set()
        self.order: list[str] = []
        launcher = tmp_path / "orch-os-setup"
        launcher.write_text("#!/usr/bin/python3 -IS\n")
        monkeypatch.setattr(remote, "LAUNCHER_PATH", str(launcher))
        monkeypatch.setattr(remote, "_is_linux", lambda: True)
        monkeypatch.setattr(remote, "_is_root", lambda: False)
        monkeypatch.setattr(remote, "_uid", lambda: UID)
        monkeypatch.setattr(remote, "_person", lambda: PERSON)
        monkeypatch.setattr(remote, "_front_uid", lambda: FRONT_UID)
        monkeypatch.setattr(remote, "_sleep", lambda s: None)
        monkeypatch.setattr(remote, "_resolve", self._resolve)
        monkeypatch.setattr(remote, "_is_local", lambda a: a in self.local)
        monkeypatch.setattr(remote, "_port_busy", lambda p: p in self.busy)
        monkeypatch.setattr(remote, "_run", self._run)
        monkeypatch.setattr(remote, "TERMINAL", self)
        monkeypatch.setattr(remote, "_no_answer", lambda p: True)
        monkeypatch.setattr("orch.cli.dashboard.find_entry_point", lambda env, err=None: ["/opt/orch-dashboard"])
        monkeypatch.setattr(remote, "_front_installed", lambda: False)
        facts = FakeOsFacts()
        facts.add_user(PERSON, UID, home="/home/sev")
        for d in ("/", "/usr", "/usr/bin", "/etc", "/etc/orch"):
            facts.add_path(d, kind="dir")
        facts.add_path(osfront.CADDY_BIN, kind="file", mode=0o755)
        monkeypatch.setattr("orch.setup.osfacts.RealOsFacts", lambda: facts)
        monkeypatch.setattr("orch.setup.osapply.DryRunExecutor", _Dry)
        self.facts = facts

    def _resolve(self, name):
        if isinstance(self.resolves, OSError):
            raise self.resolves
        return self.resolves

    def _run(self, argv, env):
        self.calls.append(list(argv))
        key = ("sudo-enroll" if "--front-enroll" in argv else "sudo") if argv[0] == remote.SUDO else argv[-1]
        self.order.append(key)
        if key == "stop":
            Path(self.state / "dashboard" / "ctl.sock").unlink(missing_ok=True)
        return self.rc.get(key, 0)

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answer

    def go(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = remote.main(list(argv), env=env or self.env, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def files(self):
        return (
            sorted(p.name for p in (self.state / "dashboard").glob("*")) if (self.state / "dashboard").exists() else []
        )


class _Dry(DryRunExecutor):
    """The real dry-run executor, but its probes read nothing of this machine: no files, nothing active."""

    def query(self, argv):
        if argv[0] == osfront.CADDY_BIN:
            return RunResult(0, "v2.11.7 h1:abc")
        return RunResult(1)  # nothing is active: no system caddy, no front yet

    def read_file(self, path):
        return None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# --- who may run it -----------------------------------------------------------------------------------------------
def test_an_agent_root_or_another_platform_is_refused_before_anything_else(rig, monkeypatch):
    for var in ("ORCH_GRANT", "ORCH_SESSION"):
        code, _, err = rig.go("enable", "--name", NAME, env={**rig.env, var: "x"})
        assert code == 3 and "human_only" in err
    monkeypatch.setattr(remote, "_is_root", lambda: True)
    assert rig.go("enable", "--name", NAME)[0] == 3
    monkeypatch.setattr(remote, "_is_root", lambda: False)
    monkeypatch.setattr(remote, "_is_linux", lambda: False)
    code, _, err = rig.go("enable", "--name", NAME)
    assert code == 3 and "unsupported.platform" in err
    assert rig.go("disable")[0] == 3 and rig.go("reset")[0] == 3
    assert rig.calls == [] and rig.files() == []


# --- name, port, proxy ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["localhost", "1.2.3.4", "orch.example.com:443", "Orch.Example.com", "a.local", "x"])
def test_a_bad_name_is_refused_with_nothing_changed(rig, name):
    code, _, err = rig.go("enable", "--name", name)
    assert code == 5 and "invalid.input" in err and rig.calls == [] and rig.files() == []


@pytest.mark.parametrize("port", ["0", "80", "1023", "65536", "-1"])
def test_a_bad_dashboard_port_is_refused(rig, port):
    code, _, err = rig.go("enable", "--name", NAME, "--port", port)
    assert code == 5 and "between 1024 and 65535" in err and rig.calls == []


def test_a_proxy_network_that_holds_everyone_is_refused(rig):
    for bad in ("0.0.0.0/0", "::/0", "nope", "10.0.0.0/40"):
        code, _, err = rig.go("enable", "--name", NAME, "--trusted-proxy", bad)
        assert code == 5 and "not a proxy network" in err
    assert rig.calls == []


# --- the name must point here -------------------------------------------------------------------------------------
def test_a_name_that_points_elsewhere_stops_before_anything_changes(rig):
    rig.resolves = {"198.51.100.9"}
    code, out, err = rig.go("enable", "--name", NAME, "--yes")
    assert code == 3 and "remote.name_mismatch" in err and "198.51.100.9" in err
    assert rig.calls == [] and rig.files() == [] and "Make these changes" not in out


def test_a_name_that_does_not_resolve_stops_too(rig):
    rig.resolves = OSError("nxdomain")
    code, _, err = rig.go("enable", "--name", NAME, "--yes")
    assert code == 3 and "remote.name_mismatch" in err and "does not resolve" in err and rig.calls == []


def test_behind_a_cloud_nat_the_public_ip_is_named_and_must_match_the_name(rig):
    rig.local = set()
    rig.resolves = {"203.0.113.5"}
    assert rig.go("enable", "--name", NAME, "--dry-run")[0] == 3
    assert rig.go("enable", "--name", NAME, "--public-ip", "198.51.100.9", "--dry-run")[0] == 3  # it points elsewhere
    res = rig.go("enable", "--name", NAME, "--public-ip", "203.0.113.5", "--dry-run")
    assert res[0] == 0, res
    assert rig.go("enable", "--name", NAME, "--public-ip", "not-an-ip", "--dry-run")[0] == 5


def test_busy_ports_stop_the_run(rig):
    for port in (80, 443):
        rig.busy = {port}
        code, _, err = rig.go("enable", "--name", NAME, "--yes")
        assert code == 3 and f"port {port}" in err and rig.calls == []


# --- dry run, confirm ------------------------------------------------------------------------------------------------
def test_dry_run_prints_exactly_what_will_change_and_changes_nothing(rig):
    code, out, _ = rig.go("enable", "--name", NAME, "--trusted-proxy", "10.0.0.0/8", "--port", "9000", "--dry-run")
    assert code == 0 and rig.calls == [] and rig.files() == [] and rig.asked == []
    for want in (
        f"https://{NAME}",
        "points to this server",
        "create the system user 'orch-front'",
        f"write {osfront.CADDYFILE}",
        f"write {osfront.UNIT_PATH}",
        "/run/orch/front/1000",
        "remote.json",
        "<state>/dashboard/port (9000",
        "one-time link",
        "Dry run: nothing was changed.",
    ):
        assert want in out, want


def test_a_root_side_problem_is_shown_in_the_dry_run_and_stops_the_real_run(rig):
    rig.facts.paths.pop(osfront.CADDY_BIN)
    code, out, _ = rig.go("enable", "--name", NAME, "--yes")
    assert code == 1 and "Caddy 2.11 or newer is not installed" in out and "Nothing was changed" in out
    assert rig.calls == [] and rig.asked == []


def test_declining_the_confirm_changes_nothing(rig):
    rig.answer = "n"
    code, out, _ = rig.go("enable", "--name", NAME)
    assert code == 1 and rig.asked == ["Make these changes? [y/N] "] and rig.calls == [] and rig.files() == []
    rig.answer = ""
    assert rig.go("enable", "--name", NAME)[0] == 1 and rig.calls == []


def test_no_terminal_means_no_change(rig, monkeypatch):
    class NoTty:
        def ask(self, prompt):
            raise OSError("no controlling terminal")

    monkeypatch.setattr(remote, "TERMINAL", NoTty())
    code, _, err = rig.go("enable", "--name", NAME)
    assert code == 3 and "human_only" in err and rig.calls == []


# --- enable ----------------------------------------------------------------------------------------------------------
def test_enable_runs_the_root_copy_through_sudo_then_writes_remote_json_then_restarts_and_enrolls(rig):
    code, out, err = rig.go(
        "enable",
        "--name",
        NAME,
        "--trusted-proxy",
        "203.0.113.5",
        "--trusted-proxy",
        "10.0.0.0/8",
        "--port",
        "9000",
        "--yes",
    )
    assert code == 0, (out, err)
    sudo = rig.calls[0]
    assert sudo == [
        "/usr/bin/sudo", "/usr/bin/python3", "-I", "-S", remote.LAUNCHER_PATH, "--front-enable", "--person", PERSON,
        "--name", NAME, "--trusted-proxy", "203.0.113.5/32", "--trusted-proxy", "10.0.0.0/8",
    ]  # fmt: skip
    assert rig.order == ["sudo", "stop", "up", "sudo-enroll"]
    assert rig.calls[1:3] == [["/opt/orch-dashboard", "stop"], ["/opt/orch-dashboard", "up"]]
    ctl = str(rig.state / "dashboard" / "ctl.sock")
    assert rig.calls[3][:5] == ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-S", remote.LAUNCHER_PATH]
    assert rig.calls[3][5:] == [
        "--front-enroll",
        "--person",
        PERSON,
        "--ctl",
        ctl,
    ]  # ROOT asks; the person's UID cannot
    directory = rig.state / "dashboard"
    path = directory / "remote.json"
    assert (
        stat.S_IMODE(path.stat().st_mode) == 0o600
        and path.read_text() == f'{{"front_uid": {FRONT_UID}, "name": "{NAME}"}}'
    )
    assert (directory / "port").read_text() == "9000\n" and stat.S_IMODE((directory / "port").stat().st_mode) == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert f"https://{NAME}" in out
    assert "stays down until the dashboard is started" in out
    assert f"ssh -o ExitOnForwardFailure=yes -L 9000:127.0.0.1:9000 {PERSON}@{NAME}" in out  # the way back in
    assert "orch dashboard open --print-url" in out and "orch remote reset" in out


def test_without_a_port_the_fallback_line_uses_the_headless_port(rig):
    out = rig.go("enable", "--name", NAME, "--yes")[1]
    assert f"-L {remote.HEADLESS_PORT}:127.0.0.1:{remote.HEADLESS_PORT} " in out


def test_the_root_step_failing_leaves_the_dashboard_and_remote_json_alone(rig):
    rig.rc["sudo"] = 1
    code, _, err = rig.go("enable", "--name", NAME, "--yes")
    assert code == 1 and "root step failed" in err
    assert rig.order == ["sudo"] and rig.files() == []


def test_a_missing_trusted_copy_is_said_plainly_before_sudo_is_asked(rig, monkeypatch):
    monkeypatch.setattr(remote, "LAUNCHER_PATH", str(rig.state / "nowhere"))
    code, _, err = rig.go("enable", "--name", NAME, "--yes")
    assert code == 3 and "is not installed" in err and rig.calls == []


def test_the_enrollment_link_is_never_in_argv_files_output_or_a_pipe(rig, monkeypatch):
    code, out, err = rig.go("enable", "--name", NAME, "--yes")
    assert code == 0
    everything = " ".join(" ".join(c) for c in rig.calls) + out + err
    assert "enroll=" not in everything and "SECRET" not in everything
    assert all("enroll=" not in p.read_text() for p in (rig.state / "dashboard").glob("*") if p.is_file())
    # the subprocess is started with the terminal's own stdio: nothing is captured, so it cannot reach a log
    seen = {}

    def fake(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", fake)
    assert remote._run(["x", "enroll"], {"A": "b"}) == 0
    assert not {"stdout", "stderr", "stdin", "capture_output", "input"} & set(seen)
    src = Path(remote.__file__).read_text()
    assert "PIPE" not in src and "capture_output" not in src and "check_output" not in src


def test_the_enroll_failing_says_how_to_get_a_link(rig):
    rig.rc["sudo-enroll"] = 1
    code, out, _ = rig.go("enable", "--name", NAME, "--yes")
    assert code == 1 and "orch remote reset" in out and (rig.state / "dashboard" / "remote.json").exists()


def test_a_second_enable_with_the_same_name_is_the_same_run(rig):
    assert rig.go("enable", "--name", NAME, "--yes")[0] == 0
    assert rig.go("enable", "--name", NAME, "--yes")[0] == 0
    assert (rig.state / "dashboard" / "remote.json").read_text() == f'{{"front_uid": {FRONT_UID}, "name": "{NAME}"}}'


# --- disable ---------------------------------------------------------------------------------------------------------
def _enabled(rig):
    assert rig.go("enable", "--name", NAME, "--yes")[0] == 0
    rig.calls.clear()
    rig.order.clear()


def test_disable_dry_run_changes_nothing(rig):
    _enabled(rig)
    code, out, _ = rig.go("disable", "--dry-run")
    assert code == 0 and rig.calls == [] and (rig.state / "dashboard" / "remote.json").exists()
    assert "remove <state>/dashboard/remote.json" in out and "Dry run" in out


def test_disable_takes_access_away_first_then_stops_the_front_and_keeps_the_user(rig):
    _enabled(rig)
    code, out, _ = rig.go("disable", "--yes")
    assert code == 0, out
    assert not (rig.state / "dashboard" / "remote.json").exists()
    assert rig.order == ["stop", "sudo"]
    assert rig.calls[1][-1:] == ["--front-disable"] and "--person" not in rig.calls[1]
    assert "Remote access is off" in out and "orch remote reset" in out


def test_disable_asks_first(rig):
    _enabled(rig)
    rig.answer = "no"
    assert rig.go("disable")[0] == 1 and rig.calls == [] and (rig.state / "dashboard" / "remote.json").exists()
    assert rig.asked[-1] == "Take remote access away? [y/N] "


def test_disable_when_the_root_step_fails_has_still_taken_remote_json_away(rig):
    _enabled(rig)
    rig.rc["sudo"] = 1
    code, _, err = rig.go("disable", "--yes")
    assert code == 1 and "root step failed" in err
    assert not (rig.state / "dashboard" / "remote.json").exists()  # the dashboard no longer opens the front


# --- reset -----------------------------------------------------------------------------------------------------------
def test_reset_needs_remote_access_to_be_on(rig):
    code, _, err = rig.go("reset", "--yes")
    assert code == 3 and "remote.not_configured" in err and rig.calls == []


def test_reset_drops_every_passkey_and_session_and_mints_a_new_link(rig):
    _enabled(rig)
    directory = rig.state / "dashboard"
    store = PasskeyStore(directory / "passkeys.json")
    for _ in range(3):
        store.add(Authenticator().record())
    assert len(store.read()) == 3
    code, out, _ = rig.go("reset", "--yes")
    assert code == 0, out
    assert store.read() == [] and stat.S_IMODE((directory / "passkeys.json").stat().st_mode) == 0o600
    assert rig.order == ["stop", "up", "sudo-enroll"]  # the stop ends sessions and open links; root mints the new one
    assert "Dropped 3 passkeys and all remote sessions." in out


def test_reset_repairs_a_store_that_fails_its_checks(rig):
    _enabled(rig)
    path = rig.state / "dashboard" / "passkeys.json"
    path.write_text("garbage")
    path.chmod(0o644)  # the store would refuse this file
    code, out, _ = rig.go("reset", "--yes")
    assert code == 0 and PasskeyStore(path).read() == [] and "Dropped 0 passkeys" in out


def test_reset_dry_run_and_decline_keep_the_passkeys(rig):
    _enabled(rig)
    store = PasskeyStore(rig.state / "dashboard" / "passkeys.json")
    store.add(Authenticator().record())
    assert rig.go("reset", "--dry-run")[0] == 0
    rig.answer = "n"
    assert rig.go("reset")[0] == 1
    assert len(store.read()) == 1 and rig.calls == []


def test_reset_uses_sudo_only_to_ask_root_for_the_link(rig):
    _enabled(rig)
    rig.go("reset", "--yes")
    sudo = [c for c in rig.calls if c[0] == remote.SUDO]
    assert len(sudo) == 1 and "--front-enroll" in sudo[0] and "--front-enable" not in sudo[0]


def test_the_command_is_registered(rig, monkeypatch):
    from orch.cli.main import main as run

    out, err = io.StringIO(), io.StringIO()
    code = run(["remote", "enable", "--name", NAME, "--dry-run"], env=rig.env, stdout=out, stderr=err)
    assert code == 0 and "orch remote enable" in out.getvalue()
