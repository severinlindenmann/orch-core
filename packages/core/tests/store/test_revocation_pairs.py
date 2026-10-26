"""Host durable revocation evidence per (device, person) pair (F1 §5.10, row-151 fix): another person's evidence for
a device id never blocks the owner's restore, the genuine pair does, chain evidence is sticky per pair, and forwarded
evidence is capped without a flood of throwaway persons ever evicting a member's evidence."""

import pytest

from orch import crypto
from orch.identity import certs
from orch.store import StoreError
from orch.store import pins as pins_mod
from orch.store.pins import MAX_FORWARDED_PER_DEVICE, MAX_FORWARDED_PER_PERSON, HostPins, is_forwarded, pair
from tests.identity.helpers import Person
from tests.store.helpers import WS, Env


def _evidence(person: Person, device_sig_pub: bytes, reason: str = "lost", at_ms: int = 1_790_000_100_000):
    """``(revocation, device_cert)`` of ``person`` for the device key ``device_sig_pub`` (any person can certify any
    public key: a certificate is no proof of possession)."""
    cert = certs.make_device_cert(
        person.pk_pub,
        person.pk_sign,
        dk_sig_pub=device_sig_pub,
        dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
        label_sealed=b"x",
        created_ms=1_790_000_000_000,
        expires_ms=None,
        scopes_max=["look", "decide", "operate", "type"],
    )
    rev = certs.make_revocation(
        person.pk_pub, person.pk_sign, device_id_hex=certs.device_ref(device_sig_pub)[2:], revoked_ms=at_ms,
        reason=reason,
    )  # fmt: skip
    return rev, cert


def _pid(person: Person) -> str:
    return "p_" + crypto.person_id(person.pk_pub).hex()


def _note(pins: HostPins, person: Person, sig_pub: bytes, *, at: str, reason="lost", chain=False, members=()):
    rev, cert = _evidence(person, sig_pub, reason)
    member_ids = {_pid(m) for m in members}
    return pins.note_revocation(
        certs.device_ref(sig_pub), reason, rev, at, cert, crypto.b64u(person.pk_pub), chain=chain,
        is_member=None if chain else member_ids.__contains__,
    )  # fmt: skip


def _at(i: int) -> str:
    return f"2026-10-11T{10 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z"


def _key() -> bytes:
    return crypto.public_bytes(crypto.generate_private_key())


def test_another_persons_forwarded_evidence_never_blocks_the_owners_restore(tmp_path):
    env = Env(tmp_path)
    store = env.bootstrap()
    bob = Person()
    # Bob certifies the owner's device key under his own PK and revokes it; the host holds it as forwarded evidence.
    assert _note(store._pins, bob, env.owner.sig_pub, at=_at(0))
    facts = store.restore_facts("workspace")
    assert facts["revocations"] == []  # pending: the device's live association names the owner, not Bob
    result = store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert result.event["type"] == "restore"
    assert store.diverged == {}  # forwarded evidence is not claimed by accepted history


