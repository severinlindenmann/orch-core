"""dev-pair's pure parts: the origin allow-list, the refusals, the root and its manifest, the allow-list
environment, the code FIFO, and the pseudo-terminal driver (orch-v2-host 11A.15.11)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch import devsandbox


@pytest.fixture(autouse=True)
def _scratch_is_below_tmp(dp, monkeypatch, request):
    """The scratch roots live below the real ``/tmp`` (short paths), which the host's run-directory rule refuses; only
    the test of that rule sees it."""
    if "sticky" not in request.node.name:
        monkeypatch.setattr(dp, "sticky_problem", lambda parent: None)


def test_a_root_below_a_sticky_directory_is_refused_early(dp, scratch):
    assert "sticky" in dp.sticky_problem(scratch)  # a directory of ours directly below /tmp
    with pytest.raises(dp.Refused, match="sticky"):
        dp.check_root_path(str(scratch / "sb"))
    assert dp.sticky_problem(Path("/usr")) is None  # root-owned, not sticky, present on macOS and Linux


# -- the origin allow-list and the refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8123", "http://localhost:9", "https://relay.dev.severin.io"])
def test_allowed_origins(dp, origin):
    assert dp.check_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://relay.severin.io", "https://relay.dev.severin.io/", "https://relay.dev.severin.io:443",
        "http://relay.dev.severin.io", "http://127.0.0.1", "http://127.0.0.1:99999", "https://127.0.0.1:8123",
        "http://[::1]:8000", "http://evil.example:80", "http://127.0.0.1:80/x", "http://user@127.0.0.1:80",
        "https://tix.severin.io", "", None,
    ],
)  # fmt: skip
def test_refused_origins(dp, origin):
    with pytest.raises(dp.Refused):
        dp.check_origin(origin)


def test_refuses_under_orch_grant_and_as_root(dp):
    dp.refuse_environment({"PATH": "/bin"}, 501)
    with pytest.raises(dp.Refused, match="ORCH_GRANT"):
        dp.refuse_environment({"ORCH_GRANT": "gr_x"}, 501)
    with pytest.raises(dp.Refused, match="root"):
        dp.refuse_environment({}, 0)


def test_main_refuses_without_touching_anything(dp, scratch, monkeypatch):
    root = scratch / "r"
    monkeypatch.setenv("ORCH_GRANT", "gr_x")
    assert dp.main(["up", "--root", str(root)]) == 2
    monkeypatch.delenv("ORCH_GRANT")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert dp.main(["up", "--root", str(root)]) == 2
    monkeypatch.undo()
    assert dp.main(["up", "--root", str(root), "--relay", "https://evil.example"]) == 2
    assert dp.main(["up", "--root", str(root), "--scopes", "operate", "--expect-code-from", str(root / "f")]) == 2
    assert dp.main(["down", "--root", str(root)]) == 2  # not a sandbox
    assert not root.exists()


def test_json_fd_must_be_a_pipe_or_socket(dp, scratch, monkeypatch):
    f = scratch / "plain"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)
    closed = os.dup(fd)
    os.close(closed)  # a number that was open a moment ago and is not now (a fixed 99 may be a live pipe under load)
    try:
        for bad in (fd, 1, closed):
            argv = [
                "up",
                "--root",
                str(scratch / "r"),
                "--json-fd",
                str(bad),
                "--expect-code-from",
                str(scratch / "r/f"),
            ]
            assert dp.main(argv) == 2
    finally:
        os.close(fd)
    assert not (scratch / "r").exists()


# -- the root -------------------------------------------------------------------------------------------------------


def test_root_path_rules(dp, scratch):
    ok = dp.check_root_path(str(scratch / "sb"))
    assert ok == scratch / "sb"
    for bad in ("sb", "./sb", str(scratch / ".." / scratch.name / "x"), str(scratch / "a//b"), "/", ""):
        with pytest.raises(dp.Refused):
            dp.check_root_path(bad)
    with pytest.raises(dp.Refused, match="does not exist"):
        dp.check_root_path(str(scratch / "nope" / "sb"))


def test_parent_must_be_private_and_the_path_free_of_links(dp, scratch):
    os.chmod(scratch, 0o770)
    with pytest.raises(dp.Refused, match="group- or world-writable"):
        dp.check_root_path(str(scratch / "sb"))
    os.chmod(scratch, 0o700)
    real = scratch / "real"
    real.mkdir(mode=0o700)
    (scratch / "link").symlink_to(real)
    with pytest.raises(dp.Refused, match="symbolic link"):
        dp.check_root_path(str(scratch / "link" / "sb"))
    with pytest.raises(dp.Refused, match="owned by you"):
        dp.check_root_path(str(real / "sb"), uid=os.getuid() + 1)


def test_a_root_too_long_for_the_socket_limit_is_refused(dp, scratch):
    deep = scratch
    while len(str(deep)) < 60:
        deep = deep / ("d" * 10)
        deep.mkdir(mode=0o700)
    assert not dp.socket_path_ok(deep / "sb")
    with pytest.raises(dp.Refused, match="too long"):
        dp.check_root_path(str(deep / "sb"))


def test_the_root_is_created_exclusively_and_pinned(dp, scratch):
    existing = scratch / "exists"
    existing.mkdir(mode=0o700)
    (existing / "keep").write_text("real data")
    with pytest.raises(dp.Refused, match="never adopts"):
        dp.create_root(existing)
    assert (existing / "keep").read_text() == "real data" and not (existing / dp.MANIFEST).exists()
    empty = scratch / "empty"
    empty.mkdir(mode=0o700)
    with pytest.raises(dp.Refused, match="never adopts"):
        dp.create_root(empty)  # an empty one too
    sb = dp.create_root(scratch / "new")
    try:
        assert (os.stat(sb.path).st_mode & 0o777) == 0o700
        m = devsandbox.check_manifest(sb.path)  # the shared check production code uses
        st = os.stat(sb.path)
        assert m["id"] == sb.sid and m["root"] == {"st_dev": st.st_dev, "st_ino": st.st_ino}
        assert (os.stat(sb.path / dp.MANIFEST).st_mode & 0o777) == 0o600
        sb.verify()
    finally:
        sb.close()


def test_a_swapped_root_is_detected(dp, scratch):
    sb = dp.create_root(scratch / "sb")
    try:
        os.rename(sb.path, scratch / "moved")
        (scratch / "sb").mkdir(mode=0o700)
        with pytest.raises(dp.Refused, match="replaced"):
            sb.verify()
    finally:
        sb.close()


def test_secret_files_are_0600_below_the_pinned_root(dp, scratch):
    sb = dp.create_root(scratch / "sb")
    try:
        sb.mkdir("secrets")
        sb.write("secrets/passphrase", b"w1 w2")
        sb.write("pair.json", b"{}")
        assert (os.stat(sb.path / "secrets" / "passphrase").st_mode & 0o777) == 0o600
        assert sb.read("secrets/passphrase") == b"w1 w2"
        with pytest.raises(FileExistsError):
            sb.write("pair.json", b"again")  # exclusive: never overwritten
        with pytest.raises(dp.Refused):
            sb.write("../escape", b"x")
        os.chmod(sb.path / "secrets" / "passphrase", 0o644)
        with pytest.raises(dp.Refused, match="0600"):
            sb.read("secrets/passphrase")
    finally:
        sb.close()


def test_manifest_update_keeps_identity_and_extras(dp, scratch):
    sb = dp.create_root(scratch / "sb")
    try:
        dp.manifest_update(sb.path, person_id="p_" + "ab" * 16)
        dp.manifest_add(sb.path, "processes", {"role": "x", "pid": 5})
        dp.manifest_add(sb.path, "processes", {"role": "y", "pid": 6})
        m = devsandbox.check_manifest(sb.path)
        assert m["person_id"] == "p_" + "ab" * 16 and [p["pid"] for p in m["processes"]] == [5, 6]
        assert m["id"] == sb.sid
    finally:
        sb.close()


# -- the environment ------------------------------------------------------------------------------------------------


def test_child_env_is_an_allow_list_of_sandbox_paths(dp, scratch, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("ORCH_GRANT", "gr_x")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/real")
    root = scratch / "sb"
    env = dp.child_env(root, "/usr/bin:/bin")
    assert set(env) == {"PATH", "HOME", "ORCH_STATE_DIR", "ORCH_HOST_RUN_DIR", "LANG", "TMPDIR", "ORCH_NO_CONVENIENCE"}
    assert env["HOME"] == f"{root}/home" and env["ORCH_STATE_DIR"] == f"{root}/state"
    assert env["ORCH_HOST_RUN_DIR"] == f"{root}/run" and env["ORCH_NO_CONVENIENCE"] == "1"
    assert "ORCH_NO_CONVENIENCE" not in dp.child_env(root, "/bin", no_convenience=False)
    assert dp.child_env(root, "/bin", term="xterm")["TERM"] == "xterm"


def test_relay_env_passes_only_caches_and_its_own_variables(dp, scratch):
    parent = {"UV_CACHE_DIR": "/c", "ORCH_STATE_DIR": "/real", "HOME": "/Users/x", "ORCH_RELAY_DATA": "/other",
              "ORCH_GRANT": "gr", "SSH_AUTH_SOCK": "/s"}  # fmt: skip
    env = dp.relay_env(scratch / "sb", "/bin", 8123, "i" * 8, parent)
    assert env["UV_CACHE_DIR"] == "/c" and env["HOME"] == f"{scratch}/sb/home"
    assert env["ORCH_RELAY_ORIGIN"] == "http://127.0.0.1:8123" and env["ORCH_RELAY_DATA"] == f"{scratch}/sb/relay-data"
    assert env["ORCH_RELAY_MAX_PUBLICATION_BYTES"] == str(4 * 1024 * 1024)
    assert not {"ORCH_STATE_DIR", "ORCH_GRANT", "SSH_AUTH_SOCK"} & set(env)


# -- the codes ------------------------------------------------------------------------------------------------------


OFFER = "ab" * 16


def test_code_lines_are_bound_to_offer_and_phase(dp):
    assert dp.parse_code_line(f"{OFFER} sas k7m2qx\n") == (OFFER, "sas", "K7M2QX")
    for bad in (f"{OFFER} other K7M2QX", f"{OFFER[:-1]} sas K7M2QX", f"{OFFER} sas K7M2Q", f"{OFFER} sas K7M2QX extra",
                "sas K7M2QX", ""):  # fmt: skip
        assert dp.parse_code_line(bad) is None


def test_fifo_must_be_a_fifo_of_this_user_directly_under_the_root(dp, scratch):
    root = scratch / "r"
    root.mkdir(mode=0o700)
    plain = root / "plain"
    plain.write_text("")
    with pytest.raises(dp.Refused, match="not a FIFO"):
        dp.CodeFifo(plain, root)
    with pytest.raises(dp.Refused):
        dp.check_fifo(str(scratch / "elsewhere"), root)
    with pytest.raises(dp.Refused):
        dp.check_fifo(str(root / "sub" / "f"), root)
    with pytest.raises(dp.Refused):
        dp.check_fifo(str(root / dp.MANIFEST), root)
    os.mkfifo(root / "f", 0o600)
    dp.CodeFifo(root / "f", root).close()
    (root / "ln").symlink_to(root / "f")
    with pytest.raises(OSError):
        dp.CodeFifo(root / "ln", root)  # O_NOFOLLOW


def test_stale_and_wrong_phase_codes_are_not_typed(dp, scratch):
    root = scratch / "r"
    root.mkdir(mode=0o700)
    os.mkfifo(root / "f", 0o600)
    fifo = dp.CodeFifo(root / "f", root)
    wfd = os.open(root / "f", os.O_WRONLY | os.O_NONBLOCK)
    other = "cd" * 16
    try:
        for line in (f"{other} sas AAAAAA", f"{OFFER} cert_code BBBBBB", "garbage", f"{OFFER} sas CCCCCC", ""):
            os.write(wfd, line.encode() + b"\n")
        assert fifo.take(OFFER, "sas") == "CCCCCC"  # not the other offer's, not the cert_code line
        assert fifo.take(OFFER, "sas") is None  # consumed
        assert fifo.take(OFFER, "cert_code") == "BBBBBB"  # kept for its own phase
        assert fifo.dropped >= 2
        assert fifo.get(OFFER, "cert_code", 0.2, sleep=lambda s: time.sleep(0.05)) is None  # a wait times out
    finally:
        os.close(wfd)
        fifo.close()


# -- recorded processes ---------------------------------------------------------------------------------------------


def spawn(root: Path, code: str = "import time; time.sleep(60)") -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=root, stdin=subprocess.DEVNULL)
    import dev_pair  # the module the fixture loaded

    dev_pair.stable_identity(proc.pid)  # past the pre-exec image
    return proc


def test_a_recorded_process_validates_and_a_reused_pid_does_not(dp, scratch):
    root = scratch / "r"
    root.mkdir(mode=0o700)
    p = spawn(root)
    try:
        ident = dp.stable_identity(p.pid)
        assert ident is not None
        rec = {"role": "t", **ident}
        real = os.path.realpath(root)
        assert dp.record_mismatch(rec, real) is None, (dp.proc_identity(p.pid), rec)
        assert not dp.validate_record({**rec, "start": "Thu Jan  1 00:00:00 1970"}, real)  # another birth: PID reuse
        assert not dp.validate_record({**rec, "exe": "/bin/sleep"}, real)
        assert not dp.validate_record(rec, scratch / "other")  # working directory not inside the root
        assert not dp.validate_record({**rec, "pid": 1}, real)  # never init
        assert not dp.validate_record({"pid": "x"}, real) and not dp.validate_record({}, real)
        assert not dp.validate_record(rec, real, inspect=lambda pid: None)  # unreadable: fail closed
    finally:
        p.kill()
        p.wait()
    assert dp.proc_identity(p.pid) is None


def fake_table(procs):
    """An inspect/kill pair over a dict {pid: identity}; kill removes the process unless it is 'stubborn'."""
    sent: list[tuple[int, int]] = []

    def inspect(pid):
        return procs.get(pid)

    def kill(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL or not procs[pid].get("stubborn"):
            procs.pop(pid)

    return inspect, kill, sent


def test_down_signals_term_then_kill_and_only_validated_processes(dp, scratch):
    root = os.path.realpath(scratch)
    mine = {"pid": 4001, "start": "s1", "exe": "/x/relay", "cwd": f"{root}/work"}
    stubborn = {"pid": 4002, "start": "s2", "exe": "/x/host", "cwd": f"{root}", "stubborn": True}
    reused = {"pid": 4003, "start": "NEW", "exe": "/usr/bin/vim", "cwd": "/Users/x"}  # the recorded pid, reused
    gone = None
    procs = {4001: mine, 4002: stubborn, 4003: reused}
    inspect, kill, sent = fake_table(procs)
    recs = [
        {"role": "relay", "pid": 4001, "start": "s1", "exe": "/x/relay", "cwd": f"{root}/work"},
        {"role": "host", "pid": 4002, "start": "s2", "exe": "/x/host", "cwd": root},
        {"role": "old", "pid": 4003, "start": "OLD", "exe": "/x/old", "cwd": root},
        {"role": "dead", "pid": 4004, "start": "s", "exe": "/x/d", "cwd": root},
        {"role": "bad", "pid": "x"},
    ]
    out = dp.stop_recorded(
        recs,
        root,
        grace=0.2,
        inspect=inspect,
        kill=kill,
        sleep=lambda s: time.sleep(0.02),
        exists=lambda pid: pid in procs,
    )
    assert out == {"bad:x": "invalid", "dead:4004": "gone", "old:4003": "not-ours", "host:4002": "killed",
                   "relay:4001": "terminated"}  # fmt: skip
    assert sent == [(4002, signal.SIGTERM), (4002, signal.SIGKILL), (4001, signal.SIGTERM)]  # reverse start order
    assert 4003 in procs and gone is None  # the reused pid was never signalled


def test_stop_recorded_ends_real_processes(dp, scratch):
    root = scratch / "r"
    root.mkdir(mode=0o700)
    polite = spawn(root)
    stubborn = spawn(root, "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    try:
        time.sleep(0.5)  # let the stubborn one install its handler
        recs = [
            {"role": "polite", **dp.stable_identity(polite.pid)},
            {"role": "stubborn", **dp.stable_identity(stubborn.pid)},
        ]
        reap = lambda s: (polite.poll(), stubborn.poll(), time.sleep(0.05))  # noqa: E731
        out = dp.stop_recorded(recs, os.path.realpath(root), grace=1.0, sleep=reap)
        assert out == {f"stubborn:{stubborn.pid}": "killed", f"polite:{polite.pid}": "terminated"}
        assert polite.wait(5) == -signal.SIGTERM and stubborn.wait(5) == -signal.SIGKILL
    finally:
        for p in (polite, stubborn):
            p.kill()
            p.wait()


# -- down: the ownership rules ----------------------------------------------------------------------------------------


def built(dp, scratch, name="sb"):
    sb = dp.create_root(scratch / name)
    for d in ("home", "state", "secrets"):
        sb.mkdir(d)
    sb.write("secrets/passphrase", b"x")
    dp.manifest_update(sb.path, entries=["home", "state", "secrets"])
    return sb


def test_down_removes_the_root_and_stops_what_it_recorded(dp, scratch):
    sb = built(dp, scratch)
    p = spawn(sb.path)
    try:
        dp.manifest_add(sb.path, "processes", {"role": "host", **dp.stable_identity(p.pid)})
        (sb.path / "home" / "deep").mkdir()
        (sb.path / "home" / "deep" / "f").write_text("data")
        (sb.path / "state" / "link").symlink_to("/etc/hosts")  # a link is removed, never followed
        reap = lambda s: p.poll()  # noqa: E731
        lines: list[str] = []
        assert dp.down(str(sb.path), grace=2.0, out=lines.append) in (0,)
        assert not sb.path.exists() and Path("/etc/hosts").exists()
        assert any("terminated" in ln or "killed" in ln for ln in lines)
        del reap
        assert p.wait(5) == -signal.SIGTERM
    finally:
        p.kill()
        p.wait()
        sb.close()


def test_down_keep_stops_processes_and_keeps_the_root(dp, scratch):
    sb = built(dp, scratch)
    p = spawn(sb.path)
    try:
        dp.manifest_add(sb.path, "processes", {"role": "host", **dp.stable_identity(p.pid)})
        assert dp.down(str(sb.path), keep=True, grace=2.0, out=lambda s: None) == 0
        assert sb.path.exists() and (sb.path / "secrets" / "passphrase").exists()
        assert p.wait(5) == -signal.SIGTERM
    finally:
        p.kill()
        p.wait()
        sb.close()


def test_down_with_a_copied_manifest_deletes_nothing(dp, scratch):
    real = built(dp, scratch, "real")
    p = spawn(real.path)
    other = scratch / "other"
    other.mkdir(mode=0o700)
    (other / "valuable").write_text("mine")
    (other / dp.MANIFEST).write_bytes((real.path / dp.MANIFEST).read_bytes())  # forged: copied from another root
    os.chmod(other / dp.MANIFEST, 0o600)
    try:
        dp.manifest_add(real.path, "processes", {"role": "host", **dp.stable_identity(p.pid)})
        (other / dp.MANIFEST).write_bytes((real.path / dp.MANIFEST).read_bytes())
        with pytest.raises(dp.Refused, match="manifest"):
            dp.down(str(other), out=lambda s: None)
        assert (other / "valuable").read_text() == "mine"
        assert p.poll() is None  # the recorded process of the OTHER root's manifest was not signalled either
    finally:
        p.kill()
        p.wait()
        real.close()


def test_down_refuses_what_is_not_a_sandbox(dp, scratch):
    plain = scratch / "plain"
    plain.mkdir(mode=0o700)
    (plain / "data").write_text("x")
    with pytest.raises(dp.Refused):
        dp.down(str(plain), out=lambda s: None)
    with pytest.raises(dp.Refused):
        dp.down(str(scratch / "missing"), out=lambda s: None)
    link = scratch / "link"
    sb = built(dp, scratch)
    link.symlink_to(sb.path)
    with pytest.raises(dp.Refused):
        dp.down(str(link), out=lambda s: None)  # a symlinked root
    assert (sb.path / "secrets" / "passphrase").exists()
    sb.close()


def test_down_refuses_entries_it_did_not_create(dp, scratch):
    sb = built(dp, scratch)
    (sb.path / "mine").write_text("not dev-pair's")
    with pytest.raises(dp.Refused, match="did not create"):
        dp.down(str(sb.path), out=lambda s: None)
    assert (sb.path / "mine").exists() and (sb.path / "secrets" / "passphrase").exists()
    sb.close()


def test_remove_tree_stops_at_foreign_files(dp, scratch, monkeypatch):
    sb = built(dp, scratch)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(sb.path).st_uid + 1)
    with pytest.raises(dp.Refused, match="not owned by you"):
        dp.remove_tree(sb.fd, os.fstat(sb.fd).st_dev)
    monkeypatch.undo()
    assert (sb.path / "secrets" / "passphrase").exists()  # the refusal came before anything under the root went
    sb.close()


def test_a_host_already_listening_on_the_root_is_refused(dp, scratch):
    root = scratch / "r"
    (root / "run" / "control").mkdir(parents=True, mode=0o700)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(root / "run" / "control" / "host.sock"))
    srv.listen(10)
    try:
        assert dp.host_listening(root)
        ctx = SimpleNamespace(root=root, args=Namespace(sim_convenience=False))
        with pytest.raises(dp.Refused, match="already running"):
            dp.step_host(ctx)
    finally:
        srv.close()
    assert not dp.host_listening(root)


# -- the pseudo-terminal driver ------------------------------------------------------------

ASK = (
    "import os, sys\n"
    "fd = os.open('/dev/tty', os.O_RDWR)\n"
    "os.write(fd, b'Passphrase: ')\n"
    "line = b''\n"
    "while not line.endswith(b'\\n'):\n"
    "    line += os.read(fd, 1)\n"
    "open(sys.argv[1], 'w').write(line.decode().strip())\n"
)


def test_pty_driver_answers_a_tty_prompt_and_keeps_the_transcript_private(dp, scratch, capsys):
    out = scratch / "answer"
    seen = []
    rc, transcript = dp.run_pty(
        [sys.executable, "-c", ASK, str(out)], env={"PATH": "/usr/bin:/bin"}, cwd=scratch,
        responders=[dp.Responder(__import__("re").compile(r"Passphrase: "), lambda m: seen.append(1) or "hunter two")],
        timeout=30,
    )  # fmt: skip
    assert rc == 0 and out.read_text() == "hunter two" and seen == [1]
    assert b"Passphrase: " in transcript
    cap = capsys.readouterr()
    assert "hunter" not in cap.out + cap.err and "Passphrase" not in cap.out + cap.err


def test_pty_driver_kills_a_stuck_child_on_timeout(dp, scratch):
    marker = scratch / "pid"
    code = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    with pytest.raises(dp.Refused, match="timed out"):
        dp.run_pty([sys.executable, "-c", code], env={"PATH": "/usr/bin:/bin"}, cwd=scratch, responders=[], timeout=1.5)
    pid = int(marker.read_text())
    time.sleep(0.3)
    assert dp.proc_identity(pid) is None  # our own direct child, killed and reaped


def test_pty_driver_reports_the_exit_code_and_passes_a_descriptor(dp, scratch):
    r, w = os.pipe()
    got: list[bytes] = []
    code = f'import os; os.write({w}, b\'{{"state": "open"}}\\n\'); raise SystemExit(3)'
    rc, _ = dp.run_pty([sys.executable, "-c", code], env={"PATH": "/usr/bin:/bin"}, cwd=scratch, responders=[],
                       timeout=30, pass_fds=(w,), watch=(r,), on_watch=lambda fd, d: got.append(d))  # fmt: skip
    os.close(r)
    os.close(w)
    assert rc == 3 and b"".join(got) == b'{"state": "open"}\n'


def test_a_transient_inspect_failure_is_not_gone(dp, scratch):
    root = os.path.realpath(scratch)
    ident = {"pid": 5001, "start": "s", "exe": "/x/h", "cwd": root}
    calls = {"n": 0}

    def flaky(pid):
        calls["n"] += 1
        return None if calls["n"] <= 2 else ident  # ps/lsof fails twice, then answers

    sent = []
    rec = {"role": "host", "pid": 5001, "start": "s", "exe": "/x/h", "cwd": root}

    def kill(pid, sig):
        sent.append(sig)
        ident_dead[0] = True

    ident_dead = [False]
    out = dp.stop_recorded([rec], root, grace=0.1, inspect=lambda p: None if ident_dead[0] else flaky(p), kill=kill,
                           sleep=lambda s: None, exists=lambda p: not ident_dead[0])  # fmt: skip
    assert out == {"host:5001": "terminated"} and sent == [signal.SIGTERM]
    # never inspectable but the pid exists: not gone, nothing signalled
    out = dp.stop_recorded([rec], root, inspect=lambda p: None, kill=lambda p, s: sent.append(99),
                           sleep=lambda s: None, exists=lambda p: True)  # fmt: skip
    assert out == {"host:5001": "unidentified"} and 99 not in sent


def test_down_keeps_the_root_when_a_record_is_not_stopped(dp, scratch):
    sb = built(dp, scratch)
    dp.manifest_add(
        sb.path, "processes", {"role": "host", "pid": os.getpid(), "start": "other", "exe": "/x", "cwd": "/"}
    )
    lines = []
    assert dp.down(str(sb.path), grace=0.1, out=lines.append, inspect=dp.proc_identity) == 1
    assert (sb.path / "secrets" / "passphrase").exists() and any("kept" in ln for ln in lines)
    sb.close()


def test_down_reports_a_removal_error_instead_of_a_traceback(dp, scratch, monkeypatch):
    sb = built(dp, scratch)

    def boom(*a, **k):
        raise OSError(66, "Directory not empty")

    monkeypatch.setattr(dp, "remove_tree", boom)
    lines = []
    assert dp.down(str(sb.path), out=lines.append) == 1
    assert any("could not remove" in ln for ln in lines) and sb.path.exists()
    sb.close()


@pytest.mark.parametrize(
    "prompt",
    [
        "Press Enter to sign, any other key cancels.\n",
        "Press Enter to sign, d for details, any other key cancels.\n",
        "Press Enter to continue, any other key cancels.\n",
    ],
)
def test_review_prompts_are_answered_with_enter_alone(dp, monkeypatch, prompt):
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)
    ctx = SimpleNamespace(passphrase="pw")
    typed_back = [r.fn(m) for r in dp.passphrase_responders(ctx) if (m := r.pattern.search(prompt))]
    assert typed_back == [""]  # run_pty adds the Enter


def test_store_dir_follows_the_orch_subfolder(dp, scratch):
    old, new = scratch / "old", scratch / "new"
    (old / "events").mkdir(parents=True)
    (new / ".orch" / "events").mkdir(parents=True)
    assert dp.store_dir(old) == old
    assert dp.store_dir(new) == new / ".orch"
