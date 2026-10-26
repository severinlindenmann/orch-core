"""Device certificates, revocations and workspace delegations (protocol §6, §7.1): all vectors, plus negatives."""

from __future__ import annotations

import copy

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import (
    Refused,
    certs,
    check_cert,
    check_delegation,
    delegation_binds,
    make_delegation,
    make_device_cert,
    make_revocation,
    open_device_label,
    person_scope_ok,
    scopes_ok,
    seal_device_label,
    verify_cert,
    verify_revocation,
)
from tests.crypto.vectors import S2

from .helpers import NOW, WS, Person

CERT_CASES = S2["certs"]["cases"]


@pytest.mark.parametrize("case", CERT_CASES, ids=lambda c: c["name"])
def test_cert_vectors(case):
    pk = bytes.fromhex(case["pk_pub"])
    expect = case["expect"]
    if "ok" in expect:
        assert verify_cert(case["signed"], pk, case["now_ms"], set(case["revoked"])) == expect["ok"]
    else:
        with pytest.raises(Refused) as e:
            verify_cert(case["signed"], pk, case["now_ms"], set(case["revoked"]))
        assert e.value.code == expect["refuse"]


@pytest.mark.parametrize("case", CERT_CASES, ids=lambda c: c["name"])
def test_cert_signed_bytes_match_the_vectors(case):
    o = case["signed"]["o"]
    if case["name"] == "extra_top_level_field":
        return
    assert (L["sig_device_cert"] + canon.cj_checked(o)).hex() == case["signed_bytes"]


REV_CASES = S2["revocations"]


@pytest.mark.parametrize("case", REV_CASES, ids=lambda c: c["name"])
def test_revocation_vectors(case):
    pk = bytes.fromhex(case["pk_pub"])
    expect = case["expect"]
    cert = case["cert"]
    if "ok" in expect:
        assert verify_revocation(case["signed"], pk, cert) == expect["ok"]
    else:
        with pytest.raises(Refused) as e:
            verify_revocation(case["signed"], pk, cert)
        assert e.value.code == expect["refuse"]


def test_delegation_vectors():
    results = {}
    for case in S2["card"]["cases"]:
        card, pk = case["card"], bytes.fromhex(case["owner_pk_pub"])
        try:
            d_o = check_delegation(card["delegation"], pk)
            delegation_binds(
                d_o,
                workspace_id=card["o"]["workspace_id"],
                wsk_pub_b64u=card["o"]["wsk_pub"],
                owner_person_id=card["o"]["owner_person_id"],
            )
            results[case["name"]] = "ok"
        except Refused as e:
            results[case["name"]] = e.code
    assert results["first_registration"] == "ok"
    assert results["wxk_rotation_seq_2_signed_by_wsk_alone"] == "ok"
    assert results["delegation_signed_by_another_person"] == "bad_signature"
    assert results["delegation_for_another_wsk"] == "delegation_mismatch"
    assert results["owner_is_another_person"] in ("bad_signature", "delegation_mismatch", "other_person")


# --- construction round trips ---------------------------------------------------------------------------------------


def test_make_and_verify_cert_round_trip():
    p = Person()
    signed = p.cert(expires=NOW + 1000)
    o = verify_cert(signed, p.pk_pub, NOW)
    assert o["device_id"] == crypto.device_id(p.sig_pub).hex()
    assert o["person_id"] == crypto.person_id(p.pk_pub).hex()
    assert (L["sig_device_cert"] + canon.cj_checked(o)) == certs._bytes("device_cert", o)


def test_cert_time_rules():
    p = Person()
    c = p.cert(created=NOW, expires=NOW + 10)
    verify_cert(c, p.pk_pub, NOW + 9)
    with pytest.raises(Refused, match="cert_expired"):
        verify_cert(c, p.pk_pub, NOW + 10)
    with pytest.raises(Refused, match="cert_expired"):
        verify_cert(c, p.pk_pub, NOW + 11)
    with pytest.raises(Refused, match="cert_invalid"):
        verify_cert(c, p.pk_pub, NOW - certs.SKEW_MS - 1)
    verify_cert(c, p.pk_pub, NOW - certs.SKEW_MS)
    with pytest.raises(Refused, match="revoked"):
        verify_cert(c, p.pk_pub, NOW, {crypto.device_id(p.sig_pub).hex()})


