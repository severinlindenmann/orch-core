"""V3 D78 (#1102): the hub reachability probe is bounded, cached, and never raises."""

from __future__ import annotations

import socket

from orch.host import hublinkprobe as P


def test_no_address_or_a_malformed_one_is_unknown_not_down():
    P.clear()
    assert P.reachable("s", None) is None and P.reachable("s", "not an address") is None


def test_answer_is_cached_for_a_few_seconds_then_asked_again():
    P.clear()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    addr = f"127.0.0.1:{srv.getsockname()[1]}"
    assert P.reachable("s", addr, now=100.0) is True
    srv.close()
    assert P.reachable("s", addr, now=101.0) is True  # cached
    assert P.reachable("s", addr, now=100.0 + P.CACHE_S + 0.1) is False  # asked again: nobody listens
    assert P.reachable("other", addr, now=100.0 + P.CACHE_S + 0.2) is False  # per workspace scope


def test_link_view_only_changes_a_bound_link_that_does_not_answer():
    assert P.link_view("bound", False) == "unreachable"
    assert P.link_view("bound", True) == P.link_view("bound", None) == "bound"
    assert P.link_view("joining", False) == "joining" and P.link_view(None, False) is None


def test_peer_status_fields_count_the_outbox_and_name_the_last_loop_error(monkeypatch):
    from types import SimpleNamespace

    from orch.host import peerops

    svc = SimpleNamespace(state_dir="/s", workspace_id="w")
    rt = SimpleNamespace(svc=SimpleNamespace(mb=SimpleNamespace(outbox={"a": "x", "b": "y"})), last_error="PinMismatch")
    monkeypatch.setattr(peerops, "_RUNTIMES", {peerops._key(svc): rt})
    assert peerops.status_fields(svc) == {"peer_outbox": 2, "link_error": "PinMismatch"}
    monkeypatch.setattr(peerops, "_RUNTIMES", {})
    monkeypatch.setattr(peerops, "_load_box", lambda s: (_ for _ in ()).throw(OSError("unreadable")))
    assert peerops.status_fields(svc) == {"peer_outbox": 0, "link_error": None}  # status always answers
