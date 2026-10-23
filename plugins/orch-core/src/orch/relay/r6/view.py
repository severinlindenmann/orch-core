"""R6b: the real :class:`orch.relay.keys.R6View` (11A.15.2 "``R6View.member_list()``"): the host's own committed list,
re-verified at load, never what the relay serves.

The committed list is ``head.json`` ``committed`` (the exact ``o`` that was exposed and that the relay answered 2xx
to) plus the signature the R6 signer ledger recorded for its ``list_seq``. Each time ``head.json`` names a new
committed list it is re-checked, in this order, and any failure serves NO member (``None``, alarm
``membership_view_refused``; fail closed, R2 then answers ``not_member``):

1. the WSK signature, the field set and the workspace (``memberlist.check_member_list``);
2. ``list_seq`` and ``epoch`` equal the committed record's, the epoch is the key store's committed epoch ``e_c``, and
   ``wk_commit`` equals the commit recomputed from the local ``WK_e`` (``MembershipKeys.wk_commit_of``);
3. ``list_hash`` equals ``"sha256:" + hex(sha256(cj(signed list)))`` and the record's;
4. every entry: the person is the owner's (P3: another person's entry refuses the whole list), the scopes are a
   non-empty prefix of ``look, decide, operate, type``, a record ``devices/<id>.json`` exists whose certificate
   verifies under the owner's ``PK`` (``check_cert``: the ids are bound to the keys) for this device and person, and
   the scopes are within the certificate's ``scopes_max``. R2 runs ``verify_cert`` again at its own clock per request
   (expiry, revocations).

The verified :class:`~orch.relay.keys.MemberList` is cached against ``(list_seq, epoch, list_hash)``; the check runs
again whenever the committed record changes. No lock is taken that a seal span could hold
(``MembershipKeys.committed_record`` is lock-free), so R2 may call it anywhere.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon, crypto
from orch.identity import certs, memberlist
from orch.identity.certs import SCOPE_ORDER
from orch.identity.epochs import MembershipKeys
from orch.identity.errors import Refused

from ..keys import Member, MemberList

__all__ = ["CommittedView", "list_hash_of"]


def list_hash_of(signed_list: Mapping[str, Any]) -> str:
    """``"sha256:" + hex(sha256(cj(signed list)))``: the identity of an accepted signed list."""
    return "sha256:" + hashlib.sha256(canon.dumps(dict(signed_list))).hexdigest()


class CommittedView:
    """``member_list()`` over the key store's committed list. ``device(id)`` reads ``devices/<id>.json`` (R6c's
    write-once record: ``cert``, ``person_id``)."""

    def __init__(
        self,
        keys: MembershipKeys,
        *,
        wsk_pub: bytes,
        owner_pk_pub: bytes,
        device: Callable[[str], Mapping[str, Any] | None],
        alarm: Callable[[str, str], None] = lambda code, detail: None,
    ) -> None:
        self._keys, self._wsk, self._pk, self._device, self._alarm = keys, wsk_pub, owner_pk_pub, device, alarm
        self._person = crypto.person_id(owner_pk_pub).hex()
        self._lock = threading.Lock()
        self._cache: tuple[tuple[int, int, str], MemberList] | None = None
        self._refused: tuple[int, int, str] | None = None

    def __repr__(self) -> str:
        return f"CommittedView(workspace={self._keys.workspace_id})"

    def member_list(self) -> MemberList | None:
        c = self._keys.committed_record()
        if c is None:
            return None
        key = (c["list_seq"], c["epoch"], c["list_hash"])
        with self._lock:
            if self._cache is not None and self._cache[0] == key:
                ml = self._cache[1]
            elif self._refused == key:
                return None
            else:
                try:
                    ml = self._load(c)
                except (Refused, KeyError, TypeError, ValueError, _ViewRefused) as e:
                    self._cache, self._refused = None, key
                    self._alarm("membership_view_refused", type(e).__name__ + (f": {e}"[:60] if str(e) else ""))
                    return None
                except OSError:  # a read that failed may succeed next time: serve nothing now, remember nothing
                    self._cache = None
                    self._alarm("membership_view_refused", "OSError")
                    return None
                self._cache, self._refused = (key, ml), None
        # the committed epoch may have moved since the cache was filled (a gate, a restart): never serve another
        if self._keys.current_epoch() != ml.epoch:
            return None
        return ml

    def _load(self, c: Mapping[str, Any]) -> MemberList:
        ws = self._keys.workspace_id
        held = self._keys.ledger.get("member_list", str(c["list_seq"]))
        if held is None:
            raise _ViewRefused("no signature recorded")
        signed = {"o": c["list"], "sig": held[1]}
        ml = memberlist.check_member_list(signed, self._wsk, ws)
        if ml.list_seq != c["list_seq"] or ml.epoch != c["epoch"]:
            raise _ViewRefused("list_seq or epoch")
        if ml.wk_commit != c["wk_commit"] or ml.wk_commit != self._keys.wk_commit_of(ml.epoch):
            raise _ViewRefused("wk_commit")
        digest = list_hash_of(signed)
        if digest != c["list_hash"]:
            raise _ViewRefused("list_hash")
        members: dict[str, Member] = {}
        for entry in ml.members:
            if entry.person_id != self._person:
                raise _ViewRefused("another person's entry")  # P3: owner only, until P8
            if list(entry.scopes) != SCOPE_ORDER[: len(entry.scopes)] or not entry.scopes:
                raise _ViewRefused("scopes")
            rec = self._device(entry.device_id)
            if not isinstance(rec, Mapping) or rec.get("device_id") != entry.device_id:
                raise _ViewRefused("no device record")
            cert = rec.get("cert")
            o = certs.check_cert(cert, self._pk)
            if o["device_id"] != entry.device_id or o["person_id"] != entry.person_id:
                raise _ViewRefused("certificate")
            if not set(entry.scopes) <= set(o["scopes_max"]):
                raise _ViewRefused("scopes above the certificate")
            members[entry.device_id] = Member(
                device_id=entry.device_id, person_id=entry.person_id, scopes=tuple(entry.scopes), cert=dict(cert),
                pk_pub=self._pk,
            )  # fmt: skip
        return MemberList(epoch=ml.epoch, list_hash=digest, members=members)


class _ViewRefused(Exception):  # noqa: N818 - a verdict: the committed list does not verify
    pass
