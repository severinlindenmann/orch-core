"""The genesis pin on the host (11A.2, 11A.5 steps 2 and 3, B1): the facts the signer checks, the signed pin the host
verifies and keeps, and the ``genesis_pin`` of ``relayr6.link_source``.

* :func:`facts` ``(service)``: the workspace's own ``workspace.created`` event and the relay origin of ``link.json``.
  The signer recomputes the pin from them (:mod:`orch.identity.genesispin`); the host's claim is never trusted.
* :func:`submit` ``(service, pin)``: recompute the expected pin from the host's own state, require the submitted pin to
  be exactly that object, verify its signature under the genesis device's ``dk_sig``, and keep it with the device
  certificate in ``hosts/<ws>/relay/genesis-pin.json`` (private, atomic). Nothing is appended.
* :func:`pin_source` ``(service)``: what ``R6LinkSource.genesis()`` returns: ``(pin, cert, genesis)`` of that file, or
  :class:`orch.relay.r6.firstcard.GenesisPathMissing` while the person has not signed (the worker then stops with that
  class, status names it; after ``orch host relay genesis-pin`` the link resumes at the next start). R1 keeps the
  result in ``link-genesis.json`` (write-once) before the first ``PUT genesis``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orch import crypto
from orch.identity import genesispin
from orch.identity.errors import Refused

from .errors import HostError

__all__ = ["facts", "pin_path", "pin_source", "submit"]


def pin_path(service: Any) -> Path:
    from .relaylink import relay_dir

    return relay_dir(service.state_dir, service.workspace_id) / "genesis-pin.json"


def facts(service: Any) -> dict[str, Any]:
    from .custody import read_json
    from .relaylink import link_path

    link = read_json(link_path(service.state_dir, service.workspace_id), service.host_uid)
    if (
        not isinstance(link, dict)
        or link.get("state") not in ("linking", "linked")
        or type(link.get("origin")) is not str
    ):
        raise HostError("relay.link_mismatch", "this workspace has no relay link to pin")
    store = service.store
    event = store.genesis_event() if store is not None else None
    if event is None:
        raise HostError("workspace.unavailable", "the workspace has no genesis")
    return {"v": 1, "workspace_id": service.workspace_id, "genesis_event": event, "origin": link["origin"]}


def _verified(service: Any) -> genesispin.Verified:
    try:
        return genesispin.check_facts(facts(service), workspace_id=service.workspace_id)
    except Refused as exc:
        raise HostError("workspace.unavailable", f"the workspace genesis does not verify: {exc.code}") from None


def submit(service: Any, pin: object) -> dict[str, Any]:
    from .custody import write_private

    v = _verified(service)
    sig = pin.get("sig") if isinstance(pin, dict) and set(pin) == {"o", "sig"} else None
    if sig is None or pin["o"] != v.o:  # type: ignore[index]
        raise HostError("invalid.input", "the signed pin is not this workspace's genesis pin")
    try:
        ok = crypto.verify(v.device_pub, crypto.unb64u(sig, 64), genesispin.pin_payload(v.o))
    except (crypto.EncodingError, TypeError):
        ok = False
    if not ok:
        raise HostError("invalid.input", "the genesis pin's signature does not verify under the genesis device")
    write_private(pin_path(service), {"v": 1, "pin": {"o": v.o, "sig": sig}, "cert": v.device_cert,
                                      "genesis": v.o["genesis"]})  # fmt: skip
    return {"workspace_id": service.workspace_id, "genesis": v.o["genesis"]}


def pin_source(service: Any) -> Any:
    """``R6LinkSource``'s ``genesis_pin`` callable."""
    from orch.relay.r6.firstcard import GenesisPathMissing

    from .custody import read_json

    def pin() -> tuple[Any, Any, str]:
        rec = read_json(pin_path(service), service.host_uid)
        if rec is None:
            raise GenesisPathMissing("sign the genesis pin first: orch host relay genesis-pin --workspace W")
        v = _verified(service)
        if (
            not isinstance(rec, dict) or rec.get("pin", {}).get("o") != v.o or rec.get("genesis") != v.o["genesis"]
            or rec.get("cert") != v.device_cert
        ):  # fmt: skip
            raise GenesisPathMissing("the stored genesis pin is not this workspace's: sign it again")
        return rec["pin"], rec["cert"], rec["genesis"]

    return pin
