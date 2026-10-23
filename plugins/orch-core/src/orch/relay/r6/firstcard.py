"""R6a: the first link's source (11A.15.5 "the first card", conflict C6; R5's ``LinkSource`` seam): the card with
``WXK``/``CK`` custody, the owner's ``PK`` from the genesis delegation, and the genesis pin's person path (not built:
refused until it is).

The card comes from :meth:`orch.identity.epochs.MembershipKeys.first_card` and is identical on every call (R1
persists it again in ``link-card.json``; both copies are the same bytes). Before anything is drawn or signed the
convenience check runs (11A.6, owner rule Q4 of 11A.11): a workspace with convenience history or the flag never gets
a card, so nothing that exposes it to new readers exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from orch import crypto
from orch.identity import certs, members
from orch.identity.epochs import CustodyRefused, MembershipKeys
from orch.identity.errors import Refused
from orch.wsproto.sign import RelaySigner

__all__ = ["GenesisPathMissing", "R6LinkSource"]


class GenesisPathMissing(Exception):  # noqa: N818 - a seam that is not built yet
    """The genesis pin's person path (``relay.genesis_pin``, 11A.2) is not built: no pin can be made."""


class R6LinkSource:
    """``card()``, ``genesis()`` and ``owner_pk_pub()`` for R1's first link (R5 wraps it in ``LinkSteps``).

    Build it with :meth:`from_genesis` (the workspace's ``workspace.created`` event, checked with
    ``identity.members.check_genesis``) or :meth:`from_delegation` (a signed delegation checked under the owner key).
    ``blocked()`` is the convenience check (``None`` = clear); ``genesis_pin()`` the person path, when it exists."""

    def __init__(
        self,
        keys: MembershipKeys,
        signer: RelaySigner,
        *,
        delegation: Mapping[str, Any],
        owner_pk_pub: bytes,
        part: Mapping[str, Any],
        now_ms: Callable[[], int],
        blocked: Callable[[], str | None],
        genesis_pin: Callable[[], tuple[Mapping[str, Any], Mapping[str, Any], str]] | None = None,
    ) -> None:
        try:
            d = certs.check_delegation(dict(delegation), owner_pk_pub)
        except Refused as e:
            raise CustodyRefused(f"genesis delegation: {e.code}") from None
        if d["workspace_id"] != keys.workspace_id or d["wsk_pub"] != crypto.b64u(signer.public_key):
            raise CustodyRefused("the delegation names another workspace or WSK")
        if signer.link.workspace_id != keys.workspace_id:
            raise CustodyRefused("the signer is another workspace's")
        self._keys, self._signer = keys, signer
        self._delegation = dict(delegation)
        self._pk = bytes(owner_pk_pub)
        self._part = dict(part)
        self._now_ms, self._blocked, self._pin = now_ms, blocked, genesis_pin

    @classmethod
    def from_genesis(
        cls, keys: MembershipKeys, signer: RelaySigner, *, genesis_event: Mapping[str, Any], **kw: Any
    ) -> R6LinkSource:
        """From the workspace log's genesis: ``PK`` and the delegation as ``check_genesis`` verified them (§5.11),
        never from the relay or the account."""
        try:
            facts = members.check_genesis(genesis_event)
        except Refused as e:
            raise CustodyRefused(f"genesis: {e.code}") from None
        return cls(keys, signer, delegation=genesis_event["delegation"], owner_pk_pub=facts["owner_pk_pub"], **kw)

    def __repr__(self) -> str:
        return f"R6LinkSource(workspace={self._keys.workspace_id})"

    def owner_pk_pub(self) -> bytes:
        return self._pk

    def card(self) -> Mapping[str, Any]:
        why = self._blocked()
        if why is not None:
            raise CustodyRefused(f"convenience: {why}")
        return self._keys.first_card(
            self._signer.card, delegation=self._delegation, owner_pk_pub=self._pk, wsk_pub=self._signer.public_key,
            relay_url=self._signer.link.origin, part=self._part, issued_ms=self._now_ms(),
        )  # fmt: skip

    def card_accepted(self) -> None:
        """The relay accepted the first ``PUT card`` (R5 calls it before the first publication, B1): the pending card
        becomes the accepted one (11A.15.5, D:9423). Idempotent: nothing pending is a no-op (a resumed link)."""
        pending = self._keys.head()["card"]["pending_card_seq"]
        if pending is not None:
            self._keys.card_accepted(pending)

    def genesis(self) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        if self._pin is None:
            raise GenesisPathMissing("the genesis pin's person path (relay.genesis_pin) is not built")
        return self._pin()
