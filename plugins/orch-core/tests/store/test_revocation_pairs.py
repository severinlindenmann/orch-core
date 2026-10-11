"""Host durable revocation evidence per (device, person) pair (F1 §5.10, row-151 fix): another person's evidence for
a device id never blocks the owner's restore, the genuine pair does, and forwarded evidence is capped without ever
evicting a member's evidence for a flood of throwaway persons."""

import pytest

from orch import crypto
from orch.identity import certs
from orch.store import StoreError
from orch.store.pins import MAX_FORWARDED_PER_DEVICE, HostPins, pair
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


def _note(pins: HostPins, person: Person, sig_pub: bytes, *, at: str, reason="lost", chain=False, member=False):
    rev, cert = _evidence(person, sig_pub, reason)
    return pins.note_revocation(
        certs.device_ref(sig_pub), reason, rev, at, cert, crypto.b64u(person.pk_pub), chain=chain, member=member
    )


def test_another_persons_forwarded_evidence_never_blocks_the_owners_restore(tmp_path):
    env = Env(tmp_path)
    store = env.bootstrap()
    bob = Person()
    # Bob certifies the owner's device key under his own PK and revokes it; the host holds it as forwarded evidence.
    assert _note(store._pins, bob, env.owner.sig_pub, at="2026-10-11T10:00:00Z")
    facts = store.restore_facts("workspace")
    assert facts["revocations"] == []  # pending: the device's live association names the owner, not Bob
    result = store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert result.event["type"] == "restore"
    assert store.diverged == {}  # forwarded evidence is not claimed by accepted history


def test_the_genuine_pair_in_forwarded_evidence_refuses_the_signer(tmp_path):
    env = Env(tmp_path)
    store = env.bootstrap()
    bob = Person()
    assert _note(store._pins, bob, env.owner.sig_pub, at="2026-10-11T10:00:00Z")  # the poison first
    assert _note(
        store._pins, env.owner, env.owner.sig_pub, at="2026-10-11T10:00:01Z", reason="compromised", member=True
    )
    held = {pair(r) for r in store._pins.revocations()}
    device = certs.device_ref(env.owner.sig_pub)
    assert held == {
        (device, "p_" + crypto.person_id(bob.pk_pub).hex()),
        (device, "p_" + crypto.person_id(env.owner.pk_pub).hex()),
    }
    facts = store.restore_facts("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert exc.value.code == "device.invalid"


def test_a_flood_of_throwaway_persons_never_evicts_a_members_evidence(tmp_path):
    pins = HostPins(tmp_path, WS)
    alice = Person()
    laptop = crypto.public_bytes(crypto.generate_private_key())  # Alice's laptop, unknown to the workspace
    assert _note(pins, alice, laptop, at="2026-10-11T10:00:00Z", reason="compromised", member=True)
    for i in range(3 * MAX_FORWARDED_PER_DEVICE):
        _note(pins, Person(), laptop, at=f"2026-10-11T10:{1 + i // 60:02d}:{i % 60:02d}Z")
    records = pins.revocations()
    device = certs.device_ref(laptop)
    mine = [r for r in records if r["device"] == device]
    assert len(mine) <= MAX_FORWARDED_PER_DEVICE  # bounded per device
    alice_pair = (device, "p_" + crypto.person_id(alice.pk_pub).hex())
    assert alice_pair in {pair(r) for r in mine}  # the member's genuine evidence survives the flood
    assert max(r["at"] for r in mine if pair(r) != alice_pair) == "2026-10-11T10:01:23Z"  # oldest non-member out


def test_a_member_is_kept_over_the_cap_and_a_non_member_is_dropped_when_nothing_is_evictable(tmp_path):
    pins = HostPins(tmp_path, WS)
    laptop = crypto.public_bytes(crypto.generate_private_key())
    for i in range(MAX_FORWARDED_PER_DEVICE):
        assert _note(pins, Person(), laptop, at=f"2026-10-11T10:00:{i:02d}Z", member=True)
    assert not _note(pins, Person(), laptop, at="2026-10-11T11:00:00Z")  # non-member: nothing evictable, dropped
    assert _note(pins, Person(), laptop, at="2026-10-11T11:00:01Z", member=True)  # a member is never refused
    assert len(pins.revocations()) == MAX_FORWARDED_PER_DEVICE + 1
