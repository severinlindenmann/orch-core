"""L8 (#1198 H1): the loopback dashboard listener drops TCP peers whose socket owner is not the person (Linux)."""

import os
import socket
import subprocess
import sys

import pytest

from orch_dashboard import __main__ as launcher
from orch_dashboard.front import LoopbackPeerChecked, _hex_addr, tcp_owner_uid

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnmt   uid  timeout inode\n"


def row(local, remote, uid):
    return f"   0: {local} {remote} 01 00000000:00000000 00:00000000 00000000  {uid}        0 12345 1 0 100 4 0\n"


def listener_and_client(proc_tcp, uid_in_table, platform="linux", person=1000, rows=None):
    """A real accepted loopback pair; the fake table lists the client's socket with ``uid_in_table``."""
    srv = LoopbackPeerChecked(person, platform=platform, proc_tcp=str(proc_tcp))
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    client = socket.create_connection(srv.getsockname())
    cport, sport = client.getsockname()[1], srv.getsockname()[1]
    if rows is None:
        rows = [row(_hex_addr("127.0.0.1", cport), _hex_addr("127.0.0.1", sport), uid_in_table)]
    proc_tcp.write_text(HEADER + "".join(rows))
    return srv, client


def test_peer_with_the_person_uid_is_accepted(tmp_path):
    srv, client = listener_and_client(tmp_path / "tcp", 1000)
    conn, _ = srv.accept()
    conn.close(), client.close(), srv.close()


def test_peer_with_another_uid_is_dropped(tmp_path):
    srv, client = listener_and_client(tmp_path / "tcp", 1234)
    with pytest.raises(ConnectionAbortedError):
        srv.accept()
    assert client.recv(1) == b""  # the server closed it
    client.close(), srv.close()


@pytest.mark.parametrize("kind", ["missing_row", "no_file", "garbage"])
def test_unreadable_peer_uid_fails_closed(tmp_path, kind):
    table = tmp_path / "tcp"
    rows = [row("0100007F:0001", "0100007F:0002", 1000)] if kind == "missing_row" else None
    srv, client = listener_and_client(table, 1000, rows=rows or [])
    if kind == "no_file":
        table.unlink()
    if kind == "garbage":
        table.write_text("not a table\n")
    with pytest.raises(ConnectionAbortedError):
        srv.accept()
    client.close(), srv.close()


def test_only_the_matching_row_counts(tmp_path):
    table = tmp_path / "tcp"
    srv = LoopbackPeerChecked(1000, platform="linux", proc_tcp=str(table))
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    client = socket.create_connection(srv.getsockname())
    conn, _ = socket.socket.accept(srv)
    c, s = client.getsockname()[1], srv.getsockname()[1]
    table.write_text(
        HEADER
        + row(_hex_addr("127.0.0.1", s), _hex_addr("127.0.0.1", c), 1000)  # the server's own end: same pair, swapped
        + row(_hex_addr("127.0.0.1", c), _hex_addr("127.0.0.1", s), 4321)
    )
    assert tcp_owner_uid(conn, str(table)) == 4321
    conn.close(), client.close(), srv.close()


def test_macos_does_not_drop_peers(tmp_path):
    srv, client = listener_and_client(tmp_path / "tcp", 1234, platform="darwin")
    conn, _ = srv.accept()
    conn.close(), client.close(), srv.close()


def test_bind_listener_is_the_checked_listener():
    listener = launcher.bind_listener(0)
    try:
        assert isinstance(listener, LoopbackPeerChecked)
        assert listener.front_uid == os.geteuid()
    finally:
        listener.close()


def test_uvicorn_loop_stays_asyncio_and_both_servers_check_it():
    assert launcher.server_config(lambda *a: None).loop == "asyncio"
    assert launcher.server_config(lambda *a: None, front=True).loop == "asyncio"
    assert issubclass(launcher.FrontServer, launcher.uvicorn.Server)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/net/tcp is Linux only")
def test_real_proc_reports_our_uid_for_our_own_connection():
    srv = launcher.bind_listener(0)
    srv.listen(1)
    client = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()  # real /proc: our own UID passes
    assert tcp_owner_uid(conn) == os.geteuid()
    conn.close(), client.close(), srv.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/net/tcp is Linux only")
def test_real_connection_from_another_process_is_judged_by_its_owner():
    srv = launcher.bind_listener(0)
    srv.listen(1)
    code = "import socket,sys,time;s=socket.create_connection(('127.0.0.1',int(sys.argv[1])));time.sleep(5)"
    child = subprocess.Popen([sys.executable, "-c", code, str(srv.getsockname()[1])])
    try:
        srv.settimeout(5)
        conn, _ = srv.accept()
        assert tcp_owner_uid(conn) == os.geteuid()
        conn.close()
    finally:
        child.kill()
        child.wait()
        srv.close()
