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
import threading
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
def dns(name):
    def f():
        socket.getaddrinfo(name, 443)
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
attempt("udp_out", udp(t["udp_out"]))  # a routable address outside the host: needs a route out of the check
attempt("dns", dns(t["dns"]))
if t.get("abstract"):  # Linux only: abstract unix sockets have no file a mount rule could hide
    attempt("abstract_socket", unix("\0" + t["abstract"]))
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
        # Linux: an abstract unix socket on the host (D-Bus, X11, systemd use these); no path, so only the network
        # namespace can keep the check away from it
        self.abstract_name = f"orch-land-probe-{os.getpid()}-{id(self)}" if sys.platform.startswith("linux") else None
        self.abstract_srv = None
        if self.abstract_name:
            self.abstract_srv = socket.socket(socket.AF_UNIX)
            self.abstract_srv.bind("\0" + self.abstract_name)
            self.abstract_srv.listen(8)

    def targets(self, backend: str = "sandbox-exec") -> dict:
        t = self._targets()
        if backend == "bwrap":  # inside bwrap the three working dirs live at fixed paths
            for group in ("read", "write"):
                for k, v in t[group].items():
                    for host, inner in ((self.export, "/work"), (self.out, "/out"), (self.tmp, "/tmp")):
                        if v.startswith(str(host) + "/"):
                            t[group][k] = inner + v[len(str(host)) :]
        return t

    def _targets(self) -> dict:
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
            "udp_out": ["192.0.2.1", 9],  # TEST-NET-1 (RFC 5737): routable syntax, never answered
            "dns": "example.com",
            "abstract": self.abstract_name,
        }  # fmt: skip

    def close(self) -> None:
        for s in (self.unix_srv, self.tcp_srv, self.udp_srv, self.abstract_srv):
            if s is not None:
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
        argv=[os.path.realpath(sys.executable), "-I", "-c", PROBE, json.dumps(lay.targets(backend))],
        export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=60, **kw,
    )  # fmt: skip
    res = sbx.run_check(spec, backend=backend)
    line = next((ln for ln in res.output.decode(errors="replace").splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"probe did not report: rc={res.returncode} out={res.output[:800]!r}"
    return res, json.loads(line[len("RESULT ") :])


# ------------------------------------------------------------------------------------------------ the probes


@pytest.mark.parametrize("with_forbidden", [True, False], ids=["forbidden-listed", "default-deny-only"])
def test_probes_all_fail(lay: Layout, backend: str, with_forbidden: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant is "a check reaches nothing outside itself". Network: on macOS Seatbelt denies every socket call,
    so each network probe fails inside the check. On Linux the boundary is bwrap's private network namespace
    (``--unshare-all``): it holds only its own loopback and no route. A UDP ``sendto(127.0.0.1)`` there succeeds (a
    datagram needs no peer) but lands in the check's own namespace, which no host process listens in; that is not
    network access, so on bwrap the in-namespace ``udp`` result is not asserted. What IS asserted on both backends:
    the host's loopback UDP/TCP servers and its unix socket see nothing, a routable outside address (``udp_out``) has
    no route, name resolution fails, and (Linux) the host's abstract unix socket, which is scoped to the network
    namespace, cannot be connected. Blocking socket() itself with seccomp was rejected: a raw per-arch BPF filter is
    fragile, and toolchains need AF_UNIX/socketpair inside the check."""
    monkeypatch.setenv("SSH_AUTH_SOCK", lay.sock_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    kw = {"forbidden": lay.forbidden} if with_forbidden else {}
    res, got = _probe(lay, backend, **kw)
    assert res.returncode == 0 and not res.timed_out and res.backend == backend
    # every forbidden attempt failed ...
    denied = ["ssh_key", "land_key", "state_file", "ssh_dir", "real_ssh_dir", "state_dir", "state_new", "home_new",
              "export_new", "agent_socket", "tcp", "udp_out", "dns"]  # fmt: skip
    if backend == "sandbox-exec":
        denied.append("udp")  # Seatbelt denies the socket call itself; bwrap: see the docstring
    if lay.abstract_name:
        denied.append("abstract_socket")
    for name in denied:
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
    if lay.abstract_srv is not None:
        lay.abstract_srv.settimeout(0.2)
        with pytest.raises(OSError):
            lay.abstract_srv.accept()
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
    assert not any(ln.startswith("(allow file-write*") and '"/w/e"' in ln for ln in lines)  # export is read-only


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


# ------------------------------------------------------------------------------------------------ links (review 21 Oct)

LINK_PROBE = r"""
import os, sys, json
res = {}
src_a, src_b, out, tmp = sys.argv[1:5]
def attempt(name, fn):
    try:
        fn(); res[name] = "CREATED"
    except OSError:
        res[name] = "DENIED"
attempt("hardlink_land_key_to_out", lambda: os.link(src_a, out + "/x"))
attempt("hardlink_ssh_key_to_tmp", lambda: os.link(src_b, tmp + "/x"))
attempt("symlink_land_key_to_out", lambda: os.symlink(src_a, out + "/s"))
attempt("symlink_ssh_key_to_tmp", lambda: os.symlink(src_b, tmp + "/s"))
for name, path in (("read_via_hardlink", out + "/x"), ("read_via_symlink", out + "/s"),
                   ("read_via_symlink_tmp", tmp + "/s")):
    try:
        res[name] = "LEAKED" if open(path).read(4) else "EMPTY"
    except OSError:
        res[name] = "DENIED"
print("RESULT " + json.dumps(res))
"""


@pytest.mark.parametrize("with_forbidden", [True, False], ids=["forbidden-listed", "default-deny-only"])
def test_check_cannot_link_a_secret_into_out_or_tmp(lay: Layout, backend: str, with_forbidden: bool) -> None:
    """Hard links share the inode, so a link to the land key / an ssh key placed in out/tmp would be readable by path.
    Creating it must fail, a symlink must be unreadable, and the key's link count must stay 1. (bwrap: the forbidden
    paths are not mounted at all, so there is no source to link; the same probe shows it.)"""
    out, tmp = ("/out", "/tmp") if backend == "bwrap" else (str(lay.out), str(lay.tmp))
    kw = {"forbidden": lay.forbidden} if with_forbidden else {}
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", LINK_PROBE, str(lay.land_key), str(lay.ssh_key), out, tmp],
        export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=60, **kw,
    )  # fmt: skip
    res = sbx.run_check(spec, backend=backend)
    got = json.loads(next(ln for ln in res.output.decode().splitlines() if ln.startswith("RESULT "))[7:])
    assert got["hardlink_land_key_to_out"] == "DENIED" and got["hardlink_ssh_key_to_tmp"] == "DENIED", got
    assert "LEAKED" not in got.values() and got["read_via_hardlink"] == "DENIED", got
    assert os.stat(lay.land_key).st_nlink == 1 and os.stat(lay.ssh_key).st_nlink == 1
    assert not (lay.out / "x").exists() and not (lay.tmp / "x").exists()


def test_hard_link_to_a_readable_file_cannot_write_through(lay: Layout, backend: str) -> None:
    """Review 21 Oct (HIGH): link a file the check may only READ (export, toolchain) into out/tmp, then write through
    it. The link itself must fail (EPERM; bwrap: EXDEV/EROFS as export, out and tmp are separate mounts) and the source
    stays unchanged."""
    tool = lay.root / "tool"
    tool.mkdir()
    (tool / "lib.txt").write_text("TOOL")
    code = (
        "import os,sys,json\n"
        "r={}\n"
        "for name,src,dst in (('export',sys.argv[1],sys.argv[3]+'/a'),('toolchain',sys.argv[2],sys.argv[4]+'/b')):\n"
        "    try:\n"
        "        os.link(src,dst); r[name]='LINKED'\n"
        "        open(dst,'a').write('PWNED')\n"
        "    except OSError: r.setdefault(name,'DENIED')\n"
        "print('RESULT '+json.dumps(r))\n"
    )
    if backend == "bwrap":
        a1, a2, o, t = "/work/hello.txt", str(tool / "lib.txt"), "/out", "/tmp"
    else:
        a1, a2, o, t = str(lay.export / "hello.txt"), str(tool / "lib.txt"), str(lay.out), str(lay.tmp)
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", code, a1, a2, o, t], export_dir=lay.export,
        out_dir=lay.out, tmp_dir=lay.tmp, toolchain_ro=[*_toolchain(), tool], timeout=30,
    )  # fmt: skip
    res = sbx.run_check(spec, backend=backend)
    got = json.loads(next(ln for ln in res.output.decode().splitlines() if ln.startswith("RESULT "))[7:])
    assert got == {"export": "DENIED", "toolchain": "DENIED"}, got
    assert (lay.export / "hello.txt").read_text() == "hello" and (tool / "lib.txt").read_text() == "TOOL"
    assert not res.tampered and res.passed


def test_planted_link_during_the_check_is_reported_as_tampering(lay: Layout, backend: str) -> None:
    """Defence in depth: if a link ever appears in out/tmp (here made by the test through a hostile 'check' that the
    sandbox cannot stop because the test creates it from outside), the result is flagged and the host reads nothing."""
    victim = lay.root / "victim"
    victim.write_text("v")
    # the host plants the link while the check sleeps (the pre-launch scan has already passed)
    spec = sbx.Spec(argv=["/bin/sh", "-c", "sleep 0.5"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp)
    done = threading.Event()

    def plant() -> None:
        time.sleep(0.2)
        os.link(victim, lay.out / "planted")
        done.set()

    t = threading.Thread(target=plant)
    t.start()
    res = sbx.run_check(spec, backend=backend)
    t.join()
    assert done.is_set() and res.tampered and not res.passed
    with pytest.raises(sbx.SandboxError):
        sbx.read_out_file(lay.out, "planted")


def test_bwrap_mount_layout_is_ro_export_and_separate_out_tmp(lay: Layout) -> None:
    spec = sbx.Spec(argv=["/usr/bin/true"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp)
    argv, _ = sbx.bwrap_argv("/usr/bin/bwrap", spec)
    i = argv.index("/work")
    assert argv[i - 2 : i + 1] == ["--ro-bind", str(lay.export), "/work"]
    assert argv[argv.index("/out") - 2 :][:3] == ["--bind", str(lay.out), "/out"]
    assert argv[argv.index("/tmp", argv.index("/out")) - 2 :][:3] == ["--bind", str(lay.tmp), "/tmp"]  # own mount each


def test_profile_denies_links_and_export_writes() -> None:
    p = sbx.seatbelt_profile(Path("/w/e"), Path("/w/o"), Path("/w/t"), [Path("/opt/tc")], [Path("/s")])
    lines = p.splitlines()
    assert "(deny file-link)" in lines and not any(ln.startswith("(allow file-link") for ln in lines)
    deny = next(ln for ln in lines if ln.startswith("(deny file-write*"))
    assert '"/w/e"' in deny and '"/opt/tc"' in deny
    assert lines.index("(deny file-link)") > max(i for i, ln in enumerate(lines) if ln.startswith("(allow file-write*"))


def test_forbidden_paths_are_not_mounted_in_bwrap(lay: Layout) -> None:
    spec = sbx.Spec(
        argv=["/usr/bin/true"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, forbidden=lay.forbidden
    )
    argv, _ = sbx.bwrap_argv("/usr/bin/bwrap", spec)
    flat = " ".join(argv)
    assert str(lay.land_key) not in flat and str(lay.ssh_key) not in flat and str(lay.state) not in flat


def test_system_system_volumes_alias_is_not_readable(lay: Layout) -> None:
    """``/System/Volumes/Data/<path>`` is the same file as ``<path>`` on macOS; the profile must not open it."""
    if sys.platform != "darwin":
        pytest.skip("macOS firmlink only")
    reason = _usable("sandbox-exec")
    if reason:
        pytest.skip(reason)
    alias = "/System/Volumes/Data" + str(lay.ssh_key)
    if not os.path.exists(alias):
        pytest.skip("this macOS has no /System/Volumes/Data firmlink alias for the test path")
    code = f"print(open({alias!r}).read())"
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", code], export_dir=lay.export, out_dir=lay.out,
        tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=30,
    )  # fmt: skip
    res = sbx.run_check(spec, backend="sandbox-exec")
    assert b"PRIVATE" not in res.output.replace(alias.encode(), b"") and res.returncode != 0


# ------------------------------------------------------------------------------------------------ host-side reading


def test_read_out_file_accepts_a_plain_file(lay: Layout) -> None:
    (lay.out / "d").mkdir()
    (lay.out / "d" / "report.txt").write_bytes(b"ok")
    assert sbx.read_out_file(lay.out, "d/report.txt") == b"ok"


def test_read_out_file_refuses_everything_else(lay: Layout) -> None:
    (lay.out / "plain").write_text("data")
    os.link(lay.out / "plain", lay.out / "plain2")  # nlink 2: a planted alias
    os.link(lay.land_key, lay.out / "alias_of_key")  # the host-side planting case (what a hostile check would try)
    os.symlink(lay.land_key, lay.out / "sym")
    os.symlink(lay.state, lay.out / "dirsym")
    os.mkfifo(lay.out / "fifo")
    (lay.out / "dir").mkdir()
    for rel in (
        "plain",
        "plain2",
        "alias_of_key",
        "sym",
        "dirsym/events.jsonl",
        "fifo",
        "dir",
        "missing",
        "../x",
        "",
        "a/../b",
    ):
        with pytest.raises(sbx.SandboxError):
            sbx.read_out_file(lay.out, rel)
    assert os.stat(lay.land_key).st_nlink == 2  # the test's own link; the reader never followed or read it


def test_start_refuses_a_tree_with_aliases(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    os.link(lay.land_key, lay.out / "planted")  # a pre-planted hard link to the key
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(**base))
    (lay.out / "planted").unlink()
    os.link(lay.export / "hello.txt", lay.tmp / "alias")  # export file reachable through the writable tmp
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(**base))
    (lay.tmp / "alias").unlink()
    os.mkfifo(lay.tmp / "f")
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(**base))
    (lay.tmp / "f").unlink()
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(os.path.join(lay.short, "x"))
    os.symlink("/", lay.tmp / "link-to-root")  # a plain symlink is fine (resolved inside the sandbox, not by us)
    sbx._validated(sbx.Spec(**base))
    srv.close()


# ------------------------------------------------------------------------------------------------ validation extras


def test_writable_dirs_must_be_ours_private_and_not_symlinks(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    link = lay.root / "linked-out"
    link.symlink_to(lay.out)
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "tmp_dir": lay.tmp}
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(out_dir=link, **base))
    os.chmod(lay.out, 0o777)
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(out_dir=lay.out, **base))


def test_account_secret_dirs_are_always_forbidden(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sbx, "_account_home", lambda: lay.home)
    (lay.home / ".aws").mkdir()
    for d in (".ssh", ".aws"):
        with pytest.raises(sbx.SandboxError):
            sbx._validated(
                sbx.Spec(
                    argv=["/bin/echo"],
                    export_dir=lay.export,
                    out_dir=lay.out,
                    tmp_dir=lay.tmp,
                    toolchain_ro=[lay.home / d],
                )
            )


def test_forbidden_path_inside_a_mounted_system_location_is_refused(lay: Layout) -> None:
    for bad in ("/usr/local/orch-state", "/etc/orch", "/bin/x"):
        spec = sbx.Spec(
            argv=["/bin/echo"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, forbidden=[Path(bad)]
        )
        with pytest.raises(sbx.SandboxError):
            sbx._validated(spec)


def test_selftest_rejects_a_wrapper_that_confines_nothing(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake backend that just runs the command (exit 0) passes ``true`` but fails to block the outside canary."""
    monkeypatch.setattr(sbx, "_trusted_exe", lambda p: True)
    fake = lay.root / "fake"
    fake.write_text('#!/bin/sh\nwhile [ "$1" != "" ]; do case "$1" in /*) exec "$@";; esac; shift; done\n')
    fake.chmod(0o755)
    monkeypatch.setattr(sbx, "SANDBOX_EXEC", str(fake))
    monkeypatch.setattr(sbx, "BWRAP_CANDIDATES", (str(fake),))
    name = "sandbox-exec" if sys.platform == "darwin" else "bwrap"
    assert sbx._selftest(name) is False


def test_untrusted_backend_binary_is_not_used(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = lay.root / "bwrap"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)  # owned by this user, not root: replaceable
    monkeypatch.setattr(sbx, "BWRAP_CANDIDATES", (str(fake),))
    assert sbx._bwrap_path() is None
    assert not sbx._trusted_exe(str(fake))


# ------------------------------------------------------------------------------------------------ Opus review 21 Oct


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive volume and firmlinks are macOS")
def test_alias_spellings_of_a_forbidden_path_are_refused_or_denied(lay: Layout) -> None:
    """Toolchain ``$H/tc`` with forbidden ``$H/TC/state`` (case variant) or ``/System/Volumes/Data$H/tc/state`` must
    be seen as overlapping, and the canonical spelling is what reaches the profile."""
    (lay.root / "tc" / "state").mkdir(parents=True)
    (lay.root / "tc" / "state" / "key").write_text("K")
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    swapped = Path(str(lay.root / "TC" / "state"))
    firm = Path("/System/Volumes/Data" + str(lay.root / "tc" / "state"))
    for forb in (swapped, firm):
        if not os.path.exists(forb):
            continue
        with pytest.raises(sbx.SandboxError):
            sbx._validated(sbx.Spec(toolchain_ro=[lay.root / "tc"], forbidden=[forb], **base))
    assert sbx._real(firm) == sbx._real(lay.root / "tc" / "state")
    assert sbx._overlaps(Path("/A/b"), Path("/a/B/c")) and not sbx._overlaps(Path("/a/b"), Path("/a/bc"))


def test_overlap_is_case_and_normalisation_insensitive() -> None:
    assert sbx._overlaps(Path("/x/Café"), Path("/X/café/k"))
    assert not sbx._overlaps(Path("/x/a"), Path("/x/b"))


def test_missing_path_is_canonicalised_through_its_parent(lay: Layout) -> None:
    assert sbx._real(lay.root / "nope" / "deeper") == sbx._real(lay.root) / "nope" / "deeper"


def test_profile_has_process_info_and_launchctl_denies() -> None:
    p = sbx.seatbelt_profile(Path("/w/e"), Path("/w/o"), Path("/w/t"), [], [])
    assert "(deny process-info*)" in p and "(allow process-info* (target self))" in p
    assert '"/bin/launchctl"' in p


def test_check_cannot_inspect_other_processes_or_launchctl(lay: Layout) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    reason = _usable("sandbox-exec")
    if reason:
        pytest.skip(reason)
    code = (
        "import ctypes,ctypes.util,os,sys\n"
        "l=ctypes.CDLL(ctypes.util.find_library('proc'))\n"
        "b=ctypes.create_string_buffer(4096)\n"
        "n=l.proc_pidpath(int(sys.argv[1]),b,4096)\n"
        "print('PIDPATH',n)\n"
        "print('SELF',l.proc_pidpath(os.getpid(),b,4096)>0)\n"
    )
    spec = sbx.Spec(
        argv=[os.path.realpath(sys.executable), "-I", "-c", code, str(os.getppid())], export_dir=lay.export,
        out_dir=lay.out, tmp_dir=lay.tmp, toolchain_ro=_toolchain(), timeout=30,
    )  # fmt: skip
    res = sbx.run_check(spec, backend="sandbox-exec")
    assert b"PIDPATH 0" in res.output or b"PIDPATH -1" in res.output, res.output
    for argv in (["/bin/launchctl", "print", f"gui/{os.getuid()}"], ["/bin/ps", "-p", str(os.getppid())]):
        r = sbx.run_check(
            sbx.Spec(argv=argv, export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp, timeout=30),
            backend="sandbox-exec",
        )
        assert r.returncode != 0 and b"PID" not in r.output and b"services" not in r.output, (argv, r.output[:200])


def test_group_is_killed_after_the_leader_exits_without_reaping_early(lay: Layout, backend: str) -> None:
    spec = sbx.Spec(
        argv=["/bin/sh", "-c", "sleep 100 & exit 0"], export_dir=lay.export, out_dir=lay.out, tmp_dir=lay.tmp
    )
    assert sbx.run_check(spec, backend=backend).returncode == 0


def test_symlink_then_dotdot_is_resolved_like_the_kernel(lay: Layout) -> None:
    deep = lay.root / "deep" / "dir"
    deep.mkdir(parents=True)
    (lay.root / "deep" / "key").write_text("K")
    (lay.root / "lnk").symlink_to(deep)
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    with pytest.raises(sbx.SandboxError):  # lnk/../key is deep/key, inside the toolchain
        sbx._validated(sbx.Spec(toolchain_ro=[lay.root / "deep"], forbidden=[lay.root / "lnk" / ".." / "key"], **base))


def test_dangling_forbidden_symlink_fails_closed(lay: Layout) -> None:
    (lay.root / "dangling").symlink_to(lay.root / "nowhere")
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(forbidden=[lay.root / "dangling"], **base))


def test_symlinked_ssh_dir_is_forbidden_at_both_spellings(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    real = lay.root / "dotfiles" / "ssh"
    real.mkdir(parents=True)
    home = lay.root / "h2"
    home.mkdir()
    (home / ".ssh").symlink_to(real)
    monkeypatch.setattr(sbx, "_account_home", lambda: home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    secrets = sbx._secret_paths()
    assert home / ".ssh" in secrets and sbx._real(real) in secrets
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(toolchain_ro=[real], **base))


def test_loop_in_a_middle_component_fails_closed(lay: Layout) -> None:
    (lay.root / "loopdir").symlink_to(lay.root / "loopdir")
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    with pytest.raises(sbx.SandboxError):
        sbx._validated(sbx.Spec(forbidden=[lay.root / "loopdir" / "x"], **base))


def test_dangling_link_in_a_middle_component_fails_closed(lay: Layout) -> None:
    (lay.root / "dang").symlink_to(lay.root / "nowhere")
    base = {"argv": ["/bin/echo"], "export_dir": lay.export, "out_dir": lay.out, "tmp_dir": lay.tmp}
    for kw in ({"forbidden": [lay.root / "dang" / "sub"]}, {"toolchain_ro": [lay.root / "dang" / "sub"]}):
        with pytest.raises(sbx.SandboxError):
            sbx._validated(sbx.Spec(**kw, **base))
    assert sbx._real(lay.root / "missing" / "tail") == sbx._real(lay.root) / "missing" / "tail"
