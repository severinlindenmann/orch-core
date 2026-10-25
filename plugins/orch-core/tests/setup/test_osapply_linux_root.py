"""H1 L2 against a real Linux machine as root (GitHub CI, ubuntu-latest with passwordless sudo).

Creates the real users, directories, pins and units on the runner, then asks ``isolation.detect()`` with the real
``RealOsFacts``. Runs only on Linux in CI; a missing prerequisite FAILS there (never skips), like test_isolation_linux.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

SUDO = ["sudo", "-n"]


def _unmet(reason: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(f"CI must run this test, but: {reason}")
    pytest.skip(reason)


def _sudo(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([*SUDO, *argv], capture_output=True, text=True, check=check)


@pytest.fixture
def root_machine(request):
    if not os.environ.get("CI"):
        _unmet("these tests create system users; they run in CI only")
    if subprocess.run([*SUDO, "true"], capture_output=True).returncode != 0:
        _unmet("passwordless sudo is not available")
    for binary in ("/usr/sbin/useradd", "/usr/bin/systemctl", "/usr/bin/systemd-tmpfiles", "/usr/bin/python3"):
        if not os.path.exists(binary):
            _unmet(f"{binary} is missing")
    person = pwd.getpwuid(os.getuid()).pw_name
    _undo(person, None, None)  # leftovers of an aborted earlier run on this machine
    staging = f"/var/lib/orch-ci-staging-{os.getpid()}"
    release = f"/var/lib/orch-ci-release-{os.getpid()}"
    request.addfinalizer(lambda: _undo(person, release, staging))  # runs when the setup below fails too
    # Under /var/lib, a root-only base the image does not touch: /opt on the GitHub runner image is shared with the
    # tool caches and may not be a plain root 0755 directory, which the release check (rightly) refuses.
    # outside /tmp: the units use PrivateTmp. A stand-in program that just stays up. Root-owned here (a person-owned
    # release needs the person's home to be traversable by the agent, which the person's ACL step does: L5).
    # every directory on its own, explicitly root-owned 0755 (install -d gives parents the umask's mode)
    for sub in ("", "/current", "/current/bin"):
        _sudo("install", "-d", "-o", "root", "-g", "root", "-m", "0755", f"{release}{sub}")
    stub = tmp_stub()
    _sudo("install", "-o", "root", "-g", "root", "-m", "0755", stub, f"{release}/current/bin/orch-runner")
    shown = _sudo("stat", "-c", "%U:%G %a %n", release, f"{release}/current", f"{release}/current/bin").stdout
    assert shown.count("root:root 755") == 3, shown
    # the installer's step: a sealed root staging directory (under root-only parents), then the documented command
    _sudo("install", "-d", "-m", "0700", "-o", "root", staging)
    copy = (
        "import shutil, pathlib, sys; from orch.setup import oslauncher as o; "
        "src = pathlib.Path(o.__file__).parents[2]; dst = pathlib.Path(sys.argv[1]); "
        "[(dst / r).parent.mkdir(parents=True, exist_ok=True) or shutil.copyfile(src / r, dst / r) "
        "for r in (*o.TRUSTED_FILES, 'orch/setup/osinstall.py')]"
    )
    assert _sudo(sys.executable, "-I", "-c", copy, staging, check=False).returncode == 0
    _sudo("chmod", "-R", "go-rwx", staging)
    done = _sudo("/usr/bin/python3", "-I", "-S", f"{staging}/orch/setup/osinstall.py", check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    yield person, release


def _undo(person: str, release: str | None, staging: str | None) -> None:
    """Remove exactly what these tests create, by exact name (never a glob), whatever state they stopped in. Called
    before the test too, so a previous aborted run on the same machine leaves no user for the plan to call foreign."""
    agent = f"orch-agent-{person}"
    systemctl = "/usr/bin/systemctl"
    units = [f"orch-runner@{person}.service", *[f"orch-addon@{n}.service" for n in range(1, 9)]]
    for unit in units:  # the runner runs as the agent user: it must be gone before the user can be
        _sudo(systemctl, "disable", "--now", unit, check=False)
        _sudo(systemctl, "reset-failed", unit, check=False)
    for name in [agent, *[f"orch-addon-{n}" for n in range(1, 9)]]:
        try:
            uid = pwd.getpwnam(name).pw_uid
        except KeyError:
            uid = None
        if uid is not None:
            _sudo("rm", "-f", f"/etc/orch/pins/{uid}.json", check=False)
            _sudo("/usr/sbin/userdel", "--remove", "--force", name, check=False)
        _sudo("/usr/sbin/groupdel", name, check=False)  # a group left without its user
    for path in (
        "/usr/lib/orch/os-setup",
        "/etc/tmpfiles.d/orch.conf",
        "/etc/ssh/sshd_config.d/10-orch-agents.conf",
        "/etc/sudoers.d/99-orch-ci-agent",
        "/etc/systemd/system/orch-runner@.service",
        "/etc/systemd/system/orch-addon@.service",
        f"/run/orch/{os.getuid()}",
        "/run/orch/agents",
        "/var/lib/orch",
        *([release] if release else []),
        *([staging] if staging else []),
    ):
        _sudo("rm", "-rf", path, check=False)
    _sudo(systemctl, "daemon-reload", check=False)


def tmp_stub() -> str:
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    handle.write("#!/bin/sh\nexec sleep 100000\n")
    handle.close()
    return handle.name


def _ancestors(path: str) -> str:
    """For a failure message: owner and mode of every directory on the way to ``path``."""
    chain = [path]
    while chain[-1] != "/":
        chain.append(os.path.dirname(chain[-1]))
    return "\n" + _sudo("stat", "-c", "%U:%G %a %n", *chain, check=False).stdout


def _apply_as_root(person: str, release: str) -> subprocess.CompletedProcess:
    """The documented command: the fixed launcher under the system interpreter, isolated and without site."""
    from orch.setup.oslauncher import LAUNCHER_PATH

    return _sudo("/usr/bin/python3", "-I", "-S", LAUNCHER_PATH, "--person", person, "--release", release, check=False)


def test_apply_creates_users_detect_says_isolated_and_rerun_is_a_noop(root_machine, tmp_path) -> None:
    from orch.host.isolation import detect
    from orch.setup.osfacts import RealOsFacts

    person, release = root_machine
    code = f"from orch.setup import osapply; raise SystemExit(osapply.main(['--person', {person!r}]))"
    refused = _sudo(sys.executable, "-I", "-c", code, check=False)  # root never runs the person's tree
    assert refused.returncode == 2 and "root-owned copy" in refused.stderr, refused.stderr
    first = _apply_as_root(person, release)
    print(first.stdout, first.stderr)
    assert first.returncode == 0, first.stdout + first.stderr + _ancestors(release)
    for n in range(1, 9):
        assert pwd.getpwnam(f"orch-addon-{n}").pw_shell == "/usr/sbin/nologin"
    agent = pwd.getpwnam(f"orch-agent-{person}")
    assert agent.pw_uid < 1000 and agent.pw_dir == f"/var/lib/orch/agents/orch-agent-{person}/home"
    assert Path(f"/etc/orch/pins/{agent.pw_uid}.json").exists()

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    result = detect(RealOsFacts(), person, os.getuid(), str(state), [str(state / "k")])
    # The runner may take a moment to start: poll briefly before judging.
    import time

    for _ in range(20):
        if result.isolated:
            break
        time.sleep(0.5)
        result = detect(RealOsFacts(), person, os.getuid(), str(state), [str(state / "k")])
    print(result.summary, [(r.code, r.message) for r in result.reasons])
    assert result.isolated, [(r.code, r.message) for r in result.reasons]

    # a second run repairs nothing and changes nothing (same test: the users are shared machine state)
    again = _apply_as_root(person, release)
    print(again.stdout, again.stderr)
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout.strip() == "Nothing to change."


def _recheck_as_root(person: str) -> subprocess.CompletedProcess:
    """What the unit's ``ExecStartPre=+`` runs: the same launcher, as root, with ``--recheck <person>``."""
    from orch.setup.oslauncher import LAUNCHER_PATH

    return _sudo("/usr/bin/python3", "-I", "-S", LAUNCHER_PATH, "--recheck", person, check=False)


