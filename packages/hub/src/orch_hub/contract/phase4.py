"""Phase-4 contract extension (protocol.md §6.3, §8.4–§8.5, §10.4 proposals, §10.7 log_sync, §10.8 relay requests;
V3-D23). Pure reference functions, no I/O, no clock, no key storage.

* :func:`check_action` - what a channel may ask the hub to build (an action, never event bytes);
* :func:`seal_view_page` / :func:`apply_view_page` - the hub's ``WK_e``-sealed view of a workspace log, verified line by
  line under the pinned ``WSK`` (host_sig and the event chain) before the hub builds anything from it;
* :func:`verify_channel_register` - proof of possession of a new channel key, under its own label;
* :func:`verify_space_proposal` / :func:`accept_from_proposal` - the HK_op-signed transition an invited workspace checks
  before its owner signs ``space.accept``;
* :func:`relay_request_bytes` / :func:`check_relay_request` - HK_op-signed hub -> relay requests (E1).
"""

from __future__ import annotations

import re
import struct
import unicodedata
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import spaces as SP
from . import suite as S
from .canon import CanonError, b64u, cj, loads_strict, unb64u, unhex
from .hub import (
    _TS as TS_RE,
)
from .hub import (
    CLOCK_SKEW_MS,
    CUSTODIAL_FIELDS,
    NOT_SIGNED,
    Refused,
    _dev,
    _envelope,
    _fields,
    _hex16,
    _key_field,
    _lp16,
    _signature,
    _under_card,
    check_custodial_role,
    log_kind,
    verify_card,
)
from .labels import KDF_HUB_SHARE_MSG, KDF_HUB_VIEW, KDF_HUB_VIEW_MSG, PURPOSE_HUB_SHARE, L

# --- §8.5 actions: what a channel may ask for -------------------------------------------------------------------------
#
# Every action field has an explicit schema: exact keys, value types and limits. There is no free-form object anywhere
# in an action, so nothing can carry a nested reserved key or a field owned by another type (security review of
# 28e767d, field smuggling).

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PERSON = re.compile(r"[pu]_[0-9a-f]{32}")
_DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_ADDON = re.compile(r"[a-z][a-z0-9-]{0,31}")
GATES = ("requirements", "plan", "code")
TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
PRIORITIES = ("low", "medium", "high", "urgent")
SIZES = ("xs", "s", "m", "l", "xl")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
MAX_LIST = 32


#: Unicode version every text rule is evaluated under (§2.1, L2P4-3): category tables of Python's unicodedata.
UNICODE_VERSION = "16.0.0"
if unicodedata.unidata_version != UNICODE_VERSION:  # fail closed: another table would judge text differently
    raise RuntimeError(f"text rule needs Unicode {UNICODE_VERSION}, runtime has {unicodedata.unidata_version}")
