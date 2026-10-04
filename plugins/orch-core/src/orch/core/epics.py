"""Epics (E2): a ticket of type `epic` groups children (tickets whose `parent` is the epic's id). An epic has the
requirements gate and no plan or tasks of its own.

The charter. Approving an epic is one human decision over the epic's requirements (hash v2) and, for every child
that is not done, its id, its requirements hash (v2) and its plan hash (None while it has no plan). The decision is
signed into the ledger (kind "charter", with the charter's own hash) next to the epic's ordinary gate entry, and each
covered child gets its gates written with `epic: <id>`. A child counts as approved only while the latest signed
charter of its current parent lists it with its current hashes and the epic's requirements still hash as signed: a
child that changes, is added later or is re-parented is not covered ("changed since epic approval") until the human
approves the epic again (or the child on its own).

Delegation. The human may opt in at approval time (`--delegate`): the limits travel only in the signed charter entry
and the approval event, never in a ticket file. While the delegation is active (not paused by a signed `pause`, and
the epic's requirements unchanged) an agent may approve a child it wrote with `orch epic auto-approve`: requirements
and plan, once per child, within the limits. Those approvals are the agent's own, so they are recorded as
`gate.delegated` events (and gates with `delegation: <id>`), never as signed human decisions; the agent may proceed on
one only while the delegation it names is signed and the epic unchanged, the child is within the limits and the
event matches. A signed pause stops further auto-approvals; it keeps (with their hashes) the ones valid when made.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager

from filelock import FileLock, Timeout

from orch.core import store
from orch.core.canonical import canonical_json
from orch.core.constants import SIZES
from orch.core.gates import HASH_VERSION, gate_hash
from orch.errors import NotFoundError, UsageError

EPIC = "epic"
DELEGATE_DEFAULTS = {"max_children": 10, "max_size": "m"}
FACTORY_DEFAULTS = {"max_children": 25, "max_size": "m", "max_hours": 72}
_SIZE_RANK = {s: i for i, s in enumerate(SIZES)}
# What a child is with respect to its epic's charter (child_state).
STATE_LABELS = {
    "covered": "covered by the epic approval", "delegated": "auto-approved under delegation",
    "approved": "approved on its own", "changed": "changed since epic approval", "new": "added since epic approval",
    "paused": "auto-approved, no longer valid: waits for you", "pending": "not approved yet", "done": "done",
}


def is_epic(t) -> bool:
    meta = t.meta if hasattr(t, "meta") else t
    return isinstance(meta, dict) and meta.get("type") == EPIC


def _parent_id(meta) -> str | None:
    p = meta.get("parent") if isinstance(meta, dict) else None
    return str(p) if isinstance(p, (str, int)) and str(p) else None


def parent_epic(ws, t, entries=None):
    """The epic `t` is a child of (a Ticket), or None (no parent, or the parent is not an epic)."""
    pid = _parent_id(t.meta)
    if not pid:
        return None
    try:
        entry = store.resolve(ws, pid, entries)
    except (NotFoundError, UsageError):
        return None
    if not is_epic(entry.meta):
        return None
    try:
        return store.read_ticket(entry.path)
    except Exception:  # an unreadable epic covers nothing
        return None


def children(ws, epic_id: str, entries=None) -> list:
    """The scan entries whose parent is `epic_id`, by id."""
    from orch.core.ids import normalize_ref
    want = normalize_ref(ws, epic_id).upper()
    out = [e for e in (store.scan(ws) if entries is None else entries)
           if e.meta is not None and (pid := _parent_id(e.meta)) and normalize_ref(ws, pid).upper() == want]
    return sorted(out, key=lambda e: e.id)


def epic_approved(t) -> bool:
    """The epic's requirements were approved at some point (the charter may be out of date since). Takes a Ticket
    or its meta."""
    meta = t.meta if hasattr(t, "meta") else t
    if not is_epic(meta):
        return False
    gates = meta.get("gates") if isinstance(meta.get("gates"), dict) else {}
    g = gates.get("requirements") if isinstance(gates.get("requirements"), dict) else {}
    return bool(g.get("approved"))


def normalize_delegate(delegate) -> dict | None:
    """The delegation limits as signed. A factory delegation (AI Factory, orch.core.permits) adds `factory: True` and
    a time budget `max_hours`; its defaults are 25 children or 72 hours (owner decision D5) and its children stay at
    size m or below (D6). An ordinary delegation keeps exactly its two keys, so its charter hash is unchanged."""
    if delegate is None or delegate is False:
        return None
    given = delegate if isinstance(delegate, dict) else {}
    factory = bool(given.get("factory"))
    d = dict(FACTORY_DEFAULTS if factory else DELEGATE_DEFAULTS)
    d.update({k: v for k, v in given.items() if v is not None and k != "factory"})
    try:
        d["max_children"] = int(d["max_children"])
    except (TypeError, ValueError):
        raise UsageError("--max-children must be a number") from None
    if d["max_children"] < 1:
        raise UsageError("--max-children must be at least 1")
    if d["max_size"] not in SIZES:
        raise UsageError(f"--max-size must be one of {', '.join(SIZES)}")
    out = {"max_children": d["max_children"], "max_size": d["max_size"]}
    if factory:
        if _SIZE_RANK[out["max_size"]] > _SIZE_RANK[FACTORY_DEFAULTS["max_size"]]:
            raise UsageError("a factory epic auto-approves children up to size m; larger children wait for you")
        try:
            hours = int(d["max_hours"])
        except (TypeError, ValueError):
            raise UsageError("the factory's hour budget must be a number") from None
        if hours < 1:
            raise UsageError("the factory's hour budget must be at least 1 hour")
        out.update(factory=True, max_hours=hours)
    return out


def budget_used_up(entry: dict, d: dict) -> bool:
    """A factory delegation's time budget (hours since the charter was signed) is used up."""
    if not d.get("max_hours"):
        return False
    from datetime import timedelta
    from orch import clock
    try:
        signed_at = clock.parse_stamp(str(entry.get("at")))
    except ValueError:
        return True  # no readable time: fail closed
    return clock.now() >= signed_at + timedelta(hours=d["max_hours"])


