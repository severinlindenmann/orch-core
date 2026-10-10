"""Genesis owner checks (F1 §5.11 order), member.added, device.added, device.revoked, person-event signing."""

from __future__ import annotations

import copy

import pytest

from orch import crypto
from orch.custody import FileBackend, KdfParams, PassphraseBackend
from orch.identity import (
    Refused,
    certs,
    check_device_added,
    check_device_revoked,
    check_genesis,
    check_member_added,
    make_revocation,
    sign_person_event,
)

from .helpers import NOW, WS, Person, build_genesis, host_sign, resign, sign_person


def refused(fn, *a, code):
    with pytest.raises(Refused) as e:
        fn(*a)
    assert e.value.code == code, e.value


def test_valid_genesis():
    g = build_genesis()
    r = check_genesis(g.event)
    assert r["owner_pk_pub"] == g.owner.pk_pub
    assert r["device_cert"]["device_id"] == crypto.device_id(g.owner.sig_pub).hex()


def test_genesis_client_hosted_flag_is_kept():
    g = build_genesis(client_hosted=True)
    assert check_genesis(g.event)["delegation"]["client_hosted"] is True


def test_genesis_shape_checks():
    g = build_genesis()
    for field, val in (("seq", 2), ("roster_v", 1), ("type", "member.added")):
        ev = copy.deepcopy(g.event)
        ev[field] = val
        refused(check_genesis, resign(g, ev), code="genesis.shape")
    ev = copy.deepcopy(g.event)
    ev["actor"] = {"kind": "host"}
    refused(check_genesis, resign(g, ev), code="genesis.shape")
    ev = copy.deepcopy(g.event)
    del ev["owner"]
    refused(check_genesis, ev, code="genesis.shape")
    ev = copy.deepcopy(g.event)
    ev["owner"]["pk_pub"] = "AAAA"
    refused(check_genesis, ev, code="genesis.shape")


def test_check1_owner_ids_must_agree():
    g = build_genesis()
    other = Person()
    ev = copy.deepcopy(g.event)
    ev["owner"]["person"] = other.ref
    refused(check_genesis, resign(g, ev), code="genesis.owner_mismatch")
    # device_cert of another person
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = other.cert()
    refused(check_genesis, resign(g, ev), code="genesis.owner_mismatch")
    # delegation owner is another person
    ev = copy.deepcopy(g.event)
    ev["delegation"] = certs.make_delegation(
        other.pk_pub,
        other.pk_sign,
        workspace_id=WS,
        wsk_pub=crypto.unb64u(ev["wsk_pub"]),
        client_hosted=False,
        issued_ms=NOW,
    )
    refused(check_genesis, resign(g, ev), code="genesis.owner_mismatch")


def test_check2_delegation_signature():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    ev["delegation"]["o"]["client_hosted"] = True  # tampered after signing
    refused(check_genesis, resign(g, ev), code="genesis.bad_delegation")
    ev = copy.deepcopy(g.event)
    ev["delegation"]["o"]["extra"] = 1
    refused(check_genesis, resign(g, ev), code="genesis.bad_delegation")


def test_check3_delegation_binds_workspace_and_wsk():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    ev["workspace_id"] = "11" * 16
    refused(check_genesis, resign(g, ev), code="genesis.delegation_mismatch")
    other_wsk = crypto.generate_private_key()
    ev = copy.deepcopy(g.event)
    ev["wsk_pub"] = crypto.b64u(crypto.public_bytes(other_wsk))
    refused(check_genesis, resign(type(g)(g.owner, other_wsk, g.event), ev), code="genesis.delegation_mismatch")


def test_check4_host_sig_under_wsk():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    host_sign(crypto.generate_private_key(), ev)
    refused(check_genesis, ev, code="genesis.host_sig")
    ev = copy.deepcopy(g.event)
    del ev["host_sig"]
    refused(check_genesis, ev, code="genesis.host_sig")
    ev = copy.deepcopy(g.event)
    ev["prefix"] = "OTHER"  # content changed after the host signed
    refused(check_genesis, ev, code="genesis.host_sig")


def test_check5_device_cert_and_actor_device():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    ev["actor"]["device"] = "d_" + "00" * 16
    refused(check_genesis, resign(g, ev), code="genesis.bad_device_cert")
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = g.owner.cert(scopes=("look",))  # no decide
    refused(check_genesis, resign(g, ev), code="genesis.bad_device_cert")
    ev = copy.deepcopy(g.event)
    ev["device_cert"]["o"]["label_sealed"] = "AAAA"
    refused(check_genesis, resign(g, ev), code="genesis.bad_device_cert")
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = g.owner.cert(scopes=("drop:" + "ab" * 16,), expires=NOW + 1)
    refused(check_genesis, resign(g, ev), code="genesis.bad_device_cert")


def test_check6_person_sig_under_the_device_key():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    sign_person(Person(), ev)  # another key signs
    host_sign(g.wsk, ev)
    refused(check_genesis, ev, code="genesis.bad_sig")
    ev = copy.deepcopy(g.event)
    ev["auth"] = "webauthn"  # auth is covered by the signature
    refused(check_genesis, resign(g, ev, person=False), code="genesis.bad_sig")


