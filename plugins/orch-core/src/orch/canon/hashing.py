"""Hashes, domain labels, the gate hash, the event chain and the bytes that get signed (ticket-format §5.3-§5.7).

Everything here is a pure function from values to ``sha256:`` strings or to bytes; signing itself is C2.

Decisions (each one is stated by the format section named):

* **One hash form (§5.6).** Every hash is ``"sha256:" + 64 lower-case hex``, artifact digests included. One parser
  (:func:`parse_hash`) refuses anything else; hashes are compared as bytes. The artifact digest is the only
  unlabelled hash (it must equal ``sha256sum`` of the file); every other hash is ``H(label || data)`` with a
  ``|``-terminated domain label from :data:`LABELS`, so the label set is prefix-free and no two kinds of hash can
  collide.
* **Refuse, don't normalise (§5.6, §11.3).** Every function that hashes or builds signed bytes first checks every
  string it is given (values and keys, at any depth) with :func:`orch.canon.text.check_text` and raises
  :class:`HashError` if one breaks the text rules. Normalising happens once, when text enters the store.
* **Typed errors.** Malformed input raises :class:`HashError`; nothing here lets ``UnicodeEncodeError``,
  ``RecursionError`` or ``KeyError`` escape for JSON-like input.
* **``hash_v``.** Only ``1`` exists. A function that sees another value refuses (:func:`check_hash_v`), so a future
  recipe can never be mistaken for this one.
* **Gate hash (§5.7).** ``H("orch/v2/gate|" || cj(G))`` with ``G`` the 15 keys of :data:`GATE_KEYS`, always all
  present; empty values (``[]``, ``{}``, ``null``) where a gate does not use a key. Every input is type-checked and
  must be in the form the format gives; lists the format says are sorted must already be sorted (refused, not
  re-sorted, since the caller built them from the logs).
* **Canonical forms that are defined by the format are applied, not demanded:** the policy object of §5.7 is hashed
  with ``approvers``/``not`` sorted and de-duplicated and ``applies`` (a list) sorted and de-duplicated; the people
  hash sorts and de-duplicates the person lists (§5.6 "lists sorted").
* **Event chain (§5.5).** ``head(e) = H("orch/v2/event|" || cj(e))`` over the full strictly parsed event, ``host_sig``
  included; ``prev`` of event ``n`` is the head of event ``n-1``, ``null`` for ``seq`` 1. ``host_sig`` covers
  ``cj({contract, suite, workspace_id, log, event: E})`` with ``E`` the event without ``host_sig`` (so it includes
  ``seq``, ``at``, ``prev``, ``ws_seq`` and ``sig``), under ``orch/v2/sig/host-event|``. A person's ``sig`` covers
  the same context with ``E`` the event without ``seq``, ``at``, ``prev``, ``ws_seq``, ``sig`` and ``host_sig``,
  under ``orch/v2/sig/ticket-event|`` (log = the ticket uid) or ``orch/v2/sig/ws-event|`` (log = ``"workspace"``).
  The signed-context object uses the key ``contract`` (ticket-format §5.3, §5.5, decisions log row 57).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from . import jcs
from .text import TextError, check_text

__all__ = [
    "ARTIFACT_KINDS",
    "CONTRACT",
    "GATES",
    "GATE_KEYS",
    "HASH_V",
    "LABELS",
    "SUITE",
    "ChainError",
    "HashError",
    "artifact_digest",
    "canonical_policy",
    "check_chain",
    "check_repo_identity",
    "cj_checked",
    "check_hash_v",
    "event_head",
    "event_line",
    "format_hash",
    "gate_hash",
    "grant_secret_hash",
    "host_signing_bytes",
    "log_head",
    "parse_event_line",
    "parse_hash",
    "people_hash",
    "person_signing_bytes",
    "policy_hash",
    "question_hash",
    "question_id",
    "same_repo_identity",
    "section_hash",
    "signed_context",
    "value_hash",
]

HASH_V = 1
CONTRACT = 1  # signed-event contract version (§5.3)
SUITE = 2  # deployment suite (§5.3, D46)
PREFIX = "sha256:"

LABELS: dict[str, str] = {
    "gate": "orch/v2/gate|",
    "section": "orch/v2/section|",
    "value": "orch/v2/value|",
    "policy": "orch/v2/policy|",
    "people": "orch/v2/people|",
    "question_id": "orch/v2/question-id|",
    "question": "orch/v2/question|",
    "event": "orch/v2/event|",
    "grant_secret": "orch/v2/grant-secret|",
    "sig_ticket_event": "orch/v2/sig/ticket-event|",
    "sig_ws_event": "orch/v2/sig/ws-event|",
    "sig_host_event": "orch/v2/sig/host-event|",
    "sig_checkpoint": "orch/v2/sig/checkpoint|",
}
"""Every domain label this package defines (ticket ``§5.6``). ``tests/vectors/f1/labels.json`` pins them and the
tests check they are prefix-free together with the protocol's own labels."""

