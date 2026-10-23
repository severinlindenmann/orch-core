"""H12d-3: the reader gate when the host opens a pair request (11A.15.6 step 4, orch-v2-host §0.8.6 item 9.4 narrowed
by C7): ``via:"pair"`` only, bound to this offer and attempt and the meta's keys, ``who`` the link's ``pk_pin`` or the
presented certificate's person, the ``view`` surface, no past window and at most 300 s ahead; then the level against
``required_reader``. The relay corpus's ``reader_decl.pair_meta`` gate cases run through the same function."""

from __future__ import annotations

import copy
import dataclasses

import pytest
from orch.identity.pairdevice import PairGateRefused, pair_gate

from orch import crypto
from orch.identity import pairing, reader

from ..crypto.vectors import S2
from .helpers import NOW, WS, Person
from .reader_helpers import decl_o, sign_decl

OFFER = "a" * 32
ATTEMPT = "b" * 32


def _phone_meta(p: Person, decl=None) -> pairing.PairMeta:
    return pairing.PairMeta(
        dk_sig_pub=p.sig_pub,
        dk_kx_pub=p.kx_pub,
        device_id=crypto.device_id(p.sig_pub).hex(),
        label="phone",
        attempt=ATTEMPT,
        cert=None,
        reader_decl=decl,
    )


def _pin(p: Person) -> dict[str, str]:
    return {"pk_pin": crypto.b64u(crypto.pk_pin(p.pk_pub))}


