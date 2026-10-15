"""The six pair answers of R6 pairing (design 11A.15.10, H12c item 5): the exact shapes of orch-relay
``docs/protocol-v2.md`` §8.3 steps 3 and 5 (G3b). Vectors: every state answer of the relay corpus sections
``bridge.pair_answers``, ``cert_binding.challenges``, ``approved_anchor.answers``, ``rejected_forms.answers`` and the
host cases (``tests/identity/test_pair_answers.py``).

A relayed challenge or rejection enters only as the typed result of ``orch.identity.certreq.check_cert_challenge`` /
``check_cert_rejection`` (review round 1 of #495, Opus L7): verify-before-relay is a type rule, and the answer carries
the object's signed bytes unchanged. The phone verifies them again under the pinned ``PK``.
"""

from __future__ import annotations

import re

from orch import canon, crypto

from .certreq import CertChallenge, CertRejection
from .pairing import _MAX_SAFE_INT, MAX_EPOCH, _bad, _pub, _raw, clean_label

__all__ = [
    "ANSWER_FORMS",
    "answer_approved",
    "answer_cert_pending",
    "answer_expired",
    "answer_pending",
    "answer_rejected_long",
    "answer_rejected_short",
]


# --- 5. the pair answers (§8.3 steps 3 and 5, G3b) -------------------------------------------------------------------
#
# Every answer to an epoch-0 request carries ``wsk_pub`` (the device checks it against the link's pin first). The six
# forms below are exact: the phone drops any other field set (``not_a_pairing_answer``), so a builder never adds a
# field. A relayed challenge or rejection is copied unchanged: the host verified it first (``check_cert_challenge``,
# ``check_cert_rejection``), the phone verifies it again under the pinned ``PK``. Keys are raw 65-byte points (b64u on
# the wire).

ANSWER_FORMS = {
    "pending": ("pending", frozenset({"state", "wsk_pub", "sas_nonce", "dk_sig_pub", "dk_kx_pub"})),
    "cert_pending": ("cert_pending", frozenset({"state", "primary_label", "pk_pub", "challenge", "wsk_pub"})),
    "approved": (
        "approved",
        frozenset({"state", "scopes", "cert", "pk_pub", "epoch", "list_seq", "card_seq", "baseline", "wsk_pub"}),
    ),
    "rejected_short": ("rejected", frozenset({"state", "wsk_pub"})),
    "rejected_long": ("rejected", frozenset({"state", "wsk_pub", "pk_pub", "rejection"})),
    "expired": ("expired", frozenset({"state", "wsk_pub"})),
}
_SCOPE_ORDER = ("look", "decide", "operate", "type")
_BASELINE_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _signed_copy(value: object, what: str) -> dict:
    """A signed object to relay: exactly ``{"o": <object>, "sig": <string>}``, deep-copied through ``cj`` so the answer
    holds the same bytes and no alias of the caller's value. Its signature is the caller's check, before this."""
    if type(value) is not dict or set(value) != {"o", "sig"} or type(value["o"]) is not dict:
        raise _bad(what)
    if type(value["sig"]) is not str:
        raise _bad(what)
    try:
        return canon.loads_strict(canon.dumps(value))
    except (canon.JcsError, ValueError, TypeError):
        raise _bad(what) from None


def _wsk(wsk_pub: object) -> str:
    return crypto.b64u(_pub(wsk_pub, "wsk_pub"))


def answer_pending(wsk_pub: bytes, sas_nonce: bytes, dk_sig_pub: bytes, dk_kx_pub: bytes) -> dict:
    """``{"state":"pending", "wsk_pub", "sas_nonce", "dk_sig_pub", "dk_kx_pub"}`` (exactly these five, §8.3 step 3):
    the consumed request's two keys echoed back, so the phone can check they are its own (``not_my_key``)."""
    return {
        "state": "pending",
        "wsk_pub": _wsk(wsk_pub),
        "sas_nonce": crypto.b64u(_raw(sas_nonce, 32, "sas_nonce")),
        "dk_sig_pub": crypto.b64u(_pub(dk_sig_pub, "dk_sig_pub")),
        "dk_kx_pub": crypto.b64u(_pub(dk_kx_pub, "dk_kx_pub")),
    }