def test_genesis_checks_run_in_the_documented_order():
    """An event broken in several ways reports the earliest check."""
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    ev["owner"]["person"] = "p_" + "00" * 16
    ev["sig"] = "AAAA"
    ev["host_sig"] = "AAAA"
    ev["delegation"]["sig"] = "AAAA"
    refused(check_genesis, ev, code="genesis.owner_mismatch")
    ev = copy.deepcopy(g.event)
    ev["sig"] = "AAAA"
    ev["host_sig"] = "AAAA"
    ev["delegation"]["sig"] = "AAAA"
    refused(check_genesis, ev, code="genesis.bad_delegation")


# --- member.added / device.added / device.revoked ------------------------------------------------------------------


def member_event(p: Person, **over):
    ev = {
        "type": "member.added",
        "person": p.ref,
        "name": "Bob",
        "role": "member",
        "pk_pub": crypto.b64u(p.pk_pub),
        "device_cert": p.cert(),
    }
    ev.update(over)
    return ev


def test_member_added():
    p = Person()
    assert check_member_added(member_event(p))["person_id"] == crypto.person_id(p.pk_pub).hex()
    other = Person()
    refused(check_member_added, member_event(p, person=other.ref), code="member.person_mismatch")
    refused(check_member_added, member_event(p, device_cert=other.cert()), code="cert_invalid")
    refused(check_member_added, member_event(p, device_cert=p.cert(scopes=("look",))), code="member.cert_scope")
    refused(
        check_member_added,
        member_event(p, device_cert=p.cert(scopes=("drop:" + "ab" * 16,), expires=NOW + 5)),
        code="member.cert_scope",
    )
    refused(check_member_added, member_event(p, pk_pub="nope"), code="malformed")


def test_device_added():
    p = Person()
    new = Person.__new__(Person)
    new.__dict__.update(p.__dict__)
    new.dk_sig, new.dk_kx = crypto.generate_private_key(), crypto.generate_private_key()
    new.sig_pub, new.kx_pub = crypto.public_bytes(new.dk_sig), crypto.public_bytes(new.dk_kx)
    cert = new.cert()
    ev = {"type": "device.added", "device": new.device, "cert": cert}
    assert check_device_added(ev, p.pk_pub)["device_id"] == crypto.device_id(new.sig_pub).hex()
    refused(check_device_added, {**ev, "device": p.device}, p.pk_pub, code="device.id_mismatch")
    refused(check_device_added, ev, Person().pk_pub, code="cert_invalid")
    refused(check_device_added, {**ev, "cert": new.cert(scopes=("look",))}, p.pk_pub, code="member.cert_scope")
    drop = new.cert(scopes=("drop:" + "ab" * 16,), expires=NOW + 5)
    refused(check_device_added, {**ev, "cert": drop}, p.pk_pub, code="member.cert_scope")


def test_device_revoked_authority_is_the_embedded_signature():
    p, evil = Person(), Person()
    did = crypto.device_id(p.sig_pub).hex()
    rev = make_revocation(p.pk_pub, p.pk_sign, device_id_hex=did, revoked_ms=NOW, reason="lost")
    ev = {"type": "device.revoked", "device": "d_" + did, "reason": "lost", "revocation": rev}
    assert check_device_revoked(ev, p.pk_pub)["device_id"] == did
    refused(check_device_revoked, {**ev, "reason": "retired"}, p.pk_pub, code="device.reason_mismatch")
    refused(check_device_revoked, {**ev, "device": "d_" + "00" * 16}, p.pk_pub, code="device.id_mismatch")
    refused(check_device_revoked, ev, evil.pk_pub, code="bad_signature")
    forged = make_revocation(evil.pk_pub, evil.pk_sign, device_id_hex=did, revoked_ms=NOW, reason="lost")
    refused(check_device_revoked, {**ev, "revocation": forged}, p.pk_pub, code="bad_signature")


# --- signing person events through custody ------------------------------------------------------------------------


def event(auth="passphrase"):
    return {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "ticket.updated",
        "actor": {"kind": "person"},
        "hash_v": 1,
        "auth": auth,
    }


def test_person_event_signed_through_passphrase_backend_verifies(tmp_path):
    b = PassphraseBackend(
        tmp_path, passphrase_provider=lambda r: "long enough pass", kdf=KdfParams(n=2**10), min_n=2**10
    )
    pub = b.create("dk")
    log = "01J9ZP0000000000000000000A"
    sig = sign_person_event(b, "dk", WS, log, event(), action="update")
    from orch import canon

    assert crypto.verify(pub, crypto.unb64u(sig), canon.person_signing_bytes(WS, log, event()))


def test_file_tier_key_never_signs_person_events(tmp_path):
    f = FileBackend(tmp_path)
    f.create("dk")
    with pytest.raises(Refused) as e:
        sign_person_event(f, "dk", WS, "workspace", event(), action="x")
    assert e.value.code == "identity.file_tier"


def test_event_auth_must_match_the_backend(tmp_path):
    b = PassphraseBackend(
        tmp_path, passphrase_provider=lambda r: "long enough pass", kdf=KdfParams(n=2**10), min_n=2**10
    )
    b.create("dk")
    for auth in ("secure-enclave", None, "none"):
        with pytest.raises(Refused) as e:
            sign_person_event(b, "dk", WS, "workspace", event(auth), action="x")
        assert e.value.code == "identity.auth_mismatch"
