"""The company MCP upstream client (H-4, condition 10): address rules, pinning, framing, caps."""

from __future__ import annotations

import json
import ssl

import pytest

from orch.host import mcpupstream
from orch.host.mcpupstream import MAX_RESPONSE, Upstream, UpstreamError, address_ok

GLOBAL = "93.184.216.34"


class FakeSock:
    def __init__(self, reply: bytes, chunk: int = 7000) -> None:
        self.reply, self.chunk, self.sent, self.closed = reply, chunk, b"", False

    def settimeout(self, t: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        out, self.reply = self.reply[: self.chunk], self.reply[self.chunk :]
        return out

    def close(self) -> None:
        self.closed = True


def resp(body: bytes = b"", status: str = "200 OK", ctype: str | None = "application/json", extra: str = "") -> bytes:
    h = f"HTTP/1.1 {status}\r\n"
    if ctype:
        h += f"Content-Type: {ctype}\r\n"
    h += f"Content-Length: {len(body)}\r\n{extra}\r\n"
    return h.encode() + body


def make(replies, url="https://mcp.example.com/mcp", bearer=None, ips=None):
    socks, calls = [], []
    queue = list(replies)

    def connect(ip, port, host, timeout):
        calls.append((ip, port, host))
        s = FakeSock(queue.pop(0))
        socks.append(s)
        return s

    up = Upstream(url, bearer, _resolve=lambda h: [GLOBAL] if ips is None else ips, _connect=connect)
    return up, socks, calls


MSG = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.mark.parametrize(
    "ip,ok",
    [
        (GLOBAL, True),
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:8.8.8.8", True),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("100.127.255.254", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("ff02::1", False),
        ("64:ff9b::808:808", False),
        ("64:ff9b:1::1", False),
        ("::ffff:0:7f00:1", False),
        ("fec0::1", False),
        ("5f00::1", False),
        ("2606:4700::1111", True),
        ("2002:808:808::1", False),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),
        ("::8.8.8.8", False),
        ("fe80::1%eth0", False),
        ("not an ip", False),
        ("", False),
    ],
)
def test_address_table(ip, ok):
    assert address_ok(ip) is ok


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/",
        "ftp://mcp.example.com/",
        "https://user:pw@mcp.example.com/",
        "https://@mcp.example.com/",
        "https:///path",
        "https://mcp.example.com:0/",
        "https://mcp.example.com:99999/",
        "https://mcp.example.com/#frag",
        "https://mcp.example.com/a b",
        "mcp.example.com",
        "",
    ],
)
def test_bad_urls_refused(url):
    with pytest.raises(UpstreamError) as e:
        Upstream(url, None)
    assert e.value.code == "address"


def test_private_and_mixed_resolution_refused():
    for ips in (["10.0.0.5"], [GLOBAL, "10.0.0.5"], ["127.0.0.1", GLOBAL], []):
        up, socks, calls = make([resp(b"{}")], ips=ips)
        with pytest.raises(UpstreamError) as e:
            up.post(MSG)
        assert e.value.code == "address"
        assert calls == []


def test_resolved_fresh_each_post():
    seen = []
    up = Upstream(
        "https://mcp.example.com/mcp",
        None,
        _resolve=lambda h: seen.append(h) or [GLOBAL],
        _connect=lambda *a: FakeSock(resp(b'{"id":1}')),
    )
    up.post(MSG)
    up.post(MSG)
    assert seen == ["mcp.example.com", "mcp.example.com"]


def test_request_shape_and_pinning():
    up, socks, calls = make([resp(b'{"jsonrpc":"2.0","id":1,"result":{}}')], url="https://mcp.example.com:8443/a/b?x=1")
    assert up.post(MSG) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert calls == [(GLOBAL, 8443, "mcp.example.com")]
    sent = socks[0].sent.decode()
    assert sent.startswith("POST /a/b?x=1 HTTP/1.1\r\n")
    assert "Host: mcp.example.com:8443\r\n" in sent
    assert "Connection: close\r\n" in sent
    assert "Accept: application/json, text/event-stream\r\n" in sent
    assert "Authorization" not in sent and "Mcp-Session-Id" not in sent
    assert json.loads(sent.split("\r\n\r\n", 1)[1]) == MSG
    assert socks[0].closed


def test_host_header_default_port_and_bearer():
    up, socks, _ = make([resp(b"{}")], bearer="tok123")
    up.post(MSG)
    sent = socks[0].sent.decode()
    assert "Host: mcp.example.com\r\n" in sent
    assert "Authorization: Bearer tok123\r\n" in sent


def test_bearer_with_newline_refused():
    up, socks, _ = make([resp(b"{}")], bearer="a\r\nX: y")
    with pytest.raises(UpstreamError):
        up.post(MSG)


def test_redirect_refused():
    up, _, _ = make([resp(b"", "302 Found", None, "Location: http://127.0.0.1/\r\n")])
    with pytest.raises(UpstreamError) as e:
        up.post(MSG)
    assert e.value.code == "redirect"


def test_http_error_never_leaks_body():
    marker = "SECRET-MARKER-xyz"
    up, _, _ = make([resp(marker.encode(), "500 Internal Server Error", "text/plain")])
    with pytest.raises(UpstreamError) as e:
        up.post(MSG)
    assert e.value.code == "http"
    assert marker not in str(e.value) and marker not in repr(e.value)