def _pin_policy(person: str) -> object:
    import json

    uid = pwd.getpwnam(f"orch-agent-{person}").pw_uid
    return json.loads(_sudo("cat", f"/etc/orch/pins/{uid}.json").stdout).get("policy")


def _wait(cond, seconds: float = 30.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.5)
    return cond()


def test_the_runner_unit_rechecks_sudo_as_root_at_every_start(root_machine, tmp_path) -> None:
    """A sudo rule that appears for the agent user AFTER setup (here a drop-in in /etc/sudoers.d, a folder the person
    cannot search on Ubuntu, so the host's signature check cannot see an in-place edit) must stop the next runner start:
    ``ExecStartPre=+ ... --recheck`` asks ``sudo -l -U`` again as root, drops the verdict from the pin and exits 1, so
    systemd refuses the start. Removing the rule and rechecking restores the verdict (with the new signatures)."""
    from orch.host.isolation import detect
    from orch.setup.osfacts import RealOsFacts

    person, release = root_machine
    applied = _apply_as_root(person, release)
    assert applied.returncode == 0, applied.stdout + applied.stderr + _ancestors(release)
    unit = f"orch-runner@{person}.service"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def isolated() -> bool:
        return detect(RealOsFacts(), person, os.getuid(), str(state), [str(state / "k")]).isolated

    def active() -> bool:
        return _sudo("/usr/bin/systemctl", "is-active", "--quiet", unit, check=False).returncode == 0

    assert (
        "ExecStartPre=+/usr/bin/python3 -I -S /usr/lib/orch/os-setup/orch-os-setup --recheck"
        in _sudo("systemctl", "cat", unit).stdout
    )
    assert _wait(active) and _wait(isolated)
    ok = _recheck_as_root(person)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    from orch.setup.oslauncher import LAUNCHER_PATH

    direct = _sudo(LAUNCHER_PATH, "--recheck", person, check=False)  # through the shebang, as `sudo <launcher>` does
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert _pin_policy(person)["sudo"] == "none"

    rule = tmp_path / "99-orch-ci-agent"
    rule.write_text(f"orch-agent-{person} ALL=(ALL) NOPASSWD: ALL\n")
    target = "/etc/sudoers.d/99-orch-ci-agent"
    _sudo("install", "-o", "root", "-g", "root", "-m", "0440", str(rule), target)
    try:
        bad = _recheck_as_root(person)
        assert bad.returncode == 1 and "sudo" in bad.stderr, bad.stdout + bad.stderr
        assert _pin_policy(person) is None  # the verdict is gone: the host answers broken
        assert not isolated()
        _sudo("/usr/bin/systemctl", "restart", unit, check=False)  # the unit's own check refuses this start
        assert _wait(lambda: not active())
    finally:
        _sudo("rm", "-f", target, check=False)
    good = _recheck_as_root(person)
    assert good.returncode == 0, good.stdout + good.stderr
    assert _pin_policy(person)["sudo"] == "none"  # restored, with the signatures of the folder as it is now
    _sudo("/usr/bin/systemctl", "reset-failed", unit, check=False)
    assert _sudo("/usr/bin/systemctl", "restart", unit, check=False).returncode == 0
    assert _wait(active) and _wait(isolated)


