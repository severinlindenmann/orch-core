"""H12c builder 5 for R6 (design 11A.15.10): the six pair answers of protocol §8.3, against every epoch-0 answer in the
vendored relay corpus. Each answer whose field set is one of the six exact forms is rebuilt byte for byte from its
parts (or refused, where the device drops it as ``not_a_pairing_answer``); every other answer has a field set no
builder can produce. Counts are exact and derived from the corpus."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity import Refused, pairing

from .test_pairing import ANSWERS, EPOCH0_SOURCES, _steps

_b = crypto.unb64u

FORMS = pairing.ANSWER_FORMS


def _form(meta: dict) -> str | None:
    """The builder whose exact field set ``meta`` has, or None."""
    for name, (state, fields) in FORMS.items():
        if meta.get("state") == state and set(meta) == fields:
            return name
    return None


def _build(name: str, meta: dict) -> dict:
    w = _b(meta["wsk_pub"])
    if name == "pending":
        return pairing.answer_pending(w, _b(meta["sas_nonce"]), _b(meta["dk_sig_pub"]), _b(meta["dk_kx_pub"]))
    if name == "cert_pending":
        return pairing.answer_cert_pending(
            w, primary_label=meta["primary_label"], pk_pub=_b(meta["pk_pub"]), challenge=meta["challenge"]
        )
    if name == "approved":
        return pairing.answer_approved(
            w,
            scopes=meta["scopes"],
            cert=meta["cert"],
            pk_pub=_b(meta["pk_pub"]),
            epoch=meta["epoch"],
            list_seq=meta["list_seq"],
            card_seq=meta["card_seq"],
            baseline=meta["baseline"],
        )
    if name == "rejected_short":
        return pairing.answer_rejected_short(w)
    if name == "rejected_long":
        return pairing.answer_rejected_long(w, pk_pub=_b(meta["pk_pub"]), rejection=meta["rejection"])
    assert name == "expired"
    return pairing.answer_expired(w)


# Every state answer (not a refusal) in the corpus: the device-side answers that open under the link's secret, and
# the answers the host cases expect the host to send.
DEVICE = [(n, exp, meta) for n, _ctx, exp, meta in ANSWERS if meta is not None and "refusal" not in meta]
HOST = [
    (f"host.{c['name']}[{i}]", s["expect"], s["expect"]["answer"])
    for c in EPOCH0_SOURCES
    for i, s in enumerate(_steps(c))
    if s["expect"].get("answer")
]
STATE_ANSWERS = DEVICE + HOST


def _malformed(exp: dict) -> bool:
    """The device drops it for its shape: an exact field set with a value outside the protocol (the join anchor)."""
    return exp.get("why") == "not_a_pairing_answer"


@pytest.mark.parametrize(("name", "exp", "meta"), STATE_ANSWERS, ids=[a[0] for a in STATE_ANSWERS])
def test_every_corpus_answer_is_built_byte_for_byte_or_cannot_be_built(name, exp, meta):
    form = _form(meta)
    if form is None:
        # an extra or a missing field: none of the six builders has this field set, and the device never attributes it
        assert exp.get("why") in ("not_a_pairing_answer", "pk_pin") and not exp.get("attributed")
        return
    if _malformed(exp):
        with pytest.raises(Refused) as e:
            _build(form, meta)
        assert e.value.code == "malformed"
        return
    assert canon.dumps(_build(form, meta)) == canon.dumps(meta)


def test_answer_cases_are_all_found():
    """Exact (==): every state answer of ``pair_answers``, ``cert_binding.challenges``, ``approved_anchor.answers``,
    ``rejected_forms.answers`` and the host cases, by the form that rebuilds it."""
    assert len(ANSWERS) == 77
    assert len(DEVICE) == 72 and len(HOST) == 19
    forms = [_form(m) for _n, _e, m in STATE_ANSWERS]
    built = [f for (_n, e, _m), f in zip(STATE_ANSWERS, forms, strict=True) if f and not _malformed(e)]
    assert {f: built.count(f) for f in FORMS} == {
        "pending": 16,
        "cert_pending": 26,
        "approved": 14,
        "rejected_short": 4,
        "rejected_long": 19,
        "expired": 1,
    }
    refused = [f for (_n, e, _m), f in zip(STATE_ANSWERS, forms, strict=True) if f and _malformed(e)]
    assert refused == ["approved"] * 4
    assert sum(f is None for f in forms) == 7


# --- the builders' own refusals -------------------------------------------------------------------------------------

_P = next(m for n, _e, m in DEVICE if n == "approved_with_cert[0]")
_C = next(m for n, _e, m in DEVICE if n == "cert_pending_with_the_primarys_challenge_shows_its_code[0]")
_R = next(m for n, _e, m in DEVICE if n == "long_form_before_any_challenge_denied_by_the_owner[0]")
_W = _b(_P["wsk_pub"])
_PK = _b(_P["pk_pub"])
_OFF = b"\x04" + bytes(64)


def _approved(**kw):
    a = {k: _P[k] for k in ("scopes", "cert", "epoch", "list_seq", "card_seq", "baseline")}
    a["pk_pub"] = _PK
    a.update(kw)
    return pairing.answer_approved(_W, **a)


@pytest.mark.parametrize(
    "kw",
    [
        {"scopes": []},
        {"scopes": ["decide"]},
        {"scopes": ["drop:" + "0" * 32]},
        {"scopes": "look"},
        {"cert": None},
        {"cert": {"o": {}}},
        {"cert": {"o": [], "sig": "x"}},
        {"pk_pub": _OFF},
        {"pk_pub": crypto.b64u(_PK)},
        {"epoch": 0},
        {"epoch": 2**32},
        {"epoch": True},
        {"list_seq": 0},
        {"list_seq": 2**53},
        {"card_seq": 1.0},
        {"baseline": {"n": 1}},
        {"baseline": {"n": -1, "hash": "sha256:" + "0" * 64}},
        {"baseline": {"n": 1, "hash": "sha256:" + "0" * 63}},
        {"baseline": {"n": 1, "hash": "sha256:" + "A" * 64}},
        {"baseline": {"n": True, "hash": "sha256:" + "0" * 64}},
        {"baseline": {"n": 1, "hash": "sha256:" + "0" * 64, "x": 1}},
    ],
)
def test_answer_approved_refuses(kw):
    with pytest.raises(Refused) as e:
        _approved(**kw)
    assert e.value.code == "malformed"


def test_answer_approved_takes_a_null_baseline():
    assert _approved(baseline=None)["baseline"] is None
    assert _approved(baseline={"n": 0, "hash": "sha256:" + "0" * 64})["baseline"]["n"] == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda: pairing.answer_pending(_OFF, bytes(32), _PK, _PK),
        lambda: pairing.answer_pending(_W, bytes(31), _PK, _PK),
        lambda: pairing.answer_pending(_W, bytes(32), _OFF, _PK),
        lambda: pairing.answer_pending(_W, bytes(32), _PK, _PK[:-1]),
        lambda: pairing.answer_rejected_short(crypto.b64u(_W)),
        lambda: pairing.answer_expired(_W[:-1]),
        lambda: pairing.answer_cert_pending(_W, primary_label="Mac​Book", pk_pub=_PK, challenge=None),
        lambda: pairing.answer_cert_pending(_W, primary_label=b"Mac", pk_pub=_PK, challenge=None),
        lambda: pairing.answer_cert_pending(_W, primary_label="Mac", pk_pub=_OFF, challenge=None),
        lambda: pairing.answer_cert_pending(_W, primary_label="Mac", pk_pub=_PK, challenge={"o": {}}),
        lambda: pairing.answer_cert_pending(_W, primary_label="Mac", pk_pub=_PK, challenge=_C["challenge"]["o"]),
        lambda: pairing.answer_rejected_long(_W, pk_pub=_PK, rejection=None),
        lambda: pairing.answer_rejected_long(_W, pk_pub=_PK, rejection={**_R["rejection"], "x": 1}),
        lambda: pairing.answer_rejected_long(_W, pk_pub=_OFF, rejection=_R["rejection"]),
    ],
)
def test_answer_builders_refuse(call):
    with pytest.raises(Refused) as e:
        call()
    assert e.value.code == "malformed"


def test_relayed_objects_are_copied_unchanged():
    """The challenge and the rejection are relayed byte-identical (11A.15.6 7c/7d) and not aliased to the caller's."""
    a = pairing.answer_cert_pending(_W, primary_label="Mac", pk_pub=_PK, challenge=_C["challenge"])
    assert canon.dumps(a["challenge"]) == canon.dumps(_C["challenge"]) and a["challenge"] is not _C["challenge"]
    r = pairing.answer_rejected_long(_W, pk_pub=_PK, rejection=_R["rejection"])
    assert canon.dumps(r["rejection"]) == canon.dumps(_R["rejection"]) and r["rejection"] is not _R["rejection"]
    assert pairing.answer_cert_pending(_W, primary_label="", pk_pub=_PK, challenge=None)["challenge"] is None
