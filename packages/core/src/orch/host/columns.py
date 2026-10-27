"""The §0.5 effective column: each request resolves to exactly one column and one viewer.

This module is pure: the caller supplies the peer class, the state of ``auth.grant`` and whether the person mapping
holds right now (F8). :func:`refusal` then applies the precedence of §0.6 steps 5 and 6 to one operation:

* an **unverified** grant on a write that column G would allow answers ``grant.required`` (malformed) or
  ``grant.expired`` (unknown, expired, revoked, wrong secret), never ``op.not_allowed``;
* a person-class UID whose mapping is invalid reads as ``no-person`` and **every write** answers ``op.not_allowed``
  (L1: a removed member's UID keeps no unattended writes);
* otherwise the permission matrix: ``op.not_allowed`` outside it, ``human_only`` for a human registry op sent as a
  plain ``req`` and for ``human.*`` with a grant.

The full matrix (every op name, `session.list` scoping, H4a1) builds on :func:`refusal` with the op's category.

Column R (orch-v2-host §11A.4, H12a) is the relay peer's: a bridge request has no kernel UID, so :func:`resolve`
refuses the ``relay`` peer class (and every class it does not list) and :func:`resolve_relay` gives R or a row that
refuses every op. R is served only an op of its closed list :data:`RELAY_OPS` whose kind is in its matrix row; the
kind alone never serves R. Only R has the kind ``device_signed`` (``relay.submit``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEVICE_SIGNED_OPS",
    "RELAY_OPS",
    "Column",
    "GrantState",
    "OpKind",
    "refusal",
    "resolve",
    "resolve_relay",
]

GrantState = Literal["absent", "verified", "malformed", "unverified"]
PeerClass = Literal["person", "agent", "worker", "control"]  # "relay" is refused: :func:`resolve_relay`
#: read: registry read ops (and the §0.8 reads); person_read: doctor, instructions.sync;
#: agent / unattended / human: the registry ``who``; human_api: ``human.*``; job, control: their sockets' ops;
#: pg_read: ``session.list``, ``grant.list`` (P and G only, H6b) and ``instructions.hook`` (the agent's own hook).
#: person_write: a host-side write only a person-class caller makes (``records.remote_private``, H11e).
#: pg_write: a host-side side effect of P and G (``worktree.create``/``remove``, H11d; the op narrows G further).
#: device_signed: device-signed person events through the relay entry (``relay.submit``, column R only, §11A.4).
#: deposit: ``grant.deposit`` (H11c, D-P2-27): a person-class caller **with** a verified grant of their own, so column G
#: at the matrix; the op itself requires the person class, the UID's valid mapping and the grant's ``label:"runner"``.
OpKind = Literal[
    "read",
    "person_read",
    "pg_read",
    "person_write",
    "pg_write",
    "agent",
    "unattended",
    "human",
    "human_api",
    "job",
    "control",
    "device_signed",
    "deposit",
]


@dataclass(frozen=True)
class Column:
    name: Literal["P", "G", "N", "W", "C", "R"]
    viewer: Literal["person", "grant", "no-person", "none"]
    #: the answer to every write in this row, overriding the matrix (None: the matrix decides)
    write_error: str | None = None
    #: the answer to ``human.*`` in this row, overriding the matrix
    human_error: str | None = None
    #: the answer to **every** op in this row (a relay identity that no longer holds, §11A.4)
    error: str | None = None


def resolve(peer: PeerClass, grant: GrantState, mapping_valid: bool) -> Column:
    """One row of the §0.5 effective-column table. Raises ``ValueError`` for the ``relay`` peer and for any peer class
    it does not list (fail closed): a relay request is resolved only by :func:`resolve_relay`."""
    if peer not in _SOCKET_PEERS:
        raise ValueError(f"peer.denied: no socket column for the peer class {peer!r}")
    if peer == "worker":
        if grant != "absent":
            raise ValueError("req.invalid: auth is not allowed on the worker socket")
        return Column("W", "none")
    if peer == "control":
        if grant != "absent":
            raise ValueError("req.invalid: auth is not allowed on the control socket")
        return Column("C", "none")
    if grant == "verified":
        return Column("G", "grant", human_error="human_only" if peer == "person" else None)
    if grant in {"malformed", "unverified"}:
        code = "grant.required" if grant == "malformed" else "grant.expired"
        return Column("N", "no-person", write_error=code, human_error="human_only" if peer == "person" else None)
    if peer == "agent":
        return Column("N", "no-person")
    if peer != "person":  # the relay, and any class not listed here: never a socket column (fail closed)
        raise ValueError(f"peer.denied: no socket column for the peer class {peer!r}")
    if mapping_valid:
        return Column("P", "person")
    return Column("N", "no-person", write_error="op.not_allowed", human_error="op.not_allowed")


def resolve_relay(ok: bool) -> Column:
    """Column R for a relay request whose identity holds now (§11A.4 steps 1 to 3 and the scopes, checked by the
    caller inside the op's own job), else a row that refuses every op."""
    if ok is True:
        return Column("R", "person")
    if ok is False:
        refused = "op.not_allowed"
        return Column("N", "no-person", write_error=refused, human_error=refused, error=refused)
    raise ValueError("resolve_relay takes a bool")


#: the ops column R may be served (§11A.4), closed: the registry read ops except ``doctor`` and ``instructions.*``
#: (and ``wait``, which parks on a socket session's bus), the host reads the relay maps, ``whoami``, ``outcome.get`` and
#: ``relay.submit``. A new registry read op is not served to R until it is listed here.
RELAY_OPS = frozenset(
    {
        # registry read ops
        "addon.list", "artifact.list", "check", "describe", "help", "inbox", "list", "next", "quick.list", "search",
        "show", "status", "task.list", "task.next", "follow_up.show", "follow_up.list", "webhook.new",
        # host reads, filtered for the viewer exactly as P (F6)
        "ticket.query", "ticket.get", "needs", "activity.list", "diff", "artifact.get", "workspace.show",
        "member.list", "ticket.text", "artifact.query", "commit.list", "workspace.settings",
        # H12d-2: the reader records of the current devices (§0.8.6 item 9.11)
        "reader.list",
        # B10 Part B: the viewer's own phone requests, and the gate-hash input a phone recomputes
        "phone.requests", "gate.input",
        # the principal's own
        "whoami", "outcome.get",
        # device-signed person events
        "relay.submit",
        # H12d-3 (§0.8.6 item 9.5, 9.11): the requesting device's own reader declaration, exempt from the rd check
        "reader.declare",
    }
)  # fmt: skip
#: the ops of kind ``device_signed`` in column R: device-signed person events (``relay.submit``) and the device's own
#: signed reader declaration (``reader.declare``, H12d-3; on a socket it is the person's ``person_write``)
DEVICE_SIGNED_OPS = frozenset({"relay.submit", "reader.declare"})
_SOCKET_PEERS = frozenset({"person", "agent", "worker", "control"})

# The permission matrix (§0.5) for each column.
_MATRIX: dict[str, set[str]] = {
    "P": {"read", "person_read", "pg_read", "person_write", "pg_write", "unattended", "human_api"},
    "G": {"read", "pg_read", "pg_write", "agent", "unattended", "deposit"},
    "N": {"read", "unattended"},
    "W": {"job"},
    "C": {"control"},
    "R": {"read", "device_signed"},
}
_WRITES = {
    "agent",
    "unattended",
    "human",
    "pg_write",
    "deposit",
}  # person_write is outside every column but P: op.not_allowed there


def refusal(column: Column, kind: OpKind, op: str | None = None) -> str | None:
    """The error code for an op of ``kind`` in ``column``, or ``None`` when it may run. Column R needs ``op`` too:
    only an op of :data:`RELAY_OPS` (``device_signed`` only for :data:`DEVICE_SIGNED_OPS`) may run there."""
    if column.error is not None:
        return column.error
    if column.name == "R":
        if op not in RELAY_OPS or (kind == "device_signed") != (op in DEVICE_SIGNED_OPS):
            return "op.not_allowed"
    if kind == "human" and column.name in {"P", "G", "N"}:
        return "human_only"  # a human registry op as a plain req, in every workspace column
    if kind == "human_api" and column.human_error is not None:
        return column.human_error
    if kind in _WRITES and column.write_error is not None:
        return column.write_error
    return None if kind in _MATRIX[column.name] else "op.not_allowed"
