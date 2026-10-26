"""One rule for every service that runs orch code: the release it loaded must be the installed one. Fake service
managers only: nothing here reaches launchctl or systemctl."""

import pytest

from orch.host import enable as svc
from orch.runner import service as rs

REAL_WRITE = rs.write_loaded_release  # before the autouse fixture pins the pid


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


def test_a_failed_restart_is_not_made_to_look_successful_by_another_process_file(tmp_path):
    """F-706-1: after the restart the NEW main pid is asked for; a dev runner's file (another pid) with the new release
    is not evidence."""
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")  # pid 4242, the service's process
    m = FakeManager()

    def kick():
        m.pid = 5555  # the service came back as a new process that has not recorded itself yet
        REAL_WRITE(tmp_path, "new", pid=999)  # a dev runner wrote the new release

    m.on_kick = kick
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert m.calls == ["restart_now"] and got["restarted"] is False and got["restart_needed"] is True


def test_the_new_process_recording_itself_after_the_restart_counts(tmp_path):
    (tmp_path / "runner").mkdir(mode=0o700)
    rs.write_loaded_release(tmp_path, "old")
    m = FakeManager()

    def kick():
        m.pid = 5555
        REAL_WRITE(tmp_path, "new", pid=5555)

    m.on_kick = kick
    assert rs.restart_if_stale(None, m, tmp_path, "new")["restarted"] is True


def test_an_unknown_main_pid_means_no_restart_and_a_clear_message(tmp_path):
    """F-706-2: with no pid the release file cannot be checked: neither accepted as current nor a reason to restart."""
    (tmp_path / "runner").mkdir(mode=0o700)
    for release in ("old", "new"):
        rs.write_loaded_release(tmp_path, release)
        m = FakeManager(pid=None)
        got = rs.restart_if_stale(None, m, tmp_path, "new")
        assert m.calls == [] and got["restarted"] is False, release
        assert "could not tell" in got["message"] and "launchctl kickstart -k" in got["message"]


def test_the_launchd_pid_is_the_top_level_line_only(tmp_path):
    import subprocess

    from orch.host import runners

    out = "io.orch.runner = {\n\tstate = running\n\tsub = {\n\t\tpid = 111\n\t}\n    pid = 222\n}\n"
    unit = svc.plan({"HOME": str(tmp_path / "h")}, tmp_path / "s", tmp_path / "r", tmp_path / "c",
                    platform="darwin", uid=501)  # fmt: skip
    run = lambda argv: subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
    assert svc.Manager(unit, run=run).main_pid() == 222
    assert runners.main_pid(501, run, platform="darwin") == 222
    nested = "io.orch.runner = {\n\tsub = {\n\t\tpid = 111\n\t}\n}\n"
    run2 = lambda argv: subprocess.CompletedProcess(argv, 0, nested, "")  # noqa: E731
    assert svc.Manager(unit, run=run2).main_pid() is None and runners.main_pid(501, run2, platform="darwin") is None


def test_a_runner_without_a_run_table_is_unknown_not_zero_live_runs(tmp_path, monkeypatch):
    """F-706-3: no runs.json (a runner from before the table existed) is not "0 live runs"."""
    monkeypatch.undo()  # the autouse fixture's live_runs = 0 and fast wait
    (tmp_path / "runner").mkdir(mode=0o700)
    assert rs.live_runs(tmp_path) is None
    REAL_WRITE(tmp_path, "old", pid=4242)
    m = FakeManager()
    got = rs.restart_if_stale(None, m, tmp_path, "new")
    assert m.calls == [] and got["restart_needed"] is True and "command" not in got
    assert "launchctl kickstart -k gui/501/io.orch.runner" in got["message"]
    (tmp_path / "runner" / "runs.json").write_text('{"v": 1, "runs": {}}')
    (tmp_path / "runner" / "runs.json").chmod(0o600)
    assert rs.live_runs(tmp_path) == 0
