"""#1285: the /h/ listener over real sockets: framing, limits, total deadline, what it reads, what it logs."""

import hashlib
import hmac
import logging
import socket
import threading
import time
from pathlib import Path

import pytest

from orch_hub.contract import hooks as HK
from orch_hub.hooks import inbox as IB
from orch_hub.hooks import listener as LS
from orch_hub.hooks.store import HookStore

WS = "a" * 32
TOKEN = "hh_" + "T" * 43
SECRET = "hs_" + "S" * 43
NOW = 1_800_000_000_000
BODY = b'{"action":"completed"}'


@pytest.fixture
def lst():
    made = []

    def make(total_s=10.0, trusted=frozenset(), limits=None, workers=8):
        st = HookStore()
        st.put(
            WS,
            {
                "token_hash": HK.token_hash(TOKEN),
                "followup": "FU-1",
                "source": "github",
                "secret": SECRET,
                "expires_ms": NOW + 3_600_000,
            },
            NOW,
        )
        ib = IB.HookInbox(st, lambda: NOW, "https://hub.example/h/", limits)
        seen = []
        real = ib.finish

        def spy(hook, headers, body, source):
            seen.append((dict(headers), body, source))
            return real(hook, headers, body, source)

        ib.finish = spy
        ls = LS.HookListener(ib, "127.0.0.1:0", trusted_peers=trusted, total_s=total_s, workers=workers)
        ls.start()
        made.append(ls)
        return ls, st, ib, seen

    yield make
    for ls in made:
        ls.close()


