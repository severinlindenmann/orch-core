"""Contract v1.4 read ops over the P1 model: ticket.query, ticket.get, needs, diff, artifact.get.

Reads only. Every function gets the viewer's already-visible ticket views, so a restricted ticket the person may not see
can never reach a row, a count, a page token, a log or a diff. Provisional wrappers: delete as stream A's host serves
these ops.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Mapping

from orch import canon
from orch.ops import views as p1_views
from orch.schema import SECTIONS_BY_TYPE
from orch.store import observe
from orch.store.render import HEADINGS, thaw

from orch_dashboard.errors import fail

_LOG = logging.getLogger("orch_dashboard.devhost")

STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
SNIP_BYTES = 1024
PAGE_TTL_S = 600
MAX_SNAPSHOTS = 32
SOURCE_ARGS = {"status", "type", "label", "assignee", "owner", "parent", "epic", "q", "updated_since", "needs_me"}
QUERY_ARGS = SOURCE_ARGS | {"sort", "order", "limit", "page"}
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
PERSON = re.compile(r"^p_[A-Za-z0-9]{1,64}$")
TAKES_ARGS = frozenset(
    {"ticket.query", "ticket.get", "needs", "diff", "artifact.get", "activity.list", "workspace.settings", "grant.list"}
)


def bad(message="invalid input"):
    return fail("invalid.input", message, exit=5)


def _list_of(value, name, *, maximum, item=str, allowed=None):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum or not all(type(x) is item for x in value):
        raise bad(name)
    if allowed is not None and not all(x in allowed for x in value):
        raise bad(name)
    return value


def validate_query(args):
    if set(args) - QUERY_ARGS:
        raise bad("unknown argument")
    out = dict(args)
    if "status" in out:
        _list_of(out["status"], "status", maximum=8, allowed=STATUSES)
    for name in ("type", "label"):
        if name in out:
            _list_of(out[name], name, maximum=16)
            if not all(1 <= len(x) <= 64 for x in out[name]):
                raise bad(name)
    for name in ("assignee", "owner"):
        if name in out and (not isinstance(out[name], str) or not PERSON.fullmatch(out[name])):
            raise bad(name)
    for name in ("parent", "epic"):
        if name in out and (not isinstance(out[name], str) or not KEY.fullmatch(out[name])):
            raise bad(name)
    if "q" in out and (not isinstance(out["q"], str) or not 1 <= len(out["q"]) <= 200):
        raise bad("q")
    if "updated_since" in out and (
        not isinstance(out["updated_since"], str)
        or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", out["updated_since"])
    ):
        raise bad("updated_since")
    if "needs_me" in out and type(out["needs_me"]) is not bool:
        raise bad("needs_me")
    if out.get("sort", "updated") not in ("updated", "priority", "key"):
        raise bad("sort")
    if out.get("order", "desc") not in ("asc", "desc"):
        raise bad("order")
    if "limit" in out and (type(out["limit"]) is not int or not 1 <= out["limit"] <= 200):
        raise bad("limit")
    if "page" in out and (not isinstance(out["page"], str) or not 1 <= len(out["page"]) <= 512):
        raise bad("page")
    return out


def can_see(v, person):
    vis = thaw(v.visibility)
    return vis == "workspace" or person in vis.get("restricted", [])


def _managers(ws, v):
    """Owners and maintainers who can see the ticket (F1 section 5.4.2)."""
    return sorted(p for p, m in ws.members.items() if m.role in ("owner", "maintainer") and can_see(v, p))


def snip(text):
    """The first 1 024 UTF-8 bytes, cut on a scalar boundary; nothing appended. Returns (text, cut)."""
    raw = str(text).encode()
    if len(raw) <= SNIP_BYTES:
        return str(text), False
    return raw[:SNIP_BYTES].decode(errors="ignore"), True


def _put_snip(out, name, value):
    if value is None:
        return
    text, cut = snip(value)
    out[name] = text
    if cut:
        out[f"{name}_cut"] = True


def _actor(a):
    keys = ("kind", "id", "for", "session", "device", "auth", "grant", "unattended")
    out = {k: a[k] for k in keys if k in a and a[k] is not None}
    out.setdefault("id", None)
    return out


# Per event type: the keys of LogItem.data (v1.4 table), copied from the stored event. Anything else is dropped.
DATA_KEYS = {
    "status.changed": ("from", "to", "reason"),
    "ticket.moved": ("from", "to", "reason"),
    "people.changed": ("role", "add", "remove"),
    "claim.released": ("session", "reason"),
    "task.started": ("task",),
    "task.reopened": ("task", "reason"),
    "task.skipped": ("task", "reason"),
    "task.blocked": ("task", "reason"),
    "gate.approved": ("gate", "gen"),
    "gate.invalidated": ("gate", "cause", "voided"),
    "branch.pushed": ("repo_name", "repo_id", "ref", "sha", "before_sha"),
    "artifact.added": ("name", "kind", "digest", "bytes", "ac", "task", "label", "addon", "replaces"),
    "artifact.replaced": ("name", "kind", "digest", "bytes", "ac", "task", "label", "addon", "replaces"),
    "edit.external": ("sections", "voided_gates", "normalised"),
    "restore": ("from_seq", "reason"),
    "invalid.acknowledged": ("invalid_seq",),
    "workspace.created": ("owner",),
    "member.added": ("person", "role"),
    "member.removed": ("person",),
    "role.changed": ("person", "role"),
    "device.added": ("person", "device"),
    "device.removed": ("person", "device"),
    "device.revoked": ("person", "device"),
    "grant.issued": ("grant", "person", "scope", "expires_at"),
    "grant.revoked": ("grant", "person"),
    "addon.granted": ("name", "version"),
    "addon.disabled": ("name",),
    "addon.purged": ("name",),
}
SNIP_FIELDS = {
    "ticket.closed": ("text",),
    "ticket.reopened": ("text",),
    "log.added": ("text",),
    "question.answered": ("text",),
    "gate.changes_requested": ("text",),
    "verdict.given": ("text",),
    "task.done": ("text",),
}


def log_data(e):
    """The closed `data` of one event: the table of section 0.8, nothing else (no sig, no hash, no secret)."""
    t = e["type"]
    out = {}
    for k in DATA_KEYS.get(t, ()):
        if e.get(k) is not None:
            out[k] = e[k]
    for k in SNIP_FIELDS.get(t, ()):
        _put_snip(out, k, e.get(k))
    if t == "ticket.created":
        out.update(
            {"ticket_type": e.get("ticket_type", e.get("kind")), "title": e.get("title"), "owner": e.get("owner")}
        )
    elif t == "ticket.updated":
        out["paths"] = sorted(e.get("set", {}))
        out["sections"] = sorted(
            e.get("sections", {}) if isinstance(e.get("sections"), dict) else e.get("sections", [])
        )
    elif t == "ticket.closed":
        out["resolution"] = e.get("resolution", "other")
        if e.get("duplicate_of"):
            out["duplicate_of"] = e["duplicate_of"]
    elif t == "visibility.changed":
        out["restricted"] = e.get("visibility", "workspace") != "workspace"
    elif t == "policy.changed" or t == "settings.changed":
        out["gates" if t == "policy.changed" else "keys"] = sorted(e.get("set", e.get("policies", {})) or [])
    elif t == "claim.taken" and e.get("takeover"):
        out["takeover"] = {k: e["takeover"].get(k) for k in ("from_session", "reason")}
    elif t == "task.done":
        out["task"] = e.get("task")
        if isinstance(e.get("receipt"), dict):
            r = e["receipt"]
            out["receipt"] = {
                "cmd": r.get("cmd"),
                "exit": r.get("exit"),
                "ms": r.get("duration_ms", r.get("ms")),
                "repo": r.get("repo"),
                "commit": r.get("commit"),
                **({"attested_by": r["attested_by"]} if r.get("attested_by") else {}),
            }
        if e.get("log"):
            out["log"] = e["log"]
    elif t == "question.asked":
        q = e.get("question") or {}
        out.update({"question": q.get("id"), "blocking": q.get("blocking"), "to": q.get("to")})
    elif t == "question.answered":
        out["question"] = e.get("question")
        if e.get("option") is not None:
            out["option"] = e["option"]
    elif t == "gate.changes_requested":
        out.update({"gate": e.get("gate"), "gen": e.get("gen")})
    elif t == "verdict.given":
        out.update({"outcome": e.get("outcome", e.get("result")), "gen": e.get("gen"), "source_sha": _sources(e)})
    return {k: v for k, v in out.items() if v is not None or k in ("duplicate_of", "before_sha")}


def _sources(e):
    raw = e.get("source_sha") or []
    return [{"repo": s.get("repo"), "ref": s.get("ref"), "sha": s.get("sha")} for s in raw if isinstance(s, Mapping)]


def closed_of(events, with_text=False):
    """`Closed` from the latest ticket.closed since the last ticket.reopened, or None."""
    out = None
    for e in events:
        if e["type"] == "ticket.reopened":
            out = None
        elif e["type"] == "ticket.closed":
            out = {
                "resolution": e.get("resolution", "other"),
                "duplicate_of": e.get("duplicate_of"),
                "at": e["at"],
                "by": e["actor"].get("id"),
            }
            if with_text:
                out["text"] = e.get("text")
    return out


def claim_of(events, v, full=False):
    """The claim as v1.4 sends it. `agent` is the actor id of the claim.taken that started it."""
    if not v.claim:
        return None
    taken = next(
        (e for e in reversed(events) if e["type"] == "claim.taken" and e["actor"].get("session") == v.claim.session),
        None,
    )
    out = {
        "session": v.claim.session,
        "agent": (taken or {}).get("actor", {}).get("id") or "",
        "for": v.claim.for_person,
        "since": (taken or {}).get("at"),
        "live": v.claim.live,
    }
    if taken is None:
        out.pop("since")
    if full:
        mine = [e for e in events if e["actor"].get("session") == v.claim.session]
        out.update(
            {
                "grant": v.claim.grant,
                "last_at": mine[-1]["at"] if mine else out.get("since"),
                "lapsed": v.claim.lapsed,
                "takeovers": [
                    {
                        "at": e["at"],
                        "session": e["actor"].get("session"),
                        "from_session": (e.get("takeover") or {}).get("from_session"),
                        "reason": (e.get("takeover") or {}).get("reason"),
                    }
                    for e in events
                    if e["type"] == "claim.taken" and e.get("takeover")
                ][-20:],
            }
        )
    return out


def summary_row(host, store, ws, v, person, visible):
    events = store.events(v.uid)
    updated = events[-1]["at"] if events else None
    epic = None
    parent = v.fields.get("parent")
    if parent:
        other = visible.get(parent)  # an invisible parent reveals nothing, not even its type
        epic = parent if other is not None and other.type == "epic" else None
    row = {
        "key": v.key,
        "uid": v.uid,
        "title": v.title,
        "type": v.type,
        "status": v.status,
        "priority": v.fields["priority"],
        "size": v.fields["size"],
        "labels": list(v.fields["labels"]),
        "assignees": list(v.people["assignees"]),
        "owner": v.owner,
        "parent": parent,
        "epic": epic,
        "claim": {"session": v.claim.session, "agent": None} if v.claim else None,
        "gates": {g: "n/a" if not x.applies else "approved" if x.approved else "open" for g, x in v.gates.items()},
        "needs": [n.kind for n in v.needs if person in n.who],
        "updated_at": updated,
        "head_seq": store.head_seq(v.uid),
    }
    if host.ext:
        row["claim"] = claim_of(events, v)
        row["restricted"] = thaw(v.visibility) != "workspace"
        row["tasks"] = {
            "done": sum(t.state == "done" for t in v.tasks),
            "skipped": sum(t.state == "skipped" for t in v.tasks),
            "total": len(v.tasks),
        }
        row["criteria"] = {"done": sum(bool(a.evidence) for a in v.acceptance), "total": len(v.acceptance)}
        row["blocking_questions"] = sum(1 for q in v.questions if not q.answered and q.blocking)
    if v.status == "closed":
        # The closed object is v1.4 base (section 0.8.4), not read_ext.
        row["closed"] = closed_of(events)
    return row


def _key_number(key):
    prefix, _, number = key.rpartition("-")
    return prefix, int(number)


def _matches(store, row, view, args, person, texts):
    if "status" in args and row["status"] not in args["status"]:
        return False
    if "status" not in args and row["status"] == "closed":
        return False  # closed tickets are listed only when asked for (section 0.8.4)
    if "type" in args and row["type"] not in args["type"]:
        return False
    if "label" in args and not set(args["label"]) & set(row["labels"]):
        return False
    if "assignee" in args and args["assignee"] not in row["assignees"]:
        return False
    if "owner" in args and args["owner"] != row["owner"]:
        return False
    if "parent" in args and args["parent"] != row["parent"]:
        return False
    if "epic" in args and args["epic"] != row["epic"]:
        return False
    if "updated_since" in args and (row["updated_at"] or "") < args["updated_since"]:
        return False
    if args.get("needs_me") and not row["needs"]:
        return False
    if "q" in args:
        needle = args["q"].casefold()
        haystack = [row["key"], row["title"], *texts(view.uid).values()]
        if not any(needle in x.casefold() for x in haystack):
            return False
    return True


class Pages:
    """Snapshots behind page tokens. A token is bound to the principal that got it and to the first page's other
    arguments; a token of anyone else is `page.expired` (v1.4 section 0.8)."""

    def __init__(self):
        self.snapshots = {}
        self._key = secrets.token_bytes(32)  # per process: an offset in a token is authenticated, not taken on trust

    def token(self, sid, offset):
        mac = hmac.new(self._key, f"{sid}.{offset}".encode(), "sha256").hexdigest()[:24]
        return f"{sid}.{offset}.{mac}"

    def open(self, rows, as_of, sub_cursor, args_key, person, then):
        now = time.monotonic()
        self.snapshots = {k: v for k, v in self.snapshots.items() if v["expires"] > now}
        if len(self.snapshots) >= MAX_SNAPSHOTS:
            self.snapshots.pop(min(self.snapshots, key=lambda k: self.snapshots[k]["expires"]))
        sid = secrets.token_urlsafe(12)
        self.snapshots[sid] = {
            "rows": rows,
            "as_of": as_of,
            "cursor": sub_cursor,
            "key": args_key,
            "person": person,
            "then": then,
            "expires": now + PAGE_TTL_S,
        }
        return sid

    def read(self, token, args_key, person):
        sid, _, rest = token.partition(".")
        offset, _, mac = rest.partition(".")
        snap = self.snapshots.get(sid)
        if (
            snap is None
            or snap["expires"] <= time.monotonic()
            or not offset.isdecimal()
            or not hmac.compare_digest(self.token(sid, offset), f"{sid}.{offset}.{mac}")
            or snap["person"] != person
        ):
            raise fail("page.expired", exit=9)
        if snap["key"] != args_key:
            raise bad("page")
        return sid, snap, int(offset)


def _page(host, snap, start, limit, visible_now, uid_of):
    """Entries from `start`: visible at the snapshot and now, `limit` of them, or fewer when the scan budget runs out.
    Returns (entries, next position or None). `next` is None only when nothing visible is left."""
    out, pos, scanned = [], start, 0
    rows = snap["rows"]
    budget = host.scan_budget
    while pos < len(rows) and len(out) < limit and (budget is None or scanned < budget):
        uid = uid_of(rows[pos])
        pos += 1
        scanned += 1
        if uid is None or (uid in visible_now and uid in snap["then"]):
            out.append(rows[pos - 1])
    left = any(uid_of(r) is None or (uid_of(r) in visible_now and uid_of(r) in snap["then"]) for r in rows[pos:])
    return out, (pos if left else None)


def query(host, store, views, args, person):
    args = validate_query(args)
    limit = args.get("limit", 50)
    source = {k: v for k, v in args.items() if k in SOURCE_ARGS}
    sort, order = args.get("sort", "updated"), args.get("order", "desc")
    args_key = repr(sorted((k, repr(v)) for k, v in args.items() if k not in ("page", "limit")))
    now = {v.uid for v in views}
    if "page" in args:
        sid, snap, start = host.pages.read(args["page"], args_key, person)
    else:
        ws = store.state.workspace
        texts = store.body_sections
        visible = {v.key: v for v in views}
        for ref_name in ("parent", "epic"):
            if ref_name in source and source[ref_name] not in visible:
                raise fail("not_found", exit=2)
        rows = []
        for v in views:
            row = summary_row(host, store, ws, v, person, visible)
            if _matches(store, row, v, source, person, texts):
                rows.append(row)
        rows.sort(key=lambda r: _key_number(r["key"]))  # ties break by key ascending
        if sort == "priority":
            rows.sort(key=lambda r: PRIORITY_RANK.get(r["priority"], 9), reverse=order == "asc")
        elif sort == "updated":
            rows.sort(key=lambda r: r["updated_at"] or "", reverse=order == "desc")
        else:
            rows.sort(key=lambda r: _key_number(r["key"]), reverse=order == "desc")
        sid = host.pages.open(rows, store.state.now, host.cursor, args_key, person, set(now))
        snap = host.pages.snapshots[sid]
        start = 0
    page, nxt = _page(host, snap, start, limit, now, lambda r: r["uid"])
    return {
        "rows": page,
        "next_page": host.pages.token(sid, nxt) if nxt is not None else None,
        "total": len(snap["rows"]),
        "sub_cursor": snap["cursor"],
        "as_of": snap["as_of"],
    }


def _since(events, kind, ref):
    """The `at` of the event that made a need hold (approximation: the question's ask, else the ticket's last event)."""
    if kind == "question":
        asked = [e for e in events if e["type"] == "question.asked" and (e.get("question") or {}).get("id") == ref]
        return asked[-1]["at"] if asked else None
    return events[-1]["at"] if events else None


def needs(host, store, views, ws, person, args):
    if set(args) - {"person"}:
        raise bad("unknown argument")
    if "person" in args and args["person"] != person:
        raise fail("op.not_allowed", "another person's needs", exit=3)
    items = []
    for v in views:
        defs = {q["id"]: q for q in v.fields["questions"]}
        events = store.events(v.uid) if host.ext else []
        for n in v.needs:
            kind, who, ref, cause = n.kind, [p for p in n.who if can_see(v, p)], n.ref, None
            if kind == "approve" and not who:  # eligible people exist but none can see the ticket
                kind, who, cause = "no_eligible", _managers(ws, v), "no_visible_person"
            elif kind == "no_eligible":
                cause = "no_token"
                who = _managers(ws, v)
            elif kind == "question" and not who:
                who = _managers(ws, v)
            if person not in who:
                continue
            item = {"kind": kind, "key": v.key, "uid": v.uid, "ref": ref, "detail": n.detail, "who": who}
            if host.ext:
                item["status"] = v.status
                since = _since(events, kind, ref)
                if since:
                    item["since"] = since
                if kind == "question":
                    d = defs.get(ref, {})
                    _put_snip(item, "text", d.get("text", ""))
                    payload = {
                        "question": ref,
                        "blocking": n.detail == "blocking",
                        "to": d.get("to", ""),
                        "options": [
                            {k: o[k] for k in ("key", "label", "cost") if k in o} for o in d.get("options") or []
                        ],
                    }
                    for k in ("recommended", "why"):
                        if d.get(k):
                            payload[k] = d[k]
                elif kind == "approve":
                    g = v.gates[ref]
                    payload = {"gate": ref, "gen": g.gen, "count": g.needed, "have": len(g.counting)}
                elif kind == "no_eligible":
                    payload = {"gate": ref, "cause": cause}
                else:
                    payload = {"addon": ref.partition(".")[0], "rule": ref}
                item["payload"] = payload
            items.append(item)
    for n in ws.needs:
        if person in n.who:
            item = {"kind": n.kind, "key": None, "uid": None, "ref": n.ref, "detail": n.detail, "who": list(n.who)}
            if host.ext:
                item["payload"] = {"scope": "workspace"}
            items.append(item)
    return {"items": items, "sub_cursor": host.cursor}


def _text(value, limit=200):
    """Host text, escaped: control and format characters become visible escapes, then cut."""
    out = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in str(value))
    return out[:limit]


def _find(views, ref):
    if not isinstance(ref, str) or not 1 <= len(ref) <= 64:
        raise bad("ref")
    wanted = ref.upper() if KEY.fullmatch(ref) else ref
    for v in views:
        if v.key == wanted or v.uid == ref:
            return v
    raise fail("not_found", exit=2)  # a ticket the viewer cannot see is the same as one that does not exist


def _log_items(events, lines, ext):
    log = []
    for e, line in zip(events, lines, strict=False):
        item = {
            "seq": e["seq"],
            "type": e["type"],
            "at": e["at"],
            "actor": _actor(e["actor"]),
            "summary": _text(line.partition(": ")[2]),
        }
        if ext:
            item["data"] = log_data(e)
        log.append(item)
    return log


def _gate_ext(ws, v, g, x, events):
    ids = {e["id"]: e for e in events if "id" in e}
    decisions = []
    for d in x.decisions:
        e = ids.get(d.id, {})
        item = {
            "id": d.id,
            "kind": {"approve": "approved", "changes": "changes_requested"}.get(d.kind, d.kind),
            "person": d.person,
            "at": e.get("at"),
            "seq": e.get("seq"),
            "gen": d.gen,
            "counting": d.counting,
            "voided": d.voided,
        }
        if e.get("actor", {}).get("auth"):
            item["auth"] = e["actor"]["auth"]
        if d.source_sha:
            item["source_sha"] = _sources({"source_sha": list(d.source_sha)})
        if e.get("text"):
            _put_snip(item, "text", e["text"])
        decisions.append(item)
    return {
        "applies": x.applies,
        "waiting": x.waiting,
        "blocked": x.blocked,
        "count": x.needed,
        "approved_by": [p for p in x.counting if can_see(v, p)],
        "eligible": [p for p in x.eligible if can_see(v, p)],
        "policy": thaw(x.policy),
        "missing": list(x.missing),
        "decisions": decisions[-100:],
    }


def get(host, store, views, args, person):
    if set(args) - {"ref", "log_since", "log_limit"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    ws = store.state.workspace
    since, limit = args.get("log_since", 0), args.get("log_limit", 100)
    if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise bad("log")
    texts = store.body_sections(v.uid)
    sections = tuple(h for h in HEADINGS if h in SECTIONS_BY_TYPE[v.type])
    defs = {q["id"]: q for q in v.fields["questions"]}
    doc = {
        "uid": v.uid,
        "key": v.key,
        "title": v.title,
        "type": v.type,
        "status": v.status,
        "priority": v.fields["priority"],
        "size": v.fields["size"],
        "labels": list(v.fields["labels"]),
        "due": v.fields["due"],
        "parent": v.fields["parent"],
        "blocked_by": list(v.fields["blocked_by"]),
        "owner": v.owner,
        "people": {r: list(v.people[r]) for r in ("assignees", "reviewers", "watchers")},
        "visibility": thaw(v.visibility),
        "links": thaw(v.fields["links"]),
        "claim": {"session": v.claim.session, "live": v.claim.live} if v.claim else None,
        "waiting": v.waiting,
        "acceptance": [{"id": a.id, "text": a.text, "evidence": list(a.evidence)} for a in v.acceptance],
        "tasks": [{"id": t.id, "text": t.text, "state": t.state, "proves": list(t.proves)} for t in v.tasks],
        "questions": [
            {
                "id": q.id,
                "to": q.to,
                "blocking": q.blocking,
                "answered": q.answered,
                "text": defs.get(q.id, {}).get("text", ""),
                **({"options": [o["key"] for o in defs[q.id]["options"]]} if defs.get(q.id, {}).get("options") else {}),
            }
            for q in v.questions
        ],
        "current_state": texts.get("current_state", ""),
        "sections": {s: texts.get(s, "") for s in sections},
    }
    all_events = store.events(v.uid)
    # as core's `doc.head` (v1.7): the seq and the head hash of the log's last event (the edit sequence chains on it)
    doc["head"] = {"seq": all_events[-1]["seq"], "head": canon.event_head(all_events[-1])}
    events = store.events(v.uid, after=since, limit=limit + 1)
    more = len(events) > limit
    events = events[:limit]
    tail = p1_views.event_lines(events, since, {"acceptance": {}, "tasks": {}})
    log = _log_items(events, tail, host.ext)
    answered = {}
    asked = {}
    for e in all_events:
        if e["type"] == "question.answered":
            answered[e["question"]] = e
        elif e["type"] == "question.asked":
            asked[e["question"]["id"]] = e
    questions = []
    for q in v.questions:
        answer = q.answer or {}
        item = {
            "id": q.id,
            "hash": q.hash,
            "state": "answered" if q.answered else "open",
            "to": [p for p in q.addressees if can_see(v, p)] or _managers(ws, v),
            "blocking": q.blocking,
            "answered_by": answer.get("by") if q.answered else None,
            "answered_seq": answered.get(q.id, {}).get("seq") if q.answered else None,
        }
        if host.ext:
            d = defs.get(q.id, {})
            _put_snip(item, "text", d.get("text"))
            item["to_ref"] = q.to
            if d.get("options"):
                item["options"] = [{k: o[k] for k in ("key", "label", "cost") if k in o} for o in d["options"]]
            for k in ("why", "recommended"):
                if d.get(k):
                    item[k] = d[k]
            if q.id in asked:
                ev = asked[q.id]
                item.update({"asked_at": ev["at"], "asked_by": _actor(ev["actor"]), "asked_seq": ev["seq"]})
            if q.answered and q.id in answered:
                ev = answered[q.id]
                ans = {"by": ev["actor"].get("id"), "at": ev["at"], "seq": ev["seq"], "option": ev.get("option")}
                ans["text"] = snip(ev["text"])[0] if ev.get("text") else None
                item["answer"] = ans
        questions.append(item)
    by_name = {a.name: a for a in v.artifacts}
    criteria = []
    for a in v.acceptance:
        evidence = []
        for ref in a.evidence:
            kind, _, name = ref.partition(":")
            art = by_name.get(name) if kind == "artifact" else None
            if art is not None and art.digest:
                evidence.append(
                    {
                        "artifact": art.name,
                        "digest": art.digest,
                        "kind": art.kind,
                        "attested_by": "person" if art.by.startswith("p_") else "agent",
                    }
                )
        crit = {"id": a.id, "text": a.text, "evidence": evidence}
        if host.ext:
            crit["covered"] = bool(a.evidence)
            crit["tasks"] = [{"id": t.id, "evidence": t.state == "done"} for t in v.tasks if a.id in t.proves]
        criteria.append(crit)
    gates = {}
    for g, x in v.gates.items():
        gate = {
            "state": "n/a" if not x.applies else "approved" if x.approved else "open",
            "gen": x.gen,
            "hash12": p1_views.short_gate_hash(x.hash) if x.hash else None,
        }
        if host.ext:
            gate.update(_gate_ext(ws, v, g, x, all_events))
        gates[g] = gate
    out = {
        "doc": doc,
        "log": log,
        "log_more": more,
        "questions": questions,
        "criteria": criteria,
        "gates": gates,
        "sub_cursor": host.cursor,
    }
    if not host.ext:
        return out
    closed = closed_of(all_events, with_text=True)
    out.update({"status": v.status, "settled": v.status in ("done", "closed")})
    if v.status == "closed" and closed:
        text = closed.pop("text", None)
        out["closed"] = {**closed, "text": snip(text)[0] if text else None}
    done = {}
    for e in all_events:
        if e["type"] == "task.done":
            done[e["task"]] = e
        elif e["type"] == "task.reopened":
            done.pop(e["task"], None)
    tasks = []
    for t in v.tasks:
        e = done.get(t.id)
        last = next((x for x in reversed(all_events) if x["type"].startswith("task.") and x.get("task") == t.id), None)
        receipt = None
        if e and isinstance(e.get("receipt"), dict):
            r = e["receipt"]
            receipt = {
                "cmd": r.get("cmd"),
                "exit": r.get("exit"),
                "ms": r.get("duration_ms", 0),
                "repo": r.get("repo"),
                "commit": r.get("commit"),
                **({"attested_by": r["attested_by"]} if r.get("attested_by") else {}),
                "log": e.get("log"),
                "seq": e["seq"],
                "at": e["at"],
                "evidence": r.get("exit") == 0,
            }
        tasks.append(
            {
                "id": t.id,
                "text": t.text,
                "state": t.state if t.state in ("open", "done", "skipped", "blocked") else "open",
                "proves": list(t.proves),
                "leased_by": t.leased_by,
                "by": _actor(last["actor"]) if last else None,
                "at": last["at"] if last else None,
                "reason": last.get("reason") if last else None,
                "receipt": receipt,
            }
        )
    out["tasks"] = tasks
    verdict = next((e for e in reversed(all_events) if e["type"] == "verdict.given"), None)
    gate = v.gates.get("verify")
    out["verdict"] = None
    if verdict:
        mine = next((d for d in (gate.decisions if gate else ()) if d.id == verdict.get("id")), None)
        item = {
            "outcome": verdict.get("outcome"),
            "person": verdict["actor"].get("id"),
            "at": verdict["at"],
            "seq": verdict["seq"],
            "gen": verdict.get("gen"),
            "text": None,
            "source_sha": _sources(verdict),
            "current": bool(gate and mine and mine.gen == gate.gen and mine.counting),
            "counting": bool(mine and mine.counting),
            "voided": bool(mine and mine.voided),
        }
        if verdict.get("text"):
            _put_snip(item, "text", verdict["text"])
        out["verdict"] = item
    added = {}
    replaced = {}
    for e in all_events:
        if e["type"] == "artifact.added":
            added[e["name"]] = e
        elif e["type"] == "artifact.replaced":
            added[e["name"]] = e
            replaced[e["name"]] = replaced.get(e["name"], 0) + 1
    arts = []
    for a in sorted(v.artifacts, key=lambda a: a.name)[:1000]:
        e = added.get(a.name, {})
        item = {
            "name": a.name,
            "kind": a.kind,
            "digest": a.digest,
            "bytes": e.get("bytes"),
            "ac": a.ac,
            "task": a.task,
            "label": e.get("label"),
            "evidence": a.evidence,
            "by": _actor(e["actor"]) if e else {"kind": "person", "id": a.by},
            "at": e.get("at"),
            "seq": e.get("seq"),
            "replaced": replaced.get(a.name, 0),
        }
        arts.append(item)
    out["artifacts"] = arts
    out["claim"] = claim_of(all_events, v, full=True)
    pushed = [e for e in all_events if e["type"] == "branch.pushed"]
    out["source"] = [
        {
            "name": next((p["repo_name"] for p in pushed if p["repo_id"] == s["repo"]), s["repo"]),
            **_sources({"source_sha": [s]})[0],
        }
        for s in v.source_list
    ]
    out["pushes"] = [
        {
            "seq": p["seq"],
            "at": p["at"],
            "repo_name": p["repo_name"],
            "repo_id": p["repo_id"],
            "ref": p["ref"],
            "sha": p["sha"],
            "before": p.get("before"),
        }
        for p in pushed[-50:]
    ]
    out["sections"] = {}
    return out


DIFF_UNAVAILABLE = "diff unavailable"
_PLAIN_HUNK = re.compile(r"(?m)^(@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@).*$")


def diff(store, views, args):
    """The Changes tab's diff, read with orch's own reader in-process (``orch.gitread.local``, #565): no git process,
    so nothing in the repository's config can run and there is no gate-then-spawn window to race. The commits are
    compared tree to tree (the index plays no part); renames show as a deletion and an addition. The base is the merge
    base with ``main`` or ``master``, else the head's first parent (as before). Whatever the reader cannot produce (a
    refused repository, a missing object, a limit) is "diff unavailable" (409, #561), never an empty diff."""
    from orch.gitread.errors import Unavailable
    from orch.gitread.local import READER

    if set(args) - {"ref", "repo", "context", "max_bytes"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    context, limit = args.get("context", 3), args.get("max_bytes", 262144)
    if type(context) is not int or not 0 <= context <= 10 or type(limit) is not int or not 1024 <= limit <= 1048576:
        raise bad("diff")
    links = v.fields["links"]
    wanted = list(links["repos"])
    if "repo" in args:
        if args["repo"] not in wanted:  # only a repo the ticket links
            raise bad("repo")
        wanted = [args["repo"]]
    sources, files, parts, truncated = [], [], [], False
    for name in wanted:
        branch = links["branches"].get(name)
        path = observe.repo_path(store.layout, store.state.workspace.repos, name)
        if not branch or path is None or not path.is_dir():
            raise fail("observe.unavailable", f"{name}: no working copy or branch", exit=3)
        identity = observe.repo_identity(path, name)
        recorded = next((e for e in v.source_list if e["repo"] == identity), None)
        if recorded is None:
            raise fail("observe.unavailable", f"{name}: the host has not observed a branch head", exit=3)
        try:
            got = READER.diff(path, branch=branch, default_branch=None, context=context, max_bytes=limit)
        except Unavailable as exc:
            _LOG.warning("%s for a linked repo (the reader refused or failed)", DIFF_UNAVAILABLE)
            raise fail("observe.unavailable", f"{name}: {DIFF_UNAVAILABLE}: {exc.reason}"[:300], exit=3) from None
        # Bound to D58: the head shown is the source_sha a verdict would sign. A branch that moved since the host
        # observed it is refused, never shown as if it were that commit.
        if got["head_sha"] != recorded["sha"]:
            raise fail("observe.unavailable", f"{name}: the branch moved since it was observed", exit=3)
        if got["base_source"] == "first_parent":  # no default branch, or no common ancestor: never silently (#546)
            _LOG.warning("merge base unavailable, using the parent commit (git.no_merge_base)")
        sources.append({"repo": name, "branch": branch, "base_sha": got["base_sha"], "head_sha": got["head_sha"]})
        for f in got["files"]:
            files.append(
                {
                    "path": f["path"],
                    "old_path": f.get("old_path"),
                    "status": f["status"],
                    "add": f["add"],
                    "del": f["del"],
                    "binary": f["binary"],
                    "repo": name,
                }
            )
        # whole hunk lines only, as since #561 (DECISIONS-LOG): no function text after a hunk header's second @@
        parts.append(_PLAIN_HUNK.sub(r"\1", base64.b64decode(got["patch"]).decode("utf-8", errors="replace")))
        truncated = truncated or bool(got["truncated"])
    unified = "".join(parts)
    if len(unified.encode()) > limit:
        cut = unified.encode()[:limit].decode(errors="ignore")
        unified = cut[: cut.rfind("\n") + 1] if "\n" in cut else ""
        truncated = True
    return {"sources": sources, "files": files, "unified": unified, "truncated": truncated}


def artifact(store, views, args):
    if set(args) - {"ref", "name", "offset", "length"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    name, offset, length = args.get("name"), args.get("offset"), args.get("length", 786432)
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
        or type(offset) is not int
        or offset < 0
        or type(length) is not int
        or not 1 <= length <= 786432
    ):
        raise bad("artifact")
    art = next((a for a in v.artifacts if a.name == name and a.digest), None)
    if art is None:
        raise fail("not_found", exit=2)
    path = store.root / "tickets" / v.uid / "artifacts" / name  # name is validated: never joined from client text
    try:
        data = path.read_bytes()
    except OSError:
        raise fail("not_found", exit=2) from None
    if "sha256:" + hashlib.sha256(data).hexdigest() != art.digest and hashlib.sha256(data).hexdigest() != art.digest:
        raise fail("artifact.mismatch", exit=3)
    if offset > len(data):
        raise bad("offset")
    chunk = data[offset : offset + length]
    return {
        "b64": base64.b64encode(chunk).decode(),
        "size": len(data),
        "digest": art.digest,
        "offset": offset,
        "last": offset + len(chunk) >= len(data),
    }


WORKSPACE_ALL = {
    "workspace.created", "member.added", "member.removed", "role.changed", "device.added", "device.removed",
    "device.revoked", "policy.changed", "settings.changed", "addon.granted", "addon.disabled", "addon.purged",
    "restore", "invalid.acknowledged",
}  # fmt: skip
WORKSPACE_OWNER = {"grant.issued", "grant.revoked"}
ACTIVITY_ARGS = {"scope", "ticket", "types", "actor_kind", "person", "since", "until", "order", "limit", "page"}
TYPE = re.compile(r"^[a-z]+(\.[a-z_]+)*$")


def _merged(store):
    """Every verified event of every log in the merged order (the workspace log first on a tie)."""
    events = [("workspace", e) for e in store._read_events("workspace", 1, store.head_seq("workspace"))]
    events += [(uid, e) for uid in store.state.tickets for e in store.events(uid)]
    events.sort(
        key=lambda pair: (
            pair[1]["seq"] if pair[0] == "workspace" else pair[1]["ws_seq"],
            0 if pair[0] == "workspace" else 1,
            pair[1]["at"],
            pair[0],
            pair[1]["seq"],
        )
    )
    return events


def activity(host, store, views, ws, person, args):
    if set(args) - ACTIVITY_ARGS:
        raise bad("unknown argument")
    limit = args.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise bad("limit")
    scope, order = args.get("scope", "all"), args.get("order", "desc")
    if scope not in ("all", "tickets", "workspace") or order not in ("asc", "desc"):
        raise bad("scope")
    if "ticket" in args and scope == "workspace":
        raise bad("ticket")
    types = args.get("types")
    if types is not None and (
        not isinstance(types, list)
        or not 1 <= len(types) <= 16
        or not all(isinstance(t, str) and TYPE.fullmatch(t) for t in types)
    ):
        raise bad("types")
    if "since" in args and "until" in args and args["since"] > args["until"]:
        raise bad("since")
    args_key = repr(sorted((k, repr(v)) for k, v in args.items() if k not in ("page", "limit")))
    now = {v.uid for v in views}
    if "page" in args:
        sid, snap, start = host.pages.read(args["page"], args_key, person)
    else:
        only = None
        if "ticket" in args:
            only = _find(views, args["ticket"]).uid
        keys = {v.uid: v for v in views}
        member = ws.members.get(person)
        owner = bool(member and member.role == "owner")
        items = []
        for pos, (log, e) in enumerate(_merged(store), 1):
            if log == "workspace":
                if scope == "tickets" or only:
                    continue
                if e["type"] in WORKSPACE_OWNER and not (owner or e.get("person") == person):
                    continue
                if e["type"] not in WORKSPACE_ALL | WORKSPACE_OWNER:
                    continue
                uid, key, title = None, None, None
                data = log_data(e)
            else:
                if scope == "workspace" or log not in keys or (only and log != only):
                    continue
                uid, key, title = log, keys[log].key, keys[log].title
                data = log_data(e)
            if types and e["type"] not in types:
                continue
            if "since" in args and e["at"] < args["since"] or "until" in args and e["at"] >= args["until"]:
                continue
            kinds = args.get("actor_kind")
            if kinds and e["actor"]["kind"] not in kinds:
                continue
            if "person" in args and args["person"] not in (e["actor"].get("id"), e["actor"].get("for")):
                continue
            items.append(
                {
                    "id": f"{pos}.e",
                    "jpos": pos,
                    "log": log,
                    "key": key,
                    "uid": uid,
                    "title": title,
                    "seq": e["seq"],
                    "ws_seq": e.get("ws_seq", e["seq"]),
                    "type": e["type"],
                    "at": e["at"],
                    "actor": _actor(e["actor"]),
                    "summary": _text(e.get("summary") or e["type"]),
                    "data": data,
                }
            )
        items.sort(key=lambda i: i["jpos"], reverse=order == "desc")
        sid = host.pages.open(items, store.state.now, host.cursor, args_key, person, set(now))
        snap = host.pages.snapshots[sid]
        start = 0
    page, nxt = _page(host, snap, start, limit, now, lambda r: r["uid"])
    return {
        "items": page,
        "next_page": host.pages.token(sid, nxt) if nxt is not None else None,
        "sub_cursor": snap["cursor"],
        "as_of": snap["as_of"],
    }