def test_cert_signed_by_another_person_or_under_another_label_is_refused():
    a, b = Person(), Person()
    signed = a.cert()
    with pytest.raises(Refused, match="cert_invalid"):
        check_cert(signed, b.pk_pub)
    # same object signed under the revocation label
    o = signed["o"]
    wrong = {"o": o, "sig": crypto.b64u(a.pk_sign(L["sig_revocation"] + canon.cj_checked(o)))}
    with pytest.raises(Refused, match="cert_invalid"):
        check_cert(wrong, a.pk_pub)


def test_tampering_and_shape_are_refused():
    p = Person()
    good = p.cert()
    for mutate in (
        lambda s: s["o"].update(scopes_max=["look"]),
        lambda s: s["o"].update(extra=1),
        lambda s: s["o"].pop("label_sealed"),
        lambda s: s.update(extra=1),
        lambda s: s["o"].update(v=3),
        lambda s: s["o"].update(suite=1),
        lambda s: s["o"].update(kind="revocation"),
        lambda s: s.update(sig=s["sig"][:-2]),
        lambda s: s.update(sig=s["sig"] + "="),
    ):
        s = copy.deepcopy(good)
        mutate(s)
        with pytest.raises(Refused, match="cert_invalid"):
            check_cert(s, p.pk_pub)
    for bad in (None, [], "x", {"o": 1, "sig": "x"}):
        with pytest.raises(Refused, match="cert_invalid"):
            check_cert(bad, p.pk_pub)


def test_cert_with_bad_keys_or_ids_is_refused_even_if_signed():
    p = Person()
    o = copy.deepcopy(p.cert()["o"])
    for field, val in (
        ("dk_sig_pub", crypto.b64u(b"\x04" + bytes(64))),  # not on curve
        ("dk_kx_pub", crypto.b64u(bytes(65))),  # identity
        ("device_id", "00" * 16),
        ("person_id", "00" * 16),
        ("device_id", "AB" * 16),  # upper-case hex
        ("created_ms", True),
        ("expires_ms", 5),  # before created
        ("label_sealed", "not b64u!"),
    ):
        bad = copy.deepcopy(o)
        bad[field] = val
        signed = certs.sign_object(bad, p.pk_sign)
        with pytest.raises(Refused, match="cert_invalid"):
            check_cert(signed, p.pk_pub)


def test_scoped_agent_cert_needs_an_expiry_and_never_signs_person_events():
    p = Person()
    scope = "drop:" + "ab" * 16
    with pytest.raises(Refused):
        p.cert(scopes=(scope,), expires=None)
    c = p.cert(scopes=(scope,), expires=NOW + 100)
    o = check_cert(c, p.pk_pub)
    assert not person_scope_ok(o)
    assert not person_scope_ok(o, needs_operate=True)


@pytest.mark.parametrize(
    ("scopes", "ok"),
    [
        (["look"], True),
        (["look", "decide"], True),
        (["look", "decide", "operate", "type"], True),
        (["decide"], False),
        (["look", "operate"], False),
        (["type", "operate", "decide", "look"], False),
        ([], False),
        ("look", False),
        (["look", "look"], False),
        (["drop:" + "ab" * 16], True),
        (["drop:" + "AB" * 16], False),
        (["drop:" + "ab" * 16, "look"], False),
        (["drop:abc"], False),
        (["look", "decide", "operate", "type", "extra"], False),
    ],
)
def test_scopes_ok(scopes, ok):
    assert scopes_ok(scopes) is ok


def test_person_scope_rules():
    p = Person()
    look = check_cert(p.cert(scopes=("look",)), p.pk_pub)
    decide = check_cert(p.cert(scopes=("look", "decide")), p.pk_pub)
    operate = check_cert(p.cert(scopes=("look", "decide", "operate")), p.pk_pub)
    assert not person_scope_ok(look)
    assert person_scope_ok(decide) and not person_scope_ok(decide, needs_operate=True)
    assert person_scope_ok(operate, needs_operate=True)


def test_make_device_cert_refuses_bad_input_before_signing_anything_useful():
    p = Person()
    with pytest.raises(Refused):
        p.cert(scopes=("decide",))
    with pytest.raises(crypto.CryptoError):
        make_device_cert(
            p.pk_pub,
            p.pk_sign,
            dk_sig_pub=bytes(65),
            dk_kx_pub=p.kx_pub,
            label_sealed=b"x",
            created_ms=NOW,
            expires_ms=None,
            scopes_max=["look"],
        )


