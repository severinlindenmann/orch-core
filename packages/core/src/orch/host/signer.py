"""The signing client's side of the human contract (§0.8.1, D-P2-21, N4): check, review, sign.

A host bug must not be able to make the person sign content they did not see. So the signer trusts nothing the host
computed: before any prompt, :func:`check` verifies the request's **shape** (N4), then **recomputes** every bound hash
from ``material`` (the gate hash from ``gate_input``, the question hash, each section hash, each ``before`` value),
every ``Review`` (payload and derived), ``review_digest`` and the fingerprint, and refuses (:class:`SignerRefused`) on
any difference. :func:`render` builds the review the person reads from ``material`` and the events only, opening with
the header (the workspace from the signer's **own** registry, the op, the origin, the fingerprint). :func:`sign` draws a
grant's secret here, never in the preparer (F1), builds the signing bytes itself and signs each event through the
custody backend, whose prompt shows fields derived from those bytes (:func:`orch.custody.describe.describe_payload`).

:func:`run` is the CLI composite (``orch approve`` and every person op in host mode): ``human.prepare`` with the
signer, check, review on the person's terminal, Enter confirmation, sign, ``human.submit``, and only then the grant
secret on the person's own terminal.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon
from orch.addons.settings_value import KEY as ADDONS_KEY
from orch.ops.errors import OrchError

from . import review, signcontent

__all__ = [
    "Signed",
    "SignerRefused",
    "check",
    "check_reviews",
    "finish",
    "header",
    "render",
    "run",
    "secret_text",
    "sign",
]

_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_DEVICE = re.compile(r"d_[0-9a-f]{32}")
_ADDON = re.compile(r"[a-z][a-z0-9-]{0,39}")
_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_QUESTION = re.compile(r"Q[1-9][0-9]*")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_CORE_SECTIONS = "summary|context|requirements|out_of_scope|plan|decisions|verification|findings|current_state"
_SECTION = re.compile(rf"(?:{_CORE_SECTIONS}|[a-z][a-z0-9-]{{0,39}}\.[a-z][a-z0-9_]*)")
_EDIT_PATH = re.compile(
    r"ticket\.(?:title|type|priority|size|labels|parent|blocked_by|due|links|acceptance|tasks)"
    r"|ticket\.addons\.[a-z][a-z0-9-]{0,39}\.[a-z][a-z0-9_]*"
    rf"|body\.{_SECTION.pattern}"
)
_ENVELOPE_OK = frozenset({"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on"})
_FORBIDDEN = frozenset({"seq", "at", "prev", "ws_seq", "host_sig", "sig"})
_GATE_TYPES = {"gate.approved": None, "gate.changes_requested": None, "verdict.given": "verify"}
_MEMBER_OPS = frozenset({"member.add", "member.remove", "member.role"})
_ADDON_OPS = frozenset({"addon.grant", "addon.disable", "addon.purge"})
#: Workspace-scoped add-on operations.
_ADDON_WS_OPS = frozenset({"addon.grant", "addon.purge"})


class SignerRefused(Exception):
    """The prepared request is refused before any prompt; nothing is signed."""


class Cancelled(SignerRefused):
    """The person pressed Ctrl-C before the signature existed (at the review or the passphrase prompt): the request
    is cancelled and nothing is signed. A Ctrl-C after :func:`sign` returned is NOT this (it stays a
    KeyboardInterrupt)."""


def cancel_request(call: Callable[..., Any], request_id: str, exc: BaseException) -> None:
    """Before any signature exists: cancel the request (it expires in 10 minutes anyway); a Ctrl-C becomes
    :class:`Cancelled`, which is the only interrupt reported as "nothing was signed"."""
    try:
        call("human.cancel", {"request_id": request_id})
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, KeyboardInterrupt):
        raise Cancelled("cancelled") from None


def _refuse(why: str) -> None:
    raise SignerRefused(f"refusing to sign: {why}")


def _same(a: Any, b: Any) -> bool:
    try:
        return canon.dumps(a) == canon.dumps(b)
    except Exception:  # noqa: BLE001 - not canonical JSON: not the same
        return False


# --- check -------------------------------------------------------------------------------------------------------


def check(
    prepared: Mapping[str, Any],
    *,
    workspace_id: str,
    person: str,
    device: str | None = None,
    auth: str | None = None,
) -> None:
    """Refuse (:class:`SignerRefused`) anything the signer cannot vouch for, **before any prompt**. ``device`` and
    ``auth``, when given, must be what the events carry (a bound request)."""
    _shape(prepared, workspace_id=workspace_id, person=person, device=device, auth=auth)
    _formats(prepared)
    _hashes(prepared, workspace_id)
    _touches(prepared)
    material = prepared["material"]
    events = prepared["events"]
    check_reviews(material, events)
    _sequence(prepared, events, workspace_id, person)
    for block in material["review"]:  # derived values are rendered too: they must have their formats
        d = block["derived"]
        if ("cert_device" in d and not _DEVICE.fullmatch(str(d["cert_device"]))) or (
            "cert_person" in d and not _PERSON.fullmatch(str(d["cert_person"]))
        ):
            _refuse("a certificate names a malformed device or person")
    digest = review.review_digest(material)
    if prepared.get("review_digest") != digest:
        _refuse("review_digest does not match the material")
    if prepared.get("fingerprint") != review.fingerprint(digest, events, prepared["op"]):
        _refuse("the fingerprint does not match")


def _sequence(prepared: Mapping[str, Any], events: list[Any], workspace_id: str, person: str) -> None:
    """Batch 11 (finding 9): the first request of a signer's sequence. The intent in the material is the one thing the
    person reviews; its digest, its step count and the first event are recomputed from it here, so a host cannot show
    one intent and prepare another."""
    from . import sequence

    meta = prepared["material"].get("sequence")
    if meta is None:
        return
    try:
        if not isinstance(meta, dict) or set(meta) != {"digest", "of", "intent"}:
            _refuse("the sequence material has keys outside its schema")
        intent = sequence.validate_intent(meta["intent"])
        plan = sequence.steps(intent)
        if meta["digest"] != sequence.digest_of(workspace_id, person, intent) or meta["of"] != len(plan):
            _refuse("the sequence digest or step count is not the intent's")
        if prepared["op"] != plan[0]["op"] or len(events) != 1:
            _refuse("a sequence starts with its first step's op")
        sequence.check_step(intent, 0, prepared, person=person, first=None, prev_head=None)
    except sequence.SequenceInvalid as exc:
        _refuse(f"the sequence is not one this signer knows ({exc})")
    except sequence.Aborted as exc:
        _refuse(exc.reason)


def verify_material(prepared: Mapping[str, Any], *, workspace_id: str) -> None:
    """The part of :func:`check` that needs no person and no device (a reader that only shows a request, like the
    dashboard server): the shape, the hashes the material binds (gate, policy, question, sections), every ``before``
    against the event's ``base_rev``, every review recomputed from the events and the material. Raises
    :class:`SignerRefused`. It vouches for the material; it is not a substitute for :func:`check` before a signature."""
    try:
        events = prepared["events"]
        person = events[0]["actor"]["id"]  # whose request it is; the signer's own check compares it
    except (KeyError, IndexError, TypeError):
        _refuse("the request carries no events")
    _shape(prepared, workspace_id=workspace_id, person=person, device=None, auth=None)
    _formats(prepared)
    _hashes(prepared, workspace_id)
    _person_op(prepared)
    check_reviews(prepared["material"], events)
    _sequence(prepared, events, workspace_id, person)
    if prepared.get("review_digest") != review.review_digest(prepared["material"]):
        _refuse("review_digest does not match the material")


def _formats(prepared: Mapping[str, Any]) -> None:
    """Every identifier the review shows or the person types is checked against its F1 format before anything is
    rendered: a host string can never carry a terminal control sequence into the header or the Enter confirmation."""
    if not isinstance(prepared.get("workspace_id"), str) or not _HEX32.fullmatch(prepared["workspace_id"]):
        _refuse("workspace_id is malformed")
    log = prepared["log"]
    if log != "workspace" and not _ULID.fullmatch(log):
        _refuse("log is not a ticket uid")
    material = prepared["material"]
    ticket = material.get("ticket")
    if ticket is not None and (
        not isinstance(ticket.get("key"), str)
        or not _KEY.fullmatch(ticket["key"])
        or not isinstance(ticket.get("uid"), str)
        or not _ULID.fullmatch(ticket["uid"])
    ):
        _refuse("material.ticket has a malformed key or uid")
    op = prepared["op"]
    for e in prepared["events"]:
        if e["actor"]["device"] is not None and not _DEVICE.fullmatch(str(e["actor"]["device"])):
            _refuse("actor.device is malformed")
        if not _PERSON.fullmatch(str(e["actor"]["id"])):
            _refuse("actor.id is malformed")
        if op in _MEMBER_OPS and not _PERSON.fullmatch(str(e.get("person"))):
            _refuse("the member is not a person id")
        if op in _ADDON_OPS and not _ADDON.fullmatch(str(e.get("name"))):
            _refuse("the addon name is malformed")
        if e["type"] in _GATE_TYPES and not isinstance(e.get("gate_gen"), int):
            _refuse("gate_gen is not a number")
    for sid in material.get("texts") or {}:
        if not isinstance(sid, str) or not _SECTION.fullmatch(sid):
            _refuse("a section id is malformed")
    for path in material.get("before") or {}:
        if not isinstance(path, str) or not (_EDIT_PATH.fullmatch(path) or _TOKEN.fullmatch(path)):
            _refuse("a before key is malformed")
    q = material.get("question")
    if q is not None and (not isinstance(q, dict) or not _QUESTION.fullmatch(str(q.get("id")))):
        _refuse("the question id is malformed")
    o = prepared.get("origin")
    if (
        not isinstance(o, dict)
        or type(o.get("uid")) is not int
        or type(o.get("pid")) is not int
        or not isinstance(o.get("client"), str)
        or not _TIME.fullmatch(str(o.get("created_at")))
    ):
        _refuse("origin is malformed")


#: what a person version of an agent op may touch with its ticket.updated (§0.8): the paths of ``set`` and the
#: sections; ``None`` for no restriction beyond the schema
_SET_FIELDS = frozenset(
    f"ticket.{k}" for k in ("title", "priority", "size", "labels", "due", "links", "parent", "blocked_by")
)


#: what the host person ops (H6b, §0.8) may change
_MOVE_TO = frozenset({"backlog", "open"})
_GATES = frozenset({"requirements", "plan", "verify", "code"})
_PEOPLE_ROLES = ("owner", "assignees", "reviewers")  # watchers: not offered by the dashboard yet
_SETTINGS_KEYS = frozenset(
    {
        "grant_hours",
        "claim_ttl_min",
        "lease_ttl_min",
        "repos",
        "publish",
        ADDONS_KEY,
    }  # fmt: skip
)


def _person_op(prepared: Mapping[str, Any]) -> None:
    """``move``, ``policy.set``, ``settings.set``: each event may change only what its op names, on the log its scope
    names (a ticket for ``move`` and a ticket ``policy.set``, the workspace log otherwise)."""
    op, log, ticket = prepared["op"], prepared.get("log"), prepared["material"].get("ticket")
    if "statement" in prepared["material"] and op not in ("ac.accept", "ac.unaccept"):
        _refuse(f"{op} carries a manual acceptance statement")
    events = prepared["events"]
    for e in events:  # every event, before the op-specific dispatch below (which returns at its first unknown op)
        label = e.get("label")
        if (
            e["type"] in ARTIFACT_TYPES
            and isinstance(label, str)
            and label.startswith(("accepted ", "withdrawn "))
            and op not in ("ac.accept", "ac.unaccept")
        ):
            _refuse(f"{op} cannot sign a file labelled as a manual acceptance or its withdrawal")
    for e in events:
        if op in ("ac.accept", "ac.unaccept"):
            from . import acceptance

            statement = prepared["material"].get("statement")
            parsed = acceptance.parse(statement)
            ok = (
                len(events) == 1
                and log != "workspace"
                and ticket is not None
                and parsed is not None
                and parsed["kind"] == ("accept" if op == "ac.accept" else "withdraw")
                and acceptance.matches(e, statement)
            )
        elif op == "task.mark_done":
            ok = (
                len(events) == 1
                and log != "workspace"
                and ticket is not None
                and e["type"] == "task.done"
                and set(e) - review.ENVELOPE == {"task"}
                and isinstance(e.get("task"), str)
                and re.fullmatch(r"T[1-9][0-9]*", e["task"]) is not None
            )
        elif op == "move":
            ok = e["type"] == "ticket.moved" and e.get("to") in _MOVE_TO and log != "workspace" and ticket is not None
        elif op == "policy.set":
            gates = e.get("gates")
            scope_ok = (log == "workspace") == (ticket is None)
            ok = e["type"] == "policy.changed" and isinstance(gates, dict) and 1 <= len(gates) <= 4
            ok = ok and set(gates) <= _GATES and scope_ok
        elif op == "people.set":
            ok = (
                e["type"] == "people.changed"
                and log != "workspace"
                and ticket is not None
                and e.get("role") in _PEOPLE_ROLES
                and isinstance(e.get("add"), list)
                and isinstance(e.get("remove"), list)
                and (len(e["add"]) + len(e["remove"]) > 0)
                and (e.get("role") != "owner" or (len(e["add"]) == 1 and not e["remove"]))
            )
        elif op == "publish.share":  # P5: one note on a ticket, whose words are the host-built approval text
            from orch.publishclient.approval import MARK

            ok = (
                e["type"] == "log.added"
                and log != "workspace"
                and ticket is not None
                and isinstance(e.get("text"), str)
                and e["text"].startswith(MARK + "\n")
            )
        elif op in ("hub.join", "hub.mode"):
            # V3 C2a/C2b: one hub.joined on the workspace log, exactly the contract's members. hub.mode only ever
            # signs a switch to full without new aliases; hub.join signs either mode (the review says which).
            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "hub.joined"
                and e.get("mode") in (("full",) if op == "hub.mode" else ("selective", "full"))
                and (op == "hub.join" or e.get("aliases") == [])
                and set(e) - review.ENVELOPE
                == {"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases", "owner_proof"}
                and e["owner_proof"] is None  # #1198 C9: the signer fills it after the review
            )
        elif op == "hub.leave":  # V3 C2a: one hub.left on the workspace log, naming the hub and the binding it ends
            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "hub.left"
                and set(e) - review.ENVELOPE == {"hub_id", "binding_seq", "owner_proof"}
                and e["owner_proof"] is None  # #1198 C9: the signer fills it after the review
            )
        elif op == "remote.set":  # R2: one remote.changed on the workspace log, exactly the contract's members
            from orch.hubclient import wire

            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "remote.changed"
                and set(e) - review.ENVELOPE == {*wire.REMOTE_CHANGED_KEYS, "owner_proof"}
                and e["owner_proof"] is None  # the signer fills it after the review (the remote owner proof)
            )
        elif op == "remote.device_confirm":  # R2: one remote.device_added, signed by the confirming owner
            from orch.hubclient import wire

            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "remote.device_added"
                and set(e) - review.ENVELOPE == {*wire.REMOTE_ADDED_KEYS, "owner_proof"}
                and e["owner_proof"] is None
                and e.get("person") == e["actor"].get("id")
            )
        elif op == "device.mark_exposed":  # #1097: one device.exposed, the signer's own device, nothing else
            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "device.exposed"
                and set(e) - review.ENVELOPE == {"device", "reason"}
                and e.get("reason") == "convenience"
                and e.get("device") == e["actor"].get("device")
            )
        elif op == "device.rotate":  # #1097: one device.rotated, signed by the NEW device (the actor), replacing `old`
            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "device.rotated"
                and set(e) - review.ENVELOPE == {"old", "device", "cert", "rotation"}
                and e.get("device") == e["actor"].get("device")
                and e.get("old") != e.get("device")
            )
        elif op == "device.remove":  # #1198 C15: one device.removed, never the signer's own device (lockout guard)
            ok = (
                len(events) == 1
                and log == "workspace"
                and e["type"] == "device.removed"
                and set(e) - review.ENVELOPE == {"device"}
                and isinstance(e.get("device"), str)
                and _DEVICE.fullmatch(e["device"]) is not None
                and e.get("device") != e["actor"].get("device")
            )
        elif op == "settings.set":
            sets = e.get("set")
            ok = e["type"] == "settings.changed" and log == "workspace" and isinstance(sets, dict) and bool(sets)
            ok = ok and set(sets) <= _SETTINGS_KEYS
            if ok and ADDONS_KEY in sets:  # G6: one add-on, alone, within the general bounds (rendered from the event)
                from orch.addons import settings_value

                ok = len(sets) == 1 and settings_value.check_changes(sets[ADDONS_KEY]) is None
        else:
            return
        if not ok:
            _refuse(f"{op} cannot sign this {e.get('type')} on this log")


def _touches(prepared: Mapping[str, Any]) -> None:
    """The op is bound: what each op's ``ticket.updated`` may change, so ``ac.add`` cannot carry a title change."""
    op = prepared["op"]
    _person_op(prepared)
    for e in prepared["events"]:
        if e["type"] != "ticket.updated":
            continue
        sets = e.get("set") or {}
        sections = e.get("sections") or {}
        if not isinstance(sets, dict) or not isinstance(sections, dict):
            _refuse("ticket.updated is malformed")
        touched = set(sets) | {"body." + sid for sid in sections}
        if set(e.get("base_rev") or {}) != touched:
            _refuse("base_rev does not name exactly the paths the edit touches")
        if op in ("ac.add", "ac.edit"):
            ok = set(sets) == {"ticket.acceptance"} and not sections
        elif op in ("task.add", "task.edit"):
            ok = set(sets) == {"ticket.tasks"} and not sections
        elif op == "section.set":
            ok = not sets and len(sections) == 1
        elif op == "set":
            ok = bool(sets) and set(sets) <= _SET_FIELDS and not sections
        else:
            ok = False
        if not ok:
            _refuse(f"{op} does not change {sorted(touched)}")


