"""``orch-hub inspect`` (dev only): a read-only view of the registry for end-to-end checks (S8).

Opens ``<state>/registry.db`` with SQLite ``mode=ro`` (works while ``serve`` holds it open, never writes) and reads
only public state: pins, binding state, the op counters. It never opens a key file; the only other file read is the
public hub card, to refuse unless that card is ``dev: true``.

Output (``--json``)::

    {"dev": true, "hub_id": "<hex16>", "workspaces": {"<workspace_id>": {
        "workspace_id": "...", "owner_person_id": "...", "wsk_pub": "<b64u>", "wsk_fingerprint": "<sha256 hex>",
        "mode": "selective" | "full" | null,
        "binding": {"state": "bound" | "unbound", "binding_seq": N},
        "ops": {"<op>": {"accepted": N, "refused": N}, ...}   # always includes "wk_seal"
    }}}
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .contract.canon import unb64u

REGISTRY_FILE = "registry.db"
ALWAYS_COUNTED = ("wk_seal",)


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
            counts = db.execute("SELECT ws, op, outcome, COUNT(*) FROM op_log GROUP BY ws, op, outcome").fetchall()
        except sqlite3.OperationalError:  # registry from before the op log
            counts = []
    finally:
        db.close()
    out: dict[str, Any] = {}
    for ws, owner, wsk_pub, bstate in rows:
        if workspace is not None and ws != workspace:
            continue
        st = json.loads(bstate)
        b = st.get("binding")
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
            "binding": {"state": st["status"], "binding_seq": st["binding_seq"]},
            "ops": ops,
        }
    return {"dev": True, "hub_id": card.get("hub_id"), "workspaces": out}
