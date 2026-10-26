"""The identity of a bridge request (orch-v2-host §11A.4): a leaf module, like :mod:`orch.host.peer`.

A relay request has no kernel UID. Its identity comes only from the verified request (proto §8.6 steps 1 to 4: the
device in the accepted member list at the request's epoch, its certificate under its person's PK, the F1 association
of the exact pair, the ``dk_sig`` signature). The bridge (R2) builds a :class:`RelayIdentity` after those checks; the
host then serves the request in column R with ``principal = "r:<device_id>"`` and the device's person as viewer.
Nothing here verifies anything: the fields are only checked for shape, so a malformed identity never reaches a
column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .peer import Peer

__all__ = ["RelayIdentity"]

_WS = re.compile(r"[0-9a-f]{32}")
_DEVICE = re.compile(r"d_[0-9a-f]{32}")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _match(pattern: re.Pattern[str], value: object, what: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"relay identity: {what} is malformed")


@dataclass(frozen=True)
class RelayIdentity:
    workspace_id: str
    device_id: str
    person_id: str
    #: ``sha256:`` of the device certificate the request was checked against
    cert_digest: str
    #: the request's epoch (``WK_e``)
    epoch: int
    #: ``sha256:`` of the accepted member list the request was checked against
    list_hash: str
    #: H12d (§0.8.6 item 9.5): the digest of the device's current reader declaration, carried by every column-R
    #: request. Its presence alone qualifies nothing: ``orch.host.readerops.check_rd`` (H12d-3) compares it with the
    #: device's current reader record while ``required_reader`` is at least 1.
    rd: str | None = None

    def __post_init__(self) -> None:
        _match(_WS, self.workspace_id, "workspace_id")
        _match(_DEVICE, self.device_id, "device_id")
        _match(_PERSON, self.person_id, "person_id")
        _match(_HASH, self.cert_digest, "cert_digest")
        _match(_HASH, self.list_hash, "list_hash")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("relay identity: epoch is malformed")
        if self.rd is not None:
            _match(_HASH, self.rd, "rd")

    @property
    def principal(self) -> str:
        """``r:<device_id>``: the outcome key's principal (§0.7); two devices with the same idem never collide."""
        return f"r:{self.device_id}"

    def peer(self) -> Peer:
        """The relay :class:`Peer`: ``uid = -1`` (never a kernel UID), the device's person."""
        return Peer(-1, "relay", self.person_id)
