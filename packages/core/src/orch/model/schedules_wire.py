"""Schedule ledger admission and replay share these transitions; no I/O or clock."""

from orch import canon

from . import claims, identity, schedules, visibility
from . import followups as fu
from .codes import Code, Refusal
from .followups_wire import canonical_ts, caps_of, enabled, stamp
from .types import Grant, ScheduleRec, ts


def denied(detail):
    return Refusal(Code.SCHEDULE_DENIED, detail)


def early_s(ws):
    """How long before an occurrence it may fire or be settled: the one window the model admits and the host timer
    uses (FU_TIME_EARLY_S, or DEV_EARLY_S in a dev sandbox workspace)."""
    return caps_of(ws).early_s


def addon_off(core):
    """The Schedules add-on was turned off or purged: every live run grant ends for good, so turning it on again
    never revives one (the next occurrence fires with a fresh grant)."""
    for ticket in core.tickets.values():
        for rec in ticket.schedules.values():
            for row in rec.occurrences.values():
                if row.get("grant") and not row.get("outcome"):
                    core.ws.grants[row["grant"]].ended = True


def device_live(ws, person, device, now):
    from .authz import device_valid, person_has_valid_device

    if not device:  # hub signer: its verified channel is not a device id
        return person_has_valid_device(ws, person, now)
    d = ws.devices.get(device)
    return bool(d and d.person == person and device_valid(d, d.cert, now, False) is None)


def first_allowed(ws, rec):
    """The earliest occurrence a schedule may fire or settle: never before it was signed plus the minimum lead, so
    a backdated `starts` cannot reach an occurrence the person never scheduled ahead of time."""
    return max(ts(rec.starts), ts(rec.set_at) + caps_of(ws).min_lead_s)


def grant_hours_cap(ws, person):
    """The longest a firing's run grant may last for this setter: a member's workspace ``grant_hours`` (an owner or
    maintainer has none), never more than the run ceiling FU_RUN_HOURS."""
    return min(
        fu.FU_RUN_HOURS, ws.settings["grant_hours"] if identity.member_role(ws, person) == "member" else fu.FU_RUN_HOURS
    )


def claim_block(core, t, rec, now):
    """Who holds the ticket's live claim, as a firing sees it: another person (`claimed_by_other`), the setter
    (`busy`, under any live, non-lapsed claim, with or without a live grant: the ledger cannot tell a running session
    from an idle one, so it never releases it), or nobody that keeps the firing from releasing a lapsed claim (None)."""
    c = t.claim
    if c is None or claims.lapse_reason(core.ws, t, now) is not None:
        return None
    if identity.canonical(core.ws, c.for_person) != rec.person:
        return "claimed_by_other"
    return "busy"


def blocked(core, t, rec, now, *, creating=None):
    # The right to act is the one authorize_agent applies to the run's person: a role that may run agents and
    # `can_see`. Ticket people (assignees, reviewers, watchers) never gate writes, so people.changed cannot remove
    # it while the setter still sees the ticket; a viewer role or leaving the restricted list does.
    if identity.member_role(core.ws, rec.person) not in ("owner", "maintainer", "member"):
        return "role_lost"
    if not device_live(core.ws, rec.person, rec.device, now):
        return "device_lost"
    if not visibility.can_see(core.ws, t, rec.person):
        return "access_lost"
    if t.status == "closed":
        return "ticket_closed"
    if rec.target == "new":
        for occurrence, row in rec.occurrences.items():
            if occurrence == creating:
                continue  # this firing reserves its own child before creation
            child = row.get("child")
            if child and (
                (child not in core.tickets and not row.get("outcome"))
                or (child in core.tickets and core.tickets[child].status != "closed")
            ):
                return "skipped_open_child"
        return None
    return claim_block(core, t, rec, now)


