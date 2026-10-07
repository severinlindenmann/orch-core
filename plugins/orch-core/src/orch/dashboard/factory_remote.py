"""The text a person approves on a paired device before the host signs an AI Factory decision for them (R13).

Each builder reads the host's LIVE state and returns the assertion subject {"kind", "shown", "digest"} that the bridge
host turns into a challenge, or None when it cannot: the request is stale (the sha or hash the form carries is not the
one the workspace has now), unknown, already answered, ambiguous, or too long to be shown in full. None means no
challenge is issued and nothing runs. The form's own `sha` / `seen` is the very value the ledger write checks again at
run time (permit_grant / approve / verdict `expected_*`), so the assertion binds the exact thing shown and the write
refuses anything that changed in between.

One parse: the builders read ONLY the urlencoded body, the way the route's Form fields do (never the query), take a
field only when it appears exactly once (the route would take another occurrence than a first-wins reader), and
interpret every field the way the route does, so what is shown is what the route will do. Every object is read once
and its hash and its shown text come from that one read. A digest is never empty.

Nothing here writes: the signed ledger writes stay the dashboard's own route handlers, called after the assertion."""
from __future__ import annotations

import hashlib

MAX_SHOWN = 4000  # a longer text cannot be shown in full, so it cannot be approved from a device
_ON = ("1", "on", "true")  # the values the approve route reads as a checked box


def _hex(h: str) -> str:
    h = h.removeprefix("sha256:")
    return h if len(h) == 64 and all(c in "0123456789abcdef" for c in h) else ""


def _one(params, key, default="") -> str:
    """The field's value when it occurs exactly once; `default` when absent; "\\0" (matches nothing) when it
    occurs more than once, so an ambiguous form never equals a live hash."""
    v = (params or {}).get(key)
    if not v:
        return default
    return v[0] if len(v) == 1 and isinstance(v[0], str) else "\0"


def _all(params, key) -> list[str]:
    return [x for x in (params or {}).get(key, ()) if isinstance(x, str)]


def _bound(**fields) -> str:
    """The digest of every field that changes what the action does, built once from the values that are shown."""
    from orch.core.canonical import canonical_json
    return hashlib.sha256(canonical_json(fields)).hexdigest()


def _subject(kind: str, shown: str, digest: str) -> dict | None:
    if len(shown) > MAX_SHOWN or not _hex(digest) or digest != _hex(digest):
        return None
    return {"kind": kind, "shown": shown, "digest": digest}


def subject(ws, kind: str, route_path: str, pp: dict, params, method: str, target: str, body: bytes) -> dict | None:
    try:
        if kind != "action" and (params is None or any(len(v) > 1 for k, v in params.items() if k != "acs")):
            return None  # a field twice: the route may read another occurrence than the one shown
        return _BUILD[kind](ws, route_path, pp, params, method, target, body)
    except Exception:  # noqa: BLE001 - no subject, no challenge
        return None


def _permission(ws, route_path, pp, params, method, target, body):
    from orch.core import permits
    rid, sha, scope = str(pp.get("rid", "")).upper(), _one(params, "sha"), _one(params, "scope", "once")
    r = permits.requests(ws).get(rid)
    if r is None or scope not in permits.SCOPES or not sha or sha != r["sha"] or (r["id"], r["sha"]) in permits.decisions(ws):
        return None
    if permits.never_grantable(ws, r["command"]):
        return None
    what = "for the whole epic" if scope == "epic" else "once"
    return _subject("permission", f"Allow {what} request {r['id']} on epic {r['epic']}: {permits.shown(r['command'])}",
                    _bound(kind="permission", request=r["id"], epic=str(r["epic"]), ticket=str(r["ticket"]),
                           scope=scope, sha=r["sha"]))


def _ticket(ws, ref):
    from orch.core import store
    return store.read_ticket(store.resolve(ws, ref).path)


def _charter(ws, route_path, pp, params, method, target, body):
    from orch.core import epics, permits
    t = _ticket(ws, pp.get("ref"))
    seen = _one(params, "seen")
    if not epics.is_epic(t.meta) or not seen or _one(params, "gate") != "requirements":
        return None
    kids = epics.open_children(ws, t)  # read once: hashed and counted from the same objects
    if seen != epics.charter(ws, t, None, tickets=kids)["content_hash"]:
        return None
    # exactly the route's reading: factory wins over delegate; anything else is an ordinary approval
    if _one(params, "factory") in _ON:
        limits, head = epics.normalize_delegate({"factory": True}), "Start the AI Factory on"
    elif _one(params, "delegate") in _ON:
        limits = epics.normalize_delegate({"max_children": _one(params, "max_children").strip() or None,
                                           "max_size": _one(params, "max_size").strip() or None})
        head = "Delegate to agents on"
    else:
        limits, head = None, "Approve the requirements, with no delegation, of"
    full = epics.charter(ws, t, limits, tickets=kids)  # binds the epic, its children and the delegation
    text = ", ".join(f"{k} {v}" for k, v in sorted((limits or {}).items()))
    return _subject("charter", f"{head} epic {t.id} {permits.shown(t.title)} with {len(kids)} open children."
                    + (f" Limits: {text}" if limits else ""),
                    _bound(kind="charter", epic=t.id, gate="requirements", seen=seen, charter=full["hash"],
                           children=[k.id for k in kids]))


def _verdict(ws, route_path, pp, params, method, target, body):
    from orch.core import epics, permits, store
    seen, verdict, note = _one(params, "seen"), _one(params, "verdict"), _one(params, "message")
    t = _ticket(ws, pp.get("ref"))
    if not seen or not verdict:
        return None
    tail = f". {permits.shown(note)}" if note else ""
    if epics.is_epic(t.meta):
        if verdict != "done":
            return None
        tickets = [store.read_ticket(e.path) for e in epics.children(ws, t.id) if e.status != "done"]
        if not tickets or seen != epics.verdict_hash(tickets, ws):
            return None
        return _subject("verdict", f"Accept epic {t.id} {permits.shown(t.title)}: mark done "
                        + ", ".join(x.id for x in tickets) + tail,
                        _bound(kind="verdict", ticket=t.id, verdict=verdict, seen=seen, message=note,
                               children=[x.id for x in tickets]))
    if seen != epics.verdict_hash([t], ws):
        return None
    acs = f" (criteria {', '.join(permits.shown(a) for a in _all(params, 'acs'))})" \
        if verdict == "follow-up" and _all(params, "acs") else ""
    return _subject("verdict", f"Verdict {permits.shown(verdict)} on {t.id} {permits.shown(t.title)}{acs}{tail}",
                    _bound(kind="verdict", ticket=t.id, verdict=verdict, seen=seen, message=note,
                           acs=_all(params, "acs") if verdict == "follow-up" else []))


def _action(ws, route_path, pp, params, method, target, body):
    from orch.core import permits
    if len(body) > 2000:
        return None
    text = body.decode("utf-8")  # strict: a body that is not text cannot be shown, so it cannot be approved
    digest = hashlib.sha256(method.encode() + b"\0" + target.encode() + b"\0" + body).hexdigest()
    return _subject("action", f"{method} {permits.shown(target)} under a running AI Factory epic: "
                    f"{permits.shown(text)}", digest)


_BUILD = {"permission": _permission, "charter": _charter, "verdict": _verdict, "action": _action}
