"""App frames and request / response heads, from the device's side (hub contract 10.2c "Inside a session", rows 61-66).

Plaintext of one Noise transport message: ``kind (1) || id (4, big-endian) || payload``. This module builds request
frames from what a browser sent -- allow-listed headers only -- and parses what the host sends back, with the contract's
exact rules, so a frame outside them is :class:`ClientError` ``malformed`` (the stream closes)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from orch.canon.jcs import JcsError, dumps, loads_strict

from .codes import ClientError

__all__ = [
    "BODY_CHUNK",
    "ALLOWED_REQUEST_HEADERS",
    "KINDS",
    "build_request_head",
    "clean_response_headers",
    "frame",
    "parse_response_head",
    "split_kind",
]

KINDS = {
    "HEAD": 1,
    "BODY": 2,
    "END": 3,
    "RESET": 4,
    "WS_OPEN": 5,
    "WS_TEXT": 6,
    "WS_BINARY": 7,
    "WS_CLOSE": 8,
    "PING": 9,
    "PONG": 10,
}
NAMES = {v: k for k, v in KINDS.items()}
PLAIN_MAX = 65535 - 16
BODY_CHUNK = PLAIN_MAX - 5  # 65514
HEAD_MAX = 16384
HEADERS_MAX = 64
HEADER_NAME_MAX = 64
HEADER_VALUE_MAX = 4096
PATH_MAX = 2048
QUERY_MAX = 4096
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
RESETS = ("cancelled", "timeout", "too_large", "remote.busy")

#: what a browser request may carry into a stream (design section 1); everything else is dropped, never forwarded
ALLOWED_REQUEST_HEADERS = frozenset(
    {"accept", "accept-language", "content-type", "if-none-match", "if-modified-since", "range", "cache-control"}
)
#: framing belongs to the frames
_FRAMING = frozenset(
    {"content-length", "transfer-encoding", "connection", "upgrade", "keep-alive", "te", "trailer", "proxy-connection",
     "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"}
)  # fmt: skip
#: a response header the client never passes to the browser (it sets its own security headers)
_STRIP_RESPONSE = frozenset(
    {"service-worker-allowed", "content-security-policy", "content-security-policy-report-only"}
)
_TCHAR = re.compile(r"[a-z0-9!#$%&'*+\-.^_`|~]+")
_VALUE = re.compile(r"(?:[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?)?")
_PATH = re.compile(r"/[\x21-\x7e]*")
_QUERY = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*")


def frame(kind: str, ident: int, payload: bytes = b"") -> bytes:
    return bytes([KINDS[kind]]) + ident.to_bytes(4, "big") + payload


def split_kind(pt: bytes) -> tuple[str, int, bytes]:
    if type(pt) is not bytes or not 5 <= len(pt) <= PLAIN_MAX or pt[0] not in NAMES:
        raise ClientError("malformed")
    return NAMES[pt[0]], int.from_bytes(pt[1:5], "big"), pt[5:]


def clean_path(p: Any) -> bool:
    if type(p) is not str or not 1 <= len(p) <= PATH_MAX or _PATH.fullmatch(p) is None:
        return False
    if any(c in p for c in "%\\;?#"):
        return False
    return p == "/" or all(seg not in ("", ".", "..") for seg in p[1:].split("/"))


def build_request_head(method: str | None, path: str, query: str, headers: Iterable[tuple[str, str]]) -> bytes:
    """The payload of a ``HEAD`` (``method`` set) or ``WS_OPEN`` (``None``) frame: ``cj`` of exactly the contract's
    keys.
    Headers: lower-cased, allow-listed, at most one of each; a value outside the plain-ASCII rule is dropped. An
    ``Authorization`` / ``Cookie`` / ``X-Orch-*`` header (the UI sends them) is consumed here, never forwarded."""
    if (method is not None and method not in METHODS) or not clean_path(path):
        raise ClientError("malformed")
    if type(query) is not str or len(query) > QUERY_MAX or _QUERY.fullmatch(query) is None:
        raise ClientError("malformed")
    out: list[list[str]] = []
    seen: set[str] = set()
    for name, value in headers:
        n = str(name).lower()
        if n not in ALLOWED_REQUEST_HEADERS or n in seen or n in _FRAMING:
            continue
        v = str(value)
        if len(v) > HEADER_VALUE_MAX or _VALUE.fullmatch(v) is None:
            continue
        seen.add(n)
        out.append([n, v])
    head: dict[str, Any] = {"headers": out, "path": path, "query": query}
    if method is not None:
        head["method"] = method
    payload = dumps(head)
    if len(payload) > HEAD_MAX:
        raise ClientError("malformed")
    return payload


def parse_response_head(payload: bytes) -> tuple[int, list[tuple[str, str]]]:
    """``HEAD`` from the host: exactly ``{headers, status}``, canonical, ``status`` 101 or 200-599, header rules of the
    contract. A ``set-cookie`` is a violation (``malformed``): a stream credential never leaves the workspace's
    process."""
    if len(payload) > HEAD_MAX:
        raise ClientError("malformed")
    try:
        head = loads_strict(payload)
    except (JcsError, UnicodeDecodeError, ValueError):
        raise ClientError("malformed") from None
    if type(head) is not dict or set(head) != {"headers", "status"} or dumps(head) != payload:
        raise ClientError("malformed")
    st = head["status"]
    if type(st) is not int or not (st == 101 or 200 <= st <= 599):
        raise ClientError("malformed")
    hs = head["headers"]
    if type(hs) is not list or len(hs) > HEADERS_MAX:
        raise ClientError("malformed")
    out: list[tuple[str, str]] = []
    for pair in hs:
        if type(pair) is not list or len(pair) != 2 or not all(type(x) is str for x in pair):
            raise ClientError("malformed")
        n, v = pair
        if (
            not 1 <= len(n) <= HEADER_NAME_MAX
            or _TCHAR.fullmatch(n) is None
            or len(v) > HEADER_VALUE_MAX
            or _VALUE.fullmatch(v) is None
            or n in _FRAMING
            or n == "set-cookie"
        ):
            raise ClientError("malformed")
        out.append((n, v))
    return st, out


def clean_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """What may go on to the browser: no ``access-control-*``, no service-worker or CSP header (the client adds its
    own)."""
    return [(n, v) for n, v in headers if not n.startswith("access-control-") and n not in _STRIP_RESPONSE]