def set_event(core, t, e):
    if error := schedules.validate_target(e):
        return denied(error)
    if e.get("target") == "new" and (
        t.anchor_schedule != e["schedule"] or t.ticket_type != "chore" or t.fields["title"] != e["title"]
    ):
        return denied("new schedules require their signed anchor")
    if t.via is not None:  # a schedule's child never carries a schedule of its own (no chaining)
        return denied("a schedule child cannot be scheduled")
    ws, now, a = core.ws, ts(e["at"]), e["actor"]
    if a["kind"] != "person":
        return denied("only a person may set a schedule")
    if not enabled(ws):
        return denied("addon_off: the Schedules add-on must be enabled")
    signer = identity.signer(ws, e)
    if signer.role not in ("owner", "maintainer", "member") or not device_live(
        ws, signer.person, a.get("device", ""), now
    ):
        return denied("a non-viewer with a live device is required")
    if e.get("target") == "new" and t.owner != signer.person:
        return denied("the anchor and schedule must have the same signer")
    if not visibility.can_see(ws, t, signer.person):
        return denied("access_lost: the setter may not see this ticket")
    if t.status == "closed":
        return denied("ticket_closed")
    if any(e["schedule"] in ticket.schedules for ticket in core.tickets.values()):
        return denied("duplicate schedule id")
    if not (canonical_ts(e["starts"]) and canonical_ts(e["ends"])):
        return denied("starts and ends must be canonical UTC timestamps")
    error = schedules.validate_set(
        e["rule"], ts(e["starts"]), ts(e["ends"]), now, e["brief"], e["run_hours"], e.get("label")
    )
    if error:
        return denied(error)
    first = schedules.occurrences(e["rule"], ts(e["starts"]), ts(e["ends"]), now - 1)
    if first is None or first < now + caps_of(ws).min_lead_s:
        return denied("first occurrence must be after the minimum scheduling lead")
    pending = sum(
        not r.cancelled and ts(r.ends) > now for ticket in core.tickets.values() for r in ticket.schedules.values()
    )
    if pending >= fu.FU_PENDING_MAX:
        return denied("pending_cap")
    rec = ScheduleRec(
        e["schedule"],
        e["id"],
        signer.person,
        a.get("device", ""),
        e["rule"],
        e["starts"],
        e["ends"],
        e["brief"],
        e["run_hours"],
        e["at"],
        e.get("label"),
        target=e.get("target", "existing"),
        title=e.get("title"),
        set_sig_hash=canon.value_hash(e.get("sig")),
    )
    t.schedules[rec.id] = rec
    # A bounded root, never a usable runner credential. Chain traversal uses the
    # same grant shape, but schedule liveness is checked before trusting this root.
    ws.grants[e["id"]] = Grant(
        e["id"], rec.person, t.uid, "agent", e["starts"], e["ends"], ended=True, device=rec.device, root_kind="schedule"
    )
    return None