def test_json_object_and_array():
    up, _, _ = make([resp(b'{"id":1}'), resp(b'[{"id":1},{"id":2}]')])
    assert up.post(MSG) == [{"id": 1}]
    assert up.post(MSG) == [{"id": 1}, {"id": 2}]


def test_non_object_is_protocol_error():
    up, _, _ = make([resp(b"[1,2]"), resp(b"not json"), resp(b"{}", ctype="text/html")])
    for _ in range(3):
        with pytest.raises(UpstreamError) as e:
            up.post(MSG)
        assert e.value.code == "protocol"


def test_sse_two_events():
    body = b': comment\nevent: message\nid: 1\ndata: {"id":1}\n\ndata: {"a":\ndata: 2}\n\n'
    up, _, _ = make([resp(body, ctype="text/event-stream; charset=utf-8")])
    assert up.post(MSG) == [{"id": 1}, {"a": 2}]


def test_sse_bad_event():
    up, _, _ = make([resp(b'data: {"id":1}\n\ndata: oops\n\n', ctype="text/event-stream")])
    with pytest.raises(UpstreamError) as e:
        up.post(MSG)
    assert e.value.code == "protocol"


def test_empty_202_and_204():
    up, _, _ = make([resp(b"", "202 Accepted", None), resp(b"", "204 No Content", None)])
    assert up.post(MSG) == []
    assert up.post(MSG) == []


def test_chunked_body():
    body = b'{"id":1,"result":{"k":"vvvv"}}'
    chunked = b"%x\r\n%s\r\n%x;ext=1\r\n%s\r\n0\r\nTrailer: x\r\n\r\n" % (10, body[:10], len(body) - 10, body[10:])
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    up, _, _ = make([head + chunked])
    assert up.post(MSG) == [json.loads(body)]


def test_size_cap_content_length_and_close_delimited():
    big = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (MAX_RESPONSE + 1)
    unbounded = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + b" " * (MAX_RESPONSE + 10)
    chunk = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    chunk += b"%x\r\n" % (MAX_RESPONSE + 1)
    up, _, _ = make([big, unbounded, chunk])
    for _ in range(3):
        with pytest.raises(UpstreamError) as e:
            up.post(MSG)
        assert e.value.code == "size"


def test_request_size_cap():
    up, socks, calls = make([resp(b"{}")])
    with pytest.raises(UpstreamError) as e:
        up.post({"x": "a" * (1024 * 1024)})
    assert e.value.code == "size" and calls == []


def test_session_id_round_trip():
    up, socks, _ = make([resp(b"{}", extra="Mcp-Session-Id: sess-42\r\n"), resp(b"{}")])
    up.post(MSG)
    assert up.session_id == "sess-42"
    up.post(MSG)
    assert "Mcp-Session-Id: sess-42\r\n" in socks[1].sent.decode()
    up.close()
    assert up.session_id is None


def test_bad_session_id_ignored():
    long = "x" * 201
    up, _, _ = make([resp(b"{}", extra=f"Mcp-Session-Id: {long}\r\n"), resp(b"{}", extra="Mcp-Session-Id: a\x01b\r\n")])
    up.post(MSG)
    up.post(MSG)
    assert up.session_id is None


def test_timeout_and_truncated():
    class Slow(FakeSock):
        def recv(self, n):
            raise TimeoutError()

    up = Upstream("https://mcp.example.com/", None, _resolve=lambda h: [GLOBAL], _connect=lambda *a: Slow(b""))
    with pytest.raises(UpstreamError) as e:
        up.post(MSG)
    assert e.value.code == "timeout"
    up2, _, _ = make([b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort"])
    with pytest.raises(UpstreamError) as e:
        up2.post(MSG)
    assert e.value.code == "protocol"


def test_default_connector_pins_ip_and_verifies_name(monkeypatch):
    log = {}

    class Raw:
        def close(self):
            log["raw_closed"] = True

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            log["wrap"] = (sock, server_hostname)
            return "tls-sock"

    def create(addr, timeout=None):
        log["addr"], log["timeout"] = addr, timeout
        return Raw()

    monkeypatch.setattr(mcpupstream.socket, "create_connection", create)
    monkeypatch.setattr(mcpupstream, "_make_context", lambda: Ctx())
    out = mcpupstream._default_connect(GLOBAL, 443, "mcp.example.com", 5.0)
    assert out == "tls-sock"
    assert log["addr"] == (GLOBAL, 443)
    assert log["wrap"][1] == "mcp.example.com"


def test_default_context_is_strict():
    ctx = mcpupstream._make_context()
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_default_connector_errors(monkeypatch):
    def refuse(addr, timeout=None):
        raise ConnectionRefusedError()

    monkeypatch.setattr(mcpupstream.socket, "create_connection", refuse)
    with pytest.raises(UpstreamError) as e:
        mcpupstream._default_connect(GLOBAL, 443, "h", 1.0)
    assert e.value.code == "connect"

    class Raw:
        def close(self):
            pass

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("bad")

    monkeypatch.setattr(mcpupstream.socket, "create_connection", lambda a, timeout=None: Raw())
    monkeypatch.setattr(mcpupstream, "_make_context", lambda: Ctx())
    with pytest.raises(UpstreamError) as e:
        mcpupstream._default_connect(GLOBAL, 443, "h", 1.0)
    assert e.value.code == "tls"


def test_no_env_proxy_use():
    import inspect

    src = inspect.getsource(mcpupstream)
    assert "os.environ" not in src and "getproxies" not in src and "http.client" not in src
