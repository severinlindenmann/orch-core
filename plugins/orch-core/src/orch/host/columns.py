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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Column", "GrantState", "OpKind", "refusal", "resolve"]

GrantState = Literal["absent", "verified", "malformed", "unverified"]
PeerClass = Literal["person", "agent", "worker", "control"]
#: read: registry read ops (and the §0.8 reads); person_read: doctor, instructions.sync, instructions.hook;
#: agent / unattended / human: the registry ``who``; human_api: ``human.*``; job, control: their sockets' ops;
#: pg_read: ``session.list`` and ``grant.list`` (P and G only, H6b).
OpKind = Literal["read", "person_read", "pg_read", "agent", "unattended", "human", "human_api", "job", "control"]


@dataclass(frozen=True)
class Column:
    name: Literal["P", "G", "N", "W", "C"]
    viewer: Literal["person", "grant", "no-person", "none"]
    #: the answer to every write in this row, overriding the matrix (None: the matrix decides)
    write_error: str | None = None
    #: the answer to ``human.*`` in this row, overriding the matrix
    human_error: str | None = None


def resolve(peer: PeerClass, grant: GrantState, mapping_valid: bool) -> Column:
    """One row of the §0.5 effective-column table."""
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
    if mapping_valid:
        return Column("P", "person")
    return Column("N", "no-person", write_error="op.not_allowed", human_error="op.not_allowed")


# The permission matrix (§0.5) for each column.
_MATRIX: dict[str, set[str]] = {
    "P": {"read", "person_read", "pg_read", "unattended", "human_api"},
    "G": {"read", "pg_read", "agent", "unattended"},
    "N": {"read", "unattended"},
    "W": {"job"},
    "C": {"control"},
}
_WRITES = {"agent", "unattended", "human"}


def refusal(column: Column, kind: OpKind) -> str | None:
    """The error code for an op of ``kind`` in ``column``, or ``None`` when it may run."""
    if kind == "human" and column.name in {"P", "G", "N"}:
        return "human_only"  # a human registry op as a plain req, in every workspace column
    if kind == "human_api" and column.human_error is not None:
        return column.human_error
    if kind in _WRITES and column.write_error is not None:
        return column.write_error
    return None if kind in _MATRIX[column.name] else "op.not_allowed"