def answer_cert_pending(wsk_pub: bytes, *, primary_label: str, pk_pub: bytes, challenge: CertChallenge | None) -> dict:
    """``{"state":"cert_pending", "primary_label", "pk_pub", "challenge", "wsk_pub"}``: ``challenge`` is ``None`` until
    the primary's challenge is stored, then that :class:`CertChallenge` (verified under ``pk_pub``, the pinned owner
    key) relayed unchanged (11A.15.6 7c). ``primary_label`` is the host's name for the primary as :func:`clean_label`
    leaves it (a label it would change is refused)."""
    if type(primary_label) is not str or clean_label(primary_label) != primary_label:
        raise _bad("primary_label")
    pk = _pub(pk_pub, "pk_pub")
    if challenge is not None and (type(challenge) is not CertChallenge or challenge.pk_pub != pk):
        raise _bad("challenge: a CertChallenge verified under pk_pub")
    return {
        "state": "cert_pending",
        "primary_label": primary_label,
        "pk_pub": crypto.b64u(pk),
        "challenge": None if challenge is None else _signed_copy(challenge.signed, "challenge"),
        "wsk_pub": _wsk(wsk_pub),
    }


def _baseline(value: object) -> dict | None:
    """``null`` or exactly ``{"n": safe integer >= 0, "hash": "sha256:" + 64 lower-case hex}`` (§8.3, §19.8)."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"n", "hash"}:
        raise _bad("baseline")
    n, h = value["n"], value["hash"]
    if type(n) is not int or not 0 <= n <= _MAX_SAFE_INT or type(h) is not str or not _BASELINE_HASH.fullmatch(h):
        raise _bad("baseline")
    return {"n": n, "hash": h}


def answer_approved(
    wsk_pub: bytes,
    *,
    scopes: list[str],
    cert: dict,
    pk_pub: bytes,
    epoch: int,
    list_seq: int,
    card_seq: int,
    baseline: dict | None,
) -> dict:
    """``{"state":"approved", "scopes", "cert", "pk_pub", "epoch", "list_seq", "card_seq", "baseline", "wsk_pub"}``
    (§8.3, the G3b join anchor): ``scopes`` a non-empty prefix of ``look, decide, operate, type`` (byte-equal to the
    accepted list entry's, 11A.15.6 step 12); ``cert`` the signed certificate, copied; ``epoch`` in ``1 .. 2^32-1``;
    ``list_seq`` and ``card_seq`` safe integers ``>= 1``; ``baseline`` ``None`` or ``{n, hash}``. The numbers must be
    those of the publication and card the relay accepted: that is the caller's rule, these are the shapes."""
    if type(scopes) is not list or not scopes or tuple(scopes) != _SCOPE_ORDER[: len(scopes)]:
        raise _bad("scopes")
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
        raise _bad("epoch")
    for v, w in ((list_seq, "list_seq"), (card_seq, "card_seq")):
        if type(v) is not int or not 1 <= v <= _MAX_SAFE_INT:
            raise _bad(w)
    return {
        "state": "approved",
        "scopes": list(scopes),
        "cert": _signed_copy(cert, "cert"),
        "pk_pub": crypto.b64u(_pub(pk_pub, "pk_pub")),
        "epoch": epoch,
        "list_seq": list_seq,
        "card_seq": card_seq,
        "baseline": _baseline(baseline),
        "wsk_pub": _wsk(wsk_pub),
    }


def answer_rejected_short(wsk_pub: bytes) -> dict:
    """``{"state":"rejected", "wsk_pub"}``: rejected on the host, or any internal failure (11A.15.6)."""
    return {"state": "rejected", "wsk_pub": _wsk(wsk_pub)}


def answer_rejected_long(wsk_pub: bytes, *, rejection: CertRejection) -> dict:
    """``{"state":"rejected", "wsk_pub", "pk_pub", "rejection"}`` (G3b): the primary's PK-signed ``cert_rejection`` as
    a :class:`CertRejection` (11A.15.6 7d), relayed unchanged with the ``PK`` it was verified under."""
    if type(rejection) is not CertRejection:
        raise _bad("rejection: a CertRejection")
    return {
        "state": "rejected",
        "wsk_pub": _wsk(wsk_pub),
        "pk_pub": crypto.b64u(rejection.pk_pub),
        "rejection": _signed_copy(rejection.signed, "rejection"),
    }


def answer_expired(wsk_pub: bytes) -> dict:
    """``{"state":"expired", "wsk_pub"}``: ``cert_pending`` ran out, or the phone's deadline passed (11A.15.6)."""
    return {"state": "expired", "wsk_pub": _wsk(wsk_pub)}
