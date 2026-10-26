"""The sealed bytes of a selective ``share_put`` (hub contract 10.2b, V3 C6/D9). No I/O, no state.

``share.item`` is signed by the workspace's ``WSK`` (label ``sig_share_item``); its content is exact per ``item_kind``
and follows the one text rule (Unicode 16.0.0: no ``Cc Cf Zl Zp Cs Co Cn``,
no ``Default_Ignorable_Code_Point``; LF only in multi-line text). The item key
``IK`` (32 fresh random bytes per put) is sealed to the hub's ``HXK`` with the v2 5.3 construction, purpose
``hub-share``; the signed item is sealed under ``IK`` with SALTED-AEAD (5.1) and an AAD that binds suite, hub,
workspace, item id and version::

    sealed = b64u(u32(item_version) || wrap (113) || salt (16) || AES-256-GCM(...))

An item of a ticket log may carry an ``f1`` block (``roster_v``, per-gate ``gen`` / ``hash`` / ``policy_hash`` /
``source_sha``, per-question ``hash``; contract 10.2b, 8.6): the values a selective custodial event is built on.

Core refuses to sign what the hub would refuse (``malformed``, ``too_large``): the checks below mirror the contract's
content rules and are run before the signature, never after."""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.text import is_assigned, is_format
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex
from orch.crypto.labels import L
from orch.crypto.sealing import seal
from orch.identity.pairing import _IGNORABLE  # noqa: PLC2701 (the Default_Ignorable_Code_Point table, one copy)

from . import wire
from .errors import HubRefused

__all__ = [
    "LABEL_RE",
    "MAX_LABELS",
    "MAX_ITEMS",
    "MAX_SEALED",
    "NEEDS_YOU_TYPES",
    "STATUSES",
    "F1_GATES",
    "build_signed_item",
    "check_content",
    "check_f1",
    "f1_gate_ok",
    "item_hash",
    "seal_share_item",
    "text_ok",
]

MAX_SEALED = 16 * 1024  # bytes of one sealed item, before base64url (`too_large` at the hub)
MAX_ITEMS = 1000  # live items per workspace (`too_many_items` at the hub)
PURPOSE = "hub-share"
_KDF_MSG = b"orch/v2/hub-share-msg"
NEEDS_YOU_TYPES = ("question", "gate", "verdict", "review", "handoff")
STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")
TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
LABEL_RE = _LABEL
_DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_QID = re.compile(r"Q[1-9][0-9]{0,5}")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
HASH_RE = _HASH
MAX_LABELS = 32


