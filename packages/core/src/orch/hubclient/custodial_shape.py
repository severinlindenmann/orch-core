"""The exact payload of a custodial event (hub contract 8.5), checked on the host.

One allow-list per custodial type: the fields of the hub's ACTION for that type (``check_action`` in the hub's
reference, with the same nested schemas and limits) plus the fields the hub derives from its view (``id``, ``based_on``,
``roster_v``, ``hash_v``, ``gate_gen``, ``policy_hash``, ``source_sha``, ``qid``, ``base_rev``, ``key``, ``owner``,
``from``, the content ``hash``). There is no free-form object anywhere: every nested value has an exact schema, so no
event can carry a reserved field, or a field owned by another type, at any depth (the second field-smuggling finding
on this surface, L2-C3-1: enforced here, in one place, not per type).

Written from the contract text (D79: no hub code is imported). Allow-list only: a key or value not listed is
``malformed``. What this does NOT decide are the ticket-format checks of step 9 (``based_on`` against the log head,
``roster_v``, gate generation, eligibility), which run against the host's own log."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.question import ANSWER_MAX, QuestionError, validate_question
from orch.canon.text import TextError, check_plain

from .errors import HubRefused

__all__ = ["UNICODE_VERSION", "check_custodial_payload", "text_ok"]

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PERSON = re.compile(r"[pu]_[0-9a-f]{32}")
_DATE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_ADDON = re.compile(r"[a-z][a-z0-9-]{0,31}")
_QID = re.compile(r"Q[1-9][0-9]{0,5}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GATES = ("requirements", "plan", "code")
TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
PRIORITIES = ("low", "medium", "high", "urgent")
SIZES = ("xs", "s", "m", "l", "xl")
STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
MAX_LIST = 32
#: phase 5 (hub protocol 8.8): value grammars of the grant, device, policy and settings actions
_OPNAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?")
_GRANT = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_DEVICE = re.compile(r"d_[0-9a-f]{32}")
#: FB15 (8.8b): the value grammars of ``addon.requested`` (the add-on name of the manifest, a semver, the capabilities)
_ADDON_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")


def _reserved_addon_names() -> frozenset[str]:
    """The names core's schema refuses for an add-on (``common#/$defs/addonName`` ``not.enum``): the one list."""
    from orch import schema

    return frozenset(schema.load("common")["$defs"]["addonName"]["not"]["enum"])


RESERVED_ADDON_NAMES = _reserved_addon_names()
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
CAPABILITIES = ("serve_http", "spawn_agent", "pty", "network", "git_push", "choose_model", "credential")
_HANDOFF = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
RUNTIME_KINDS = ("python-requirements", "node-package-lock")
APPROVER_TOKENS = ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers")
POLICY_GATES = ("requirements", "plan", "verify", "code")

#: The Unicode version the text rule is evaluated under (contract 2.1, L2P4-3). Another table would judge text
#: differently from the hub and the channels, so a runtime on another version refuses (fail closed) instead.
UNICODE_VERSION = "16.0.0"
_BAD_CATS = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
#: Default_Ignorable_Code_Point (Unicode 16.0): blank or invisible characters that are not Cf.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)  # fmt: skip


