"""H12b: forwarded revocation intake, ``Store.note_forwarded_revocation`` (§11A.9, P3-D31 host MUSTs).

Every fact comes from the signed objects (person key, certificate judged at its own ``created_ms``, revocation);
the item's outer ``device_id``, ``person_id``, ``reason`` and ``provenance.digest`` must equal them. The result is
``held`` (the device is associated with that person), ``pending`` (the person is a current or past member, the
device is not associated with them) or ``dropped`` (anything else, including anything that does not verify and a cap
rejection). Idempotent per ``(device_id, person_id)``.
"""

from __future__ import annotations

import hashlib

import pytest

from orch import canon, crypto
from orch.identity import certs
from orch.store import pins as pins_mod
from orch.store.pins import is_forwarded, pair
from tests.identity.helpers import Person
from tests.store.helpers import T0, Env


def _cert(person: Person, sig_pub: bytes, *, created_ms: int = T0 * 1000, expires_ms: int | None = None) -> dict:
    return certs.make_device_cert(
        person.pk_pub,
        person.pk_sign,
        dk_sig_pub=sig_pub,
        dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
        label_sealed=b"x",
        created_ms=created_ms,
        expires_ms=expires_ms,
        scopes_max=["look", "decide", "operate", "type"],
    )


def item(person: Person, sig_pub: bytes, reason: str = "compromised", cert: dict | None = None) -> dict:
    cert = cert or _cert(person, sig_pub)
    device_hex = certs.device_ref(sig_pub)[2:]
    rev = certs.make_revocation(
        person.pk_pub, person.pk_sign, device_id_hex=device_hex, revoked_ms=T0 * 1000 + 5, reason=reason
    )
    return {
        "device_id": device_hex,
        "person_id": crypto.person_id(person.pk_pub).hex(),
        "reason": reason,
        "revocation": rev,
        "device_cert": cert,
        "pk_pub": crypto.b64u(person.pk_pub),
        "provenance": {"cursor": "c1", "digest": "sha256:" + hashlib.sha256(canon.cj_checked(rev)).hexdigest()},
        "chain_reason": None,
    }


def _revocations_file(env: Env):
    p = env.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "revocations.jsonl"
    return p.read_bytes() if p.exists() else b""


@pytest.fixture
def ws(env):
    env.bootstrap()
    device, sig_pub = env.add_device()  # the owner's second device: associated with the owner
    return env, device, sig_pub


def test_an_associated_pair_is_held(ws):
    env, device, sig_pub = ws
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "held"
    recs = [r for r in env.store._pins.revocations() if pair(r) == (device, env.owner.ref)]
    assert len(recs) == 1 and is_forwarded(recs[0]) and recs[0]["reason"] == "compromised"


def test_intake_is_idempotent_per_pair(ws):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub)
    assert env.store.note_forwarded_revocation(it) == "held"
    first = _revocations_file(env)
    assert env.store.note_forwarded_revocation(it) == "held"
    assert _revocations_file(env) == first


def test_a_member_whose_device_is_not_associated_is_held_as_unknown(ws):
    env, _device, _sig_pub = ws
    stray = crypto.public_bytes(crypto.generate_private_key())  # a key the workspace never listed
    assert env.store.note_forwarded_revocation(item(env.owner, stray)) == "held"  # visible: standing `unknown`
    assert env.store.pair_standings([(certs.device_ref(stray), env.owner.ref, "compromised")]) == ["unknown"]
    assert any(pair(r) == (certs.device_ref(stray), env.owner.ref) for r in env.store._pins.revocations())


def test_a_stranger_is_dropped(ws):
    env, _device, sig_pub = ws
    bob = Person()  # never a member; certifies the owner's device key himself
    assert env.store.note_forwarded_revocation(item(bob, sig_pub)) == "dropped"
    assert _revocations_file(env) == b""


