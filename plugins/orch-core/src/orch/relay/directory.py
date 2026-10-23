"""R1 directory calls: the card, the genesis pin, and readers that verify what they return (contract 11A.1 R1 row).

* ``PUT card``: the first card goes without a session (P3-D4, the only unfenced workspace write); once the relay holds
  a card a session-less PUT is 401, and the card is PUT again under W(g). The card is checked locally first (proto
  §7.1 under the owner's person key, this workspace, this WSK, ``relay_url`` = the stored origin): the host never
  sends a card it would itself refuse.
* ``GET card`` returns the card only if it verifies under the owner's person key and names this workspace and WSK.
* ``GET members`` returns the signed member list only if the WSK signature verifies and it names this workspace. Its
  field set, scopes and persons are R6's checks (11A.1: R6 owns how the member list is verified).
* ``GET checkpoint`` returns the held (or named) checkpoint only if: the WSK signature and schema verify
  (``store.checkpoints.verify_object``), it is a workspace checkpoint of this workspace with the pinned genesis, the
  requested hash matches, and the genesis pin verifies under an owner device certificate (``check_cert`` under the
  owner's person key, ``operate`` in its scopes) and binds this workspace, this WSK and the pinned genesis. Anything
  else is :class:`RelayMisbehaving` (alarm ``relay_misbehaving``): a relay claim is never evidence by itself.
* ``PUT genesis`` (P3-D18, W(g)): the pin from the person path, checked the same way before it is sent.

A 404 on a read is ``None`` (nothing held yet). Response caps: card 64 KiB, reads 4 MiB (11A.7).

**Authenticity, not currency (review L6).** These readers prove that an object was signed by the right key for this
workspace. They do not prove it is the newest: a relay can serve an older card (a lower ``card_seq`` than
``link.json`` holds), an older WSK-signed member list (a lower ``list_seq`` or epoch), or a replayed older checkpoint H,
and ``transitions_head`` is an unauthenticated relay claim. The callers compare against local state: R3 classifies H
against the logs and the publication history (11A.5), R6 compares the member list with the host's own, and the card
reader's caller compares ``card_seq`` with ``link.json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orch import canon, crypto
from orch.identity.certs import check_cert, person_scope_ok
from orch.identity.errors import Refused
from orch.net.http import Transport
from orch.store.checkpoints import verify_object as verify_checkpoint
from orch.wsproto import card as cards
from orch.wsproto import wire

from .retry import RelayRefused
from .session import RelayMisbehaving, Session

__all__ = ["CARD_CAP", "READ_CAP", "Directory", "VerifiedCheckpoint"]

CARD_CAP = 64 * 1024
READ_CAP = 4 << 20
READ_DEADLINE_S = 30.0
_PIN_FIELDS = frozenset({"v", "suite", "kind", "workspace_id", "wsk_pub", "genesis"})
_HASH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789:")


@dataclass(frozen=True)
class VerifiedCheckpoint:
    checkpoint: dict[str, Any]  # the signed {o, sig}, as served
    hash: str  # h_checkpoint(o)
    n: int
    transitions_head: int
    pin: dict[str, Any]
    cert: dict[str, Any]


def _quote(value: str) -> str:
    """Percent-encode a query value (RFC 3986 unreserved characters kept)."""
    out = []
    for b in value.encode("utf-8"):
        c = chr(b)
        out.append(c if c.isascii() and (c.isalnum() or c in "-._~") else f"%{b:02X}")
    return "".join(out)


class Directory:
    def __init__(
        self, session: Session, *, owner_pk_pub: bytes, wsk_pub: bytes, transport: Transport | None = None
    ) -> None:
        """``transport`` is the calling thread's own connection (11A.7: one per thread; R3's publisher thread reads
        through its own); None uses the session's."""
        self._s = session
        self._t = transport
        self._owner = crypto.validate_public_key(owner_pk_pub)
        self._wsk = crypto.validate_public_key(wsk_pub)
        self._ws = session.workspace_id
        self._base = f"/v2/workspaces/{self._ws}"

    # -- card -----------------------------------------------------------------------------------------------------

    def check_own_card(self, c: object) -> dict[str, Any]:
        try:
            o = cards.verify_card(c, self._owner)
        except cards.CardRefused as e:
            raise RelayMisbehaving(f"card refused: {e.code}") from None
        if o["workspace_id"] != self._ws or o["wsk_pub"] != crypto.b64u(self._wsk):
            raise RelayMisbehaving("card of another workspace or key")
        return o

    def put_card(self, c: Mapping[str, Any], *, first: bool) -> int | None:
        """PUT the card; ``first`` sends it without a session and returns ``None`` when the relay already holds a card
        (401), so the caller PUTs it again under W(g). Returns the relay's ``card_seq`` otherwise."""
        try:
            o = self.check_own_card(dict(c))
        except RelayMisbehaving as e:
            raise ValueError(f"refusing to publish an invalid card ({e})") from None
        if o["relay_url"] != self._s.origin:
            raise ValueError("refusing to publish a card for another relay origin")
        route = "PUT /v2/workspaces/{ws}/card"
        try:
            obj = self._s.call(
                "PUT", f"{self._base}/card", route, body={"card": dict(c)}, auth=not first, holder=not first,
                request_cap=CARD_CAP, keys={"card_seq"}, transport=self._t,
            )  # fmt: skip
        except RelayRefused as e:
            if first and e.status == 401 and e.code == "unauthenticated":
                return None
            raise
        if obj["card_seq"] != o["card_seq"]:
            raise RelayMisbehaving("card_seq answered differs")
        return o["card_seq"]

    def get_card(self) -> dict[str, Any] | None:
        obj = self._read(f"{self._base}/card", "GET /v2/workspaces/{ws}/card", {"card"}, CARD_CAP)
        if obj is None:
            return None
        self.check_own_card(obj["card"])
        return obj["card"]

    # -- member list ----------------------------------------------------------------------------------------------

    def get_members(self) -> dict[str, Any] | None:
        obj = self._read(f"{self._base}/members", "GET /v2/workspaces/{ws}/members", {"member_list"}, READ_CAP)
        if obj is None:
            return None
        o = wire.verify_signed(self._wsk, obj["member_list"], "member_list")
        if o is None or o.get("workspace_id") != self._ws:
            raise RelayMisbehaving("member list does not verify under this workspace's key")
        return obj["member_list"]

    # -- checkpoint and genesis -----------------------------------------------------------------------------------

    def check_genesis_pin(self, pin: object, cert: object, genesis: str) -> None:
        try:
            cert_o = check_cert(cert, self._owner)
        except Refused:
            raise RelayMisbehaving("genesis pin certificate") from None
        if not person_scope_ok(cert_o, needs_operate=True):
            raise RelayMisbehaving("genesis pin certificate scopes")
        dk = crypto.unb64u(cert_o["dk_sig_pub"], crypto.PUB_LEN)
        o = wire.verify_signed(dk, pin, "genesis_pin")
        if o is None or set(o) != _PIN_FIELDS:
            raise RelayMisbehaving("genesis pin signature or field set")
        if o["workspace_id"] != self._ws or o["wsk_pub"] != crypto.b64u(self._wsk) or o["genesis"] != genesis:
            raise RelayMisbehaving("genesis pin binds another workspace, key or genesis")

    def get_checkpoint(self, *, genesis: str, hash: str | None = None) -> VerifiedCheckpoint | None:
        """The relay's held checkpoint H (or the immutable checkpoint ``hash``), verified; ``genesis`` is the
        workspace's pinned genesis hash from the host's own log."""
        target = f"{self._base}/checkpoint"
        if hash is not None:
            if type(hash) is not str or not set(hash) <= _HASH_CHARS or len(hash) > 80:
                raise ValueError("checkpoint hash")
            target += f"?hash={_quote(hash)}"
        obj = self._read(target, "GET /v2/workspaces/{ws}/checkpoint", {"genesis", "checkpoint", "transitions_head"},
                         READ_CAP)  # fmt: skip
        if obj is None:
            return None
        g, cp, head = obj["genesis"], obj["checkpoint"], obj["transitions_head"]
        if type(g) is not dict or set(g) != {"pin", "cert"} or type(head) is not int or head < 0:
            raise RelayMisbehaving("checkpoint answer shape")
        if not verify_checkpoint(self._wsk, cp):
            raise RelayMisbehaving("checkpoint does not verify under this workspace's key")
        o = cp["o"]
        if o.get("kind") != "workspace_checkpoint" or o.get("workspace_id") != self._ws or o.get("genesis") != genesis:
            raise RelayMisbehaving("checkpoint of another workspace or genesis")
        h = canon.checkpoint_hash(o)
        if hash is not None and h != hash:
            raise RelayMisbehaving("checkpoint is not the one requested")
        self.check_genesis_pin(g["pin"], g["cert"], genesis)
        return VerifiedCheckpoint(cp, h, o["n"], head, g["pin"], g["cert"])

    def put_genesis(self, pin: Mapping[str, Any], cert: Mapping[str, Any], *, genesis: str) -> None:
        try:
            self.check_genesis_pin(dict(pin), dict(cert), genesis)
        except RelayMisbehaving as e:
            raise ValueError(f"refusing to publish an invalid genesis pin ({e})") from None
        self._s.call(
            "PUT", f"{self._base}/genesis", "PUT /v2/workspaces/{ws}/genesis",
            body={"pin": dict(pin), "cert": dict(cert)}, holder=True, request_cap=CARD_CAP, keys=set(),
            transport=self._t,
        )  # fmt: skip

    # -- reads ----------------------------------------------------------------------------------------------------

    def _read(self, target: str, route: str, keys: set[str], cap: int) -> dict[str, Any] | None:
        try:
            return self._s.call("GET", target, route, cap=cap, deadline_s=READ_DEADLINE_S, keys=keys, transport=self._t)
        except RelayRefused as e:
            if e.status == 404 and e.code == "not_found":
                return None
            raise
