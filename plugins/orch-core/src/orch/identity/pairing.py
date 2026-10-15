"""Pure builders for R6 pairing (design 11A.15.10, H12c items 1-4): the epoch-key commitment, the two 6-character codes,
the pairing links and the pair request's meta.

Specs: orch-relay ``docs/protocol-v2.md`` §2.5 (labels), §4 (pins, SAS, ``cert_code``), §7.2 (``wk_commit``), §8.3
(links, the pair meta), §8.4 (the challenge). Vectors: the relay corpus sections ``wk_commit``, ``bridge.sas``,
``cert_request.cert_code``, ``bridge.links`` and the epoch-0 ``bridge.host_cases`` (``tests/identity/test_pairing.py``).

No I/O, no clock, no key held. Every function validates its inputs exactly and raises :class:`Refused` with code
``malformed`` (the protocol's code for a bad meta shape) on anything outside the contract; the message never carries a
secret.
"""

from __future__ import annotations

from orch import crypto
from orch.crypto.labels import L

from .errors import Refused

__all__ = ["MAX_EPOCH", "wk_commit"]

MAX_EPOCH = 2**32 - 1  # protocol §7.2: an epoch is a u32 in 1 .. 2^32-1 wherever it is parsed


def _bad(what: str) -> Refused:
    return Refused("malformed", what)


def _id(value: object, what: str) -> bytes:
    """A 16-byte id in its one text form, 32 lower-case hex characters (protocol §2.2)."""
    try:
        return crypto.unhex(value, 16)
    except crypto.EncodingError:
        raise _bad(what) from None


def _raw(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise _bad(what)
    return value


# --- 1. wk_commit (§7.2, G2b) ----------------------------------------------------------------------------------------


def wk_commit(workspace_id: str, epoch: int, wk: bytes) -> str:
    """``b64u(H("orch/v2/wk-commit|" || workspace_id || u32(epoch) || WK_epoch))``: the key commitment every member list
    at ``epoch`` carries. No suite byte (unlike the pins), as the protocol and the vectors have it."""
    ws = _id(workspace_id, "workspace_id")
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
        raise _bad("epoch")
    key = _raw(wk, 32, "wk")
    return crypto.b64u(crypto.sha256(L["h_wk_commit"] + ws + epoch.to_bytes(4, "big") + key))