def test_sign_object_checks_the_signer_output():
    p = Person()
    o = {"v": 2, "suite": 2, "kind": "revocation", "x": 1}
    with pytest.raises(Refused):
        certs.sign_object(o, lambda payload: b"short")
    with pytest.raises(Refused):
        certs.sign_object({"v": 2, "suite": 2, "kind": "card"}, p.pk_sign)
    with pytest.raises(Refused):
        certs.sign_object({"v": 2, "suite": 1, "kind": "revocation"}, p.pk_sign)


# --- revocations --------------------------------------------------------------------------------------------------


def test_revocation_round_trip_and_negatives():
    p, other = Person(), Person()
    cert_o = check_cert(p.cert(), p.pk_pub)
    rev = make_revocation(p.pk_pub, p.pk_sign, device_id_hex=cert_o["device_id"], revoked_ms=NOW, reason="lost")
    assert verify_revocation(rev, p.pk_pub, cert_o)["reason"] == "lost"
    with pytest.raises(Refused) as e:
        verify_revocation(rev, other.pk_pub, cert_o)
    assert e.value.code == "bad_signature"
    other_cert = check_cert(other.cert(), other.pk_pub)
    with pytest.raises(Refused) as e:
        verify_revocation(rev, p.pk_pub, other_cert)
    assert e.value.code == "other_person"
    with pytest.raises(Refused) as e:
        verify_revocation(rev, p.pk_pub, None)
    assert e.value.code == "other_person"
    with pytest.raises(Refused):
        make_revocation(p.pk_pub, p.pk_sign, device_id_hex=cert_o["device_id"], revoked_ms=NOW, reason="whim")
    # a cert signature under the revocation label's twin: certificate bytes as a revocation
    assert certs.check_revocation(rev, p.pk_pub)["device_id"] == cert_o["device_id"]
    with pytest.raises(Refused):
        certs.check_revocation(p.cert(), p.pk_pub)


# --- delegation ---------------------------------------------------------------------------------------------------


def test_delegation_round_trip_and_negatives():
    p, other = Person(), Person()
    wsk = crypto.public_bytes(crypto.generate_private_key())
    d = make_delegation(p.pk_pub, p.pk_sign, workspace_id=WS, wsk_pub=wsk, client_hosted=True, issued_ms=NOW)
    o = check_delegation(d, p.pk_pub)
    assert o["client_hosted"] is True and o["owner_person_id"] == crypto.person_id(p.pk_pub).hex()
    with pytest.raises(Refused) as e:
        check_delegation(d, other.pk_pub)
    assert e.value.code == "bad_signature"
    bad = copy.deepcopy(d)
    bad["o"]["client_hosted"] = False  # a client host trying to drop the flag
    with pytest.raises(Refused):
        check_delegation(bad, p.pk_pub)
    with pytest.raises(Refused):
        delegation_binds(o, workspace_id="00" * 16, wsk_pub_b64u=o["wsk_pub"], owner_person_id=o["owner_person_id"])
    with pytest.raises(Refused):
        delegation_binds(
            o,
            workspace_id=WS,
            wsk_pub_b64u=crypto.b64u(wsk[:-1] + bytes([wsk[-1] ^ 1])),
            owner_person_id=o["owner_person_id"],
        )
    with pytest.raises(crypto.CryptoError):
        make_delegation(p.pk_pub, p.pk_sign, workspace_id=WS, wsk_pub=bytes(65), client_hosted=False, issued_ms=NOW)
    # a cert is not a delegation (label separation)
    with pytest.raises(Refused):
        check_delegation(p.cert(), p.pk_pub)


def test_device_label_seal_round_trip_and_binding():
    pvk = bytes(range(32))
    pid, did = bytes(16), bytes(range(16))
    blob = seal_device_label(pvk, pid, did, "Severin's Mac")
    assert open_device_label(pvk, pid, did, blob) == "Severin's Mac"
    from cryptography.exceptions import InvalidTag

    for args in ((bytes(32), pid, did), (pvk, bytes([1]) * 16, did), (pvk, pid, bytes(16))):
        with pytest.raises(InvalidTag):
            open_device_label(*args, blob)


def test_refs():
    p = Person()
    assert certs.person_ref(p.pk_pub) == "p_" + crypto.person_id(p.pk_pub).hex()
    assert certs.device_ref(p.sig_pub).startswith("d_") and len(certs.device_ref(p.sig_pub)) == 34
