"""The push object (orch-relay protocol-v2 §9 and §19.10; orch-v2-host 11A.16.2 and 11A.16.4; H13-0).

::

    outer   = {"v":2, "ws": hex, "epoch": e, "c": b64u(SALTED-AEAD(K_push(e), "orch/v2/push-msg",
                                                       "orch/v2/push-aad|" || suite || ws || u32(e), cj(inner)))}
    inner   = {"p": payload, "sig": b64u(Sign(WSK, "orch/v2/sig/push|" || suite || ws || u32(e) || cj(payload)))}
    payload = {"kind", "id", "label", "ts_ms"}
    K_push  = HKDF(WK_e, "", "orch/v2/push|" hex(ws) "|" dec(e))

Pure functions. The builder never holds a signing key: the caller (``_PushSealer.seal_push``, H13-1) gets the signature
from ``RelaySigner.push`` over :func:`push_signed_bytes` and passes it in, with ``K_push`` of the snapshot's epoch.

* **Kinds.** :data:`PUSH_KINDS` is the protocol's set (the opener's); the host seals only :data:`HOST_PUSH_KINDS`
  (D2: ``question``, ``question.closed``, ``drop.new``; B10 Part B adds ``ticket.update`` for the phone-approval
  hint; ``join`` and ``peer.ticket`` never).
* **Label.** A fixed point of ``clean_label``, at most 80 code points, not ending in a space, then padded with U+0020
  to exactly 80 code points (11A.16.2 "Label padding"; the NSE trims trailing spaces), so every label has one length.
* **``ts_ms``** is passed in (the caller's ``max(now_ms, push_ts.last + 1)``, Q-H3); an integer in ``[0, 2^53)``.
* **Size.** ``cj(outer)`` is at most 3,072 bytes; the worst case (a 128-byte id, 80 four-byte code points) fits.
* :func:`open_push` is the service worker's steps 1 to 6 (§9) for tests and the host's own verification; step 7 (the
  per-``(ws, id)`` replay memory) is the caller's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag

from orch import canon

from . import suite
from ._closed import close_module
from .encoding import EncodingError, b64u, unb64u, unhex
from .labels import LABELS, L

__all__ = [
    "HOST_PUSH_KINDS",
    "MAX_AGE_MS",
    "MAX_LABEL",
    "MAX_PUSH",
    "PUSH_KINDS",
    "SKEW_MS",
    "PushError",
    "PushKeys",
    "k_push",
    "open_push",
    "pad_label",
    "push_aad",
    "push_payload",
    "push_signed_bytes",
    "seal_push",
]

PUSH_KINDS = ("question", "question.closed", "ticket.update", "drop.new", "peer.ticket", "join")
#: B10 Part B: ``ticket.update`` is the phone-approval wake ("something needs you"), sent only for a phone request hint
HOST_PUSH_KINDS = ("question", "question.closed", "drop.new", "ticket.update")
MAX_PUSH = 3072
MAX_LABEL = 80
MAX_ID_BYTES = 128
MAX_AGE_MS = 24 * 3600 * 1000
SKEW_MS = 300_000
MAX_SAFE_INT = (1 << 53) - 1
_PAYLOAD = frozenset({"kind", "id", "label", "ts_ms"})
_OUTER = frozenset({"v", "ws", "epoch", "c"})


class PushError(Exception):
    """A refused push input or a dropped push. ``why`` is the closed class: for the opener the service worker's
    ``size``, ``shape``, ``no_key``, ``tag``, ``payload``, ``signature``, ``stale``; for the builders ``input``. The one
    error of this module (every public function is a closed boundary); not a ``ValueError``."""

    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(f"{why}: {detail}" if detail else why)
        self.why = why


@dataclass(frozen=True)
class PushKeys:
    """What an opener holds per workspace: the pinned ``wsk_pub`` and the ``WK`` per epoch it holds."""

    wsk_pub: bytes
    wk: Mapping[int, bytes]


def _ws(ws: object) -> bytes:
    if type(ws) is not bytes or len(ws) != 16:
        raise PushError("input", "workspace id is 16 bytes")
    return ws


def _epoch(epoch: object) -> int:
    if type(epoch) is not int or not 0 <= epoch < 2**32:
        raise PushError("input", "epoch is a u32")
    return epoch


def k_push(wk: bytes, ws: bytes, epoch: int) -> bytes:
    if type(wk) is not bytes or len(wk) != 32:
        raise PushError("input", "WK is 32 bytes")
    return suite.hkdf(wk, b"", f"{LABELS['kdf_push']}|{_ws(ws).hex()}|{_epoch(epoch)}".encode("ascii"))


def push_aad(ws: bytes, epoch: int) -> bytes:
    return L["aad_push"] + bytes([suite.SUITE_ID]) + _ws(ws) + _epoch(epoch).to_bytes(4, "big")


def _check_payload(payload: object, kinds: tuple[str, ...], *, padded: bool) -> dict:
    if type(payload) is not dict or set(payload) != _PAYLOAD:
        raise PushError("input", "payload is exactly {kind, id, label, ts_ms}")
    kind, pid, label, ts = payload["kind"], payload["id"], payload["label"], payload["ts_ms"]
    if kind not in kinds:
        raise PushError("input", "kind")
    if type(pid) is not str or not pid or not pid.isascii() or len(pid) > MAX_ID_BYTES:
        raise PushError("input", "id is ASCII, 1 to 128 bytes")
    if type(label) is not str or len(label) > MAX_LABEL:
        raise PushError("input", "label")
    if padded and pad_label(label.rstrip(" ")) != label:
        raise PushError("input", "label is not padded to 80 code points")
    if type(ts) is not int or not 0 <= ts <= MAX_SAFE_INT:
        raise PushError("input", "ts_ms")
    return payload


def pad_label(label: str) -> str:
    """A label as the host sends it: a non-empty fixed point of ``clean_label`` that does not end in a space, padded
    with U+0020 to exactly :data:`MAX_LABEL` code points."""
    from orch.identity.errors import Refused
    from orch.identity.pairing import clean_label  # identity imports crypto; resolved at call time

    if type(label) is not str or not label or label.endswith(" ") or len(label) > MAX_LABEL:
        raise PushError("input", "label")
    try:
        fixed = clean_label(label) == label
    except Refused:  # a lone surrogate
        fixed = False
    if not fixed:
        raise PushError("input", "label is not a fixed point of clean_label")
    return label + " " * (MAX_LABEL - len(label))


def push_payload(kind: str, id: str, label: str, ts_ms: int) -> dict:  # noqa: A002 - the protocol's field name
    """The host's payload (D2 kinds, a padded label), checked."""
    return _check_payload(
        {"kind": kind, "id": id, "label": pad_label(label), "ts_ms": ts_ms}, HOST_PUSH_KINDS, padded=True
    )


