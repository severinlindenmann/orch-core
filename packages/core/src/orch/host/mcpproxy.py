"""Company MCP proxy (M3 H-4, Opus host conditions 4, 8, 9, 11).

A loopback-only HTTP/1.1 listener. An agent reaches a company MCP server only through ``/m/<name>`` with a per-run
capability. The proxy talks to the real server itself (injected ``upstream`` factory), keeps the upstream session,
filters what comes back (only owner-approved tools, sanitized text, no server->client traffic) and scrubs secrets.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from .mcpshared import CAPABILITY_SHAPE, new_capability, server_name_ok, tool_pin, tool_view

__all__ = ["Proxy"]

log = logging.getLogger("orch.host.mcpproxy")

CAPABILITY_TTL = 24 * 3600.0
ISOLATION_CACHE = 60.0
HEADER_MAX = 16 * 1024
HEADERS_MAX = 64
BODY_MAX = 1024 * 1024
READ_TIMEOUT = 10.0
RESPONSE_MAX = 4 * 1024 * 1024
UPSTREAM_TIMEOUT = 30
CALLS_PER_MINUTE = 60
CALLS_PER_CAPABILITY = 2000
INFLIGHT_MAX = 4
TOOLS_MAX = 200
TOOLS_BYTES_MAX = 256 * 1024
ALLOWED_METHODS = ("initialize", "ping", "tools/list", "tools/call")
_REASONS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Payload Too Large",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class _Redact(logging.Filter):
    """Replace capability-shaped strings and every secret the proxy currently holds."""

    def __init__(self, held: Callable[[], list[str]]) -> None:
        super().__init__()
        self._held = held

    def scrub(self, text: str) -> str:
        text = CAPABILITY_SHAPE.sub("[redacted]", text)
        for value in sorted(self._held(), key=len, reverse=True):
            if value:
                text = text.replace(value, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = self.scrub(msg)
        record.args = None
        return True


class _Refuse(Exception):
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text


class _Cap:
    """One capability record (held under its hash only)."""

    def __init__(
        self, key: str, ws: str, binding_seq: int, run: str, servers: dict[str, str], expires_at: float
    ) -> None:
        self.key = key
        self.ws = ws
        self.binding_seq = binding_seq
        self.run = run
        self.servers = servers
        self.expires_at = expires_at
        self.upstreams: dict[str, tuple[Any, str | None]] = {}
        self.pins: dict[str, dict[str, str]] = {}  # server -> latest {tool: pin} (present once listed)
        self.inflight: dict[str, int] = {}
        self.calls = 0
        self.list_n = 0


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _visible_ascii(text: object, limit: int) -> bool:
    return type(text) is str and 0 < len(text) <= limit and all(0x21 <= ord(c) <= 0x7E for c in text)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _secret_forms(value: str) -> list[str]:
    raw = value.encode()
    forms = {value, urllib.parse.quote(value, safe="")}
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        padded = enc(raw).decode()
        forms.add(padded)
        forms.add(padded.rstrip("="))
    forms.discard("")
    return sorted(forms, key=len, reverse=True)


def _scrub(obj: Any, forms: list[str]) -> Any:
    def fix(text: str) -> str:
        for form in forms:
            if form in text:
                text = text.replace(form, "[redacted]")
        return text

    if type(obj) is str:
        return fix(obj)
    if isinstance(obj, dict):
        return {(fix(k) if type(k) is str else k): _scrub(v, forms) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, forms) for v in obj]
    return obj


class Proxy:
    def __init__(
        self,
        *,
        upstream: Callable[[dict, str | None], Any],
        served: Callable[[str], list[dict]],
        tool_policy: Callable[[str, str], dict[str, str]],
        isolated: Callable[[str], bool],
        secret: Callable[[str, str], str | None],
        on_seen: Callable[[str, str, str, list[dict]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._upstream = upstream
        self._served = served
        self._tool_policy = tool_policy
        self._isolated = isolated
        self._secret = secret
        self._on_seen = on_seen
        self._clock = clock
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._lock = threading.RLock()
        self._caps: dict[str, _Cap] = {}
        self._iso: dict[str, tuple[bool, float]] = {}
        self._runs: dict[str, collections.deque] = {}
        self._usage: dict[tuple[str, str, str, str], int] = {}
        self._held: set[str] = set()
        self._filter = _Redact(lambda: list(self._held))
        log.addFilter(self._filter)

    # -- capabilities ---------------------------------------------------------------------------------------------

    @staticmethod
    def _hash(cap: str) -> str:
        return hashlib.sha256(cap.encode()).hexdigest()

    def mint(self, ws: str, binding_seq: int, run: str, servers: list[dict]) -> str:
        if not servers:
            raise ValueError("no servers")
        names: dict[str, str] = {}
        for rec in servers:
            if not isinstance(rec, dict) or not server_name_ok(rec.get("name")):
                raise ValueError("bad server name")
            names[rec["name"]] = str(rec.get("digest", ""))
        cap = new_capability()
        with self._lock:
            key = self._hash(cap)
            self._caps[key] = _Cap(key, ws, binding_seq, run, names, self._clock() + CAPABILITY_TTL)
        return cap

    def _drop(self, key: str) -> None:
        rec = self._caps.pop(key, None)
        if rec is not None:
            for up, _ in rec.upstreams.values():
                self._close_up(up)
            rec.upstreams.clear()

    @staticmethod
    def _close_up(up: Any) -> None:
        try:
            up.close()
        except Exception:
            pass

    def revoke_run(self, run: str) -> None:
        with self._lock:
            for key in [k for k, c in self._caps.items() if c.run == run]:
                self._drop(key)
            self._runs.pop(run, None)

    def revoke_workspace(self, ws: str) -> None:
        with self._lock:
            for key in [k for k, c in self._caps.items() if c.ws == ws]:
                self._drop(key)
            self._iso.pop(ws, None)

    def revoke_server(self, ws: str, name: str) -> None:
        with self._lock:
            for key in [k for k, c in self._caps.items() if c.ws == ws and name in c.servers]:
                cap = self._caps[key]
                del cap.servers[name]
                held = cap.upstreams.pop(name, None)
                cap.pins.pop(name, None)
                if held is not None:
                    self._close_up(held[0])
                if not cap.servers:
                    self._drop(key)

    def usage(self) -> dict:
        with self._lock:
            return dict(self._usage)

    def _lookup(self, token: str) -> tuple[str, _Cap]:
        key = self._hash(token)
        with self._lock:
            cap = self._caps.get(key)
            if cap is None or not hmac.compare_digest(cap.key, key):
                raise _Refuse(401, "unauthorized")
            if self._clock() >= cap.expires_at:
                self._drop(key)
                raise _Refuse(401, "unauthorized")
            return key, cap

    # -- lifecycle ------------------------------------------------------------------------------------------------

    async def start(self) -> int:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=HEADER_MAX)
            self.port = self._server.sockets[0].getsockname()[1]
        assert self.port is not None
        return self.port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except Exception:
                pass
        with self._lock:
            for key in list(self._caps):
                self._drop(key)
            self._iso.clear()
            self._runs.clear()
            self._held.clear()
        self.port = None
        log.removeFilter(self._filter)

    # -- HTTP -----------------------------------------------------------------------------------------------------

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, payload: Any | None) -> None:
        body = b"" if payload is None else _dumps(payload).encode()
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nConnection: close\r\nContent-Length: {len(body)}\r\n"
        )
        if payload is not None:
            head += "Content-Type: application/json\r\n"
        writer.write(head.encode() + b"\r\n" + body)
        try:
            await writer.drain()
        except Exception:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, payload = await self._serve(reader, writer)
        except _Refuse as exc:
            status, payload = exc.status, {"error": exc.text}
        except Exception:
            log.warning("proxy request failed")
            status, payload = 500, {"error": "internal error"}
        try:
            await self._send(writer, status, payload)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[int, Any]:
        peer = writer.get_extra_info("peername")
        try:
            if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
                raise _Refuse(403, "forbidden")
        except ValueError:
            raise _Refuse(403, "forbidden") from None
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
        except TimeoutError:
            raise _Refuse(408, "timeout") from None
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            raise _Refuse(431, "request too large") from None
        if len(raw) > HEADER_MAX:
            raise _Refuse(431, "request too large")
        try:
            lines = raw[:-4].decode("latin-1").split("\r\n")
        except Exception:
            raise _Refuse(400, "bad request") from None
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise _Refuse(400, "bad request")
        method, target = parts[0], parts[1]
        if len(lines) - 1 > HEADERS_MAX:
            raise _Refuse(431, "request too large")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise _Refuse(400, "bad request")
            name = name.lower()
            if name in headers and name in ("host", "authorization", "content-length", "origin"):
                raise _Refuse(400, "bad request")
            headers[name] = value.strip()
        if "transfer-encoding" in headers:
            raise _Refuse(400, "bad request")
        if headers.get("host") != f"127.0.0.1:{self.port}":
            raise _Refuse(403, "forbidden")
        if "origin" in headers:
            raise _Refuse(403, "forbidden")
        if method != "POST":
            raise _Refuse(405, "method not allowed")
        prefix = "/m/"
        name = target[len(prefix) :] if target.startswith(prefix) else ""
        if not server_name_ok(name):
            raise _Refuse(404, "not found")
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 200:
            raise _Refuse(401, "unauthorized")
        key, cap = self._lookup(auth[7:])
        if name not in cap.servers:
            raise _Refuse(403, "forbidden")
        record = self._check_served(cap, name)
        bearer, secret_value = self._credentials(cap, record)
        length = headers.get("content-length")
        if length is None:
            raise _Refuse(411, "length required")
        if not length.isdigit() or len(length) > 9:
            raise _Refuse(400, "bad request")
        if int(length) > BODY_MAX:
            raise _Refuse(413, "too large")
        try:
            body = await asyncio.wait_for(reader.readexactly(int(length)), READ_TIMEOUT)
        except TimeoutError:
            raise _Refuse(408, "timeout") from None
        except asyncio.IncompleteReadError:
            raise _Refuse(400, "bad request") from None
        status, payload = await self._rpc(key, cap, name, record, bearer, body)
        if payload is not None and secret_value:
            payload = _scrub(payload, _secret_forms(secret_value))
        if payload is not None and len(_dumps(payload)) > RESPONSE_MAX:
            rid = payload.get("id") if isinstance(payload, dict) else None
            return 200, _rpc_error(rid, -32603, "the answer was too large")
        return status, payload

    # -- per request checks ---------------------------------------------------------------------------------------

    def _check_served(self, cap: _Cap, name: str) -> dict:
        try:
            records = self._served(cap.ws)
        except Exception:
            raise _Refuse(503, "unavailable") from None
        rec = next((r for r in records if isinstance(r, dict) and r.get("name") == name), None)
        if rec is None or rec.get("digest") != cap.servers.get(name):
            self.revoke_server(cap.ws, name)
            raise _Refuse(403, "no longer available")
        return rec

    def _isolated_now(self, ws: str) -> bool:
        now = self._clock()
        with self._lock:
            hit = self._iso.get(ws)
            if hit is not None and now - hit[1] < ISOLATION_CACHE:
                return hit[0]
        try:
            value = self._isolated(ws) is True
        except Exception:
            return False
        with self._lock:
            self._iso[ws] = (value, now)
        return value

    def _credentials(self, cap: _Cap, record: dict) -> tuple[str | None, str | None]:
        if record.get("auth") != "hub_secret":
            return None, None
        if not self._isolated_now(cap.ws):
            raise _Refuse(403, "forbidden")
        try:
            value = self._secret(cap.ws, record.get("secret"))
        except Exception:
            value = None
        if type(value) is not str or not value:
            raise _Refuse(503, "unavailable")
        self._held.add(value)
        return value, value

    def _count(self, cap: _Cap) -> None:
        now = self._clock()
        with self._lock:
            window = self._runs.setdefault(cap.run, collections.deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= CALLS_PER_MINUTE or cap.calls >= CALLS_PER_CAPABILITY:
                raise _Refuse(429, "too many calls")
            window.append(now)
            cap.calls += 1

    # -- JSON-RPC -------------------------------------------------------------------------------------------------

    def _up(self, cap: _Cap, name: str, record: dict, bearer: str | None) -> Any:
        held = cap.upstreams.get(name)
        if held is not None and held[1] == bearer:
            return held[0]
        if held is not None:
            self._close_up(held[0])
            cap.pins.pop(name, None)
        up = self._upstream(record, bearer)
        cap.upstreams[name] = (up, bearer)
        return up

    async def _forward(self, key: str, cap: _Cap, name: str, up: Any, message: dict) -> list[dict]:
        self._count(cap)
        if cap.inflight.get(name, 0) >= INFLIGHT_MAX:
            raise _Refuse(429, "too many calls")
        cap.inflight[name] = cap.inflight.get(name, 0) + 1
        try:
            msgs = await asyncio.to_thread(up.post, message, timeout=UPSTREAM_TIMEOUT)
        except Exception:
            log.warning("company server %s did not answer", name)
            return []
        finally:
            cap.inflight[name] -= 1
        return msgs if isinstance(msgs, list) else []

    @staticmethod
    def _answer(msgs: list[dict], rid: Any) -> dict | None:
        for m in msgs:
            if (
                isinstance(m, dict)
                and "method" not in m
                and "id" in m
                and type(m["id"]) is type(rid)
                and m["id"] == rid
                and ("result" in m or "error" in m)
            ):
                return m
        return None

    @staticmethod
    def _upstream_error(rid: Any, answer: dict) -> dict:
        err = answer.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        if type(code) is not int or not -32768 <= code <= 32767:
            code = -32603
        return _rpc_error(rid, code, "the company server returned an error")

    async def _rpc(
        self, key: str, cap: _Cap, name: str, record: dict, bearer: str | None, body: bytes
    ) -> tuple[int, Any]:
        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):
            return 400, _rpc_error(None, -32700, "parse error")
        if isinstance(msg, list):
            return 200, _rpc_error(None, -32600, "batches are not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or type(msg.get("method")) is not str:
            return 200, _rpc_error(None, -32600, "invalid request")
        method = msg["method"]
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return 200, _rpc_error(msg.get("id"), -32602, "invalid params")
        params = params or {}
        if "id" not in msg:
            if method == "notifications/initialized":
                up = self._up(cap, name, record, bearer)
                await self._forward(key, cap, name, up, {"jsonrpc": "2.0", "method": method})
            return 202, None
        rid = msg["id"]
        if type(rid) not in (str, int):
            return 200, _rpc_error(None, -32600, "invalid request")
        if method not in ALLOWED_METHODS:
            return 200, _rpc_error(rid, -32601, "method not found")
        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}
        up = self._up(cap, name, record, bearer)
        if method == "initialize":
            return 200, await self._initialize(key, cap, name, up, rid, params)
        if method == "tools/list":
            return 200, await self._tools_list(key, cap, name, record, up, rid)
        return 200, await self._tools_call(key, cap, name, up, rid, params)

    async def _initialize(self, key: str, cap: _Cap, name: str, up: Any, rid: Any, params: dict) -> dict:
        asked = params.get("protocolVersion")
        asked = asked if _visible_ascii(asked, 32) else "2025-06-18"
        message = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "initialize",
            "params": {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "orch", "version": "1"}},
        }
        answer = self._answer(await self._forward(key, cap, name, up, message), rid)
        if answer is None:
            return _rpc_error(rid, -32603, "the company server did not answer")
        if "error" in answer:
            return self._upstream_error(rid, answer)
        result = answer.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if not _visible_ascii(version, 32):
            version = asked
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": "1"},
            },
        }

    async def _fetch_tools(
        self, key: str, cap: _Cap, name: str, record: dict, up: Any, rid: Any
    ) -> tuple[list[dict] | None, dict | None]:
        """Ask upstream for its tools. Returns (sanitized tools, error response) and updates the session pins."""
        answer = self._answer(
            await self._forward(
                key, cap, name, up, {"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}}
            ),
            rid,
        )
        if answer is None:
            return None, _rpc_error(rid, -32603, "the company server did not answer")
        if "error" in answer:
            return None, self._upstream_error(rid, answer)
        result = answer.get("result")
        raw = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(raw, list):
            return None, _rpc_error(rid, -32603, "the company server did not answer")
        tools: list[dict] = []  # good tools: the view plus its pin
        seen_rows: list[dict] = []  # what the owner's page gets: good tools and too_long ones
        seen: set[str] = set()
        size = 0
        for item in raw:
            if len(seen_rows) >= TOOLS_MAX:
                break
            view, problem = tool_view(item)
            if view is None or view["name"] in seen:
                continue  # not a tool, or hidden characters: skipped
            if problem is None:
                row = {**view, "pin": tool_pin(view)}  # approved == pinned == forwarded: one view
            else:
                row = {"name": view["name"], "problem": problem}
            size += len(_dumps(row))
            if size > TOOLS_BYTES_MAX:
                break
            seen.add(view["name"])
            seen_rows.append(row)
            if problem is None:
                tools.append(row)
        cap.pins[name] = {t["name"]: t["pin"] for t in tools}
        if self._on_seen is not None:
            try:
                self._on_seen(cap.ws, name, str(record.get("digest", "")), seen_rows)
            except Exception:
                pass
        return tools, None

    def _policy(self, cap: _Cap, name: str) -> dict[str, str]:
        try:
            policy = self._tool_policy(cap.ws, name)
        except Exception:
            return {}
        return policy if isinstance(policy, dict) else {}

    async def _tools_list(self, key: str, cap: _Cap, name: str, record: dict, up: Any, rid: Any) -> dict:
        tools, error = await self._fetch_tools(key, cap, name, record, up, rid)
        if error is not None or tools is None:
            return error or _rpc_error(rid, -32603, "the company server did not answer")
        policy = self._policy(cap, name)
        shown = []
        for t in tools:
            if policy.get(t["name"]) == t["pin"]:
                shown.append({k: v for k, v in t.items() if k != "pin"})  # the view the pin covers
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": shown}}

    async def _tools_call(self, key: str, cap: _Cap, name: str, up: Any, rid: Any, params: dict) -> dict:
        tool = params.get("name")
        off = _rpc_error(rid, -32602, "this tool is off")
        if type(tool) is not str:
            return _rpc_error(rid, -32602, "invalid params")
        if tool not in self._policy(cap, name):
            return off
        if name not in cap.pins:
            cap.list_n += 1
            _, error = await self._fetch_tools(
                key, cap, name, self._served_record(cap, name), up, f"orch-list-{cap.list_n}"
            )
            if error is not None:
                return _rpc_error(rid, -32603, "the company server did not answer")
        pin = cap.pins.get(name, {}).get(tool)
        if pin is None or self._policy(cap, name).get(tool) != pin:
            return off
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            k = (cap.ws, name, tool, day)
            self._usage[k] = self._usage.get(k, 0) + 1
        arguments = params.get("arguments")
        message = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments if isinstance(arguments, dict) else {}},
        }
        answer = self._answer(await self._forward(key, cap, name, up, message), rid)
        if answer is None:
            return _rpc_error(rid, -32603, "the company server did not answer")
        if "error" in answer:
            return self._upstream_error(rid, answer)
        result = answer.get("result")
        if not isinstance(result, dict):
            return _rpc_error(rid, -32603, "the company server did not answer")
        clean = {k: result[k] for k in ("content", "structuredContent", "isError") if k in result}
        return {"jsonrpc": "2.0", "id": rid, "result": clean}

    def _served_record(self, cap: _Cap, name: str) -> dict:
        return {"name": name, "digest": cap.servers.get(name, "")}
