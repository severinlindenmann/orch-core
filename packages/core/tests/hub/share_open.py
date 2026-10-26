"""Test-side port of the hub's check of a selective ``share_put`` / ``share_revoke`` (hub contract 10.2b, vectors
``share_item_*`` and ``share_revoke_*``). orch-core never imports hub code (D79): this is written from the contract
text and is the oracle the production builder (``orch.hubclient.sharefmt``) is tested against. It shares nothing with
the production validators on purpose."""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag

from orch.canon import text as canon_text
from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u, unhex
from orch.crypto.sealing import open_sealed

from . import contract as H
from .contract import Refused

MAX_SEALED = 16 * 1024
MAX_ITEMS = 1000
LABEL_SIG = b"orch/v2/sig/share-item|"
LABEL_AAD = b"orch/v2/hub-share-aad|"
LABEL_HASH = b"orch/v2/hub-share-item|"
KDF_MSG = b"orch/v2/hub-share-msg"
WRAP_LEN = 65 + 32 + 16
_HEX16 = re.compile(r"[0-9a-f]{32}")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
TYPES = ("feature", "bug", "chore", "spike", "epic")
STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")


_DI = (  # Default_Ignorable_Code_Point, Unicode 16.0.0 (written from DerivedCoreProperties, not shared with production)
    (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)  # fmt: skip
_WS = {*range(9, 14), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000}
_FIXED = ((0, 0x1F), (0x7F, 0x9F), (0x2028, 0x2029), (0xD800, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def bad_char(cp: int) -> bool:
    return any(a <= cp <= b for a, b in _FIXED + _DI) or canon_text.is_format(cp) or not canon_text.is_assigned(cp)


def text(n: int, multiline: bool = False):
    def ok(x: Any) -> bool:
        if type(x) is not str or not 0 < len(x.encode("utf-8", "surrogatepass")) <= n:
            return False
        if ord(x[0]) in _WS or ord(x[-1]) in _WS:
            return False
        return all((c == "\n" and multiline) or not bad_char(ord(c)) for c in x)

    return ok


def rx(r):
    return lambda x: type(x) is str and r.fullmatch(x) is not None


def enum(*v):
    return lambda x: type(x) is str and x in v


def nullable(f):
    return lambda x: x is None or f(x)


CONTENT = {
    "needs_you": {
        "type": enum("question", "gate", "verdict", "review", "handoff"),
        "title": text(200),
        "deadline": nullable(rx(_DATE)),
    },
    "ticket": {
        "key": rx(_KEY),
        "type": enum(*TYPES),
        "title": text(200),
        "status": enum(*STATUSES),
        "labels": lambda x: type(x) is list and len(x) <= 32 and all(rx(_LABEL)(i) for i in x) and x == sorted(set(x)),
        "deadline": nullable(rx(_DATE)),
    },
    "question": {
        "key": rx(_KEY),
        "type": enum(*TYPES),
        "id": rx(re.compile(r"Q[1-9][0-9]{0,5}")),
        "title": text(200),
        "text": text(2000, True),
        "deadline": nullable(rx(_DATE)),
    },
}
ITEM_FIELDS = {
    "hub_id": rx(_HEX16), "workspace_id": rx(_HEX16), "item_id": rx(_HEX16),
    "item_kind": enum("needs_you", "ticket", "question"),
    "item_version": lambda x: type(x) is int and 1 <= x < 2**32, "issued_ms": lambda x: type(x) is int and x >= 0,
    "chain": lambda c: type(c) is dict and set(c) == {"log", "seq", "head"} and type(c["log"]) is str
    and (c["log"] == "workspace" or _ULID.fullmatch(c["log"]) is not None) and type(c["seq"]) is int and c["seq"] >= 1
    and type(c["head"]) is str and _HASH.fullmatch(c["head"]) is not None,
    "content": lambda x: type(x) is dict, "dev": lambda x: type(x) is bool,
}  # fmt: skip


_QID = re.compile(r"Q[1-9][0-9]{0,5}")
_REF = re.compile(r"refs/heads/[!-~]{1,244}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _f1_gate(name: str, g: Any) -> bool:
    src = name in ("code", "verify")
    if type(g) is not dict or set(g) != {"gen", "hash", "policy_hash"} | ({"source_sha"} if src else set()):
        return False
    if not (type(g["gen"]) is int and 0 <= g["gen"] < 2**53 and rx(_HASH)(g["hash"]) and rx(_HASH)(g["policy_hash"])):
        return False
    if not src:
        return True
    s = g["source_sha"]
    if type(s) is not list or len(s) > 32:
        return False
    if not all(
        type(e) is dict and set(e) == {"repo", "ref", "sha"} and text(512)(e["repo"]) and rx(_REF)(e["ref"])
        and rx(_SHA)(e["sha"])
        for e in s
    ):  # fmt: skip
        return False
    return [e["repo"] for e in s] == sorted({e["repo"] for e in s})


def check_f1(f1: Any, chain: Mapping[str, Any]) -> None:
    """Contract 10.2b: the optional ``f1`` block (written from the text, not shared with production)."""
    ok = (
        type(f1) is dict and set(f1) == {"roster_v", "gates", "questions"}
        and type(f1["roster_v"]) is int and 0 <= f1["roster_v"] < 2**53
        and type(f1["gates"]) is dict and all(k in ("requirements", "plan", "code", "verify") and _f1_gate(k, v)
                                              for k, v in f1["gates"].items())
        and type(f1["questions"]) is dict and len(f1["questions"]) <= 64
        and all(rx(_QID)(k) and type(v) is dict and set(v) == {"hash"} and rx(_HASH)(v["hash"])
                for k, v in f1["questions"].items())
    )  # fmt: skip
    if not ok or chain["log"] == "workspace":
        raise Refused("malformed")


def aad(hub_id: str, workspace_id: str, item_id: str, version: int) -> bytes:
    return (
        LABEL_AAD + bytes([S.SUITE_ID]) + unhex(hub_id, 16) + unhex(workspace_id, 16) + unhex(item_id, 16)
        + struct.pack(">I", version)
    )  # fmt: skip


def item_hash(signed: Mapping[str, Any]) -> str:
    return "sha256:" + S.sha256(LABEL_HASH + H.cj(dict(signed))).hex()


def open_share_item(
    body: Any, *, hub_id: str, workspace_id: str, hxk_priv: Any, wsk_pub: bytes, held: Mapping[str, Any] | None = None,
    live_items: int = 0, dev_mode: bool = True,
) -> dict[str, Any]:  # fmt: skip
    if type(body) is not dict or set(body) != {"item_id", "sealed"} or not rx(_HEX16)(body["item_id"]):
        raise Refused("malformed")
    try:
        raw = unb64u(body["sealed"])
    except H.CanonError:
        raise Refused("malformed") from None
    if len(raw) > MAX_SEALED:
        raise Refused("too_large")
    if len(raw) < 4 + WRAP_LEN + 32:
        raise Refused("bad_wrap")
    version = struct.unpack(">I", raw[:4])[0]
    if version < 1:
        raise Refused("malformed")
    wrap, ct = raw[4 : 4 + WRAP_LEN], raw[4 + WRAP_LEN :]
    try:
        ik = open_sealed(hxk_priv, unhex(hub_id, 16), "hub-share", unhex(body["item_id"], 16), version, wrap,
                         unhex(workspace_id, 16))  # fmt: skip
        pt = S.salted_open(ik, KDF_MSG, aad(hub_id, workspace_id, body["item_id"], version), ct)
    except (InvalidTag, S.CryptoError, ValueError):
        raise Refused("bad_wrap") from None
    signed = H.parse(pt)
    o = H._envelope(signed, "share.item")  # noqa: SLF001
    H._signature(signed, LABEL_SIG, wsk_pub)  # noqa: SLF001
    if set(o) - {"f1"} != {"v", "suite", "kind", "hv", *ITEM_FIELDS} or not all(
        ok(o[k]) for k, ok in ITEM_FIELDS.items()
    ):
        raise Refused("malformed")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["item_id"] != body["item_id"] or o["item_version"] != version:
        raise Refused("malformed")
    spec, c = CONTENT[o["item_kind"]], o["content"]
    if set(c) != set(spec) or not all(ok(c[k]) for k, ok in spec.items()):
        raise Refused("malformed")
    if "f1" in o:
        check_f1(o["f1"], o["chain"])
    H._dev(o, dev_mode)  # noqa: SLF001
    h = item_hash(signed)
    if held is not None:
        if version < held["item_version"]:
            raise Refused("stale_version")
        if version == held["item_version"]:
            if h != held["hash"] or held.get("revoked"):
                raise Refused("stale_version")
            return {"item": o, "hash": h, "new": False}
    if (held is None or held.get("revoked")) and live_items >= MAX_ITEMS:
        raise Refused("too_many_items")
    return {"item": o, "hash": h, "new": True}


def check_share_revoke(body: Any) -> None:
    if type(body) is not dict or set(body) != {"item_id"} or not rx(_HEX16)(body["item_id"]):
        raise Refused("malformed")


__all__ = ["b64u", "check_share_revoke", "item_hash", "open_share_item"]
