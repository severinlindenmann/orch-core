"""R6a test kit: a membership key store over a fresh directory, and the head transitions R6b will drive (a rotation
committed, an exposure abandoned), built with the H12c builders so every number is the real one."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity.epochs import MembershipKeys
from orch.identity.memberlist import member_list_o
from orch.wsproto import sign

from . import corpus
from .helpers import T0, WS, Key, MemLedger

PERSON = corpus.IDS["person_alice"]
DEVICE = corpus.IDS["device_phone"]
DELEGATION = corpus.S2["card"]["card"]["delegation"]


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def open_keys(
    tmp: Path,
    *,
    fault: Callable[[str], None] | None = None,
    clock: Clock | None = None,
    previously_linked: bool = False,
) -> MembershipKeys:
    mk = MembershipKeys(tmp / "membership", WS, now_ms=clock or Clock(), fault=fault,
                        previously_linked=previously_linked)  # fmt: skip
    return mk


def list_for(epoch: int, list_seq: int, commit: str, devices: list[str] | None = None) -> dict[str, Any]:
    devs = [DEVICE] if devices is None else devices
    members = [{"device_id": d, "person_id": PERSON, "scopes": ["look", "decide"]} for d in devs]
    return member_list_o(WS, epoch, list_seq, T0, commit, members, owner_person_id=PERSON, first=list_seq == 1)


def digest(o: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canon.dumps(o)).hexdigest()


def rotate(mk: MembershipKeys, epoch: int | None = None, *, devices: list[str] | None = None) -> int:
    """New key, expose and commit a rotation to ``epoch`` (default EHW + 1); returns the epoch."""
    lhw, ehw = mk.high_water()
    e = ehw + 1 if epoch is None else epoch
    commit = mk.new_epoch_key(e)
    o = list_for(e, lhw + 1, commit, devices)
    mk.expose(list_seq=lhw + 1, epoch=e, body=canon.dumps(o), digest=digest(o), adds=devices or [DEVICE])
    assert mk.commit(list_seq=lhw + 1, epoch=e, list_hash=digest(o), list_obj=o, at_ms=T0)
    return e


def expose_rotation(
    mk: MembershipKeys, epoch: int | None = None, *, devices: list[str] | None = None
) -> tuple[int, int]:
    """New key and expose a rotation, uncommitted; ``(list_seq, epoch)``."""
    lhw, ehw = mk.high_water()
    e = ehw + 1 if epoch is None else epoch
    commit = mk.new_epoch_key(e)
    o = list_for(e, lhw + 1, commit, devices)
    mk.expose(list_seq=lhw + 1, epoch=e, body=canon.dumps(o), digest=digest(o), adds=devices or [DEVICE])
    return lhw + 1, e


def ck_of(mk: MembershipKeys, card_seq: int) -> bytes:
    """Test-only: ``CK_s`` read from its key file (the store never hands it out raw)."""
    import json

    from orch import crypto

    raw = (mk.root / "keys" / f"ck-{card_seq}.json").read_bytes()
    return crypto.unb64u(json.loads(raw)["ck"])


def signer(mk: MembershipKeys, origin: str = "https://relay.example", ledger: Any = None) -> sign.RelaySigner:
    return sign.RelaySigner(
        Key(), sign.LinkContext(WS, origin), ledger=ledger if ledger is not None else mk.ledger, burned=mk.is_burned
    )


__all__ = ["DELEGATION", "DEVICE", "PERSON", "Clock", "MemLedger", "ck_of", "digest", "expose_rotation", "list_for",
           "open_keys", "rotate", "signer"]  # fmt: skip
