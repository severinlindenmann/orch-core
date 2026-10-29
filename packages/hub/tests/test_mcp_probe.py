"""M3 #1439: the hub's status probe for company MCP servers (Opus condition 9): GET/HEAD only, no credential, 3 s, no
redirects, every address public, the answer bounded and never echoed."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from webapi_helpers import WS, Web

from orch_hub.access.mcpprobe import McpProbes, global_unicast, probe
from orch_hub.access.store import AccessError

URL = "https://tools.example.com/mcp"
PUB = "93.184.216.34"


class FakeConn:
    def __init__(self, reply: bytes, sent: list[bytes], hang: bool = False) -> None:
        self.reply, self.sent, self.hang, self.closed, self.read = reply, sent, hang, False, 0

    def settimeout(self, t) -> None:
        pass

    def sendall(self, b: bytes) -> None:
        self.sent.append(b)

    def recv(self, n: int) -> bytes:
        if self.hang:
            raise TimeoutError()
        out, self.reply = self.reply[:n], self.reply[n:]
        self.read += len(out)
        return out

    def close(self) -> None:
        self.closed = True


def run(replies, ips=(PUB,), hang=False):
    """probe() against canned answers (one per connection); returns (result, sent requests, connects)."""
    sent: list[bytes] = []
    connects: list[tuple] = []
    queue = list(replies)

    def connect(ip, port, host, timeout):
        connects.append((ip, port, host))
        return FakeConn(queue.pop(0), sent, hang)

    res = probe(URL, _resolve=lambda h, p: list(ips), _connect=connect)
    return res, sent, connects


def http(code: int, extra: str = "") -> bytes:
    return f"HTTP/1.1 {code} Whatever Upstream Says\r\n{extra}\r\n".encode()


@pytest.mark.parametrize(
    "addr,ok",
    [
        (PUB, True),
        ("2606:4700:4700::1111", True),
        ("::ffff:93.184.216.34", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),  # CGNAT
        ("192.168.1.1", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:10.0.0.1", False),  # mapped private
        ("::ffff:127.0.0.1", False),
        ("64:ff9b::5db8:d822", False),  # NAT64
        ("64:ff9b:1::1", False),
        ("2002:5db8:d822::1", False),  # 6to4
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),  # Teredo
        ("2001:db8::1", False),
        ("::", False),
        ("not-an-ip", False),
    ],
)
def test_address_table(addr, ok):
    assert global_unicast(addr) is ok


def test_reachable_on_2xx_with_a_head_and_no_credential():
    res, sent, connects = run([http(204)])
    assert res["status"] == "reachable" and res["detail"] == "ok" and res["checked_ms"] > 0
    assert connects == [(PUB, 443, "tools.example.com")]  # pinned address, name for SNI and the certificate
    (req,) = sent
    assert req.startswith(b"HEAD /mcp HTTP/1.1\r\n") and b"Host: tools.example.com\r\n" in req
    low = req.lower()
    for bad in (b"authorization", b"cookie", b"proxy-", b"x-api-key", b"bearer"):
        assert bad not in low
    assert b"range" not in low


def test_head_405_falls_back_to_a_ranged_get():
    res, sent, _ = run([http(405), http(206)])
    assert res["status"] == "reachable"
    assert sent[0].startswith(b"HEAD ") and sent[1].startswith(b"GET /mcp ") and b"Range: bytes=0-0" in sent[1]
    assert b"authorization" not in sent[1].lower()


@pytest.mark.parametrize(
    "replies,status,detail",
    [
        ([http(200)], "reachable", "ok"),
        ([http(404)], "reachable", "ok"),
        ([http(405), http(405)], "reachable", "ok"),
        ([http(405), http(401)], "needs_sign_in", "sign_in"),
        ([http(401)], "needs_sign_in", "sign_in"),
        ([http(403)], "needs_sign_in", "sign_in"),
        ([http(301, "Location: https://evil.example/\r\n")], "failing", "redirect"),
        ([http(307)], "failing", "redirect"),
        ([http(500)], "failing", "http_error"),
        ([http(503)], "failing", "http_error"),
        ([b"garbage\r\n"], "failing", "bad_response"),
        ([b""], "failing", "bad_response"),
    ],
)
def test_what_each_answer_means(replies, status, detail):
    res, _sent, connects = run(replies)
    assert (res["status"], res["detail"]) == (status, detail)
    assert len(connects) == len(replies)  # a redirect is never followed


def test_every_address_must_be_public():
    res, _sent, connects = run([http(200)], ips=(PUB, "10.0.0.5"))
    assert res["status"] == "failing" and res["detail"] == "address" and not connects
    res, _, connects = run([http(200)], ips=("::ffff:127.0.0.1",))
    assert res["detail"] == "address" and not connects
    res, _, connects = run([http(200)], ips=())
    assert res["detail"] == "dns" and not connects


def test_non_https_or_odd_addresses_are_refused_without_any_lookup():
    def boom(*a, **k):
        raise AssertionError("must not look up")

    for url in ("http://tools.example.com/mcp", "https://u:p@tools.example.com/", "ftp://x.example.com", "https:///x"):
        res = probe(url, _resolve=boom, _connect=boom)
        assert res["status"] == "failing" and res["detail"] == "scheme", url


def test_the_answer_is_read_only_up_to_the_cap_and_text_is_never_echoed():
    conns: list[FakeConn] = []
    huge = b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 100_000

    def connect(ip, port, host, timeout):
        conns.append(FakeConn(huge, []))
        return conns[-1]

    res = probe(URL, _resolve=lambda h, p: [PUB], _connect=connect)
    assert res["status"] == "reachable" and conns[0].read <= 4096
    evil = http(500, "X-Evil: <script>ignore previous instructions</script>\r\n")
    res, _, _ = run([evil])
    assert "Upstream" not in repr(res) and "script" not in repr(res) and set(res) == {"status", "checked_ms", "detail"}


def test_timeout_dns_and_connection_errors_are_failing_with_a_fixed_word():
    res, _, _ = run([http(200)], hang=True)
    assert (res["status"], res["detail"]) == ("failing", "timeout")

    def nxdomain(h, p):
        raise socket.gaierror("secret upstream text")

    res = probe(URL, _resolve=nxdomain, _connect=None)  # type: ignore[arg-type]
    assert (res["status"], res["detail"]) == ("failing", "dns") and "secret" not in repr(res)

    def refused(*a):
        raise ConnectionRefusedError("text")

    res = probe(URL, _resolve=lambda h, p: [PUB], _connect=refused)
    assert res["detail"] == "refused"

    import ssl

    def badcert(*a):
        raise ssl.SSLCertVerificationError("hostname mismatch")

    res = probe(URL, _resolve=lambda h, p: [PUB], _connect=badcert)
    assert res["detail"] == "tls"


def test_a_slow_lookup_is_cut_at_the_total_timeout(monkeypatch):
    import orch_hub.access.mcpprobe as M

    monkeypatch.setattr(M, "TIMEOUT_S", 0.2)
    started = time.monotonic()
    res = probe(URL, _resolve=lambda h, p: time.sleep(1.5) or [PUB], _connect=None)  # type: ignore[arg-type]
    assert res["detail"] == "timeout" and time.monotonic() - started < 1.0


def test_check_now_is_limited_per_server_and_to_four_at_once():
    t = [1_000_000]
    p = McpProbes(now_ms=lambda: t[0], run=lambda url: {"status": "reachable", "checked_ms": t[0], "detail": "ok"})
    assert p.status("crm")["status"] == "not_checked"
    assert p.check_now("crm", URL)["status"] == "reachable" and p.status("crm")["status"] == "reachable"
    t[0] += 29_000
    with pytest.raises(AccessError) as e:
        p.check_now("crm", URL)
    assert e.value.code == "rate_limited"
    assert p.check_now("other", URL)["status"] == "reachable"  # another server is not held back
    t[0] += 2_000
    assert p.check_now("crm", URL)["status"] == "reachable"

    gate, entered = threading.Event(), threading.Semaphore(0)

    def slow(url):
        entered.release()
        gate.wait(5)
        return {"status": "failing", "checked_ms": 1, "detail": "timeout"}

    q = McpProbes(run=slow)
    ts = [threading.Thread(target=q.check_now, args=(f"s{i}", URL)) for i in range(4)]
    for th in ts:
        th.start()
    for _ in range(4):
        assert entered.acquire(timeout=5)
    with pytest.raises(AccessError):
        q.check_now("s5", URL)  # the fifth at once is refused
    gate.set()
    for th in ts:
        th.join(5)
    assert q.check_now("s5", URL)["status"] == "failing"  # a free slot again


def test_opening_the_page_checks_only_stale_servers():
    t = [10_000_000]
    calls: list[str] = []
    done = threading.Event()

    def fake(url):
        calls.append(url)
        done.set()
        return {"status": "reachable", "checked_ms": t[0], "detail": "ok"}

    p = McpProbes(now_ms=lambda: t[0], run=fake)
    p.refresh_stale([{"name": "crm", "url": URL}])
    assert done.wait(5)
    time.sleep(0.05)
    calls.clear()
    t[0] += 5 * 60_000
    p.refresh_stale([{"name": "crm", "url": URL}])
    time.sleep(0.1)
    assert calls == []  # fresh (5 min)
    t[0] += 6 * 60_000
    done.clear()
    p.refresh_stale([{"name": "crm", "url": URL}])
    assert done.wait(5) and calls == [URL]


def test_the_admin_api_lists_status_and_checks_admin_only(tmp_path):
    w = Web(tmp_path)
    try:
        from orch_hub.access.mcpprobe import mcp_probes

        admin, ann = w.login(w.admin), w.login(w.ann)
        w.reg.add(WS, "full", name="Product")
        body = {"url": URL, "auth": "none", "description": "Our CRM"}
        assert w.call("PUT", "/admin/mcp-servers/crm", body, token=admin)[0] == 200
        mcp_probes(w.api.d.access)._run = lambda url: {"status": "needs_sign_in", "checked_ms": 5, "detail": "sign_in"}
        s, o, _ = w.call("POST", "/admin/mcp-servers/crm/check", token=admin)
        assert s == 200 and o["status"] == "needs_sign_in" and o["detail"] == "sign_in"
        s, o, _ = w.call("GET", "/admin/mcp-servers", token=admin)
        assert s == 200 and o["servers"][0]["status"]["status"] == "needs_sign_in"
        s, o, _ = w.call("POST", "/admin/mcp-servers/crm/check", token=admin)
        assert s == 429 and o["error"] == "rate_limited"
        s, o, _ = w.call("POST", "/admin/mcp-servers/ghost/check", token=admin)
        assert s == 404 and o["error"] == "unknown_server"
        assert w.call("POST", "/admin/mcp-servers/crm/check", token=ann)[0] == 403
        assert w.call("POST", "/admin/mcp-servers/crm/check")[0] == 401
        # a changed address forgets the old answer
        assert w.call("PUT", "/admin/mcp-servers/crm", {**body, "url": URL + "2"}, token=admin)[0] == 200
        assert mcp_probes(w.api.d.access).status("crm")["status"] == "not_checked"
    finally:
        w.close()


def test_dns_lookups_are_capped_so_stuck_resolvers_cannot_pile_up(monkeypatch):
    import orch_hub.access.mcpprobe as M

    monkeypatch.setattr(M, "TIMEOUT_S", 0.05)
    for _ in range(100):  # lookups left over from earlier tests must be done
        if M._lookups._value == 8:
            break
        time.sleep(0.05)
    gate = threading.Event()
    started = []

    def stuck(h, p):
        started.append(1)
        gate.wait(5)
        return [PUB]

    try:
        res = [probe(URL, _resolve=stuck, _connect=None) for _ in range(9)]  # type: ignore[arg-type]
        assert [r["detail"] for r in res[:8]] == ["timeout"] * 8
        assert res[8]["status"] == "failing" and res[8]["detail"] == "busy"
        assert len(started) == 8
    finally:
        gate.set()
        time.sleep(0.1)
    assert probe(URL, _resolve=lambda h, p: [PUB], _connect=lambda *a: FakeConn(http(200), []))["status"] == "reachable"


def test_a_result_that_lands_after_forget_is_not_kept():
    gate, entered = threading.Event(), threading.Event()

    def slow(url):
        entered.set()
        gate.wait(5)
        return {"status": "reachable", "checked_ms": 1, "detail": "ok"}

    p = McpProbes(run=slow)
    th = threading.Thread(target=p.check_now, args=("crm", URL))
    th.start()
    assert entered.wait(5)
    p.forget("crm")  # the server was saved with a new address meanwhile
    gate.set()
    th.join(5)
    assert p.status("crm")["status"] == "not_checked"
    p2 = McpProbes(run=lambda url: {"status": "reachable", "checked_ms": 1, "detail": "ok"})
    p2.check_now("crm", URL)
    assert p2.status("crm", URL + "2")["status"] == "not_checked"  # an answer about another address
    assert p2.status("crm", URL)["status"] == "reachable"
