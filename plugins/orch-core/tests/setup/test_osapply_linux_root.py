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
def root_machine():
    if not os.environ.get("CI"):
        _unmet("these tests create system users; they run in CI only")
    if subprocess.run([*SUDO, "true"], capture_output=True).returncode != 0:
        _unmet("passwordless sudo is not available")
    for binary in ("/usr/sbin/useradd", "/usr/bin/systemctl", "/usr/bin/systemd-tmpfiles", "/usr/bin/python3"):
        if not os.path.exists(binary):
            _unmet(f"{binary} is missing")
    person = pwd.getpwuid(os.getuid()).pw_name
    # Under /var/lib, a root-only base the image does not touch: /opt on the GitHub runner image is shared with the
    # tool caches and may not be a plain root 0755 directory, which the release check (rightly) refuses.
    release = f"/var/lib/orch-ci-release-{os.getpid()}"
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
    staging = f"/var/lib/orch-ci-staging-{os.getpid()}"
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
    names = [f"orch-agent-{person}", *[f"orch-addon-{n}" for n in range(1, 9)]]
    for unit in [f"orch-runner@orch-agent-{person}"] + [f"orch-addon@orch-addon-{n}" for n in range(1, 9)]:
        _sudo("/usr/bin/systemctl", "disable", "--now", f"{unit}.service", check=False)
    for name in names:
        try:
            _sudo("rm", "-f", f"/etc/orch/pins/{pwd.getpwnam(name).pw_uid}.json", check=False)
        except KeyError:
            pass
        _sudo("/usr/sbin/userdel", "--remove", name, check=False)
    _sudo("rm", "-rf", "/usr/lib/orch/os-setup", staging, check=False)
    for path in (
        "/etc/tmpfiles.d/orch.conf",
        "/etc/ssh/sshd_config.d/10-orch-agents.conf",
        "/etc/systemd/system/orch-runner@.service",
        "/etc/systemd/system/orch-addon@.service",
    ):
        _sudo("rm", "-f", path, check=False)
    _sudo("rm", "-rf", release, f"/run/orch/{os.getuid()}", "/run/orch/agents", "/var/lib/orch", check=False)
    _sudo("/usr/bin/systemctl", "daemon-reload", check=False)


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