GATES = ("requirements", "plan", "verify", "code")
GATE_KEYS = (
    "workspace_id",
    "uid",
    "gate",
    "schema",
    "hash_v",
    "sections",
    "fields",
    "addon_packages",
    "tasks",
    "artifacts",
    "receipts",
    "source_sha",
    "prior",
    "policy_hash",
    "people_hash",
)
SCHEMA = "orch.ticket/2"
# Core sections of each gate by ticket type (§4 table; "yes" and "optional" are present, "-" is absent). The gate
# hash input has exactly these keys (a missing section is the hash of ""), plus addon sections.
_REQ = ("summary", "context", "requirements", "out_of_scope")
_CORE_SECTIONS = {
    "requirements": {
        "feature": _REQ,
        "bug": _REQ,
        "chore": ("summary", "context", "requirements"),
        "spike": ("summary", "context", "requirements"),
        "epic": _REQ,
    },
    "plan": {
        "feature": ("plan", "decisions"),
        "bug": ("plan", "decisions"),
        "chore": ("plan", "decisions"),
        "spike": ("plan", "decisions"),
        "epic": ("decisions",),
    },
    "verify": {
        "feature": ("verification",),
        "bug": ("verification",),
        "chore": (),
        "spike": ("findings",),
        "epic": (),
    },
    "code": dict.fromkeys(("feature", "bug", "chore", "spike", "epic"), ()),
}
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_SECTION_BYTES = 65_536
TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
SIZES = ("xs", "s", "m", "l", "xl")
ARTIFACT_KINDS = ("screenshot", "log", "report", "link", "dataset", "build", "diagram", "receipt", "feedback", "other")
APPROVER_TOKENS = ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_AC = re.compile(r"AC[1-9][0-9]*")
_TASK = re.compile(r"T[1-9][0-9]*")
_QUESTION = re.compile(r"Q[1-9][0-9]*")
_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_OCTET = re.compile(r"0|[1-9][0-9]{0,2}")
_REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class HashError(ValueError):
    """Unknown ``hash_v``, a malformed hash string or id, a malformed hash input, or text that breaks §11.3."""


class ChainError(HashError):
    """A log breaks the chain rules of §5.5 (``seq`` gap, wrong ``prev``); ``seq`` is the offending position."""

    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"chain broken at seq {seq}: {reason}")
        self.seq = seq


# --- basic forms -----------------------------------------------------------------------------------------------


def check_hash_v(hash_v: object) -> int:
    """Return ``hash_v`` if it is exactly the int ``1``; refuse anything else (0, 2, True, 1.0, "1", None)."""
    if type(hash_v) is not int or hash_v != HASH_V:
        raise HashError(f"unsupported hash_v {hash_v!r}")
    return hash_v


def format_hash(digest_hex: str) -> str:
    """``"sha256:" + digest_hex``; ``digest_hex`` must be exactly 64 lower-case hex characters."""
    if not isinstance(digest_hex, str) or not _HEX64.fullmatch(digest_hex):
        raise HashError("digest must be 64 lower-case hex characters")
    return PREFIX + digest_hex


def parse_hash(s: object) -> bytes:
    """The one hash parser: the 32 digest bytes of a canonical ``sha256:<64 lower-case hex>``; refuse anything else."""
    if not isinstance(s, str) or not s.startswith(PREFIX) or not _HEX64.fullmatch(s[len(PREFIX) :]):
        raise HashError("not a canonical 'sha256:<64 hex>' hash string")
    return bytes.fromhex(s[len(PREFIX) :])


