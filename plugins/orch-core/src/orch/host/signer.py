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
signer, check, review on the person's terminal, confirmation word, sign, ``human.submit``, and only then the grant
secret on the person's own terminal.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

from . import review

__all__ = ["Signed", "SignerRefused", "check", "check_reviews", "confirm_word", "header", "render", "run", "sign"]

_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ENVELOPE_OK = frozenset({"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on"})
_FORBIDDEN = frozenset({"seq", "at", "prev", "ws_seq", "host_sig", "sig"})
_GATE_TYPES = {"gate.approved": None, "gate.changes_requested": None, "verdict.given": "verify"}
_MEMBER_OPS = frozenset({"member.add", "member.remove", "member.role"})
_ADDON_OPS = frozenset({"addon.grant", "addon.disable", "addon.purge"})


class SignerRefused(Exception):
    """The prepared request is refused before any prompt; nothing is signed."""


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
    _hashes(prepared, workspace_id)
    material = prepared["material"]
    events = prepared["events"]
    check_reviews(material, events)
    digest = review.review_digest(material)
    if prepared.get("review_digest") != digest:
        _refuse("review_digest does not match the material")
    if prepared.get("fingerprint") != review.fingerprint(digest, events):
        _refuse("the fingerprint does not match")


def check_reviews(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> None:
    """Every ``Review`` (payload and derived) recomputed from the events and the material, compared with what the
    host sent; a section text whose ``section_hash`` differs from the event is refused here (F1 §5.6)."""
    try:
        reviews = [review.of(e, material) for e in events]
    except review.ReviewError as exc:
        _refuse(str(exc))
    if not _same(material.get("review"), reviews):
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


def _hashes(prepared: Mapping[str, Any], workspace_id: str) -> None:
    """Recompute every hash an event binds from what the material shows."""
    material = prepared.get("material")
    if not isinstance(material, dict):
        _refuse("no material")
    events, log, op = prepared["events"], prepared["log"], prepared["op"]
    kind = material.get("kind")
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
    if ev.get("policy_hash") != inp.get("policy_hash"):
        _refuse("the policy hash of the event is not the one in the gate input")
    if "source_sha" in ev and not _same(ev["source_sha"], inp.get("source_sha")):
        _refuse("the source list of the event is not the one in the gate input")


def _question(material: Mapping[str, Any], ev: Mapping[str, Any], workspace_id: str, log: str) -> None:
    q = material.get("question")
    if not isinstance(q, dict) or q.get("id") != ev.get("question"):
        _refuse("the material shows another question")
    try:
        qid = canon.question_id(workspace_id, log, q["id"])
        bound = canon.question_hash(qid, log, q["text"], q.get("options") or [])
    except Exception as exc:  # noqa: BLE001
        _refuse(f"the question cannot be hashed: {exc}")
    if ev.get("hash") != bound:
        _refuse("the question hash of the event is not the hash of the question shown")


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
    "answer": "answer {question} on {ticket}",
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
}


def _clean(value: Any) -> str:
    text = value if isinstance(value, str) else canon.dumps(value).decode("utf-8")
    return canon.clean(text).replace("\n", "⟨U+000A⟩")


def confirm_word(prepared: Mapping[str, Any]) -> str:
    """The word the person types: the ticket key for decisions and ticket ops, ``GRANT`` for grants, the person id
    for member ops, the addon name for addon ops, else the op name."""
    op, ev, ticket = prepared["op"], prepared["events"][0], prepared["material"].get("ticket")
    if op == "grant":
        return "GRANT"
    if op in _MEMBER_OPS:
        return str(ev.get("person"))
    if op in _ADDON_OPS:
        return str(ev.get("name"))
    if ticket is not None:
        return str(ticket["key"])
    return op.upper()


def header(prepared: Mapping[str, Any], workspace: Callable[[str], tuple[str, str] | None]) -> list[str]:
    """Built before any ticket content: the workspace from the signer's own registry, the op in plain words, the
    origin (kernel UID and PID recorded by the host; the client name is unverified), the fingerprint."""
    known = workspace(prepared["workspace_id"])
    if known is None:
        _refuse("this workspace is not in your own registry")
    name, prefix = known  # type: ignore[misc]
    ev, ticket = prepared["events"][0], prepared["material"].get("ticket")
    fields = {k: _clean(v) for k, v in review.payload(ev).items()}
    fields.setdefault("gate", "verify")
    fields["ticket"] = ticket["key"] if ticket else "the workspace"
    try:
        plain = _PLAIN.get(prepared["op"], prepared["op"] + " on {ticket}").format_map(_Default(fields))
    except (KeyError, ValueError):
        plain = prepared["op"]
    o = prepared["origin"]
    return [
        f"workspace: {_clean(name)} ({_clean(prefix)}) {prepared['workspace_id']}",
        f"op: {_clean(plain)}",
        f"requested by uid {int(o['uid'])} pid {int(o['pid'])} at {_clean(o['created_at'])}; "
        f"client {_clean(o['client'])} (self-declared, unverified)",
        f"fingerprint: {prepared['fingerprint']}",
    ]


