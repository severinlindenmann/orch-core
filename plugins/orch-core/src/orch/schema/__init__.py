"""JSON Schemas for the v2 format (JSON Schema 2020-12), with a small loader and validator.

The schemas live as ``.json`` files in ``schemas/`` (package data). Names are the file stems, for example
``ticket``, ``workspace``, ``event``, ``event.gate.approved``, ``gate-input`` or ``addon-manifest``. They follow
``docs/architecture/orch-v2-ticket-format.md`` (F1) section by section; each schema's ``description`` names the
section.

* ``names()``: every schema name.
* ``load(name)``: the schema as a dict (a copy).
* ``validate(name, obj, log=None)``: raises :class:`SchemaError` (with a JSON pointer) on the first problem.
  Besides the schema it enforces what JSON Schema cannot express: the text rules of section 11.3 (NFC, LF, no
  controls or bidi controls), the canonical JSON subset (no floats, safe integers, ASCII keys, depth 16), byte
  limits, real calendar dates, git ref names and the cross-field rules (unique ids, ``proves``, the per-actor
  rules of section 5.4, the genesis links of section 5.11, ...).
* ``validate("event", obj, log="ticket" | "workspace")`` dispatches on ``obj["type"]`` to ``event.<type>`` and
  checks the log-specific rules (``ws_seq`` only in ticket logs, which types belong to which log, seq 1). A type
  that is not in section 5.4 is refused, also ``<addon>.<verb>`` (custom addon events are deferred, A5).
* ``parse_json(text)``: the strict parser of ``orch.canon`` (:func:`orch.canon.loads_strict`), so that this module
  and the signing code accept and refuse the same inputs.

Every JSON Schema pattern ends in ``(?![\\s\\S])`` instead of ``$``: Python's ``$`` also matches before a final LF,
which would let ``"sha256:...\\n"`` through.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from orch.canon import dumps, loads_strict

__all__ = ["LOG_TYPES", "SchemaError", "event_types", "load", "names", "parse_json", "validate"]

BASE = "https://schemas.orch.dev/v2/"
MAX_STR_BYTES = 4096
MAX_SECTION_BYTES = 65_536
MAX_TICKET_BYTES = 262_144
MAX_EVENT_LINE_BYTES = 524_288
MAX_DEPTH = 16
MAX_SAFE_INT = 2**53 - 1
HANDOFF_MAX_BYTES = 2048

_WORKSPACE_ONLY = frozenset(
    {
        "workspace.created",
        "member.added",
        "member.removed",
        "role.changed",
        "device.added",
        "device.removed",
        "device.revoked",
        "settings.changed",
        "grant.issued",
        "grant.revoked",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
    }
)
_BOTH_LOGS = frozenset({"policy.changed", "projection.repaired", "restore", "invalid.acknowledged"})

_COMMON = "summary context requirements decisions current_state"
SECTIONS_BY_TYPE: dict[str, frozenset[str]] = {
    "feature": frozenset(f"{_COMMON} out_of_scope plan verification".split()),
    "bug": frozenset(f"{_COMMON} out_of_scope plan verification".split()),
    "chore": frozenset(f"{_COMMON} plan".split()),
    "spike": frozenset(f"{_COMMON} plan findings".split()),
    "epic": frozenset(f"{_COMMON} out_of_scope".split()),
}
GATE_SECTIONS = {
    "requirements": frozenset({"summary", "context", "requirements", "out_of_scope"}),
    "plan": frozenset({"plan", "decisions"}),
    "verify": frozenset({"verification", "findings"}),
    "code": frozenset(),
}
GATE_ORDER = ("requirements", "plan", "verify", "code")
_HOST_RELEASE_REASONS = frozenset({"expired", "grant_ended", "member_removed", "ticket_done", "ticket_closed"})


class SchemaError(ValueError):
    """A document does not match its schema. ``path`` is an RFC 6901 JSON pointer ("" is the root)."""

    def __init__(self, schema: str, path: str, message: str) -> None:
        super().__init__(f"{schema}: {path or '/'}: {message}")
        self.schema = schema
        self.path = path
        self.message = message


def _pointer(parts: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@cache
def _files() -> dict[str, Any]:
    root = resources.files(__package__).joinpath("schemas")
    return {p.name[: -len(".json")]: p for p in root.iterdir() if p.name.endswith(".json")}


def names() -> list[str]:
    """All schema names, sorted."""
    return sorted(_files())


@cache
def _raw(name: str) -> dict[str, Any]:
    try:
        path = _files()[name]
    except KeyError:
        raise KeyError(f"unknown schema {name!r}") from None
    return json.loads(path.read_text(encoding="utf-8"))


def load(name: str) -> dict[str, Any]:
    """Return a copy of the named schema."""
    return json.loads(json.dumps(_raw(name)))


@cache
def _registry() -> Registry:
    reg: Registry = Registry()
    for n in names():
        reg = reg.with_resource(BASE + n, Resource.from_contents(_raw(n), default_specification=DRAFT202012))
    return reg


@cache
def _validator(ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": BASE + ref}, registry=_registry())


def event_types(log: str | None = None) -> list[str]:
    """The event types of section 5.4: of one log (``"ticket"`` or ``"workspace"``), or all of them, sorted."""
    every = {n[len("event.") :] for n in names() if n.startswith("event.")}
    if log is None:
        return sorted(every)
    if log == "workspace":
        return sorted(_WORKSPACE_ONLY | _BOTH_LOGS)
    if log == "ticket":
        return sorted(every - _WORKSPACE_ONLY)
    raise ValueError(f"unknown log {log!r}")


LOG_TYPES = {"ticket": frozenset(event_types("ticket")), "workspace": frozenset(event_types("workspace"))}


def parse_json(text: str | bytes) -> Any:
    """Strict JSON parse: exactly :func:`orch.canon.loads_strict` (protocol-v2 section 2.3), raising ``ValueError``.

    Refuses duplicate keys, NaN/Infinity, floats, unsafe integers, non-ASCII or empty keys, lone surrogates, invalid
    UTF-8 and nesting deeper than 16, so no document passes here that the canonical serialiser would refuse.
    """
    return loads_strict(text)


# ---- text rules (section 11.3) and the canonical JSON subset (section 11.2) ----
_FORBIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩‎‏؜]")
_UNICODE_16 = unicodedata.unidata_version == "16.0.0"


def _check_string(name: str, value: str, parts: tuple[Any, ...], limit: int) -> None:
    p = _pointer(parts)
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaError(name, p, "text contains a lone surrogate") from None
    if "\r" in value:
        raise SchemaError(name, p, "text must use LF line endings")
    if not value.isascii() and not unicodedata.is_normalized("NFC", value):
        raise SchemaError(name, p, "text must be NFC-normalised")
    m = _FORBIDDEN.search(value)
    if m:
        raise SchemaError(name, p, f"text contains a forbidden control or bidi character U+{ord(m.group()):04X}")
    # The pinned Unicode version is 16.0 (11.3); older runtimes can not tell, so this check waits for bundled tables.
    if _UNICODE_16 and not value.isascii() and any(unicodedata.category(c) == "Cn" for c in value):
        raise SchemaError(name, p, "text contains a code point unassigned in Unicode 16.0")
    if len(raw) > limit:
        raise SchemaError(name, p, f"text exceeds {limit} bytes (UTF-8)")


def _walk(name: str, value: Any, parts: tuple[Any, ...] = (), depth: int = 0) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float):
        raise SchemaError(name, _pointer(parts), "floats are not allowed (use integers such as ms or cents)")
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise SchemaError(name, _pointer(parts), "integer outside +-(2^53 - 1)")
    elif isinstance(value, str):
        limit = MAX_SECTION_BYTES if name == "body" and parts[:1] == ("sections",) else MAX_STR_BYTES
        _check_string(name, value, parts, limit)
    elif isinstance(value, dict):
        if depth + 1 > MAX_DEPTH:
            raise SchemaError(name, _pointer(parts), f"nested deeper than {MAX_DEPTH} levels")
        for k, v in value.items():
            if not isinstance(k, str) or not k or not k.isascii():
                raise SchemaError(name, _pointer((*parts, k)), "object keys must be non-empty ASCII strings")
            _check_string(name, k, (*parts, k), MAX_STR_BYTES)
            _walk(name, v, (*parts, k), depth + 1)
    elif isinstance(value, list):
        if depth + 1 > MAX_DEPTH:
            raise SchemaError(name, _pointer(parts), f"nested deeper than {MAX_DEPTH} levels")
        for i, v in enumerate(value):
            _walk(name, v, (*parts, i), depth + 1)
    else:
        raise SchemaError(name, _pointer(parts), f"{type(value).__name__} is not JSON")


def _first_error(name: str, obj: Any, ref: str | None = None, prefix: tuple[Any, ...] = ()) -> None:
    errors = list(_validator(ref or name).iter_errors(obj))
    if not errors:
        return
    from jsonschema.exceptions import best_match

    # An unevaluatedProperties error is a by-product when a $ref'd envelope fails; report the real cause instead.
    primary = [e for e in errors if e.validator != "unevaluatedProperties"] or errors
    err = best_match(primary) or primary[0]
    raise SchemaError(name, _pointer((*prefix, *err.absolute_path)), err.message)


# ---- helpers for the cross-field rules ----
def _date(name: str, path: tuple[Any, ...], text: str, fmt: str) -> datetime:
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=UTC)
    except ValueError:
        raise SchemaError(name, _pointer(path), f"{text!r} is not a real calendar date/time") from None


def _ts(name: str, path: tuple[Any, ...], text: str) -> datetime:
    return _date(name, path, text, "%Y-%m-%dT%H:%M:%SZ")


_REF_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _valid_ref(ref: str) -> bool:
    """git check-ref-format for a full ref name such as ``refs/heads/feat/x``."""
    if _REF_BAD.search(ref) or ref.endswith(("/", ".")) or ".." in ref or "@{" in ref or "//" in ref or ref == "@":
        return False
    return all(c and not c.startswith(".") and not c.endswith(".lock") for c in ref.split("/"))


def _check_ref(name: str, path: tuple[Any, ...], ref: str) -> None:
    if not _valid_ref(ref):
        raise SchemaError(name, _pointer(path), f"{ref!r} is not a valid git ref name")


def _check_source_list(name: str, path: tuple[Any, ...], items: list[dict[str, Any]]) -> None:
    for i, e in enumerate(items):
        _check_ref(name, (*path, i, "ref"), e["ref"])
    repos = [e["repo"] for e in items]
    if repos != sorted(set(repos)):
        raise SchemaError(name, _pointer(path), "source list must be sorted by repo, one entry per repo")


def _check_question(name: str, path: tuple[Any, ...], q: dict[str, Any]) -> None:
    keys = [o["key"] for o in q.get("options", [])]
    if len(set(keys)) != len(keys):
        raise SchemaError(name, _pointer((*path, "options")), "duplicate option key")
    if "recommended" in q and q["recommended"] not in keys:
        raise SchemaError(name, _pointer((*path, "recommended")), "recommended is not an option key")


def _check_sections_map(name: str, path: tuple[Any, ...], sections: dict[str, Any]) -> None:
    for sid, v in sections.items():
        if v is not None and v["refs"] != sorted(v["refs"]):
            raise SchemaError(name, _pointer((*path, sid, "refs")), "refs must be sorted")


def _check_cert(name: str, path: tuple[Any, ...], cert: dict[str, Any]) -> None:
    o = cert["o"]
    if o["expires_ms"] is not None and o["expires_ms"] <= o["created_ms"]:
        raise SchemaError(name, _pointer((*path, "o", "expires_ms")), "expires_ms must be greater than created_ms")


# ---- documents ----
def _check_ticket(obj: dict[str, Any]) -> None:
    for field in ("acceptance", "tasks", "questions"):
        seen: set[str] = set()
        for i, x in enumerate(obj[field]):
            if x["id"] in seen:
                raise SchemaError("ticket", _pointer([field, i, "id"]), f"duplicate id {x['id']!r}")
            seen.add(x["id"])
    acs = {a["id"] for a in obj["acceptance"]}
    for i, t in enumerate(obj["tasks"]):
        for j, ac in enumerate(t["proves"]):
            if ac not in acs:
                raise SchemaError("ticket", _pointer(["tasks", i, "proves", j]), f"unknown acceptance criterion {ac!r}")
    for i, q in enumerate(obj["questions"]):
        _check_question("ticket", ("questions", i), q)
    if obj["key"] in obj["blocked_by"] or obj["key"] == obj["parent"]:
        raise SchemaError("ticket", "/key", "a ticket cannot be its own parent or blocker")
    if obj["due"] is not None:
        _date("ticket", ("due",), obj["due"], "%Y-%m-%d")
    for repo, branch in obj["links"]["branches"].items():
        _check_ref("ticket", ("links", "branches", repo), "refs/heads/" + branch)


def _check_body(obj: dict[str, Any]) -> None:
    allowed = SECTIONS_BY_TYPE[obj["type"]]
    for sid, text in obj["sections"].items():
        if "." not in sid and sid not in allowed:
            raise SchemaError("body", _pointer(["sections", sid]), f"a {obj['type']} ticket has no section {sid!r}")
        if text.startswith("\n") or text.endswith("\n"):
            raise SchemaError("body", _pointer(["sections", sid]), "section text has leading or trailing LF")


def _check_workspace(obj: dict[str, Any]) -> None:
    persons = [m["person"] for m in obj["members"]]
    for i, p in enumerate(persons):
        if p in persons[:i]:
            raise SchemaError("workspace", _pointer(["members", i, "person"]), f"duplicate member {p!r}")


_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _check_manifest(obj: dict[str, Any]) -> None:
    if any(unicodedata.category(c) in _INVISIBLE for c in obj["title"]):
        raise SchemaError("addon-manifest", "/title", "title contains an invisible character")
    for fname, f in obj.get("fields", {}).items():
        if f["type"] == "integer" and "min" in f and "max" in f and f["min"] > f["max"]:
            raise SchemaError("addon-manifest", _pointer(["fields", fname, "min"]), "min is greater than max")
    for key, label in (("sections", "id"), ("artifact_kinds", "kind")):
        seen: set[str] = set()
        for i, s in enumerate(obj.get(key, [])):
            if s[label] in seen:
                raise SchemaError("addon-manifest", _pointer([key, i, label]), f"duplicate {label} {s[label]!r}")
            seen.add(s[label])


def _check_gate_input(obj: dict[str, Any]) -> None:
    gate = obj["gate"]
    code_or_verify = gate in ("verify", "code")
    for sid in obj["sections"]:
        if gate == "code":
            raise SchemaError("gate-input", _pointer(["sections", sid]), "the code gate has no sections")
        if "." not in sid and sid not in GATE_SECTIONS[gate]:
            raise SchemaError("gate-input", _pointer(["sections", sid]), f"section {sid!r} is not a section of {gate}")
    if (obj["fields"]["links"] is not None) != code_or_verify:
        raise SchemaError("gate-input", "/fields/links", "links is set for verify and code, null otherwise")
    if gate != "plan" and obj["tasks"]:
        raise SchemaError("gate-input", "/tasks", "tasks are only part of the plan gate")
    if gate == "code" and obj["artifacts"]:
        raise SchemaError("gate-input", "/artifacts", "the code gate has no artifacts")
    if gate != "verify" and obj["receipts"]:
        raise SchemaError("gate-input", "/receipts", "receipts are only part of the verify gate")
    if not code_or_verify and obj["source_sha"]:
        raise SchemaError("gate-input", "/source_sha", "source_sha is only part of verify and code")
    _check_source_list("gate-input", ("source_sha",), obj["source_sha"])
    earlier = GATE_ORDER[: GATE_ORDER.index(gate)]
    for g, p in obj["prior"].items():
        if g not in earlier:
            raise SchemaError("gate-input", _pointer(["prior", g]), f"{g!r} is not an earlier gate than {gate}")
        if p["approvals"] != sorted(p["approvals"]):
            raise SchemaError("gate-input", _pointer(["prior", g, "approvals"]), "approvals must be sorted")


# ---- events ----
def _check_event(obj: dict[str, Any], log: str | None) -> None:
    t, actor = obj["type"], obj["actor"]
    if log not in (None, "ticket", "workspace"):
        raise ValueError(f"unknown log {log!r}")
    if log is None and t not in _BOTH_LOGS:
        log = "workspace" if t in _WORKSPACE_ONLY else "ticket"
    if log is not None:
        if t not in LOG_TYPES[log]:
            raise SchemaError("event", "/type", f"{t!r} is not an event of the {log} log")
        if log == "ticket" and "ws_seq" not in obj:
            raise SchemaError("event", "", "ticket-log events carry ws_seq")
        if log == "workspace" and "ws_seq" in obj:
            raise SchemaError("event", "/ws_seq", "workspace-log events have no ws_seq")
        first = "ticket.created" if log == "ticket" else "workspace.created"
        if (t == first) != (obj["seq"] == 1):
            raise SchemaError("event", "/seq", f"seq 1 of a {log} log is {first} and nothing else is")
    _ts("event", ("at",), obj["at"])
    fn = _EVENT_CHECKS.get(t)
    if fn:
        fn(obj, actor)


def _ev_ticket_created(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    owner = actor["id"] if actor["kind"] == "person" else actor["for"]
    if obj["owner"] != owner:
        raise SchemaError("event", "/owner", "owner must be the person actor, or the agent's for")


def _ev_ticket_updated(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    touched = set(obj.get("set", {})) | {"body." + s for s in obj.get("sections", {})}
    if set(obj["base_rev"]) != touched:
        raise SchemaError("event", "/base_rev", "base_rev must name exactly the paths the edit touches")
    for path, value in obj.get("set", {}).items():
        key = path[len("ticket.") :]
        if "." in key:
            continue  # an addon field: its manifest decides
        _first_error("event", value, "ticket#/properties/" + key, ("set", path))
        if key in ("tasks", "acceptance"):
            ids = [x["id"] for x in value]
            if len(set(ids)) != len(ids):
                raise SchemaError("event", _pointer(["set", path]), "duplicate id")
        if key == "due" and value is not None:
            _date("event", ("set", path), value, "%Y-%m-%d")
        if key == "links":
            for repo, branch in value["branches"].items():
                _check_ref("event", ("set", path, "branches", repo), "refs/heads/" + branch)
    _check_sections_map("event", ("sections",), obj.get("sections", {}))


def _ev_edit_external(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    _check_sections_map("event", ("sections",), obj["sections"])


def _ev_claim_released(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    reason = obj["reason"]
    if actor["kind"] == "agent":
        if obj["session"] != actor["session"]:
            raise SchemaError("event", "/session", "an agent releases only its own claim")
        if reason not in ("released", "handoff"):
            raise SchemaError("event", "/reason", "an agent releases with released or handoff")
    elif actor["kind"] == "person":
        if reason != "released":
            raise SchemaError("event", "/reason", "a person releases with released")
    elif reason not in _HOST_RELEASE_REASONS:
        raise SchemaError("event", "/reason", "the host never releases for released or handoff")


def _ev_handoff(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    if len(obj["text"].encode("utf-8")) > HANDOFF_MAX_BYTES:
        raise SchemaError("event", "/text", f"handoff text exceeds {HANDOFF_MAX_BYTES} bytes")


def _ev_question_asked(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    _check_question("event", ("question",), obj["question"])


def _ev_source(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    if "source_sha" in obj:
        _check_source_list("event", ("source_sha",), obj["source_sha"])


def _ev_branch_pushed(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    _check_ref("event", ("ref",), obj["ref"])
    if obj["before"] is not None:
        _check_ref("event", ("before", "ref"), obj["before"]["ref"])


def _ev_restore(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    if obj["seq"] != obj["from_seq"] + 1:
        raise SchemaError("event", "/seq", "a restore is appended as from_seq + 1")
    if obj["prev"] != obj["head"]:
        raise SchemaError("event", "/prev", "a restore is appended with prev = head")
    if obj["abandoned"] is not None and obj["abandoned"]["seq"] <= obj["from_seq"]:
        raise SchemaError("event", "/abandoned/seq", "abandoned must lie above from_seq")


def _ev_invalid_ack(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    if obj["invalid_seq"] >= obj["seq"]:
        raise SchemaError("event", "/invalid_seq", "the acknowledgement names an earlier event")


def _ev_workspace_created(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    d, c = obj["delegation"]["o"], obj["device_cert"]
    pid = d["owner_person_id"]
    if not (obj["owner"]["person"] == "p_" + pid and c["o"]["person_id"] == pid):
        raise SchemaError("event", "/owner/person", "owner, delegation and device_cert must name the same person")
    if d["workspace_id"] != obj["workspace_id"]:
        raise SchemaError("event", "/delegation/o/workspace_id", "delegation is for another workspace")
    if d["wsk_pub"] != obj["wsk_pub"]:
        raise SchemaError("event", "/delegation/o/wsk_pub", "delegation is for another workspace key")
    if actor["id"] != obj["owner"]["person"] or actor["device"] != "d_" + c["o"]["device_id"]:
        raise SchemaError("event", "/actor", "the genesis is signed by the owner's first device")
    _check_cert("event", ("device_cert",), c)


def _ev_member_added(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    if obj["person"] != "p_" + obj["device_cert"]["o"]["person_id"]:
        raise SchemaError("event", "/device_cert/o/person_id", "device_cert is for another person")
    _check_cert("event", ("device_cert",), obj["device_cert"])


def _ev_device_added(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    o = obj["cert"]["o"]
    if obj["device"] != "d_" + o["device_id"]:
        raise SchemaError("event", "/cert/o/device_id", "cert is for another device")
    if "p_" + o["person_id"] != actor["id"]:
        raise SchemaError("event", "/cert/o/person_id", "a device is added by a device of the same person")
    _check_cert("event", ("cert",), obj["cert"])


def _ev_device_revoked(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    o = obj["revocation"]["o"]
    if obj["reason"] != o["reason"]:
        raise SchemaError("event", "/reason", "reason must equal revocation.o.reason")
    if obj["device"] != "d_" + o["device_id"]:
        raise SchemaError("event", "/revocation/o/device_id", "revocation is for another device")


def _ev_grant_issued(obj: dict[str, Any], actor: dict[str, Any]) -> None:
    issued = _ts("event", ("issued_at",), obj["issued_at"])
    expires = _ts("event", ("expires_at",), obj["expires_at"])
    if int((expires - issued).total_seconds()) != 3600 * obj["hours"]:
        raise SchemaError("event", "/expires_at", "expires_at must be issued_at + 3600 * hours seconds")
    if abs((_ts("event", ("at",), obj["at"]) - issued).total_seconds()) > 300:
        raise SchemaError("event", "/issued_at", "issued_at is more than 300 s away from at")


_EVENT_CHECKS = {
    "ticket.created": _ev_ticket_created,
    "ticket.updated": _ev_ticket_updated,
    "edit.external": _ev_edit_external,
    "claim.released": _ev_claim_released,
    "handoff.written": _ev_handoff,
    "question.asked": _ev_question_asked,
    "gate.approved": _ev_source,
    "verdict.given": _ev_source,
    "branch.pushed": _ev_branch_pushed,
    "restore": _ev_restore,
    "invalid.acknowledged": _ev_invalid_ack,
    "workspace.created": _ev_workspace_created,
    "member.added": _ev_member_added,
    "device.added": _ev_device_added,
    "device.revoked": _ev_device_revoked,
    "grant.issued": _ev_grant_issued,
}


def validate(name: str, obj: Any, *, log: str | None = None) -> None:
    """Validate ``obj`` against the named schema or raise :class:`SchemaError`.

    ``log`` (``"ticket"`` or ``"workspace"``) only matters for events; see the module docstring.
    """
    _raw(name)  # unknown name -> KeyError
    _walk(name, obj)
    try:
        line = dumps(obj)
    except (
        ValueError,
        TypeError,
    ) as e:  # the canonical serialiser is the reference: anything it refuses is refused here
        raise SchemaError(name, "", f"not canonical JSON: {e}") from None
    kind = name.split(".")[0]
    limit = {"ticket": MAX_TICKET_BYTES, "event": MAX_EVENT_LINE_BYTES - 1}.get(kind)  # an event line has an LF
    if limit is not None and len(line) > limit:
        raise SchemaError(name, "", f"{kind} exceeds {limit + (kind == 'event')} bytes")
    if name == "event":
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            _first_error("event", obj)
            raise SchemaError("event", "/type", "event needs a type")
        specific = "event." + obj["type"]
        if specific not in _files():
            _first_error("event", obj)
            raise SchemaError("event", "/type", f"unknown event type {obj['type']!r} (custom addon events are refused)")
        name = specific
    _first_error(name, obj)
    if name.startswith("event."):
        _check_event(obj, log)
    elif name == "ticket":
        _check_ticket(obj)
    elif name == "body":
        _check_body(obj)
    elif name == "workspace":
        _check_workspace(obj)
    elif name == "addon-manifest":
        _check_manifest(obj)
    elif name == "gate-input":
        _check_gate_input(obj)
    elif name == "checkpoint":
        _ts("checkpoint", ("o", "at"), obj["o"]["at"])
    elif name == "keys-line":
        _ts("keys-line", ("at",), obj["at"])
