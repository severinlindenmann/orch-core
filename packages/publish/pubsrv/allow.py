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

from . import auth, db
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
    held = conn.execute(
        "SELECT hub_id, binding_seq FROM hub_allows WHERE workspace_id=?", (ws,)
    ).fetchone()
    # Who may write the row. Precondition of the whole store, nothing is written otherwise:
    #  - bound to the REQUESTING hub: that hub's allowance for the live epoch (it may overwrite any older row);
    #  - bound to another hub: refused (other_hub);
    #  - unbound from this hub: not at or below its unbound seq (unbound);
    #  - no binding row yet (the join: publish learns the hub's allowance BEFORE the card and the binding it depends on,
    #    because the binding needs the registered workspace): first writer only: another hub's row is never replaced (a same-hub row only by an equal or newer epoch).
    if b is not None and bh != hub and b["status"] == "bound":
        raise H.Refused("other_hub")
    if b is not None and bh == hub:
        if seq < b["binding_seq"]:
            raise H.Refused("stale_binding")
        if b["status"] == "unbound" and seq <= b["binding_seq"]:
            raise H.Refused("unbound")
    if (
        held is not None
        and held["hub_id"] != hub
        and not (b is not None and bh == hub and b["status"] == "bound")
    ):
        raise H.Refused(
            "other_hub"
        )  # another hub's row is replaced only by the hub the workspace is bound to
    if held is not None and held["hub_id"] == hub and seq < held["binding_seq"]:
        raise H.Refused("stale_binding")
    conn.execute(
        "INSERT INTO hub_allows(workspace_id, hub_id, person_id, binding_seq, cert_id, updated_ms)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET hub_id=excluded.hub_id,"
        " person_id=excluded.person_id, binding_seq=excluded.binding_seq, cert_id=excluded.cert_id,"
        " updated_ms=excluded.updated_ms",
        (ws, hub, person, seq, cert, now),
    )