class _Default(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def _summary(block: Mapping[str, Any]) -> str:
    derived = block["derived"]
    if "ticket_key" in derived:
        return f"{block['type']} on {derived['ticket_key']}"
    if "cert_device" in derived:
        return f"{block['type']}: device {derived['cert_device']} of {derived['cert_person']}"
    if derived.get("sections"):
        return f"{block['type']}: sections {', '.join(sorted(derived['sections']))}"
    return str(block["type"])


def render(prepared: Mapping[str, Any], workspace: Callable[[str], tuple[str, str] | None]) -> str:
    """The review the person reads, from the material and the events only (never host text): the header, then one
    block per event in event order, every payload key in canonical order, then derived values; section texts and
    ``before`` values are data, one ``| `` mark per line."""
    out = [*header(prepared, workspace), ""]
    material = prepared["material"]
    for i, block in enumerate(material["review"], 1):
        out.append(f"--- event {i} of {len(material['review'])}: {_summary(block)} ---")
        payload = block["payload"]
        for key in sorted(payload):
            if block["type"] == "grant.issued" and key == "secret_hash":
                out.append("secret_hash: drawn by this signer after you confirm")
                continue
            out.append(f"{_clean(key)}: {_clean(payload[key])}")
        for sid, sec in sorted((block["derived"].get("sections") or {}).items()):
            out.append(f"section {_clean(sid)} ({sec['hash']}):")
            out += ["| " + _clean(line) for line in sec["text"].split("\n")]
        for key, value in sorted(block["derived"].items()):
            if key != "sections":
                out.append(f"{key}: {_clean(value)}")
    if material.get("kind") == "gate":
        out.append(f"gate input of {material['gate']}: {_clean(material['input'])}")
    if material.get("kind") == "question":
        out.append(f"question: {_clean(material['question'])}")
    for path, value in sorted((material.get("before") or {}).items()):
        label = "current value according to the host, not signed" if not path.startswith(("ticket.", "body.")) else ""
        out.append(f"before {_clean(path)}{' (' + label + ')' if label else ''}:")
        out += ["| " + _clean(line) for line in (value if isinstance(value, str) else _clean(value)).split("\n")]
    for c in prepared.get("checks") or []:
        out.append(
            f"check: {_clean(c.get('repo'))} clean at {_clean(c.get('at'))} (attested by the worker, best effort)"
        )
    return "\n".join(out) + "\n"


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


def sign(prepared: Mapping[str, Any], *, backend: Any, key_id: str, workspace_id: str) -> Signed:
    """Sign every event (after :func:`check` and the person's confirmation). A ``grant.issued`` gets its secret here:
    32 random bytes, hashed into the event before the signing bytes are built. Nothing else changes in an event."""
    from orch.identity import GrantToken, format_grant, sign_person_event

    log = prepared["log"]
    events = copy.deepcopy(prepared["events"])
    secret: str | None = None
    out = []
    for ev in events:
        if ev["type"] == "grant.issued":
            import secrets as _secrets

            token = GrantToken(ev["grant"], _secrets.token_bytes(32))
            ev["secret_hash"] = canon.grant_secret_hash(token.secret)
            secret = format_grant(token)
        ev["sig"] = sign_person_event(backend, key_id, workspace_id, log, ev, action=f"{prepared['op']} ({ev['type']})")
        out.append(ev)
    return Signed(out, secret)


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
    confirm: Callable[[str, str], bool],
    show_secret: Callable[[str], None],
    idem: str,
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The CLI composite of a person op in host mode. ``call(op, args, **opts)`` sends one request on the pinned
    workspace connection and returns its ``result`` (raising the host's error); ``confirm(text, word)`` shows the
    review on the person's terminal and returns whether the person typed ``word``; ``show_secret`` writes to that
    terminal only. Returns ``human.submit``'s result."""
    prepared = call(
        "human.prepare", {"op": op, "args": args, "signer": {"device": device, "auth": backend.auth}}, files=files
    )["data"]
    request_id = prepared["request"]["id"]
    try:
        check(prepared, workspace_id=workspace_id, person=person, device=device, auth=backend.auth)
        if not confirm(render(prepared, workspace), confirm_word(prepared)):
            _refuse("not confirmed")
        signed = sign(prepared, backend=backend, key_id=key_id, workspace_id=workspace_id)
    except BaseException:
        try:
            call("human.cancel", {"request_id": request_id})
        except Exception:  # noqa: BLE001 - it expires in 10 minutes anyway
            pass
        raise
    result = call("human.submit", {"request_id": request_id, "events": signed.events}, idem=idem)
    if signed.secret is not None:
        show_secret(f"\nORCH_GRANT={signed.secret}\n(shown once; orch keeps only its hash)\n")
    return result