def test_the_genuine_pair_in_forwarded_evidence_refuses_the_signer(tmp_path):
    env = Env(tmp_path)
    store = env.bootstrap()
    bob = Person()
    assert _note(store._pins, bob, env.owner.sig_pub, at=_at(0))  # the poison first
    assert _note(store._pins, env.owner, env.owner.sig_pub, at=_at(1), reason="compromised", members=[env.owner])
    device = certs.device_ref(env.owner.sig_pub)
    assert {pair(r) for r in store._pins.revocations()} == {(device, _pid(bob)), (device, _pid(env.owner))}
    facts = store.restore_facts("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert exc.value.code == "device.invalid"


def test_forwarded_evidence_for_a_tombstone_only_device_is_never_carried(tmp_path):
    """S1: a tombstone is not a workspace association; a stranger's pair for it stays pending at the host."""
    env = Env(tmp_path)
    store = env.bootstrap()
    gone = Person()
    tomb_rev, tomb_cert = _evidence(gone, gone.sig_pub)
    facts = store.restore_facts("workspace")
    facts["revocations"] = [{"revocation": tomb_rev, "device_cert": tomb_cert, "pk_pub": crypto.b64u(gone.pk_pub)}]
    store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    device = certs.device_ref(gone.sig_pub)
    assert (device, _pid(gone)) in store.state.revocation_evidence()  # the tombstone
    assert _note(store._pins, Person(), gone.sig_pub, at=_at(0))  # a stranger's pair for the tombstoned device
    carried = {
        (r["revocation"]["o"]["device_id"], r["revocation"]["o"]["person_id"])
        for r in store.restore_facts("workspace")["revocations"]
    }
    assert carried == {(device[2:], _pid(gone)[2:])}


# --- chain evidence is sticky per pair (Sonnet #397 finding 1) -------------------------------------------------------


def test_chain_evidence_promotes_a_forwarded_record_of_the_pair(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice, laptop = Person(), _key()
    assert _note(pins, alice, laptop, at=_at(0), members=[alice])  # forwarded lost first
    (rec,) = pins.revocations()
    assert is_forwarded(rec)
    assert _note(pins, alice, laptop, at=_at(1), chain=True)  # the same lost now occurs in the log
    (rec,) = pins.revocations()
    assert not is_forwarded(rec) and rec["at"] == _at(1) and "upgrade" not in rec


def test_a_stronger_forwarded_record_survives_promotion_as_upgrade(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice, laptop = Person(), _key()
    assert _note(pins, alice, laptop, at=_at(0), reason="compromised", members=[alice])
    assert _note(pins, alice, laptop, at=_at(1), chain=True)  # a weaker lost occurs in the log
    (rec,) = pins.revocations()
    assert not is_forwarded(rec) and rec["reason"] == "lost" and rec["upgrade"]["reason"] == "compromised"


def test_a_forwarded_upgrade_never_replaces_or_demotes_a_chain_record(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice, laptop = Person(), _key()
    assert _note(pins, alice, laptop, at=_at(0), chain=True)  # chain lost
    assert _note(pins, alice, laptop, at=_at(1), reason="compromised", members=[alice])  # forwarded compromised
    (rec,) = pins.revocations()
    assert not is_forwarded(rec) and rec["reason"] == "lost" and rec["at"] == _at(0)
    assert rec["upgrade"]["reason"] == "compromised"
    assert _note(pins, alice, laptop, at=_at(2), reason="compromised", chain=True)  # the upgrade occurs in the log
    (rec,) = pins.revocations()
    assert not is_forwarded(rec) and rec["reason"] == "compromised" and "upgrade" not in rec


# --- caps and eviction (S2, S4, D3; Sonnet finding 2) ----------------------------------------------------------------


def test_a_flood_of_throwaway_persons_never_evicts_a_members_evidence(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice, laptop = Person(), _key()  # Alice's laptop, unknown to the workspace
    assert _note(pins, alice, laptop, at=_at(0), reason="compromised", members=[alice])
    flood = [Person() for _ in range(3 * MAX_FORWARDED_PER_DEVICE)]
    kept = [_note(pins, p, laptop, at=_at(1 + i), members=[alice]) for i, p in enumerate(flood)]
    # a non-member never evicts: the first ones fill the device's room, every later one is dropped
    room = MAX_FORWARDED_PER_DEVICE - 1
    assert kept == [True] * room + [False] * (len(flood) - room)
    device = certs.device_ref(laptop)
    assert {pair(r) for r in pins.revocations()} == {(device, _pid(alice))} | {(device, _pid(p)) for p in flood[:room]}


def test_a_members_record_evicts_the_oldest_non_member_record(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice, laptop = Person(), _key()
    strangers = [Person() for _ in range(MAX_FORWARDED_PER_DEVICE)]
    for i, p in enumerate(strangers):
        assert _note(pins, p, laptop, at=_at(i))
    assert _note(pins, alice, laptop, at=_at(100), reason="compromised", members=[alice])
    held = {pair(r)[1] for r in pins.revocations()}
    assert held == {_pid(alice)} | {_pid(p) for p in strangers[1:]}  # exactly the oldest non-member went


def test_membership_is_evaluated_at_eviction_time(tmp_path):
    """S4: a person noted as a non-member who has since joined is not evicted for a member's record."""
    pins = HostPins(tmp_path, WS)
    laptop = _key()
    people = [Person() for _ in range(MAX_FORWARDED_PER_DEVICE)]
    for i, p in enumerate(people):
        assert _note(pins, p, laptop, at=_at(i))  # all noted while non-members
    newcomer = Person()
    assert _note(pins, newcomer, laptop, at=_at(100), members=[*people, newcomer])  # by now all are members
    assert len(pins.revocations()) == MAX_FORWARDED_PER_DEVICE + 1  # nothing evicted: no current non-member


def test_the_total_cap_evicts_non_members_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pins_mod, "MAX_FORWARDED_TOTAL", 3)
    pins = HostPins(tmp_path, WS)
    strangers = [Person() for _ in range(3)]
    for i, p in enumerate(strangers):  # three devices, one record each: the total cap is full
        assert _note(pins, p, _key(), at=_at(i))
    assert not _note(pins, Person(), _key(), at=_at(10))  # a non-member never evicts: dropped
    alice = Person()
    assert _note(pins, alice, _key(), at=_at(11), members=[alice])
    held = {pair(r)[1] for r in pins.revocations()}
    assert held == {_pid(alice), _pid(strangers[1]), _pid(strangers[2])}  # strangers[0], the oldest, went


def test_chain_evidence_is_never_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(pins_mod, "MAX_FORWARDED_TOTAL", 2)
    pins = HostPins(tmp_path, WS)
    bob, chain_dev = Person(), _key()
    assert _note(pins, bob, chain_dev, at=_at(0), chain=True)  # a non-member's pair that occurred in the log
    alice = Person()
    for i in range(4):
        assert _note(pins, alice, _key(), at=_at(1 + i), members=[alice])
    assert (certs.device_ref(chain_dev), _pid(bob)) in {pair(r) for r in pins.revocations()}


def test_a_member_is_bounded_per_person(tmp_path):
    """S2: a member's forwarded evidence is never evicted for a non-member's, but it is capped per person."""
    pins = HostPins(tmp_path, WS)
    mallory = Person()
    results = [_note(pins, mallory, _key(), at=_at(i), members=[mallory]) for i in range(MAX_FORWARDED_PER_PERSON + 3)]
    assert results == [True] * MAX_FORWARDED_PER_PERSON + [False] * 3
    assert len(pins.revocations()) == MAX_FORWARDED_PER_PERSON


def test_forwarded_evidence_needs_the_current_roster(tmp_path):
    with pytest.raises(TypeError):
        HostPins(tmp_path, WS).note_revocation("d_" + "ab" * 32, "lost", *_evidence(Person(), _key())[:1],
                                               _at(0), {}, "x", chain=False)  # fmt: skip


def test_the_pinned_owner_is_exempt_from_the_per_person_cap(tmp_path):
    """Genuine owner revocations are never dropped: the per-person cap does not apply to the pinned card owner."""
    pins = HostPins(tmp_path, WS)
    owner = Person()
    for i in range(MAX_FORWARDED_PER_PERSON + 3):
        rev, cert = _evidence(owner, _key())
        assert pins.note_revocation(
            "d_" + rev["o"]["device_id"], "lost", rev, _at(i), cert, crypto.b64u(owner.pk_pub), chain=False,
            is_member={_pid(owner)}.__contains__, owner=_pid(owner),
        )  # fmt: skip
    assert len(pins.revocations()) == MAX_FORWARDED_PER_PERSON + 3


# --- host completeness and rollback detection per pair (Opus #397 R1, R2) --------------------------------------------


def _entry(person: Person, sig_pub: bytes, reason: str = "lost") -> dict:
    rev, cert = _evidence(person, sig_pub, reason)
    return {"revocation": rev, "device_cert": cert, "pk_pub": crypto.b64u(person.pk_pub)}


def test_host_completeness_another_persons_pair_never_satisfies_an_obligation(tmp_path):
    """R1: restore 1 carries (U, zed, lost); restore 2 omits it and carries (U, mara, compromised) instead. The
    obligation is `lost`: a `compromised` one would also fail on its different bytes and hide a device-only check."""
    env = Env(tmp_path)
    store = env.bootstrap()
    zed, mara, laptop = Person(), Person(), _key()
    facts = store.restore_facts("workspace")
    facts["revocations"] = [_entry(zed, laptop, "lost")]
    store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    facts = store.restore_facts("workspace")
    facts["revocations"] = [_entry(mara, laptop, "compromised")]  # zed's obligation omitted, mara's pair instead
    with pytest.raises(StoreError) as exc:
        store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert exc.value.code == "validation.restore"


def test_rollback_detection_is_per_pair(tmp_path):
    """R2: chain pairs of several persons on one tombstoned device. Both pairs in accepted history: nothing flagged
    (a device-only match would compare one pair's evidence with the other's). A pair accepted history lacks (its
    restore was rolled back) is flagged, although the device id still has other pairs in history."""
    env = Env(tmp_path)
    store = env.bootstrap()
    zed, mara, kim, laptop = Person(), Person(), Person(), _key()
    both = sorted([_entry(zed, laptop), _entry(kim, laptop)], key=lambda e: e["revocation"]["o"]["person_id"])
    facts = store.restore_facts("workspace")
    facts["revocations"] = both
    store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    store.close()
    store = env.open()
    assert store.diverged == {}  # control: two pairs of one device, both in accepted history, neither is flagged
    gone = _entry(mara, laptop, "compromised")  # noted from a restore that a rollback then removed from the log
    device = "d_" + gone["revocation"]["o"]["device_id"]
    store._pins.note_revocation(device, "compromised", gone["revocation"], _at(0), gone["device_cert"], gone["pk_pub"])
    store.close()
    store = env.open()
    assert "workspace" in store.diverged and device in store.diverged["workspace"].detail


# -- equal strength never replaces (R4 round 4: N23/N24, the single `revoke_strength`) -----------------------------


def _rec(pins: HostPins, person: Person, key: bytes):
    return next(r for r in pins.revocations() if pair(r) == (certs.device_ref(key), _pid(person)))


def test_an_equal_strength_forwarded_item_keeps_the_earliest_forwarded_record(tmp_path):
    pins = HostPins(tmp_path, WS)
    p, key = Person(), _key()
    assert _note(pins, p, key, at=_at(0), reason="lost", members=[p])
    assert _note(pins, p, key, at=_at(5), reason="retired", members=[p])  # lost = retired: equal strength
    rec = _rec(pins, p, key)
    assert rec["at"] == _at(0) and rec["reason"] == "lost"
    assert _note(pins, p, key, at=_at(9), reason="compromised", members=[p])  # stronger: replaces
    assert _rec(pins, p, key)["reason"] == "compromised"


def test_an_equal_strength_forwarded_item_never_becomes_the_upgrade_of_a_chain_record(tmp_path):
    pins = HostPins(tmp_path, WS)
    p, key = Person(), _key()
    assert _note(pins, p, key, at=_at(0), reason="lost", chain=True)
    assert _note(pins, p, key, at=_at(3), reason="retired", members=[p])  # equal strength: no upgrade
    assert "upgrade" not in _rec(pins, p, key)
    assert _note(pins, p, key, at=_at(4), reason="compromised", members=[p])  # stronger: kept beside the chain record
    rec = _rec(pins, p, key)
    assert rec["reason"] == "lost" and rec["upgrade"]["reason"] == "compromised"


def test_an_equal_strength_chain_record_keeps_the_earliest_of_the_highest_reason(tmp_path):
    pins = HostPins(tmp_path, WS)
    p, key = Person(), _key()
    assert _note(pins, p, key, at=_at(0), reason="lost", chain=True)
    assert _note(pins, p, key, at=_at(7), reason="retired", chain=True)
    assert _rec(pins, p, key)["at"] == _at(0)
    assert _note(pins, p, key, at=_at(8), reason="compromised", chain=True)
    assert _rec(pins, p, key)["reason"] == "compromised"