#: Refused categories: controls, format, line/paragraph separators, surrogates, private use, unassigned.
_BAD_CATS = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
#: Default_Ignorable_Code_Point (Unicode 16.0 DerivedCoreProperties) — blank or invisible characters that are not Cf:
#: U+3164 HANGUL FILLER, U+115F/1160, U+FFA0, U+034F, variation selectors, …
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _default_ignorable(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def _char_ok(c: str) -> bool:
    return unicodedata.category(c) not in _BAD_CATS and not _default_ignorable(ord(c))


def text_ok(x: Any, n: int, *, multiline: bool = False) -> bool:
    """The one text rule (§8.5, L2P4-3): 1..n UTF-8 bytes; no leading or trailing ``White_Space``; no character of
    the categories Cc, Cf, Zl, Zp, Cs, Co, Cn (Unicode :data:`UNICODE_VERSION`) and no Default_Ignorable_Code_Point;
    LF is the one exception, in multi-line text only. Never raises (a lone surrogate is a plain refusal)."""
    if type(x) is not str or not x:
        return False
    try:
        size = len(x.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    if size > n or x != x.strip():
        return False
    return all((c == "\n" and multiline) or _char_ok(c) for c in x)


def _text(n: int, *, multiline: bool = False) -> Any:
    return lambda x: text_ok(x, n, multiline=multiline)


def render_text(s: str) -> str:
    """What a channel shows for a string that comes from the workspace's view (§8.4 step 3, L2P4-2d): every character
    the text rule refuses (other than LF) is shown escaped as ``<U+XXXX>``, so an agent-written title cannot hide or
    reorder what the person sees. Reference for channel conformance (vectors ``render_*``)."""
    return "".join(c if c == "\n" or _char_ok(c) else f"<U+{ord(c):04X}>" for c in s)


def _re(rx: re.Pattern[str]) -> Any:
    return lambda x: type(x) is str and rx.fullmatch(x) is not None


def _hash(x: Any) -> bool:
    return type(x) is str and _HASH.fullmatch(x) is not None


def _enum(*v: str) -> Any:
    return lambda x: type(x) is str and x in v


def _or_null(ok: Any) -> Any:
    return lambda x: x is None or ok(x)


def _sorted_unique(item: Any, lo: int = 0, hi: int = MAX_LIST) -> Any:
    return lambda x: type(x) is list and lo <= len(x) <= hi and all(item(i) for i in x) and x == sorted(set(x))


#: ``ticket.updated`` ``set``: the leaf paths a channel may change, and their values (ticket-format §3). Everything
#: else (type, acceptance, tasks with their ``verify.cmd``, links, addons, visibility, questions) is not settable
#: through a channel in hv 0.
SET_PATHS: dict[str, Any] = {
    "ticket.title": _text(200),
    "ticket.priority": _enum(*PRIORITIES),
    "ticket.size": _or_null(_enum(*SIZES)),
    "ticket.labels": _sorted_unique(_re(_LABEL)),
    "ticket.due": _or_null(_re(_DATE)),
    "ticket.parent": _or_null(_re(_KEY)),
    "ticket.blocked_by": _sorted_unique(_re(_KEY)),
}


def _set(x: Any) -> bool:
    return (
        type(x) is dict
        and 0 < len(x) <= len(SET_PATHS)
        and all(k in SET_PATHS and SET_PATHS[k](v) for k, v in x.items())
    )


def _option(x: Any) -> bool:
    return (
        type(x) is dict
        and {"key", "label"} <= set(x) <= {"key", "label", "cost"}
        and _re(_TOKEN)(x["key"])
        and _text(200)(x["label"])
        and ("cost" not in x or _text(64)(x["cost"]))
    )


def _question(x: Any) -> bool:
    """The question a person asks (the hub assigns its ``Q`` id from its view)."""
    if type(x) is not dict or not {"to", "text", "blocking"} <= set(x) <= {
        "to",
        "text",
        "why",
        "options",
        "recommended",
        "blocking",
    }:
        return False
    opts = x.get("options", [])
    keys = [o["key"] for o in opts] if type(opts) is list and all(_option(o) for o in opts) else None
    return (
        (_re(_PERSON)(x["to"]) or x["to"] in TICKET_ROLES)
        and _text(2000, multiline=True)(x["text"])
        and ("why" not in x or _text(1000, multiline=True)(x["why"]))
        and keys is not None
        and len(opts) <= 8
        and len(keys) == len(set(keys))
        and ("options" not in x or len(opts) > 0)
        and ("recommended" not in x or x["recommended"] in keys)
        and type(x["blocking"]) is bool
    )


def _visibility(x: Any) -> bool:
    return x == "workspace" or (
        type(x) is dict and set(x) == {"restricted"} and _sorted_unique(_re(_PERSON), 1, 64)(x["restricted"])
    )


#: phase 5 (§8.8): value grammars of the grant, device, policy and settings actions (core's schemas, sorted lists)
_OPNAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?")
_GRANT = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_DEVICE = re.compile(r"d_[0-9a-f]{32}")
APPROVER_TOKENS = ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers")
POLICY_GATES = ("requirements", "plan", "verify", "code")
SETTINGS_SET: dict[str, Any] = {
    "grant_hours": lambda x: type(x) is int and 1 <= x <= 168,
    "claim_ttl_min": lambda x: type(x) is int and 15 <= x <= 1440,
    "lease_ttl_min": lambda x: type(x) is int and 5 <= x <= 1440,
}


def _gate_policy(x: Any, gate: str) -> bool:
    """core's ``gatePolicy`` exactly (all five keys), lists sorted and unique; ``code`` per D59 (``codePolicy``)."""
    if type(x) is not dict or set(x) != {"approvers", "count", "not", "applies", "independent"}:
        return False
    tok = _sorted_unique(_enum(*APPROVER_TOKENS), 0, len(APPROVER_TOKENS))
    applies = x["applies"]
    ok = (
        tok(x["approvers"])
        and len(x["approvers"]) >= 1
        and type(x["count"]) is int
        and 1 <= x["count"] <= 16
        and tok(x["not"])
        and (applies in ("all", "off") or _sorted_unique(_enum(*TICKET_TYPES), 1, len(TICKET_TYPES))(applies))
        and type(x["independent"]) is bool
    )
    if gate == "code":
        ok = ok and "assignees" in x["not"] and x["independent"] is True
    return bool(ok)


def _gates(x: Any) -> bool:
    return type(x) is dict and 1 <= len(x) and all(g in POLICY_GATES and _gate_policy(v, g) for g, v in x.items())


def _settings(x: Any) -> bool:
    return type(x) is dict and 1 <= len(x) and all(k in SETTINGS_SET and SETTINGS_SET[k](v) for k, v in x.items())


def visible(visibility: Any, me: Any) -> bool:
    """§10.7 "Visibility": core's ``can_see`` minus the membership test (the hub role / snapshot gates cover that).
    ``workspace`` is visible to everyone; ``{restricted: [...]}`` only to a person on the list. Being the ticket owner
    does not count, nor does any role. Anything else is invisible (fail closed). ``me`` = the canonical person ids
    the hub user answers to."""
    if visibility == "workspace":
        return True
    if type(visibility) is dict and type(visibility.get("restricted")) is list:
        lst = visibility["restricted"]
        return any(m in lst for m in me)  # list membership: an unhashable entry cannot raise
    return False


#: type -> (required fields, optional fields). ``type`` and ``log`` are always present (``ticket.created`` has no
#: ``log``: the hub makes the new ticket's uid). Decisions carry the ``hash`` the person reviewed: the hub refuses with
#: ``event_changed`` when its view no longer has that hash. Every other envelope or custodial field is the hub's.
ACTIONS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "ticket.created": ({"ticket_type": _enum(*TICKET_TYPES), "title": _text(200)}, {}),
    "ticket.updated": ({"set": _set}, {}),
    "status.changed": ({"to": _enum("backlog", "open")}, {"reason": _text(500)}),
    "ticket.submitted": ({}, {}),
    "ticket.closed": (
        {"resolution": _enum("wont_do", "duplicate", "obsolete", "other")},
        {"duplicate_of": _re(_KEY), "text": _text(4096, multiline=True)},
    ),
    "ticket.reopened": ({}, {"text": _text(4096, multiline=True)}),
    "ticket.moved": ({"to": _enum("backlog", "open"), "reason": _text(500)}, {}),
    "visibility.changed": ({"visibility": _visibility}, {}),
    "people.changed": (
        {
            "role": _enum("owner", "assignees", "reviewers", "watchers"),
            "add": _sorted_unique(_re(_PERSON), 0, 16),
            "remove": _sorted_unique(_re(_PERSON), 0, 16),
        },
        {},
    ),
    "claim.released": ({"session": _text(64), "reason": _enum("released")}, {}),
    "log.added": ({"text": _text(4096, multiline=True)}, {}),
    "handoff.written": ({"text": _text(2048, multiline=True)}, {}),
    "question.asked": ({"question": _question}, {}),
    "question.answered": (
        {"question": _re(re.compile(r"Q[1-9][0-9]{0,5}")), "hash": _hash},
        {"option": _re(_TOKEN), "text": _text(4096, multiline=True)},
    ),
    "gate.approved": ({"gate": _enum(*GATES), "hash": _hash}, {}),
    "gate.changes_requested": ({"gate": _enum(*GATES), "hash": _hash, "text": _text(4096, multiline=True)}, {}),
    "verdict.given": ({"outcome": _enum("pass", "fail"), "hash": _hash}, {"text": _text(4096, multiline=True)}),
    "addon.disabled": ({"name": _re(_ADDON)}, {}),
    "addon.purged": ({"name": _re(_ADDON)}, {}),
    # phase 5 (§8.8, C10 M6; DRAFT, not before phase-4 exit): grants, device exclusion, policy and settings
    "grant.requested": (
        {
            "scope": _enum("all", "workable"),
            "verbs": lambda x: x == "agent" or _sorted_unique(_re(_OPNAME), 1, 32)(x),
            "hours": lambda x: type(x) is int and 1 <= x <= 168,
        },
        {"label": _text(200)},
    ),
    "grant.revoked": ({"grant": _re(_GRANT)}, {"reason": _text(500)}),
    "device.excluded": ({"device": _re(_DEVICE), "reason": _enum("lost", "retired", "compromised")}, {}),
    "policy.changed": ({"gates": lambda x: _gates(x)}, {}),
    "settings.changed": ({"set": lambda x: _settings(x)}, {}),
}
#: Names an action can never carry at its top level: the envelope, the custodial fields and the host fields are built
#: by the hub/host. (Nested objects have exact schemas above, so they cannot carry them either.)
RESERVED = frozenset(
    {
        "v",
        "id",
        "actor",
        "based_on",
        "roster_v",
        "hash_v",
        "gate_gen",
        "policy_hash",
        "source_sha",
        "qid",
        "base_rev",
        "sections",
        *CUSTODIAL_FIELDS,
        *NOT_SIGNED,
    }
)


def check_action(action: Any, *, role: Any) -> dict[str, Any]:
    """The channel's request to the hub (``prepare``), checked before the hub reads its view: exact fields per type,
    the type custodial for its log, and the person's hub role high enough. Returns the action."""
    if type(action) is not dict or type(action.get("type")) is not str:
        raise Refused("malformed")
    typ = action["type"]
    if any(k in RESERVED for k in action if k not in ("type", "log")):
        raise Refused("malformed")
    if typ == "ticket.created":
        log = "new"
        base = {"type"}
    else:
        log = action.get("log")
        if type(log) is not str or not (log == "workspace" or _ULID.fullmatch(log)):
            raise Refused("malformed")
        base = {"type", "log"}
    if typ not in ACTIONS:
        raise Refused("not_custodial")
    check_custodial_role(typ, "ticket" if log == "new" else log, role)
    req, opt = ACTIONS[typ]
    keys = set(action) - base
    if not set(req) <= keys or not keys <= set(req) | set(opt):
        raise Refused("malformed")
    for k in keys:
        if not {**req, **opt}[k](action[k]):
            raise Refused("malformed")
    if typ == "question.answered" and not ({"option", "text"} & keys):
        raise Refused("malformed")
    if typ == "verdict.given" and action["outcome"] == "fail" and "text" not in keys:
        raise Refused("malformed")
    if typ == "ticket.closed" and ("duplicate_of" in keys) != (action["resolution"] == "duplicate"):
        raise Refused("malformed")
    if typ == "people.changed":
        add, rem = action["add"], action["remove"]
        if set(add) & set(rem) or not (add or rem):
            raise Refused("malformed")
        if action["role"] == "owner" and (len(add) != 1 or rem):
            raise Refused("malformed")
    return action


DECISION_TYPES = ("gate.approved", "gate.changes_requested", "verdict.given", "question.answered")


def short_hash(h: Any) -> str:
    """The one display form of a content hash (§8.4 step 3, orch-hub#55): the first 12 lower-case hex digits after
    ``sha256:`` in groups of 4, ``abcd ef01 2345``. Anything that is not ``sha256:`` plus 64 lower-case hex digits is
    ``malformed`` (the channel then shows the full field). Same function on the phone, in the browser and in Mission
    Control (vectors ``short_hash_*``)."""
    if type(h) is not str or _HASH.fullmatch(h) is None:
        raise Refused("malformed")
    return _groups(h[7:19])


def short_digest(d: Any) -> str:
    """The same form for the "signature reference": a 64-digit lower-case hex digest (vectors ``short_digest_*``)."""
    if type(d) is not str or re.fullmatch(r"[0-9a-f]{64}", d) is None:
        raise Refused("malformed")
    return _groups(d[:12])


def _groups(x: str) -> str:
    return " ".join(x[i : i + 4] for i in range(0, 12, 4))


def check_built(action: Mapping[str, Any], log: str, event: Any) -> None:
    """The hub's contract check of the event it built, before it asks the channel for a proof (§8.4 step 2,
    L2P4-2b). Always runs: the type is the action's, the log is the action's (a new ticket's uid for
    ``ticket.created``), and on the four decision types ``event.hash`` exists and is byte-equal to ``action.hash``
    (the hash the person reviewed). A missing hash is ``malformed``; another one ``event_changed``."""
    if type(event) is not dict or event.get("type") != action.get("type"):
        raise Refused("event_changed")
    if action.get("type") != "ticket.created" and log != action.get("log"):
        raise Refused("event_changed")
    if action.get("type") in DECISION_TYPES:
        if type(event.get("hash")) is not str:
            raise Refused("malformed")
        if event["hash"] != action.get("hash"):
            raise Refused("event_changed")


def check_prepared(ctx: Any, digest_hex: Any) -> bytes:
    """What a channel does with the hub's ``prepare`` reply before it shows anything (WYSIWYS, §8.4): the digest it
    will sign is recomputed from the context it renders, never taken from the hub."""
    from .hub import event_digest

    if (
        type(ctx) is not dict
        or set(ctx) != {"contract", "suite", "workspace_id", "log", "event"}
        or ctx["contract"] != 1
        or ctx["suite"] != S.SUITE_ID
        or type(ctx["event"]) is not dict
        or "channel_proof" in ctx["event"]
        or ctx["event"].get("auth") != "hub"
    ):
        raise Refused("malformed")
    d = event_digest(ctx["workspace_id"], ctx["log"], ctx["event"])
    if type(digest_hex) is not str or d.hex() != digest_hex:
        raise Refused("event_changed")
    return d


# --- §10.7 log_sync: the hub's view of a workspace log ---------------------------------------------------------------

VIEW_MAX_LINES = 256
VIEW_MAX_SEALED = 1 << 20


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def view_key(wk_e: bytes, workspace_id: str, epoch: int) -> bytes:
    """``HKDF(WK_e, salt = "", info = "orch/v2/hub-view|" hex(ws) "|" dec(epoch))``, like v2 §8.2's ``K_bridge``."""
    info = KDF_HUB_VIEW + b"|" + workspace_id.encode() + b"|" + str(epoch).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(wk_e)


def view_aad(workspace_id: str, epoch: int, log: str, from_seq: int, count: int, head: str, reset: bool) -> bytes:
    if not _HASH.fullmatch(head):
        raise Refused("malformed")
    return (
        L["aad_hub_view"]
        + bytes([S.SUITE_ID])
        + unhex(workspace_id, 16)
        + _u32(epoch)
        + _lp16(log.encode("ascii"))
        + struct.pack(">Q", from_seq)
        + _u32(count)
        + bytes.fromhex(head[7:])
        + bytes([1 if reset else 0])
    )


def event_head(event: Mapping[str, Any]) -> str:
    """ticket-format §5.5: ``"sha256:" + hex(H("orch/v2/event|" || cj(event)))`` over the full event."""
    try:
        return "sha256:" + S.sha256(L["h_event"] + cj(dict(event))).hex()
    except CanonError:
        raise Refused("malformed") from None


def host_sig_bytes(workspace_id: str, log: str, event: Mapping[str, Any]) -> bytes:
    """ticket-format §5.5: ``"orch/v2/sig/host-event|" || cj(ctx)`` over the event without ``host_sig``."""
    e = {k: v for k, v in event.items() if k != "host_sig"}
    try:
        return L["sig_host_event"] + cj(
            {"contract": 1, "suite": S.SUITE_ID, "workspace_id": workspace_id, "log": log, "event": e}
        )
    except CanonError:
        raise Refused("malformed") from None


def _log_ok(log: Any) -> bool:
    return type(log) is str and (log == "workspace" or _ULID.fullmatch(log) is not None)


def seal_view_page(
    wk_e: bytes,
    workspace_id: str,
    epoch: int,
    log: str,
    from_seq: int,
    lines: list[dict[str, Any]],
    salt: bytes,
    *,
    reset: bool = False,
    proj: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The host's ``log_sync`` body: the exact log lines ``from_seq ..`` sealed with SALTED-AEAD (v2 §5.1) under the
    view key of ``epoch``, with the ticket projection ``proj`` (D15b) when given. Test/host helper; ``salt`` is 16
    fresh bytes in real use."""
    head = event_head(lines[-1])
    aad = view_aad(workspace_id, epoch, log, from_seq, len(lines), head, reset)
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=KDF_HUB_VIEW_MSG).derive(
        view_key(wk_e, workspace_id, epoch)
    )
    doc: dict[str, Any] = {"lines": lines} if proj is None else {"lines": lines, "proj": dict(proj)}
    sealed = salt + AESGCM(k).encrypt(bytes(12), cj(doc), aad)
    return {
        "epoch": epoch,
        "log": log,
        "from_seq": from_seq,
        "count": len(lines),
        "head": head,
        "reset": reset,
        "sealed": b64u(sealed),
    }


def open_view_page(wk_e: bytes, workspace_id: str, body: Mapping[str, Any]) -> list[Any]:
    """The lines of one sealed page (its ``proj``, if any, is read by :func:`open_view_doc`)."""
    return open_view_doc(wk_e, workspace_id, body)[0]


def open_view_doc(wk_e: bytes, workspace_id: str, body: Mapping[str, Any]) -> tuple[list[Any], Any]:
    """``(lines, proj)`` of one sealed page; ``proj`` is None when the page carries none (D15b, §10.7). The plaintext
    is exactly ``{"lines"}`` or ``{"lines", "proj"}``; ``proj`` is only shape-free here (:func:`check_view_proj`)."""
    try:
        raw = unb64u(body["sealed"])
    except CanonError:
        raise Refused("malformed") from None
    if len(raw) > VIEW_MAX_SEALED:
        raise Refused("too_large")
    if len(raw) < 16 + 16:
        raise Refused("bad_wrap")
    aad = view_aad(
        workspace_id, body["epoch"], body["log"], body["from_seq"], body["count"], body["head"], body["reset"]
    )
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=raw[:16], info=KDF_HUB_VIEW_MSG).derive(
        view_key(wk_e, workspace_id, body["epoch"])
    )
    try:
        pt = AESGCM(k).decrypt(bytes(12), raw[16:], aad)
    except InvalidTag:
        raise Refused("bad_wrap") from None
    try:
        doc = loads_strict(pt)
    except CanonError:
        raise Refused("malformed") from None
    if type(doc) is not dict or set(doc) not in ({"lines"}, {"lines", "proj"}) or type(doc["lines"]) is not list:
        raise Refused("malformed")
    return doc["lines"], doc.get("proj")


def apply_view_page(
    views: Mapping[str, list[str]],
    body: Any,
    *,
    workspace_id: str,
    wsk_pub: bytes,
    wk_epochs: Mapping[int, bytes],
) -> dict[str, list[str]]:
    """The hub's check of one ``log_sync`` body, in order (§10.7). ``views`` maps each log to the event heads it holds
    (index ``i`` = seq ``i + 1``); ``wk_epochs`` the ``WK_e`` the hub can open (from its sealed wraps). Returns the new
    ``views``. Nothing is built from a page that did not pass every step."""
    return apply_view_page_proj(views, {}, body, workspace_id=workspace_id, wsk_pub=wsk_pub, wk_epochs=wk_epochs)[0]


def apply_view_page_proj(
    views: Mapping[str, list[str]],
    projs: Mapping[str, Mapping[str, Any]],
    body: Any,
    *,
    workspace_id: str,
    wsk_pub: bytes,
    wk_epochs: Mapping[int, bytes],
) -> tuple[dict[str, list[str]], dict[str, Mapping[str, Any]]]:
    """:func:`apply_view_page` plus the ticket projections (D15b): ``projs`` maps a ticket log to the newest ``proj``
    the hub holds for it. Returns ``(views, projs)``. A page's ``proj`` is checked after its lines
    (:func:`check_view_proj`); the newer by ``(seq, ws_seq)`` is kept (ties: this page's). A reset of a log drops its
    ``proj``; a reset of the workspace log drops every one, as it drops every ticket view."""
    views2 = _apply_view_page(views, body, workspace_id=workspace_id, wsk_pub=wsk_pub, wk_epochs=wk_epochs)
    log = body["log"]
    lines, proj = open_view_doc(wk_epochs[body["epoch"]], workspace_id, body)  # opened and checked above
    out = {} if (log == "workspace" and body["reset"]) else dict(projs)
    if body["reset"]:
        out.pop(log, None)
    if proj is not None:
        check_view_proj(
            proj,
            log=log,
            seq=body["from_seq"] + body["count"] - 1,
            head=body["head"],
            line_ws_seq=lines[-1].get("ws_seq"),
            ws_heads=views2.get("workspace", []),
        )
        held = out.get(log)
        if held is None or (proj["seq"], proj["ws_seq"]) >= (held["seq"], held["ws_seq"]):
            out[log] = proj
    return views2, out


def _apply_view_page(
    views: Mapping[str, list[str]],
    body: Any,
    *,
    workspace_id: str,
    wsk_pub: bytes,
    wk_epochs: Mapping[int, bytes],
) -> dict[str, list[str]]:
    if type(body) is not dict or set(body) != {"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}:
        raise Refused("malformed")
    if (
        type(body["epoch"]) is not int
        or not _log_ok(body["log"])
        or type(body["from_seq"]) is not int
        or body["from_seq"] < 1
        or type(body["count"]) is not int
        or not 1 <= body["count"] <= VIEW_MAX_LINES
        or type(body["head"]) is not str
        or not _HASH.fullmatch(body["head"])
        or type(body["reset"]) is not bool
        or (body["reset"] and body["from_seq"] != 1)
    ):
        raise Refused("malformed")
    # 1. the epoch: only the newest WK_e the hub holds (it seals after wk_seal, never before)
    if not wk_epochs or body["epoch"] != max(wk_epochs):
        raise Refused("bad_epoch")
    log, start = body["log"], body["from_seq"]
    held = [] if body["reset"] else list(views.get(log, []))
    # 2. position: a page extends the held view or overlaps it; a hole is a gap
    if start > len(held) + 1:
        raise Refused("view_gap")
    # 3. open (AEAD under the view key; the AAD binds workspace, epoch, log, range, head, reset)
    lines = open_view_page(wk_epochs[body["epoch"]], workspace_id, body)
    if len(lines) != body["count"]:
        raise Refused("malformed")
    ws_view = len(views.get("workspace", [])) if log != "workspace" else None
    prev_head = held[start - 2] if start > 1 else None
    out = held[: start - 1]
    for i, e in enumerate(lines):
        seq = start + i
        if type(e) is not dict or e.get("seq") != seq or "host_sig" not in e:
            raise Refused("malformed")
        # 4. every line is the host's: host_sig under the pinned WSK (ticket-format §5.5)
        try:
            hs = unb64u(e["host_sig"], S.SIG_LEN)
        except CanonError:
            raise Refused("malformed") from None
        if not S.verify(wsk_pub, hs, host_sig_bytes(workspace_id, log, e)):
            raise Refused("bad_signature")
        # 5. the chain: prev is the head of the line before (null at seq 1)
        if e.get("prev") != prev_head:
            raise Refused("view_fork")
        h = event_head(e)
        if seq <= len(held) and held[seq - 1] != h:
            raise Refused("view_fork")
        # 6. a ticket line never runs ahead of the workspace log the hub holds (sync the workspace log first)
        if ws_view is not None and (type(e.get("ws_seq")) is not int or e["ws_seq"] > ws_view):
            raise Refused("view_gap")
        out.append(h)
        prev_head = h
    if prev_head != body["head"]:
        raise Refused("malformed")
    if len(held) > len(out):  # an overlap that matched keeps the longer held view
        out = held
    if log == "workspace" and body["reset"]:
        # L2P4-9: a reset workspace log may be shorter than the ws_seq of held ticket lines; every ticket view is
        # dropped and the host resends the ticket logs (§10.7).
        return {"workspace": out}
    return {**views, log: out}


# --- §10.2b the content of a selective share_put (C6/D9) -------------------------------------------------------------

SHARE_MAX_SEALED = 16 * 1024  # bytes of one sealed item (before b64u)
SHARE_MAX_ITEMS = 1000  # live items per workspace (so at most 16 MiB sealed per workspace)
_WRAP_LEN = 65 + 32 + 16
NEEDS_YOU_TYPES = ("question", "gate", "verdict", "review", "handoff")
STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")

#: item_kind -> the exact content fields (C6's lists, orch-core #1022). No free objects.
SHARE_CONTENT: dict[str, dict[str, Any]] = {
    "needs_you": {"type": _enum(*NEEDS_YOU_TYPES), "title": _text(200), "deadline": _or_null(_re(_DATE))},
    "ticket": {
        "key": _re(_KEY),
        "type": _enum(*TICKET_TYPES),
        "title": _text(200),
        "status": _enum(*STATUSES),
        "labels": _sorted_unique(_re(_LABEL)),
        "deadline": _or_null(_re(_DATE)),
    },
    "question": {
        "key": _re(_KEY),
        "type": _enum(*TICKET_TYPES),
        "id": _re(re.compile(r"Q[1-9][0-9]{0,5}")),
        "title": _text(200),
        "text": _text(2000, multiline=True),
        "deadline": _or_null(_re(_DATE)),
    },
}


def _chain_head(x: Any) -> bool:
    return (
        type(x) is dict
        and set(x) == {"log", "seq", "head"}
        and _log_ok(x["log"])
        and type(x["seq"]) is int
        and x["seq"] >= 1
        and _hash(x["head"])
    )


def share_aad(hub_id: str, workspace_id: str, item_id: str, item_version: int) -> bytes:
    return (
        L["aad_hub_share"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id, 16)
        + unhex(workspace_id, 16)
        + unhex(item_id, 16)
        + _u32(item_version)
    )


def item_hash(signed: Mapping[str, Any]) -> str:
    """``"sha256:" + hex(H("orch/v2/hub-share-item|" || cj(signed share.item)))``: what a custodial event about a shared
    item binds (spec §8a "a custodial digest binds the item hash")."""
    try:
        return "sha256:" + S.sha256(L["h_share_item"] + cj(dict(signed))).hex()
    except CanonError:
        raise Refused("malformed") from None


def seal_share_item(signed: Mapping[str, Any], *, hxk_pub: bytes, ik: bytes, eph_seed: bytes, salt: bytes) -> str:
    """The host side: ``sealed = u32(item_version) || seal_to(HXK, "hub-share", recipient hub_id, object item_id,
    epoch item_version, extra workspace_id; IK) || SALTED-AEAD(IK, "orch/v2/hub-share-msg", AAD, cj(signed item))``.
    ``ik``, ``eph_seed`` and ``salt`` are fresh for every put in real use."""
    from cryptography.hazmat.primitives.asymmetric import ec

    o = signed["o"]
    eph = S.key_from_seed(eph_seed)
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), hxk_pub))
    k = SP._seal_key(ss, eph_pub, hxk_pub, PURPOSE_HUB_SHARE, o["hub_id"])
    aad = SP._seal_aad(PURPOSE_HUB_SHARE, unhex(o["item_id"], 16), o["item_version"], unhex(o["workspace_id"], 16))
    wrap = eph_pub + AESGCM(k).encrypt(bytes(12), ik, aad)
    mk = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=KDF_HUB_SHARE_MSG).derive(ik)
    ct = salt + AESGCM(mk).encrypt(
        bytes(12), cj(dict(signed)), share_aad(o["hub_id"], o["workspace_id"], o["item_id"], o["item_version"])
    )
    return b64u(_u32(o["item_version"]) + wrap + ct)


def open_share_item(
    body: Any,
    *,
    hub_id: str,
    workspace_id: str,
    hxk_priv: Any,
    wsk_pub: bytes,
    held: Mapping[str, Any] | None,
    live_items: int,
    dev_mode: bool,
) -> dict[str, Any]:
    """The hub's check of a selective ``share_put`` (§10.2b), in order. ``held`` is ``{item_version, hash, revoked}``
    of what it holds under this ``item_id`` (a revoked item stays as a tombstone; None if never seen); ``live_items``
    the workspace's live item count. Returns ``{"item": o, "signed": the signed item, "hash": item_hash, "new": bool}``; a higher
    ``item_version`` replaces the held item (or re-shares a revoked id)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    if type(body) is not dict or set(body) != {"item_id", "sealed"} or not _hex16(body["item_id"]):
        raise Refused("malformed")
    try:
        raw = unb64u(body["sealed"])
    except CanonError:
        raise Refused("malformed") from None
    if len(raw) > SHARE_MAX_SEALED:
        raise Refused("too_large")
    if len(raw) < 4 + _WRAP_LEN + 16 + 16:
        raise Refused("bad_wrap")
    version = struct.unpack(">I", raw[:4])[0]
    if version < 1:
        raise Refused("malformed")
    eph_pub, wct, ct = raw[4:69], raw[69 : 4 + _WRAP_LEN], raw[4 + _WRAP_LEN :]
    try:
        if not S.valid_public_key(eph_pub):
            raise Refused("bad_wrap")
        ss = hxk_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
        k = SP._seal_key(ss, eph_pub, S.public_bytes(hxk_priv), PURPOSE_HUB_SHARE, hub_id)
        aad = SP._seal_aad(PURPOSE_HUB_SHARE, unhex(body["item_id"], 16), version, unhex(workspace_id, 16))
        ik = AESGCM(k).decrypt(bytes(12), wct, aad)
        mk = HKDF(algorithm=hashes.SHA256(), length=32, salt=ct[:16], info=KDF_HUB_SHARE_MSG).derive(ik)
        pt = AESGCM(mk).decrypt(bytes(12), ct[16:], share_aad(hub_id, workspace_id, body["item_id"], version))
    except InvalidTag, ValueError:
        raise Refused("bad_wrap") from None
    try:
        signed = loads_strict(pt)
    except CanonError:
        raise Refused("malformed") from None
    o = _envelope(signed, "share.item")
    _signature(signed, L["sig_share_item"], wsk_pub)
    _fields({k: v for k, v in o.items() if k != "f1"}, "share.item")  # f1 is the one optional key (C3b addendum)
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["item_id"] != body["item_id"] or o["item_version"] != version or not _chain_head(o["chain"]):
        raise Refused("malformed")
    spec = SHARE_CONTENT[o["item_kind"]]
    c = o["content"]
    if set(c) != set(spec) or not all(ok(c[k]) for k, ok in spec.items()):
        raise Refused("malformed")
    if "f1" in o:
        check_f1(o["f1"], o["chain"])
    _dev(o, dev_mode)
    h = item_hash(signed)
    if held is not None:
        if version < held["item_version"]:
            raise Refused("stale_version")
        if version == held["item_version"]:
            if h != held["hash"] or held.get("revoked"):
                raise Refused("stale_version")
            return {"item": o, "signed": signed, "hash": h, "new": False}
    if (held is None or held.get("revoked")) and live_items >= SHARE_MAX_ITEMS:
        raise Refused("too_many_items")
    return {"item": o, "signed": signed, "hash": h, "new": True}


def check_share_revoke(body: Any) -> None:
    """``share_revoke {item_id}`` is idempotent: an unknown or already revoked id is ``{}`` too. The hub deletes the
    item and keeps a tombstone ``{item_id, item_version}``, so a re-share needs a higher ``item_version``."""
    if type(body) is not dict or set(body) != {"item_id"} or not _hex16(body["item_id"]):
        raise Refused("malformed")


# --- §6.3 channel registration ---------------------------------------------------------------------------------------

#: the factor a channel certificate gets follows the gate the hub verified, never the caller's choice (§6.3)
FACTOR_OF = {("phone", "pairing-code"): "se-unlocked", ("browser", "session"): "password+totp"}


def verify_channel_register(
    signed: Any,
    *,
    hub_id: str,
    challenge: Mapping[str, Any],
    now_ms: int,
    dev_mode: bool,
    revoked_channels: Any = (),
    registered: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The hub's check of a registration proof. ``challenge`` is what the hub stored when it opened the registration:
    ``{user_id, key_pub, nonce, expires_ms, gate, session}`` with ``gate`` the check it already passed
    (``pairing-code`` or ``session``) and ``session`` whether a live session of ``user_id`` opened it (required for
    both gates, L2P4-D7a). ``revoked_channels``: every channel id ever revoked (permanent, L2P4-6); ``registered``:
    channel id -> the user it is registered to. Returns ``{channel_id, user_id, key_pub, factor}``; the factor comes
    from the gate."""
    o = _envelope(signed, "hub.channel_register")
    key = _key_field(o, "key_pub")
    _signature(signed, L["sig_hub_channel_register"], key)
    _fields(o, "hub.channel_register")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["user_id"] != challenge["user_id"]:
        raise Refused("other_user")
    if (
        o["nonce"] != challenge["nonce"]
        or o["key_pub"] != challenge["key_pub"]
        or o["expires_ms"] != challenge["expires_ms"]
        or now_ms >= o["expires_ms"]
    ):
        raise Refused("challenge_invalid")
    factor = FACTOR_OF.get((o["channel_kind"], o["attestation"]))
    if factor is None:
        raise Refused("malformed")
    if o["attestation"] != challenge["gate"]:
        raise Refused("factor_mismatch")
    if challenge.get("session") is not True:
        raise Refused("no_session")
    _dev(o, dev_mode)
    from .hub import channel_id

    cid = channel_id(key)
    if cid in set(revoked_channels):
        raise Refused("channel_revoked")
    owner = (registered or {}).get(cid)
    if owner is not None and owner != o["user_id"]:
        raise Refused("key_in_use")
    return {"channel_id": cid, "user_id": o["user_id"], "key_pub": o["key_pub"], "factor": factor}


# --- §10.4 the hub-signed space proposal (#1020) ----------------------------------------------------------------------


def _member_list(x: list[Any], lo: int) -> None:
    if not lo <= len(x) <= SP.MAX_MEMBERS or any(not _hex16(w) for w in x) or x != sorted(set(x)):
        raise Refused("malformed")


def verify_space_proposal(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    now_ms: int,
    dev_mode: bool,
    held_space: Mapping[str, Any] | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The invited host's check of a ``hub.space_proposal`` before its owner is asked (§10.4). ``held_space`` is the
    ``hub.space`` it already holds for that space, if it was ever a member."""
    o = _under_card(signed, "hub.space_proposal", held, dev_mode, revoked_op_certs)
    _member_list(o["prev_members"], 0)
    _member_list(o["members"], 1)
    if (o["epoch"] == 1) != (o["prev_members"] == []):
        raise Refused("malformed")
    if o["workspace_id"] not in o["members"] or o["workspace_id"] in o["prev_members"]:
        raise Refused("malformed")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["expires_ms"] <= o["issued_ms"] or o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("cert_invalid")
    if now_ms >= o["expires_ms"]:
        raise Refused("cert_expired")
    if held_space is not None and (held_space["space_id"] != o["space_id"] or o["epoch"] <= held_space["epoch"]):
        raise Refused("stale_epoch")
    return o


def accept_from_proposal(p: Mapping[str, Any]) -> dict[str, Any]:
    """The ``space.accept`` object (unsigned) the host builds from a verified proposal, after its owner confirmed: the
    hashes come from the signed lists, never from a hub admin's text."""
    return {
        "v": 2,
        "suite": S.SUITE_ID,
        "kind": "space.accept",
        "hv": 0,
        "hub_id": p["hub_id"],
        "space_id": p["space_id"],
        "workspace_id": p["workspace_id"],
        "epoch": p["epoch"],
        "prev_members_hash": SP.members_hash(p["prev_members"]),
        "members_hash": SP.members_hash(p["members"]),
        "dev": p["dev"],
    }


# --- §10.8 hub -> relay requests (E1) --------------------------------------------------------------------------------

RELAY_WINDOW_MS = 120_000
RELAY_NONCE_RETENTION_MS = 240_000
RELAY_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")
RELAY_TARGET_MAX = 2048
_SEG = re.compile(r"[A-Za-z0-9._~-]+")
_QPART = re.compile(r"[A-Za-z0-9._~-]+")


def parse_relay_target(target: Any, hub_id: str) -> None:
    """Canonical target (§10.8): ASCII, at most 2048 bytes, path ``/v2/hubs/<hub_id>`` or below it with unreserved
    characters only (no ``%``, ``+``, ``#``, ``;``, empty, ``.`` or ``..`` segments, no trailing ``/``), and an
    optional query of unique ``key=value`` pairs of unreserved characters. The ``hub_id`` segment must be the signing
    hub's (``other_hub``)."""
    if type(target) is not str or not target.isascii() or len(target) > RELAY_TARGET_MAX:
        raise Refused("malformed")
    path, sep, query = target.partition("?")
    segs = path.split("/")
    if segs[0] != "" or len(segs) < 4 or segs[1:3] != ["v2", "hubs"]:
        raise Refused("malformed")
    for s in segs[1:]:
        if not _SEG.fullmatch(s) or s in (".", ".."):
            raise Refused("malformed")
    if not re.fullmatch(r"[0-9a-f]{32}", segs[3]):
        raise Refused("malformed")
    if sep:
        keys = []
        for part in query.split("&"):
            k, eq, v = part.partition("=")
            if not eq or not _QPART.fullmatch(k) or not _QPART.fullmatch(v):
                raise Refused("malformed")
            keys.append(k)
        if len(keys) != len(set(keys)):
            raise Refused("malformed")
    if segs[3] != hub_id:
        raise Refused("other_hub")


def relay_request_bytes(
    hub_id: str, op_cert_id: str, ts_ms: int, nonce: bytes, method: str, target: str, body: bytes
) -> bytes:
    """``"orch/v2/hub-relay-req|" || suite || hub_id (16) || op_cert_id (16) || u64(ts_ms) || nonce (16) ||
    lp16(METHOD) || lp16(target) || H(body)``."""
    if type(ts_ms) is not int or not 0 <= ts_ms < 1 << 64 or type(nonce) is not bytes or len(nonce) != 16:
        raise Refused("malformed")
    for part in (method, target):
        if type(part) is not str or not part.isascii():
            raise Refused("malformed")
    if type(body) is not bytes:
        raise Refused("malformed")
    return (
        L["sig_hub_relay_request"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id, 16)
        + unhex(op_cert_id, 16)
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + S.sha256(body)
    )


def check_relay_request(
    req: Mapping[str, Any], st: Mapping[str, Any], *, now_ms: int, dev_mode: bool
) -> dict[str, Any]:
    """The relay's check of a hub request (§10.8), in order. ``req`` = {method, target, headers, body (bytes)}; ``st`` =
    {hub_cards: {hub_id: signed card}, revoked_op_certs: [..], nonces: {hub_id: {nonce hex: ts_ms}}}. Returns the hub
    and the nonce to record (the caller stores it for :data:`RELAY_NONCE_RETENTION_MS`)."""
    h = {str(k).lower(): v for k, v in req["headers"].items()}
    try:
        hub = h["orch-hub"]
        unhex(hub, 16)
        op_id = h["orch-hub-op-cert"]
        unhex(op_id, 16)
        if type(h["orch-hub-ts"]) is not str or not TS_RE.fullmatch(h["orch-hub-ts"]):
            raise CanonError("ts")
        ts = int(h["orch-hub-ts"])
        nonce = unhex(h["orch-hub-nonce"], 16)
        sig = unb64u(h["orch-hub-signature"], 64)
        if req["method"] not in RELAY_METHODS:
            raise CanonError("method")
        parse_relay_target(req["target"], hub)
        msg = relay_request_bytes(hub, op_id, ts, nonce, req["method"], req["target"], req["body"])
    except KeyError, CanonError:
        raise Refused("malformed") from None
    except Refused as r:
        raise Refused("other_hub" if r.code == "other_hub" else "malformed") from None
    if abs(now_ms - ts) > RELAY_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    try:  # the held card, verified again for this request (op cert valid, not revoked, root's hub_id, dev marker)
        held = verify_card(
            st["hub_cards"][hub],
            now_ms=now_ms,
            dev_mode=dev_mode,
            revoked_op_certs=frozenset(st.get("revoked_op_certs", [])),
        )
    except KeyError, Refused:
        raise Refused("unknown_hub") from None
    if op_id != held["op_cert"]["cert_id"]:
        raise Refused("stale_op_cert")
    if not S.verify(held["hk_op_pub"], sig, msg):
        raise Refused("bad_hub_signature")
    seen = st.get("nonces", {}).get(hub, {})
    if nonce.hex() in seen and now_ms - seen[nonce.hex()] < RELAY_NONCE_RETENTION_MS:
        raise Refused("replay")
    return {"hub": hub, "nonce": nonce.hex()}


__all__ = [
    "ACTIONS",
    "FACTOR_OF",
    "accept_from_proposal",
    "apply_view_page",
    "apply_view_page_proj",
    "build_from_proj",
    "check_action",
    "check_built",
    "check_prepared",
    "check_relay_request",
    "check_view_proj",
    "event_head",
    "host_sig_bytes",
    "log_kind",
    "open_view_doc",
    "open_view_page",
    "parse_relay_target",
    "relay_request_bytes",
    "render_text",
    "roster_v",
    "seal_view_page",
    "text_ok",
    "verify_channel_register",
    "verify_space_proposal",
    "view_aad",
    "view_key",
    "visible",
]


# --- C3b addendum (§10.2b f1, §8.6 selective custodial events, §8.7 delivery acks, §9.1 user ids) --------------------

F1_GATES = ("requirements", "plan", "code", "verify")
F1_SOURCE_GATES = ("code", "verify")  # ticket-format §5.7: only these carry a source list
F1_MAX_QUESTIONS = 64
F1_MAX_SOURCES = 32
_QID = re.compile(r"Q[1-9][0-9]{0,5}")
_REF = re.compile(r"refs/heads/[!-~]{1,244}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _int0(x: Any) -> bool:
    return type(x) is int and 0 <= x < 2**53


def _source(x: Any) -> bool:
    """One source-list entry (ticket-format §5.7): ``{repo, ref, sha}``, exact keys."""
    return (
        type(x) is dict
        and set(x) == {"repo", "ref", "sha"}
        and _text(512)(x["repo"])
        and type(x["ref"]) is str
        and _REF.fullmatch(x["ref"]) is not None
        and type(x["sha"]) is str
        and _SHA.fullmatch(x["sha"]) is not None
    )


def _sources(x: Any) -> bool:
    if type(x) is not list or len(x) > F1_MAX_SOURCES or not all(_source(e) for e in x):
        return False
    repos = [e["repo"] for e in x]
    return repos == sorted(set(repos))  # sorted by repo, one entry per repo


def _f1_gate(name: str, x: Any) -> bool:
    keys = {"gen", "hash", "policy_hash"} | ({"source_sha"} if name in F1_SOURCE_GATES else set())
    return (
        type(x) is dict
        and set(x) == keys
        and _int0(x["gen"])
        and _hash(x["hash"])
        and _hash(x["policy_hash"])
        and ("source_sha" not in x or _sources(x["source_sha"]))
    )


def check_f1(f1: Any, chain: Any) -> None:
    """The optional ``f1`` block of a ``share.item`` (§10.2b): exactly the F1 values a selective custodial build needs
    that the item does not already carry (``based_on`` is ``chain.head``). Exact keys at every depth; ``source_sha``
    exactly on ``code`` and ``verify``; only for an item of a ticket log (no selective workspace-log types)."""
    if (
        type(f1) is not dict
        or set(f1) != {"roster_v", "gates", "questions"}
        or not _int0(f1["roster_v"])
        or type(f1["gates"]) is not dict
        or not all(k in F1_GATES and _f1_gate(k, v) for k, v in f1["gates"].items())
        or type(f1["questions"]) is not dict
        or len(f1["questions"]) > F1_MAX_QUESTIONS
        or not all(
            type(k) is str and _QID.fullmatch(k) and type(v) is dict and set(v) == {"hash"} and _hash(v["hash"])
            for k, v in f1["questions"].items()
        )
    ):
        raise Refused("malformed")
    if type(chain) is not dict or chain.get("log") == "workspace":
        raise Refused("malformed")


#: §8.6: the only types a selective workspace's hub builds (decisions on a shared item of a ticket log).
SELECTIVE_TYPES = DECISION_TYPES


def build_selective(action: Any, item: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """§8.6: the event core the hub builds for a selective workspace from one shared item it holds (``item`` is the
    signed ``share.item`` that passed :func:`open_share_item`, live, and visible to the user). Returns ``(log,
    core)``; the engine then adds ``v``, ``id``, ``actor`` and the custodial fields exactly as in full mode (§8.4).
    Fills only ``based_on`` (= ``chain.head``), ``roster_v``, ``hash_v``, ``item_hash`` and the decision's F1 values
    from ``f1``; every other field is the person's, from the checked action."""
    if type(action) is not dict or type(action.get("type")) is not str or not _hex16(action.get("item")):
        raise Refused("malformed")
    rest = {k: v for k, v in action.items() if k != "item"}
    typ = rest["type"]
    if typ not in SELECTIVE_TYPES:
        raise Refused("not_custodial")
    check_action(rest, role="edit")  # the decisions' floor; the host checks the person's real member role
    o = item["o"]
    if o["item_id"] != action["item"] or o["chain"]["log"] != rest["log"]:
        raise Refused("unknown_item")
    if "f1" not in o:
        raise Refused("unavailable")  # the host shared it without F1 values: the hub cannot build on it
    f1, log = o["f1"], o["chain"]["log"]
    core: dict[str, Any] = {
        "type": typ,
        "based_on": o["chain"]["head"],
        "roster_v": f1["roster_v"],
        "hash_v": 1,
        "item_hash": item_hash(item),
    }
    if typ == "question.answered":
        if "option" in rest:
            raise Refused("malformed")  # options are not shared in V3: the person could not have seen them
        q = f1["questions"].get(rest["question"])
        if q is None:
            raise Refused("unknown_item")
        if q["hash"] != rest["hash"]:
            raise Refused("event_changed")
        core.update(question=rest["question"], hash=q["hash"], text=rest["text"])
    else:
        gate = "verify" if typ == "verdict.given" else rest["gate"]
        g = f1["gates"].get(gate)
        if g is None:
            raise Refused("unknown_item")
        if g["hash"] != rest["hash"]:
            raise Refused("event_changed")
        core.update(gate_gen=g["gen"], hash=g["hash"], policy_hash=g["policy_hash"])
        if typ == "verdict.given":
            core["outcome"] = rest["outcome"]
        else:
            core["gate"] = gate
        if "text" in rest:
            core["text"] = rest["text"]
        if typ == "verdict.given" or (typ == "gate.approved" and gate == "code"):
            core["source_sha"] = g["source_sha"]
    check_built(rest, log, core)
    return log, core


# --- §10.7 the ticket projection of a full-mode view (D15b) --------------------------------------------------------------

VIEW_PROJ_KEYS = frozenset({"seq", "head", "ws_seq", "ws_head", "status", "roster_v", "gates"})


def check_view_proj(proj: Any, *, log: str, seq: int, head: str, line_ws_seq: Any, ws_heads: list[str]) -> None:
    """The hub's check of a page's ``proj`` (§10.7 "The ticket projection"), after the page's lines passed. ``seq`` /
    ``head`` are the page's last line, ``line_ws_seq`` that line's ``ws_seq``, ``ws_heads`` the workspace view the hub
    holds. Exact keys at every depth; the gate entries are the §10.2b ``f1`` gate schema."""
    if (
        log == "workspace"
        or type(proj) is not dict
        or set(proj) != VIEW_PROJ_KEYS
        or not _int0(proj["seq"])
        or not _hash(proj["head"])
        or not _int0(proj["ws_seq"])
        or proj["ws_seq"] < 1
        or not _hash(proj["ws_head"])
        or proj["status"] not in STATUSES
        or not _int0(proj["roster_v"])
        or type(proj["gates"]) is not dict
        or not all(k in F1_GATES and _f1_gate(k, v) for k, v in proj["gates"].items())
    ):
        raise Refused("malformed")
    if proj["seq"] != seq or proj["head"] != head or type(line_ws_seq) is not int or proj["ws_seq"] < line_ws_seq:
        raise Refused("malformed")  # it describes the page's last line, at a workspace position not before that line's
    if proj["ws_seq"] > len(ws_heads):
        raise Refused("view_gap")  # sync the workspace log first
    if ws_heads[proj["ws_seq"] - 1] != proj["ws_head"]:
        raise Refused("view_fork")


#: ticket-format §5.4.2: the workspace-log types that raise the member-list version (genesis → 1, +1 per type)
ROSTER_TYPES = ("workspace.created", "member.added", "member.removed", "role.changed")


def roster_v(lines: Any) -> int:
    """The ``roster_v`` a hub counts from its workspace view (D15b-1, vectors ``workspace_roster_v*``): one per line of
    :data:`ROSTER_TYPES`. Core's replay (``WsCore.roster_v``) must give the same number, or every full-mode build is
    ``unavailable`` (§10.7 build rule)."""
    if type(lines) is not list or not all(type(e) is dict for e in lines):
        raise Refused("malformed")
    return sum(1 for e in lines if e.get("type") in ROSTER_TYPES)


#: the full-mode types whose derived fields come from the projection (the rest come from the lines, §8.5)
PROJ_TYPES = ("gate.approved", "gate.changes_requested", "verdict.given", "status.changed", "ticket.moved")


def build_from_proj(
    action: Mapping[str, Any], proj: Any, *, ticket_seq: int, ticket_head: str | None, roster_v: int
) -> dict[str, Any]:
    """§8.5 / §10.7 (D15b): the fields a full-mode hub copies from the held projection of the action's ticket log, for
    a checked action of :data:`PROJ_TYPES`. ``ticket_seq`` / ``ticket_head`` are the hub's held view of that log,
    ``roster_v`` its own count from the workspace lines. Never computes an F1 value: a missing or stale projection is
    ``unavailable``, a gate the host left out ``unknown_item``, another hash ``event_changed``."""
    typ = action["type"]
    if typ not in PROJ_TYPES:
        raise Refused("not_custodial")
    if (
        type(proj) is not dict
        or proj.get("seq") != ticket_seq
        or proj.get("head") != ticket_head
        or proj.get("roster_v") != roster_v
    ):
        raise Refused("unavailable")  # no projection of the held head yet: the host sends one (sender rule 7)
    if typ in ("status.changed", "ticket.moved"):
        return {"from": proj["status"]}
    gate = "verify" if typ == "verdict.given" else action["gate"]
    g = proj["gates"].get(gate)
    if g is None:
        raise Refused("unknown_item")
    if g["hash"] != action["hash"]:
        raise Refused("event_changed")
    out: dict[str, Any] = {"gate_gen": g["gen"], "hash": g["hash"], "policy_hash": g["policy_hash"]}
    if typ == "verdict.given" or (typ == "gate.approved" and gate == "code"):
        out["source_sha"] = g["source_sha"]
    return out


#: §8.7: the ``event_deliver`` replies after which the hub resends the same event bytes; every other code is final.
DELIVER_RETRYABLE = frozenset({"unavailable", "unknown_snapshot"})
DELIVER_FIRST_DELAY_MS = 1_000
DELIVER_MAX_DELAY_MS = 300_000
DELIVER_GIVE_UP_MS = 24 * 3600 * 1000


def deliver_next(reply: Any, *, attempt: int, first_sent_ms: int, now_ms: int) -> dict[str, Any]:
    """What the hub does after delivery attempt ``attempt`` (1, 2, ...) of one custodial event (§8.7). ``reply`` is the
    host's reply body, or None when no reply came (link drop, timeout). Returns ``{"done": True}``,
    ``{"resend_in_ms": n}`` (the SAME bytes, never re-signed) or ``{"drop": code}`` (tell the channel ``code``)."""
    if type(reply) is dict and reply == {}:
        return {"done": True}
    if type(reply) is dict and set(reply) == {"error"} and type(reply["error"]) is str:
        if reply["error"] not in DELIVER_RETRYABLE:
            return {"drop": reply["error"]}
        code = reply["error"]
    else:
        code = "unavailable"  # no reply, or a reply that is neither {} nor {"error": code}: not provably refused
    if now_ms - first_sent_ms >= DELIVER_GIVE_UP_MS:
        return {"drop": code}
    return {"resend_in_ms": min(DELIVER_FIRST_DELAY_MS << min(max(attempt, 1) - 1, 20), DELIVER_MAX_DELAY_MS)}


def check_user_hub(
    user_hub: Mapping[str, str], aliases: Mapping[str, Mapping[str, str]], hub_id: str, user_id: str
) -> None:
    """§9.1 (C3b, L2P4-C1, L2-C3-2): a ``u_`` id belongs to the hub under which it first appeared in the workspace.
    ``user_hub`` = ``{user_id: hub_id}`` of first appearance (never re-pointed); ``aliases`` = ``{hub_id: {user_id:
    person_id}}`` from every ``hub.joined``. Refuses ``foreign_user`` when the id was first recorded under another hub
    or another hub's alias book names it, unless this hub's own book aliases it to the same person as every other
    book that names it (C4: one person may keep one id on two hubs)."""
    mine = aliases.get(hub_id, {}).get(user_id)
    others = {h: b[user_id] for h, b in aliases.items() if h != hub_id and user_id in b}
    first = user_hub.get(user_id)
    if first is None or first == hub_id:
        foreign = bool(others)
    else:
        foreign = True
        others.setdefault(first, aliases.get(first, {}).get(user_id))  # an unaliased first record is no person
    if foreign and (mine is None or any(p != mine for p in others.values())):
        raise Refused("foreign_user")


# --- §8.5 the event side: the exact payload of a custodial event after the hub derived its fields --------------------

STATUSES_ALL = STATUSES
ENVELOPE = ("v", "id", "type", "actor", "based_on", "roster_v", "hash_v")
_QID32 = re.compile(r"[0-9a-f]{32}")


def _base_rev(x: Any, keys: Any) -> bool:
    """``base_rev`` (ticket-format §5.8): an object mapping EVERY ``set`` path, and nothing else, to the value hash the
    hub's view holds for it. Never a single hash string."""
    return type(x) is dict and type(keys) is dict and set(x) == set(keys) and all(_hash(v) for v in x.values())


#: type -> the fields the hub derives (§8.5 "Everything else in the event is the hub's"), with their checks. The rest of
#: the payload is exactly the action's fields (``check_action``).
DERIVED: dict[str, dict[str, Any]] = {
    "ticket.created": {"key": _re(_KEY), "owner": _re(re.compile(r"u_[0-9a-f]{32}"))},
    "ticket.updated": {"base_rev": None},  # checked against `set` below
    "status.changed": {"from": _enum(*STATUSES_ALL)},
    "ticket.moved": {"from": _enum(*STATUSES_ALL)},
    "question.asked": {"qid": _re(_QID32), "hash": _hash},
    "gate.approved": {"gate_gen": _int0, "policy_hash": _hash},
    "gate.changes_requested": {"gate_gen": _int0, "policy_hash": _hash},
    "verdict.given": {"gate_gen": _int0, "policy_hash": _hash, "source_sha": _sources},
}


def check_custodial_payload(event: Any, log: str) -> None:
    """Host §8.3 step 1 (L2-C3-1, structural): a custodial event's envelope and payload are EXACTLY what the hub may
    build for its type: the action's fields (``check_action``, the same nested schemas, text rule included) plus the
    fields the hub derives (:data:`DERIVED`, ``source_sha`` exactly on a ``code`` approval). Anything else, at any
    depth, is ``malformed``. The caller has checked the custodial fields; the type's table entry and role are 7b."""
    from .hub import CUSTODIAL_FIELDS as CF

    if type(event) is not dict or "log" in event:  # the log is the context's, never a body field
        raise Refused("malformed")
    typ = event.get("type")
    rest = {k: v for k, v in event.items() if k not in ENVELOPE and k not in CF and k not in ("sig", "item_hash")}
    if (
        event.get("v") != 2
        or type(event.get("id")) is not str
        or not _ULID.fullmatch(event["id"])
        or event.get("hash_v") != 1
        or not _int0(event.get("roster_v"))
        or not (event.get("based_on") is None if typ == "ticket.created" else _hash(event.get("based_on")))
    ):
        raise Refused("malformed")
    derived = dict(DERIVED.get(typ, {}))
    if typ == "gate.approved" and rest.get("gate") == "code":
        derived["source_sha"] = _sources
    for k, ok in derived.items():
        if k not in rest:
            raise Refused("malformed")
        if ok is not None and not ok(rest[k]):
            raise Refused("malformed")
    if typ == "ticket.updated" and not _base_rev(rest["base_rev"], rest.get("set")):
        raise Refused("malformed")
    action = {k: v for k, v in rest.items() if k not in derived}
    if typ == "question.asked":
        q = action.get("question")
        if type(q) is not dict or type(q.get("id")) is not str or not _QID.fullmatch(q["id"]):
            raise Refused("malformed")
        action["question"] = {k: v for k, v in q.items() if k != "id"}
    action["type"] = typ
    if typ != "ticket.created":
        action["log"] = log
    if any(k in RESERVED for k in action if k not in ("type", "log")):
        raise Refused("malformed")
    try:
        check_action(action, role="admin")  # shape only; the role floor is §8.3 step 7b
    except Refused as r:
        raise Refused("not_custodial" if r.code == "not_custodial" else "malformed") from None
