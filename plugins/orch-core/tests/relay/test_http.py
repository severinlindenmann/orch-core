"""``orch.net.http`` (11A.7): stdlib transport with verified TLS, a total monotonic deadline per call, bounded bodies,
no redirects, no environment proxies, and redaction. Fault injection against the loopback mock relay."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time

import pytest

from orch.net import http

from .mock import Expect, MockRelay, Reply, json_reply, make_tls


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    m.close()


def conn(m: MockRelay, **kw) -> http.RelayConn:
    return http.RelayConn(m.origin(), **kw)


def call(c, method="POST", target="/v2/x", *, body=b"{}", cap=16 * 1024, deadline_s=5.0, headers=None):
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    return c.request(method, target, route=f"{method} /v2/x", headers=hdrs, body=body, deadline_s=deadline_s, cap=cap)


def test_round_trip_keeps_the_connection_alive(relay):
    relay.expect(
        Expect("POST", "/v2/x", body=b'{"a":1}', reply=json_reply(200, {"ok": 1})),
        Expect("GET", "/v2/y", body=b"", reply=json_reply(200, {"ok": 2})),
    )
    c = conn(relay)
    r = call(c, body=b'{"a":1}')
    assert (r.status, r.body, r.headers["content-type"]) == (200, b'{"ok":1}', "application/json")
    r2 = c.request("GET", "/v2/y", route="GET /v2/y", headers={}, body=None, deadline_s=5, cap=1024)
    assert r2.body == b'{"ok":2}'
    assert relay.errors == []
    assert "content-length" not in relay.seen[1].headers  # GET has no body
    c.close()


def test_request_bytes_are_sent_exactly(relay):
    body = b'{"b":2,"a":[1,true,null]}'
    relay.expect(Expect("PUT", "/v2/x", headers={"Content-Type": "application/json"}, body=body))
    call(conn(relay), "PUT", body=body)
    assert relay.errors == [] and relay.seen[0].body == body


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused_never_followed(relay, status):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(status=status, headers={"Location": "https://evil.example/"})))
    with pytest.raises(http.RedirectRefused) as e:
        call(conn(relay))
    assert e.value.transient is False and e.value.kind == "redirect"
    assert len(relay.seen) == 1


def test_body_over_cap_by_content_length_is_refused(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b"{" + b" " * 2000 + b"}")))
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), cap=1024)
    assert e.value.kind == "too_large" and e.value.transient


def test_body_over_cap_by_chunked_framing_is_refused(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b" " * 100_000, fault="chunked")))
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), cap=50_000)
    assert e.value.kind == "too_large"


def test_body_exactly_at_cap_is_accepted(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b" " * 1024, fault="chunked")))
    assert len(call(conn(relay), cap=1024).body) == 1024


def test_truncated_body_is_transient(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b'{"a":', fault="truncate")))
    with pytest.raises(http.TransportError) as e:
        call(conn(relay))
    assert e.value.kind in ("truncated", "protocol") and e.value.transient


def test_connection_reopened_after_an_error(relay):
    relay.expect(
        Expect("POST", "/v2/x", reply=Reply(body=b"{}", fault="truncate")),
        Expect("POST", "/v2/x", reply=json_reply(200, {})),
    )
    c = conn(relay)
    with pytest.raises(http.TransportError):
        call(c)
    assert call(c).status == 200


def test_slow_drip_headers_end_at_the_total_deadline(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b"{}", fault="drip_headers", drip_s=0.05)))
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), deadline_s=0.5)
    assert e.value.kind == "deadline" and e.value.transient
    assert time.monotonic() - t0 < 4.0  # the drip alone would take several seconds


def test_slow_drip_body_ends_at_the_total_deadline(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b"{" + b" " * 200 + b"}", fault="drip_body", drip_s=0.05)))
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), deadline_s=0.5)
    assert e.value.kind == "deadline"
    assert time.monotonic() - t0 < 4.0


def test_hanging_relay_ends_at_the_total_deadline(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(fault="hang")))
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), deadline_s=0.3)
    assert e.value.kind == "deadline"
    assert time.monotonic() - t0 < 4.0


def test_deadline_uses_the_injected_monotonic_clock(relay):
    """An already-spent deadline never touches the network."""
    now = [100.0]
    c = http.RelayConn(relay.origin(), mono=lambda: now[0])
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=0)
    assert e.value.kind == "deadline" and relay.seen == []


def test_connection_refused_is_transient():
    from .mock import free_port

    c = http.RelayConn(f"http://127.0.0.1:{free_port()}")
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=2)
    assert e.value.kind == "connect" and e.value.transient


def test_shutdown_from_another_thread_ends_a_blocked_call(relay):
    relay.expect(Expect("POST", "/v2/x", reply=Reply(fault="hang")))
    c = conn(relay)
    threading.Timer(0.3, c.shutdown).start()
    t0 = time.monotonic()
    with pytest.raises(http.TransportError):
        call(c, deadline_s=20)
    assert time.monotonic() - t0 < 10.0


def test_status_and_headers_are_returned_for_refusals(relay):
    relay.expect(Expect("POST", "/v2/x", reply=json_reply(429, {"error": "rate_limited"}, {"Retry-After": "20"})))
    r = call(conn(relay))
    assert r.status == 429 and r.headers["retry-after"] == "20"


def test_environment_proxies_are_ignored(relay, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    relay.expect(Expect("POST", "/v2/x"))
    assert call(conn(relay)).status == 200


def test_request_header_injection_is_refused(relay):
    with pytest.raises(http.TransportError):
        call(conn(relay), headers={"Authorization": "Bearer a\r\nX-Evil: 1"})
    assert relay.seen == []


# --- TLS ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def tls_relay(tmp_path):
    ctx, ca = make_tls(tmp_path)
    m = MockRelay(tls=ctx)
    m.ca_file = ca
    yield m
    m.close()


def test_tls_with_the_named_ca_file(tls_relay):
    tls_relay.expect(Expect("POST", "/v2/x"))
    c = http.RelayConn(tls_relay.origin("localhost"), ca_file=str(tls_relay.ca_file))
    assert call(c).status == 200


def test_tls_untrusted_certificate_is_refused_never_downgraded(tls_relay):
    c = http.RelayConn(tls_relay.origin("localhost"))  # default trust store: the test CA is not in it
    with pytest.raises(http.TransportError) as e:
        call(c)
    assert e.value.kind == "tls"
    assert tls_relay.seen == []  # no request byte was sent


def test_tls_hostname_is_checked(tmp_path):
    from orch.wsproto import wire

    ctx = http.tls_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert wire.origin_parts("https://relay.example")[0] == "https"


def test_an_empty_trust_store_fails_closed_at_the_handshake(tls_relay):
    """No emptiness probe (capath-only stores are valid): a context that trusts nothing simply fails verification."""

    def empty_context(**_kw):
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    c = http.RelayConn(tls_relay.origin("localhost"), context_factory=empty_context)
    with pytest.raises(http.TransportError) as e:
        call(c)
    assert e.value.kind == "tls" and tls_relay.seen == []


def test_tls_context_uses_the_default_store_when_no_ca_file():
    ctx = http.tls_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def _tls_case(tmp_path, **kw):
    ctx, ca = make_tls(tmp_path, **kw)
    return MockRelay(tls=ctx), ca


def test_tls_another_ca_file_is_refused(tmp_path, tls_relay):
    (tmp_path / "other").mkdir()
    _ctx, other_ca = make_tls(tmp_path / "other")
    c = http.RelayConn(tls_relay.origin("localhost"), ca_file=str(other_ca))
    with pytest.raises(http.TransportError) as e:
        call(c)
    assert e.value.kind == "tls" and tls_relay.seen == []


def test_tls_hostname_mismatch_is_refused(tmp_path):
    m, ca = _tls_case(tmp_path, names=("relay.example",))
    try:
        c = http.RelayConn(m.origin("localhost"), ca_file=str(ca))
        with pytest.raises(http.TransportError) as e:
            call(c)
        assert e.value.kind == "tls" and m.seen == []
    finally:
        m.close()


def test_tls_expired_certificate_is_refused(tmp_path):
    m, ca = _tls_case(tmp_path, expired=True)
    try:
        c = http.RelayConn(m.origin("localhost"), ca_file=str(ca))
        with pytest.raises(http.TransportError) as e:
            call(c)
        assert e.value.kind == "tls" and m.seen == []
    finally:
        m.close()


def test_tls_context_is_never_unverified(tmp_path):
    ctx, ca = make_tls(tmp_path)
    c = http.tls_context(str(ca))
    assert c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname


def test_http_only_for_loopback_origins():
    with pytest.raises(http.TransportError):
        http.RelayConn("http://relay.example")


# --- proxy from link.json -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("proxy", ["proxy.example:3128", "127.0.0.1:8080"])
def test_proxy_names_host_and_port(proxy):
    c = http.RelayConn("https://relay.example", proxy=proxy)
    assert c.proxy == (proxy.rsplit(":", 1)[0], int(proxy.rsplit(":", 1)[1]))


@pytest.mark.parametrize("proxy", ["http://proxy:3128", "proxy", "proxy:0", "proxy:99999", "a b:1", "proxy:1\r\n"])
def test_bad_proxy_refused(proxy):
    with pytest.raises(http.TransportError):
        http.RelayConn("https://relay.example", proxy=proxy)


# --- redaction ------------------------------------------------------------------------------------------------------

SECRET = "TOKENSECRETxyz123"


def test_logs_and_errors_hold_no_token_or_body(relay, caplog):
    caplog.set_level(logging.DEBUG, logger="orch.net")
    relay.expect(
        Expect("POST", "/v2/x", reply=json_reply(200, {"token": SECRET})),
        Expect("POST", "/v2/x", reply=Reply(body=SECRET.encode() * 100, fault="chunked")),
    )
    c = conn(relay)
    call(c, body=b'{"sig":"' + SECRET.encode() + b'"}', headers={"Authorization": f"Bearer {SECRET}"})
    with pytest.raises(http.TransportError) as e:
        call(c, cap=64, headers={"Authorization": f"Bearer {SECRET}"})
    text = caplog.text + str(e.value) + repr(e.value) + repr(c)
    assert SECRET not in text
    assert "POST /v2/x" in caplog.text  # the route template is logged


def test_declared_oversize_is_refused_before_reading_the_body(relay):
    """A Content-Length over the cap ends the call at once, not after a dripped body reaches the cap."""
    relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b" " * 5000, fault="drip_body", drip_s=0.05)))
    with pytest.raises(http.TransportError) as e:
        call(conn(relay), cap=1024, deadline_s=10)
    assert e.value.kind == "too_large"


@pytest.mark.filterwarnings("ignore:ssl.TLSVersion.TLSv1 is deprecated:DeprecationWarning")
def test_tls_minimum_version_is_raised_to_1_2():
    def old(**kw):
        c = ssl.create_default_context(**kw)
        c.minimum_version = ssl.TLSVersion.TLSv1
        return c

    assert http.tls_context(None, factory=old).minimum_version == ssl.TLSVersion.TLSv1_2


# --- the watchdog never touches a freed fd (review L3) ----------------------------------------------------------------


def test_shutdown_after_the_socket_was_freed_never_hits_a_reused_fd(relay):
    """http.client frees the socket at EOF of a will_close response before RelayConn.close runs. If the freed fd
    number is reused (here by a socketpair), a late watchdog must not shut that unrelated socket down."""
    import socket

    c = conn(relay)
    c._deadline = time.monotonic() + 5  # as a call sets it
    sock = c._socket(("127.0.0.1", relay.port), 5)
    sock.close()  # freed by http.client
    a, b = socket.socketpair()
    try:
        c.shutdown()  # the late watchdog
        a.sendall(b"x")
        assert b.recv(1) == b"x"
        b.sendall(b"y")
        assert a.recv(1) == b"y"
    finally:
        a.close()
        b.close()
        c.close()


def test_shutdown_and_close_are_idempotent_and_never_raise(relay):
    c = conn(relay)
    c.shutdown()
    c.close()
    c.shutdown()
    c.close()


# --- no proxy for a cleartext origin (review L4) ----------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8101", "http://localhost"])
def test_http_origin_with_a_proxy_is_refused(origin):
    with pytest.raises(http.TransportError) as e:
        http.RelayConn(origin, proxy="proxy.example:3128")
    assert e.value.kind == "request"


def test_tls_slow_drip_headers_end_at_the_total_deadline(tls_relay):
    """Review D3: over TLS, wrap_socket detaches the original socket, so the watchdog must act on its own dup."""
    tls_relay.expect(Expect("POST", "/v2/x", reply=Reply(body=b"{}", fault="drip_headers", drip_s=0.1)))
    c = http.RelayConn(tls_relay.origin("localhost"), ca_file=str(tls_relay.ca_file))
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=0.5)
    assert e.value.kind == "deadline"
    assert time.monotonic() - t0 < 4.0  # the drip alone takes over 10 s
    c.close()


# --- a stuck resolver is bounded by the call's deadline and by shutdown (review D5) ---------------------------------


@pytest.fixture
def stuck_resolver():
    release = threading.Event()
    calls = []

    def resolve(host, port, *a, **kw):
        calls.append(host)
        release.wait(60)
        raise OSError("released")

    resolve.calls = calls
    yield resolve
    release.set()


def test_stuck_dns_ends_at_the_total_deadline(stuck_resolver):
    c = http.RelayConn("https://relay.example", resolve=stuck_resolver)
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=0.5)
    assert e.value.kind == "deadline" and stuck_resolver.calls == ["relay.example"]
    assert time.monotonic() - t0 < 4.0


def test_stuck_dns_ends_on_shutdown(stuck_resolver):
    c = http.RelayConn("https://relay.example", resolve=stuck_resolver)
    threading.Timer(0.3, c.shutdown).start()
    t0 = time.monotonic()
    with pytest.raises(http.TransportError):
        call(c, deadline_s=30)
    assert time.monotonic() - t0 < 4.0


def test_resolved_address_is_used_and_the_hostname_kept_for_tls(tls_relay):
    """Resolution goes through the injected resolver; TLS still verifies the origin's hostname (SNI unchanged)."""
    seen = []

    def resolve(host, port, *a, **kw):
        seen.append(host)
        import socket

        return socket.getaddrinfo("127.0.0.1", port, 0, socket.SOCK_STREAM)

    tls_relay.expect(Expect("POST", "/v2/x"))
    c = http.RelayConn(tls_relay.origin("localhost"), ca_file=str(tls_relay.ca_file), resolve=resolve)
    assert call(c).status == 200 and seen == ["localhost"]
    c.close()