def child_hashes(t) -> dict:
    plan = gate_hash(t, "plan") if t.section("Plan").strip() else None
    return {"id": t.id, "requirements": gate_hash(t, "requirements"), "plan": plan}


def open_children(ws, epic, entries=None) -> list:
    """The epic's children that are not done, each read once (Tickets, by id)."""
    return [store.read_ticket(e.path) for e in children(ws, epic.id, entries) if e.status != "done"]


def pending_plans(ws, epic, entries=None) -> list:
    """The children whose plan waits for the human (`orch approve <epic> plans`): in progress or waiting (where a
    plan is approved), with a Plan, a plan gate, and that gate not approved for the current text. A child still open
    is covered by approving the epic again, which binds every child that is not done, plan included."""
    from orch.core.gates import gate_state, plan_required
    return [t for t in open_children(ws, epic, entries)
            if t.status in ("in-progress", "waiting") and t.section("Plan").strip() and plan_required(ws, t)
            and gate_state(t, "plan") != "approved"]


def charter(ws, epic, delegate=None, entries=None, tickets=None) -> dict:
    """What approving `epic` now would bind: {epic, epic_hash, hash_v, children: [{id, requirements, plan}],
    delegate, hash, content_hash}. Done children are left out. `hash` binds everything including the delegation;
    `content_hash` only what the human reads (the epic and its children): the approve view sends it back, and the
    delegation is chosen in the same form. `tickets`: the open children as already read (and shown) by the caller,
    so the hash binds exactly what was rendered."""
    kids = [child_hashes(t) for t in (open_children(ws, epic, entries) if tickets is None else tickets)]
    body = {"epic": epic.id, "epic_hash": gate_hash(epic, "requirements"), "hash_v": HASH_VERSION,
            "children": kids, "delegate": normalize_delegate(delegate)}
    content = {k: v for k, v in body.items() if k != "delegate"}
    return {**body, "hash": "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest(),
            "content_hash": "sha256:" + hashlib.sha256(canonical_json(content)).hexdigest()}


def _signed(ws, signed):
    """The signed ledger entries: as given, else read once per request scope (orch.core.store.memo)."""
    if signed is not None:
        return signed
    from orch.core import ledger
    return store.memo(ws, "ledger-entries", lambda: ledger.entries(ws))


def _events(ws, events):
    """The event log: as given, else read once per request scope."""
    if events is not None:
        return events
    from orch.core.events import read_events
    return store.memo(ws, "events-all", lambda: read_events(ws))