def _char_ok(c: str) -> bool:
    cp = ord(c)
    return unicodedata.category(c) not in _BAD_CATS and not any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def text_ok(x: Any, n: int, *, multiline: bool = False) -> bool:
    """The one text rule (8.5): 1..n UTF-8 bytes, no leading or trailing White_Space, no character of Cc, Cf, Zl, Zp,
    Cs, Co, Cn and no Default_Ignorable_Code_Point; LF only in multi-line text. Never raises."""
    if type(x) is not str or not x:
        return False
    try:
        size = len(x.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return size <= n and x == x.strip() and all((c == "\n" and multiline) or _char_ok(c) for c in x)


Check = Callable[[Any], bool]


def _text(n: int, *, multiline: bool = False) -> Check:
    return lambda x: text_ok(x, n, multiline=multiline)


def _re(rx: re.Pattern[str]) -> Check:
    return lambda x: type(x) is str and rx.fullmatch(x) is not None


def _enum(*v: str) -> Check:
    return lambda x: type(x) is str and x in v


def _or_null(ok: Check) -> Check:
    return lambda x: x is None or ok(x)


def _nonneg(x: Any) -> bool:
    return type(x) is int and 0 <= x <= 9007199254740991


def _sorted_unique(item: Check, lo: int = 0, hi: int = MAX_LIST) -> Check:
    return lambda x: type(x) is list and lo <= len(x) <= hi and all(item(i) for i in x) and x == sorted(set(x))


#: ``ticket.updated`` ``set``: the only paths a channel may change (8.5), with their values.
SET_PATHS: dict[str, Check] = {
    "ticket.title": _text(200),
    "ticket.priority": _enum(*PRIORITIES),
    "ticket.size": _or_null(_enum(*SIZES)),
    "ticket.labels": _sorted_unique(_re(_LABEL)),
    "ticket.due": _or_null(_re(_DATE)),
    "ticket.parent": _or_null(_re(_KEY)),
    "ticket.blocked_by": _sorted_unique(_re(_KEY)),
}
_hash = _re(_HASH)


def _set(x: Any) -> bool:
    return (
        type(x) is dict
        and 0 < len(x) <= len(SET_PATHS)
        and all(k in SET_PATHS and SET_PATHS[k](v) for k, v in x.items())
    )


def _base_rev(x: Any) -> bool:
    """Derived by the hub: the hash of each path the update sets (the object form, as ticket-format 15.1 and the hub
    addendum pin it; the single-hash string form is gone)."""
    return type(x) is dict and 0 < len(x) <= len(SET_PATHS) and all(k in SET_PATHS and _hash(v) for k, v in x.items())


def _question(x: Any) -> bool:
    """The question a person asks through the hub: the one question shape (ticket-format §5.6.1), checked by the one
    validator, plus the routing the hub may set: the id is the hub's next free `Q<n>` (derived like `qid`), `to` a
    person or a ticket role; no `sig`."""
    try:
        validate_question(x)
    except QuestionError:
        return False
    return bool(_re(_PERSON)(x["to"]) or x["to"] in TICKET_ROLES)


def _answer_text(x: Any) -> bool:
    """An answer's text: the answer rule of ticket-format §5.6.1 (1..1000 code points, check_plain, not blank). Which
    kind takes a text alone is the model's check (it holds the question)."""
    try:
        check_plain(x, one_line=False, limit=ANSWER_MAX, what="the answer text")
    except TextError:
        return False
    return True


def _visibility(x: Any) -> bool:
    return x == "workspace" or (
        type(x) is dict and set(x) == {"restricted"} and _sorted_unique(_re(_PERSON), 1, 64)(x["restricted"])
    )


def _source_sha(x: Any) -> bool:
    """Derived by the hub: one ``{repo, ref, sha}`` per repo, sorted by repo (the exact repo and ref grammar is
    ticket-format's, checked in step 9)."""
    return (
        type(x) is list
        and len(x) <= MAX_LIST
        and all(
            type(i) is dict and set(i) == {"repo", "ref", "sha"} and _text(255)(i["repo"]) and _text(255)(i["ref"])
            and _re(_COMMIT)(i["sha"])
            for i in x
        )
        and [i["repo"] for i in x] == sorted({i["repo"] for i in x})
    )  # fmt: skip


def _int_in(lo: int, hi: int) -> Check:
    return lambda x: type(x) is int and lo <= x <= hi


def _gate_policy(gate: str) -> Check:
    """core's ``gatePolicy`` exactly (the five keys, no ``factor``: a channel never changes a factor floor, 8.8), lists
    sorted and unique; ``code`` per D59 (``not`` names assignees, independent)."""
    tok = _sorted_unique(_enum(*APPROVER_TOKENS), 0, len(APPROVER_TOKENS))
    types = _sorted_unique(_enum(*TICKET_TYPES), 1, len(TICKET_TYPES))

    def ok(x: Any) -> bool:
        if type(x) is not dict or set(x) != {"approvers", "count", "not", "applies", "independent"}:
            return False
        good = (
            tok(x["approvers"])
            and len(x["approvers"]) >= 1
            and _int_in(1, 16)(x["count"])
            and tok(x["not"])
            and (x["applies"] in ("all", "off") or types(x["applies"]))
            and type(x["independent"]) is bool
        )
        return bool(good and (gate != "code" or ("assignees" in x["not"] and x["independent"] is True)))

    return ok


def _gates(x: Any) -> bool:
    return type(x) is dict and len(x) >= 1 and all(g in POLICY_GATES and _gate_policy(g)(v) for g, v in x.items())


#: ``settings.changed`` ``set``: only these three keys (``repos``, ``publish``, an add-on's own settings, the ignored
#: ``sign_methods`` and the rest stay off the channel, V3-D29)
SETTINGS_SET: dict[str, Check] = {
    "grant_hours": _int_in(1, 168),
    "claim_ttl_min": _int_in(15, 1440),
    "lease_ttl_min": _int_in(5, 1440),
}


def _settings(x: Any) -> bool:
    return type(x) is dict and len(x) >= 1 and all(k in SETTINGS_SET and SETTINGS_SET[k](v) for k, v in x.items())


def _runtime(x: Any) -> bool:
    """``addon.requested`` ``runtime``: exactly the signed ``runtime`` of the grant (kind, lock path, package count)."""
    return (
        type(x) is dict
        and set(x) == {"kind", "lock", "packages"}
        and x["kind"] in RUNTIME_KINDS
        and text_ok(x["lock"], 200)
        and _int_in(1, 1000)(x["packages"])
    )


#: Fields every custodial event carries besides the hub fields (8.1): the envelope the hub derives.
COMMON_REQUIRED: dict[str, Check] = {
    "v": lambda x: x == 2 and type(x) is int,
    "id": _re(_ULID),
    "based_on": _or_null(_hash),
    "hash_v": lambda x: x == 1 and type(x) is int,
}
COMMON_OPTIONAL: dict[str, Check] = {"roster_v": _nonneg}

_DECISION_DERIVED: dict[str, Check] = {"gate_gen": _nonneg, "policy_hash": _hash}
_KEYS = _re(_KEY)

#: type -> (required, optional) payload checks: the hub's action fields plus the fields it derives for that type.
PAYLOAD: dict[str, tuple[dict[str, Check], dict[str, Check]]] = {
    "ticket.created": (
        {"ticket_type": _enum(*TICKET_TYPES), "title": _text(200), "key": _KEYS, "owner": _re(_PERSON)},
        {},
    ),
    "ticket.updated": ({"set": _set, "base_rev": _base_rev}, {}),
    "status.changed": ({"to": _enum("backlog", "open"), "from": _enum(*STATUSES)}, {"reason": _text(500)}),
    "ticket.submitted": ({}, {}),
    "ticket.closed": (
        {"resolution": _enum("wont_do", "duplicate", "obsolete", "other")},
        {"duplicate_of": _KEYS, "text": _text(4096, multiline=True)},
    ),
    "ticket.reopened": ({}, {"text": _text(4096, multiline=True)}),
    "ticket.moved": ({"to": _enum("backlog", "open"), "reason": _text(500), "from": _enum(*STATUSES)}, {}),
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
    "question.asked": ({"question": _question, "qid": _re(_HEX32), "hash": _hash}, {}),
    "question.answered": (
        {"question": _re(_QID), "hash": _hash},
        {"option": _re(_TOKEN), "text": _answer_text},
    ),
    "gate.approved": ({"gate": _enum(*GATES), "hash": _hash, **_DECISION_DERIVED}, {"source_sha": _source_sha}),
    "gate.changes_requested": (
        {"gate": _enum(*GATES), "hash": _hash, "text": _text(4096, multiline=True), **_DECISION_DERIVED},
        {"source_sha": _source_sha},
    ),
    "verdict.given": (
        {"outcome": _enum("pass", "fail"), "hash": _hash, "source_sha": _source_sha, **_DECISION_DERIVED},
        {"text": _text(4096, multiline=True)},
    ),
    "addon.disabled": ({"name": _re(_ADDON)}, {}),
    "addon.purged": ({"name": _re(_ADDON)}, {}),
    # phase 5 (8.8, V3-D29): the event carries no grant id, secret hash or times; the host derives them
    "grant.requested": (
        {
            "handoff": _re(re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")),
            "scope": _enum("all", "workable"),
            "verbs": lambda x: x == "agent" or _sorted_unique(_re(_OPNAME), 1, 32)(x),
            "hours": _int_in(1, 168),
        },
        {"label": _text(200)},
    ),
    "grant.revoked": ({"grant": _re(_GRANT)}, {"reason": _text(500)}),
    "addon.requested": (
        {
            "name": lambda x: _re(_ADDON_NAME)(x) and x not in RESERVED_ADDON_NAMES,
            "version": _re(_SEMVER),
            "package_sha256": _hash,
            "capabilities": _sorted_unique(_enum(*CAPABILITIES), 0, len(CAPABILITIES)),
            "handoff": _re(_HANDOFF),
        },
        {"runtime": lambda x: _runtime(x)},
    ),
    "device.excluded": ({"device": _re(_DEVICE), "reason": _enum("lost", "retired", "compromised")}, {}),
    "policy.changed": ({"gates": _gates}, {}),
    "settings.changed": ({"set": _settings}, {}),
}


def check_custodial_payload(typ: str, event: Mapping[str, Any], hub_fields: frozenset[str]) -> None:
    """Refuse (``malformed``) any custodial ``event`` that is not exactly the 8.5 shape of its type: the allow-list of
    top-level names (``hub_fields`` are 8.1's, already shape-checked by the caller), each value against its exact
    schema, and the cross-field rules of the actions. ``not_custodial`` for a type without a schema."""
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise HubRefused("unavailable")  # fail closed: this runtime's tables are not the contract's
    spec = PAYLOAD.get(typ)
    if spec is None:
        raise HubRefused("not_custodial")
    req, opt = ({**COMMON_REQUIRED, **spec[0]}, {**COMMON_OPTIONAL, **spec[1]})
    names = set(event) - hub_fields - {"type", "actor"}
    if not set(req) <= names or not names <= set(req) | set(opt):
        raise HubRefused("malformed")
    checks = {**req, **opt}
    if not all(checks[k](event[k]) for k in names):
        raise HubRefused("malformed")
    if typ == "ticket.created" and event["based_on"] is not None:
        raise HubRefused("malformed")  # the first event of a log has no base
    if typ in ("gate.approved", "gate.changes_requested"):  # source_sha belongs on a code approval, and only there
        if ("source_sha" in names) != (typ == "gate.approved" and event["gate"] == "code"):
            raise HubRefused("malformed")
    if typ == "question.answered" and not ({"option", "text"} & names):
        raise HubRefused("malformed")
    if typ == "verdict.given" and event["outcome"] == "fail" and "text" not in names:
        raise HubRefused("malformed")
    if typ == "ticket.closed" and ("duplicate_of" in names) != (event["resolution"] == "duplicate"):
        raise HubRefused("malformed")
    if typ == "ticket.updated" and type(event["base_rev"]) is dict and set(event["base_rev"]) != set(event["set"]):
        raise HubRefused("malformed")
    if typ == "people.changed":
        add, rem = event["add"], event["remove"]
        if set(add) & set(rem) or not (add or rem) or (event["role"] == "owner" and (len(add) != 1 or rem)):
            raise HubRefused("malformed")
