"""One rule for every service that runs orch code: the release it loaded must be the installed one. Fake service
managers only: nothing here reaches launchctl or systemctl."""

import pytest

from orch.host import enable as svc
from orch.runner import service as rs


class FakeManager:
    def __init__(self, *, active=True, kick_ok=True, pid=4242):
        self.calls = []
        self.pid = pid
        self._active = active
        self.kick_ok = kick_ok
        self.on_kick = lambda: None

    def active(self):
        return self._active

    def start(self, *, changed):
        self.calls.append("start")
        return []

    def restart_now(self):
        self.calls.append("restart_now")
        if not self.kick_ok:
            raise OSError("no")
        self.on_kick()
        return []

    def main_pid(self):
        return self.pid

    def restart_command(self):
        return ["launchctl", "kickstart", "-k", "gui/501/io.orch.runner"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(svc, "START_WAIT", 0.3)
    monkeypatch.setattr(rs, "live_runs", lambda state: 0)  # no live runs unless a test says so
    real = rs.write_loaded_release
    monkeypatch.setattr(
        rs, "write_loaded_release", lambda state, release: real(state, release, pid=4242)
    )  # the service's process


def test_the_runner_records_the_release_it_loaded(tmp_path):
    (tmp_path / "runner").mkdir(mode=0o700)
    assert rs.loaded_release(tmp_path) is None  # an older runner wrote nothing
    rs.write_loaded_release(tmp_path, "abc123abc123")
    assert rs.loaded_release(tmp_path) == "abc123abc123"


def test_a_current_runner_is_left_alone(tmp_path):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "new")
    m = FakeManager()
    assert rs.restart_if_stale(None, m, tmp_path, "new") is None and m.calls == []


def test_a_runner_service_that_is_not_running_is_not_started_by_this(tmp_path):
    m = FakeManager(active=False)
    assert rs.restart_if_stale(None, m, tmp_path, "new") is None and m.calls == []


def test_an_older_runner_is_restarted_and_the_person_is_told_what_stops(tmp_path):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")
    m = FakeManager()
    m.on_kick = lambda: rs.write_loaded_release(tmp_path, "new")  # the new runner records itself at start
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert m.calls == ["restart_now"] and got["restarted"] is True
    assert "agents and terminals" in got["message"]


def test_a_runner_without_a_release_file_counts_as_older(tmp_path):
    m = FakeManager()
    m.on_kick = lambda: (tmp_path / "runner").mkdir(mode=0o700) or rs.write_loaded_release(tmp_path, "new")
    assert rs.restart_if_stale(None, m, tmp_path, "new")["restarted"] is True


def test_a_runner_that_cannot_be_restarted_gets_the_exact_command(tmp_path):
    m = FakeManager(kick_ok=False)
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert got["restarted"] is False and "launchctl kickstart -k gui/501/io.orch.runner" in got["message"]


def test_the_shared_rule_drains_first_when_it_can():
    seen = iter(["old", "old", "new"])
    m = FakeManager()
    assert svc.restart_to_release(m, lambda: next(seen), "new", drain=lambda: True) == "drained"
    assert m.calls == []
    assert svc.restart_to_release(m, lambda: "new", "new", drain=lambda: pytest.fail("not needed")) == "current"


def test_live_runs_stop_the_automatic_restart_and_the_person_gets_the_command(tmp_path, monkeypatch):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")
    monkeypatch.setattr(rs, "live_runs", lambda state: 2)
    m = FakeManager()
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert m.calls == [] and got["restarted"] is False and got["restart_needed"] is True
    assert "2 running agents/terminals" in got["message"] and "was not restarted" in got["message"]
    assert "launchctl kickstart -k gui/501/io.orch.runner" in got["message"] and "--restart-runner" in got["message"]


def test_force_restarts_even_with_live_runs(tmp_path, monkeypatch):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")
    monkeypatch.setattr(rs, "live_runs", lambda state: 2)
    m = FakeManager()
    m.on_kick = lambda: rs.write_loaded_release(tmp_path, "new")
    assert rs.restart_if_stale(None, m, tmp_path, "new", force=True)["restarted"] is True


def test_an_unreadable_run_table_counts_as_live_runs(tmp_path, monkeypatch):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")
    monkeypatch.setattr(rs, "live_runs", lambda state: None)
    m = FakeManager()
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert m.calls == [] and got["restart_needed"] is True


def test_a_release_file_from_another_process_is_not_evidence(tmp_path):
    """A dev runner, or the process before a restart, wrote it: wait briefly, never restart on that alone."""
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")  # pid 4242
    m = FakeManager(pid=999)
    assert rs.restart_if_stale(None, m, tmp_path, "new", wait=0.3) is None and m.calls == []


def test_main_pid_per_platform(tmp_path):
    from orch.host import enable as e

    for kind, platform, out, want in (
        ("launchd", "darwin", "x\n\tpid = 777\n\tstate = running\n", 777),
        ("systemd", "linux", "888\n", 888),
        ("systemd", "linux", "0\n", None),
    ):
        unit = e.plan(
            {"HOME": str(tmp_path / "h")}, tmp_path / "s", tmp_path / "r", tmp_path / "c", platform=platform, uid=501
        )
        import subprocess

        m = e.Manager(unit, run=lambda argv, out=out: subprocess.CompletedProcess(argv, 0, out, ""))
        assert m.main_pid() == want, kind
