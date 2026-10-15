import socket
import threading

import pytest
from devhost.host import StubHost

from orch_dashboard.errors import HostError
from orch_dashboard.hostlink import HostLink


def test_dev_only(p1, monkeypatch):
    monkeypatch.delenv("ORCH_DEV")
    with pytest.raises(RuntimeError):
        StubHost(p1.root, p1.host_state, p1.owner.ref)
    monkeypatch.setenv("ORCH_DEV", "1")
    (p1.root / ".orch-dev").unlink()
    with pytest.raises(RuntimeError):
        StubHost(p1.root, p1.host_state, p1.owner.ref)


def test_p1_registry_reads_no_writes(stub):
    assert stub.call("list", {})["data"]["tickets"][0]["title"] == "Real P1 ticket"
    assert stub.call("addon.list", {})["data"]["addons"][0]["enabled"]
    for op in ["new", "set", "doctor"]:
        with pytest.raises(HostError, match="unknown_command"):
            stub.call(op, {})


def test_socket_reads_and_poll_invalidations(stub, p1):
    import os

    from orch_dashboard.hostlink import receive

    a, b = socket.socketpair()
    thread = threading.Thread(target=stub.serve_connection, args=(b,), daemon=True)
    thread.start()
    link = HostLink(a, os.getuid(), stub.ws)
    try:
        assert link.call("whoami")["person"] == p1.owner.ref
        before = link.call("ticket.query")["sub_cursor"]
        sub = link.call("sub.open", {"topics": ["tickets"], "after": before})
        p1.new_ticket("Added after snapshot")
        event = receive(a)
        assert event["t"] == "evt" and event["sub"] == sub["sub"]
        assert any(i.get("type") == "ticket.created" for i in event["items"])
        assert all("title" not in i for i in event["items"])
        reopened = link.call("sub.open", {"topics": ["tickets"], "after": before})
        assert reopened["sub"].startswith("sub_")
        stub.cursors.pop(before, None)
        with pytest.raises(HostError, match="sub.reset"):
            link.call("sub.open", {"topics": ["tickets"], "after": before})
    finally:
        link.close()
        thread.join(3)
        assert not thread.is_alive()


def test_unmapped_person_refused(p1):
    host = StubHost(p1.root, p1.host_state, "p_not_a_member")
    with pytest.raises(HostError, match="peer.denied"):
        host.call("whoami", {})


def test_restricted_ticket_does_not_leak(stub, p1):
    from orch import crypto

    from tests.store.helpers import Person

    other = Person()
    p1.store.append(
        p1.person_event(
            p1.owner,
            "workspace",
            "member.added",
            person=other.ref,
            name="Other",
            role="member",
            pk_pub=crypto.b64u(other.pk_pub),
            device_cert=other.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )
    uid = next(iter(p1.store.state.tickets))
    p1.store.append(
        p1.person_event(p1.owner, uid, "visibility.changed", visibility={"restricted": [p1.owner.ref]}), log=uid
    )
    assert len(stub.call("ticket.query", {})["data"]["rows"]) == 1
    stranger = StubHost(p1.root, p1.host_state, other.ref)
    assert stranger.call("ticket.query", {})["data"]["rows"] == []
    assert stranger.call("needs", {})["data"]["items"] == []


@pytest.mark.parametrize(
    "case",
    [
        "unset",
        "default",
        "nested_default",
        "unmarked",
        "run_override",
        "socket_override",
        "alias",
        "xdg_default",
        "registry_alias",
    ],
)
def test_isolated_state_required(tmp_path, monkeypatch, case):
    from pathlib import Path

    from devhost.__main__ import dev_directories

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".orch-dev").touch()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
    default = Path.home() / ".config/orch"
    if case == "unset":
        monkeypatch.delenv("ORCH_STATE_DIR")
    elif case in ("default", "nested_default", "alias"):
        default.mkdir(parents=True)
        (default / ".orch-dev").touch()
        target = default
        if case == "nested_default":
            target = default / "child"
            target.mkdir()
            (target / ".orch-dev").touch()
        if case == "alias":
            target = tmp_path / "alias"
            target.symlink_to(default)
        monkeypatch.setenv("ORCH_STATE_DIR", str(target))
    elif case == "unmarked":
        (state / ".orch-dev").unlink()
    elif case == "run_override":
        monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(default / "run"))
    elif case == "xdg_default":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        state.rename(tmp_path / "orch")
        monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch"))
    elif case == "registry_alias":
        (state / "host").symlink_to(default)
    else:
        monkeypatch.setenv("ORCH_HOST_SOCKET", str(default / "run/control/host.sock"))
    with pytest.raises(RuntimeError):
        dev_directories()
    assert not (default / "host/workspaces.json").exists()


def test_isolated_state_accepted(tmp_path, monkeypatch):
    from devhost.__main__ import dev_directories

    (tmp_path / ".orch-dev").touch()
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
    assert dev_directories() == (tmp_path.resolve(), tmp_path.resolve() / "run")


@pytest.mark.parametrize("sig_name", ["SIGTERM", "SIGINT"])
def test_signal_cleanup_without_binding(p1, monkeypatch, sig_name):
    import signal
    import sys
    from unittest.mock import Mock

    from devhost import __main__ as dev

    state = p1.host_state
    state.mkdir(parents=True, exist_ok=True)
    (state / ".orch-dev").touch()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
    monkeypatch.setattr(sys, "argv", ["devhost", str(p1.root), "--person", p1.owner.ref])
    sockets = []
    previous = signal.getsignal(getattr(signal, sig_name))

    def serve(hosts, run, opened, stopped):
        for name in ("control", hosts[0].ws):
            path = run / name / "host.sock"
            path.parent.mkdir(parents=True)
            path.touch()
            sock = Mock()
            opened.append((sock, path))
            sockets.append((sock, path))
        signal.getsignal(getattr(signal, sig_name))(getattr(signal, sig_name), None)
        assert stopped.is_set()

    monkeypatch.setattr(dev, "run_stub", serve)
    dev.main()
    for sock, path in sockets:
        sock.close.assert_called_once()
        assert not path.exists()
    assert signal.getsignal(getattr(signal, sig_name)) == previous


def test_poll_gone_and_overflow(stub, p1):
    from orch import crypto

    from tests.store.helpers import Person

    other = Person()
    p1.store.append(
        p1.person_event(
            p1.owner,
            "workspace",
            "member.added",
            person=other.ref,
            name="Other",
            role="member",
            pk_pub=crypto.b64u(other.pk_pub),
            device_cert=other.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )
    viewer = StubHost(p1.root, p1.host_state, other.ref)
    cursor = viewer.call("ticket.query", {})["data"]["sub_cursor"]
    uid = next(iter(p1.store.state.tickets))
    p1.store.append(
        p1.person_event(p1.owner, uid, "visibility.changed", visibility={"restricted": [p1.owner.ref]}), log=uid
    )
    items, end = viewer.pending(cursor, ["tickets", "needs"])
    assert end is None
    assert any(i.get("gone") and i["uid"] == uid for i in items)
    assert not any(i.get("key") for i in items)
    # Bound the dev journal delivery even when many log writes happen between polls.
    before = viewer.cursor
    pos = len(viewer.history)
    viewer.journal.extend(
        (pos + n, [{"id": f"{pos + n}.n", "jpos": pos + n, "type": "needs.changed"}]) for n in range(1, 258)
    )
    assert viewer.pending(before, ["needs"]) == ([], "overflow")
    viewer.cursors.clear()
    assert viewer.pending(before, ["needs"]) == ([], "reset")