def sig(body=BODY, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def raw(ls, data: bytes, read=True, timeout=5) -> bytes:
    s = socket.create_connection(ls.address, timeout=timeout)
    try:
        s.sendall(data)
        return s.recv(4096) if read else b""
    finally:
        s.close()


def post(token=TOKEN, body=BODY, extra=b"", signature=None, line=None):
    sg = sig(body) if signature is None else signature
    head = line or f"POST /h/{token} HTTP/1.1".encode()
    return (
        head
        + b"\r\nHost: h\r\nX-Hub-Signature-256: "
        + sg.encode()
        + b"\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n"
        + extra
        + b"\r\n"
        + body
    )


def status(resp: bytes) -> int:
    return int(resp.split(b" ", 2)[1])


def test_good_ring_is_204_with_empty_body_and_stored(lst):
    ls, st, _ib, _seen = lst()
    resp = raw(ls, post())
    assert status(resp) == 204 and resp.endswith(b"\r\n\r\n") and b"Content-Length: 0" in resp
    assert len(st.pull(WS, 0, NOW)["rings"]) == 1


@pytest.mark.parametrize(
    "data",
    [
        post(token="hh_" + "U" * 43),  # unknown
        post(signature="sha256=" + "0" * 64),  # bad signature
        post(line=b"GET /h/" + TOKEN.encode() + b" HTTP/1.1"),
        post(line=b"POST /h/" + TOKEN.encode() + b"?x=1 HTTP/1.1"),
        post(line=b"POST /x/" + TOKEN.encode() + b" HTTP/1.1"),
        post(line=b"POST /h/ HTTP/1.1"),
        b"garbage\r\n\r\n",
    ],
)
def test_everything_that_is_not_a_good_ring_is_the_same_404(lst, data):
    ls, st, _ib, _seen = lst()
    resp = raw(ls, data)
    assert resp == b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    assert st.pull(WS, 0, NOW)["rings"] == []


def test_length_cap_is_checked_before_any_body_byte(lst):
    ls, _st, _ib, seen = lst()
    head = f"POST /h/{TOKEN} HTTP/1.1\r\nContent-Length: {HK.BODY_CAP + 1}\r\n\r\n".encode()
    assert status(raw(ls, head)) == 413  # no body was sent at all, and it answered
    assert seen == []
    ok = f"POST /h/{TOKEN} HTTP/1.1\r\nContent-Length: {HK.BODY_CAP}\r\n\r\n".encode()
    s = socket.create_connection(ls.address, timeout=2)
    s.sendall(ok + b"x" * 100)  # exactly the cap is accepted into the read; we stop sending: no answer but not 413
    s.close()


@pytest.mark.parametrize(
    "head",
    [
        b"POST /h/%s HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"POST /h/%s HTTP/1.1\r\n\r\n",
        b"POST /h/%s HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
        b"POST /h/%s HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
    ],
)
def test_framing_errors_never_reach_the_inbox(lst, head):
    ls, _st, _ib, seen = lst()
    assert status(raw(ls, head % TOKEN.encode())) in (400, 411)
    assert seen == []


def test_slow_sender_is_cut_at_the_total_deadline_not_per_read(lst):
    ls, _st, _ib, seen = lst(total_s=0.6)
    s = socket.create_connection(ls.address, timeout=5)
    t0 = time.monotonic()
    try:
        for b in post():
            s.sendall(bytes([b]))
            time.sleep(0.1)  # every single read is "fast", the whole is not
            s.settimeout(0.01)
            try:
                if s.recv(100):
                    break
            except TimeoutError, BlockingIOError:
                pass
            except ConnectionError:
                break
            if time.monotonic() - t0 > 3:
                break
    except OSError:
        pass
    finally:
        s.close()
    assert time.monotonic() - t0 < 2.5 and seen == []


def test_pool_is_small_and_overflow_is_refused_at_once(lst):
    ls, _st, _ib, _seen = lst(total_s=2.0, workers=1)
    hogs = [socket.create_connection(ls.address, timeout=5) for _ in range(1 + LS.BACKLOG)]  # 1 busy + a full queue
    time.sleep(0.2)
    resp = raw(ls, post())
    assert status(resp) == 503
    for h in hogs:
        h.close()


def test_never_reads_cookie_or_bearer(lst):
    ls, _st, _ib, seen = lst()
    extra = b"Cookie: orch_session=abc\r\nAuthorization: Bearer " + SECRET.encode() + b"\r\n"
    assert status(raw(ls, post(extra=extra))) == 204  # the github signature decides; the bearer is no credential
    headers = seen[0][0]
    assert "cookie" not in headers and set(headers) <= LS.KEEP
    # a bearer is not a signature either
    assert status(raw(ls, post(signature="Bearer " + SECRET, extra=extra))) == 404


def test_module_imports_no_session_channel_or_admin_code():
    import ast

    for path in Path(LS.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [(node.module or ""), *[a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for n in names:
                assert not any(w in n for w in ("channels", "access", "webapi", "custodial", "keys", "switch")), (
                    path,
                    n,
                )


def test_source_comes_from_the_edge_only_when_the_peer_is_trusted(lst):
    ls, _st, _ib, seen = lst(trusted=frozenset({"127.0.0.1"}))
    raw(ls, post(extra=b"X-Forwarded-For: 6.6.6.6, 203.0.113.9\r\n"))
    assert seen[0][2] == "203.0.113.9"
    ls2, _st2, _ib2, seen2 = lst()
    raw(ls2, post(extra=b"X-Forwarded-For: 203.0.113.9\r\n"))
    assert seen2[0][2] == "127.0.0.1"


def test_request_and_secrets_are_never_logged(lst, caplog):
    caplog.set_level(logging.DEBUG)
    ls, _st, _ib, _seen = lst()
    raw(ls, post(extra=b"Cookie: orch_session=abc\r\n"))
    raw(ls, post(token="hh_" + "U" * 43))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (TOKEN, SECRET, "orch_session", "hh_" + "U" * 43, BODY.decode()):
        assert secret not in text


def test_public_url_parsing():
    assert LS.parse_public_url("https://hub.example.com", dev=False) == "https://hub.example.com/h/"
    assert LS.parse_public_url("http://127.0.0.1:8790", dev=True) == "http://127.0.0.1:8790/h/"
    for bad in (
        "http://hub.example.com",
        "https://hub.example.com/x",
        "https://u@hub.example.com",
        "hub.example.com",
        "https://h/?q",
    ):
        with pytest.raises(ValueError):
            LS.parse_public_url(bad, dev=False)
    with pytest.raises(ValueError):
        LS.parse_public_url("ftp://x", dev=True)


def test_threads_are_bounded(lst):
    before = threading.active_count()
    ls, *_ = lst(workers=4)
    assert threading.active_count() - before <= 5
    ls.close()
