"""``orch.relay.http`` (11A.7): stdlib transport with verified TLS, a total monotonic deadline per call, bounded bodies,
no redirects, no environment proxies, and redaction. Fault injection against the loopback mock relay."""

from __future__ import annotations

import logging
import ssl
import threading
import time

import pytest

from orch.relay import http

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
    from orch.relay import wire

    ctx = http.tls_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert wire.origin_parts("https://relay.example")[0] == "https"


def test_no_trust_store_refuses_to_start():
    def empty_context(**_kw):
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies, but trusts nothing

    with pytest.raises(http.NoTrustStore):
        http.tls_context(None, factory=empty_context)
    with pytest.raises(http.NoTrustStore):
        http.RelayConn("https://relay.example", context_factory=empty_context)


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
    caplog.set_level(logging.DEBUG, logger="orch.relay")
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
