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
    for op in ["human.prepare", "human.submit", "new", "set", "doctor"]:
        with pytest.raises(HostError, match="op.unsupported"):
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
        assert event == {"t": "evt", "sub": sub["sub"], "invalidated": "overflow"}
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