_PROBE = """#!/usr/bin/python3
import json, socket, time

def frame(value):
    body = json.dumps(value, separators=(",", ":")).encode()
    return len(body).to_bytes(4, "big") + body

def read(sock):
    size = int.from_bytes(sock.recv(4), "big")
    data = b""
    while len(data) < size:
        data += sock.recv(size - len(data))
    return json.loads(data)

def save(result):
    with open("/run/orch/agents/orch-agent-%(person)s/probe.json", "w") as out:
        out.write(json.dumps(result))

for _ in range(240):
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.connect("/run/orch/%(uid)d/runner/host.sock")
        sock.sendall(frame({"t": "hello", "proto": [1], "client": "probe", "caps": ["runner/1"], "ops": {}}))
        welcome = read(sock)
        save({"welcome": welcome})
        if welcome.get("t") == "welcome":
            break
    except Exception as exc:
        save({"error": repr(exc)})
    time.sleep(0.5)
time.sleep(100000)
"""

_CLIENT = """
import json, socket, sys
sock = socket.socket(socket.AF_UNIX)
try:
    sock.connect(sys.argv[1])
except OSError as exc:
    print(json.dumps({"errno": exc.errno})); raise SystemExit
body = json.dumps({"t": "hello", "proto": [1], "client": "x", "caps": []}).encode()
sock.sendall(len(body).to_bytes(4, "big") + body)
size = int.from_bytes(sock.recv(4), "big"); data = b""
while len(data) < size:
    data += sock.recv(size - len(data))
print(data.decode())
"""