@pytest.mark.parametrize("field", ["device_id", "person_id", "reason"])
def test_a_substituted_outer_field_is_dropped(ws, field):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub, reason="lost")
    other = Person()
    it[field] = {
        "device_id": certs.device_ref(other.sig_pub)[2:],
        "person_id": crypto.person_id(other.pk_pub).hex(),
        "reason": "compromised",
    }[field]
    assert env.store.note_forwarded_revocation(it) == "unverifiable"
    assert _revocations_file(env) == b""


def test_a_wrong_provenance_digest_is_dropped(ws):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub)
    it["provenance"]["digest"] = "sha256:" + "0" * 64
    assert env.store.note_forwarded_revocation(it) == "unverifiable"


def test_a_forged_revocation_is_dropped(ws):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub, reason="lost")
    it["revocation"]["o"]["reason"] = "compromised"  # signature no longer verifies
    it["reason"] = "compromised"
    it["provenance"]["digest"] = "sha256:" + hashlib.sha256(canon.cj_checked(it["revocation"])).hexdigest()
    assert env.store.note_forwarded_revocation(it) == "unverifiable"
    assert _revocations_file(env) == b""


def test_a_certificate_of_another_person_is_dropped(ws):
    env, _device, sig_pub = ws
    bob = Person()
    it = item(env.owner, sig_pub, cert=_cert(bob, sig_pub))
    assert env.store.note_forwarded_revocation(it) == "unverifiable"


def test_a_pk_pub_that_is_not_the_persons_is_dropped(ws):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub)
    it["pk_pub"] = crypto.b64u(Person().pk_pub)
    assert env.store.note_forwarded_revocation(it) == "unverifiable"


def test_an_expired_historical_certificate_is_still_evidence(ws):
    env, _device, sig_pub = ws
    old = _cert(env.owner, sig_pub, created_ms=(T0 - 10 * 86400) * 1000, expires_ms=(T0 - 9 * 86400) * 1000)
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub, cert=old)) == "held"


def test_a_cap_rejection_is_dropped(ws, monkeypatch):
    env, _device, _sig_pub = ws
    stray = crypto.public_bytes(crypto.generate_private_key())
    monkeypatch.setattr(pins_mod.HostPins, "note_revocation", lambda *a, **k: False)  # its caps dropped it
    assert env.store.note_forwarded_revocation(item(env.owner, stray)) == "held"  # the ledger never loses it
    assert not any(pair(r) == (certs.device_ref(stray), env.owner.ref) for r in env.store._pins.revocations())


@pytest.mark.parametrize(
    "bad",
    [None, "x", [], {}, {"device_id": "00"}, {"revocation": {"o": {}, "sig": ""}}],
)
def test_garbage_is_dropped(ws, bad):
    env, _device, _sig_pub = ws
    assert env.store.note_forwarded_revocation(bad) == "unverifiable"


def test_garbage_fields_inside_a_well_shaped_item_are_dropped(ws):
    env, _device, sig_pub = ws
    for key, value in (("pk_pub", "!!"), ("device_cert", "x"), ("provenance", None), ("revocation", 3)):
        it = item(env.owner, sig_pub)
        it[key] = value
        assert env.store.note_forwarded_revocation(it) == "unverifiable", key


def test_held_evidence_refuses_that_pair_as_a_restore_signer(ws):
    env, device, sig_pub = ws
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "held"
    assert any(pair(r) == (device, env.owner.ref) for r in env.store._pins.revocations())
    assert env.store.diverged == {}  # forwarded evidence is never claimed by accepted history


# --- review round 1: bounded forwarded evidence (Opus F2, F8) ----------------------------------------------------


def _big_label_item(person: Person, size: int) -> dict:
    sig_pub = crypto.public_bytes(crypto.generate_private_key())
    cert = certs.make_device_cert(
        person.pk_pub,
        person.pk_sign,
        dk_sig_pub=sig_pub,
        dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
        label_sealed=b"L" * size,
        created_ms=T0 * 1000,
        expires_ms=None,
        scopes_max=["look", "decide", "operate", "type"],
    )
    return item(person, sig_pub, cert=cert)


