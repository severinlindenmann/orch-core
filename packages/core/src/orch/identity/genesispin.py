"""The genesis pin's person path (orch-v2-host 11A.2, "Review binding of the new person-path kinds").

``PUT /v2/workspaces/{ws}/genesis`` carries a pin an owner device signs with its ``dk_sig`` (``sig_genesis_pin``,
protocol P3-D18): ``{"o": {v, suite, kind: "genesis_pin", workspace_id, wsk_pub, genesis}, "sig"}``, with the owner
device's certificate. This is a **non-event** material: it is no F1 event and appends nothing.

The signer never signs bytes the host supplies. The host sends *facts*: the workspace's own ``workspace.created`` event
and the relay origin. :func:`check_facts` recomputes everything from the event with the signer's own checks
(``members.check_genesis``, the event's ``workspace_id``, ``event_head``): the workspace id must be the one the signer
asked for, the genesis hash and the WSK key come from that event, the signing device must be the genesis device, and
nothing else is accepted (an exact field set). :func:`pin_o` then builds the signed object from those recomputed values.
The relay origin is shown for review only (it is not in the pin).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orch import canon, crypto
from orch.crypto.labels import L

from .errors import Refused
from .members import check_genesis

__all__ = ["OP", "Verified", "check_facts", "confirm_word", "pin_o", "pin_payload", "render", "sign_pin"]

OP = "relay.genesis_pin"
FACT_KEYS = frozenset({"v", "workspace_id", "genesis_event", "origin"})
_HEX32 = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Verified:
    """What the signer recomputed from the facts."""

    o: dict[str, Any]  # the pin object it will sign
    origin: str
    device_cert: dict[str, Any]
    device_pub: bytes
    owner_pk_pub: bytes


def pin_o(workspace_id: str, wsk_pub: bytes, genesis: str) -> dict[str, Any]:
    return {
        "v": 2, "suite": crypto.SUITE_ID, "kind": "genesis_pin", "workspace_id": workspace_id,
        "wsk_pub": crypto.b64u(wsk_pub), "genesis": genesis,
    }  # fmt: skip


def pin_payload(o: Mapping[str, Any]) -> bytes:
    """The signing bytes: ``"orch/v2/sig/genesis-pin|" || cj(o)``."""
    return L["sig_genesis_pin"] + canon.dumps(dict(o))


def check_facts(facts: object, *, workspace_id: str, device_pub: bytes | None = None) -> Verified:
    """Recompute the pin from ``facts``; :class:`Refused` (``malformed`` or ``genesis.*``) on any difference.
    ``workspace_id`` is the one the signer asked about (its own registry). ``device_pub`` (when given) is the public
    key of the signing device: it must be the genesis device's, whose certificate the relay checks the pin against."""
    if not isinstance(facts, Mapping) or set(facts) != FACT_KEYS or facts.get("v") != 1:
        raise Refused("malformed", "genesis pin facts: exact field set")
    if not _HEX32.fullmatch(workspace_id) or facts["workspace_id"] != workspace_id:
        raise Refused("malformed", "the facts are for another workspace")
    event = facts["genesis_event"]
    if not isinstance(event, Mapping) or event.get("workspace_id") != workspace_id:
        raise Refused("malformed", "the genesis event is of another workspace")
    verified = check_genesis(event)  # §5.11, in order: owner, delegation, host sig, device certificate, signature
    from orch.canon import HashError, event_head

    try:
        genesis = event_head(event)
    except (HashError, KeyError, TypeError):
        raise Refused("malformed", "the genesis event has no hash") from None
    origin = facts["origin"]
    if type(origin) is not str or not origin.isascii() or not origin.isprintable() or len(origin) > 256:
        raise Refused("malformed", "origin")
    cert_o = verified["device_cert"]  # checked by check_genesis: signed by the owner, with operate
    cert = event["device_cert"]  # the signed certificate {o, sig} the relay checks the pin against
    dk = crypto.unb64u(cert_o["dk_sig_pub"], crypto.PUB_LEN)
    if device_pub is not None and device_pub != dk:
        raise Refused("genesis.bad_device_cert", "this key is not the workspace's genesis device")
    wsk = crypto.unb64u(event["wsk_pub"], crypto.PUB_LEN)
    return Verified(pin_o(workspace_id, wsk, genesis), origin, dict(cert), dk, verified["owner_pk_pub"])


def confirm_word(workspace_id: str) -> str:
    return f"{workspace_id[-8:]}/GENESIS"


def render(v: Verified, *, name: str) -> str:
    """The review the person reads, from the recomputed values only (never host text)."""
    o = v.o
    return (
        f"workspace:   {name} ({o['workspace_id']})\n"
        f"action:      publish the workspace's genesis pin to the relay (relay.genesis_pin)\n"
        f"relay:       {v.origin}\n"
        f"genesis:     {o['genesis']}\n"
        f"workspace key: {o['wsk_pub']}\n"
        f"signed by:   your genesis device d_{v.device_cert['o']['device_id']}\n"
        "The relay will accept this workspace's checkpoints only under this genesis and this key.\n"
    )


def sign_pin(backend: Any, key_id: str, v: Verified) -> dict[str, Any]:
    """The signed pin, from the recomputed object only. ``backend`` signs with the device key ``key_id``."""
    if bytes(backend.public_key(key_id)) != v.device_pub:
        raise Refused("genesis.bad_device_cert", "this key is not the workspace's genesis device")
    sig = backend.sign(key_id, pin_payload(v.o), action="relay.genesis_pin (publish the workspace genesis pin)")
    return {"o": v.o, "sig": crypto.b64u(sig)}