# --- the DNS helper: bounded threads, no busy spin, a shutdown is never lost (round 3, R4) ----------------------------


def test_stuck_lookups_are_capped_per_connection(stuck_resolver):
    c = http.RelayConn("https://relay.example", resolve=stuck_resolver)
    kinds = []
    for _ in range(200):
        with pytest.raises(http.TransportError) as e:
            call(c, deadline_s=0.01)
        kinds.append(e.value.kind)
    live = [t for t in threading.enumerate() if t.name == "orch-relay-dns"]
    assert len(stuck_resolver.calls) <= http.MAX_LOOKUPS and len(live) <= http.MAX_LOOKUPS
    assert "connect" in kinds  # above the cap: refused at once ("resolver stuck")


def test_a_stale_lookup_does_not_make_the_next_one_spin():
    """A helper from an earlier, timed-out call finishing during the next lookup must not wake it in a loop."""
    release_first, second_started = threading.Event(), threading.Event()
    n = [0]

    def resolve(host, port, *a, **kw):
        n[0] += 1
        if n[0] == 1:
            release_first.wait(30)
            raise OSError("late")
        second_started.set()
        time.sleep(0.6)
        import socket

        return socket.getaddrinfo("127.0.0.1", 9, 0, socket.SOCK_STREAM)

    c = http.RelayConn("https://relay.example", resolve=resolve)
    with pytest.raises(http.TransportError):
        call(c, deadline_s=0.05)
    threading.Timer(0.1, release_first.set).start()  # the stale helper ends while the second lookup waits
    before = c._lookup_wakeups
    with pytest.raises(http.TransportError):
        call(c, deadline_s=5)
    assert c._lookup_wakeups - before == 1  # woken only by its own helper; a shared event adds the stale wake-up