def latest_charter(ws, epic_id: str, signed=None) -> dict | None:
    for e in reversed(_signed(ws, signed)):
        if e.get("kind") == "charter" and e.get("ticket") == epic_id:
            return e
    return None


def ever_chartered(ws, epic_id: str, child_id: str, signed=None) -> bool:
    return any(e.get("kind") == "charter" and e.get("ticket") == epic_id
               and any(isinstance(c, dict) and c.get("id") == child_id for c in e.get("children") or [])
               for e in _signed(ws, signed))


def _epic_current(epic, entry: dict) -> bool:
    try:
        v = int(entry.get("hash_v") or 1)
    except (TypeError, ValueError):
        return False
    return epic.status != "done" and entry.get("epic_hash") == gate_hash(epic, "requirements", v)


def delegation(ws, epic, signed=None) -> dict | None:
    """The delegation of the latest signed charter, or None: {id, max_children, max_size, active, paused,
    epic_changed, expired} (a factory delegation also `factory`, `max_hours`). Active only while not paused, the
    epic's requirements still hash as signed and a factory's time budget is not used up."""
    signed = _signed(ws, signed)
    entry = latest_charter(ws, epic.id, signed)
    if not entry or not isinstance(entry.get("delegate"), dict) or not entry.get("delegation"):
        return None
    did = entry["delegation"]
    pause = next((e for e in signed if e.get("kind") == "pause" and e.get("ticket") == epic.id
                  and e.get("delegation") == did), None)
    changed = not _epic_current(epic, entry)
    d = normalize_delegate(entry["delegate"]) or dict(DELEGATE_DEFAULTS)
    kept = [k for k in (pause or {}).get("kept") or [] if isinstance(k, dict) and k.get("id")]
    expired = budget_used_up(entry, d)
    return {"id": did, **d, "paused": pause is not None, "epic_changed": changed, "expired": expired,
            "active": pause is None and not changed and not expired, "kept": kept, "at": entry.get("at")}


def _covered(ws, epic, child, gate: str, h, signed) -> bool:
    entry = latest_charter(ws, epic.id, signed)
    if not entry or not _epic_current(epic, entry):
        return False
    for c in entry.get("children") or []:
        if isinstance(c, dict) and c.get("id") == child.id:
            return bool(h) and c.get(gate) == h
    return False


def delegated_events(events, did: str) -> list:
    return [e for e in events if e.kind == "gate.delegated" and e.data.get("delegation") == did]


def delegated_children(events, did: str) -> list[str]:
    """Children auto-approved under delegation `did`, in the order of their first auto-approval."""
    out: list[str] = []
    for e in delegated_events(events, did):
        if e.ticket not in out:
            out.append(e.ticket)
    return out


def created_by_agent(events, child_id: str) -> bool:
    """The child was created by an agent (its `ticket.created` event): only those are auto-approved; a ticket the
    human created waits for the human. Exactly one such event must exist; it is the first and only one."""
    created = [e for e in events if e.kind == "ticket.created" and e.ticket == child_id]
    if len(created) != 1:
        return False  # none, or a second "created" for the same id (an appended event): not trusted
    return str(created[0].actor).startswith("agent:")


def frontmatter_delegated(ws, epic_id: str, did: str, entries=None) -> list[str]:
    """Children whose frontmatter names delegation `did` on any gate (counted with the events for the limit)."""
    out = []
    for e in children(ws, epic_id, entries):
        gates = e.meta.get("gates") if isinstance(e.meta.get("gates"), dict) else {}
        if any(isinstance(g, dict) and g.get("delegation") == did for g in gates.values()):
            out.append(e.id)
    return out


def _marker_dir():
    from orch.core.ledger import base_dir
    return base_dir() / "permits" / "children"


