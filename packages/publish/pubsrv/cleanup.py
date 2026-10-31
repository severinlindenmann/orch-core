"""The ONE place that removes a workspace, a hub pin or a hub allowance (P02 class fix).

Invariant: a ``hub_allows`` row never outlives what it depends on, and the operator's word beats a hub's. The use-time
check (``registry.hub_allowed``) already makes a stale row dead (epoch, pin, op-cert revocation, deny marker); this module
keeps the rows from piling up and makes sure no path forgets one. Every ``DELETE`` on ``workspaces``, ``hub_bindings``,
``trusted_hubs`` or ``hub_allows`` in pubsrv is here (a guard test lists them). Callers hold the transaction.
"""

from __future__ import annotations


def drop_allow(conn, ws, hub=None):
    if hub is None:
        conn.execute("DELETE FROM hub_allows WHERE workspace_id=?", (ws,))
    else:
        conn.execute("DELETE FROM hub_allows WHERE workspace_id=? AND hub_id=?", (ws, hub))


def drop_workspace(conn, ws, now_ms):
    """Operator unbind: forget the workspace and its pre-bind and allowance, and write the deny marker so a trusted hub
    cannot admit it again. Only the operator clears the marker (``admin ws allow``). Returns the rows removed."""
    n = conn.execute("DELETE FROM workspaces WHERE workspace_id=?", (ws,)).rowcount
    conn.execute("DELETE FROM prebinds WHERE workspace_id=?", (ws,))
    drop_allow(conn, ws)
    conn.execute(
        "INSERT OR IGNORE INTO hub_denied(workspace_id, added_ms) VALUES (?,?)", (ws, now_ms)
    )
    return n


def clear_deny(conn, ws):
    conn.execute("DELETE FROM hub_denied WHERE workspace_id=?", (ws,))


def drop_hub(conn, hub):
    """The operator stops trusting a hub: its pin and every allowance it gave."""
    n = conn.execute("DELETE FROM trusted_hubs WHERE hub_id=?", (hub,)).rowcount
    conn.execute("DELETE FROM hub_allows WHERE hub_id=?", (hub,))
    return n


def drop_cert(conn, hub, cert_id):
    """An op cert of a hub was revoked: the allowances it signed."""
    conn.execute("DELETE FROM hub_allows WHERE hub_id=? AND cert_id=?", (hub, cert_id))
