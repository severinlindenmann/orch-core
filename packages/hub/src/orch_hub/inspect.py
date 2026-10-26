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
        "shares": {"items": N, "bytes": M},   # selective: shared items held (counts only, no ids, no content)
        "wk_epochs": [N, ...]         # epochs of wk_seal wraps held for the live full binding (no key bytes)
        "routes": [{"id": "<envelope id hex16>", "from": "<ws>", "to": "<ws>", "sha256": "<hex of the envelope bytes>"}]
                                      # envelopes the router took where this workspace is sender or recipient
    }},
     "unjoined_ops": {"<op>": {"accepted": N, "refused": N}},    # links with no pinned workspace; unknown ops = "other"
     "spaces": [{"space_id", "name", "epoch", "members": [ws], "epochs": [{"epoch", "members", "sk_commit"}],
                 "items": [{"item_id", "seq", "epoch", "workspace_id", "revoked", "sha256"}]}]}
        # shared spaces (D75): ids, epochs, members, item metadata and the SHA-256 of each sealed item; never SK_e, wraps
        # or sealed bytes
     "audit": {"head": {"seq", "hash"} | null, "records": [{"record": <signed hub.audit>, "event": {...}}],
               "acks": {"<workspace_id>": seq}}
        # the hub audit chain (D4): the signed records, the events behind their hashes (the operator's view), the acks
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
SPACES_FILE = "spaces.db"
AUDIT_FILE = "audit.db"
WK_SEAL_FILE = "wk-seal.db"
SHARES_FILE = "shares.db"
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


def _spaces(state: Path) -> list[dict[str, Any]]:
    path = state / SPACES_FILE
    if not path.is_file():
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        out = []
        for sid, name, epoch in db.execute("SELECT space_id, name, epoch FROM space ORDER BY space_id").fetchall():
            eps = []
            for e, members, rec in db.execute(
                "SELECT epoch, members, record FROM space_epoch WHERE space_id=? ORDER BY epoch", (sid,)
            ).fetchall():
                eps.append({"epoch": e, "members": json.loads(members), "sk_commit": json.loads(rec)["o"]["sk_commit"]})
            items = [
                {"item_id": i, "seq": q, "epoch": e, "workspace_id": w, "revoked": bool(r), "sha256": d}
                for i, q, e, w, r, d in db.execute(
                    "SELECT item_id, seq, epoch, workspace_id, revoked, digest FROM space_item "
                    "WHERE space_id=? ORDER BY seq",
                    (sid,),
                ).fetchall()
            ]
            cur = next((x["members"] for x in eps if x["epoch"] == epoch), [])
            out.append({"space_id": sid, "name": name, "epoch": epoch, "members": cur, "epochs": eps, "items": items})
        return out
    except sqlite3.Error, ValueError, KeyError, TypeError:
        return []  # unreadable or foreign rows: inspect shows nothing rather than failing
    finally:
        db.close()


def _audit(state: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"head": None, "records": [], "acks": {}}
    path = state / AUDIT_FILE
    if not path.is_file():
        return empty
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT seq, chain_hash, signed, event FROM record ORDER BY seq").fetchall()
        acks = dict(db.execute("SELECT workspace_id, seq FROM ack ORDER BY workspace_id").fetchall())
        recs = [{"record": json.loads(sg), "event": json.loads(ev)} for _s, _h, sg, ev in rows]
        head = None if not rows else {"seq": rows[-1][0], "hash": rows[-1][1]}
        return {"head": head, "records": recs, "acks": acks}
    except sqlite3.Error, ValueError, KeyError, TypeError:
        return empty
    finally:
        db.close()


def _wk_epochs(state: Path) -> dict[str, list[int]]:
    """``{workspace_id: [epoch, ...]}`` of the non-retired wk_seal rows. Epoch numbers only: the wrap column is never
    selected."""
    path = state / WK_SEAL_FILE
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT ws, epoch FROM wk_seal WHERE retired=0 ORDER BY ws, epoch").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        db.close()
    out: dict[str, list[int]] = {}
    for ws, e in rows:
        out.setdefault(ws, []).append(e)
    return out


def _shares(state: Path) -> dict[str, dict[str, int]]:
    """``{workspace_id: {"items": N, "bytes": M}}``: counts only, never an id, a digest or the sealed column."""
    path = state / SHARES_FILE
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT ws, COUNT(*), SUM(size) FROM share_item WHERE revoked=0 GROUP BY ws").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        db.close()
    return {ws: {"items": n, "bytes": z} for ws, n, z in rows}


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
    wk_epochs = _wk_epochs(state)
    shares = _shares(state)
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
            "wk_epochs": wk_epochs.get(ws, []),
            "shares": shares.get(ws, {"items": 0, "bytes": 0}),
            "routes": [r for r in routes if ws in (r["from"], r["to"])],
        }
    unjoined = {op: {"accepted": 0, "refused": 0} for op in ALWAYS_COUNTED}
    for cws, op, outcome, n in counts:
        if cws == "":
            unjoined.setdefault(op, {"accepted": 0, "refused": 0})[outcome] = n
    return {
        "dev": True,
        "hub_id": card.get("hub_id"),
        "workspaces": out,
        "unjoined_ops": unjoined,
        "spaces": _spaces(state),
        "audit": _audit(state),
    }