def artifact_digest(data: bytes) -> str:
    """The artifact digest: ``sha256:`` + hex SHA-256 of the file bytes, unlabelled (equals ``sha256sum``)."""
    if not isinstance(data, bytes):
        raise HashError("artifact bytes must be bytes")
    return PREFIX + hashlib.sha256(data).hexdigest()


def _labelled(label_key: str, data: bytes) -> str:
    return PREFIX + hashlib.sha256(LABELS[label_key].encode("ascii") + data).hexdigest()


def _check_strings(o: Any) -> None:
    """Every string (values and keys) of an already-serialisable object follows §11.3."""
    if type(o) is str:
        check_text(o)
    elif type(o) is list:
        for v in o:
            _check_strings(v)
    elif type(o) is dict:
        for k, v in o.items():
            check_text(k)
            _check_strings(v)


def cj_checked(obj: Any) -> bytes:
    """``cj(obj)`` after refusing any string that breaks the text rules; :class:`HashError` on every failure."""
    try:
        data = jcs.dumps(obj)
        _check_strings(obj)
    except (jcs.JcsError, TextError) as e:
        raise HashError(str(e)) from None
    return data


def _text(value: object, what: str, *, one_line: bool = False, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise HashError(f"{what} must be a string")
    try:
        check_text(value, one_line=one_line)
    except TextError as e:
        raise HashError(f"{what}: {e}") from None
    if nonempty and not value:
        raise HashError(f"{what} must not be empty")
    return value


def _match(rx: re.Pattern[str], value: object, what: str) -> str:
    if type(value) is not str or not rx.fullmatch(value):
        raise HashError(f"{what} is malformed: {value!r}"[:200])
    return value


def _obj(value: object, keys: tuple[str, ...], what: str, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    if type(value) is not dict:
        raise HashError(f"{what} must be an object")
    missing = [k for k in keys if k not in value]
    extra = [k for k in value if k not in keys and k not in optional]
    if missing or extra:
        raise HashError(f"{what} keys: missing {missing}, unexpected {extra}")
    return value


def _int(value: object, what: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise HashError(f"{what} must be an int" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def _enum(value: object, allowed: tuple[str, ...], what: str) -> str:
    if type(value) is not str or value not in allowed:
        raise HashError(f"{what} must be one of {list(allowed)}")
    return value


def _list(value: object, what: str) -> list[Any]:
    if type(value) is not list:
        raise HashError(f"{what} must be a list")
    return value


def _strictly_sorted(items: list[str], what: str) -> None:
    if any(a >= b for a, b in zip(items, items[1:], strict=False)):
        raise HashError(f"{what} must be sorted and without duplicates")


# --- section, value, grant secret ------------------------------------------------------------------------------


def section_hash(text: str) -> str:
    """``H("orch/v2/section|" || UTF-8(text))`` of one body section; a missing section is the hash of ``""``.

    ``text`` must already follow §11.3 and be section text as §4 defines it: no leading or trailing LF, at most
    65 536 UTF-8 bytes (refused, not trimmed). The stored map is keyed by section id, so moving text between
    sections is a change; the key is not part of this hash.
    """
    _text(text, "section text")
    if text.startswith("\n") or text.endswith("\n"):
        raise HashError("section text has a leading or trailing LF (§4 trims them before hashing)")
    data = text.encode("utf-8")
    if len(data) > MAX_SECTION_BYTES:
        raise HashError("section text is longer than 65 536 bytes")
    return _labelled("section", data)


def value_hash(value: Any) -> str:
    """``H("orch/v2/value|" || cj(value))``: the ``base_rev`` of a ``ticket.json`` path (§5.6)."""
    return _labelled("value", cj_checked(value))


def grant_secret_hash(secret: bytes) -> str:
    """``H("orch/v2/grant-secret|" || secret)`` as stored by ``grant.issued``; ``secret`` is the 32 decoded bytes
    (not their b64u text)."""
    if type(secret) is not bytes or len(secret) != 32:
        raise HashError("grant secret must be exactly 32 bytes")
    return _labelled("grant_secret", secret)


# --- policy and people ------------------------------------------------------------------------------------------


def _token_set(value: object, what: str, *, nonempty: bool) -> list[str]:
    items = _list(value, what)
    for t in items:
        _enum(t, APPROVER_TOKENS, f"{what} item")
    if nonempty and not items:
        raise HashError(f"{what} must name at least one token")
    return sorted(set(items))


def canonical_policy(policy: object) -> dict[str, Any]:
    """The policy object in canonical form (§5.7): all five keys, ``approvers``/``not``/``applies`` sorted, unique.

    ``applies`` is ``"all"``, ``"off"`` or a non-empty list of ticket types. Raises :class:`HashError` otherwise.
    """
    p = _obj(policy, ("approvers", "count", "not", "applies", "independent"), "policy")
    applies = p["applies"]
    if applies not in ("all", "off"):
        items = _list(applies, "policy.applies")
        for t in items:
            _enum(t, TICKET_TYPES, "policy.applies item")
        if not items:
            raise HashError("policy.applies list must not be empty")
        applies = sorted(set(items))
    if type(p["independent"]) is not bool:
        raise HashError("policy.independent must be a bool")
    return {
        "approvers": _token_set(p["approvers"], "policy.approvers", nonempty=True),
        "count": _int(p["count"], "policy.count", 1),
        "not": _token_set(p["not"], "policy.not", nonempty=False),
        "applies": applies,
        "independent": p["independent"],
    }


def policy_hash(gate: str, policy: Mapping[str, Any]) -> str:
    """``H("orch/v2/policy|" || cj({"gate": gate, "policy": P}))``, ``P`` the effective policy in canonical form."""
    _enum(gate, GATES, "gate")
    return _labelled("policy", cj_checked({"gate": gate, "policy": canonical_policy(policy)}))


def people_hash(people: Mapping[str, Any]) -> str:
    """``H("orch/v2/people|" || cj({role: value}))`` over the roles the caller selected (§5.6).

    Keys are ticket roles (``ticket_owner`` for the owner, never ``owner``); ``ticket_owner`` maps to a person id or
    ``None``, the others to lists of person ids (sorted and de-duplicated here). Which roles to include (those the
    gate's effective policy names in ``approvers`` or ``not``, plus ``assignees`` when ``independent``) is the
    caller's decision; the hash covers exactly what it is given.
    """
    if type(people) is not dict:
        raise HashError("people must be an object")
    out: dict[str, Any] = {}
    for role, value in people.items():
        _enum(role, TICKET_ROLES, "people role")
        if role == "ticket_owner":
            out[role] = None if value is None else _match(_PERSON, value, "ticket_owner")
        else:
            out[role] = sorted({_match(_PERSON, p, f"{role} item") for p in _list(value, f"people.{role}")})
    return _labelled("people", cj_checked(out))


# --- questions --------------------------------------------------------------------------------------------------


def question_id(workspace_id: str, ticket_uid: str, question: str) -> str:
    """``qid``: the first 16 bytes, as 32 lower-case hex (not ``sha256:``), of
    ``H("orch/v2/question-id|" || cj({"workspace_id": W, "ticket": uid, "question": "Q1"}))`` (§5.6)."""
    body = {
        "workspace_id": _match(_HEX32, workspace_id, "workspace_id"),
        "ticket": _match(_ULID, ticket_uid, "ticket uid"),
        "question": _match(_QUESTION, question, "question id"),
    }
    full = hashlib.sha256(LABELS["question_id"].encode("ascii") + cj_checked(body)).digest()
    return full[:16].hex()


def question_hash(qid: str, ticket_uid: str, text: str, options: list[dict[str, str]] | None = None) -> str:
    """``H("orch/v2/question|" || cj({"question_id": qid, "ticket": uid, "text", "options"}))`` (§5.6).

    ``options`` is ``[]`` when there are none (``None`` is accepted as that); each option is ``{key, label}`` with an
    optional ``cost``, all strings.
    """
    opts = [] if options is None else _list(options, "options")
    for o in opts:
        _obj(o, ("key", "label"), "option", ("cost",))
        for v in o.values():
            _text(v, "option field", one_line=True)
    body = {
        "question_id": _match(_HEX32, qid, "question_id"),
        "ticket": _match(_ULID, ticket_uid, "ticket uid"),
        "text": _text(text, "question text"),
        "options": opts,
    }
    return _labelled("question", cj_checked(body))


# --- the gate hash ----------------------------------------------------------------------------------------------


def _check_sections(sections: object, gate: str, ticket_type: str) -> None:
    """Exactly the core sections of (gate, ticket type) must be present, plus any ``<addon>.<token>`` sections."""
    if type(sections) is not dict:
        raise HashError("sections must be an object")
    core = set(_CORE_SECTIONS[gate][ticket_type])
    present = set()
    for sid, h in sections.items():
        if type(sid) is not str:
            raise HashError("section id must be a string")
        if "." in sid:
            addon, _, tok = sid.partition(".")
            _match(_TOKEN, addon, "section addon")
            _match(_TOKEN, tok, "section token")
        elif sid in core:
            present.add(sid)
        else:
            raise HashError(f"section {sid!r} does not exist for gate {gate!r} and type {ticket_type!r}")
        parse_hash(h)
    if present != core:
        raise HashError(f"sections missing for gate {gate!r}: {sorted(core - present)} (a missing one is H(''))")


def _check_links(links: object) -> None:
    lk = _obj(links, ("repos", "branches", "prs", "external"), "fields.links")
    for r in _list(lk["repos"], "links.repos"):
        _match(_REPO_NAME, r, "links.repos item")
    br = lk["branches"]
    if type(br) is not dict:
        raise HashError("links.branches must be an object")
    for k, v in br.items():
        _match(_REPO_NAME, k, "links.branches key")
        _text(v, "links.branches value", one_line=True, nonempty=True)
    for pr in _list(lk["prs"], "links.prs"):
        _obj(pr, ("repo", "url"), "links.prs item")
        _match(_REPO_NAME, pr["repo"], "links.prs repo")
        _text(pr["url"], "links.prs url", one_line=True, nonempty=True)
    for u in _list(lk["external"], "links.external"):
        _text(u, "links.external item", one_line=True, nonempty=True)


def _check_fields(fields: object, gate: str) -> None:
    f = _obj(fields, ("ticket_type", "size", "acceptance", "links", "addons"), "fields")
    _enum(f["ticket_type"], TICKET_TYPES, "fields.ticket_type")
    if f["size"] is not None:
        _enum(f["size"], SIZES, "fields.size")
    seen = set()
    for ac in _list(f["acceptance"], "fields.acceptance"):
        _obj(ac, ("id", "text"), "acceptance item")
        if _match(_AC, ac["id"], "acceptance id") in seen:
            raise HashError("duplicate acceptance id")
        seen.add(ac["id"])
        _text(ac["text"], "acceptance text", nonempty=True)
    if gate in ("verify", "code"):
        _check_links(f["links"])
    elif f["links"] is not None:
        raise HashError("fields.links must be null for this gate")
    addons = f["addons"]
    if type(addons) is not dict:
        raise HashError("fields.addons must be an object")
    for name, vals in addons.items():
        _match(_TOKEN, name, "addon name")
        if type(vals) is not dict:
            raise HashError("fields.addons values must be objects")
        for fld in vals:
            _match(_TOKEN, fld, "addon field")


def _check_tasks(tasks: object, gate: str) -> None:
    items = _list(tasks, "tasks")
    if gate != "plan":
        if items:
            raise HashError("tasks must be [] for every gate but plan")
        return
    seen = set()
    for t in items:
        _obj(t, ("id", "text", "verify", "proves"), "task")
        if _match(_TASK, t["id"], "task id") in seen:
            raise HashError("duplicate task id")
        seen.add(t["id"])
        _text(t["text"], "task text", nonempty=True)
        if t["verify"] is not None:
            _text(_obj(t["verify"], ("cmd",), "task.verify")["cmd"], "task.verify.cmd", nonempty=True)
        for ac in _list(t["proves"], "task.proves"):
            _match(_AC, ac, "task.proves item")


def _check_artifacts(artifacts: object, gate: str) -> None:
    if type(artifacts) is not dict:
        raise HashError("artifacts must be an object")
    if gate == "code" and artifacts:
        raise HashError("artifacts must be {} for the code gate")
    for name, a in artifacts.items():
        _match(_ARTIFACT_NAME, name, "artifact name")
        _obj(a, ("kind", "digest", "ac", "task"), "artifact")
        _enum(a["kind"], ARTIFACT_KINDS, "artifact kind")
        parse_hash(a["digest"])
        if a["ac"] is not None:
            _match(_AC, a["ac"], "artifact ac")
        if a["task"] is not None:
            _match(_TASK, a["task"], "artifact task")


def _check_receipts(receipts: object, gate: str) -> None:
    if type(receipts) is not dict:
        raise HashError("receipts must be an object")
    if gate != "verify" and receipts:
        raise HashError("receipts must be {} for every gate but verify")
    for tid, r in receipts.items():
        _match(_TASK, tid, "receipt task id")
        _obj(r, ("event", "repo", "commit", "exit"), "receipt")
        _match(_ULID, r["event"], "receipt event")
        if r["repo"] is not None:  # a receipt of a command that is not tied to a repo (§5.4.1, §6)
            _match(_REPO_NAME, r["repo"], "receipt repo")
        if r["commit"] is not None:
            _match(_COMMIT, r["commit"], "receipt commit")
        _int(r["exit"], "receipt exit")


def check_repo_identity(value: object) -> str:
    """Return ``value`` if it is a canonical repo identity (§5.7 "Repo identity"), else raise :class:`HashError`.

    Either ``local:<repo name>`` or ``https://host[:port]/path`` where: host labels are
    ``[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`` joined by single dots (at most 253 characters, no trailing dot, ``xn--``
    allowed, no Unicode or IPv6; an all-numeric last label only as a plain dotted quad of octets without leading
    zeros); port ``[1-9][0-9]{0,4}`` up to 65535 and never 443; path segments ``[A-Za-z0-9._~-]+`` joined by single
    slashes, none empty, ``.`` or ``..``, no trailing slash, no ``.git`` suffix in any case. Everything else
    (userinfo, ``%``, ``?``, ``#``, whitespace, non-ASCII) is refused, never converted.
    """
    if type(value) is not str or not value.isascii():
        raise HashError("repo identity must be an ASCII string")
    if value.startswith("local:"):
        _match(_REPO_NAME, value[6:], "repo identity")
        return value
    bad = HashError(f"repo identity is not canonical: {value[:100]!r}")
    if not value.startswith("https://"):
        raise bad
    authority, slash, path = value[8:].partition("/")
    if not slash:
        raise bad
    host, colon, port = authority.partition(":")
    if colon and (not _PORT.fullmatch(port) or int(port) > 65535 or port == "443"):
        raise bad
    labels = host.split(".")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(x) for x in labels):
        raise bad
    if labels[-1].isdigit() and not (len(labels) == 4 and all(_OCTET.fullmatch(x) and int(x) < 256 for x in labels)):
        raise bad
    segments = path.split("/")
    if not all(_PATH_SEGMENT.fullmatch(x) and x not in (".", "..") for x in segments) or path.lower().endswith(".git"):
        raise bad
    return value


def same_repo_identity(a: str, b: str) -> bool:
    """Whether two canonical identities name one repo: equal ignoring ASCII case (§5.7). Hashes keep the raw form."""
    return check_repo_identity(a).lower() == check_repo_identity(b).lower()


def _check_source_sha(source: object, gate: str) -> None:
    items = _list(source, "source_sha")
    if gate not in ("verify", "code"):
        if items:
            raise HashError("source_sha must be [] for requirements and plan")
        return
    repos = []
    for s in items:
        _obj(s, ("repo", "ref", "sha"), "source entry")
        repos.append(check_repo_identity(s["repo"]))
        ref = _text(s["ref"], "source ref", one_line=True)
        if not ref.startswith("refs/heads/") or ref == "refs/heads/":
            raise HashError("source ref must be refs/heads/<branch>")
        _match(_COMMIT, s["sha"], "source sha")
    _strictly_sorted(repos, "source_sha repos")
    if len({r.lower() for r in repos}) != len(repos):
        raise HashError("source_sha repos name one identity twice (compared ignoring ASCII case)")


def _check_prior(prior: object, gate: str) -> None:
    if type(prior) is not dict:
        raise HashError("prior must be an object")
    earlier = GATES[: GATES.index(gate)]
    for g, p in prior.items():
        if g not in earlier:
            raise HashError(f"prior may only name gates before {gate!r}")
        _obj(p, ("gen", "approvals"), "prior entry")
        _int(p["gen"], "prior gen", 0)
        ids = [_match(_ULID, a, "prior approval id") for a in _list(p["approvals"], "prior approvals")]
        _strictly_sorted(ids, "prior approvals")


def gate_hash(g: Mapping[str, Any]) -> str:
    """``H("orch/v2/gate|" || cj(G))`` (§5.5-§5.7). ``G`` must have exactly the 15 keys of :data:`GATE_KEYS`.

    Each input is validated against the table of §5.7 (types, id forms, enums, ``sha256:`` strings, the per-gate
    emptiness rules: ``tasks`` only on ``plan``, ``receipts`` only on ``verify``, ``source_sha`` and ``links`` only
    on ``verify``/``code``, ``code`` has no sections and no artifacts, ``prior`` names only earlier gates), every
    string must already follow §11.3, and ``G`` must be within the ``cj`` limits. Raises :class:`HashError`.
    """
    if type(g) is not dict:
        raise HashError("G must be an object")
    _obj(g, GATE_KEYS, "G")
    _match(_HEX32, g["workspace_id"], "workspace_id")
    _match(_ULID, g["uid"], "uid")
    gate = _enum(g["gate"], GATES, "gate")
    if g["schema"] != SCHEMA or type(g["schema"]) is not str:
        raise HashError(f"schema must be {SCHEMA!r}")
    check_hash_v(g["hash_v"])
    _check_fields(g["fields"], gate)
    _check_sections(g["sections"], gate, g["fields"]["ticket_type"])
    pk = g["addon_packages"]
    if type(pk) is not dict:
        raise HashError("addon_packages must be an object")
    for name, h in pk.items():
        _match(_TOKEN, name, "addon name")
        parse_hash(h)
    _check_tasks(g["tasks"], gate)
    _check_artifacts(g["artifacts"], gate)
    _check_receipts(g["receipts"], gate)
    _check_source_sha(g["source_sha"], gate)
    _check_prior(g["prior"], gate)
    parse_hash(g["policy_hash"])
    parse_hash(g["people_hash"])
    return _labelled("gate", cj_checked(g))


# --- events: head, chain, signed bytes --------------------------------------------------------------------------


def _event(event: object) -> dict[str, Any]:
    if type(event) is not dict:
        raise HashError("event must be an object")
    check_hash_v(event.get("hash_v"))
    return event


def event_head(event: Mapping[str, Any]) -> str:
    """``head(e) = H("orch/v2/event|" || cj(e))`` over the full event, ``host_sig`` included (§5.5).

    ``event`` is the strictly parsed object; to hash a log line use :func:`parse_event_line` first, never the
    raw bytes.
    """
    return _labelled("event", cj_checked(_event(event)))


def event_line(event: Mapping[str, Any]) -> bytes:
    """One log line: ``cj(event)`` followed by one LF (§5)."""
    return cj_checked(_event(event)) + b"\n"


def parse_event_line(line: bytes) -> dict[str, Any]:
    """Strictly parse one log line (including its LF) and check it is exactly ``cj`` of what it parses to.

    A line that is not canonical (extra whitespace, other key order, a different escape, no or a doubled LF) is
    refused, because §5 treats it like a bad ``host_sig``. The root must be an object whose strings follow §11.3.
    """
    if not isinstance(line, bytes) or not line.endswith(b"\n") or line.endswith(b"\n\n"):
        raise HashError("an event line is cj(event) followed by exactly one LF")
    try:
        obj = jcs.loads_strict(line[:-1])
    except jcs.JcsError as e:
        raise HashError(str(e)) from None
    if type(obj) is not dict:
        raise HashError("the root of an event line must be an object")
    if cj_checked(obj) != line[:-1]:
        raise HashError("line is not canonical JSON")
    return obj


def log_head(events: list[Mapping[str, Any]]) -> str | None:
    """The head of the last event; an empty log has no head (``None``)."""
    if type(events) is not list:
        raise HashError("events must be a list")
    return event_head(events[-1]) if events else None


def check_chain(events: list[Mapping[str, Any]]) -> list[str]:
    """Check ``seq`` runs 1, 2, ... without gaps and ``prev`` is ``None`` for ``seq`` 1, else the previous head.

    Returns the heads. Raises :class:`ChainError` at the first event that breaks it. Signatures, ``ws_seq``,
    ``based_on`` and authorisation are not checked here.
    """
    if type(events) is not list:
        raise HashError("events must be a list")
    heads: list[str] = []
    for i, e in enumerate(events):
        if type(e) is not dict:
            raise ChainError(i + 1, "event is not an object")
        if "prev" not in e:
            raise ChainError(i + 1, "event has no prev")
        seq = e.get("seq")
        if type(seq) is not int or seq != i + 1:
            raise ChainError(i + 1, f"seq is {seq!r}, expected {i + 1}")
        expected = heads[-1] if heads else None
        if e.get("prev") != expected:
            raise ChainError(seq, f"prev is {e.get('prev')!r}, expected {expected!r}")
        heads.append(event_head(e))
    return heads


_SIG_LABELS = {"sig_ticket_event", "sig_ws_event", "sig_host_event"}
_PERSON_EXCLUDED = ("seq", "at", "prev", "ws_seq", "sig", "host_sig")


def signed_context(
    label_key: str,
    workspace_id: str,
    log: str,
    event: Mapping[str, Any],
    *,
    contract: int = CONTRACT,
    suite: int = SUITE,
) -> bytes:
    """``label || cj({"contract", "suite", "workspace_id", "log", "event"})``: the bytes a signer signs (§5.3, §5.5).

    ``label_key`` is one of ``sig_ticket_event`` (``log`` is the ticket uid), ``sig_ws_event`` (``log`` is
    ``"workspace"``) or ``sig_host_event`` (either). ``event`` is used exactly as given: the caller removes the
    fields the signature does not cover (use :func:`person_signing_bytes` / :func:`host_signing_bytes`). Only
    ``contract`` 1 and ``suite`` 2 exist; anything else is refused. This function builds bytes; signing is C2.
    """
    if label_key not in _SIG_LABELS:
        raise HashError(f"unknown signing label {label_key!r}")
    if type(contract) is not int or contract != CONTRACT:
        raise HashError(f"unsupported signed-event contract {contract!r}")
    if type(suite) is not int or suite != SUITE:
        raise HashError(f"unsupported suite {suite!r}")
    _match(_HEX32, workspace_id, "workspace_id")
    if log == "workspace":
        if label_key == "sig_ticket_event":
            raise HashError("the ticket-event label needs a ticket uid as log")
    else:
        _match(_ULID, log, "log")
        if label_key == "sig_ws_event":
            raise HashError("the ws-event label needs log 'workspace'")
    _event(event)
    ctx = {"contract": contract, "suite": suite, "workspace_id": workspace_id, "log": log, "event": dict(event)}
    return LABELS[label_key].encode("ascii") + cj_checked(ctx)


def person_signing_bytes(workspace_id: str, log: str, event: Mapping[str, Any]) -> bytes:
    """The bytes a person's device key signs: ``E`` is ``event`` without ``seq``, ``at``, ``prev``, ``ws_seq``, ``sig``
    and ``host_sig`` (§5.3). ``log`` is the ticket uid, or ``"workspace"`` for the workspace log."""
    e = {k: v for k, v in _event(event).items() if k not in _PERSON_EXCLUDED}
    return signed_context("sig_ws_event" if log == "workspace" else "sig_ticket_event", workspace_id, log, e)


def host_signing_bytes(workspace_id: str, log: str, event: Mapping[str, Any]) -> bytes:
    """The bytes the workspace key signs as ``host_sig``: ``E`` is the full event without ``host_sig`` (§5.5), so it
    includes ``seq``, ``at``, ``prev``, ``ws_seq`` and ``sig``. ``seq``, ``at`` and ``prev`` must be present."""
    e = {k: v for k, v in _event(event).items() if k != "host_sig"}
    for k in ("seq", "at", "prev"):
        if k not in e:
            raise HashError(f"event has no {k!r}: the host signs the full event")
    return signed_context("sig_host_event", workspace_id, log, e)
