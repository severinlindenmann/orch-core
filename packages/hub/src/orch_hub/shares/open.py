"""Opening a shared item (protocol.md §10.2b): the one place that parses ``sealed``.

``sealed = b64u(u32(item_version) || wrap || ct)``: the per-item key ``IK`` is sealed to the hub's ``HXK`` (v2 §5.3,
purpose ``hub-share``), the content is the WSK-signed ``share.item`` under ``IK``. The hub opens it on every
``share_put`` (to check it, then stores the SEALED bytes) and again for the read API (the web view, the phone).
Both go through the contract's ``open_share_item`` (reference implementation and vectors ``share_item_*``), so the
text rule, the exact content per kind and the WSK signature are checked there and nowhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..contract import phase4 as P4
from ..contract.canon import b64u


class _Hxk:
    """What ``phase4.open_share_item`` needs of the hub's ``HXK``: ECDH and the public key. The private scalar stays
    inside :class:`orch_hub.keys.HubKeys` (``hxk_exchange``); this holds only the callback."""

    def __init__(self, hxk_pub: bytes, exchange: Callable[[bytes], bytes]) -> None:
        self._pub, self._ex = hxk_pub, exchange

    def exchange(self, _algorithm: Any, peer: Any) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._ex(peer.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    def public_key(self) -> _Hxk:
        return self

    def public_bytes(self, *_a: Any) -> bytes:
        return self._pub


def hxk_handle(hxk_pub: bytes, exchange: Callable[[bytes], bytes]) -> _Hxk:
    return _Hxk(hxk_pub, exchange)


def check_put(
    body: Any,
    *,
    hub_id: str,
    workspace_id: str,
    hxk: Any,
    wsk_pub: bytes,
    dev_mode: bool,
    held: Mapping[str, Any] | None,
    live_items: int,
) -> dict[str, Any]:
    """The hub's check of a ``share_put`` body, in the contract's order (size, wrap, signature, field set, hub,
    workspace, ids, content, dev marker, version rule, item cap). Returns ``{"item", "hash", "new"}``."""
    return P4.open_share_item(
        body,
        hub_id=hub_id,
        workspace_id=workspace_id,
        hxk_priv=hxk,
        wsk_pub=wsk_pub,
        held=held,
        live_items=live_items,
        dev_mode=dev_mode,
    )


def open_shared_item(
    sealed: bytes,
    *,
    hub_id: str,
    workspace_id: str,
    item_id: str,
    hxk: Any,
    wsk_pub: bytes,
    dev_mode: bool,
) -> dict[str, Any]:
    """Open and verify one stored item for a reader (the web view, the phone): the wrap under ``HXK``, the content
    under ``IK``, the WSK signature, the exact content per kind and the text rule. Returns ``{"item": <share.item o>,
    "signed": <the signed item, what ``item_hash`` and ``build_selective`` take>, "hash": "sha256:..."}``; raises ``Refused`` with the §10.2b code (``bad_wrap``, ``bad_signature``, ``malformed``,
    ``other_hub``, ``other_workspace``, ``too_large``, ``dev_refused``). The version rule and the cap are not applied."""
    r = check_put(
        {"item_id": item_id, "sealed": b64u(sealed)},
        hub_id=hub_id,
        workspace_id=workspace_id,
        hxk=hxk,
        wsk_pub=wsk_pub,
        dev_mode=dev_mode,
        held=None,
        live_items=0,
    )
    return {"item": r["item"], "signed": r["signed"], "hash": r["hash"]}
