"""``land/sandbox.py`` (design #596, I7): repository code in a check must not reach credentials, the network, the host
state or the landing key, and no sandbox means no check. The probes run a real Python inside the sandbox that tries
each forbidden thing; every attempt must fail. The ``sandbox-exec`` tests run on macOS, the ``bwrap`` tests wherever
bwrap works (skipped with the reason where it is missing or user namespaces are off)."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch.land import sandbox as sbx

PROBE = r"""
import json, os, socket, sys
t = json.loads(sys.argv[1])
res = {}
def attempt(name, fn):
    try:
        fn()
        res[name] = "OK"
    except BaseException as e:  # noqa
        res[name] = "DENIED"
def rd(p):
    def f():
        with open(p, "rb") as fh:
            fh.read(1)
    return f
def wr(p):
    def f():
        with open(p, "wb") as fh:
            fh.write(b"x")
    return f
def unix(p):
    def f():
        s = socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(p)
    return f
def tcp(a):
    def f():
        s = socket.socket(); s.settimeout(2); s.connect(tuple(a))
    return f
def udp(a):
    def f():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b"x", tuple(a))
    return f
for k, p in t["read"].items():
    attempt(k, rd(p))
for k, p in t["list"].items():
    attempt(k, lambda p=p: os.listdir(p))
for k, p in t["write"].items():
    attempt(k, wr(p))
for k, p in t["unix"].items():
    attempt(k, unix(p))
attempt("tcp", tcp(t["tcp"]))
attempt("udp", udp(t["udp"]))
env = os.environ
res["env"] = sorted(env)
res["home"] = env.get("HOME")
res["fds"] = []
for n in range(3, 64):
    try:
        os.fstat(n)
        res["fds"].append(n)
    except OSError:
        pass