def _marker(did: str, child: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{did}.{child}")


def mark_delegated(did: str, child: str) -> None:
    """Note, beside the ledger and outside the repository, that delegation `did` auto-approved `child` (one exclusive
    marker per child). A marker only ever counts against the budget, so the agent's own process may write it."""
    d = _marker_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(d / _marker(did, child), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        pass


@contextmanager
def delegation_lock(did: str):
    """One lock for every checkout and process sharing the config dir: the child count and the marker write of an
    auto-approval happen under it, so two of them at the limit cannot both pass."""
    d = _marker_dir()
    d.mkdir(parents=True, exist_ok=True)
    fl = FileLock(str(d / f".{_marker(did, '')}lock"), timeout=30)
    try:
        fl.acquire()
    except Timeout as e:
        raise UsageError("another auto-approval holds the delegation", hint="retry in a moment") from e
    try:
        yield
    finally:
        fl.release()


def is_marked(did: str, child: str) -> bool:
    return (_marker_dir() / _marker(did, child)).exists()


def marked_delegated(did: str) -> int:
    """How many children delegation `did` approved, by markers (the repository's events and files cannot lower it)."""
    try:
        return sum(1 for n in os.listdir(_marker_dir()) if n.startswith(_marker(did, "")))
    except FileNotFoundError:
        return 0
    except OSError:
        return 10**9  # unreadable: fail closed, the limit counts as reached


def delegated_count(ws, epic_id: str, did: str, events, entries=None) -> int:
    """How many children the delegation has approved: the largest of the markers, the events and the frontmatter."""
    return max(marked_delegated(did), len(delegated_children(events, did)),
               len(frontmatter_delegated(ws, epic_id, did, entries)))


def hidden_in(child) -> bool:
    """The child's title or anything its approvals bind holds hidden or control characters (orch.textsafe)."""
    from orch.core.gates import gate_meta, gate_parts
    from orch.textsafe import decodes_to_hidden
    texts = [child.title, *(child.section(n) for n, _ in gate_parts(child, "requirements")),
             *(v for _, v in gate_meta(child, "requirements")), child.section("Plan")]
    return any(decodes_to_hidden(x) for x in texts)


def charter_blocker(child) -> str | None:
    """Why an epic approval cannot cover `child` now, or None: the one readiness check behind Ops' refusal and the
    epic page's approve view."""
    from orch.core.lifecycle import unanswered_blocking
    if is_epic(child):
        return f"{child.id} is an epic: no nested epics"
    if hidden_in(child):
        return f"the text of {child.id} holds hidden or control characters"
    missing = [x for x in ("Requirements", "Acceptance criteria") if not child.section(x).strip()]
    if missing:
        return f"{child.id} has {', '.join(missing)} empty"
    open_qs = unanswered_blocking(child)
    if open_qs:
        return f"{child.id} has blocking questions open ({', '.join(str(q.get('id')) for q in open_qs)})"
    return None


def within_limits(child, d: dict) -> str | None:
    """Why `child` is outside delegation `d`'s limits, or None."""
    if is_epic(child):
        return "epics are never auto-approved (no nested epics)"
    size = child.meta.get("size")
    if _SIZE_RANK.get(size, len(SIZES)) > _SIZE_RANK[d["max_size"]]:
        return f"size {size} is above the delegation's limit ({d['max_size']}); the human approves it"
    return None


def _delegated_ok(ws, epic, child, gate: str, g: dict, signed, events, entries=None) -> bool:
    """A delegated approval the agent may proceed on: the signed delegation is current (the epic unchanged); it is
    active, or it was paused and the signed pause kept this child with this hash; the child is within the limits,
    was created by an agent, was never in a charter, holds no hidden characters, its `gate.delegated` event matches,
    and it is among the first `max_children` the delegation approved (events and frontmatter both counted)."""
    d = delegation(ws, epic, signed)
    if not d or d["epic_changed"] or g.get("delegation") != d["id"] or within_limits(child, d):
        return False
    if d["paused"]:
        kept = next((k for k in d["kept"] if k.get("id") == child.id), None)
        if kept is None or kept.get(gate) != g.get("hash"):
            return False  # the pause stops every auto-approval it did not keep
    if ever_chartered(ws, epic.id, child.id, signed):
        return False  # a child the human saw in a charter is the human's to approve again
    if hidden_in(child):
        return False
    events = _events(ws, events)
    if not created_by_agent(events, child.id):
        return False
    if not any(e.ticket == child.id and e.data.get("gate") == gate and e.data.get("hash") == g.get("hash")
               and str(e.actor).startswith("agent:") for e in delegated_events(events, d["id"])):
        return False
    order = delegated_children(events, d["id"])
    return (child.id in order and order.index(child.id) < d["max_children"]
            and delegated_count(ws, epic.id, d["id"], events, entries) <= d["max_children"])


def gate_coverage(ws, child, gate: str, signed=None, events=None, entries=None) -> str | None:
    """"verified" when the latest signed charter of the child's epic covers the gate's stored hash, "delegated"
    when a valid delegated approval does, else None."""
    g = ((child.meta.get("gates") or {}).get(gate) or {})
    if not g.get("approved") or not g.get("hash"):
        return None
    epic = parent_epic(ws, child, entries)
    if epic is None:
        return None
    signed = _signed(ws, signed)
    if _covered(ws, epic, child, gate, g.get("hash"), signed):
        return "verified"
    if g.get("delegation") and _delegated_ok(ws, epic, child, gate, g, signed, events, entries):
        return "delegated"
    return None


def delegated_snapshot(ws, epic, d: dict, signed=None, events=None, entries=None) -> list[dict]:
    """The children whose auto-approval is valid right now, with the hashes it covers: what a pause keeps."""
    out = []
    for t in open_children(ws, epic, entries):
        gates = {}
        for gate in ("requirements", "plan"):
            g = (t.meta.get("gates") or {}).get(gate) or {}
            if g.get("delegation") == d["id"] and gate_coverage(ws, t, gate, signed, events, entries) == "delegated":
                gates[gate] = g.get("hash")
        if gates.get("requirements"):
            out.append({"id": t.id, "requirements": gates["requirements"], "plan": gates.get("plan")})
    return out


def child_state(ws, epic, child, signed=None, events=None, entries=None) -> str:
    """One of STATE_LABELS' keys: how `child` stands against `epic`'s approval."""
    from orch.core import ledger
    from orch.core.gates import gate_state, plan_required
    if child.status == "done":
        return "done"
    signed = _signed(ws, signed)
    entry = latest_charter(ws, epic.id, signed)
    gates = ["requirements"] + (["plan"] if plan_required(ws, child) and (
        child.section("Plan").strip() or child.status in ("in-progress", "waiting", "testing")) else [])
    verdicts = []
    for gate in gates:
        if gate_state(child, gate) != "approved":
            verdicts.append("pending")
            continue
        verdicts.append(ledger.gate_verification(ws, child, gate, signed, events=events, scan=entries))
    listed = entry is not None and any(isinstance(c, dict) and c.get("id") == child.id
                                       for c in entry.get("children") or [])
    if all(v in ("verified", "delegated") for v in verdicts):
        if any(gate_coverage(ws, child, g, signed, events, entries) == "verified" for g in gates):
            return "covered"
        return "delegated" if "delegated" in verdicts else "approved"
    g = (child.meta.get("gates") or {}).get("requirements") or {}
    if g.get("delegation") and gate_state(child, "requirements") == "approved":
        return "paused"
    if entry is None:
        return "pending"
    return "changed" if listed or ever_chartered(ws, epic.id, child.id, signed) else "new"


def charter_diff(ws, epic, signed=None, entries=None, tickets=None) -> dict:
    """The current charter against the latest signed one: {epic_changed, children: {id: unchanged|changed|new},
    removed: [ids], previous: entry or None}."""
    entry = latest_charter(ws, epic.id, signed)
    now_ = charter(ws, epic, entries=entries, tickets=tickets)
    before = {c["id"]: c for c in (entry or {}).get("children") or [] if isinstance(c, dict) and c.get("id")}
    out = {}
    for c in now_["children"]:
        old = before.get(c["id"])
        out[c["id"]] = ("new" if old is None
                        else "unchanged" if (old.get("requirements"), old.get("plan")) == (c["requirements"], c["plan"])
                        else "changed")
    return {"epic_changed": bool(entry) and not _epic_current(epic, entry), "children": out,
            "removed": [i for i in before if i not in out], "previous": entry}


def rollup(ws, epic, entries=None, needs=None) -> dict:
    """The epic's strip: children done n/m, the human's open moves among them, criteria proven n/m."""
    from orch.core import evidence
    kids = children(ws, epic.id, entries)
    ids = {e.id.upper() for e in kids}
    done = sum(1 for e in kids if e.status == "done")
    testing = sum(1 for e in kids if e.status == "testing")
    proven = total = 0
    for e in kids:
        try:
            t = store.read_ticket(e.path, shared=True)
        except Exception:
            continue
        p, n = evidence.progress(t)
        proven, total = proven + p, total + n
    you = 0
    if needs is not None:
        you = len({str(i.get("ticket")).upper() for i in needs} & ids)
    return {"done": done, "total": len(kids), "testing": testing, "needs_you": you, "ac_proven": proven,
            "ac_total": total}


def auto_approvals(ws, epic, events=None, signed=None, entries=None) -> list[dict]:
    """The audit of every auto-approval under the epic's delegations: {ticket, gate, hash, at, actor, delegation,
    valid}; valid = the agent may still proceed on it (orch.core.ledger.gate_verification says "delegated")."""
    from orch.core import ledger
    events = _events(ws, events)
    signed = _signed(ws, signed)
    by_id = {x.id: x for x in (store.scan(ws) if entries is None else entries)}
    out, tickets = [], {}
    for e in events:
        if e.kind != "gate.delegated" or e.data.get("epic") != epic.id:
            continue
        if e.ticket not in tickets:
            try:
                tickets[e.ticket] = store.read_ticket(by_id[e.ticket].path)
            except Exception:
                tickets[e.ticket] = None
        t = tickets[e.ticket]
        gate = str(e.data.get("gate"))
        g = ((t.meta.get("gates") or {}).get(gate) or {}) if t is not None else {}
        valid = (t is not None and g.get("hash") == e.data.get("hash")
                 and ledger.gate_verification(ws, t, gate, signed, events=events, scan=entries) == "delegated")
        out.append({"ticket": e.ticket, "title": t.title if t is not None else "", "gate": gate,
                    "hash": e.data.get("hash"), "at": e.at, "actor": e.actor, "delegation": e.data.get("delegation"),
                    "valid": valid})
    return out


def summary(ws, epic, entries=None, needs=None, events=None, tickets=None) -> dict:
    """Everything the epic page and `orch epic show` print: the current charter and its diff against the signed
    one, each child's state, the delegation, the audit of auto-approvals and the rolled-up strip."""
    signed = _signed(ws, None)
    events = _events(ws, events)
    kids = []
    for e in children(ws, epic.id, entries):
        try:
            t = store.read_ticket(e.path)
        except Exception:
            kids.append({"id": e.id, "title": "", "status": e.status, "state": "pending", "state_label": "unreadable"})
            continue
        state = child_state(ws, epic, t, signed, events, entries)
        kids.append({"id": t.id, "title": t.title, "status": t.status, "size": t.meta.get("size"), "state": state,
                     "state_label": STATE_LABELS[state]})
    current = charter(ws, epic, entries=entries, tickets=tickets)
    previous = latest_charter(ws, epic.id, signed)
    return {"epic": epic.id, "title": epic.title, "status": epic.status, "approved": epic_approved(epic),
            "charter": current, "signed_charter": previous.get("charter") if previous else None,
            "diff": {k: v for k, v in charter_diff(ws, epic, signed, entries, tickets=tickets).items()
                     if k != "previous"},
            "children": kids, "delegation": delegation(ws, epic, signed),
            "auto_approvals": auto_approvals(ws, epic, events, signed, entries),
            "rollup": rollup(ws, epic, entries, needs)}


def verdict_hash(tickets, ws) -> str:
    """What the epic verdict binds: each open child's id, status, criteria and Verification, in id order. The
    dashboard and the CLI show exactly these before the human accepts, and send the hash back. An artifact the
    criteria or the evidence show inline adds `artifacts` {"artifact <name>": "sha256:<hex>" | "missing"} to its item,
    and so does what a widget there pins ({"widget <name@v>" | "widget file <ref>": "sha256:<hex>" | "drift" |
    "missing"}, orch.core.artifacts.widget_binding). `ws` is required: every caller that shows or checks a verdict
    passes it, so a template or pinned file changed on disk after the page was drawn changes the hash (None only for
    text without widgets, e.g. a fixed example; a widget there raises)."""
    from orch.core.artifacts import binding
    body = []
    for t in sorted(tickets, key=lambda t: t.id):
        item = {"id": t.id, "status": t.status, "ac": t.section("Acceptance criteria"),
                "verification": t.section("Verification")}
        # evidence images by the sha256 their entry records, widget templates and files by their pins
        shown = binding(t, [item["ac"], item["verification"]], ws, widgets=True)
        if shown:
            item["artifacts"] = dict(shown)
        body.append(item)
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()
