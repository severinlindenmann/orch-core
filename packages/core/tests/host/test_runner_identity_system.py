"""H1 L4: who is the runner in tier B: the unit's main process, bound to the process (pidfd or start time)."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from orch.host._socket import Credentials
from orch.host._unix import UnsafePath
from orch.host.runners import system_runner_identity, unit_main_process
from tests.host.tier_b_helpers import AGENT_UID, GID, UID, FixedPins, fake_systemctl, share, show

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="Unix sockets")


def test_unit_main_process_asks_the_system_manager_for_the_instance():
    seen: list = []
    assert unit_main_process("orch-runner@sev.service", fake_systemctl(show(), seen=seen))[0] == 4242
    argv = seen[0]
    assert argv[0].endswith("/systemctl") and "--user" not in argv
    assert argv[-2:] == ["--", "orch-runner@sev.service"] and "MainPID" in argv


def test_the_system_manager_query_has_a_clean_environment_and_a_short_timeout(monkeypatch):
    from orch.host import runners

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=show(), stderr="")

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ORCH_NO_SERVICE_MANAGER", raising=False)
    monkeypatch.setattr("subprocess.run", fake_run)
    assert runners.unit_main_process("orch-runner@sev.service") == (4242, 5000000)
    assert seen["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"} and seen["timeout"] <= 3


def test_a_hung_system_manager_is_a_denial(monkeypatch):
    def hang(argv):
        raise subprocess.TimeoutExpired(argv, 3)

    assert unit_main_process("orch-runner@sev.service", hang) is None


@pytest.mark.parametrize(
    "out,code",
    [
        (show("0"), 0),
        (show("1"), 0),
        (show("", ""), 0),
        (show("12 13"), 0),
        (show("abc"), 0),
        (show(start="0"), 0),
        ("MainPID=4242\n", 0),
        ("", 0),
        (show(), 1),
    ],
)
def test_unit_main_process_denies_anything_unclear(out, code):
    assert unit_main_process("orch-runner@sev.service", fake_systemctl(out, code)) is None


@pytest.mark.parametrize(
    "unit", ["orch-runner@.service", "orch-runner@a b.service", "ssh.service", "orch-runner@sev", "x@sev.service"]
)
def test_unit_main_process_names_only_the_runner_template(unit):
    seen: list = []
    assert unit_main_process(unit, fake_systemctl(show(), seen=seen)) is None
    assert seen == []


def test_unit_main_process_is_denied_when_the_manager_is_unreachable():
    def boom(argv):
        raise OSError("no systemctl")

    assert unit_main_process("orch-runner@sev.service", boom) is None


def test_system_identity_needs_uid_pin_main_pid_and_the_start_time(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "monotonic_to_boot_offset", lambda: 100.0)
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 105.2)  # 5 s + 100 s offset, within a tick
    good = Credentials(AGENT_UID, GID, 777)
    assert system_runner_identity(mine, FixedPins(mine), good, run) is True
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 778), run) is False  # a child
    assert system_runner_identity(mine, FixedPins(mine), Credentials(UID, GID, 777), run) is False  # the person
    assert system_runner_identity(mine, FixedPins(None), good, run) is False  # the pin is gone
    assert system_runner_identity(mine, FixedPins(share(gid=GID + 1)), good, run) is False  # the pin names another
    assert system_runner_identity(mine, FixedPins(UnsafePath("broken")), good, run) is False
    assert system_runner_identity(mine, FixedPins(mine), good, fake_systemctl("", 3)) is False


def test_a_reused_pid_with_another_start_time_is_denied(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "monotonic_to_boot_offset", lambda: 100.0)
    good = Credentials(AGENT_UID, GID, 777)
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 60.0)  # the old process that took the connection
    assert system_runner_identity(mine, FixedPins(mine), good, run) is False
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: None)  # the start time cannot be read
    assert system_runner_identity(mine, FixedPins(mine), good, run) is False


def test_the_kernels_pidfd_binds_the_pid_where_there_is_one(monkeypatch):
    from orch.host import runners

    mine = share()
    run = fake_systemctl(show("777", "5000000"))
    monkeypatch.setattr(runners, "proc_start_seconds", lambda pid: 1.0)  # never consulted with a pidfd
    monkeypatch.setattr(runners, "pidfd_pid_now", lambda fd: 777)
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777, 9), run) is True
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 778, 9), run) is False
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, -1), run) is False
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777), run) is False  # no fd


def test_the_pidfd_is_read_again_after_the_manager_was_asked(monkeypatch):
    from orch.host import runners

    mine = share()
    order: list[str] = []

    def run(argv):
        order.append("systemctl")
        return SimpleNamespace(returncode=0, stdout=show("777"), stderr="")

    def now(fd):
        order.append("fdinfo")
        return -1  # the peer exited while the manager was asked

    monkeypatch.setattr(runners, "pidfd_pid_now", now)
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777, 9), run) is False
    assert order == ["systemctl", "fdinfo"]
    monkeypatch.setattr(runners, "pidfd_pid_now", lambda fd: 778)  # the pidfd names another process now
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777, 9), run) is False


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERPIDFD is Linux")
@pytest.mark.parametrize(
    "code,expected", [(errno.ENOPROTOOPT, None), (errno.EINVAL, -1), (errno.ESRCH, -1), (errno.EPERM, -1)]
)
def test_only_a_kernel_without_the_option_falls_back_to_start_time(code, expected):
    from orch.host._socket import with_pidfd

    class Sock:
        def getsockopt(self, *args):
            raise OSError(code, "x")

    got = with_pidfd(Sock(), Credentials(AGENT_UID, GID, 777))  # type: ignore[arg-type]
    assert got.pidfd_pid == expected and got.pidfd is None


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERPIDFD is Linux")
def test_an_unreadable_pidfd_is_a_denial_and_leaks_no_fd(monkeypatch):
    import struct

    from orch.host import _socket

    fd = os.open("/dev/null", os.O_RDONLY)

    class Sock:
        def getsockopt(self, *args):
            return struct.pack("i", fd)

    monkeypatch.setattr(_socket, "pidfd_pid_now", lambda f: -1)
    assert _socket.with_pidfd(Sock(), Credentials(AGENT_UID, GID, 777)).pidfd_pid == -1  # type: ignore[arg-type]
    with pytest.raises(OSError):
        os.fstat(fd)  # closed


def test_proc_start_seconds_reads_this_process_on_linux():
    from orch.host.runners import proc_start_seconds

    if sys.platform != "linux":
        assert proc_start_seconds(os.getpid()) is None  # no /proc: the identity then denies
        return
    assert 0 < proc_start_seconds(os.getpid()) < proc_start_seconds(os.getpid()) + 1


def test_a_denial_says_which_condition_denied(monkeypatch, caplog):
    import logging

    from orch.host import runners

    mine = share()
    monkeypatch.setattr(runners, "pidfd_pid_now", lambda fd: 777)
    caplog.set_level(logging.DEBUG, logger="orch.host.runners")
    run = fake_systemctl(show("777"))
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 777, 777, 9), run) is True
    assert system_runner_identity(mine, FixedPins(mine), Credentials(AGENT_UID, GID, 778, 778, 9), run) is False
    assert "peer pid 778 is not the unit's MainPID 777" in caplog.text
    assert system_runner_identity(mine, FixedPins(mine), Credentials(UID, GID, 777), run) is False
    assert "is not the agent uid" in caplog.text
    assert system_runner_identity(mine, FixedPins(None), Credentials(AGENT_UID, GID, 777), run) is False
    assert "pin no longer names" in caplog.text
