"""``PUT /v2/hub/allow/{workspace_id}``: a trusted hub admits one of its own workspaces (orch-core #1290, P02, protocol 11.9).

Until now a workspace reached orch-publish only after the operator typed ``admin ws allow`` and ``admin owners add``.
With a hub that is a second admission for a workspace the hub has already verified at ``card_put``. The hub now says it
itself, in a ``hub.allow`` signed by its HK_op. What makes that safe, in order:

1. the hub must be in ``trusted_hubs``: the operator pinned it (``admin hub trust`` / ``--trust-hub``). A hub that merely
   sent its card (anyone can) is not trusted to admit anyone;
2. the held hub card verifies NOW (clock, dev marker, revocations) and its HK_op signed the object; the window is
   +-120 s and the nonce is spent once;
3. the allowance names ONE workspace, ONE owner and ONE ``binding_seq``. It admits nobody else and adds no row to the
   owner allowlist. At registration the card's owner must equal ``owner_person_id`` (``registry.require_prebound``);
4. it only COUNTS while the workspace's live binding is this hub's at exactly that ``binding_seq``
   (``registry.hub_allowed``): leaving or unbinding, a re-join (a higher seq), another hub, or the operator removing
   the pin ends it on the very next request. An unbound also deletes the row;
5. it never moves a workspace between hubs or backwards: an older ``binding_seq`` or another hub's row is refused.
"""

from __future__ import annotations

from . import auth, cleanup, db
from . import hub_path as H
from . import published as P


def handle(settings, ws, obj):
    now = settings.clock()
    o = obj.get("o")
    hub = o.get("hub_id") if type(o) is dict else None
    if type(hub) is not str or len(hub) != 32:
        raise H.Refused(
            "malformed"
        )  # only to find the pin and the card; the contract check re-validates everything
    conn = db.connect(settings.db_path)
    try:
        trusted = (
            conn.execute("SELECT 1 FROM trusted_hubs WHERE hub_id=?", (hub,)).fetchone() is not None
        )
        held = H.held_hub_card(conn, hub, now, settings.dev_mode) if trusted else None
        cert = held["op_cert"]["cert_id"] if held is not None else ""
        o = P.check_allow(
            obj, path_ws=ws, trusted=trusted, held=held, nonces=auth.DbNonces(conn), now_ms=now
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            _store(conn, o, now, cert)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"ok": True}


def _store(conn, o, now, cert):
    ws, hub, person, seq = o["workspace_id"], o["hub"], o["owner_person_id"], o["binding_seq"]
    # on freshly read state, inside the lock
    if conn.execute("SELECT 1 FROM hub_denied WHERE workspace_id=?", (ws,)).fetchone() is not None:
        raise H.Refused(
            "workspace_blocked"
        )  # the operator removed it; only the operator clears that
    row = conn.execute(
        "SELECT owner_person_id FROM workspaces WHERE workspace_id=?", (ws,)
    ).fetchone()
    if row is not None and row["owner_person_id"] != person:
        raise H.Refused("other_person")
    b = H.binding_state(conn, ws)
    bh = None
    if b is not None:
        bh = conn.execute("SELECT hub_id FROM hub_bindings WHERE workspace_id=?", (ws,)).fetchone()[
            "hub_id"
        ]
    # Each hub's allowance is its OWN row (workspace, hub): nothing here reads or replaces another hub's. The workspace's
    # own signed binding (it names the hub) later selects the row that counts (registry.hub_allowed) and drops the others.
    if b is not None and bh != hub and b["status"] == "bound":
        raise H.Refused("other_hub")  # bound to another hub: this one has nothing to say about it
    if b is not None and bh == hub:
        if seq < b["binding_seq"]:
            raise H.Refused("stale_binding")
        if b["status"] == "unbound" and seq <= b["binding_seq"]:
            raise H.Refused("unbound")
    cleanup.drop_expired(conn, now)
    held = conn.execute(
        "SELECT binding_seq FROM hub_allows WHERE workspace_id=? AND hub_id=?", (ws, hub)
    ).fetchone()
    if held is not None and seq < held["binding_seq"]:
        raise H.Refused("stale_binding")
    if (
        held is None
    ):  # a new row: one hub may hold only so many that no workspace is bound to it yet
        pending = conn.execute(
            "SELECT COUNT(*) n FROM hub_allows a WHERE a.hub_id=? AND NOT EXISTS (SELECT 1 FROM hub_bindings b"
            " WHERE b.workspace_id=a.workspace_id AND b.hub_id=a.hub_id AND b.status='bound')",
            (hub,),
        ).fetchone()["n"]
        if pending >= cleanup.ALLOW_PENDING_CAP:
            raise H.Refused("quota_exceeded")
    conn.execute(
        "INSERT INTO hub_allows(workspace_id, hub_id, person_id, binding_seq, cert_id, updated_ms)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id, hub_id) DO UPDATE SET person_id=excluded.person_id, binding_seq=excluded.binding_seq, cert_id=excluded.cert_id,"
        " updated_ms=excluded.updated_ms",
        (ws, hub, person, seq, cert, now),
    )