def _refused_resolver(calls):
    def resolve(host, port, *a, **kw):
        import socket

        calls.append(host)
        return socket.getaddrinfo("127.0.0.1", 9, 0, socket.SOCK_STREAM)  # discard port: refused at once

    return resolve


def test_completed_lookups_release_their_slots():
    """More than MAX_LOOKUPS lookups on one connection: each finished helper frees its slot (review round 4, N4)."""
    calls: list[str] = []
    c = http.RelayConn("https://relay.example", resolve=_refused_resolver(calls))
    n = 3 * http.MAX_LOOKUPS
    for _ in range(n):
        with pytest.raises(http.TransportError) as e:
            call(c, deadline_s=5)
        assert e.value.kind == "connect"
    assert len(calls) == n and c._lookups == 0


def test_a_failed_thread_start_releases_the_lookup_slot(monkeypatch):
    calls: list[str] = []
    c = http.RelayConn("https://relay.example", resolve=_refused_resolver(calls))
    real = threading.Thread

    class NoDns(real):
        def start(self):
            if self.name == "orch-relay-dns":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(http.threading, "Thread", NoDns)
    for _ in range(http.MAX_LOOKUPS + 2):
        with pytest.raises(http.TransportError) as e:
            call(c, deadline_s=5)
        assert e.value.kind == "connect"
    assert c._lookups == 0 and calls == []
    monkeypatch.setattr(http.threading, "Thread", real)
    with pytest.raises(http.TransportError):
        call(c, deadline_s=5)
    assert calls == ["relay.example"]  # the slots were not leaked