def push_signed_bytes(ws: bytes, epoch: int, payload: dict) -> bytes:
    """What ``RelaySigner.push`` signs with ``WSK`` (label ``sig_push``)."""
    _check_payload(payload, PUSH_KINDS, padded=False)
    return L["sig_push"] + bytes([suite.SUITE_ID]) + _ws(ws) + _epoch(epoch).to_bytes(4, "big") + canon.dumps(payload)


def _seal_push(
    k: bytes,
    ws: bytes,
    epoch: int,
    payload: dict,
    sig: bytes,
    *,
    kinds: tuple[str, ...] = HOST_PUSH_KINDS,
    padded: bool = True,
) -> bytes:
    """The sealer; :func:`seal_push` is it with the host's kinds and padding. The salt is drawn by
    :func:`orch.crypto.suite.salted_seal` (tests fix it there, never through a seam here)."""
    _check_payload(payload, kinds, padded=padded)
    if type(sig) is not bytes or len(sig) != suite.SIG_LEN:
        raise PushError("input", "signature is 64 bytes")
    if type(k) is not bytes or len(k) != 32:
        raise PushError("input", "K_push is 32 bytes")
    pt = canon.dumps({"p": payload, "sig": b64u(sig)})
    c = suite.salted_seal(k, L["kdf_push_msg"], push_aad(ws, epoch), pt)
    out = canon.dumps({"v": 2, "ws": ws.hex(), "epoch": epoch, "c": b64u(c)})
    if len(out) > MAX_PUSH:
        raise PushError("input", "cj(outer) is above 3,072 bytes")
    return out


def seal_push(k_push_e: bytes, ws: bytes, epoch: int, payload: dict, sig: bytes) -> bytes:
    """``cj(outer)`` for a host payload (:func:`push_payload`) and its ``WSK`` signature, under ``K_push`` of
    ``epoch``, with a fresh 16-byte salt. The bytes the host persists and sends (``outer`` of §19.10)."""
    return _seal_push(k_push_e, ws, epoch, payload, sig)


def open_push(raw: bytes, keys: Mapping[str, PushKeys], now_ms: int) -> dict:
    """The payload, or :class:`PushError` with the service worker's reason (§9 steps 1 to 6). ``keys`` maps the
    workspace id (hex) to what the opener holds."""
    if type(raw) is not bytes:
        raise PushError("shape")
    if len(raw) > MAX_PUSH:
        raise PushError("size")
    try:
        m = canon.loads_strict(raw)
        if type(m) is not dict or set(m) != _OUTER or m["v"] != 2:
            raise PushError("shape")
        ws = unhex(m["ws"], 16)
        c = unb64u(m["c"])
    except (canon.JcsError, EncodingError):
        raise PushError("shape") from None
    if not isinstance(keys, Mapping):
        raise PushError("no_key")
    entry = keys.get(m["ws"])
    epoch = m["epoch"]
    if type(entry) is not PushKeys or not isinstance(entry.wk, Mapping):
        entry = None
    if entry is None or type(epoch) is not int or epoch not in entry.wk or not 0 <= epoch < 2**32:
        raise PushError("no_key")
    try:
        inner = canon.loads_strict(suite.salted_open(k_push(entry.wk[epoch], ws, epoch), L["kdf_push_msg"],
                                                     push_aad(ws, epoch), c))  # fmt: skip
    except (InvalidTag, canon.JcsError):
        raise PushError("tag") from None
    if type(inner) is not dict or set(inner) != {"p", "sig"} or type(inner["p"]) is not dict:
        raise PushError("payload")
    try:
        sig = unb64u(inner["sig"], suite.SIG_LEN)
    except EncodingError:
        raise PushError("signature") from None  # review F6: a malformed signature is a signature failure
    p = inner["p"]
    try:
        signed = L["sig_push"] + bytes([suite.SUITE_ID]) + ws + epoch.to_bytes(4, "big") + canon.dumps(p)
    except canon.JcsError:
        raise PushError("payload") from None
    if not suite.verify(entry.wsk_pub, sig, signed):
        raise PushError("signature")  # a member device holds K_push, not WSK (§9 R3)
    try:
        _check_payload(p, PUSH_KINDS, padded=False)
    except PushError:
        raise PushError("payload") from None
    if not now_ms - MAX_AGE_MS <= p["ts_ms"] <= now_ms + SKEW_MS:
        raise PushError("stale")
    return p


close_module(
    globals(),
    PushError,
    lambda m: PushError("input", m),
    lambda m: PushError("input", m),
    overrides={"open_push": (lambda m: PushError("shape", m), lambda m: PushError("tag", m))},
)
