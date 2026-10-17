"""The plain view of a signing request (B6, owner decision E of 16 Oct 2026): a headline, the content formatted like the
ticket page, and (in the terminal) the confirm prompt; the full dump stays one key away.

``build`` is a **pure function of the request the signer already checked** (``events`` and ``material``): every word it
shows is taken from those, never from host text outside them, so the content shown is the content that is signed. It
adds nothing to the material and changes no hash; the fingerprint, the review digest and the signed bytes are exactly
what they were.

**The invariant (security review of 16 Oct 2026).** The plain view shows every signed payload key that carries authority
or identity. A key may stay out of it only if it is in :data:`TECHNICAL_KEYS`, an explicit, reviewed list of hashes,
positions and attestations (they are in the details). For every event type the renderer names the keys it shows
(:data:`SHOWN`); a payload, gate input or task key that is neither shown nor technical, or an event type this module
does not know, raises :class:`PlainUnavailable` and the caller falls back to the full review (no plain view at all).
``tests/host/test_sign_plain.py`` enumerates every event type a person can sign and every property of its schema, and
fails when a key is added without being classified here.

The shape (``SignContent``) is the contract the dashboard reads (batch-6.md "Contract")::

    {headline, ticket?: {key, title}, sections?: [{id, title, text}], acceptance?: [{id, text, checked?}],
     tasks?: [{n, id, title, verify?: [str]}], fields_changed?: [{field, before, after, before_note?}],
     fields_title?: str, notes?: [str]}
    ticket = {key, title, uid_tail, key_note}   // key_note: the key is the host's word; uid_tail is the signed id's end

``before_note`` is set when ``before`` is the host's word and not checked against anything signed. The ticket title is
shown only when a signed event states it (a ``ticket.created``): a gate does not bind the title, so ``ticket.title`` is
``""`` for a gate request.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

__all__ = ["DETAILS_ONLY", "SHOWN", "TECHNICAL_KEYS", "PlainUnavailable", "build", "clean_tree", "headline", "text"]


class PlainUnavailable(ValueError):
    """The request holds something the plain view does not know how to show: the caller shows the full review."""


#: payload keys that are envelope, stamps, hashes or attestations: never shown in the plain view, always in the details
TECHNICAL_KEYS = frozenset(
    {
        # envelope and stamps
        "v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig", "seq", "at", "prev", "ws_seq",
        "host_sig",
        # hashes and attestations the details carry
        "gate_gen", "hash", "policy_hash", "base_rev", "secret_hash",
    }
)  # fmt: skip

#: per event type, the payload keys the plain view shows (in a section, a list or a field row)
SHOWN: dict[str, frozenset[str]] = {
    "addon.disabled": frozenset({"name"}),
    "addon.purged": frozenset({"name"}),
    "addon.granted": frozenset(
        {"name", "version", "package_sha256", "capabilities", "artifact_kinds", "fields", "sections"}
    ),
    "artifact.added": frozenset({"name", "kind", "label", "ac", "task", "ref", "addon", "bytes", "sha256"}),
    "artifact.replaced": frozenset({"name", "kind", "label", "ac", "task", "replaces", "bytes", "sha256"}),
    "device.added": frozenset({"device", "cert"}),
    "gate.approved": frozenset({"gate", "source_sha"}),
    "gate.changes_requested": frozenset({"gate", "text"}),
    "verdict.given": frozenset({"outcome", "text", "source_sha"}),
    "grant.issued": frozenset({"grant", "scope", "verbs", "label", "hours", "issued_at", "expires_at"}),
    "grant.revoked": frozenset({"grant", "reason"}),
    "log.added": frozenset({"text"}),
    "member.added": frozenset({"person", "name", "role", "pk_pub", "device_cert"}),
    "member.removed": frozenset({"person"}),
    "role.changed": frozenset({"person", "role"}),
    "people.changed": frozenset({"role", "add", "remove"}),
    "policy.changed": frozenset({"gates"}),
    "question.answered": frozenset({"question", "option", "text"}),
    "question.asked": frozenset({"qid", "question"}),
    "settings.changed": frozenset({"set"}),
    "ticket.closed": frozenset({"resolution", "duplicate_of", "text"}),
    "ticket.created": frozenset({"key", "ticket_type", "title", "owner"}),
    "ticket.moved": frozenset({"from", "to", "reason"}),
    "ticket.reopened": frozenset({"text"}),
    "ticket.updated": frozenset({"set", "sections"}),
}

#: K4: keys the plain view says in WORDS or leaves to the technical details (``d``, the full review, which carries every
#: signed key). Only for the ordinary grant (scope ``workable``, verbs ``agent``); any other value shows them as rows.
DETAILS_ONLY: dict[str, frozenset[str]] = {
    "grant.issued": frozenset({"grant", "scope", "verbs", "issued_at", "expires_at"}),
}

#: the keys of a gate input the plain view shows, and the ones that are hashes; of its ``fields``, a task, an
#: acceptance criterion, a policy, an artifact and a receipt, the ones it shows
_INPUT_SHOWN = frozenset({"fields", "tasks", "artifacts", "receipts", "source_sha", "prior"})
_INPUT_TECHNICAL = frozenset(
    {"workspace_id", "uid", "gate", "schema", "hash_v", "sections", "policy_hash", "people_hash", "addon_packages"}
)
_FIELDS_SHOWN = frozenset({"ticket_type", "size", "acceptance", "links", "addons"})
_TASK_SHOWN = frozenset({"id", "text", "verify", "proves"})
_AC_SHOWN = frozenset({"id", "text"})
_POLICY_SHOWN = frozenset({"approvers", "count", "not", "applies", "independent"})
_ARTIFACT_SHOWN = frozenset({"kind", "digest", "ac", "task"})
_RECEIPT_SHOWN = frozenset({"event", "repo", "commit", "exit"})


def _s(value: Any) -> str:
    return value if isinstance(value, str) else canon.dumps(value).decode("utf-8")


def span(hours: Any) -> str:
    """B8: a grant length in the words a person uses: ``3 days`` for a whole number of days, else ``N hours``."""
    if type(hours) is not int or hours < 1:
        return one(hours)
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" + ("" if days == 1 else "s")
    return f"{hours} hour" + ("" if hours == 1 else "s")


def one(value: Any) -> str:
    """The ONE helper every single-line value goes through (security review: a newline inside a title, a verify command,
    an acceptance text or a field must not fake structure such as a second task). Each line break is made visible as
    `` ⏎ ``; a block (a section's text) is rendered by :func:`text` under a ``| `` mark on every line instead."""
    return " ⏎ ".join(_s(value).replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _line1(value: Any, limit: int = 80) -> str:
    one = " ".join(_s(value).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _title(sid: str) -> str:
    return sid.replace("_", " ").replace(".", " ").capitalize()


def _show(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return one(value)
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return one(", ".join(value)) if value else "(none)"
    return one(_s(value))


def _short(digest: Any) -> str:
    """The first 12 hex digits of a ``sha256:`` digest."""
    return _s(digest).removeprefix("sha256:")[:12]


def _fp(value: Any) -> str:
    """A short fingerprint of a key or certificate: the first 12 hex of the sha256 of its canonical text."""
    return hashlib.sha256(_s(value).encode("utf-8")).hexdigest()[:12]


def _check_keys(
    where: str, got: Mapping[str, Any], shown: frozenset[str], technical: frozenset[str] = frozenset()
) -> None:
    extra = set(got) - shown - technical
    if extra:
        raise PlainUnavailable(f"the plain view does not know {where} {sorted(extra)}")


def _ticket(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> dict[str, str] | None:
    t = material.get("ticket")
    if not isinstance(t, Mapping) or not isinstance(t.get("key"), str):
        return None
    title = ""
    for ev in events:
        if ev.get("type") == "ticket.created" and isinstance(ev.get("title"), str):
            title = ev["title"]
    tail = _s(t.get("uid"))[-8:]
    return {
        "key": t["key"],
        "title": title,
        "uid_tail": tail,
        "key_note": f"key per host, not proven; the ticket's signed id ends …{tail}",
    }


def _ref(ticket: Mapping[str, str] | None) -> str:
    """The ticket as the headline names it: the host's key (not proven), the title only when a signed event states it,
    and the tail of the ticket's signed id (the key is the host's word, the id is what the signature names)."""
    if ticket is None:
        return "the workspace"
    name = f"{ticket['key']} · {one(ticket['title'])}" if ticket["title"] else ticket["key"]
    return f"{name} (key per host; signed id …{ticket['uid_tail']})"


def _row(out: dict[str, Any], field: str, after: Any, before: Any = None, note: str | None = None) -> None:
    row: dict[str, Any] = {"field": one(field), "before": _show(before), "after": _show(after)}
    if note and before is not None:
        row["before_note"] = note
    out.setdefault("fields_changed", []).append(row)


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def local_time(iso: Any, hours: Any = None) -> str | None:
    """K4/N1: a signed UTC ``YYYY-MM-DDTHH:MM:SSZ`` in this computer's time with the zone name (``Tue 23:45 CEST``; with
    the date when it is a week away or more). ``None`` when ``iso`` is not such a time (a placeholder): the caller then
    keeps the technical wording. English day names on purpose: the same bytes must render the same everywhere."""
    from datetime import UTC, datetime

    if type(iso) is not str:
        return None
    try:
        when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone()
    except ValueError:
        return None
    zone = when.tzname() or "UTC"
    day = (
        f"{_DAYS[when.weekday()]} {when.day} {_MONTHS[when.month - 1]}" if (hours or 0) > 144 else _DAYS[when.weekday()]
    )
    return f"{day} {when:%H:%M} {zone}"


def _grant_issued(ev: Mapping[str, Any], out: dict[str, Any]) -> str:
    """The grant review. The plain view of the ordinary grant (scope ``workable``, verbs ``agent``) is one sentence;
    scope, verbs, the grant id and the UTC times are in the technical details (``d``), which carry every signed field.
    Any other scope or verbs keeps the technical wording and rows: the plain sentence must never claim less than is
    signed (WYSIWYS)."""
    hours = ev.get("hours")
    plain = ev.get("scope") == "workable" and ev.get("verbs") == "agent" and type(hours) is int
    until = local_time(ev.get("expires_at"), hours) if plain else None
    _row(out, "who can use it", "agents working for you")
    if ev.get("label"):
        _row(out, "label", ev["label"])
    if not plain or until is None:
        _row(out, "scope", ev.get("scope"))
        _row(out, "verbs", ev.get("verbs"))
        _row(out, "from", ev.get("issued_at"))
        _row(out, "until", ev.get("expires_at"))
        _row(out, "grant id", ev.get("grant"))
        out.setdefault("notes", []).append(
            f"Agents can act for you for {span(hours)}, as far as the scope and verbs above allow."
        )
        return f"Give agents access for {span(hours)} (scope {_s(ev.get('scope'))}, verbs {_s(ev.get('verbs'))})"
    out.setdefault("notes", []).append("Scope, verbs, grant id and the exact times (UTC) are in the technical details.")
    return f"Agents may work on all open tickets for {span(hours)} (until {until}, your time)"


def _acceptance(items: Any) -> list[dict[str, Any]]:
    out = []
    for a in items or []:
        if not isinstance(a, Mapping):
            raise PlainUnavailable("an acceptance criterion is not an object")
        _check_keys("acceptance keys", a, _AC_SHOWN)
        out.append({"id": one(a.get("id")), "text": one(a.get("text")), "checked": False})
    return out


def _tasks(items: Any) -> list[dict[str, Any]]:
    out = []
    for n, t in enumerate(items or [], 1):
        if not isinstance(t, Mapping):
            raise PlainUnavailable("a task is not an object")
        _check_keys("task keys", t, _TASK_SHOWN)
        title = one(t.get("text"))
        if t.get("proves"):
            title += f" (proves {one(', '.join(_s(x) for x in t['proves']))})"
        task: dict[str, Any] = {"n": n, "id": one(t.get("id")), "title": title}
        verify = t.get("verify")
        if isinstance(verify, Mapping):
            _check_keys("verify keys", verify, frozenset({"cmd"}))
        elif verify is not None:
            raise PlainUnavailable("a task verify is not an object")
        cmd = verify.get("cmd") if isinstance(verify, Mapping) else None
        if cmd:
            task["verify"] = [one(cmd)]
        out.append(task)
    return out


def policy_words(policy: Mapping[str, Any]) -> str:
    """A gate policy in plain words; every key of it is in the sentence."""
    _check_keys("policy keys", policy, _POLICY_SHOWN)
    approvers = ", ".join(_s(x) for x in policy.get("approvers") or []) or "nobody"
    n = policy.get("count")
    out = f"{n} approval{'s' if n != 1 else ''} from {approvers}"
    if policy.get("not"):
        out += f"; not by {', '.join(_s(x) for x in policy['not'])}"
    applies = policy.get("applies")
    out += "; applies to " + ("all tickets" if applies == "all" else "no ticket" if applies == "off" else _s(applies))
    out += "; approvers independent of the author" if policy.get("independent") else ""
    return out


def clean_tree(value: Any) -> Any:
    """Every string of ``value`` through :func:`orch.canon.clean` (the escaping of the terminal review): no control or
    invisible character reaches a browser either."""
    if isinstance(value, str):
        return canon.clean(value)
    if isinstance(value, list):
        return [clean_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_tree(v) for k, v in value.items()}
    return value


def headline(prepared: Mapping[str, Any], *, verified: bool) -> str:
    return build(prepared, verified=verified)["headline"]


def build(prepared: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    """The ``SignContent`` of a prepared request (see the module doc). Raises :class:`PlainUnavailable` for anything
    it does not know how to show.

    ``verified`` says whether the caller ran the signer's material checks (:func:`orch.host.signer.check` or
    :func:`orch.host.signer.verify_material`: the gate, policy, question and section hashes, every ``before`` against
    the event's ``base_rev``) on exactly this request. Without them nothing the host sent is vouched for: every
    ``before`` is labelled "per host, not checked" and the gate's policy row is left out."""
    material, events = prepared["material"], list(prepared["events"])
    if not events:
        raise PlainUnavailable("a request without events")
    if material.get("sequence") is not None:  # batch 11: the first request of a sequence is one review of all its steps
        from . import sequence

        try:
            return sequence.build_content(prepared, verified=verified)
        except sequence.SequenceInvalid as exc:
            raise PlainUnavailable(str(exc)) from None
    ticket = _ticket(material, events)
    out: dict[str, Any] = {"headline": ""}
    ctx = _Ctx(verified)
    if ticket is not None:
        out["ticket"] = ticket
    kind = material.get("kind")
    if kind == "gate":
        _gate(material, events, ticket, out, ctx)
    elif kind == "question":
        _question(material, events, ticket, out)
    elif kind == "payload":
        _payload(material, events, ticket, out, ctx)
    else:
        raise PlainUnavailable(f"the plain view does not know the material kind {kind!r}")
    return {k: v for k, v in out.items() if v not in (None, [], "")}


class _Ctx:
    """What the caller vouched for (``verified``) and the label of a ``before`` nothing signed binds."""

    def __init__(self, verified: bool) -> None:
        self.verified = verified

    def note(self, bound: bool) -> str | None:
        """The ``before_note`` of a ``before``: ``bound`` is "the event's ``base_rev`` binds it"."""
        if not self.verified:
            return "per host, not checked"
        return None if bound else "per host, not signed"


# --- gate decisions -------------------------------------------------------------------------------------------------


def _gate(
    material: Mapping[str, Any], events: list[Mapping[str, Any]], ticket: Any, out: dict[str, Any], ctx: _Ctx
) -> None:
    if len(events) != 1:
        raise PlainUnavailable("a gate request holds one decision")
    ev = events[0]
    typ = _s(ev.get("type"))
    if typ not in ("gate.approved", "gate.changes_requested", "verdict.given"):
        raise PlainUnavailable(f"a gate request cannot hold {typ}")
    _check_keys(typ, ev, SHOWN[typ], TECHNICAL_KEYS)
    gate_id = _s(material.get("gate"))
    if "gate" in ev and ev["gate"] != gate_id:
        raise PlainUnavailable("the event and the material name different gates")
    gate = _title(gate_id).lower()
    verb = {
        "gate.approved": f"Approve the {gate} of",
        "gate.changes_requested": f"Ask for changes to the {gate} of",
        "verdict.given": f"Give the verdict {_s(ev.get('outcome'))} on the {gate} of",
    }[typ]
    out["headline"] = f"{verb} {_ref(ticket)}"
    texts = sorted((material.get("texts") or {}).items())
    out["sections"] = [{"id": sid, "title": _title(sid), "text": body} for sid, body in texts if body]
    # N12: a bound section with no text is noise in the plain view, said in one line (the full review lists each)
    empty = [_title(sid) for sid, body in texts if not body]
    if empty:
        out.setdefault("notes", []).append(f"Empty, not shown: {', '.join(empty)}.")
    out["fields_title"] = "Applies under"  # initial values of the ticket and the rule, not changes (N12)
    if ev.get("text"):
        out["sections"].append({"id": "comment", "title": "Your comment", "text": _s(ev["text"])})
    inp = material.get("input")
    if not isinstance(inp, Mapping):
        raise PlainUnavailable("a gate request without its input")
    _check_keys("gate input keys", inp, _INPUT_SHOWN, _INPUT_TECHNICAL)
    fields = inp.get("fields") or {}
    _check_keys("gate fields", fields, _FIELDS_SHOWN)
    if fields.get("acceptance"):
        out["acceptance"] = _acceptance(fields["acceptance"])
    if inp.get("tasks"):
        out["tasks"] = _tasks(inp["tasks"])
    for name in ("ticket_type", "size", "links"):
        if fields.get(name) is not None:
            _row(out, "type" if name == "ticket_type" else name, fields[name])
    for addon, value in sorted((fields.get("addons") or {}).items()):
        _row(out, f"add-on {addon}", value)
    for name, a in sorted((inp.get("artifacts") or {}).items()):
        _check_keys("gate artifact keys", a, _ARTIFACT_SHOWN)
        extra = "".join(f"; {k} {_s(a[k])}" for k in ("ac", "task") if a.get(k))
        _row(out, f"file {name}", f"{_s(a.get('kind'))}, digest {_short(a.get('digest'))}{extra}")
    for tid, r in sorted((inp.get("receipts") or {}).items()):
        _check_keys("gate receipt keys", r, _RECEIPT_SHOWN)
        _row(
            out,
            f"result of {tid}",
            f"{_s(r.get('repo'))} commit {_short(r.get('commit'))}, exit {_s(r.get('exit'))}, "
            f"event {_s(r.get('event'))}",
        )
    for label, items in (("code", inp.get("source_sha")), ("code you decide on", ev.get("source_sha"))):
        for item in items or []:
            _check_keys("source_sha keys", item, frozenset({"repo", "ref", "sha"}))
            _row(out, label, f"{_s(item.get('repo'))} {_s(item.get('ref'))} at {_short(item.get('sha'))}")
    for earlier, p in sorted((inp.get("prior") or {}).items()):
        _check_keys("prior gate keys", p, frozenset({"gen", "approvals"}))
        who = ", ".join(_s(x) for x in p.get("approvals") or []) or "nobody"
        _row(out, f"earlier gate {earlier}", f"generation {_s(p.get('gen'))}, approved by {who}")
    policy = material.get("policy")
    if not isinstance(policy, Mapping):
        raise PlainUnavailable("a gate request without its policy")
    if ctx.verified:  # the policy is shown only when the signer recomputed its hash from it
        _row(out, "rule for this gate", policy_words(policy))


def _question(material: Mapping[str, Any], events: list[Mapping[str, Any]], ticket: Any, out: dict[str, Any]) -> None:
    if len(events) != 1:
        raise PlainUnavailable("a question request holds one answer")
    ev = events[0]
    _check_keys("question.answered", ev, SHOWN["question.answered"], TECHNICAL_KEYS)
    q = material.get("question") or {}
    _check_keys("question keys", q, frozenset({"id", "text", "options", "to", "blocking"}))
    for o in q.get("options") or []:
        _check_keys("question option keys", o, frozenset({"key", "label", "cost"}))
    opts = {_s(o.get("key")): _s(o.get("label")) for o in q.get("options") or []}
    picked = ev.get("option")
    answer = opts.get(_s(picked), _s(picked)) if picked else "your own words"
    out["headline"] = f"Answer “{_line1(q.get('text'), 60)}” on {_ref(ticket)} with {answer}"
    out["sections"] = [{"id": "question", "title": "Question", "text": _s(q.get("text", ""))}]
    if ev.get("text"):
        out["sections"].append({"id": "answer", "title": "Your answer", "text": _s(ev["text"])})
    if ev.get("question") != q.get("id"):
        raise PlainUnavailable("the event and the material name different questions")
    for o in q.get("options") or []:
        _row(
            out,
            f"option {_s(o.get('key'))}",
            _s(o.get("label")) + (f" (cost {_s(o['cost'])})" if o.get("cost") else ""),
        )
    # who it is addressed to and whether it blocks are the host's word: the question hash binds neither
    for k, label in (("to", "addressed to"), ("blocking", "blocks the ticket")):
        if k in q:
            _row(out, label, f"{_s(q[k])} (per host, not signed)")


# --- everything else ------------------------------------------------------------------------------------------------


def _payload(
    material: Mapping[str, Any], events: list[Mapping[str, Any]], ticket: Any, out: dict[str, Any], ctx: _Ctx
) -> None:
    before = material.get("before") or {}
    texts = material.get("texts") or {}
    ref = _ref(ticket)
    heads: list[str] = []
    for ev in events:
        typ = _s(ev.get("type"))
        if typ not in SHOWN:
            raise PlainUnavailable(f"the plain view does not know the event type {typ}")
        _check_keys(typ, ev, SHOWN[typ], TECHNICAL_KEYS)
        heads.append(_event(typ, ev, ref, out, before, texts, ctx))
    uniq = list(dict.fromkeys(heads))
    if len(uniq) == 1:
        out["headline"] = uniq[0]
    elif len(uniq) <= 3:
        out["headline"] = " and ".join(uniq)
    else:
        out["headline"] = f"{uniq[0]} (+{len(uniq) - 1} more changes)"


def _event(
    typ: str,
    ev: Mapping[str, Any],
    ref: str,
    out: dict[str, Any],
    before: Mapping[str, Any],
    texts: Mapping[str, str],
    ctx: _Ctx,
) -> str:
    """The headline of one event; everything it shows is added to ``out``."""
    if typ == "ticket.created":
        title = _s(ev.get("title"))
        out["ticket"] = {**(out.get("ticket") or {}), "key": _s(ev.get("key")), "title": one(title)}
        _row(out, "type", ev.get("ticket_type"))
        _row(out, "owner", ev.get("owner"))
        tail = (out.get("ticket") or {}).get("uid_tail")
        return f"Create ticket {_s(ev.get('key'))} · {one(title)}" + (
            f" (key per host; signed id …{tail})" if tail else ""
        )
    if typ == "ticket.updated":
        return _updated(ev, ref, out, before, texts, ctx)
    if typ == "log.added":
        out.setdefault("sections", []).append({"id": "note", "title": "Note", "text": _s(ev.get("text"))})
        return f"Add a note to {ref}"
    if typ in ("ticket.closed", "ticket.reopened"):
        if ev.get("text"):
            out.setdefault("sections", []).append({"id": "reason", "title": "Message", "text": _s(ev["text"])})
        if ev.get("duplicate_of"):
            _row(out, "duplicate of", ev["duplicate_of"])
        if typ == "ticket.reopened":
            return f"Reopen {ref}"
        return f"Close {ref} as {_s(ev.get('resolution'))}"
    if typ == "ticket.moved":
        _row(out, "from", ev.get("from"))
        if ev.get("reason"):
            _row(out, "reason", ev["reason"])
        return f"Move {ref} to {_s(ev.get('to'))}"
    if typ == "grant.issued":
        return _grant_issued(ev, out)
    if typ == "grant.revoked":
        _row(out, "grant id", ev.get("grant"))
        if ev.get("reason") == "extended":  # B8: the old half of an Extend
            out.setdefault("notes", []).append("Agents that are running now stop; new agents use the extended access.")
        if ev.get("reason"):
            _row(out, "reason", ev["reason"])
        return f"Take back the agent access {_s(ev.get('grant'))}"
    if typ == "settings.changed":
        for key, value in sorted((ev.get("set") or {}).items()):
            _row(out, key, value, before.get(key), ctx.note(False))
        if "grant_hours" in (ev.get("set") or {}):  # B8: the longest agent access, in days/hours
            out.setdefault("notes", []).append(
                f"Agents may get access for up to {span(ev['set']['grant_hours'])} (members; the runner too)."
            )
        return "Change the workspace settings"
    if typ == "people.changed":
        role = _s(ev.get("role"))
        add, remove = ev.get("add") or [], ev.get("remove") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            raise PlainUnavailable("people.changed lists are not lists")
        for verb, people in (("add", add), ("remove", remove)):
            for i, who in enumerate(people, 1):  # one row per person: the dashboard labels a lone person id by name
                _row(out, f"{role}: {verb}" + (f" {i}/{len(people)}" if len(people) > 1 else ""), one(who))
        who = ", ".join(one(x) for x in add) or ", ".join(one(x) for x in remove)
        if role == "owner":
            return f"Make {who} the owner of {ref}"
        return f"{'Add' if add else 'Remove'} {who} as {role[:-1] if role.endswith('s') else role} of {ref}"
    if typ == "policy.changed":
        for gate, p in sorted((ev.get("gates") or {}).items()):
            _row(out, f"rule for {gate}", policy_words(p))
        return "Change the approval rules" + (f" of {ref}" if ref != "the workspace" else "")
    if typ == "member.added":
        _row(out, "person", ev.get("person"))
        _row(out, "name", ev.get("name"))
        _row(out, "role", ev.get("role"))
        _row(out, "key fingerprint", _fp(ev.get("pk_pub")))
        _cert_rows(out, "device certificate", ev.get("device_cert"))
        return f"Add {_s(ev.get('name'))} as {_s(ev.get('role'))}"
    if typ == "member.removed":
        _row(out, "person", ev.get("person"))
        return f"Remove member {_s(ev.get('person'))}"
    if typ == "role.changed":
        _row(out, "person", ev.get("person"))
        return f"Make {_s(ev.get('person'))} {_s(ev.get('role'))}"
    if typ in ("artifact.added", "artifact.replaced"):
        for k in ("kind", "label", "ref", "addon", "ac", "task", "replaces"):
            if ev.get(k) is not None:
                _row(out, k, ev[k])
        _row(out, "size", f"{_s(ev.get('bytes'))} bytes")
        _row(out, "digest", _short(ev.get("sha256")))
        out.setdefault("notes", []).append(
            "The file itself is not shown here; compare the digest with the file you meant (details: d)."
        )
        return f"{'Add' if typ == 'artifact.added' else 'Replace'} the file {_s(ev.get('name'))} on {ref}"
    if typ in ("addon.disabled", "addon.purged", "addon.granted"):
        verb = {"addon.granted": "Grant", "addon.disabled": "Disable", "addon.purged": "Purge"}[typ]
        if typ == "addon.granted":
            _row(out, "version", ev.get("version"))
            _row(out, "package digest", _short(ev.get("package_sha256")))
            for k in ("capabilities", "artifact_kinds", "fields", "sections"):
                if ev.get(k) is not None:
                    _row(out, k.replace("_", " "), ev[k])
        return f"{verb} the add-on {_s(ev.get('name'))}"
    if typ == "device.added":
        _row(out, "device", ev.get("device"))
        _cert_rows(out, "certificate", ev.get("cert"))
        return "Add a paired device to your devices"
    if typ == "question.asked":
        out.setdefault("sections", []).append({"id": "question", "title": "Question", "text": _s(ev.get("question"))})
        _row(out, "question id", ev.get("qid"))
        return f"Ask a question on {ref}"
    raise PlainUnavailable(f"the plain view does not know the event type {typ}")


def _cert_rows(out: dict[str, Any], label: str, cert: Any) -> None:
    """A device certificate: the fingerprint of the whole certificate and the fields that decide what it may do."""
    _row(out, f"{label} fingerprint", _fp(cert))
    o = cert.get("o") if isinstance(cert, Mapping) else None
    if not isinstance(o, Mapping):
        raise PlainUnavailable("a certificate without its signed part")
    for k in ("person_id", "device_id", "scopes_max", "expires_ms"):
        if o.get(k) is not None:
            _row(out, f"{label} {k.replace('_', ' ')}", o[k])


def _updated(
    ev: Mapping[str, Any],
    ref: str,
    out: dict[str, Any],
    before: Mapping[str, Any],
    texts: Mapping[str, str],
    ctx: _Ctx,
) -> str:
    names: list[str] = []
    checked = set(ev.get("base_rev") or {})
    for sid, entry in sorted((ev.get("sections") or {}).items()):
        if isinstance(entry, Mapping):
            _check_keys("section keys", entry, frozenset({"hash", "refs"}))
            if entry.get("refs"):
                _row(out, f"{_title(sid)} references", list(entry["refs"]))
        out.setdefault("sections", []).append({"id": sid, "title": _title(sid), "text": texts.get(sid, "")})
        names.append(_title(sid).lower())
    for path, value in sorted((ev.get("set") or {}).items()):
        field = path[len("ticket.") :] if path.startswith("ticket.") else path
        if field == "acceptance":
            out["acceptance"] = _acceptance(value)
            names.append("acceptance criteria")
        elif field == "tasks":
            out["tasks"] = _tasks(value)
            names.append("plan")
        else:
            # `before` is shown as checked only when the event's base_rev binds it; otherwise it is the host's word
            _row(out, field, value, before.get(path), ctx.note(path in checked))
            names.append(field)
    return f"Change the {', '.join(names)} of {ref}" if names else f"Change {ref}"


# --- the terminal text ---------------------------------------------------------------------------------------------


def text(content: Mapping[str, Any], clean: Callable[[Any], str], *, workspace: str | None = None) -> list[str]:
    """``content`` as the terminal's plain view. Every value goes through ``clean`` (control characters never reach
    the terminal) and :func:`one` (a line break never starts a line of its own: structure such as ``2. task`` cannot be
    faked); only a section's text is a block, every line of it under a ``| `` mark."""

    def v(value: Any) -> str:
        return clean(one(value))

    def block(value: Any) -> list[str]:
        lines = _s(value).split("\n") if value not in (None, "") else ["(empty)"]
        return ["  | " + clean(line) for line in lines]

    out = [v(content["headline"])]
    if workspace:
        out.append(f"in workspace {v(workspace)}")
    # N13: the host's word for the key ("per host, not proven") is in the technical details (`d`), not in this view
    out.append("")
    for sec in content.get("sections") or []:
        out += [v(sec["title"]), *block(sec["text"]), ""]
    if content.get("acceptance"):
        out.append("Acceptance criteria")
        out += [f"  [ ] {v(a['id'])}  {v(a['text'])}" for a in content["acceptance"]]
        out.append("")
    if content.get("tasks"):
        out.append("Plan")
        for t in content["tasks"]:
            ident = f" [{v(t['id'])}]" if t.get("id") else ""
            out.append(f"  {t['n']}.{ident} {v(t['title'])}")
            out += [f"       verify: {v(x)}" for x in t.get("verify") or []]
        out.append("")
    if content.get("fields_changed"):
        title = content.get("fields_title")
        out.append(
            title or ("Changes" if any(c["before"] is not None for c in content["fields_changed"]) else "Fields")
        )
        for c in content["fields_changed"]:
            new = "(not set)" if c["after"] is None else v(c["after"])
            if c["before"] is None:
                out.append(f"  {v(c['field'])}: {new}")
            else:
                note = f" ({v(c['before_note'])})" if c.get("before_note") else ""
                out.append(f"  {v(c['field'])}: {v(c['before'])}{note} -> {new}")
        out.append("")
    for note in content.get("notes") or []:
        out += [v(note), ""]
    return out