def test_shutdown_just_before_a_call_fails_it_at_once(stuck_resolver):
    c = http.RelayConn("https://relay.example", resolve=stuck_resolver)
    c.shutdown()
    t0 = time.monotonic()
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=20)
    assert e.value.kind == "shutdown" and stuck_resolver.calls == []
    assert time.monotonic() - t0 < 4.0


# --- round 6 (orch-52 #3): the watchdog sees a socket before it connects ---------------------------------------------


@pytest.fixture
def listener():
    """A loopback listener that never accepts: a connect completes in the kernel backlog, nothing ever answers."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s
    s.close()


def _accepted(listener, wait_s=0.3) -> bool:

    listener.settimeout(wait_s)
    try:
        c, _ = listener.accept()
    except TimeoutError:
        return False
    c.close()
    return True


def _resolve_to(listener, before=None):
    import socket

    def resolve(host, port, *a, **kw):
        if before is not None:
            before()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", listener.getsockname())]

    return resolve


def test_a_socket_still_connecting_is_ended_by_shutdown(listener):
    """``connect`` emulated as blocking on the very socket being connected (a real blocking ``recv`` after the kernel
    connect): ``shutdown()`` from another thread must reach it, so the socket is registered before ``connect``."""
    entered = threading.Event()

    class SlowConnect(http.RelayConn):
        def _connect(self, sock, sa):
            sock.connect(sa)
            entered.set()
            if sock.recv(1) == b"":
                raise ConnectionResetError("connect ended")
            raise AssertionError("unexpected byte")

    c = SlowConnect(f"http://127.0.0.1:{listener.getsockname()[1]}")
    out: dict = {}

    def run():
        try:
            call(c, deadline_s=30)
        except http.TransportError as e:
            out["kind"] = e.kind

    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    assert entered.wait(5)
    c.shutdown()
    t.join(5)
    assert not t.is_alive(), "shutdown did not reach the connecting socket"
    assert out["kind"] == "shutdown" and time.monotonic() - t0 < 5


def test_a_deadline_passed_before_the_socket_is_installed_never_connects(listener):
    """The injected clock passes the deadline while the name lookup runs (so the watchdog had nothing to kill): the call
    fails at the install check, before ``connect``."""
    now = [100.0]

    def late():
        now[0] += 60

    c = http.RelayConn("http://localhost:1", mono=lambda: now[0], resolve=_resolve_to(listener, late))
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=5)
    assert e.value.kind == "deadline"
    assert not _accepted(listener)


def test_a_shutdown_before_the_socket_is_installed_never_connects(listener):
    """``shutdown()`` lands while the name lookup completes (no socket to kill yet): the install check fails the call
    at once and nothing is connected or sent."""
    holder: dict = {}
    c = http.RelayConn("http://localhost:1", resolve=_resolve_to(listener, lambda: holder["c"].shutdown()))
    holder["c"] = c
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=5)
    assert e.value.kind == "shutdown"
    assert not _accepted(listener)


def test_a_shutdown_between_the_lookup_and_the_install_never_connects(listener):
    """``shutdown()`` lands after the name lookup returned and before the socket is installed (the lookup's own check
    is past): the install check fails the call at once, nothing is connected."""

    class ShutAfterLookup(http.RelayConn):
        def _lookup(self, host, port):
            r = super()._lookup(host, port)
            self.shutdown()
            return r

    c = ShutAfterLookup("http://localhost:1", resolve=_resolve_to(listener))
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=5)
    assert e.value.kind == "shutdown"
    assert not _accepted(listener)


def test_a_shutdown_racing_connect_sends_no_byte(listener):
    """Round 7 (R6-2): ``shutdown()`` lands after the install check and before the real ``connect`` (it hits a socket
    not yet connected: ENOTCONN, swallowed; on Darwin a shutdown during a pending connect can even let ``connect``
    succeed). The post-connect recheck fails the call before a byte is sent: the server sees no request."""

    class ShutInConnect(http.RelayConn):
        def _connect(self, sock, sa):
            self.shutdown()
            sock.connect(sa)

    c = ShutInConnect(f"http://127.0.0.1:{listener.getsockname()[1]}")
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=2)
    assert e.value.kind == "shutdown"
    listener.settimeout(1)
    try:
        peer, _ = listener.accept()
    except TimeoutError:
        return  # the connect itself failed: nothing reached the server either
    peer.settimeout(2)
    try:
        got = peer.recv(65536)
    except ConnectionResetError:
        got = b""
    finally:
        peer.close()
    assert got == b"", got


def test_a_deadline_firing_during_connect_sends_no_byte(listener):
    """The watchdog's counterpart: it fired while ``connect`` ran (so its kill found an unconnected socket); the
    recheck after ``connect`` fails the call as a deadline, with no byte sent."""

    class FireInConnect(http.RelayConn):
        def _connect(self, sock, sa):
            self._fired.set()
            sock.connect(sa)

    c = FireInConnect(f"http://127.0.0.1:{listener.getsockname()[1]}")
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=2)
    assert e.value.kind == "deadline"
    listener.settimeout(1)
    try:
        peer, _ = listener.accept()
    except TimeoutError:
        return
    peer.settimeout(2)
    try:
        got = peer.recv(65536)
    except ConnectionResetError:
        got = b""
    finally:
        peer.close()
    assert got == b"", got


def test_a_deadline_passed_during_connect_sends_no_byte_even_if_the_watchdog_is_late(listener):
    """Round 9: the injected clock passes the deadline while ``connect`` runs, and the watchdog (a real 30 s timer)
    has not fired: the post-connect recheck compares the clock itself and fails the call as a deadline before a byte
    is sent, so a late watchdog thread cannot let the call run past its deadline."""
    now = [100.0]

    class SlowClockConnect(http.RelayConn):
        def _connect(self, sock, sa):
            sock.connect(sa)
            now[0] += 60
            assert not self._fired.is_set()

    c = SlowClockConnect(f"http://127.0.0.1:{listener.getsockname()[1]}", mono=lambda: now[0])
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=30)
    assert e.value.kind == "deadline"
    listener.settimeout(1)
    try:
        peer, _ = listener.accept()
    except TimeoutError:
        return
    peer.settimeout(2)
    try:
        got = peer.recv(65536)
    except ConnectionResetError:
        got = b""
    finally:
        peer.close()
    assert got == b"", got


# -- round 10 (R9-2): the clock is rechecked after the TLS handshake and the proxy tunnel; connect errors past the
# deadline stop the address loop ------------------------------------------------------------------------------------


def test_a_deadline_passed_during_the_tls_handshake_sends_no_request_byte(tls_relay):
    """The injected clock passes the deadline inside the TLS handshake (the watchdog, a real 30 s timer, has not
    fired): the recheck after the handshake fails the call as a deadline, and the relay sees no request."""
    now = [100.0]

    class SlowHandshake(ssl.SSLContext):
        def wrap_socket(self, *a, **kw):
            s = super().wrap_socket(*a, **kw)
            now[0] += 60
            return s

    def factory(**kw):
        ctx = SlowHandshake(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=kw["cafile"])
        return ctx

    c = http.RelayConn(
        tls_relay.origin("localhost"), ca_file=str(tls_relay.ca_file), mono=lambda: now[0], context_factory=factory
    )
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=30)
    assert e.value.kind == "deadline"
    assert tls_relay.seen == []


def test_a_deadline_passed_during_the_proxy_tunnel_sends_nothing_through_it(listener):
    """The clock passes the deadline while the proxy answers CONNECT: the recheck right after the tunnel fails the call
    before the TLS ClientHello (let alone a request byte) goes through it; the proxy gets only the CONNECT."""
    now = [100.0]
    got: dict = {}

    def proxy():
        peer, _ = listener.accept()
        peer.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += peer.recv(4096)
        got["connect"] = buf
        now[0] += 60
        peer.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            got["after"] = peer.recv(65536)
        except (ConnectionResetError, TimeoutError):
            got["after"] = b""
        peer.close()

    t = threading.Thread(target=proxy, daemon=True)
    t.start()
    c = http.RelayConn("https://relay.example", proxy=f"127.0.0.1:{listener.getsockname()[1]}", mono=lambda: now[0])
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=30)
    t.join(5)
    assert e.value.kind == "deadline"
    assert got["connect"].startswith(b"CONNECT relay.example:443 ")
    assert got["after"] == b"", got


def test_a_connect_error_past_the_deadline_tries_no_further_address(listener):
    """Two addresses; the first is refused while the clock passes the deadline: the loop stops at that error (no socket
    is made for the second address), the call fails as a deadline, and the second (a live listener) sees nothing."""
    import socket

    now = [100.0]
    dead = ("127.0.0.1", 9)
    tried: list = []

    def resolve(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", sa) for sa in (dead, listener.getsockname())]

    installed: list = []

    class RefusedLate(http.RelayConn):
        def _watch_socket(self, sock):
            installed.append(sock)
            super()._watch_socket(sock)

        def _connect(self, sock, sa):
            tried.append(sa)
            if sa == dead:
                now[0] += 60
                raise ConnectionRefusedError("refused")
            sock.connect(sa)

    c = RefusedLate("http://localhost:1", mono=lambda: now[0], resolve=resolve)
    with pytest.raises(http.TransportError) as e:
        call(c, deadline_s=30)
    assert e.value.kind == "deadline"
    assert tried == [dead]
    assert len(installed) == 1  # the loop stopped at the error itself: no socket was even made for the second address
    assert not _accepted(listener)


# -- a kept connection the relay has closed (uvicorn closes an idle keep-alive after 5 s) -----------------------------


class _ClosingServer:
    """Answers each request on a connection with a keep-alive 200, then closes that connection ``close_after`` seconds
    later (0: at once, the client sees a dead connection on its next request); counts connections and requests."""

    def __init__(self) -> None:
        import socket

        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.conns = 0
        self.requests = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def origin(self) -> str:
        return f"http://127.0.0.1:{self.srv.getsockname()[1]}"

    def _run(self) -> None:
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self._serve, args=(s,), daemon=True).start()

    def _serve(self, s) -> None:
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                d = s.recv(4096)
                if not d:
                    return
                buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = next(
                (int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length:")), 0
            )
            while len(rest) < length:  # the whole request is read: an unread byte at close would make the FIN a RST
                d = s.recv(4096)
                if not d:
                    return
                rest += d
            self.requests += 1
            s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}")
            s.shutdown(socket.SHUT_WR)
        finally:
            s.close()  # no `Connection: close` was announced: the client believes the connection is alive

    def close(self) -> None:
        self.srv.close()


def test_a_connection_idle_past_the_reuse_limit_is_replaced_before_sending():
    srv = _ClosingServer()
    clock = [0.0]
    c = http.RelayConn(srv.origin(), mono=lambda: clock[0])
    try:
        assert call(c).status == 200
        clock[0] += http.IDLE_REUSE_S + 0.5  # proactive: the connection is dropped without trying it
        assert call(c).status == 200
        assert srv.conns == 2 and srv.requests == 2
    finally:
        c.close()
        srv.close()


def test_a_reused_connection_the_peer_closed_is_reopened_and_resent_once_within_the_deadline():
    srv = _ClosingServer()
    clock = [0.0]
    c = http.RelayConn(srv.origin(), mono=lambda: clock[0])
    try:
        assert call(c).status == 200
        time.sleep(0.2)  # the server has closed its end; the clock says the connection is fresh (reactive path)
        # The OS decides whether the request on the closed connection meets a clean EOF (RemoteDisconnected: resent
        # once) or a reset (never resent after the send: the caller gets a TransportError and retries itself). The
        # property under test is safety: the server never processes the request twice, and the next call works.
        try:
            second = call(c).status
        except http.TransportError:
            second = None
        assert srv.requests == (2 if second == 200 else 1)
        clock[0] += http.IDLE_REUSE_S + 1  # the server closes after every answer: the next call must start afresh
        assert call(c).status == 200
        assert srv.requests <= 3
    finally:
        c.close()
        srv.close()


def test_a_fresh_connection_that_fails_is_not_retried():
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    seen = []

    def run() -> None:
        while True:
            try:
                s, _ = srv.accept()
            except OSError:
                return
            seen.append(1)
            s.recv(4096)
            s.close()  # no answer at all, on a connection that was never reused

    threading.Thread(target=run, daemon=True).start()
    c = http.RelayConn(f"http://127.0.0.1:{srv.getsockname()[1]}")
    try:
        with pytest.raises(http.TransportError):
            call(c)
        assert len(seen) == 1
    finally:
        c.close()
        srv.close()


def _server(second: str):
    """Per connection, the first request is answered fully (keep-alive). What happens to the SECOND request on that
    connection (``second``): ``rst_after_first``: the connection is reset right after the first answer (the client's
    next write fails); ``fin``: the request is read, then a clean close with no bytes;
    ``partial_status``: ``HTTP/1.1 2``
    (no CRLF), 0.3 s, RST; ``partial_headers``: a full status line and half a header, RST. Counts parsed requests."""
    import socket
    import struct

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    hits = []
    rst = struct.pack("ii", 1, 0)

    def serve(s) -> None:
        n = 0
        buf = b""
        while True:
            while b"\r\n\r\n" not in buf:
                d = s.recv(4096)
                if not d:
                    return
                buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = next(
                (int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length:")), 0
            )
            while (
                len(rest) < length
            ):  # the COMPLETE request is read: an unread byte at close would turn the FIN into a RST
                d = s.recv(4096)
                if not d:
                    return
                rest += d
            buf = b""
            hits.append(1)
            n += 1
            if n == 1:
                s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                if second == "rst_after_first":
                    time.sleep(0.05)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    s.close()
                    return
                continue
            if second == "partial_status":
                s.sendall(b"HTTP/1.1 2")
                time.sleep(0.3)
            elif second == "partial_headers":
                s.sendall(b"HTTP/1.1 200 OK\r\nContent-Le")
                time.sleep(0.1)
            if second == "fin":
                s.shutdown(socket.SHUT_WR)  # a clean FIN on every OS
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, rst)
            s.close()
            return

    def run() -> None:
        while True:
            try:
                s, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(s,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv, hits


def _second_call(second: str, *, resend_ok: bool = True):
    srv, hits = _server(second)
    c = http.RelayConn(f"http://127.0.0.1:{srv.getsockname()[1]}")
    try:
        assert call(c).status == 200
        time.sleep(0.2)
        if second == "rst_after_first":
            c._last_used = c._mono()
        try:
            r = c.request("POST", "/v2/x", route="POST /v2/x", headers={}, body=b"{}", deadline_s=5.0, cap=1024,
                          resend_ok=resend_ok)  # fmt: skip
            return r, hits
        except http.TransportError as e:
            return e, hits
    finally:
        c.close()
        srv.close()


@pytest.mark.parametrize("second", ["partial_status", "partial_headers"])
def test_a_reset_after_the_request_was_sent_is_never_resent(second):
    r, hits = _second_call(second)
    assert isinstance(r, http.TransportError)
    assert len(hits) == 2  # the first request, and the second exactly once: never sent a third time


def test_a_clean_close_after_the_request_was_read_is_resent_once_on_a_new_connection():
    r, hits = _second_call("fin")
    assert not isinstance(r, Exception) and r.status == 200
    assert len(hits) == 3  # first, the second (read, then FIN), its one resend


def test_a_reset_while_sending_on_a_reused_connection_is_resent_once():
    r, hits = _second_call("rst_after_first")
    assert not isinstance(r, Exception) and r.status == 200
    assert len(hits) == 2  # the first request; the second never arrived on the dead connection, its resend did


@pytest.mark.parametrize("second", ["fin", "rst_after_first"])
def test_resend_off_never_resends(second):
    r, hits = _second_call(second, resend_ok=False)
    assert isinstance(r, http.TransportError)
    assert len(hits) <= 2 and (second == "rst_after_first" or len(hits) == 2)