def test_the_host_admits_only_the_runner_unit_through_the_shared_run_directory(
    root_machine, tmp_path, monkeypatch
) -> None:
    """L4 against real users: the host (the person) serves ``/run/orch/<uid>/runner/host.sock`` in the setgid run
    directory root made. The runner unit's main process (here a probe standing in for the runner) is admitted as class
    ``runner``; another process of the SAME agent user is ``peer.denied``; an unrelated user cannot even connect."""
    import logging

    person, release = root_machine
    applied = _apply_as_root(person, release)
    assert applied.returncode == 0, applied.stdout + applied.stderr + _ancestors(release)
    uid, agent = os.getuid(), pwd.getpwnam(f"orch-agent-{person}")
    probe = tmp_path / "probe"
    probe.write_text(_PROBE % {"uid": uid, "person": person})
    _sudo("install", "-o", "root", "-g", "root", "-m", "0755", str(probe), f"{release}/current/bin/orch-runner")
    unit = f"orch-runner@{person}.service"
    assert _sudo("/usr/bin/systemctl", "restart", unit, check=False).returncode == 0
    run = Path("/run/orch") / str(uid)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)  # the real service manager is asked below
    monkeypatch.delenv("ORCH_NO_SERVICE_MANAGER", raising=False)  # (CI may set it: the host would deny every runner)
    host_log: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            host_log.append(self.format(record))

    capture = Capture(level=logging.DEBUG)
    top = logging.getLogger("orch")
    top.addHandler(capture)
    top.setLevel(logging.DEBUG)
    try:
        _check_shared_run(person, uid, agent, run, state, host_log)
    finally:
        top.removeHandler(capture)


def _diagnostics(person: str, uid: int, host_log: list[str]) -> str:
    """What the next CI run needs to say why the probe never connected (shown only on failure)."""
    unit = f"orch-runner@{person}.service"
    sock = f"/run/orch/{uid}/runner/host.sock"
    parts = [
        _sudo("systemctl", "status", unit, "--no-pager", "-l", check=False).stdout,
        _sudo("journalctl", "-u", unit, "-n", "80", "--no-pager", check=False).stdout,
        _sudo("ls", "-lnd", "/run/orch", f"/run/orch/{uid}", f"/run/orch/{uid}/runner", sock, check=False).stdout,
        _sudo(
            "stat", "-c", "%U:%G %a %n", "/run/orch", f"/run/orch/{uid}", f"/run/orch/{uid}/runner", sock, check=False
        ).stdout,
        _sudo("ls", "-ln", f"/run/orch/{uid}", "/run/orch/agents", check=False).stdout,
        "probe.json: " + _sudo("cat", f"/run/orch/agents/orch-agent-{person}/probe.json", check=False).stdout,
        _sudo("systemctl", "show", "-p", "MainPID", "-p", "ExecMainStartTimestampMonotonic", unit, check=False).stdout,
        "host log:\n" + "\n".join(host_log[-60:]),
    ]
    return "\n----\n".join(parts)


def _check_shared_run(person, uid, agent, run, state, host_log) -> None:
    import json
    import stat

    from tests.host.lifecycle_helpers import HostThread

    with HostThread(state, run) as host:
        assert host.host.share is not None and host.host.share.uid == agent.pw_uid
        shown = {p: os.stat(p) for p in (run, run / "runner", run / "runner" / "host.sock", run / "control")}
        assert stat.S_IMODE(shown[run].st_mode) == 0o2750 and shown[run].st_gid == agent.pw_gid
        assert stat.S_IMODE(shown[run / "runner"].st_mode) == 0o2750
        sock = shown[run / "runner" / "host.sock"]
        assert stat.S_IMODE(sock.st_mode) == 0o660 and sock.st_uid == uid and sock.st_gid == agent.pw_gid
        assert stat.S_IMODE(shown[run / "control"].st_mode) == 0o700
        path = f"/run/orch/agents/orch-agent-{person}/probe.json"

        def welcomed() -> bool:
            return '"welcome": {"t": "welcome"' in _sudo("cat", path, check=False).stdout

        if not _wait(welcomed, 60):
            pytest.fail("the runner never connected\n" + _diagnostics(person, uid, host_log))
        got = json.loads(_sudo("cat", path).stdout)["welcome"]
        assert (got["peer"]["class"], got["host_uid"]) == ("runner", uid), got
        target = str(run / "runner" / "host.sock")
        same_user = _sudo("-u", agent.pw_name, "/usr/bin/python3", "-I", "-c", _CLIENT, target)
        assert "peer.denied" in same_user.stdout, same_user.stdout  # the agent user, but not the unit's main process
        other = _sudo("-u", "nobody", "/usr/bin/python3", "-I", "-c", _CLIENT, target)
        assert json.loads(other.stdout) == {"errno": 13}, other.stdout  # EACCES: not even a connection
        control = _sudo(
            "-u", agent.pw_name, "/usr/bin/python3", "-I", "-c", _CLIENT, str(run / "control" / "host.sock")
        )
        assert json.loads(control.stdout) == {"errno": 13}, control.stdout  # host.* stay person-only
