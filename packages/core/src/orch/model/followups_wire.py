"""Ledger wiring for the pure follow-up rules; no clocks or I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import claims, identity
from . import followup_events as fe
from . import followups as fu
from .codes import Code, Refusal
from .types import Core, FollowupRec, Grant, TCore, WsCore, ts


def grant_chain(ws: WsCore, gid: str):
    return fu.ancestors(
        {
            g.id: fu.GrantTerms(
                g.id, g.person, g.scope, g.verbs, ts(g.issued_at), ts(g.expires_at), g.parent, g.revoked
            )
            for g in ws.grants.values()
        },
        gid,
    )


def caps_of(ws: WsCore) -> fu.Caps:
    """The caps of a workspace. DEV-ONLY: a dev sandbox workspace (signed genesis prefix SBX, never a real one) gets the
    short minimum lead and zero early tolerance, so real-host e2e runs take seconds."""
    from .engine import DEV_PREFIX

    return fu.effective_caps(ws.settings.get("followups", {}), dev=ws.prefix == DEV_PREFIX)


def enabled(ws: WsCore) -> bool:
    addon = ws.addons.get("schedules")
    return bool(addon and addon.enabled and not addon.purged)


def active(ws: WsCore, person: str) -> bool:
    return identity.member_role(ws, person) in ("owner", "maintainer", "member")


def chain_state(ws: WsCore, t: TCore, now: int, *, before: int | None = None):
    c = t.fu_chain
    events = [fu.ChainSet(at) for at in c.get("sets", [])]
    events.extend(fu.ChainDone(at, task) for at, task in c.get("dones", []))
    return fu.chain_state(
        sorted((e for e in events if before is None or e.at < before), key=lambda e: e.at),
        open_tasks_at_start=c.get("start_tasks", frozenset()),
        caps=caps_of(ws),
        now=now,
        since=c.get("since"),
    )


def view_state(core: Core, t: TCore, rec: FollowupRec, now: int) -> tuple[str, str | None]:
    """Return the effective state and cancellation reason without changing replay state."""
    ws = core.ws
    reason = fu.derived_cancel_reason(
        state=rec.state,
        replaced=False,
        ticket_status=t.status,
        claimed_by_other=bool(t.claim and not claims.in_family(t.claim.session, rec.setter_session)),
        chain=grant_chain(ws, rec.parent_grant),
        root_person_active=active(ws, rec.person),
        member_removed=identity.member_role(ws, rec.person) is None,
        addon_enabled=enabled(ws),
        # The set's chain verdict is recorded by set_state. Later sets replace it explicitly;
        # counting this set again would incorrectly cancel the last permitted pending set.
        chain_needs_person=chain_state(ws, t, now, before=ts(rec.set_at)).needs_person,
    )
    return ("cancelled", reason) if reason else (rec.state, rec.cancel_reason)


def denied(denial: fu.Denial | None) -> Refusal | None:
    return Refusal(Code.FOLLOWUP_DENIED, f"{denial.code}: {denial.detail}") if denial else None


def set_event(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    ws, a, now = core.ws, e["actor"], ts(e["at"])
    if e["followup"] in t.followups:
        return Refusal(Code.FOLLOWUP_DENIED, "duplicate follow-up id")
    if e.get("trigger") == "addon":
        return Refusal(Code.FOLLOWUP_DENIED, "bad_trigger: addon follow-ups are not built yet")
    if e.get("trigger") == "time":
        if "on" in e or "hook" in e:
            return Refusal(Code.FOLLOWUP_DENIED, "bad_trigger: a time follow-up has no event match")
    elif not fe.valid_match(e.get("on")) or e["on"]["source"] != e["trigger"]:
        return Refusal(Code.FOLLOWUP_DENIED, "bad_trigger: the event match does not fit the trigger")
    elif (e["trigger"] == "webhook") != ("hook" in e) or (e["trigger"] == "webhook" and e["hook"] != e["on"]["hook"]):
        return Refusal(Code.FOLLOWUP_DENIED, "bad_trigger: a webhook follow-up names exactly its hook")
    pending = [r for r in t.followups.values() if view_state(core, t, r, now)[0] == "pending"]
    ctx = fu.SetContext(
        a["kind"] == "agent",
        a.get("grant") in ws.grants,
        a.get("unattended", False),
        bool(t.claim and t.claim.session == a.get("session") and claims.lapse_reason(ws, t, now) is None),
        t.status,
        enabled(ws),
        enabled(ws),
        sum(
            view_state(core, ticket, r, now)[0] == "pending"
            for ticket in core.tickets.values()
            for r in ticket.followups.values()
        ),
        bool(pending),
    )
    req = fu.SetRequest(
        now,
        e["trigger"],
        ts(e["due"]) if "due" in e else None,
        ts(e["deadline"]) if "deadline" in e else None,
        e["brief"],
    )
    caps = caps_of(ws)
    if (r := denied(fu.check_set(ctx, req, caps))) is not None:
        return r
    for old in pending:
        old.state, old.cancel_reason = "cancelled", "replaced"
    rec = FollowupRec(
        e["followup"],
        fu.set_state(chain_state(ws, t, now), req, caps),
        e["trigger"],
        e["at"],
        e.get("due"),
        e.get("deadline"),
        e["brief"],
        a["session"],
        a["for"],
        a["grant"],
        e["id"],
    )
    rec.label = e.get("label")
    rec.on, rec.hook = e.get("on"), e.get("hook")
    t.followups[rec.id] = rec
    t.claim = None
    claims.release_leases(t)
    return None


def fired_event(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    rec = t.followups.get(e["followup"])
    if rec is None:
        return Refusal(Code.FOLLOWUP_DENIED, "unknown follow-up")
    ws, now = core.ws, ts(e["at"])
    chain = grant_chain(ws, rec.parent_grant)
    facts = fu.FollowupFacts(
        view_state(core, t, rec, now)[0],
        rec.trigger,
        ts(rec.set_at),
        ts(rec.due) if rec.due else None,
        ts(rec.deadline) if rec.deadline else None,
    )
    req = fu.FiredRequest(
        now, e["trigger"], e.get("fallback"), e["actor"]["kind"] == "host", e["secret_hash"].removeprefix("sha256:")
    )
    if (r := denied(fu.check_fired(facts, req, chain=chain, root_person_active=active(ws, rec.person)))) is not None:
        return r
    if e["grant"] in ws.grants:
        return Refusal(Code.GRANT_EXISTS, e["grant"])
    wake = e.get("event")
    if wake is not None and (e["trigger"] in ("time", "deadline") or not fe.matches(rec.on or {}, wake)):
        return Refusal(Code.FOLLOWUP_DENIED, "bad_trigger: the event does not match the follow-up")
    assert chain
    g = fu.derive_grant(
        chain[0],
        ticket_uid=t.uid,
        grant_id=e["grant"],
        fired_at=now,
        secret_hash=e["secret_hash"].removeprefix("sha256:"),
    )

    def stamp(at):
        return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    ws.grants[g.id] = Grant(g.id, g.person, g.scope, g.verbs, stamp(g.issued_at), stamp(g.expires_at), parent=g.parent)
    rec.state, rec.fired_at, rec.grant, rec.run, rec.fallback = "fired", e["at"], g.id, e["run"], e.get("fallback")
    rec.event = wake
    return None


def outcome_event(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    rec = t.followups.get(e["followup"])
    if rec is None:
        return Refusal(Code.FOLLOWUP_DENIED, "unknown follow-up")
    if (
        r := denied(
            fu.check_outcome(
                view_state(core, t, rec, ts(e["at"]))[0], e["outcome"], e.get("reason"), ran_has_reason="reason" in e
            )
        )
    ) is not None:
        return r
    if "run" in e and e["run"] != rec.run:
        return Refusal(Code.FOLLOWUP_DENIED, "run does not match the fired run")
    rec.state = rec.outcome = e["outcome"]
    rec.reason = e.get("reason")
    if rec.grant is not None:
        core.ws.grants[rec.grant].ended = True
    return None


def cancelled_event(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    rec = t.followups.get(e["followup"])
    if rec is None or view_state(core, t, rec, ts(e["at"]))[0] not in ("pending", "needs_person"):
        return Refusal(Code.FOLLOWUP_DENIED, "only pending or needs_person follow-ups can be cancelled")
    a = e["actor"]
    if a["kind"] == "agent":
        if (
            not claims.in_family(a["session"], rec.setter_session)
            or a["for"] != rec.person
            or e["reason"] not in ("agent", "dismissed")
        ):
            return Refusal(Code.FOLLOWUP_DENIED, "an agent may cancel only its own session family's follow-up")
    elif a["kind"] == "person" and a["id"] != rec.person and not claims.signer_manages(core.ws, t, e):
        return Refusal(Code.FOLLOWUP_DENIED, "only the follow-up's person or a manager may cancel it")
    if e["reason"] == "run_now" and view_state(core, t, rec, ts(e["at"]))[0] != "needs_person":
        return Refusal(Code.FOLLOWUP_DENIED, "run_now applies to a follow-up that needs a person")
    rec.state, rec.cancel_reason = "cancelled", e["reason"]
    return None


def note_event(t: TCore, e: dict[str, Any]) -> None:
    now = ts(e["at"])
    if e["actor"]["kind"] == "person":
        t.fu_chain = {"since": now}
    elif e["type"] == "followup.set":
        if not t.fu_chain.get("sets"):
            t.fu_chain["start_tasks"] = frozenset(task["id"] for task in t.fields["tasks"])
        t.fu_chain.setdefault("sets", []).append(now)
    elif e["type"] == "task.done":
        t.fu_chain.setdefault("dones", []).append((now, e["task"]))
