"""The exec trampoline (D-P2-29): session, controlling TTY, rlimits, pinned cwd, closed fds, exact environment."""

from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

import pytest

from orch.runner.supervise import SpawnError
from tests.runner.helpers import py, spec, wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")

PROBE = r"""
import json, os, resource, sys
fds = {}
for n in (%(leak)d,):
    try:
        os.fstat(n)
        fds[n] = "open"
    except OSError:
        fds[n] = "closed"
out = {
    "pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgrp(), "tty": os.ttyname(0),
    "tpgid": os.tcgetpgrp(0), "cwd": os.getcwd(), "core": resource.getrlimit(resource.RLIMIT_CORE),
    "nofile": resource.getrlimit(resource.RLIMIT_NOFILE), "env": dict(os.environ), "fds": fds,
}
sys.stdout.write("BEGIN" + json.dumps(out) + "END")
sys.stdout.flush()
"""


def _probe(sup, cwd: Path, env: dict[str, str]) -> dict:
    leak_r, leak_w = os.pipe()
    os.set_inheritable(leak_w, True)  # an inheritable fd of the runner must still not reach the run
    try:
        s = spec(cwd, py(PROBE % {"leak": leak_w}), env=env)
        res = sup.spawn(s)
    finally:
        os.close(leak_r)
        os.close(leak_w)
    ring = sup.ring(s.run)

    def done():
        data = ring.read()[2]
        return b"END" in data and data

    data = wait_for(done)
    doc = json.loads(data[data.index(b"BEGIN") + 5 : data.index(b"END")])
    doc["res"] = res
    doc["leak"] = leak_w
    return doc


def test_session_tty_rlimits_cwd_fds_env(sup, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": "/usr/bin:/bin", "ORCH_TICKET": "ABC-1", "LANG": "C"}
    doc = _probe(sup, work, env)
    assert doc["pid"] == doc["res"]["pid"] == doc["sid"] == doc["pgid"]  # its own session and group
    assert doc["tty"].startswith("/dev/")  # the PTY is the controlling terminal
    assert doc["tpgid"] == doc["pgid"]  # and the run is its foreground group
    assert doc["core"] == [0, 0]
    soft, hard = doc["nofile"]
    want = 8192 if hard == resource.RLIM_INFINITY else min(hard, 8192)
    assert soft >= want or soft == resource.RLIM_INFINITY
    assert os.path.realpath(doc["cwd"]) == os.path.realpath(work)
    assert doc["fds"][str(doc["leak"])] == "closed"
    extra = {k for k in doc["env"] if k not in env}
    # the interpreter of the probe may add its own (macOS: __CF_USER_TEXT_ENCODING, __PYVENV_LAUNCHER__)
    assert extra <= {"__CF_USER_TEXT_ENCODING", "__PYVENV_LAUNCHER__", "LC_CTYPE"}, extra
    assert {k: doc["env"][k] for k in env} == env


def test_swapped_cwd_is_cwd_changed(sup, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    s = spec(work, ["/bin/sh", "-c", "exit 0"])
    work.rename(tmp_path / "moved")
    work.mkdir()  # same path, another directory
    with pytest.raises(SpawnError) as err:
        sup.spawn(s)
    assert err.value.code == "launch.cwd_changed"
    assert sup.get(s.run)["exit"]["reason"] == "spawn_failed"


def test_symlinked_cwd_component_is_refused(sup, tmp_path):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    s = spec(real / "inner", ["/bin/sh", "-c", "exit 0"])
    s = type(s)(s.run, s.workspace_id, s.kind, s.argv, s.env, {**s.cwd, "path": str(link / "inner")})
    with pytest.raises(SpawnError) as err:
        sup.spawn(s)
    assert err.value.code == "launch.cwd_changed"


def test_missing_program_is_harness_not_found(sup, tmp_path):
    with pytest.raises(SpawnError) as err:
        sup.spawn(spec(tmp_path, [str(tmp_path / "no-such-harness")]))
    assert err.value.code == "harness.not_found"


def test_relative_program_is_refused_before_any_process(sup, tmp_path):
    with pytest.raises(SpawnError) as err:
        sup.spawn(spec(tmp_path, ["sh", "-c", "true"]))
    assert err.value.code == "invalid.input"


def test_trampoline_joins_the_cgroup_before_exec(tmp_path):
    """The cgroup step writes the trampoline's own PID (which becomes the run's PID) to ``cgroup.procs`` before exec;
    a fake cgroup directory stands in for the delegated one."""
    import subprocess

    from orch.runner.supervise import EXEC

    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "cgroup.procs").write_text("")
    work = tmp_path / "w"
    work.mkdir()
    st = os.stat(work)
    payload = {
        "argv": ["/bin/sh", "-c", "echo $$"],
        "env": {},
        "cwd": {"path": str(work), "dev": st.st_dev, "ino": st.st_ino},
        "cgroup": str(cg),
        "tty": False,
    }
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-I", str(EXEC), str(spec_r), str(status_w)],
        pass_fds=(spec_r, status_w),
        stdout=subprocess.PIPE,
        env={},
    )
    os.close(spec_r)
    os.close(status_w)
    os.write(spec_w, json.dumps(payload).encode())
    os.close(spec_w)
    out, _ = proc.communicate(timeout=10)
    status = os.read(status_r, 4096)
    os.close(status_r)
    assert status == b""  # exec'd
    assert (cg / "cgroup.procs").read_text().strip() == str(proc.pid) == out.decode().strip()


def test_trampoline_refuses_a_bad_spec(tmp_path):
    import subprocess

    from orch.runner.supervise import EXEC

    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-I", str(EXEC), str(spec_r), str(status_w)], pass_fds=(spec_r, status_w), env={}
    )
    os.close(spec_r)
    os.close(status_w)
    os.write(spec_w, json.dumps({"argv": ["relative"], "env": {}, "cwd": {}}).encode())
    os.close(spec_w)
    assert proc.wait(timeout=10) == 127
    assert json.loads(os.read(status_r, 4096))["step"] == "spec"
    os.close(status_r)
