"""Workspace admission (design 2.6b): the delegation of a PK-verified card (hub `card_put`), the owner allowlist, block."""

import sqlite3

from . import cleanup
from . import relay_proto as proto
from .auth import Refusal

ID32 = r"[0-9a-f]{32}"


def check_delegation(delegation, pk, ws_hex, path_ws=None):
    """Design 2.6b steps 1-2: the owner PK signed this delegation, it names this owner, and it is
    for exactly this workspace (the signed Orch-Workspace and, when given, the path). Returns the
    signed object `o`."""
    res = proto.verify_delegation(delegation, pk)
    if "refuse" in res:
        raise Refusal(400, "delegation_refused", reason=res["refuse"])
    o = res["ok"]
    if o["workspace_id"] != ws_hex or (path_ws is not None and path_ws != ws_hex):
        raise Refusal(400, "other_workspace")
    return o


_ALLOW_OK = (
    " WHERE a.workspace_id=? AND a.person_id=?"
    " AND NOT EXISTS (SELECT 1 FROM hub_op_revoked r WHERE r.hub_id=a.hub_id AND r.cert_id=a.cert_id)"
    " AND NOT EXISTS (SELECT 1 FROM hub_denied d WHERE d.workspace_id=a.workspace_id)"
)


def hub_allowed_live(conn, ws_hex, person_id):
    """P02, the gate of every REQUEST. A TRUSTED hub (`trusted_hubs`) said by a signed `hub.allow` that this workspace
    and owner are its own, AND all of: the operator never unbound the workspace (`hub_denied`), the op cert that signed
    it is not revoked, and the workspace's binding is `bound` to that same hub at exactly the allowed `binding_seq`.
    Leaving, unbinding, a re-join, another hub, a revoked cert, an untrusted hub or an operator unbind each end it on the
    very next request; a stale row is dead by construction."""
    q = (
        "SELECT 1 FROM hub_allows a JOIN trusted_hubs t ON t.hub_id=a.hub_id"
        " JOIN hub_bindings b ON b.workspace_id=a.workspace_id AND b.hub_id=a.hub_id"
        " AND b.binding_seq=a.binding_seq AND b.status='bound'" + _ALLOW_OK
    )
    return conn.execute(q, (ws_hex, person_id)).fetchone() is not None


def hub_allowed_pending(conn, ws_hex, person_id, *, now_ms):
    """P02, the gate of REGISTRATION (the binding cannot exist before the workspace does): the same row, pin, cert and
    deny rules, and the row must be younger than the 24 h expiry. `now_ms` is required (keyword only): there is no
    default that could silently switch the expiry off."""
    q = (
        "SELECT 1 FROM hub_allows a JOIN trusted_hubs t ON t.hub_id=a.hub_id"
        " AND a.updated_ms >= ?" + _ALLOW_OK
    )
    return (
        conn.execute(q, (now_ms - cleanup.ALLOW_TTL_MS, ws_hex, person_id)).fetchone() is not None
    )


def owner_listed(conn, person_id):
    return (
        conn.execute("SELECT 1 FROM owners WHERE person_id=?", (person_id,)).fetchone() is not None
    )


def require_prebound(conn, ws_hex, person_id, *, now_ms):
    """An unknown workspace may register only if the admin pre-bound it to this owner (M2) and the
    owner is on the allowlist, or a trusted hub allowed exactly this workspace and owner (P02). Every
    failure is the same 401, whatever the reason (L3)."""
    ok = (
        conn.execute(
            "SELECT 1 FROM prebinds p JOIN owners o ON o.person_id = p.person_id"
            " WHERE p.workspace_id=? AND p.person_id=?",
            (ws_hex, person_id),
        ).fetchone()
        is not None
    ) or hub_allowed_pending(conn, ws_hex, person_id, now_ms=now_ms)
    if not ok:
        raise Refusal(401, "unknown_workspace")


def get_workspace(conn, ws_hex):
    return conn.execute("SELECT * FROM workspaces WHERE workspace_id=?", (ws_hex,)).fetchone()


def require_binding(conn, ws_hex, wsk_pub):
    """Re-check, on freshly read state, that this workspace is still bound to this key and still
    admitted. Call it inside any write transaction (P2 handlers) and in the middleware after the body
    is read: an unbind, re-register or block that happened since the request was admitted refuses it.
    Returns the fresh row."""
    row = get_workspace(conn, ws_hex)
    if row is None or row["wsk_pub"] != wsk_pub:
        raise Refusal(401, "unknown_workspace")
    check_admitted(conn, row)
    return row


def check_admitted(conn, row):
    """Policy for an authenticated, known workspace; evaluated on every request from the DB row,
    with no cache in front, so a block or an allowlist removal bites on the very next request."""
    if row["status"] != "active":
        raise Refusal(403, "workspace_blocked")
    if not owner_listed(conn, row["owner_person_id"]) and not hub_allowed_live(
        conn, row["workspace_id"], row["owner_person_id"]
    ):
        raise Refusal(403, "owner_not_allowed")


def register(conn, ws_hex, delegation, pk, now_ms, guard=None, after=None):
    """Bind a workspace to its genesis delegation (design 2.6b). Everything is decided inside one
    BEGIN IMMEDIATE transaction on freshly read state. The binding is permanent: a byte-identical
    delegation is "unchanged", any other one is 409 delegation_changed; only an admin unbind
    releases it. Returns "registered" | "unchanged".

    `guard(conn, admitted)` runs right after BEGIN IMMEDIATE and `after(conn)` right before COMMIT, so a caller's own
    checks and writes share this transaction (the hub card route keeps its freshness floor there)."""
    o = check_delegation(delegation, pk, ws_hex, ws_hex)
    blob = proto.cj(delegation).decode()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = get_workspace(conn, ws_hex)
        if guard is not None:
            guard(conn, row is not None)
        if row is None:
            # checked on freshly read state, inside the lock (M2)
            require_prebound(conn, ws_hex, o["owner_person_id"], now_ms=now_ms)
            if not owner_listed(conn, o["owner_person_id"]) and not hub_allowed_pending(
                conn, ws_hex, o["owner_person_id"], now_ms=now_ms
            ):
                raise Refusal(401, "unknown_workspace")
            conn.execute(
                "INSERT INTO workspaces(workspace_id, wsk_pub, owner_person_id, owner_pk,"
                " delegation_json, status, created_ms, updated_ms)"
                " VALUES (?,?,?,?,?,'active',?,?)",
                (ws_hex, o["wsk_pub"], o["owner_person_id"], proto.b64u(pk), blob, now_ms, now_ms),
            )
            outcome = "registered"
        else:
            check_admitted(conn, row)
            if row["delegation_json"] != blob:
                raise Refusal(409, "delegation_changed")
            outcome = "unchanged"
        if after is not None:
            after(conn)
        conn.execute("COMMIT")
        return outcome
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