def test_poc_f2_huge_items_are_dropped_and_the_store_stays_usable(ws):
    env, _device, _sig_pub = ws
    for _ in range(3):
        assert env.store.note_forwarded_revocation(_big_label_item(env.owner, 300_000)) == "unverifiable"
    assert _revocations_file(env) == b""
    stray = crypto.public_bytes(crypto.generate_private_key())
    assert env.store.note_forwarded_revocation(item(env.owner, stray)) == "held"
    env.store.restore_facts("workspace")  # still readable


def test_an_item_above_4_kib_is_dropped(ws):
    env, _device, _sig_pub = ws
    assert env.store.note_forwarded_revocation(_big_label_item(env.owner, 3000)) == "unverifiable"


def test_extra_keys_are_dropped(ws):
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub)
    it["extra"] = 1
    assert env.store.note_forwarded_revocation(it) == "unverifiable"
    it = item(env.owner, sig_pub)
    it["provenance"]["extra"] = 1
    assert env.store.note_forwarded_revocation(it) == "unverifiable"
    it = item(env.owner, sig_pub)
    del it["chain_reason"]
    assert env.store.note_forwarded_revocation(it) == "unverifiable"


def test_the_record_count_is_bounded_for_every_person_including_the_owner(ws, monkeypatch):
    env, _device, _sig_pub = ws
    monkeypatch.setattr(pins_mod, "MAX_FORWARDED_PER_PERSON_HARD", 3)
    results = [
        env.store.note_forwarded_revocation(item(env.owner, crypto.public_bytes(crypto.generate_private_key())))
        for _ in range(5)
    ]
    assert results == ["held"] * 5  # every one is visible to the owner ...
    assert len([r for r in env.store._pins.revocations() if is_forwarded(r)]) == 3  # ... HostPins keeps its cap


def _add_member(env: Env) -> Person:
    mara = Person()
    env.store.append(
        env.person_event(
            env.owner, "workspace", "member.added", person=mara.ref, name="Mara", role="member",
            pk_pub=crypto.b64u(mara.pk_pub), device_cert=mara.cert(created=T0 * 1000),
        ),
        log="workspace",
    )  # fmt: skip
    return mara


def _stray() -> bytes:
    return crypto.public_bytes(crypto.generate_private_key())


def test_the_total_is_bounded_for_members_and_the_owner_is_exempt_from_it(ws, monkeypatch):
    env, _device, _sig_pub = ws
    mara = _add_member(env)
    monkeypatch.setattr(pins_mod, "MAX_FORWARDED_HARD", 2)
    results = [env.store.note_forwarded_revocation(item(mara, _stray())) for _ in range(4)]
    assert results == ["held"] * 4
    assert len([r for r in env.store._pins.revocations() if is_forwarded(r) and pair(r)[1] == mara.ref]) == 2
    # the owner's records do not count against the total and are not refused by it (only by its own 256)
    assert [env.store.note_forwarded_revocation(item(env.owner, _stray())) for _ in range(3)] == ["held"] * 3


def test_revocations_are_read_without_a_size_limit(ws, monkeypatch):
    env, _device, sig_pub = ws
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "held"
    seen = []
    real = pins_mod.read_or_none

    def spy(path, *a, **k):
        if path.name == "revocations.jsonl":
            seen.append(a[0] if a else k.get("limit", "default"))
        return real(path, *a, **k)

    monkeypatch.setattr(pins_mod, "read_or_none", spy)
    env.store._pins.revocations()
    assert seen == [None]


def test_the_worst_case_file_stays_well_below_the_read_limit():
    from orch.store import fsio
    from orch.store.store import MAX_FORWARDED_ITEM_BYTES

    assert MAX_FORWARDED_ITEM_BYTES < pins_mod.MAX_FORWARDED_RECORD_BYTES
    # non-owner records up to the total, plus the owner's own 256: about 10.5 MiB, below 16 MiB
    worst = (pins_mod.MAX_FORWARDED_HARD + pins_mod.MAX_FORWARDED_PER_PERSON_HARD) * pins_mod.MAX_FORWARDED_RECORD_BYTES
    assert worst < fsio.MAX_READ
