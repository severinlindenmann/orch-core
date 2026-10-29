"""The hub's narrowing at orch-publish (contracts/hub/protocol.md 11.6, orch-core #1453): ``PUT /v2/hub/narrow``.

A hub admin may revoke a link or stop an app of a FULL-mode workspace bound to the hub, and nothing else. The object is
a ``hub.release_narrow`` signed by the hub's HK_op; ``published.check_release_narrow`` is the contract's check in order
(syntax, window, held card, signature, nonce, binding, owner). This module loads the state, takes the held card from the
same ``hub_path.held_hub_card`` that refuses dev cards (``dev_refused`` is NOT hidden as ``unknown_hub``), keeps the
nonce for the full window (``auth.NONCE_RETENTION_MS``, 240 s), applies the narrowing in one transaction and records it
(``hub_narrowings``) so the workspace sees it in its own list. Nothing here can start, extend or widen anything.
"""

from __future__ import annotations

from . import auth, db
from . import hub_path as H
from . import published as P

#: how long a spent nonce is remembered; must cover the whole +-120 s window (condition: nonces kept >= 240 s)
NONCE_KEEP_MS = auth.NONCE_RETENTION_MS


def _load(conn, hub_hex, ws, now_ms, dev_mode):
    """State for one narrowing: the held card (a refusal such as ``dev_refused`` is kept, not swallowed), the card and
    binding of the one workspace, and the owner of every target this workspace has."""
    held, held_err = None, None
    try:
        held = H.held_hub_card(conn, hub_hex, now_ms, dev_mode)
    except H.Refused as r:
        held_err = r
    cards, bindings = {}, {}
    row = conn.execute("SELECT wsk_pub FROM workspaces WHERE workspace_id=?", (ws,)).fetchone()
    if row is not None:
        cards[ws] = {"wsk_pub": row["wsk_pub"]}
        bindings[ws] = H.binding_state(conn, ws)
    return held, held_err, cards, bindings


def _owners(conn, target, tid):
    if target == "share":
        row = conn.execute(
            "SELECT owner_ws FROM shares WHERE id=? AND state!='staged'", (tid,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT owner_ws FROM apps WHERE id=? AND state!='staged'", (tid,)
        ).fetchone()
    return {} if row is None else {f"{target}:{tid}": row["owner_ws"]}


def handle(settings, store, obj):
    """The whole of ``PUT /v2/hub/narrow`` on a parsed body. Raises ``hub_path.Refused``. Returns the result."""
    now = settings.clock()
    o = obj.get("o") if type(obj) is dict else None
    if type(o) is not dict:
        raise H.Refused("malformed")
    hub, ws, target, tid = o.get("hub_id"), o.get("workspace_id"), o.get("target"), o.get("id")
    # only to find the state; the contract check below re-validates every one of these first
    if not all(type(x) is str and len(x) == 32 for x in (hub, ws, tid)) or target not in (
        "share",
        "app",
    ):
        raise H.Refused("malformed")
    conn = db.connect(settings.db_path)
    try:
        held, held_err, cards, bindings = _load(conn, hub, ws, now, settings.dev_mode)
        try:
            res = P.check_release_narrow(
                obj,
                held=held,
                cards=cards,
                bindings=bindings,
                owners=_owners(conn, target, tid),
                nonces=auth.DbNonces(conn),
                now_ms=now,
            )
        except H.Refused as r:
            if r.code == "unknown_hub" and held_err is not None:
                raise held_err from None  # dev_refused, held_card_invalid, ...: said as such
            raise
        return _apply(conn, store, res, now)
    finally:
        conn.close()


def _apply(conn, store, res, now):
    from . import apps  # late: apps imports shares, which imports this module

    target, tid, ws = res["target"], res["id"], res["workspace_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        if target == "share":
            row = conn.execute(
                "SELECT state FROM shares WHERE id=? AND owner_ws=?", (tid, ws)
            ).fetchone()
            changed = row is not None and row["state"] != "revoked"
            if changed:
                conn.execute(
                    "UPDATE shares SET state='revoked', ended_ms=?, token_hash=? WHERE id=?",
                    (now, "0" * 64, tid),
                )
                conn.execute("DELETE FROM views WHERE share_id=?", (tid,))
            out = "revoked" if changed else "unchanged"
        else:
            row = conn.execute(
                "SELECT state FROM apps WHERE id=? AND owner_ws=?", (tid, ws)
            ).fetchone()
            if row is None or row["state"] in ("removed", "stopped"):
                out = "unchanged"
            else:
                out = apps.stop_row(conn, tid, now)
        if out in ("revoked", "stopped"):
            conn.execute(
                "INSERT INTO hub_narrowings(target, target_id, workspace_id, hub_id, action, at_ms) VALUES (?,?,?,?,?,?)",
                (target, tid, ws, res["hub"], res["action"], now),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if target == "share" and out == "revoked":
        store.remove(tid)  # files go now; a failure here is picked up by the sweep
    return {"ok": True, "target": target, "id": tid, "result": out}


def narrowed_by_hub(conn, ws, target, tid):
    """True when a hub admin narrowed this target (the workspace's own list shows it)."""
    return (
        conn.execute(
            "SELECT 1 FROM hub_narrowings WHERE workspace_id=? AND target=? AND target_id=? LIMIT 1",
            (ws, target, tid),
        ).fetchone()
        is not None
    )
