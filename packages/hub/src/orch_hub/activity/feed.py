"""Admin-only read model over the two independent histories."""

from ..contract.phase4 import render_text
from .store import ACTIVITY_TYPES, bounded

AUDIT_TYPES = ("custodial", "grant", "membership", "space", "channel", "share", "market_policy")
WORDS = {
    "invite.created": "invited a person",
    "invite.redeemed": "accepted an invitation",
    "reset.issued": "started a password and code reset",
    "recovery.redeemed": "recovered an account",
    "user.disabled": "disabled a person",
    "user.enabled": "enabled a person",
    "authenticator.rotated": "changed an authenticator",
    "password.changed": "changed a password",
    "sessions.ended": "signed out everywhere",
    "change.proposed": "asked for an access change",
    "change.approved": "approved an access change",
    "change.overridden": "applied an access change without the owner",
    "change.expired": "an access change lapsed",
    "change.rejected": "rejected an access change",
    "connection.allowed": "allowed a connection",
    "connection.denied": "closed a connection",
    "connection.migrated": "carried over a connection",
    "channel.revoked": "removed a phone or browser",
    "pairing.issued": "made a code to pair a phone",
    "pairing.used": "paired a phone",
}
assert set(WORDS) == set(ACTIVITY_TYPES), "every activity type needs plain words"

#: plain names for the signed audit chain's record types (the "Kind of event" list)
AUDIT_WORDS = {
    "custodial": "approvals",
    "grant": "workspace access",
    "membership": "group members",
    "space": "shared spaces",
    "channel": "phones and browsers",
    "share": "shared information",
    "market_policy": "add-on rules",
}

#: what an approval was about, in words (a verb the table does not know is said generically)
VERB_WORDS = {
    "ticket.created": "a new ticket",
    "ticket.updated": "a ticket change",
    "status.changed": "a status change",
    "ticket.submitted": "a ticket for review",
    "ticket.closed": "closing a ticket",
    "ticket.reopened": "reopening a ticket",
    "question.answered": "an answer",
    "gate.approved": "a gate",
    "gate.changes_requested": "a request for changes",
    "verdict.given": "a verdict",
    "grant.requested": "agent access",
    "addon.requested": "an add-on",
    "settings.changed": "a settings change",
}


def feed(deps, query):
    def number(key, default=None):
        value = query.get(key)
        if value is None:
            return default
        if not value.isascii() or not value.isdigit() or len(value) > 18:
            raise ValueError("invalid number")
        return int(value)

    limit = number("limit", 100)
    if not 1 <= limit <= 200:
        raise ValueError("invalid limit")
    since, until = number("since_ms"), number("until_ms")
    if since is not None and until is not None and since > until:
        raise ValueError("invalid date range")
    typ, ws_filter, person = (query.get(k) or None for k in ("type", "workspace_id", "person"))
    if typ is not None and typ not in (*ACTIVITY_TYPES, *AUDIT_TYPES):
        raise ValueError("unknown event type")
    bounded(ws_filter)
    bounded(person)
    before = None
    if query.get("before"):
        parts = query["before"].split(":")
        if (
            len(parts) != 3
            or parts[1] not in ("a", "e")
            or any(not x.isascii() or not x.isdigit() or len(x) > 18 for x in (parts[0], parts[2]))
        ):
            raise ValueError("invalid cursor")
        before = (int(parts[0]), parts[1], int(parts[2]))
    names = {u["user_id"]: render_text(u["name"]) for u in deps.access.users()}
    workspaces = {ws: render_text((deps.registry.card(ws) or {}).get("name") or ws) for ws in deps.registry.pins()}

    def actor(kind, ident=None, session=None):
        if kind == "agent":
            return {"kind": "agent", "session": render_text(session or "")}
        if kind == "person" and ident and ident != "0" * 32:
            return {"kind": "person", "id": ident, "name": names.get(ident, render_text(ident)), "session": session}
        return {"kind": kind if kind in ("host", "hub") else "hub", "id": None, "name": None, "session": None}

    rows = []

    def add(source, seq, ts, event_type, who, ws, text, subject=None, detail=None):
        key = (ts, source, seq)
        detail = detail or {}
        if (
            (before is not None and key >= before)
            or (typ and event_type != typ)
            or (ws_filter and ws != ws_filter)
            or (since is not None and ts < since)
            or (until is not None and ts > until)
            or (
                person
                and person
                not in (who.get("id") if who["kind"] == "person" else None, subject, detail.get("approved_by"))
            )
        ):
            return
        row = {
            "id": f"{source}:{seq}",
            "ts_ms": ts,
            "type": event_type,
            "actor": who,
            "text": text,
            "workspace_id": ws,
            "workspace_name": workspaces.get(ws, render_text(ws) if ws else None),
            "subject_user": subject,
        }
        if detail.get("requested_by_agent_session"):
            row["requested_by_agent_session"] = render_text(detail["requested_by_agent_session"])
        if detail.get("approved_by"):
            ident = detail["approved_by"]
            row["approved_by"] = names.get(ident, render_text(ident))
            row["approved_by_id"] = ident
        rows.append((key, row))

    if deps.audit is not None:
        seq = 0
        while batch := deps.audit.store.after(seq, 200):
            for seq, signed, ev in batch:
                o = signed["o"]
                t, ws = o["type"], ev.get("workspace_id")
                who = actor("person", ev.get("user_id") if t == "custodial" else ev.get("actor_user_id"))
                text = {
                    "custodial": "approved "
                    + VERB_WORDS.get(ev.get("verb"), "a change")
                    + " in "
                    + workspaces.get(ws, render_text(ws or "workspace")),
                    "grant": ("allowed workspace access for " if ev.get("role") else "removed workspace access for ")
                    + render_text(ev.get("group", "group")),
                    "membership": ("added " if ev.get("added") else "removed ")
                    + names.get(ev.get("user_id"), render_text(ev.get("user_id", "person")))
                    + (" to " if ev.get("added") else " from ")
                    + render_text(ev.get("group", "group")),
                    "space": "changed a space",
                    "channel": "removed a device" if ev.get("change") == "revoke" else "registered a device",
                    "share": "changed shared information",
                    "market_policy": "changed the add-on policy",
                }.get(t, "changed workspace settings")
                add("a", seq, o["ts_ms"], t, who, ws, text, ev.get("user_id"))
    if deps.activity is not None:
        seq = None
        while batch := deps.activity.list(before_seq=seq, limit=200):
            for ev in batch:
                seq = ev["seq"]
                add(
                    "e",
                    seq,
                    ev["ts_ms"],
                    ev["type"],
                    actor(ev["actor_kind"], ev["actor_id"], ev["actor_session"]),
                    ev["workspace_id"],
                    WORDS[ev["type"]],
                    ev["subject_user"],
                    ev["detail"],
                )
    rows.sort(key=lambda pair: pair[0], reverse=True)
    page = rows[:limit]
    cursor = None
    if len(rows) > limit:
        ts, source, seq = page[-1][0]
        cursor = f"{ts}:{source}:{seq}"
    return {
        "rows": [row for _, row in page],
        "before": cursor,
        "types": [*ACTIVITY_TYPES, *AUDIT_TYPES],
        "type_words": {**WORDS, **AUDIT_WORDS},
    }