def _pair_decl(phone: Person, owner: Person, *, level=1, created_ms=NOW, who=None, bind=None, **changes):
    o = decl_o(
        phone,
        WS,
        level=level,
        via="pair",
        created_ms=created_ms,
        surfaces=("view",),
        who=who if who is not None else _pin(owner),
        bind=bind if bind is not None else {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
        **changes,
    )
    return sign_decl(phone, o)


def gate(meta, *, required=1, now_ms=NOW, who=None, owner=None, offer_id=OFFER):
    return pair_gate(meta, workspace_id=WS, offer_id=offer_id, who=who or _pin(owner), required=required, now_ms=now_ms)


def refused(meta, code, reason, **kw) -> PairGateRefused:
    with pytest.raises(PairGateRefused) as exc:
        gate(meta, **kw)
    assert (exc.value.code, exc.value.reason) == (code, reason), exc.value
    return exc.value


def test_a_level_1_pair_declaration_is_admitted_and_kept_verbatim():
    phone, owner = Person(), Person()
    d = _pair_decl(phone, owner)
    got = gate(_phone_meta(phone, d), owner=owner)
    assert (got.reader, got.level, got.digest) == (1, 1, reader.decl_digest(d))
    assert got.decl == d and got.decl_who == _pin(owner)
    got.decl["o"]["reader"] = 9  # a copy: the stored declaration is not reachable through the answer
    assert d["o"]["reader"] == 1


def test_no_declaration_is_level_0_admitted_only_while_nothing_requires_more():
    phone, owner = Person(), Person()
    got = gate(_phone_meta(phone), owner=owner, required=0)
    assert (got.decl, got.decl_who, got.level, got.digest) == (None, None, 0, None)
    e = refused(_phone_meta(phone), "sign.convenience_readers", "no_declaration", owner=owner)
    assert (e.required, e.declared) == (1, 0)


def test_a_level_0_declaration_is_valid_and_refused_while_required_is_1():
    phone, owner = Person(), Person()
    meta = _phone_meta(phone, _pair_decl(phone, owner, level=0))
    assert gate(meta, owner=owner, required=0).level == 0
    e = refused(meta, "sign.convenience_readers", "reader_too_old", owner=owner)
    assert (e.required, e.declared) == (1, 0)


def test_a_higher_unknown_level_counts_as_the_highest_known():
    phone, owner = Person(), Person()
    o = decl_o(
        phone, WS, level=7, via="pair", created_ms=NOW, surfaces=("view",), who=_pin(owner),
        bind={"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
    )  # fmt: skip
    got = gate(_phone_meta(phone, sign_decl(phone, o)), owner=owner)
    assert (got.reader, got.level) == (7, 1)


def test_pairing_has_no_past_window_but_at_most_300_s_ahead():
    phone, owner = Person(), Person()
    old = _pair_decl(phone, owner, created_ms=NOW - 40 * 86_400_000)
    assert gate(_phone_meta(phone, old), owner=owner).level == 1
    ahead = _pair_decl(phone, owner, created_ms=NOW + 300_000)
    assert gate(_phone_meta(phone, ahead), owner=owner).level == 1
    far = _pair_decl(phone, owner, created_ms=NOW + 300_001)
    e = refused(_phone_meta(phone, far), "reader.decl_invalid", "time", owner=owner)
    assert str(NOW) in e.detail  # the refusal states the host's time


@pytest.mark.parametrize(
    "bind",
    [
        {"via": "pair", "offer_id": "c" * 32, "attempt": ATTEMPT},
        {"via": "pair", "offer_id": OFFER, "attempt": "c" * 32},
        {"via": "declare"},
        {"via": "enrol"},
    ],
)
def test_only_a_pair_declaration_of_this_offer_and_attempt_counts(bind):
    """C7 (DECIDED 15 Oct 2026): no ``via:"declare"`` beside a pairing, on either path."""
    phone, owner = Person(), Person()
    refused(_phone_meta(phone, _pair_decl(phone, owner, bind=bind)), "reader.decl_invalid", "bind", owner=owner)


def test_who_is_the_links_pin_or_the_presented_certs_person():
    phone, owner, other = Person(), Person(), Person()
    pid = {"person_id": crypto.person_id(owner.pk_pub).hex()}
    # path 7a: the presented certificate's person
    meta = _phone_meta(phone, _pair_decl(phone, owner, who=pid))
    assert gate(meta, who=pid).decl_who == pid
    refused(meta, "reader.decl_invalid", "who", owner=owner)  # signed for the person, checked against the pin
    # path 7b: another person's pin
    refused(_phone_meta(phone, _pair_decl(phone, owner)), "reader.decl_invalid", "who", owner=other)


def test_the_declaration_binds_the_meta_keys_and_device():
    phone, owner, other = Person(), Person(), Person()
    d = _pair_decl(phone, owner)
    refused(_phone_meta(other, d), "reader.decl_invalid", "signature", owner=owner)
    meta = _phone_meta(phone, d)
    swapped = dataclasses.replace(meta, dk_kx_pub=other.kx_pub)
    refused(swapped, "reader.decl_invalid", "keys", owner=owner)


def test_the_phone_surface_is_view_a_log_only_declaration_is_kat_missing():
    phone, owner = Person(), Person()
    o = decl_o(
        phone, WS, via="pair", created_ms=NOW, surfaces=("log",), who=_pin(owner),
        bind={"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
    )  # fmt: skip
    refused(_phone_meta(phone, sign_decl(phone, o)), "reader.decl_invalid", "kat_missing", owner=owner)


def test_another_workspace_is_refused():
    phone, owner = Person(), Person()
    o = decl_o(
        phone, "c" * 32, via="pair", created_ms=NOW, surfaces=("view",), who=_pin(owner),
        bind={"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
    )  # fmt: skip
    refused(_phone_meta(phone, sign_decl(phone, o)), "reader.decl_invalid", "workspace", owner=owner)


@pytest.mark.parametrize(
    "kw",
    [{"workspace_id": "x"}, {"offer_id": "x"}, {"who": {}}, {"required": -1}, {"now_ms": None}, {"required": True}],
)
def test_a_bad_context_is_a_programming_error(kw):
    phone, owner = Person(), Person()
    base = {"workspace_id": WS, "offer_id": OFFER, "who": _pin(owner), "required": 1, "now_ms": NOW}
    with pytest.raises(ValueError):
        pair_gate(_phone_meta(phone), **{**base, **kw})


# --- the relay corpus: reader_decl.pair_meta (the meta as the phone sends it, then the gate) -----------------------


def _gate_cases():
    section = S2.get("reader_decl") or {}
    return [c for c in section.get("pair_meta", []) if "gate" in c]


@pytest.mark.skipif(not _gate_cases(), reason="no reader_decl.pair_meta gate cases in the vendored corpus")
@pytest.mark.parametrize("case", _gate_cases(), ids=lambda c: c["name"])
def test_relay_pair_meta_gate_vectors(case):
    ctx, expect = case["gate"]["ctx"], case["gate"]["expect"]
    assert tuple(ctx["surfaces"]) == ("view",) and ctx["host_max"] == reader.HOST_MAX
    meta = pairing.check_pair_meta(copy.deepcopy(case["meta"]))
    assert meta.attempt == ctx["bind"]["attempt"]
    kw = {"workspace_id": ctx["workspace_id"], "offer_id": ctx["bind"]["offer_id"], "who": ctx["who"]}
    if "ok" in expect:
        got = pair_gate(meta, **kw, required=1, now_ms=NOW)
        assert {"device_id": meta.device_id, "reader": got.reader, "level": got.level, "digest": got.digest} == (
            expect["ok"]
        )
        assert got.decl == case["meta"]["reader_decl"]
    else:
        with pytest.raises(PairGateRefused) as exc:
            pair_gate(meta, **kw, required=0, now_ms=NOW)
        assert (exc.value.code, exc.value.reason) == (expect["refuse"], expect["reason"])


@pytest.mark.skipif(not _gate_cases(), reason="no reader_decl.pair_meta cases in the vendored corpus")
def test_relay_meta_v1_without_a_declaration_is_level_0():
    case = next(c for c in S2["reader_decl"]["pair_meta"] if c["name"] == "meta_v1_without_reader_decl_still_pending")
    meta = pairing.check_pair_meta(copy.deepcopy(case["meta"]))
    ws, pin = "705d40abbb8c1c90354a1acaa94c935c", {"pk_pin": "i86czekgFn_kNZP3cAsvdyCuW9XR33396GzE2dn3wBI"}
    assert pair_gate(meta, workspace_id=ws, offer_id=OFFER, who=pin, required=0, now_ms=NOW).level == 0
    with pytest.raises(PairGateRefused) as exc:
        pair_gate(meta, workspace_id=ws, offer_id=OFFER, who=pin, required=1, now_ms=NOW)
    assert exc.value.reason == "no_declaration"
