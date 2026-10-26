"""The hub checks the model's ``Verifier`` injects (ticket-format 15.8), over this package's own verifiers.

``orch.model`` is pure (D79): replay and admission call ``verify_custodial_sigs``, ``verify_hub_pin`` and
``verify_hub_actor`` on whatever verifier they are given. :class:`HubModelChecks` is the host's implementation,
built only from :mod:`.wire`, :func:`.snapshot.verify_snapshot` and :func:`.custodial.verify_custodial` with
``now_ms`` = the line's ``at`` (never a clock). A ``HubRefused`` becomes the contract code the model maps (15.3).
Fail closed: any malformed input is a refusal, never an exception."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u

from . import custodial as C
from . import wire
from .errors import HubRefused
from .snapshot import verify_snapshot

__all__ = ["HubModelChecks"]

#: the model applies the D66 factor floor itself (``policies.factor_floor``, 15.4); the contract verifier below gets
#: the weakest value, so the floor is judged in exactly one place
_NO_FLOOR = "password+totp"


class HubModelChecks:
    """Mixin with the three hub methods of the model's ``Verifier``."""

    def verify_custodial_sigs(self, event: Mapping[str, Any], *, workspace_id: str, log: str) -> bool:
        """The line check of 15.3: ``sig`` under ``uk_cert.o.uk_pub`` and ``channel_proof`` under the channel key."""
        try:
            body = {k: v for k, v in event.items() if k not in C.NOT_SIGNED}
            uk = unb64u(event["uk_cert"]["o"]["uk_pub"], 65)
            ch = unb64u(event["channel_cert"]["o"]["key_pub"], 65)
            ok = S.verify(
                uk,
                unb64u(event["sig"], S.SIG_LEN),
                C.custodial_label(event["type"]) + C._context(workspace_id, log, body),  # noqa: SLF001
            )
            return ok and S.verify(
                ch,
                unb64u(event["channel_proof"], S.SIG_LEN),
                C.SIG_CHANNEL_PROOF + C.event_digest(workspace_id, log, body),
            )
        except Exception:  # noqa: BLE001 - fail closed on anything unreadable
            return False

    @staticmethod
    def _held(card: Any, root: str, at_ms: int, dev: bool, revoked: frozenset[str]) -> dict[str, Any]:
        return wire.verify_card(
            card, now_ms=at_ms, dev_mode=dev, pinned_root=unb64u(root, 65), revoked_op_certs=revoked
        )

    def verify_hub_pin(
        self,
        obj: Mapping[str, Any],
        *,
        root: str,
        card: Any,
        revoked: frozenset[str],
        snapshot: Any,
        workspace_id: str,
        at_ms: int,
        dev: bool,
    ) -> str | None:
        """15.2 rules 2-4 for one pinned object."""
        try:
            kind = obj["o"]["kind"]
            pinned = unb64u(root, 65)
            if kind == "hub.op_revoked":
                wire.verify_op_revoked(obj, pinned)
            elif kind == "hub.card":
                old = None
                if card is not None:  # the card as it was when pinned (no revocation, its own issue time)
                    old = wire.verify_card(
                        card, now_ms=card["op_cert"]["o"]["issued_ms"] + 1, dev_mode=dev, pinned_root=pinned
                    )
                wire.verify_card(
                    obj, now_ms=at_ms, dev_mode=dev, pinned_root=pinned, revoked_op_certs=revoked, held=old
                )
            elif kind == "hub.snapshot":
                if card is None:
                    return "no_card"
                held = self._held(card, root, at_ms, dev, revoked)
                verify_snapshot(
                    obj, held, workspace_id=workspace_id, dev_mode=dev,
                    pinned=snapshot["o"] if snapshot else None, revoked_op_certs=revoked,
                )  # fmt: skip
            else:
                return "malformed"
        except HubRefused as e:
            return e.code
        except Exception:  # noqa: BLE001
            return "malformed"
        return None

    def verify_hub_actor(
        self,
        event: Mapping[str, Any],
        *,
        workspace_id: str,
        log: str,
        root: str,
        card: Any,
        revoked: frozenset[str],
        snapshot: Any,
        revoked_channels: frozenset[str],
        at_ms: int,
        dev: bool,
        mode: str,
        member_role: str | None,
    ) -> str | None:
        """15.3 steps 2, 3 and 5 for a log line (the delivered event plus host fields)."""
        try:
            if card is None:
                return "no_card"
            if snapshot is None:
                return "unknown_snapshot"
            held = self._held(card, root, at_ms, dev, revoked)
            snap = {**snapshot["o"], "revoked_channels": sorted(revoked_channels)}
            user = event["actor"]["id"][2:]
            delivered = {k: v for k, v in event.items() if k not in C.HOST_FIELDS}
            C.verify_custodial(
                delivered, workspace_id=workspace_id, log=log, held=held, snapshot=snap, now_ms=at_ms, dev_mode=dev,
                mode=mode, granted_users=frozenset({user}) if member_role is not None else frozenset(),
                member_roles={user: member_role} if member_role is not None else {},
                revoked_op_certs=revoked, min_factor=_NO_FLOOR, check_item=False,
            )  # fmt: skip
        except HubRefused as e:
            return e.code
        except Exception:  # noqa: BLE001
            return "malformed"
        return None

    def verify_hub_owner_proof(
        self,
        event: Mapping[str, Any],
        *,
        workspace_id: str,
        actor_person_id: str,
        pk_pub: str | None,
        prior: Any,
        at_ms: int,
        dev: bool,
    ) -> str | None:
        """Hub contract 9.6 "Readers" (#1198 C9) over :func:`wire.check_recorded_owner_proof`: the proof, its place
        after the prior hub event, the host-signed append time, the dev marker. Fail closed."""
        try:
            keys = wire.JOINED_KEYS if event["type"] == "hub.joined" else wire.LEFT_KEYS
            payload = {k: event[k] for k in (*keys, "owner_proof") if k in event}
            wire.check_recorded_owner_proof(
                event["type"], payload, at_ms=at_ms, prior=prior, workspace_id=workspace_id,
                actor_person_id=actor_person_id, pk_pub=None if pk_pub is None else unb64u(pk_pub, 65),
                dev_mode=dev,
            )  # fmt: skip
        except HubRefused as e:
            return e.code
        except Exception:  # noqa: BLE001
            return "malformed"
        return None
