"""The dev-only foreground runner admission (``orch host run --foreground --dev-runner``).

A host that runs in the foreground with that explicit opt-in admits, besides the runner unit's main PID, a runner
process of the host's own UID that ``orch runner run --foreground`` announced in ``<run>/term/<uid>/dev-runner.json``
(0600, its PID and birth time): the kernel's peer PID and UID must match the file, and the PID must still be the
process born then. Never for a service run directory, never for another UID, never without the opt-in.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from orch.host._socket import Credentials
from orch.host.errors import HostError
from orch.host.service import Host
from orch.runner import devmode, procs
from tests.host.lifecycle_helpers import UID
from tests.host.live_helpers import world
from tests.host.test_runner_socket import _first_frame, _hello

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
ME = os.getpid()


def _creds(pid: int = ME, uid: int = UID) -> Credentials:
    return Credentials(uid=uid, gid=os.getgid(), pid=pid)


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir(mode=0o711)
    return run


# -- the announcement file and its check -----------------------------------------------------------------------------


def test_announce_writes_a_private_file_with_pid_and_birth_and_withdraw_removes_only_its_own(tmp_path):
    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    assert path == run / "term" / str(UID) / "dev-runner.json"
    st = os.lstat(path)
    assert st.st_mode & 0o777 == 0o600 and st.st_uid == UID
    assert os.lstat(path.parent).st_mode & 0o777 == 0o700
    doc = json.loads(path.read_text())
    assert doc == {"v": 1, "pid": ME, "birth": procs.birth(ME), "uid": UID}
    devmode.withdraw(path, ME + 1)  # another runner's file is left alone
    assert path.exists()
    devmode.withdraw(path, ME)
    assert not path.exists()


def test_verify_admits_the_announced_process_of_the_host_uid(tmp_path):
    run = _run_dir(tmp_path)
    devmode.announce(run, UID)
    assert devmode.verify(run, UID, _creds()) is True


def test_verify_refuses_without_a_file(tmp_path):
    assert devmode.verify(_run_dir(tmp_path), UID, _creds()) is False


def test_verify_refuses_a_mismatching_pid(tmp_path):
    run = _run_dir(tmp_path)
    devmode.announce(run, UID)
    assert devmode.verify(run, UID, _creds(pid=os.getppid())) is False


def test_verify_refuses_another_uid(tmp_path):
    run = _run_dir(tmp_path)
    devmode.announce(run, UID)
    assert devmode.verify(run, UID, _creds(uid=UID + 1)) is False


def _rewrite(path: Path, **change) -> None:
    doc = {**json.loads(path.read_text()), **change}
    path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)


def test_verify_refuses_a_stale_file_whose_pid_was_reused(tmp_path):
    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    _rewrite(path, birth=int(json.loads(path.read_text())["birth"]) - 1)  # another process once had this PID
    assert devmode.verify(run, UID, _creds()) is False


def test_verify_refuses_a_file_for_a_dead_process(tmp_path):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    _rewrite(path, pid=child.pid)
    assert devmode.verify(run, UID, _creds(pid=child.pid)) is False


def test_verify_refuses_a_file_that_names_another_uid_or_is_not_private(tmp_path):
    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    _rewrite(path, uid=UID + 1)
    assert devmode.verify(run, UID, _creds()) is False
    path = devmode.announce(run, UID)
    os.chmod(path, 0o644)
    assert devmode.verify(run, UID, _creds()) is False
    os.chmod(path, 0o600)
    os.chmod(path.parent, 0o755)
    assert devmode.verify(run, UID, _creds()) is False


def test_verify_refuses_a_link(tmp_path):
    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    real = tmp_path / "elsewhere.json"
    os.replace(path, real)
    path.symlink_to(real)
    assert devmode.verify(run, UID, _creds()) is False


@pytest.mark.parametrize(
    "run", ["/Users/Shared/orch/run/501", "/Users/Shared/orch/run", "/run/orch/1000", "/run/orch/1000/../1000"]
)
def test_the_service_run_directories_are_recognized(run):
    assert devmode.is_service_run_dir(Path(run)) is True


def test_a_dev_run_directory_is_not_a_service_one(tmp_path):
    assert devmode.is_service_run_dir(tmp_path / "state" / "run") is False


def test_verify_refuses_in_a_service_run_directory():
    assert devmode.verify(Path("/Users/Shared/orch/run") / str(UID), UID, _creds()) is False


# -- the host --------------------------------------------------------------------------------------------------------


def test_the_host_refuses_dev_runner_with_a_service_run_directory(tmp_path):
    for run in (Path("/Users/Shared/orch/run") / str(UID), Path("/run/orch") / str(UID), None):
        with pytest.raises(HostError) as exc:
            Host(tmp_path / "state", run, dev_runner=True)
        assert exc.value.code == "invalid.input"


def test_a_dev_runner_host_admits_the_announced_runner(tmp_path):
    w = world(tmp_path, dev_runner=True)
    try:
        devmode.announce(Path(w.lab.run), UID)
        sock, welcome = _hello(w.h.host.runner_path)
        try:
            assert welcome["peer"]["class"] == "runner"
        finally:
            sock.close()
    finally:
        w.h.stop()
        w.lab.close()


def test_a_dev_runner_host_refuses_an_unannounced_runner(tmp_path):
    w = world(tmp_path, dev_runner=True)
    try:
        frame = _first_frame(w.h.host.runner_path)
        assert frame["t"] == "fatal" and frame["error"]["code"] == "peer.denied"
    finally:
        w.h.stop()
        w.lab.close()


def test_without_the_opt_in_an_announced_runner_is_refused(tmp_path):
    w = world(tmp_path)  # the default identity: the service manager's main PID only (none under pytest)
    try:
        devmode.announce(Path(w.lab.run), UID)
        frame = _first_frame(w.h.host.runner_path)
        assert frame["t"] == "fatal" and frame["error"]["code"] == "peer.denied"
    finally:
        w.h.stop()
        w.lab.close()


# -- the CLI ---------------------------------------------------------------------------------------------------------


def _host_cli(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, dict]:
    from orch.host import cli

    out = io.StringIO()
    code = cli.main(argv, env=env or {}, out=out)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_the_cli_needs_foreground_for_dev_runner(tmp_path):
    code, doc = _host_cli(["run", "--dev-runner", "--state", str(tmp_path), "--run", str(tmp_path / "r")])
    assert code != 0 and doc["error"]["code"] == "invalid.input"
    assert "--foreground" in doc["error"]["message"]


@pytest.mark.parametrize("run", ["/Users/Shared/orch/run/501", "/run/orch/501"])
def test_the_cli_refuses_dev_runner_with_a_service_run_directory(tmp_path, run):
    code, doc = _host_cli(["run", "--foreground", "--dev-runner", "--state", str(tmp_path), "--run", run])
    assert code != 0 and doc["error"]["code"] == "invalid.input"


@pytest.mark.parametrize("env", [{"XPC_SERVICE_NAME": "io.orch.host"}, {"XPC_SERVICE_NAME": "io.orch.runner"}])
def test_the_cli_refuses_dev_runner_under_the_service_manager(tmp_path, env):
    code, doc = _host_cli(
        ["run", "--foreground", "--dev-runner", "--state", str(tmp_path), "--run", str(tmp_path / "r")], env
    )
    assert code != 0 and doc["error"]["code"] == "invalid.input"


# -- end to end: a separate `orch runner run --foreground` process -----------------------------------------------------


def _runner_process(w, tmp_path: Path):
    import subprocess

    src = str(Path(__file__).resolve().parents[2] / "src")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "PYTHONPATH": src, "SHELL": "/bin/sh",
           "ORCH_STATE_DIR": str(w.lab.state), "ORCH_HOST_RUN_DIR": str(w.lab.run)}  # fmt: skip
    code = "import sys; from orch.cli.main import main; sys.exit(main(['runner', 'run', '--foreground']))"
    return subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip


def _stop(p) -> None:
    import signal
    import subprocess

    if p.poll() is None:  # only the process this test started
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


@pytest.mark.parametrize("dev_runner", [True, False])
def test_a_separate_foreground_runner_connects_only_to_a_dev_runner_host(tmp_path, dev_runner):
    import time

    w = world(tmp_path, dev_runner=dev_runner)
    child = None
    try:
        child = _runner_process(w, tmp_path)
        announced = devmode.path_for(Path(w.lab.run), UID)
        hub = w.h.host.runners
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (hub.connected if dev_runner else announced.exists()):
            time.sleep(0.05)
        if dev_runner:
            assert hub.connected and hub.pid == child.pid
        else:
            assert announced.exists()
            time.sleep(1.0)  # several connection attempts: every one is peer.denied
            assert not hub.connected
        _stop(child)
        assert not announced.exists()  # withdrawn at exit
    finally:
        if child is not None:
            _stop(child)
        w.h.stop()
        w.lab.close()


# -- review hardening (#475): identity, not spelling; never block on a FIFO --------------------------------------------


@pytest.mark.skipif(not os.path.isdir("/Users/Shared"), reason="needs /Users/Shared")
def test_a_service_run_directory_in_another_case_is_recognized_by_identity():
    if not os.path.exists("/users/shared"):
        pytest.skip("case-sensitive file system")
    assert devmode.is_service_run_dir(Path("/users/shared/orch/run/501")) is True
    assert devmode.is_service_run_dir(Path("/USERS/Shared")) is False  # an ancestor of the root is not inside it


def test_a_link_to_a_service_root_is_recognized_by_identity(tmp_path, monkeypatch):
    root = tmp_path / "svc"
    root.mkdir()
    monkeypatch.setattr(devmode, "SERVICE_RUN_ROOTS", (root,))
    (tmp_path / "alias").symlink_to(root)
    assert devmode.is_service_run_dir(tmp_path / "alias" / "501") is True
    assert devmode.is_service_run_dir(tmp_path / "other") is False


def test_verify_never_blocks_on_a_fifo(tmp_path):
    import threading

    run = _run_dir(tmp_path)
    path = devmode.announce(run, UID)
    path.unlink()
    os.mkfifo(path, 0o600)
    got: list[bool] = []
    t = threading.Thread(target=lambda: got.append(devmode.verify(run, UID, _creds())), daemon=True)
    t.start()
    t.join(5)
    assert got == [False], "verify blocked on a FIFO"
