"""The action intent: the one key that makes "one action -> at most one request -> at most one signature" hold.

The host honours `opts.idem` on `human.prepare` (orch-v2-host section 0.7, 0.8.1): the same principal, the same idem
and the same arguments return the SAME request, in its current state, and its terminal state for 24 hours after it was
prepared. So the idem must be a function of what the person is doing, never of the browser tab, the session or the
click:

    idem = "a_" + crockford(sha256(cj({person, workspace_id, op, args, base})))   # 128 bits: 3 + 25 x 5

`op` and `args` are exactly what this server sends to the host (built by the typed models' `host_call`; the browser's
body is never hashed), `person` is the workspace's own person, and `base` is the state the action is based on: the
gate's generation and hash for a gate decision, the question's id and hash for an answer, else none.
Unrelated agent edits never move the base.

`retry` (an explicit click on "Do it again" or "Start again") is folded in only when nonzero, so the common intent
hashes exactly as above. Host retention expiry (`idem.expired`) ends the attempt; it never renews it automatically.

This module has no state. Nothing here, nothing in this process's memory, is what holds the invariant: the host's idem
record is.
"""

import hashlib

from . import adapters

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
IDEM_SHAPE = r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$"


def intent_idem(
    person: str,
    ws: str,
    op: str,
    args: dict,
    base,
    files: dict | None = None,
    retry: int = 0,
) -> str:
    """The host idem of one action intent: `a_` + one char [0-7] (3 bits) + 25 Crockford chars (5 bits each)."""
    intent: dict = {"person": person, "workspace_id": ws, "op": op, "args": args, "base": base}
    if files:
        intent["files"] = files  # the bytes of a certificate are part of what is asked for
    if retry:
        intent["retry"] = retry
    n = int.from_bytes(hashlib.sha256(adapters.handoff.cj(intent)).digest()[:16], "big")  # 128 bits
    first = CROCKFORD[n >> 125]
    rest = "".join(CROCKFORD[(n >> (120 - 5 * i)) & 31] for i in range(25))
    return "a_" + first + rest


def question_base(data: dict, question: str) -> dict:
    """Only the question's identity and content hash, never its answered flag or the rest of the ticket."""
    q = next((q for q in data.get("questions", []) if q.get("id") == question), None)
    if q is None or not q.get("hash"):
        from .errors import fail

        raise fail("not_found", "The host did not provide this question's version.", exit=2)
    return {"id": question, "hash": q["hash"]}


def gate_base(data: dict, gate: str) -> dict:
    """The base of a gate decision: the gate's generation and hash (a stale decision is a different intent)."""
    g = (data.get("gates") or {}).get(gate) or {}
    return {"gate": gate, "gen": g.get("gen"), "hash": g.get("hash")}
