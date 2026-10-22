"""Opening a shared item: NOT SPECIFIED yet.

protocol.md §10.2 fixes ``sealed`` only as "canonical base64url bytes". Spec §8a says the host seals each item to HXK
under a per-item key and that the item carries its WSK-signed state and the host-chain head, but the byte format is
not defined. The hub therefore stores ``sealed`` opaquely and every consumer (the web view, the phone) goes through
this one function, which fails until the phase-4 contract extension defines the format (V3-D23). Nothing else in the
hub may parse ``sealed``.

Fields the contract will need to define (reported with D9):
  * the seal: format version, ephemeral public key, AEAD nonce, ciphertext (per-item key sealed to HXK, with the
    purpose / recipient (hub_id) / object (workspace_id, item_id) bound in the AAD like the v2 §5.3 ``wk`` seal);
  * inside the plaintext: ``kind`` ("item" | "question" | "needs_you"), ``item_type``, ``title`` (one line),
    ``deadline_ms`` (needs-you summaries show type + title + deadline only), ``state`` (the WSK-signed item state),
    ``item_version`` (monotonic per item, for replace-on-newer), ``chain_head`` (host chain head, b64u(32)),
    ``item_hash`` (the digest a custodial event binds), ``issued_ms``.
"""

from __future__ import annotations

from typing import Any


class NotSpecified(NotImplementedError):
    """The format of ``sealed`` is not part of the contract yet."""


def open_shared_item(sealed: bytes) -> dict[str, Any]:
    raise NotSpecified("the sealed item format is not defined in protocol.md yet (phase-4 contract extension)")