_WHITE_SPACE = frozenset(
    [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)


def _forbidden(cp: int) -> bool:
    """Cc, Cf, Zl, Zp, Cs, Co, Cn or Default_Ignorable_Code_Point under Unicode 16.0.0 (contract 3 and 8.5): the bundled
    tables of ``orch.canon.text``, never the running Python's ``unicodedata``."""
    return (
        cp <= 0x1F
        or 0x7F <= cp <= 0x9F
        or cp in (0x2028, 0x2029)
        or 0xD800 <= cp <= 0xDFFF
        or 0xE000 <= cp <= 0xF8FF
        or 0xF0000 <= cp <= 0xFFFFD
        or 0x100000 <= cp <= 0x10FFFD
        or is_format(cp)
        or not is_assigned(cp)
        or any(a <= cp <= b for a, b in _IGNORABLE)
    )


def text_ok(x: Any, n: int, *, multiline: bool = False) -> bool:
    """The contract's text rule (Unicode 16.0.0): 1..``n`` UTF-8 bytes, no leading or trailing White_Space, no
    character of the categories Cc, Cf, Zl, Zp, Cs, Co, Cn and no Default_Ignorable_Code_Point (LF allowed only
    when ``multiline``)."""
    if type(x) is not str or not 0 < len(x.encode("utf-8", "surrogatepass")) <= n:
        return False
    if ord(x[0]) in _WHITE_SPACE or ord(x[-1]) in _WHITE_SPACE:
        return False
    return all((c == "\n" and multiline) or not _forbidden(ord(c)) for c in x)


def _re(rx: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda x: type(x) is str and rx.fullmatch(x) is not None


def _enum(*v: str) -> Callable[[Any], bool]:
    return lambda x: type(x) is str and x in v


def _null_or(ok: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda x: x is None or ok(x)


def _labels(x: Any) -> bool:
    return type(x) is list and len(x) <= MAX_LABELS and all(_re(_LABEL)(i) for i in x) and x == sorted(set(x))


_CONTENT: dict[str, dict[str, Callable[[Any], bool]]] = {
    "needs_you": {
        "type": _enum(*NEEDS_YOU_TYPES),
        "title": lambda x: text_ok(x, 200),
        "deadline": _null_or(_re(_DATE)),
    },
    "ticket": {
        "key": _re(_KEY),
        "type": _enum(*TICKET_TYPES),
        "title": lambda x: text_ok(x, 200),
        "status": _enum(*STATUSES),
        "labels": _labels,
        "deadline": _null_or(_re(_DATE)),
    },
    "question": {
        "key": _re(_KEY),
        "type": _enum(*TICKET_TYPES),
        "id": _re(_QID),
        "title": lambda x: text_ok(x, 200),
        "text": lambda x: text_ok(x, 2000, multiline=True),
        "deadline": _null_or(_re(_DATE)),
    },
}


def check_content(item_kind: str, content: Any) -> None:
    """The exact key set and value rules of one ``item_kind`` (``malformed`` otherwise)."""
    spec = _CONTENT.get(item_kind) if type(item_kind) is str else None
    if spec is None or type(content) is not dict or set(content) != set(spec):
        raise HubRefused("malformed")
    if not all(ok(content[k]) for k, ok in spec.items()):
        raise HubRefused("malformed")


F1_GATES = ("requirements", "plan", "code", "verify")
F1_SOURCE_GATES = ("code", "verify")  # ticket-format 5.7: only these carry a source list
F1_MAX_QUESTIONS = 64
F1_MAX_SOURCES = 32
_REF = re.compile(r"refs/heads/[!-~]{1,244}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _int0(x: Any) -> bool:
    return type(x) is int and 0 <= x < 2**53


def _source_ok(x: Any) -> bool:
    return (
        type(x) is dict and set(x) == {"repo", "ref", "sha"} and text_ok(x["repo"], 512)
        and type(x["ref"]) is str and _REF.fullmatch(x["ref"]) is not None
        and type(x["sha"]) is str and _SHA.fullmatch(x["sha"]) is not None
    )  # fmt: skip


def f1_gate_ok(name: Any, x: Any) -> bool:
    """One ``f1.gates`` entry (contract 10.2b): exact keys, ``source_sha`` exactly on ``code`` and ``verify``, sorted by
    repo, one entry per repo, at most 32."""
    if name not in F1_GATES or type(x) is not dict:
        return False
    with_source = name in F1_SOURCE_GATES
    if set(x) != {"gen", "hash", "policy_hash"} | ({"source_sha"} if with_source else set()):
        return False
    if not (_int0(x["gen"]) and _re(_HASH)(x["hash"]) and _re(_HASH)(x["policy_hash"])):
        return False
    if not with_source:
        return True
    src = x["source_sha"]
    if type(src) is not list or len(src) > F1_MAX_SOURCES or not all(_source_ok(e) for e in src):
        return False
    repos = [e["repo"] for e in src]
    return repos == sorted(set(repos))


def check_f1(f1: Any, chain: Mapping[str, Any]) -> None:
    """The optional ``f1`` block of a ``share.item`` (contract 10.2b): exact keys at every depth, only on an item of a
    ticket log (``malformed`` otherwise). Core refuses to sign what the hub would refuse."""
    if (
        type(f1) is not dict or set(f1) != {"roster_v", "gates", "questions"} or not _int0(f1["roster_v"])
        or type(f1["gates"]) is not dict or not all(f1_gate_ok(k, v) for k, v in f1["gates"].items())
        or type(f1["questions"]) is not dict or len(f1["questions"]) > F1_MAX_QUESTIONS
        or not all(
            type(k) is str and _QID.fullmatch(k) is not None and type(v) is dict and set(v) == {"hash"}
            and _re(_HASH)(v["hash"])
            for k, v in f1["questions"].items()
        )
    ):  # fmt: skip
        raise HubRefused("malformed")
    if chain.get("log") == "workspace":
        raise HubRefused("malformed")


def _chain_ok(c: Any) -> bool:
    return (
        type(c) is dict
        and set(c) == {"log", "seq", "head"}
        and type(c["log"]) is str
        and (c["log"] == "workspace" or _ULID.fullmatch(c["log"]) is not None)
        and type(c["seq"]) is int
        and c["seq"] >= 1
        and type(c["head"]) is str
        and _HASH.fullmatch(c["head"]) is not None
    )


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def build_signed_item(
    *,
    hub_id: str,
    workspace_id: str,
    item_id: str,
    item_kind: str,
    item_version: int,
    issued_ms: int,
    chain: Mapping[str, Any],
    content: Mapping[str, Any],
    dev: bool,
    sign: Callable[[bytes], bytes],
    f1: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``WSK``-signed ``share.item``. Everything is checked first (never sign what the hub would refuse)."""
    if not (wire.hex16(hub_id) and wire.hex16(workspace_id) and wire.hex16(item_id)):
        raise HubRefused("malformed")
    if type(item_version) is not int or not 1 <= item_version < 2**32 or type(issued_ms) is not int or issued_ms < 0:
        raise HubRefused("malformed")
    if not _chain_ok(dict(chain)):
        raise HubRefused("malformed")
    check_content(item_kind, dict(content))
    if f1 is not None:
        check_f1(f1, chain)
    o = {
        "v": 2, "suite": suite.SUITE_ID, "kind": "share.item", "hv": wire.HV, "hub_id": hub_id,
        "workspace_id": workspace_id, "item_id": item_id, "item_kind": item_kind, "item_version": item_version,
        "issued_ms": issued_ms, "chain": dict(chain), "content": dict(content), "dev": bool(dev),
    }  # fmt: skip
    if f1 is not None:
        o["f1"] = dict(f1)
    return {"o": o, "sig": b64u(sign(L["sig_share_item"] + wire.cj(o)))}


def item_hash(signed: Mapping[str, Any]) -> str:
    """``"sha256:" + hex(H("orch/v2/hub-share-item|" || cj(signed share.item)))``: what a custodial event about a shared
    item binds (the hub computes the same value)."""
    try:
        return "sha256:" + suite.sha256(L["h_share_item"] + wire.cj(dict(signed))).hex()
    except wire._CanonError:  # noqa: SLF001
        raise HubRefused("malformed") from None


def _aad(hub_id: str, workspace_id: str, item_id: str, version: int) -> bytes:
    try:
        return (
            L["aad_hub_share"] + bytes([suite.SUITE_ID]) + unhex(hub_id, 16) + unhex(workspace_id, 16)
            + unhex(item_id, 16) + _u32(version)
        )  # fmt: skip
    except (EncodingError, struct.error):
        raise HubRefused("malformed") from None


def _new_ik() -> bytes:
    return os.urandom(32)


def seal_share_item(signed: Mapping[str, Any], *, hxk_pub: bytes) -> str:
    """The ``sealed`` of one ``share_put``: ``IK`` fresh per call, sealed to ``HXK`` (purpose ``hub-share``, recipient
    ``hub_id``, object ``item_id``, epoch ``item_version``, extra ``workspace_id``); the signed item is sealed under
    ``IK`` with a fresh salt. Refuses an item over 16 KiB (``too_large``) before anything leaves."""
    o = signed["o"]
    try:
        hub, ws, item = unhex(o["hub_id"], 16), unhex(o["workspace_id"], 16), unhex(o["item_id"], 16)
    except EncodingError:
        raise HubRefused("malformed") from None
    ik = _new_ik()
    wrap = seal(hxk_pub, hub, PURPOSE, item, o["item_version"], ik, ws)
    aad = _aad(o["hub_id"], o["workspace_id"], o["item_id"], o["item_version"])
    ct = suite.salted_seal(ik, _KDF_MSG, aad, wire.cj(dict(signed)))
    raw = _u32(o["item_version"]) + wrap + ct
    if len(raw) > MAX_SEALED:
        raise HubRefused("too_large")
    return b64u(raw)


def sealed_item_version(sealed: str) -> int:
    """The version prefix of a ``sealed`` string (for logs and tests)."""
    try:
        return int(struct.unpack(">I", unb64u(sealed)[:4])[0])
    except (EncodingError, struct.error):
        raise HubRefused("malformed") from None