def check_reviews(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """Every ``Review`` (payload and derived) recomputed from the events and the material, compared with what the
    host sent; a section text whose ``section_hash`` differs from the event is refused here (F1 §5.6)."""
    try:
        reviews = [review.of(e, material) for e in events]
    except review.ReviewError as exc:
        _refuse(str(exc))
    sent = material.get("review")
    if isinstance(sent, list) and len(sent) == len(reviews):
        for got, want in zip(sent, reviews, strict=True):
            if want["type"] in review.READER_TYPES:  # H12d-2: the reader level, a host attestation (shown as such)
                level = got.get("derived", {}).get("reader") if isinstance(got, dict) else None
                if type(level) is not int or not 0 <= level <= review.LEVEL_MAX:
                    _refuse("a reader-adding event carries no reader level (update orch)")
                want["derived"]["reader"] = level
    if not _same(sent, reviews):
        _refuse("the review the host sent differs from the one recomputed from the events and the material")


def _shape(
    prepared: Mapping[str, Any], *, workspace_id: str, person: str, device: str | None, auth: str | None
) -> None:
    """N4: the op, the event types, their number and order, one log, one ``based_on``, the signer's person."""
    op = prepared.get("op")
    if not isinstance(op, str) or review.person_op_types(op) is None:
        _refuse(f"{op!r} is not a person op this signer knows")
    if prepared.get("workspace_id") != workspace_id:
        _refuse("the request is for another workspace")
    events = prepared.get("events")
    if not isinstance(events, list) or not events or not all(isinstance(e, dict) for e in events):
        _refuse("the request carries no events")
    types = [e.get("type") for e in events]
    from orch.ops.actors import EVENT_ACTORS, allows

    for t in types:
        if not isinstance(t, str) or t not in EVENT_ACTORS or not allows("human", t):
            _refuse(f"{t!r} is not an event type a person signs")
    if not review.events_fit(op, types):  # type: ignore[arg-type]
        _refuse(f"the events {types} do not fit {op} (EMITS_V1)")
    if "grant.issued" in types and op != "grant":
        _refuse("a grant.issued outside a grant")
    if len({canon.dumps(e.get("based_on")) for e in events}) != 1:
        _refuse("the events do not share one based_on")
    log = prepared.get("log")
    if not isinstance(log, str):
        _refuse("the request names no log")
    seen: set[str] = set()
    for e in events:
        if set(e) & _FORBIDDEN:
            _refuse(f"{e.get('type')} carries {sorted(set(e) & _FORBIDDEN)[0]}: the store stamps it, the signer signs")
        if e.get("v") != 2 or e.get("hash_v") != 1 or not isinstance(e.get("roster_v"), int):
            _refuse(f"{e.get('type')}: not a version-2 event")
        eid = e.get("id")
        if not isinstance(eid, str) or not _ULID.fullmatch(eid) or eid in seen:
            _refuse("event ids must be distinct ULIDs")
        seen.add(eid)
        actor = e.get("actor")
        if not isinstance(actor, dict) or set(actor) != {"kind", "id", "device"} or actor.get("kind") != "person":
            _refuse("the actor must be exactly {kind: person, id, device}")
        if actor["id"] != person:
            _refuse(f"the events are for {actor['id']}, not for this signer's person")
        if device is not None and actor["device"] != device:
            _refuse("the events are bound to another device")
        if auth is not None and e.get("auth") != auth:
            _refuse("the events carry another auth")
        if e["type"] == "grant.issued" and e.get("secret_hash") is not None:
            _refuse("a prepared grant.issued must carry secret_hash null: the signer draws the secret (F1)")
    if len({canon.dumps(e.get("roster_v")) for e in events}) != 1:
        _refuse("the events do not share one roster_v")


#: kind -> (required keys, allowed keys) of ``Material`` (§0.8.1)
_MATERIAL_KEYS = {
    "gate": (
        {"kind", "gate", "input", "policy", "texts", "ticket", "review"},
        {"kind", "gate", "input", "policy", "texts", "ticket", "review", "manual_acceptances", "sequence"},
    ),
    "question": ({"kind", "ticket", "question", "review"}, {"kind", "ticket", "question", "review"}),
    "payload": (
        {"kind", "ticket", "review"},
        {
            "kind",
            "ticket",
            "review",
            "texts",
            "before",
            "sequence",
            "statement",
            "grants",
            "market",
            "deps",
            "removed_device",
            "agent_access_until",
        },
    ),
}


def _hashes(prepared: Mapping[str, Any], workspace_id: str) -> None:
    """Recompute every hash an event binds from what the material shows."""
    material = prepared.get("material")
    if not isinstance(material, dict):
        _refuse("no material")
    events, log, op = prepared["events"], prepared["log"], prepared["op"]
    kind = material.get("kind")
    keys = _MATERIAL_KEYS.get(kind)
    if keys is None or not keys[0] <= set(material) <= keys[1]:  # fail closed: a key outside the Material schema
        _refuse(f"material has keys outside the {kind!r} schema")
    if "manual_acceptances" in material:
        manual = material["manual_acceptances"]
        if material.get("gate") != "verify" or not isinstance(manual, list):
            _refuse("manual acceptances require a verify gate list")
        for row in manual:
            if (
                not isinstance(row, dict)
                or set(row) != {"ac", "name", "by"}
                or not all(isinstance(value, str) for value in row.values())
            ):
                _refuse("malformed manual acceptance")
    ticket = material.get("ticket")
    if ticket is not None and (not isinstance(ticket, dict) or set(ticket) != {"key", "uid"}):
        _refuse("material.ticket is malformed")
    gate_event = len(events) == 1 and events[0]["type"] in _GATE_TYPES
    question_event = len(events) == 1 and events[0]["type"] == "question.answered"
    want = "gate" if gate_event else ("question" if question_event else "payload")
    if kind != want:
        _refuse(f"material of kind {kind!r} for {op}")
    if log == "workspace":
        if ticket is not None:
            _refuse("a workspace event with a ticket material")
    elif ticket is None or ticket["uid"] != log:
        _refuse("material.ticket is not the ticket of the log")
    if kind == "gate":
        _gate(material, events[0], workspace_id, log)
    elif kind == "question":
        _question(material, events[0], workspace_id, log)
    _before(material, events)
    _grants(material, events)
    market_events = events
    seq = material.get("sequence")
    if isinstance(seq, Mapping) and (seq.get("intent") or {}).get("kind") == "addon_enable":
        from . import sequence

        try:
            intent = sequence.validate_intent(seq["intent"])
        except sequence.SequenceInvalid:
            _refuse("invalid add-on sequence")
        if intent.get("staged"):
            market_events = [{"type": "addon.granted", **intent["grant"]}]
    _market(material, market_events)
    _removed_device(material, events)
    _deps(material, market_events)


def _gate(material: Mapping[str, Any], ev: Mapping[str, Any], workspace_id: str, log: str) -> None:
    gate = _GATE_TYPES[ev["type"]] or ev.get("gate")
    inp = material.get("input")
    if material.get("gate") != gate or not isinstance(inp, dict):
        _refuse("the material is for another gate")
    try:
        bound = canon.gate_hash(inp)
    except Exception as exc:  # noqa: BLE001
        _refuse(f"gate_input cannot be hashed: {exc}")
    if inp.get("workspace_id") != workspace_id or inp.get("uid") != log or inp.get("gate") != gate:
        _refuse("gate_input is for another workspace, ticket or gate")
    if ev.get("hash") != bound:
        _refuse("the gate hash of the event is not the hash of the gate input shown")
    policy = material.get("policy")
    try:
        bound_policy = canon.policy_hash(gate, policy) if isinstance(policy, dict) else None
    except Exception:  # noqa: BLE001
        bound_policy = None
    if bound_policy is None:
        _refuse("the material carries no policy the signer can hash")
    if bound_policy != inp.get("policy_hash") or bound_policy != ev.get("policy_hash"):
        _refuse("the policy hash differs from the policy shown (in the gate input or the event)")
    if "source_sha" in ev and not _same(ev["source_sha"], inp.get("source_sha")):
        _refuse("the source list of the event is not the one in the gate input")
    texts = material.get("texts")
    sections = inp.get("sections") or {}
    if not isinstance(texts, dict) or set(texts) != set(sections):
        _refuse("the material does not carry the text of every section the gate binds")
    for sid, want in sections.items():
        if not isinstance(texts[sid], str) or canon.section_hash(texts[sid]) != want:
            _refuse(f"section {sid}: the text does not hash to the gate input's section hash")


def _question(material: Mapping[str, Any], ev: Mapping[str, Any], workspace_id: str, log: str) -> None:
    q = material.get("question")
    if not isinstance(q, dict) or q.get("id") != ev.get("question"):
        _refuse("the material shows another question")
    try:
        qid = canon.question_id(workspace_id, log, q["id"])
        bound = canon.question_hash(qid, log, q)  # the whole raw question: kind, details, why, facts are bound
    except Exception as exc:  # noqa: BLE001
        _refuse(f"the question cannot be hashed: {exc}")
    if ev.get("hash") != bound:
        _refuse("the question hash of the event is not the hash of the question shown")


def _grants(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """``material["grants"]`` is the host's word (unsigned, labelled in the review) about each access a
    ``grant.revoked`` of the request takes back: exactly those ids, each with four short strings. A key without such an
    event, or any other shape, is refused."""
    grants = material.get("grants")
    if grants is None:
        return
    revoked = {e.get("grant") for e in events if e.get("type") == "grant.revoked"}
    if not isinstance(grants, dict) or not set(grants) <= revoked:
        _refuse("material.grants names an access the request does not take back")
    for info in grants.values():
        if (
            not isinstance(info, dict)
            or set(info) != {"label", "scope", "expires_at", "person"}
            or not all(isinstance(v, str) and len(v) <= 200 for v in info.values())
        ):
            _refuse("material.grants is malformed")


_MARKET_KEYS = {"url", "class", "commit", "source_note", "publisher", "index_version", "stale", "summary", "replaces"}
_TOFU = ("unsigned", "new", "known", "pinned")


def _market(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """``material["market"]`` (V3 G3, C4) is the host's word about where an add-on came from: the marketplace, its
    class, the commit, the publisher key's fingerprint and first-use status, the index version and a display summary.
    It is NOT signed and the review says so. Exactly these keys and these shapes, only on a request that signs one
    ``addon.granted``; anything else (an unknown key, a number where a string belongs, a long text) is refused. No
    capability, field or section travels here: those come from the signed event."""
    block = material.get("market")
    if block is None:
        return
    pub = block.get("publisher") if isinstance(block, dict) else None
    if (
        not isinstance(block, dict)
        or set(block) != _MARKET_KEYS
        or [e.get("type") for e in events] != ["addon.granted"]
        or not isinstance(pub, dict)
        or set(pub) != {"fingerprint", "tofu"}
    ):
        _refuse("material.market is malformed")
    short = lambda v, n: isinstance(v, str) and len(v) <= n  # noqa: E731
    ok = (
        short(block["url"], 200)
        and block["class"] in ("official", "dev", "company", "dev-folder")
        and isinstance(block["commit"], str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", block["commit"]) is not None
        and block["source_note"]
        == (
            "a local folder at this commit (orch addon dev), not a marketplace"
            if block["class"] == "dev-folder"
            else "source as claimed by the marketplace"
        )
        and (pub["fingerprint"] is None or short(pub["fingerprint"], 60))
        and pub["tofu"] in _TOFU
        and type(block["index_version"]) is int
        and type(block["stale"]) is bool
        and (block["summary"] is None or short(block["summary"], 300))
        and (block["replaces"] is None or re.fullmatch(r"sha256:[0-9a-f]{64}", str(block["replaces"])) is not None)
    )
    if not ok:
        _refuse("material.market is malformed")


def _removed_device(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """``material["removed_device"]`` (#1198 C15) names the holder of the device a ``device.removed`` removes, from the
    host's roster: exactly ``{device, holder}``, the device being the event's own, only on that one event."""
    block = material.get("removed_device")
    if block is None:
        return
    if (
        not isinstance(block, dict)
        or set(block) != {"device", "holder"}
        or [e.get("type") for e in events] != ["device.removed"]
        or block["device"] != events[0].get("device")
        or not isinstance(block["holder"], str)
        or not 1 <= len(block["holder"]) <= 100
    ):
        _refuse("material.removed_device is malformed")


_DEP_KINDS = ("python-requirements", "node-package-lock")
_DEP_NOTES = (None, "compiled", "other_platform", "dev")
_DEP_NAME = re.compile(r"[A-Za-z0-9@._~/ >+-]{1,200}")
_DEP_VERSION = re.compile(r"[0-9A-Za-z.+!_-]{1,60}")


from orch.market.deps import DIFF_LINE as deps_diff_line  # noqa: E402


def _deps(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """``material["deps"]`` (V3 G4, C5): the packages an ``addon.granted`` with ``runtime`` installs, as this computer
    listed them from the lockfile (covered by the package digest, but not itself signed: the review labels it so).
    Required on a request that signs an ``addon.granted`` with ``runtime``, forbidden anywhere else, and exactly these
    keys and shapes; ``kind``, ``lock`` and the package count must equal the SIGNED ``runtime``. Every name and version
    is a short plain token: nothing here can carry a line break or a command."""
    block = material.get("deps")
    with_runtime = [e for e in events if e.get("type") == "addon.granted" and e.get("runtime") is not None]
    if with_runtime and len(events) != 1:
        _refuse("an add-on that installs dependencies is signed alone, not in a batch")
    signed = with_runtime[0]["runtime"] if with_runtime else None
    if block is None:
        if signed is not None:
            _refuse("material.deps is missing for an add-on that installs dependencies")
        return
    rows = block.get("rows") if isinstance(block, dict) else None
    if (
        signed is None
        or not isinstance(block, dict)
        or set(block) - {"diff"} != {"kind", "lock", "count", "bytes", "rows"}
        or not isinstance(rows, list)
        or (block["kind"], block["lock"], block["count"]) != (signed["kind"], signed["lock"], signed["packages"])
        or block["kind"] not in _DEP_KINDS
        or type(block["count"]) is not int
        or len(rows) != block["count"]
        or type(block["bytes"]) is not int
        or not 0 <= block["bytes"] <= 1 << 30
    ):
        _refuse("material.deps is malformed")
    d = block.get("diff")
    if d is not None and not (
        isinstance(d, dict)
        and set(d) == {"lines", "widened"}
        and type(d["widened"]) is bool
        and isinstance(d["lines"], list)
        and len(d["lines"]) <= 2 * 1000 + 1
        and all(isinstance(x, str) and 0 < len(x) <= 300 and deps_diff_line.fullmatch(x) for x in d["lines"])
    ):
        _refuse("material.deps has a malformed diff")
    for r in rows:
        if (
            not isinstance(r, dict)
            or set(r) != {"name", "version", "installs", "note"}
            or not (isinstance(r["name"], str) and _DEP_NAME.fullmatch(r["name"]))
            or not (isinstance(r["version"], str) and _DEP_VERSION.fullmatch(r["version"]))
            or type(r["installs"]) is not bool
            or r["note"] not in _DEP_NOTES
            or (r["note"] in ("other_platform", "dev")) == r["installs"]
        ):
            _refuse("material.deps has a malformed row")


def _before(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """Each ``before`` value is checked against the event's ``base_rev`` (set, section.set) or is a key of the
    ``set`` of a ``settings.changed`` (labelled unsigned); any other key is refused."""
    before = material.get("before")
    if before is None:
        return
    if not isinstance(before, dict):
        _refuse("material.before is not a map")
    for path, value in before.items():
        ok = False
        for ev in events:
            base = ev.get("base_rev") or {}
            if path in base:
                if path.startswith("body."):
                    got = canon.section_hash(value) if isinstance(value, str) else None
                else:
                    try:
                        got = canon.value_hash(value)
                    except Exception:  # noqa: BLE001
                        got = None
                if got != base[path]:
                    _refuse(f"before.{path} does not match the event's base_rev")
                ok = True
            elif ev.get("type") == "settings.changed" and path in (ev.get("set") or {}):
                ok = True
        if not ok:
            _refuse(f"before.{path} cannot be checked by any event of the request")


# --- review ------------------------------------------------------------------------------------------------------

_PLAIN = {
    "approve": "approve the {gate} gate of {ticket}",
    "request_changes": "request changes on the {gate} gate of {ticket}",
    "verdict": "give the verdict {outcome} on {ticket}",
    "answer": "answer {question} on {ticket} with option {option}",
    "follow_up.reschedule": "move the follow-up {field} of {ticket} to {when}",
    "follow_up.undo": "restore the cancelled follow-up of {ticket}",
    "close": "close {ticket} as {resolution}",
    "reopen": "reopen {ticket}",
    "grant": "issue a grant {grant}: scope {scope}, verbs {verbs}, {hours} h",
    "grant.revoke": "revoke grant {grant}",
    "member.add": "add {person} as {role}",
    "member.remove": "remove member {person}",
    "member.role": "make {person} {role}",
    "addon.grant": "grant addon {name}",
    "addon.disable": "disable addon {name}",
    "addon.purge": "purge addon {name}",
    "move": "move {ticket} to {to}",
    "policy.set": "set the gate policies {gates} of {ticket}",
    "settings.set": "change the workspace settings {set}",
    "people.set": "change the {role} of {ticket}",
    "publish.share": "let the staged copy of files of {ticket} be shared outside this computer",
    "hub.join": (
        "join hub {hub_id} in {mode} mode: the hub sees only what you share, plus this workspace's name, "
        "and never gets the workspace key"
    ),
    "hub.join.full": "join hub {hub_id} in full mode: the hub will be able to read everything in this workspace",
    "hub.mode": "switch hub {hub_id} to {mode} mode: the hub will be able to read everything in this workspace",
    "hub.leave": "leave hub {hub_id} (binding {binding_seq}) and tell it that this workspace is no longer bound",
    "remote.set": "turn on remote access ({level}, until {until_ms}): devices you add can use this workspace from afar",
    "remote.device_confirm": (
        "add the remote device {name} (fingerprint {fingerprint}) up to {level_cap}, until {expires_ms}"
    ),
    "device.mark_exposed": "record that your device key {device} was in one-click custody (it only loses authority)",
    "device.remove": "remove the device {device}; it can no longer sign for this workspace",
    "device.rotate": "replace your device key {old} with the new key {device}; the old key stops signing everywhere",
}


def _clean(value: Any) -> str:
    text = value if isinstance(value, str) else canon.dumps(value).decode("utf-8")
    return canon.clean(text).replace("\n", "⟨U+000A⟩")


def header(prepared: Mapping[str, Any], workspace: Callable[[str], tuple[str, str] | None]) -> list[str]:
    """Built before any ticket content: the workspace from the signer's own registry, the op in plain words, the
    origin (kernel UID and PID recorded by the host; the client name is unverified), the fingerprint."""
    known = workspace(prepared["workspace_id"])
    if known is None:
        _refuse("this workspace is not in your own registry")
    name, prefix = known  # type: ignore[misc]
    ev, ticket = prepared["events"][0], prepared["material"].get("ticket")
    fields = {k: _clean(v) for k, v in review.payload(ev).items()}
    if prepared["op"] == "follow_up.reschedule":
        fields["field"] = "due" if "due" in ev else "deadline"
        fields["when"] = fields.get(fields["field"], "")
    fields.setdefault("gate", "verify")
    fields.setdefault("option", "none (text only)")
    fields["ticket"] = f"{_clean(ticket['key'])}/{prepared['log'][-8:]}" if ticket else "the workspace"
    try:
        key = "hub.join.full" if prepared["op"] == "hub.join" and fields.get("mode") == "full" else prepared["op"]
        plain = _PLAIN.get(key, "{op} on {ticket}").format_map(_Default({**fields, "op": prepared["op"]}))
    except (KeyError, ValueError):
        plain = prepared["op"]
    o = prepared["origin"]
    lines = [
        f"workspace: {_clean(name)} ({_clean(prefix)}) {prepared['workspace_id']}",
        f"op: {_clean(prepared['op'])}: {_clean(plain)}",
    ]
    if ticket is not None:
        lines.append(
            f"ticket: {_clean(ticket['key'])} (ticket key per host, not proven) uid {prepared['log']} "
            "(signed ticket id)"
        )
    return [
        *lines,
        f"requested by uid {int(o['uid'])} pid {int(o['pid'])} at {_clean(o['created_at'])}; "
        f"client {_clean(o['client'])} (self-declared, unverified)",
        f"fingerprint: {prepared['fingerprint']}",
    ]


class _Default(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def _summary(block: Mapping[str, Any]) -> str:
    derived, typ = block["derived"], _clean(block["type"])
    if "ticket_key" in derived:
        return f"{typ} on {_clean(derived['ticket_key'])} (ticket key per host)"
    if "cert_device" in derived:
        return f"{typ}: device {_clean(derived['cert_device'])} of {_clean(derived['cert_person'])}"
    if derived.get("sections"):
        return f"{typ}: sections {', '.join(_clean(k) for k in sorted(derived['sections']))}"
    return typ


#: event types whose digest (``sha256``, ``bytes``) has no preimage in the material: the signer can compare it only with
#: bytes it holds itself
ARTIFACT_TYPES = frozenset({"artifact.added", "artifact.replaced"})
UNSEEN = "signed commitment: the signer has not seen these bytes; compare with the file you meant"
SEEN = "checked against your local file"


_QUESTION_KEYS = frozenset({"id", "to", "text", "kind", "why", "details", "options", "recommended", "allow_other",
                            "blocking"})  # fmt: skip
_OPTION_KEYS = frozenset({"key", "label", "text", "facts", "cost"})


def _lines(label: str, text: Any) -> list[str]:
    return [f"{label}:", *("| " + _clean(line) for line in str(text).split("\n"))]


def _question_content(q: Any) -> list[str]:
    """The full review of a question (#1123): EVERY key of the shown question, so the fallback view (when the plain
    view is unavailable) still shows each hash-bound byte. A key outside the one question shape is refused, never
    skipped: an unknown key could be bound and unseen."""
    if not isinstance(q, dict) or set(q) - _QUESTION_KEYS:
        _refuse("the question carries a key the review does not show")
    opts = q.get("options", [])
    if not isinstance(opts, list) or not all(isinstance(o, dict) and not set(o) - _OPTION_KEYS for o in opts):
        _refuse("an option carries a key the review does not show")
    out = [f"question {_clean(q.get('id'))} (kind {_clean(q.get('kind'))}):"]
    out += ["| " + _clean(line) for line in str(q.get("text", "")).split("\n")]
    if "why" in q:
        out += _lines("why it was asked (signed)", q["why"])
    if "details" in q:
        out += _lines("details (signed)", q["details"])
    for o in opts:
        out.append(f"option {_clean(o.get('key'))}: {_clean(o.get('label'))}")
        if "text" in o:
            out.append(f"  text: {_clean(o['text'])}")
        if "cost" in o:
            out.append(f"  cost: {_clean(o['cost'])}")
        facts = o.get("facts", [])
        if not isinstance(facts, list) or not all(isinstance(f, list) and len(f) == 2 for f in facts):
            _refuse("an option's facts are not [label, value] pairs")
        out += [f"  {_clean(f[0])}: {_clean(f[1])}" for f in facts]
    if "recommended" in q:
        out.append(f"recommended option (signed): {_clean(q['recommended'])}")
    if "allow_other" in q:
        out.append(f"an answer in your own words is allowed (signed): {_clean(q['allow_other'])}")
    for k, label in (("to", "addressed to"), ("blocking", "blocks the ticket")):
        if k in q:
            out.append(f"{label} (routing, not signed): {_clean(q[k])}")
    return out


def render(
    prepared: Mapping[str, Any],
    workspace: Callable[[str], tuple[str, str] | None],
    *,
    artifacts: str | None = None,
) -> str:
    """The review the person reads, from the material and the events only (never host text): the header, then one
    block per event in event order, every payload key in canonical order, then derived values; section texts and
    ``before`` values are data, one ``| `` mark per line.

    ``artifacts`` labels the ``sha256`` and ``bytes`` of artifact events: ``"unseen"`` (a hand-off: this signer never
    held the bytes, :data:`UNSEEN`), ``"checked"`` (compared with local files, :data:`SEEN`), ``None`` (no label: the
    composite, which uploaded the bytes itself and checks them with :func:`check_uploads`)."""
    labels = {"unseen": UNSEEN, "checked": SEEN}
    if artifacts is not None and artifacts not in labels:
        raise ValueError(f"artifacts: {artifacts!r}")
    out = [*header(prepared, workspace), ""]
    extra: list[str] = []
    if prepared["material"].get("sequence") is not None:  # one review of every step (batch 11, finding 9)
        from . import sequence

        out.append(
            f"=== {int(prepared['material']['sequence']['of'])} signatures, ONE confirmation; all steps below ==="
        )
        intent = sequence.validate_intent(prepared["material"]["sequence"]["intent"])
        extra = [
            *sequence.extra_lines(intent, full=True),
            sequence.steps_line(intent, int(prepared["material"]["sequence"]["of"])),
            *sequence.technical_lines(intent),
        ]
        prepared = sequence.expand(prepared)
    material = prepared["material"]
    events = prepared["events"]
    for i, block in enumerate(material["review"], 1):
        out.append(f"--- event {i} of {len(material['review'])}: {_summary(block)} ---")
        payload = block["payload"]
        for key in sorted(payload):
            if block["type"] == "grant.issued" and key == "secret_hash":
                out.append("secret_hash: drawn by this signer after you confirm")
                continue
            label = " (consistency check against the gate input; host attestation)" if key == "source_sha" else ""
            if artifacts is not None and block["type"] in ARTIFACT_TYPES and key in ("sha256", "bytes"):
                label = f" ({labels[artifacts]})"
            out.append(f"{_clean(key)}: {_clean(payload[key])}{label}")
        if block["type"] in review.GATE_TYPES:
            out.append(f"gate_gen: {int(events[i - 1]['gate_gen'])} (host attestation, checked by the host at submit)")
        for sid, sec in sorted((block["derived"].get("sections") or {}).items()):
            out.append(f"section {_clean(sid)} ({_clean(sec['hash'])}):")
            out += ["| " + _clean(line) for line in sec["text"].split("\n")]
        for key, value in sorted(block["derived"].items()):
            if key == "ticket_key":
                out.append(f"ticket_key: {_clean(value)} (ticket key per host)")
            elif key == "reader":
                out.append(review.reader_line(int(value)))
            elif key == "owner_proof":
                out.append(f"owner proof: {_clean(value)}")
            elif key == "statement":
                out.append("statement:")
                out += ["| " + _clean(line) for line in value.split("\n")]
                if prepared["op"] == "ac.accept":
                    out.append("No automated check backs this: you vouch for it yourself.")
            elif key != "sections":
                out.append(f"{_clean(key)}: {_clean(value)}")
    if material.get("kind") == "gate":
        out += _gate_content(material)
    if material.get("kind") == "question":
        out += _question_content(material["question"])
    for gid, info in sorted((material.get("grants") or {}).items()):  # the host's word on each access taken back
        out.append(
            f"access {_clean(gid)} taken back (per host, not signed): label {_clean(info['label']) or '(none)'}, "
            f"tickets {_clean(info['scope'])}, ends {_clean(info['expires_at'])}, person {_clean(info['person'])}"
        )
    market = material.get("market")
    if market is not None:  # the host's word, labelled; nothing here is signed (C4)
        pub = market["publisher"]
        who = {"unsigned": "unsigned (no publisher key)", "new": "first use: remembered when you sign",
               "known": "same publisher as before", "pinned": "key pinned by your hub"}[pub["tofu"]]  # fmt: skip
        if market["class"] == "dev-folder":
            commit = _clean(market["commit"])
            out.append(
                f"DEV add-on from a local folder, not from a marketplace (per host, not signed): commit {commit}"
            )
            out.append(f"source (per host, not signed): {_clean(market['source_note'])}")
            market = None
    if market is not None:
        out.append(f"marketplace (per host, not signed): {_clean(market['url'])} ({_clean(market['class'])})")
        out.append(f"marketplace commit (per host, not signed): {_clean(market['commit'])}")
        out.append(f"source (per host, not signed): {_clean(market['source_note'])}")
        out.append(
            f"publisher (per host, not signed): {_clean(pub['fingerprint']) if pub['fingerprint'] else 'none'}, {who}; "
            "a key proves it is the same publisher as last time, not who they are"
        )
        out.append(
            f"index version (per host, not signed): {int(market['index_version'])}"
            + (" (STALE: past its expiry)" if market["stale"] else "")
        )
        if market["replaces"]:
            out.append(f"replaces package (per host, not signed): {_clean(market['replaces'])}")
        if market["summary"]:
            out.append(f"summary from the marketplace (per host, not signed): {_clean(market['summary'])}")
    deps = material.get("deps")
    if deps is not None:  # the host's list of what gets installed, from the lockfile (covered by the digest)
        out.append(
            f"dependencies (listed by this computer from the lockfile, covered by the digest, not signed itself): "
            f"{int(deps['count'])} packages, {_clean(deps['kind'])}, {_clean(deps['lock'])}"
        )
        for r in deps["rows"]:
            note = {None: "", "compiled": " (compiled code)", "other_platform": " (not on this computer)",
                    "dev": " (development, not installed)"}[r["note"]]  # fmt: skip
            out.append(f"dep {_clean(r['name'])} {_clean(r['version'])}{note}")
    if deps is not None and deps.get("diff") is not None:
        d = deps["diff"]
        out.append(
            "update of the dependencies (host-derived, not signed): "
            + ("WIDENED, read all of it" if d["widened"] else "not widened")
        )
        out += [f"dep change {_clean(x)}" for x in d["lines"]]
    for path, value in sorted((material.get("before") or {}).items()):
        label = "current value according to the host, not signed" if not path.startswith(("ticket.", "body.")) else ""
        out.append(f"before {_clean(path)}{' (' + label + ')' if label else ''}:")
        text = value if isinstance(value, str) else canon.dumps(value).decode("utf-8")
        out += ["| " + _clean(line) for line in text.split("\n")]
    out += [_clean(line) for line in extra]
    for c in prepared.get("checks") or []:
        out.append(
            f"check: {_clean(c.get('repo'))} clean at {_clean(c.get('at'))} (attested by the worker, best effort)"
        )
    text = "\n".join(out) + "\n"
    if any(unicodedata.category(c) in ("Cc", "Cf") and c != "\n" for c in text):  # every value went through _clean
        _refuse("the review would carry a control character")
    return text


class Review(str):
    """The text the person reads first (the plain view: headline, content, no hashes) with the full review of
    :func:`render` as :attr:`details` (``d`` at the prompt, ``--details`` or ``ORCH_SIGN_DETAILS=1`` shows it first)."""

    details: str = ""
    details_first: bool = False


def members_of(call: Callable[..., Any]) -> list[dict[str, Any]]:
    """The members of the workspace as the host lists them (``member.list``), for the plain view's names. The host's
    word, never signed: a person id is shown with a name only by :mod:`orch.host.personlabel`'s rules, and the full
    id stays in the details. Any failure is an empty list (every person is then an "unknown person": short id only)."""
    try:
        data = call("member.list", {})["data"]
        rows = data.get("members") if isinstance(data, Mapping) else None
        return [
            {"person": m["person"], "name": m.get("name")}
            for m in rows or []
            if isinstance(m, Mapping) and isinstance(m.get("person"), str)
        ]
    except Exception:  # noqa: BLE001 - names are a convenience; the review is complete without them
        return []


def plain(
    prepared: Mapping[str, Any],
    workspace: Callable[[str], tuple[str, str] | None],
    *,
    artifacts: str | None = None,
    prefix: str = "",
    members: list[Mapping[str, Any]] | None = None,
) -> Review:
    """B6 (owner decision E): the review in three layers. The plain view is :func:`signcontent.text` of the request's
    own events and material (so it is exactly what is signed); the details are today's full :func:`render`, which
    also runs here, so every refusal of the full review still comes before any prompt."""
    full = render(prepared, workspace, artifacts=artifacts)
    known = workspace(prepared["workspace_id"])
    try:
        lines = signcontent.text(
            signcontent.build(prepared, verified=True),
            _clean,
            workspace=f"{known[0]} ({known[1]})" if known is not None else None,
            members=members,
        )
    except (KeyError, TypeError, ValueError, AttributeError):  # a shape the plain view does not know: the full review
        return Review(prefix + full)
    # The dashboard says "check that this matches the fingerprint in your terminal": the same 12 hex it shows, on a
    # labelled line under the headline (before the first blank line). The full review's `fingerprint:` line stays too.
    fp = f"Verification code: {_clean(prepared['fingerprint'])}"
    lines.insert(lines.index("") if "" in lines else len(lines), fp)
    if artifacts in ("unseen", "checked") and any(e["type"] in ARTIFACT_TYPES for e in prepared["events"]):
        lines += [_clean(UNSEEN if artifacts == "unseen" else SEEN), ""]
    for c in prepared.get("checks") or []:
        lines.append(f"check: {_clean(c.get('repo'))} clean (attested by the worker, best effort)")
    body = "\n".join(lines).rstrip("\n") + "\n"
    if any(unicodedata.category(c) in ("Cc", "Cf") and c != "\n" for c in body):
        _refuse("the review would carry a control character")
    out = Review(prefix + body)
    out.details = prefix + full
    out.details_first = os.environ.get("ORCH_SIGN_DETAILS") == "1"
    return out


def _gate_content(material: Mapping[str, Any]) -> list[str]:
    """What the gate hash binds, as P1's review shows it: every section text (checked against the gate input), the
    acceptance criteria, the tasks with their verify commands, the links, the policy (its hash recomputed)."""
    inp = material["input"]
    out = [f"=== the {_clean(material['gate'])} gate binds ==="]
    for sid in sorted(material["texts"]):
        text = material["texts"][sid]
        lines = text.split("\n") if text else []
        out.append(f"--- section {_clean(sid)} ({len(lines)} lines) ---")
        out += ["| " + _clean(line) for line in lines or ["(empty)"]]
    fields = inp.get("fields") or {}
    out.append(f"type {_clean(fields.get('ticket_type'))} size {_clean(fields.get('size'))}")
    for a in fields.get("acceptance") or []:
        out.append(f"{_clean(a.get('id'))}:")
        out += ["| " + _clean(line) for line in str(a.get("text", "")).split("\n")]
    for t in inp.get("tasks") or []:
        out.append(f"{_clean(t.get('id'))}:")
        out += ["| " + _clean(line) for line in str(t.get("text", "")).split("\n")]
        out.append(f"   verify: {_clean((t.get('verify') or {}).get('cmd') or '(none)')}")
    if fields.get("links") is not None:
        out.append(f"links: {_clean(fields['links'])}")
    for name, a in sorted((inp.get("artifacts") or {}).items()):
        out.append(f"artifact {_clean(name)}: {_clean(a)}")
    out.append(f"policy: {_clean(material['policy'])} (hash recomputed)")
    out.append(f"gate input: {_clean(inp)}")
    return out


# --- signing -----------------------------------------------------------------------------------------------------


class Signed:
    """What :func:`sign` returns: the signed events and, for a grant, the secret (shown only after the commit)."""

    def __init__(self, events: list[dict[str, Any]], secret: str | None) -> None:
        self.events, self._secret = events, secret

    @property
    def secret(self) -> str | None:
        return self._secret

    def __repr__(self) -> str:
        return f"Signed({len(self.events)} events, secret={'<redacted>' if self._secret else None})"


def sign(
    prepared: Mapping[str, Any],
    *,
    backend: Any,
    key_id: str,
    workspace_id: str,
    owner_proof: Mapping[str, Any] | None = None,
) -> Signed:
    """Sign every event (after :func:`check` and the person's confirmation). A ``grant.issued`` gets its secret here:
    32 random bytes, hashed into the event before the signing bytes are built. A ``hub.joined`` / ``hub.left`` gets
    its ``owner_proof`` (#1198 C9: made by the owner's person key, see :func:`terminal_owner_proof`) here, before the
    signing bytes are built, and is refused without one. Nothing else changes in an event."""
    from orch.identity import GrantToken, format_grant, sign_person_event

    log = prepared["log"]
    events = copy.deepcopy(prepared["events"])
    secret: str | None = None
    out = []
    with contextlib.ExitStack() as stack:
        be = backend
        if len(events) > 1:
            # ONE passphrase for the events of one reviewed request (an Extend is two): the person saw every event in
            # the one review and pressed Enter. The key signs this log's event label and nothing else (D65 stays the
            # rule for anything that was not reviewed as one batch). The unlock itself lives in sequence_terminal.
            from .sequence_terminal import request_backend

            be = stack.enter_context(request_backend(backend, key_id, prepared, events, workspace_id))
        for ev in events:
            if ev["type"] == "grant.issued":
                import secrets as _secrets

                token = GrantToken(ev["grant"], _secrets.token_bytes(32))
                ev["secret_hash"] = canon.grant_secret_hash(token.secret)
                secret = format_grant(token)
            if ev["type"] in review.OWNER_PROOF_TYPES:
                if owner_proof is None or ev.get("owner_proof") is not None:
                    raise SignerRefused("refusing to sign: a hub step needs the owner proof of your backup key")
                ev["owner_proof"] = copy.deepcopy(dict(owner_proof))
            ev["sig"] = sign_person_event(be, key_id, workspace_id, log, ev, action=f"{prepared['op']} ({ev['type']})")
            out.append(ev)
    return Signed(out, secret)


#: how long a terminal-made owner proof lives: the contract's maximum (hub contract 9.6; a host request lives as long)
OWNER_PROOF_LIFE_MS = 600_000


def terminal_owner_proof(
    prepared: Mapping[str, Any],
    request_id: str,
    *,
    workspace_id: str,
    person: str,
    now_ms: Callable[[], int] | None = None,
    pk_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """#1198 C9, the Terminal (backup) path of a hub step: ask for the backup key (the recovery code, typed on
    ``/dev/tty``, never stored), derive the person key ``PK``, check that it is ``person``'s (before anything is
    signed), make the owner proof over the event payload the person just reviewed, and let the key go again.
    ``pk_factory`` is :class:`~orch.custody.personkey.RecoveryCodeSource` (a seam for tests); it signs only
    ``sig_hub_owner_proof`` here."""
    import time

    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    from . import hubproof

    events, facts = prepared["events"], prepared.get("owner_proof")
    if len(events) != 1 or not isinstance(facts, dict) or type(facts.get("dev")) is not bool:
        _refuse("the step carries no dev marker for its owner proof")
    remote = prepared["op"] in hubproof.REMOTE_OPS
    label = "sig_remote_owner_proof" if remote else "sig_hub_owner_proof"
    if pk_factory is None:
        from orch.custody.personkey import RecoveryCodeSource as pk_factory  # noqa: N813
    clock = now_ms or (lambda: int(time.time() * 1000))
    issued = clock()
    with pk_factory(
        labels=(label,),
        person_id=bytes.fromhex(person.removeprefix("p_")),
        action="sign the remote access step with your backup key"
        if remote
        else "sign the hub step with your backup key",
    ) as pk:
        try:
            return wire.build_owner_proof(
                workspace_id=workspace_id, pk_pub=pk.pk_pub, op=prepared["op"],
                payload=hubproof.payload_of(events[0]), request_id=request_id, issued_ms=issued,
                expires_ms=issued + OWNER_PROOF_LIFE_MS, dev=facts["dev"], sign=pk.signer(label),
            )  # fmt: skip
        except HubRefused as exc:
            raise SignerRefused(f"refusing to sign: the owner proof is not well formed ({exc.code})") from None


def check_uploads(prepared: Mapping[str, Any], files: Mapping[str, Any] | None) -> None:
    """Every artifact event must name bytes this client uploaded itself (``files``): its ``sha256`` and ``bytes`` are
    recomputed here, so a host cannot swap the file and the event together."""
    import base64
    import binascii

    uploaded = []
    for spec in (files or {}).values():
        try:
            uploaded.append(base64.b64decode(spec["b64"], validate=True))
        except (KeyError, TypeError, binascii.Error, ValueError):
            _refuse("an uploaded file is not inline base64")
    sums = {(canon.artifact_digest(b), len(b)) for b in uploaded}
    for e in prepared["events"]:
        if prepared.get("op") in ("ac.accept", "ac.unaccept"):
            _person_op(prepared)  # generated bytes are independently rebuilt, never exempted without checking
            continue
        if e["type"] in ("artifact.added", "artifact.replaced") and (e.get("sha256"), e.get("bytes")) not in sums:
            _refuse(f"{_clean(e.get('name'))}: the artifact is not the file you sent")


def run(
    call: Callable[..., Any],
    op: str,
    args: dict[str, Any],
    *,
    backend: Any,
    key_id: str,
    workspace_id: str,
    person: str,
    device: str,
    workspace: Callable[[str], tuple[str, str] | None],
    confirm: Callable[[str], bool],
    show_secret: Callable[[str], None],
    idem: str,
    files: dict[str, Any] | None = None,
    on_secret: Callable[[str], list[str]] | None = None,
    owner_proof: Callable[[Mapping[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The CLI composite of a person op in host mode. ``call(op, args, **opts)`` sends one request on the pinned
    workspace connection and returns its ``result`` (raising the host's error); ``confirm(text)`` shows the
    review on the person's terminal and returns whether the person pressed Enter; ``show_secret`` writes to that
    terminal only. Returns ``human.submit``'s result."""
    prepared = call(
        "human.prepare", {"op": op, "args": args, "signer": {"device": device, "auth": backend.auth}}, files=files
    )["data"]
    return finish(
        call,
        prepared,
        prepared["request"]["id"],
        backend=backend,
        key_id=key_id,
        workspace_id=workspace_id,
        person=person,
        device=device,
        workspace=workspace,
        confirm=confirm,
        show_secret=show_secret,
        idem=idem,
        files=files or {},  # every artifact event must name bytes this client uploaded: none uploaded, none passes
        on_secret=on_secret,
        owner_proof=owner_proof,
    )


def hand_over(call: Callable[..., Any], secret: str, deposit_id: str) -> dict[str, Any]:
    """H11c (D-P2-27): give a freshly drawn runner grant to the orch runner (``grant.deposit`` with ``auth.grant``) on
    the signer's own connection, after the commit. ``{deposit, deposit_id, grant}``; an unknown outcome asks
    ``grant.deposit_status`` once. Never returns or logs the secret."""
    from .errors import HostError

    gid = secret.partition(".")[0]
    outcome, reason = "unknown", None
    try:
        got = call("grant.deposit", {"deposit_id": deposit_id}, auth={"grant": secret})["data"]
        outcome, reason = got.get("deposit", "unknown"), got.get("reason")
    except HostError as exc:
        if exc.code not in ("outcome.unknown", "timeout", "host.unavailable"):
            outcome, reason = "rejected", exc.code
    except (OSError, ConnectionError, TimeoutError):
        pass
    if outcome not in ("confirmed", "rejected"):
        try:
            got = call("grant.deposit_status", {"deposit_id": deposit_id})["data"]
            outcome = got.get("deposit") if got.get("deposit") in ("confirmed", "rejected") else "unknown"
        except Exception:  # noqa: BLE001 - still unknown
            outcome = "unknown"
    out: dict[str, Any] = {"deposit": outcome, "deposit_id": deposit_id, "grant": gid}
    if reason:
        out["reason"] = str(reason)[:64]
    return out


def deposit_lines(out: Mapping[str, Any]) -> list[str]:
    """What the person's terminal says about a hand-over (never the secret)."""
    gid = out.get("grant")
    if out.get("deposit") == "confirmed":
        return [f"{gid} is with your orch runner; it was not shown. Forget it there: the dashboard, or grant.forget"]
    if out.get("deposit") == "rejected":
        why = f" ({out['reason']})" if out.get("reason") else ""
        return [f"the orch runner refused {gid}{why}; revoke it: orch grant revoke {gid}"]
    return [f"The grant may or may not be with your runner. Revoke it to be sure: orch grant revoke {gid}"]


def secret_text(secret: str) -> str:
    """What the person's own terminal shows after a grant's commit (and nothing else ever shows)."""
    return f"\nORCH_GRANT={secret}\n(shown once; orch keeps only its hash)\n"


def finish(
    call: Callable[..., Any],
    prepared: Mapping[str, Any],
    request_id: str,
    *,
    backend: Any,
    key_id: str,
    workspace_id: str,
    person: str,
    device: str,
    workspace: Callable[[str], tuple[str, str] | None],
    confirm: Callable[[str], bool],
    show_secret: Callable[[str], None],
    idem: str,
    files: dict[str, Any] | None = None,
    artifacts: str | None = None,
    before: Callable[[], None] | None = None,
    prefix: str = "",
    on_secret: Callable[[str], list[str]] | None = None,
    owner_proof: Callable[[Mapping[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The tail every signing client shares (the composite and the CLI signer): ``before`` (a client's own extra
    checks), :func:`check`, :func:`check_uploads` (when ``files`` is given), the review (``prefix``, then
    :func:`render`), the Enter confirmation, :func:`sign`; any failure up to there cancels the request (it expires
    anyway) and re-raises. Then ``human.submit`` with ``idem``, and only after the commit a grant's secret on the
    person's terminal. Returns ``human.submit``'s result."""
    try:
        if before is not None:
            before()
        check(prepared, workspace_id=workspace_id, person=person, device=device, auth=backend.auth)
        if files is not None:
            check_uploads(prepared, files)
        text = plain(prepared, workspace, artifacts=artifacts, prefix=prefix, members=members_of(call))
        if not confirm(text):
            raise OrchError("sign.cancelled", "nothing was signed")
        proof = None
        if any(e.get("type") in review.OWNER_PROOF_TYPES for e in prepared["events"]):  # #1198 C9: the backup key
            make = owner_proof or (
                lambda p, rid: terminal_owner_proof(p, rid, workspace_id=workspace_id, person=person)
            )
            proof = make(prepared, request_id)
        signed = sign(prepared, backend=backend, key_id=key_id, workspace_id=workspace_id, owner_proof=proof)
    except BaseException as exc:
        cancel_request(call, request_id, exc)
        raise
    result = call("human.submit", {"request_id": request_id, "events": signed.events}, idem=idem)
    if signed.secret is not None:
        if on_secret is not None:  # H11c: the secret goes where on_secret sends it (the runner), its lines are shown
            lines = on_secret(signed.secret)
            result = {**result, "lines": [*(result.get("lines") or []), *lines]}
        else:
            show_secret(secret_text(signed.secret))
    return result