print("RESULT " + json.dumps(res))
"""

BACKENDS = ["sandbox-exec", "bwrap"]


def _usable(name: str) -> str | None:
    """None when the backend works here, else the reason to skip."""
    if name == "sandbox-exec" and sys.platform != "darwin":
        return "sandbox-exec exists only on macOS"
    if name == "bwrap" and sys.platform == "darwin":
        return "bwrap is a Linux tool"
    if name == "bwrap" and sbx._bwrap_path() is None:
        return "bwrap is not installed"
    return (
        None
        if sbx._selftest(name)
        else f"{name} is installed but cannot start a sandbox on this host (self-test failed)"
    )


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    reason = _usable(request.param)
    if reason:
        pytest.skip(reason)
    return request.param


class Layout:
    """A fake host: export / out / tmp for the check, and canaries outside them that the check must not reach."""

    def __init__(self, root: Path) -> None:
        self.root = root = Path(os.path.realpath(root))
        self.export, self.out, self.tmp = root / "export", root / "out", root / "tmp"
        for d in (self.export, self.out, self.tmp):
            d.mkdir()
        (self.export / "hello.txt").write_text("hello")
        self.home = root / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.ssh_key = self.home / ".ssh" / "id_ed25519"
        self.ssh_key.write_text("PRIVATE")
        self.state = root / "state"
        (self.state / "land").mkdir(parents=True)
        self.land_key = self.state / "land" / "push_key"
        self.land_key.write_text("PUSHKEY")
        self.state_file = self.state / "events.jsonl"
        self.state_file.write_text("{}")
        self.forbidden = [self.home / ".ssh", self.state / "land", self.state]
        # a host service: a unix socket (ssh-agent stand-in) and a loopback TCP server; neither may see a connection
        self.short = tempfile.mkdtemp(prefix="osb")  # unix socket paths are limited to ~100 bytes
        self.sock_path = os.path.join(self.short, "a.sock")
        self.unix_srv = socket.socket(socket.AF_UNIX)
        self.unix_srv.bind(self.sock_path)
        self.unix_srv.listen(8)
        self.tcp_srv = socket.socket()
        self.tcp_srv.bind(("127.0.0.1", 0))
        self.tcp_srv.listen(8)
        self.udp_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_srv.bind(("127.0.0.1", 0))
        self.udp_srv.settimeout(0.3)

    def targets(self) -> dict:
        real_ssh = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".ssh"  # the account home, whatever $HOME says
        return {
            "read": {"ssh_key": str(self.ssh_key), "land_key": str(self.land_key), "state_file": str(self.state_file),
                     "export_hello": str(self.export / "hello.txt")},
            "list": {"ssh_dir": str(self.home / ".ssh"), "real_ssh_dir": str(real_ssh), "state_dir": str(self.state)},
            "write": {"state_new": str(self.state / "planted"), "home_new": str(self.home / "planted"),
                      "export_new": str(self.export / "planted"), "out_new": str(self.out / "ok"),
                      "tmp_new": str(self.tmp / "ok")},
            "unix": {"agent_socket": self.sock_path},
            "tcp": list(self.tcp_srv.getsockname()),
            "udp": list(self.udp_srv.getsockname()),
        }  # fmt: skip

    def close(self) -> None:
        for s in (self.unix_srv, self.tcp_srv, self.udp_srv):
            s.close()
        shutil.rmtree(self.short, ignore_errors=True)


@pytest.fixture
def lay(tmp_path: Path) -> Iterator[Layout]:
    lay = Layout(tmp_path)
    yield lay
    lay.close()


def _toolchain() -> list[Path]:
    return [Path(os.path.realpath(sys.base_prefix))]


def _probe(lay: Layout, backend: str, **kw) -> tuple[sbx.Result, dict]:
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", PROBE, json.dumps(lay.targets())],
        export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=60, **kw,
    )  # fmt: skip
    res = sbx.run_check(spec, backend=backend)
    line = next((ln for ln in res.output.decode(errors="replace").splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"probe did not report: rc={res.returncode} out={res.output[:800]!r}"
    return res, json.loads(line[len("RESULT ") :])


# ------------------------------------------------------------------------------------------------ the probes


@pytest.mark.parametrize("with_forbidden", [True, False], ids=["forbidden-listed", "default-deny-only"])
def test_probes_all_fail(lay: Layout, backend: str, with_forbidden: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", lay.sock_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    kw = {"forbidden": lay.forbidden} if with_forbidden else {}
    res, got = _probe(lay, backend, **kw)
    assert res.returncode == 0 and not res.timed_out and res.backend == backend
    # every forbidden attempt failed ...
    for name in ("ssh_key", "land_key", "state_file", "ssh_dir", "real_ssh_dir", "state_dir", "state_new", "home_new",
                 "export_new", "agent_socket", "tcp", "udp"):  # fmt: skip
        assert got[name] == "DENIED", f"{name} was not denied under {backend}"
    # ... and the allowed ones worked, so the probe itself is sound
    for name in ("export_hello", "out_new", "tmp_new"):
        assert got[name] == "OK", f"{name} should be allowed under {backend}"
    # no service saw anything
    lay.tcp_srv.settimeout(0.2)
    with pytest.raises(OSError):
        lay.tcp_srv.accept()
    lay.unix_srv.settimeout(0.2)
    with pytest.raises(OSError):
        lay.unix_srv.accept()
    with pytest.raises(OSError):
        lay.udp_srv.recvfrom(10)
    assert not (lay.state / "planted").exists() and not (lay.home / "planted").exists()
    assert not (lay.export / "planted").exists()


def test_environment_is_built_from_scratch(lay: Layout, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("SSH_AUTH_SOCK", "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "GIT_SSH_COMMAND", "ORCH_GRANT"):
        monkeypatch.setenv(k, "leak")
    _res, got = _probe(lay, backend, env={"TZ": "UTC"})
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "TERM", "CI", "ORCH_LAND_EXPORT", "ORCH_LAND_OUT", "TZ"}
    # the shell/runtime may add PWD, SHLVL, _, LC_CTYPE; nothing from the parent may appear
    assert set(got["env"]) - allowed <= {"PWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert "leak" not in json.dumps(got["env"])
    assert got["home"] in (str(lay.tmp), "/tmp")


def test_no_fds_leak_into_the_check(lay: Layout, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    held = [os.open(lay.export / "hello.txt", os.O_RDONLY) for _ in range(3)]
    for fd in held:
        os.set_inheritable(fd, True)
    try:
        _res, got = _probe(lay, backend)
    finally:
        for fd in held:
            os.close(fd)
    assert got["fds"] == []  # stdin/out/err only; python's own scratch fds are closed by the time it lists


def test_keychain_binary_is_useless_inside(lay: Layout) -> None:
    """A copy of ``/usr/bin/security`` placed in the (executable) export dir cannot reach securityd: no mach lookups."""
    if sys.platform != "darwin":
        pytest.skip("macOS keychain only")
    reason = _usable("sandbox-exec")
    if reason:
        pytest.skip(reason)
    shutil.copy("/usr/bin/security", lay.export / "sec")
    for argv in (
        ["/usr/bin/security", "find-generic-password", "-s", "orch-land-probe-does-not-exist"],
        [str(lay.export / "sec"), "find-generic-password", "-s", "orch-land-probe-does-not-exist"],
        [str(lay.export / "sec"), "dump-keychain"],
    ):
        spec = sbx.Spec(argv=argv, export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, timeout=30)
        res = sbx.run_check(spec, backend="sandbox-exec")
        # 44 = "item not found": securityd answered, so the keychain was reachable. Anything else = not reached.
        assert res.returncode not in (0, 44), (argv[0], res.returncode, res.output[:300])
        assert b"password:" not in res.output.lower()


def test_ssh_agent_listing_denied_on_launchd_socket_dir(lay: Layout) -> None:
    """The real launchd listener (``/private/tmp/com.apple.launchd.*/Listeners``) cannot be listed or connected."""
    if sys.platform != "darwin":
        pytest.skip("launchd sockets are macOS only")
    reason = _usable("sandbox-exec")
    if reason:
        pytest.skip(reason)
    code = (
        "import glob,socket,sys\n"
        "found=glob.glob('/private/tmp/com.apple.launchd.*/Listeners')\n"
        "print('LISTED',len(found))\n"
        "ok=0\n"
        "for p in found:\n"
        "    try:\n"
        "        s=socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect(p); ok+=1\n"
        "    except OSError: pass\n"
        "print('CONNECTED',ok)\n"
    )
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", code], export_dir=lay.export, out_dir=lay.out,
        tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=30,
    )  # fmt: skip
    res = sbx.run_check(spec, backend="sandbox-exec")
    assert b"LISTED 0" in res.output and b"CONNECTED 0" in res.output, res.output


# ------------------------------------------------------------------------------------------------ process control


def test_timeout_kills_the_group(lay: Layout, backend: str) -> None:
    spec = sbx.Spec(
        argv=["/bin/sh", "-c", "sleep 60"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, timeout=1
    )
    t0 = time.monotonic()
    res = sbx.run_check(spec, backend=backend)
    assert res.timed_out and res.returncode != 0 and time.monotonic() - t0 < 10


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_background_children_do_not_outlive_the_check(lay: Layout, backend: str) -> None:
    script = 'sleep 120 & echo $! > "$ORCH_LAND_OUT/pid"; sleep 0.3'
    spec = sbx.Spec(argv=["/bin/sh", "-c", script], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, timeout=30)
    res = sbx.run_check(spec, backend=backend)
    assert res.returncode == 0
    pidfile = lay.out / "pid"
    if backend == "bwrap":
        return  # the pid namespace is torn down with the check; the pid is not meaningful on the host
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid)


def test_output_is_capped_and_merged(lay: Layout, backend: str) -> None:
    script = "echo out; echo err 1>&2; head -c 300000 /dev/zero | tr '\\0' x"
    spec = sbx.Spec(
        argv=["/bin/sh", "-c", script],
        export_dir=lay.export,
        out_dir=lay.out,
        tmp_dir=lay.tmp,
        max_output=1000,
        timeout=30,
    )  # fmt: skip
    res = sbx.run_check(spec, backend=backend)
    assert res.returncode == 0 and res.truncated and len(res.output) == 1000
    assert res.output.startswith(b"out\nerr\n") or res.output.startswith(b"err\nout\n")


def test_exit_status_is_reported(lay: Layout, backend: str) -> None:
    spec = sbx.Spec(argv=["/bin/sh", "-c", "exit 7"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp)
    res = sbx.run_check(spec, backend=backend)
    assert res.returncode == 7 and not res.timed_out and not res.truncated


# ------------------------------------------------------------------------------------------------ refusals (any OS)


def test_no_backend_refuses_and_never_spawns(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sbx, "SANDBOX_EXEC", str(lay.root / "no-such-sandbox-exec"))
    monkeypatch.setattr(sbx, "BWRAP_CANDIDATES", ())

    def boom(*a, **k):
        raise AssertionError("a check was spawned without a sandbox")

    monkeypatch.setattr(subprocess, "Popen", boom)
    spec = sbx.Spec(argv=["/bin/echo", "x"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp)
    assert sbx.available_backend() is None
    with pytest.raises(sbx.NoSandbox) as e:
        sbx.run_check(spec)
    assert e.value.code == "land.no_sandbox"


def test_forced_backend_must_pass_selftest(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sbx, "_selftest", lambda name: False)
    spec = sbx.Spec(argv=["/bin/echo", "x"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp)
    for forced in ("sandbox-exec", "bwrap", "none", "unsandboxed"):
        with pytest.raises(sbx.NoSandbox):
            sbx.run_check(spec, backend=forced)


def test_broken_backend_binary_is_not_available(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = lay.root / "fake-sandbox-exec"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sbx, "SANDBOX_EXEC", str(fake))
    monkeypatch.setattr(sbx, "BWRAP_CANDIDATES", (str(fake),))
    assert not sbx._selftest("sandbox-exec") if sys.platform == "darwin" else not sbx._selftest("bwrap")


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda x: {"argv": ["sh", "-c", "x"]}, "relative argv[0]"),
        (lambda x: {"argv": []}, "empty argv"),
        (lambda x: {"env": {"SSH_AUTH_SOCK": "/x"}}, "env name not allowed"),
        (lambda x: {"env": {"GITHUB_TOKEN": "t"}}, "env name not allowed"),
        (lambda x: {"env": {"LANG": "a\nb"}}, "newline in env value"),
        (lambda x: {"out_dir": x.root / "missing"}, "not a directory"),
        (lambda x: {"out_dir": x.export}, "overlap"),
        (lambda x: {"tmp_dir": x.export / "sub"}, "tmp inside export"),
        (lambda x: {"toolchain_ro": [Path("/")]}, "root as toolchain"),
        (lambda x: {"toolchain_ro": [x.state]}, "toolchain over forbidden"),
        (lambda x: {"toolchain_ro": [x.home]}, "toolchain inside forbidden"),
        (lambda x: {"export_dir": x.state / "land"}, "export inside forbidden"),
        (lambda x: {"timeout": 0}, "timeout"),
        (lambda x: {"max_output": 0}, "max_output"),
    ],
)  # fmt: skip
def test_bad_requests_are_refused_before_anything_runs(
    lay: Layout, monkeypatch: pytest.MonkeyPatch, mutate, why
) -> None:
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    (lay.export / "sub").mkdir(exist_ok=True)
    base = {"argv": ["/bin/echo", "x"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp,
            "forbidden": [lay.state, lay.home / ".ssh"]}  # fmt: skip
    base.update(mutate(lay))
    spec = sbx.Spec(**base)
    with pytest.raises(sbx.SandboxError):
        sbx._validated(spec)
        sbx.build_env(spec, home="/h", out="/o", export="/e", tool=[])  # env checks live here


def test_allowed_path_may_not_be_the_home_directory(lay: Layout) -> None:
    spec = sbx.Spec(argv=["/bin/echo"], export_dir=lay.export, out_dir=Path.home(), tmp_dir=lay.tmp)
    with pytest.raises(sbx.SandboxError):
        sbx._validated(spec)


def test_profile_text_is_deny_by_default() -> None:
    p = sbx.seatbelt_profile(
        Path("/w/e"), Path("/w/o"), Path("/w/t"), [Path("/opt/tc")], [Path("/h/.ssh"), Path("/s/land")]
    )
    lines = p.splitlines()
    assert lines[:2] == ["(version 1)", "(deny default)"]
    assert "(deny network*)" in lines
    assert not any(ln.startswith("(allow default") or "(allow network" in ln or "mach-lookup" in ln for ln in lines)
    assert lines[-1].startswith("(deny file*") and '"/h/.ssh"' in lines[-1] and '"/s/land"' in lines[-1]
    assert 'file-write* (subpath "/w/o") (subpath "/w/t")' in p  # writes: out and tmp only
    assert not any("file-write*" in ln and '"/w/e"' in ln for ln in lines)  # export is read-only


def test_bwrap_command_line_shape(lay: Layout) -> None:
    spec = sbx.Spec(
        argv=["/usr/bin/make", "test"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp,
        toolchain_ro=[Path("/opt/toolchain")], forbidden=lay.forbidden, env={"TZ": "UTC"},
    )  # fmt: skip
    argv, _cwd = sbx.bwrap_argv("/usr/bin/bwrap", spec)
    flat = " ".join(argv)
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--cap-drop ALL"):
        assert flag in flat
    assert "--share-net" not in argv
    assert (
        f"--ro-bind {lay.export} /work" in flat
        and f"--bind {lay.out} /out" in flat
        and f"--bind {lay.tmp} /tmp" in flat
    )
    assert "--ro-bind /opt/toolchain /opt/toolchain" in flat
    assert str(lay.state) not in flat and str(lay.home) not in flat  # forbidden and home are never mounted
    assert "SSH_AUTH_SOCK" not in flat
    assert argv[-3:] == ["--", "/usr/bin/make", "test"] or argv[-2:] == ["/usr/bin/make", "test"]
    # the only ro-bind of a writable target would be a bug: out and tmp are rw binds, export is ro
    assert "--bind /usr" not in flat and "--bind / " not in flat