def fired_event(core, t, e):
    rec = t.schedules.get(e["schedule"])
    if e["actor"]["kind"] != "host" or rec is None or rec.cancelled:
        return denied("schedule is not live")
    # At-most-once is keyed on the occurrence string: only its canonical spelling may be recorded.
    if not canonical_ts(e["occurrence"]):
        return denied("occurrence must be a canonical UTC timestamp")
    now, occurrence = ts(e["at"]), ts(e["occurrence"])
    if not enabled(core.ws):
        return denied("addon_off: the Schedules add-on must be enabled")
    if e["occurrence"] in rec.occurrences:
        return denied("occurrence already recorded")
    if not ts(rec.starts) <= now < ts(rec.ends):
        return denied("schedule is outside its interval")
    if schedules.occurrences(rec.rule, ts(rec.starts), ts(rec.ends), occurrence - 1) != occurrence:
        return denied("not a rule occurrence")
    if occurrence < first_allowed(core.ws, rec):
        return denied("before_set: the occurrence precedes the schedule's signing plus the minimum lead")
    if not occurrence - early_s(core.ws) <= now <= occurrence + fu.FU_LATE_S:
        return denied("missed")
    if reason := blocked(core, t, rec, now):
        return denied(reason)
    runs = [o for ticket in core.tickets.values() for r in ticket.schedules.values() for o in r.occurrences.values()]
    if sum("fired_at" in o and ts(o["fired_at"]) // 86400 == now // 86400 for o in runs) >= fu.FU_RUNS_PER_DAY:
        return denied("runs_per_day")
    concurrent = sum(o.get("outcome") is None and "grant" in o and not core.ws.grants[o["grant"]].ended for o in runs)
    concurrent += sum(
        r.state == "fired" and r.grant is not None and not core.ws.grants[r.grant].ended
        for ticket in core.tickets.values()
        for r in ticket.followups.values()
    )
    if concurrent >= fu.FU_CONCURRENT_RUNS:
        return denied("concurrent_cap")
    if e["grant"] in core.ws.grants:
        return Refusal(Code.GRANT_EXISTS, e["grant"])
    target = t.uid
    if rec.target == "new":
        target = schedules.child_uid(rec.id, e["occurrence"])
        if e.get("child") != target or target in core.tickets:
            return denied("invalid child identity")
    elif "child" in e:
        return denied("existing-ticket schedules cannot create children")
    hours = min(rec.run_hours, grant_hours_cap(core.ws, rec.person))
    parent = fu.GrantTerms(rec.set_event, rec.person, target, "agent", now, min(ts(rec.ends), now + hours * 3600), None)
    try:
        derived = fu.derive_grant(
            parent,
            ticket_uid=target,
            grant_id=e["grant"],
            fired_at=now,
            secret_hash=e["secret_hash"].removeprefix("sha256:"),
            run_hours=hours,
        )
    except ValueError as exc:
        return denied(str(exc))
    until = stamp(derived.expires_at)
    core.ws.grants[derived.id] = Grant(
        derived.id, rec.person, target, derived.verbs, e["at"], until, parent=rec.set_event
    )
    row = {"fired_at": e["at"], "grant": derived.id, "run": e["run"]}
    # The firing clears the setter's own lapsed claim atomically, so the fresh session can claim the ticket; only
    # then does the row record `released`, the one case claim.taken admits this run without the ordinary rule. A
    # different person's live claim, or the setter's session under a live grant (busy), was refused above.
    if rec.target == "new":
        row["child"] = target
    if rec.target == "existing" and t.claim and identity.canonical(core.ws, t.claim.for_person) == rec.person:
        t.claim = None
        claims.release_leases(t)
        row["released"] = True
    rec.occurrences[e["occurrence"]] = row
    return None


def outcome_event(core, t, e):
    rec = t.schedules.get(e["schedule"])
    if e["actor"]["kind"] != "host" or rec is None:
        return denied("unknown schedule or non-host outcome")
    occ = e["occurrence"]
    if not canonical_ts(occ):
        return denied("occurrence must be a canonical UTC timestamp")
    instant = ts(occ)
    if schedules.occurrences(rec.rule, ts(rec.starts), ts(rec.ends), instant - 1) != instant:
        return denied("not a rule occurrence")
    if instant < first_allowed(core.ws, rec):
        return denied("before_set: the occurrence precedes the schedule's signing plus the minimum lead")
    row = rec.occurrences.get(occ, {})
    if row.get("outcome") or (not row and e["outcome"] != "did_not_run"):
        return denied("invalid outcome transition")
    if (
        e["outcome"] not in schedules.OUTCOMES
        or (e["outcome"] in ("ran", "ran_nothing") and "reason" in e)
        or (e["outcome"] not in ("ran", "ran_nothing") and e.get("reason") not in schedules.REASONS)
    ):
        return denied("invalid outcome or reason")
    if e["outcome"] == "ran_nothing":
        child = core.tickets.get(row.get("child"))
        if child is None or child.status != "closed" or child.close_resolution != "nothing_to_do":
            return denied("nothing_to_do requires a closed schedule child")
    if not row and ts(e["at"]) < instant - early_s(core.ws):
        return denied("occurrence is not due")
    rec.occurrences[occ] = {**row, "outcome": e["outcome"], "reason": e.get("reason")}
    if row.get("grant"):
        core.ws.grants[row["grant"]].ended = True
    return None


def cancelled_event(core, t, e):
    rec = t.schedules.get(e["schedule"])
    if rec is None or rec.cancelled or e["actor"]["kind"] != "person":
        return denied("a person and live schedule are required")
    signer = identity.signer(core.ws, e)
    if signer.person != rec.person and signer.role != "owner":
        return denied("only the setter or an owner/admin may cancel")
    rec.cancelled = True
    if rec.target == "new":
        return None  # cancellation stops future fires, not an in-flight child
    root = core.ws.grants[rec.set_event]
    root.revoked = True
    from .followups_wire import grant_chain

    for grant in core.ws.grants.values():
        chain = grant_chain(core.ws, grant.id)
        if chain and any(g.id == rec.set_event for g in chain):
            grant.ended = True
    return None


def root_valid(core, root_id, now):
    for ticket in core.tickets.values():
        for rec in ticket.schedules.values():
            if rec.set_event == root_id:
                return (
                    (not rec.cancelled or rec.target == "new")
                    and enabled(core.ws)
                    and ts(rec.starts) <= now < ts(rec.ends)
                    and blocked(core, ticket, rec, now) not in ("role_lost", "device_lost", "access_lost")
                )
    return True  # ordinary person-issued root


CHILD_ENVELOPE = frozenset(
    {"v", "id", "type", "actor", "hash_v", "based_on", "seq", "at", "prev", "ws_seq", "host_sig"}
)
CHILD_FIELDS = frozenset({"key", "ticket_type", "title", "owner", "via"})


def child_creation(core, uid, e):
    """A host-authenticated creation spends one recorded firing, never a runner grant."""
    # an ALLOWLIST: the chain envelope the store stamps plus exactly the child's own fields; any other field (auth, sig,
    # roster_v, schedule, an optional ticket field...) means it did not come from the runner's _create_child
    if set(e) - CHILD_ENVELOPE - CHILD_FIELDS:
        return denied("a schedule child uses only its recorded schedule authority")
    via = e.get("via")
    if not isinstance(via, dict) or set(via) != {"anchor", "schedule_id", "occurrence", "set_sig_hash"}:
        return denied("invalid schedule child reference")
    anchor = core.tickets.get(via["anchor"])
    rec = anchor.schedules.get(via["schedule_id"]) if anchor else None
    if rec is None or rec.target != "new" or rec.cancelled or not enabled(core.ws):
        return denied("schedule is not live")
    row = rec.occurrences.get(via["occurrence"], {})
    if (
        row.get("child") != uid
        or row.get("outcome")
        or uid != schedules.child_uid(rec.id, via["occurrence"])
        or via["set_sig_hash"] != rec.set_sig_hash
    ):
        return denied("child does not match the signed schedule and firing")
    if (
        e["actor"] != {"kind": "person", "id": rec.person, "device": rec.device}
        or e.get("owner") != rec.person
        or e.get("ticket_type") != "chore"
        or e.get("title") != schedules.child_title(rec, via["occurrence"])
    ):
        return denied("child must match the signed person, type and literal title")
    reason = blocked(core, anchor, rec, ts(e["at"]), creating=via["occurrence"])
    if reason:
        return denied(reason)
    return None


def child_run(core, grant):
    """Only a schedule child's own direct run grant can use the agent close exception."""
    child = core.tickets.get(grant.scope) if grant else None
    root = core.ws.grants.get(grant.parent) if grant else None
    if not (child and child.via and root and root.root_kind == "schedule"):
        return False
    # bound to THIS child's own run: the schedule must be a live new-ticket one on the anchor, and the root grant is the
    # one this very occurrence was fired with (another run's or another schedule's grant never closes it)
    anchor = core.tickets.get(child.via["anchor"])
    rec = anchor.schedules.get(child.via["schedule_id"]) if anchor else None
    if rec is None or rec.target != "new":
        return False
    return rec.occurrences.get(child.via["occurrence"], {}).get("grant") == grant.id
