"""L8 (#1198 H1): the loopback dashboard listener drops TCP peers whose socket owner is not the person (Linux)."""

import os
import socket
import struct
import subprocess
import sys

import pytest

from orch_dashboard import __main__ as launcher
from orch_dashboard.front import LoopbackPeerChecked, tcp_owner_uid


def diag_reply(uid, state=1, family=socket.AF_INET, seq=1, kind=20):
    msg = struct.pack("=BBBB", family, state, 0, 0) + bytes(48) + struct.pack("=5I", 0, 0, 0, uid, 99)
    return struct.pack("=IHHII", 16 + len(msg), kind, 0, seq, 0) + msg


class FakeNetlink:
    """Stands for the sock_diag socket: records the request, answers with ``reply`` (or raises it)."""

    def __init__(self, reply):
        self.reply, self.sent, self.closed = reply, [], False

    def send(self, data):
        self.sent.append(data)

    def recv(self, size):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self):
        self.closed = True


def pair(reply, platform="linux", person=1000):
    """A real accepted loopback pair whose peer lookup is answered by a fake sock_diag."""
    nl = FakeNetlink(reply)
    srv = LoopbackPeerChecked(person, platform=platform, netlink=lambda: nl)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, socket.create_connection(srv.getsockname()), nl


def test_peer_with_the_person_uid_is_accepted():
    srv, client, nl = pair(diag_reply(1000))
    conn, _ = srv.accept()
    assert nl.closed
    # The request is an exact 4-tuple lookup (no dump flag), TCP over IPv4, established sockets only.
    length, kind, flags, seq, _ = struct.unpack_from("=IHHII", nl.sent[0])
    assert (length, kind, flags) == (len(nl.sent[0]), 20, 1)
    family, proto, _ext, _pad, states = struct.unpack_from("=BBBBI", nl.sent[0], 16)
    assert (family, proto, states) == (socket.AF_INET, socket.IPPROTO_TCP, 1 << 1)
    sport, dport = struct.unpack_from("!HH", nl.sent[0], 24)
    assert (sport, dport) == (client.getsockname()[1], srv.getsockname()[1])
    conn.close(), client.close(), srv.close()


@pytest.mark.parametrize(
    "reply",
    [
        diag_reply(1234),  # another user
        diag_reply(0, state=6),  # TIME_WAIT entries read uid 0
        diag_reply(1000, state=6),
        diag_reply(1000, family=socket.AF_INET6),
        diag_reply(1000, seq=7),
        diag_reply(1000, kind=2),  # NLMSG_ERROR type
        diag_reply(1000)[:60],  # truncated
        struct.pack("=IHHII", 36, 2, 0, 1, 0) + bytes(20),  # a real error reply: ENOENT / no tcp_diag
        b"",
        OSError("netlink refused"),
    ],
)
def test_everything_but_an_established_socket_of_the_person_is_dropped(reply):
    srv, client, nl = pair(reply)
    with pytest.raises(ConnectionAbortedError):
        srv.accept()
    assert nl.closed and client.recv(1) == b""
    client.close(), srv.close()


def test_a_root_person_does_not_accept_a_time_wait_row_of_uid_0():
    srv, client, _ = pair(diag_reply(0, state=6), person=0)
    with pytest.raises(ConnectionAbortedError):
        srv.accept()
    client.close(), srv.close()


def test_a_missing_sock_diag_fails_closed():
    def no_netlink():
        raise OSError("protocol not supported")

    srv = LoopbackPeerChecked(1000, platform="linux", netlink=no_netlink)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    client = socket.create_connection(srv.getsockname())
    with pytest.raises(ConnectionAbortedError):
        srv.accept()
    client.close(), srv.close()


def test_macos_does_not_drop_peers():
    srv, client, nl = pair(diag_reply(1234), platform="darwin")
    conn, _ = srv.accept()
    assert not nl.sent
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
