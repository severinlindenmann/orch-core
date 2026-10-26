"""Exact-shape check of a ``publish_out`` request body (contracts/hub/protocol.md §10.2, §11.1).

The host sends ``{method, host, target, headers, body_hex}``. The hub forwards it unchanged, so anything that is not
exactly the shape orch-publish verifies is refused here (``malformed``), never repaired: unknown keys, unknown or
repeated headers, a hub header from the host, non-ASCII, control characters, upper-case hex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..contract.canon import CanonError, loads_strict, unb64u, unhex
from ..contract.hub import Refused

KEYS = frozenset({"method", "host", "target", "headers", "body_hex"})
METHODS = frozenset({"GET", "HEAD", "PUT", "POST", "DELETE", "PATCH"})
#: the only headers the host may send; ``Orch-Hub`` and ``Orch-Hub-Signature`` are the hub's alone
REQUIRED = ("orch-workspace", "orch-ts", "orch-nonce", "orch-signature")
OPTIONAL = ("orch-owner-approval", "content-type")
_HOST = re.compile(r"[a-z0-9.-]{1,253}(:[0-9]{1,5})?|\[[0-9a-f:]{2,45}\](:[0-9]{1,5})?")
_TARGET = re.compile(r"/[\x21-\x22\x24-\x7e]{0,8000}")  # printable ASCII, no space, no '#'
_TS = re.compile(r"0|[1-9][0-9]{0,15}")
_VALUE = re.compile(r"[\x21-\x7e]([\x20-\x7e]{0,2046}[\x21-\x7e])?")
_HEX = re.compile(r"(?:[0-9a-f]{2})*")


@dataclass(frozen=True)
class Parsed:
    method: str
    host: str
    target: str
    headers: tuple[tuple[str, str], ...]  # the host's own, as sent (name case kept)
    body: bytes
    workspace_id: str
    ts_ms: int
    nonce: bytes
    wsk_sig: bytes
    approval: dict[str, Any] | None


def parse(request: object, *, max_body: int) -> Parsed:
    if type(request) is not dict or set(request) != KEYS:
        raise Refused("malformed")
    method, host, target = request["method"], request["host"], request["target"]
    if type(method) is not str or method not in METHODS:
        raise Refused("malformed")
    if type(host) is not str or _HOST.fullmatch(host) is None:
        raise Refused("malformed")
    if type(target) is not str or _TARGET.fullmatch(target) is None:
        raise Refused("malformed")
    body_hex = request["body_hex"]
    if type(body_hex) is not str or len(body_hex) > 2 * max_body or _HEX.fullmatch(body_hex) is None:
        raise Refused("malformed")
    raw = request["headers"]
    if type(raw) is not dict or len(raw) > len(REQUIRED) + len(OPTIONAL):
        raise Refused("malformed")
    by_lower: dict[str, str] = {}
    for k, v in raw.items():
        if type(k) is not str or type(v) is not str or not k.isascii() or _VALUE.fullmatch(v) is None:
            raise Refused("malformed")
        low = k.lower()
        if low not in REQUIRED and low not in OPTIONAL or low in by_lower or k != k.strip() or " " in k:
            raise Refused("malformed")
        by_lower[low] = v
    if any(r not in by_lower for r in REQUIRED):
        raise Refused("malformed")
    try:
        ws = unhex(by_lower["orch-workspace"], 16)
        nonce = unhex(by_lower["orch-nonce"], 16)
        sig = unb64u(by_lower["orch-signature"], 64)
        if _TS.fullmatch(by_lower["orch-ts"]) is None:
            raise Refused("malformed")
        approval = None
        if "orch-owner-approval" in by_lower:
            approval = loads_strict(unb64u(by_lower["orch-owner-approval"]))
            if type(approval) is not dict:
                raise Refused("malformed")
    except ValueError, TypeError, CanonError, RecursionError:
        raise Refused("malformed") from None
    return Parsed(
        method,
        host,
        target,
        tuple(raw.items()),
        bytes.fromhex(body_hex),
        ws.hex(),
        int(by_lower["orch-ts"]),
        nonce,
        sig,
        approval,
    )
