"""``orch-hub inspect`` (dev only): a read-only view of the registry for end-to-end checks (S8).

Opens ``<state>/registry.db`` with SQLite ``mode=ro`` (works while ``serve`` holds it open, never writes) and reads
only public state: pins, binding state, the op counters. It never opens a key file; the only other file read is the
public hub card, to refuse unless that card is ``dev: true``.

Output (``--json``)::

    {"dev": true, "hub_id": "<hex16>", "workspaces": {"<workspace_id>": {
        "workspace_id": "...", "owner_person_id": "...", "wsk_pub": "<b64u>", "wsk_fingerprint": "<sha256 hex>",
        "mode": "selective" | "full" | null,
        "binding": {"state": "bound" | "unbound", "binding_seq": N},
        "snapshot": {"version": N, "roles": {"<user_id>": "read"|"edit"|"admin"}},   # last issued (0, {} if none yet)
        "ops": {"<op>": {"accepted": N, "refused": N}, ...},  # always includes "wk_seal", "route_out", "route_in"
        "routes": [{"id": "<envelope id hex16>", "from": "<ws>", "to": "<ws>", "sha256": "<hex of the envelope bytes>"}]
                                      # envelopes the router took where this workspace is sender or recipient
    }},
     "unjoined_ops": {"<op>": {"accepted": N, "refused": N}}}   # links with no pinned workspace; unknown ops = "other"
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .contract.canon import unb64u

REGISTRY_FILE = "registry.db"
ROUTE_QUEUE_FILE = "route-queue.db"
ALWAYS_COUNTED = ("wk_seal", "route_out", "route_in")


class InspectError(Exception):
    pass


def _hub_card(state: Path) -> dict[str, Any]:
    try:
        meta = json.loads((state / "meta.json").read_text())
        card = json.loads((state / "op" / str(int(meta["op_seq"])) / "card.json").read_text())
        o = card["o"]
        if type(o) is not dict:
            raise TypeError
        return o
    except OSError, ValueError, KeyError, TypeError:
        raise InspectError("no readable hub card in the state dir") from None


def workspace_mode(state: Path, workspace: str) -> str | None:
    """The binding mode of one workspace from the registry (read-only), or None if unknown / unreadable."""
    try:
        w = inspect(state, workspace)["workspaces"].get(workspace)
    except InspectError, sqlite3.Error, OSError, ValueError:
        return None
    return w["mode"] if w else None


def _snapshots(state: Path) -> dict[str, dict[str, Any]]:
    """Per workspace ``{version, roles}`` of the last issued snapshot (access.db, read-only); empty if none."""
    path = state / "access.db"
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT workspace_id, version, signed FROM snapshots").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    out = {}
    for ws, v, sg in rows:
        try:
            out[ws] = {"version": int(v), "roles": dict(json.loads(sg)["o"]["roles"])}
        except ValueError, KeyError, TypeError:
            continue  # a corrupt row is left out, never a crash
    return out


def _routes(state: Path) -> list[dict[str, str]]:
    """Ids and SHA-256 of the envelopes the router has taken (queued or delivered); never their bytes."""
    path = state / ROUTE_QUEUE_FILE
    if not path.is_file():
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT env_id, from_ws, to_ws, digest FROM route_queue ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
    return [{"id": i, "from": f, "to": t, "sha256": bytes(d).hex()} for i, f, t, d in rows]


def inspect(state: Path, workspace: str | None = None) -> dict[str, Any]:
    card = _hub_card(state)
    if card.get("dev") is not True:
        raise InspectError("refused: the hub card is not dev: true (inspect is dev only)")
    reg = state / REGISTRY_FILE
    if not reg.is_file():
        raise InspectError("no registry.db in the state dir")
    db = sqlite3.connect(f"{reg.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT workspace_id, owner_person_id, wsk_pub, binding_state FROM workspace").fetchall()
        try:
            counts = db.execute("SELECT ws, op, outcome, count FROM op_counts").fetchall()
        except sqlite3.OperationalError:  # registry from before the op counters
            counts = []
    finally:
        db.close()
    snaps = _snapshots(state)
    routes = _routes(state)
    out: dict[str, Any] = {}
    for ws, owner, wsk_pub, bstate in rows:
        if workspace is not None and ws != workspace:
            continue
        try:
            st = json.loads(bstate)
            b = st.get("binding")
            binding_view = {"state": st["status"], "binding_seq": st["binding_seq"]}
        except ValueError, KeyError, TypeError, AttributeError:
            st, b, binding_view = {}, None, {"state": "corrupt", "binding_seq": None}
        ops: dict[str, dict[str, int]] = {op: {"accepted": 0, "refused": 0} for op in ALWAYS_COUNTED}
        for cws, op, outcome, n in counts:
            if cws == ws:
                ops.setdefault(op, {"accepted": 0, "refused": 0})[outcome] = n
        out[ws] = {
            "workspace_id": ws,
            "owner_person_id": owner,
            "wsk_pub": wsk_pub,
            "wsk_fingerprint": hashlib.sha256(unb64u(wsk_pub, 65)).hexdigest(),
            "mode": b.get("mode") if type(b) is dict else None,
            "binding": binding_view,
            "ops": ops,
            "snapshot": snaps.get(ws, {"version": 0, "roles": {}}),
            "routes": [r for r in routes if ws in (r["from"], r["to"])],
        }
    unjoined = {op: {"accepted": 0, "refused": 0} for op in ALWAYS_COUNTED}
    for cws, op, outcome, n in counts:
        if cws == "":
            unjoined.setdefault(op, {"accepted": 0, "refused": 0})[outcome] = n
    return {"dev": True, "hub_id": card.get("hub_id"), "workspaces": out, "unjoined_ops": unjoined}
