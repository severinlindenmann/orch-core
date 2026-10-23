"""P4: ``PublishSigner`` and ``PublishClient`` (design-publish-addon §2.4, protocol-v2 §12.1).

Vectors: ``tests/publish/vectors/publish_v2.json``, byte for byte orch-publish's ``tests/vectors/publish_v2.json`` (P1),
which is the suite-2 subset of orch-relay ``tests/vectors_v2.json``. Pinned by sha256 in ``vectors/SOURCE`` and checked
against the relay corpus vendored in ``tests/relay/vectors``, so a drift in either repo fails here."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import re
from pathlib import Path

import pytest

from orch import crypto
from orch.publishclient import PublishClient, PublishError, share_target
from orch.wsproto.pubsign import PublishContext, PublishSigner, publish_signed_bytes
from orch.wsproto.sign import SignerRefused
from tests.relay import corpus
from tests.relay.helpers import Key, MemLedger

VEC_DIR = Path(__file__).resolve().parent / "vectors"
VEC_BYTES = (VEC_DIR / "publish_v2.json").read_bytes()
VEC = json.loads(VEC_BYTES)
V = VEC["publish"]
WS = V["headers"]["Orch-Workspace"]
WSK_PUB = bytes.fromhex(VEC["keys"]["wsk_a.sig"]["pub"])
ORIGIN = "https://pub.dev.severin.io"
SECRET = "TOKEN-SENTINEL-zQ9x-secret"


def ctx(origin=ORIGIN, ws=WS):
    return PublishContext(ws, origin)


def target(*p):
    return share_target(WS, *p)


class Clock:
    def __init__(self, t=1_790_000_000_000):
        self.t = t

    def __call__(self):
        return self.t


def mk(origin=ORIGIN, **kw):
    kw.setdefault("clock_ms", Clock())
    return PublishSigner(Key(), ctx(origin), **kw), kw["clock_ms"]


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_vector_file_is_the_pinned_copy_of_the_relay_vectors():
    pin = dict(line.split(": ", 1) for line in (VEC_DIR / "SOURCE").read_text().splitlines())
    assert hashlib.sha256(VEC_BYTES).hexdigest() == pin["sha256"]
    assert V == corpus.S2["publish"]  # the same publish block as the vendored relay corpus
    assert VEC["ids"] == corpus.S2["ids"]
    assert VEC["keys"]["wsk_a.sig"]["pub"] == corpus.KEYS["wsk_a.sig"]["pub"]
    assert VEC["source"]["repo"] == "orch-relay" and len(VEC["source"]["sha256"]) == 64


# --- vectors -----------------------------------------------------------------------------------------------------


def test_vector_signed_bytes_and_signature():
    h = V["headers"]
    c0 = V["cases"][0]
    got = publish_signed_bytes(
        bytes.fromhex(h["Orch-Workspace"]), int(h["Orch-Ts"]), bytes.fromhex(h["Orch-Nonce"]),
        c0["method"], c0["host"], c0["target"], bytes.fromhex(c0["body"]),
    )  # fmt: skip
    assert got.hex() == V["signed_bytes"]
    assert crypto.verify(WSK_PUB, unb64u(h["Orch-Signature"]), got)


def _server_check(case, now_ms=None):
    """§12.1 steps 1 to 4 (the nonce store is the case's), written from the protocol text, over the case's inputs."""
    hd = case["headers"]
    try:
        ws = crypto.unhex(hd["Orch-Workspace"], 16)
        if not re.fullmatch(r"0|[1-9][0-9]{0,15}", hd["Orch-Ts"]):
            raise ValueError
        ts = int(hd["Orch-Ts"])
        nonce = crypto.unhex(hd["Orch-Nonce"], 16)
        sig = unb64u(hd["Orch-Signature"])
    except (KeyError, ValueError, crypto.EncodingError):
        return {"status": 400, "error": "malformed"}
    now = case["now_ms"]
    if abs(now - ts) > 120_000:
        return {"status": 401, "error": "stale_timestamp", "server_ms": now}
    if ws.hex() != WS:
        return {"status": 401, "error": "unknown_workspace"}
    msg = publish_signed_bytes(ws, ts, nonce, case["method"], case["host"], case["target"], bytes.fromhex(case["body"]))
    if not crypto.verify(WSK_PUB, sig, msg):
        return {"status": 401, "error": "bad_signature"}
    seen = case["nonces"].get(ws.hex(), {})
    if seen.get(nonce.hex(), 0) > now:
        return {"status": 401, "error": "replay"}
    return {"status": 200, "workspace": ws.hex()}


@pytest.mark.parametrize("case", V["cases"], ids=[c["name"] for c in V["cases"]])
def test_vector_cases_with_our_bytes(case):
    assert _server_check(case) == case["expect"]


def test_signer_output_checks_out_under_the_same_rules():
    """Our signer's headers pass the vector-pinned check, and the same request with any one input changed fails."""
    s, clock = mk()
    t, body = target("shares", "abc"), b'{"k":1}'
    sp = s.request("PUT", t, body)
    base = {
        "headers": sp.headers,
        "method": "PUT",
        "host": sp.host,
        "target": t,
        "body": body.hex(),
        "now_ms": clock(),
        "nonces": {},
    }
    assert _server_check(base) == {"status": 200, "workspace": WS}
    for k, v in (
        ("method", "DELETE"),
        ("host", "pub.example.com"),
        ("target", target("shares", "abd")),
        ("body", "00"),
    ):
        assert _server_check({**base, k: v})["error"] == "bad_signature", k


# --- canonical bytes and headers ---------------------------------------------------------------------------------


def test_layout_and_header_set():
    nonce = bytes(range(16))
    s, clock = mk(rand=lambda n: nonce)
    t = target("shares", "s1", "files", "a b.txt")
    sp = s.request("PUT", t, b"hello")
    assert set(sp.headers) == {"Orch-Workspace", "Orch-Ts", "Orch-Nonce", "Orch-Signature"}
    assert sp.headers["Orch-Workspace"] == WS
    assert sp.headers["Orch-Ts"] == "1790000000000"
    assert sp.headers["Orch-Nonce"] == nonce.hex()
    want = (
        b"orch/v2/publish|" + bytes([crypto.SUITE_ID]) + bytes.fromhex(WS) + (1_790_000_000_000).to_bytes(8, "big")
        + nonce + b"\x00\x03PUT" + b"\x00\x12pub.dev.severin.io" + len(t).to_bytes(2, "big") + t.encode()
        + crypto.sha256(b"hello")
    )  # fmt: skip
    assert crypto.verify(WSK_PUB, unb64u(sp.headers["Orch-Signature"]), want)
    assert "%20" in t  # the target is signed as sent: encoded, never decoded


@pytest.mark.parametrize(
    "origin,host",
    [
        ("https://pub.dev.severin.io", "pub.dev.severin.io"),
        ("https://pub.dev.severin.io:443", "pub.dev.severin.io"),
        ("https://pub.dev.severin.io:8443", "pub.dev.severin.io:8443"),
        ("http://127.0.0.1:8103", "127.0.0.1:8103"),
        ("http://127.0.0.1", "127.0.0.1"),
    ],
)
def test_host_is_derived_from_origin(origin, host):
    assert ctx(origin).host == host
    s, _ = mk(origin)
    assert s.request("GET", target("shares")).host == host


@pytest.mark.parametrize(
    "origin",
    [
        "http://pub.example.com",
        "https://Pub.Example.com",
        "https://x.io/p",
        "https://x.io?q",
        "ftp://x.io",
        "",
        "https://x.io:0",
    ],
)
def test_origin_refused(origin):
    with pytest.raises(SignerRefused):
        PublishContext(WS, origin)


@pytest.mark.parametrize(
    "t",
    [
        "", "/", "/v1/apps/x", f"/v1/ws/{'0' * 32}/shares", f"/v1/ws/{WS}", "v1/ws/" + WS + "/x", f"/v1/ws/{WS}/a b",
        f"/v1/ws/{WS}/a#b", f"/v1/ws/{WS}/a\\b", f"/v1/ws/{WS}/a\nb", f"/v1/ws/{WS}/é",
        f"/v1/ws/{WS}/" + "a" * 2100, None, b"/x",
    ],
)  # fmt: skip
def test_target_refused(t):
    s, _ = mk()
    with pytest.raises(SignerRefused):
        s.request("GET", t)


@pytest.mark.parametrize("method", ["get", "PATCH", "", "CONNECT", None])
def test_method_refused(method):
    s, _ = mk()
    with pytest.raises(SignerRefused):
        s.request(method, target("shares"))


def test_body_must_be_bytes():
    s, _ = mk()
    for b in ("str", bytearray(b"x"), memoryview(b"x"), None):
        with pytest.raises(SignerRefused):
            s.request("PUT", target("shares"), b)


def test_no_generic_sign_and_no_key_in_repr():
    s, _ = mk()
    assert not hasattr(s, "sign")
    assert "priv" not in repr(s).lower() and WS in repr(s)
    with pytest.raises(AttributeError):
        s.extra = 1  # __slots__


# --- nonce, ts, body hash ----------------------------------------------------------------------------------------


def test_nonce_unique_and_16_random_bytes():
    s, _ = mk(rand=None)
    ns = {s.request("GET", target("shares")).headers["Orch-Nonce"] for _ in range(300)}
    assert len(ns) == 300 and all(re.fullmatch(r"[0-9a-f]{32}", n) for n in ns)


def test_same_request_twice_gets_new_nonce_and_signature():
    s, _ = mk()
    a, b = s.request("GET", target("shares")), s.request("GET", target("shares"))
    assert a.headers["Orch-Nonce"] != b.headers["Orch-Nonce"]
    assert a.headers["Orch-Signature"] != b.headers["Orch-Signature"]


def test_repeated_nonce_refused_even_with_a_broken_rng():
    s, _ = mk(rand=lambda n: b"\x07" * 16)
    s.request("GET", target("shares"))
    with pytest.raises(SignerRefused, match="nonce reuse"):
        s.request("GET", target("shares"))


def test_ledger_records_and_refuses_known_nonce():
    led = MemLedger()
    s, _ = mk(ledger=led, rand=lambda n: b"\x01" * 16)
    sp = s.request("GET", target("shares"))
    dig, sig = led.get("publish", "01" * 16)
    assert sig == sp.headers["Orch-Signature"] and len(dig) == 64
    s2, _ = mk(ledger=led, rand=lambda n: b"\x01" * 16)  # a restarted process, same ledger
    with pytest.raises(SignerRefused, match="nonce reuse"):
        s2.request("GET", target("shares"))


@pytest.mark.parametrize("n", [b"", b"\x00" * 15, b"\x00" * 17, "x" * 16, None])
def test_bad_rng_output_refused(n):
    s, _ = mk(rand=lambda k: n)
    with pytest.raises(SignerRefused):
        s.request("GET", target("shares"))


def test_ts_comes_from_the_signer_clock_each_call():
    s, clock = mk()
    assert s.request("GET", target("shares")).headers["Orch-Ts"] == "1790000000000"
    clock.t += 1234
    assert s.request("GET", target("shares")).headers["Orch-Ts"] == "1790000001234"
    clock.t = -1
    with pytest.raises(SignerRefused):
        s.request("GET", target("shares"))
    clock.t = 2**53
    with pytest.raises(SignerRefused):
        s.request("GET", target("shares"))


def test_default_clock_is_wall_time_ms():
    import time

    s = PublishSigner(Key(), ctx())
    before = time.time_ns() // 1_000_000
    ts = int(s.request("GET", target("shares")).headers["Orch-Ts"])
    assert before <= ts <= time.time_ns() // 1_000_000
    assert not str(ts).startswith("0")


@pytest.mark.parametrize(
    "body", [b"", b"x", b"\x00" * 1000, bytes(range(256)) * 40_000], ids=["empty", "one", "1k", "10MB"]
)
def test_body_hash_binds_any_size(body):
    s, clock = mk(rand=lambda n: b"\x02" * 16)
    t = target("shares", "s")
    sp = s.request("PUT", t, body)
    msg = publish_signed_bytes(bytes.fromhex(WS), clock(), b"\x02" * 16, "PUT", sp.host, t, body)
    assert msg.endswith(crypto.sha256(body))
    assert crypto.verify(WSK_PUB, unb64u(sp.headers["Orch-Signature"]), msg)
    other = msg[:-32] + crypto.sha256(body + b"!")
    assert not crypto.verify(WSK_PUB, unb64u(sp.headers["Orch-Signature"]), other)


def test_malformed_signature_from_key_refused():
    class Bad(Key):
        def sign(self, payload):
            return b"short"

    s = PublishSigner(Bad(), ctx(), clock_ms=Clock())
    with pytest.raises(SignerRefused):
        s.request("GET", target("shares"))


# --- client: retries and errors ----------------------------------------------------------------------------------


class Net:
    """A scripted transport: replies are (status, body) or an exception; every call is recorded."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, scheme, netloc, method, tgt, headers, body, timeout):
        self.calls.append((scheme, netloc, method, tgt, dict(headers), body))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def err(code):
    return json.dumps({"error": code}).encode()


def client(net, **kw):
    s, clock = mk(**kw)
    sleeps = []
    return PublishClient(s, transport=net, sleep=sleeps.append), net, sleeps, clock


def test_success_returns_body_and_sends_signed_headers():
    c, net, _, _ = client(Net((200, b'{"ok":true}')))
    r = c.request("GET", target("shares"))
    assert r.json() == {"ok": True}
    scheme, netloc, method, tgt, hd, body = net.calls[0]
    assert (scheme, netloc, method, tgt, body) == ("https", "pub.dev.severin.io", "GET", target("shares"), b"")
    assert set(hd) == {"Orch-Workspace", "Orch-Ts", "Orch-Nonce", "Orch-Signature"}


def test_stale_timestamp_retries_once_with_new_nonce_and_clock_reread():
    s, clock = mk()
    start, calls = clock.t, []

    def net(*a):
        calls.append(a)
        if len(calls) == 1:
            clock.t = start + 5000
            return 401, json.dumps({"error": "stale_timestamp", "server_ms": 1}).encode()
        return 200, b"{}"

    c = PublishClient(s, transport=net, sleep=lambda x: None)
    assert c.request("PUT", target("shares", "s")).status == 200
    h1, h2 = calls[0][4], calls[1][4]
    assert h1["Orch-Nonce"] != h2["Orch-Nonce"]
    assert h1["Orch-Ts"] == str(start) and h2["Orch-Ts"] == str(start + 5000)  # server_ms (=1) was ignored


def test_stale_twice_is_final_and_says_clock():
    c, net, _, _ = client(Net((401, err("stale_timestamp")), (401, err("stale_timestamp"))))
    with pytest.raises(PublishError) as e:
        c.request("PUT", target("shares", "s"))
    assert e.value.code == "stale_timestamp" and e.value.clock_off and len(net.calls) == 2


def test_post_stale_timestamp_also_retries_once():
    # the server rejected it before doing anything, so even a non-idempotent POST may be signed again
    c, net, _, _ = client(Net((401, err("stale_timestamp")), (200, b"{}")))
    assert c.request("POST", target("shares", "s", "commit")).status == 200 and len(net.calls) == 2


def test_lost_answer_resends_idempotent_put_with_fresh_nonce():
    c, net, sleeps, _ = client(Net(TimeoutError(), ConnectionResetError(), (200, b"{}")))
    assert c.request("PUT", target("shares", "s"), b"meta").status == 200
    nonces = [x[4]["Orch-Nonce"] for x in net.calls]
    assert len(set(nonces)) == 3 and all(x[5] == b"meta" for x in net.calls) and len(sleeps) == 2


def test_lost_answer_gives_up_after_three_attempts():
    c, net, _, _ = client(Net(OSError(), OSError(), OSError()))
    with pytest.raises(PublishError) as e:
        c.request("DELETE", target("shares", "s"))
    assert e.value.code == "network" and len(net.calls) == 3


def test_post_is_not_resent_unless_declared_idempotent():
    c, net, _, _ = client(Net(OSError(), (200, b"{}")))
    with pytest.raises(PublishError) as e:
        c.request("POST", target("shares", "s", "commit"))
    assert e.value.code == "network" and e.value.sent and len(net.calls) == 1
    c, net, _, _ = client(Net(OSError(), (200, b"{}")))
    assert c.request("POST", target("shares", "s", "commit"), idempotent=True).status == 200 and len(net.calls) == 2


def test_5xx_gateway_resend_only_when_idempotent():
    c, net, _, _ = client(Net((503, b"<html>"), (200, b"{}")))
    assert c.request("PUT", target("shares", "s")).status == 200
    c, net, _, _ = client(Net((503, b"<html>"), (200, b"{}")))
    with pytest.raises(PublishError) as e:
        c.request("POST", target("shares", "s", "commit"))
    assert e.value.status == 503 and len(net.calls) == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "malformed"),
        (401, "replay"),
        (401, "bad_signature"),
        (401, "unknown_workspace"),
        (404, "not_found"),
        (409, "exists"),
        (500, "oops"),
    ],
)
def test_other_errors_are_final_with_the_servers_code(status, code):
    c, net, _, _ = client(Net((status, err(code)), (200, b"{}")))
    with pytest.raises(PublishError) as e:
        c.request("PUT", target("shares", "s"))
    assert (e.value.code, e.value.status) == (code, status) and len(net.calls) == 1


def test_junk_error_code_is_not_echoed():
    c, _, _, _ = client(Net((400, json.dumps({"error": "X" * 500 + SECRET}).encode())))
    with pytest.raises(PublishError) as e:
        c.request("PUT", target("shares", "s"))
    assert e.value.code == "rejected"


def test_signer_refusal_means_nothing_was_sent():
    c, net, _, _ = client(Net())
    with pytest.raises(PublishError) as e:
        c.request("PUT", "/v1/apps/x")
    assert e.value.code == "refused" and not e.value.sent and net.calls == []


# --- no secret in logs or errors ---------------------------------------------------------------------------------


def test_no_secret_in_errors_reprs_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    t = target("shares", "s", "files", SECRET)  # a hostile path part or query is the closest thing to a secret here
    body = SECRET.encode() * 3
    reply = json.dumps({"error": "bad_signature", "detail": SECRET}).encode()
    c, net, _, _ = client(Net((401, reply), http.client.RemoteDisconnected(SECRET)))
    with pytest.raises(PublishError) as e:
        c.request("PUT", t, body)
    shown = " ".join([str(e.value), repr(e.value), repr(e.value.__dict__), repr(c._signer), caplog.text])
    assert SECRET not in shown and SECRET.lower() not in shown
    c, net, _, _ = client(
        Net(http.client.RemoteDisconnected(SECRET), http.client.RemoteDisconnected(SECRET), OSError(SECRET))
    )
    with pytest.raises(PublishError) as e:
        c.request("PUT", t, body)
    assert SECRET not in str(e.value) + repr(e.value.__dict__) + caplog.text
    assert e.value.__cause__ is None and e.value.__suppress_context__  # `from None`: the OSError text is not chained
    sp = c._signer.request("PUT", t, body)
    assert SECRET not in repr(sp) + caplog.text


def test_signer_refusal_text_has_no_input():
    s, _ = mk()
    with pytest.raises(SignerRefused) as e:
        s.request("PUT", f"/v1/ws/{WS}/{SECRET} x")
    assert SECRET not in str(e.value)


def test_share_target_encodes_each_part():
    t = share_target(WS, "shares", "id", "files", "a b?d#e")
    assert t == f"/v1/ws/{WS}/shares/id/files/a%20b%3Fd%23e"


# --- path normalisation: one form only (scan finding, 18 Oct) ---------------------------------------------------------

BAD_SEGMENTS = [
    "", ".", "..", "%2e", "%2E", "%2e%2e", "%2E%2E", ".%2e", "%2e.", "%252e", "%252e%252e", "%25252e", "%2f", "%2F",
    "a%2fb", "a%2Fb", "%252f", "%255c", "%5c", "a%5Cb", "%00", "a%", "%zz", "%2",
]  # fmt: skip


@pytest.mark.parametrize("seg", BAD_SEGMENTS)
def test_signer_refuses_dot_empty_and_encoded_separator_segments(seg):
    s, _ = mk()
    for t in (
        f"/v1/ws/{WS}/shares/{seg}/x",
        f"/v1/ws/{WS}/shares/{seg}",
        f"/v1/ws/{WS}/{seg}",
        f"/v1/ws/{WS}/shares/ok/files/{seg}?x=1",
    ):
        with pytest.raises(SignerRefused):
            s.request("GET", t)


@pytest.mark.parametrize("t", ["//", "/x//y", "/./x", "/x/./y", "/x/../y", "/x/", "/x/.", "/x/.."])
def test_signer_refuses_unnormalised_paths(t):
    s, _ = mk()
    with pytest.raises(SignerRefused):
        s.request("GET", f"/v1/ws/{WS}" + t)


def test_signer_refuses_dot_segments_before_the_workspace_too():
    s, _ = mk()
    for t in (f"/v1/ws/../ws/{WS}/x", f"/v1//ws/{WS}/x", f"/v1/ws/{WS}//x"):
        with pytest.raises(SignerRefused):
            s.request("GET", t)


def test_signer_accepts_plain_and_encoded_names_and_query_dots():
    s, _ = mk()
    for t in (
        f"/v1/ws/{WS}/shares",
        f"/v1/ws/{WS}/shares/abc/files/a%20b.txt",
        f"/v1/ws/{WS}/shares/abc/files/.hidden",
        f"/v1/ws/{WS}/shares/abc/files/a..b",
        f"/v1/ws/{WS}/shares/abc/files/%C3%A9.txt",
        f"/v1/ws/{WS}/shares?path=../x&q=%2e%2e%2f",  # the query is a value, not a path
    ):
        s.request("GET", t)


@pytest.mark.parametrize("part", ["", ".", "..", "a/b", "a\\b", "a\x00b", "/", None, 5])
def test_share_target_refuses_bad_parts_before_quoting(part):
    with pytest.raises(PublishError) as e:
        share_target(WS, "shares", part)
    assert e.value.code == "bad_path" and not e.value.sent


def test_share_target_output_always_passes_the_signer():
    s, _ = mk()
    for p in ("a b", "é", "a..b", ".hidden", "50%", "x?y", "x#y"):
        s.request("GET", share_target(WS, "shares", "id", "files", p))


def test_a_name_that_decodes_to_a_separator_is_refused_even_when_quoted():
    # the literal name "a%2fb" is quoted to a%252fb, which one more decode turns into a/b: refused on purpose
    with pytest.raises(SignerRefused):
        mk()[0].request("GET", share_target(WS, "shares", "a%2fb"))
    with pytest.raises(SignerRefused):
        mk()[0].request("GET", share_target(WS, "shares", "%2e"))


def test_network_errors_then_stale_send_at_most_three_times():
    c, net, _, _ = client(Net(OSError(), OSError(), (401, err("stale_timestamp")), (200, b"{}")))
    with pytest.raises(PublishError):
        c.request("PUT", target("shares", "s"))
    assert len(net.calls) == 3


def test_target_ending_in_empty_query_is_refused():
    s, _ = mk()
    for t in (f"/v1/ws/{WS}/shares?", f"/v1/ws/{WS}/shares/x?"):
        with pytest.raises(SignerRefused):
            s.request("GET", t)
    s.request("GET", f"/v1/ws/{WS}/shares?a=1")
