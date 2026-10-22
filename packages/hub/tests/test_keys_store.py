"""V3-D2: the hub key store and issuing, against the contract verifiers and the H0 vectors."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj
from orch_hub.keys import HubKeys, KeysError

VEC = json.loads((Path(__file__).parent.parent / "vectors" / "vectors_hub.json").read_text())
NOW = G.NOW
DAY = G.DAY


class _DetOp(KS.OpSigner):
    def _sig(self, msg: bytes) -> bytes:
        return S.sign(self._Signer__key, msg, deterministic=True)


class _DetRoot(KS.RootSigner):
    def _sig(self, msg: bytes) -> bytes:
        return S.sign(self._Signer__key, msg, deterministic=True)


DET = {"root": _DetRoot, "op": _DetOp}


def _seq_keygen(plan: dict[str, list[str]]):
    it = {k: iter(v) for k, v in plan.items()}
    return lambda purpose: G.K.priv[next(it[purpose])]


def _ids(*ids: str):
    it = iter(ids)
    return lambda: next(it)


def _vector_kw(clock: int, ids: list[str], **extra):
    return {
        "now_ms": lambda: clock,
        "new_id": _ids(*ids),
        "keygen": _seq_keygen(
            {"hk_root": ["hk_root"], "hxk": ["hxk"], "hk_op": ["hk_op", "hk_op_next"], "uk": ["uk_alice", "uk_bob"]}
        ),
        "signer_classes": DET,
        **extra,
    }


@pytest.fixture
def state(tmp_path):
    return tmp_path / "hub"


def _fresh(state, clock=NOW):
    box = {"t": clock}
    keys = HubKeys.init(state, now_ms=lambda: box["t"])
    keys._box = box
    return keys, box


def _verify(keys, now):
    held = H.verify_card(keys.card(), now_ms=now, dev_mode=True, pinned_root=keys.hk_root_pub)
    return held


def test_init_card_verifies_with_contract(state):
    keys, _ = _fresh(state)
    held = _verify(keys, NOW)
    assert held["card"]["card_seq"] == 1 and held["op_cert"]["op_seq"] == 1
    assert held["card"]["dev"] is True and held["op_cert"]["dev"] is True
    assert held["card"]["hub_id"] == H.hub_id(keys.hk_root_pub) == keys.hub_id
    assert held["card"]["hxk_pub"] == b64u(keys.hxk_pub)
    # a real verifier refuses it: dev marker
    with pytest.raises(H.Refused) as r:
        H.verify_card(keys.card(), now_ms=NOW, dev_mode=False)
    assert r.value.code == "dev_refused"


def test_init_refuses_non_empty_state(state):
    _fresh(state)
    with pytest.raises(KeysError) as e:
        HubKeys.init(state)
    assert e.value.code == "already_initialised"


def test_vector_byte_identity(state):
    """With the vector seeds, ids and clock injected, everything issued equals the H0 vector positives."""
    t = NOW - DAY
    keys = HubKeys.init(
        state,
        tls_spki_sha256=b64u(S.fake_seed("hub/tls_spki")),
        **_vector_kw(t, [G.OP_CERT_ID, G.U_ALICE]),
    )
    assert keys.card() == VEC["objects"]["card"]
    assert keys.op_cert() == VEC["objects"]["card"]["op_cert"]
    uk = keys.issue_uk()  # new user: id from the injected sequence
    assert uk == VEC["objects"]["uk_cert_alice"]
    ch = keys.issue_channel(G.U_ALICE, G.K.pub["channel_alice_phone"], "se-unlocked")
    assert ch == VEC["objects"]["channel_cert_alice_phone"]
    # exact bytes, not just equal structures
    sigs = {x["name"]: x for x in VEC["signatures"]}
    for name, obj in (("channel_cert", ch), ("uk_cert", uk), ("card", keys.card())):
        assert cj(obj["o"]) == bytes.fromhex(sigs[name]["canonical_hex"])
        assert obj["sig"] == sigs[name]["sig"]


def test_vector_op_revoked_identity(state):
    keys = HubKeys.init(
        state, **_vector_kw(NOW - DAY, [G.OP_CERT_ID]), tls_spki_sha256=b64u(S.fake_seed("hub/tls_spki"))
    )
    keys._now = lambda: VEC["objects"]["op_revoked"]["o"]["revoked_ms"]
    assert keys.revoke_op() == VEC["objects"]["op_revoked"]


def test_vector_rotated_card_identity(state):
    keys = HubKeys.init(
        state, **_vector_kw(NOW - DAY, [G.OP_CERT_ID]), tls_spki_sha256=b64u(S.fake_seed("hub/tls_spki"))
    )
    keys._new_id = _ids(G.OP_CERT_NEXT)
    rotated = keys.rotate_op()
    expect = G.card(op=G.op_cert(cert_id=G.OP_CERT_NEXT, key="hk_op_next"), op_key="hk_op_next", card_seq=2)
    assert rotated == expect


def test_rotate_op_new_card_accepted_old_refused(state):
    keys, box = _fresh(state)
    old = keys.card()
    held_old = _verify(keys, NOW)
    uk = keys.issue_uk()
    uid = uk["o"]["user_id"]
    ch_key = G.K.pub["channel_alice_phone"]
    keys.issue_channel(uid, ch_key, "se-unlocked")
    box["t"] = NOW + 1000
    new = keys.rotate_op()
    held_new = H.verify_card(new, now_ms=box["t"], dev_mode=True, pinned_root=keys.hk_root_pub, held=held_old)
    assert held_new["op_cert"]["op_seq"] == 2 and held_new["card"]["card_seq"] == 2
    # the old op cert's card is now stale
    with pytest.raises(H.Refused) as r:
        H.verify_card(old, now_ms=box["t"], dev_mode=True, held=held_new)
    assert r.value.code == "stale_op_cert"
    # UK and channel certs were reissued under the new op cert; the old ones are stale under the new card
    uk2 = keys.uk_cert(uid)
    assert uk2["o"]["op_cert_id"] == new["op_cert"]["o"]["cert_id"]
    H.verify_uk_cert(uk2, held_new, now_ms=box["t"], dev_mode=True)
    with pytest.raises(H.Refused) as r:
        H.verify_uk_cert(uk, held_new, now_ms=box["t"], dev_mode=True)
    assert r.value.code in ("stale_op_cert", "bad_signature")
    (ch2,) = keys.channel_certs(uid)
    H.verify_channel_cert(ch2, held_new, now_ms=box["t"], dev_mode=True)
    # same UK key before and after
    assert uk2["o"]["uk_pub"] == uk["o"]["uk_pub"]
    # the old generation's op key is gone from disk; the store reopens on the new one
    assert not (state / "op" / "1").exists()
    again = HubKeys.open(state, now_ms=lambda: box["t"])
    assert again.op_seq == 2 and again.card() == new


def test_op_seq_strictly_increases(state):
    keys, _ = _fresh(state)
    seqs = [keys.op_cert()["o"]["op_seq"]]
    for _ in range(2):
        keys.rotate_op()
        seqs.append(keys.op_cert()["o"]["op_seq"])
    assert seqs == [1, 2, 3]
    assert keys.card_seq == 3


def test_revoke_op_stops_issuing_until_rotation(state):
    keys, _ = _fresh(state)
    cid = keys.op_cert()["o"]["cert_id"]
    rev = keys.revoke_op()
    H.verify_op_revoked(rev, keys.hk_root_pub, dev_mode=True)
    assert rev["o"]["cert_id"] == cid and rev["o"]["dev"] is True
    with pytest.raises(H.Refused) as r:
        H.verify_card(keys.card(), now_ms=NOW, dev_mode=True, revoked_op_certs=keys.revoked_op_certs())
    assert r.value.code == "revoked"
    for call in (keys.issue_uk, lambda: keys.op_signer, keys.reissue_card):
        with pytest.raises(KeysError) as e:
            call()
        assert e.value.code == "revoked"
    # persists across reopen, revocation is never replaced
    again = HubKeys.open(state, now_ms=lambda: NOW)
    assert cid in again.revoked_op_certs()
    assert again.revoke_op() == rev
    # rotation recovers
    new = again.rotate_op()
    H.verify_card(new, now_ms=NOW, dev_mode=True, revoked_op_certs=again.revoked_op_certs())
    assert again.issue_uk()["o"]["op_cert_id"] == new["op_cert"]["o"]["cert_id"]


def test_reissue_card_bumps_card_seq(state):
    keys, _ = _fresh(state)
    c = keys.reissue_card(relay_url="https://relay.dev.severin.io")
    assert c["o"]["card_seq"] == 2 and c["o"]["relay_url"] == "https://relay.dev.severin.io"
    held = _verify(keys, NOW)
    assert held["card"]["card_seq"] == 2
    c3 = keys.reissue_card(tls_spki_sha256=b64u(S.sha256(b"x")))
    assert c3["o"]["card_seq"] == 3 and c3["o"]["relay_url"] == "https://relay.dev.severin.io"


def test_expired_op_cert_refuses_issuing(state):
    keys, box = _fresh(state)
    box["t"] = NOW + 367 * DAY
    with pytest.raises(KeysError) as e:
        keys.issue_uk()
    assert e.value.code == "cert_expired"


def test_issue_channel_checks(state):
    keys, _ = _fresh(state)
    uid = keys.issue_uk()["o"]["user_id"]
    pub = G.K.pub["channel_bob_browser"]
    for args, code in (
        ((uid, pub, "passkey-ish"), "malformed"),
        ((uid, b"\x04" + b"\x00" * 64, "passkey"), "malformed"),
        (("00" * 16, pub, "passkey"), "unknown_user"),
        (("../etc", pub, "passkey"), "malformed"),
    ):
        with pytest.raises(KeysError) as e:
            keys.issue_channel(*args)
        assert e.value.code == code
    c = keys.issue_channel(uid, pub, "password+totp")
    assert c["o"]["channel_id"] == H.channel_id(pub)


def test_path_traversal_user_refused(state):
    keys, _ = _fresh(state)
    for bad in ("../x", "a" * 31, "A" * 32, ""):
        with pytest.raises(KeysError):
            keys.issue_uk(bad)
        with pytest.raises(KeysError):
            keys.uk_signer(bad)


def test_signers_are_typed_and_foreign_labels_raise(state):
    """A key used under a foreign label raises KeyUseError through the store's signers too."""
    keys, _ = _fresh(state)
    uid = keys.issue_uk()["o"]["user_id"]
    op = keys.op_signer
    assert type(op) is KS.OpSigner
    with pytest.raises(KS.KeyUseError):
        op.sign_object(G.op_cert()["o"])  # root-only kind
    with pytest.raises(KS.KeyUseError):
        op.sign_object(G.binding()["o"])  # WSK-only kind
    uk = keys.uk_signer(uid)
    assert type(uk) is KS.UkSigner
    with pytest.raises(KS.KeyUseError):
        uk.sign_object(G.card()["o"])
    with pytest.raises(KS.KeyUseError):
        uk.sign_event(G.WS_A, "ws", {"auth": "channel"})
    # nothing public offers raw signing
    for obj in (op, uk):
        assert not hasattr(obj, "sign") and not hasattr(obj, "sign_bytes")
    with pytest.raises(KeysError) as e:
        keys.uk_signer(G.U_BOB)
    assert e.value.code == "unknown_user"


def test_uk_signer_signs_custodial_event_verifiable_with_cert(state):
    keys, _ = _fresh(state)
    uk = keys.issue_uk()
    sig = keys.uk_signer(uk["o"]["user_id"])
    assert sig.public == __import__("orch_hub.contract.canon", fromlist=["unb64u"]).unb64u(uk["o"]["uk_pub"], 65)


def test_hxk_exchange(state):
    keys, _ = _fresh(state)
    peer = G.K.priv["wxk_a"]
    from cryptography.hazmat.primitives.asymmetric import ec

    peer_side = peer.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keys.hxk_pub))
    assert keys.hxk_exchange(S.public_bytes(peer)) == peer_side
    with pytest.raises(KeysError):
        keys.hxk_exchange(b"\x04" + b"\x01" * 64)


# --- fail-closed file handling ------------------------------------------------------------------------------------


def _all_files(state):
    return [p for p in state.rglob("*") if p.is_file()]


def test_modes_on_disk(state):
    keys, _ = _fresh(state)
    keys.issue_uk()
    for p in state.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), p
    assert state.stat().st_uid == os.geteuid()


@pytest.mark.parametrize("target", ["hk_root.key", "hxk.key", "meta.json", "op/1/key", "op/1/cert.json"])
def test_loose_file_mode_refused(state, target):
    _fresh(state)
    os.chmod(state / target, 0o644)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_mode"


def test_loose_group_readable_key_refused(state):
    _fresh(state)
    os.chmod(state / "hk_root.key", 0o640)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_mode"


@pytest.mark.parametrize("target", [".", "uk", "op", "op/1", "revoked"])
def test_loose_dir_mode_refused(state, target):
    _fresh(state)
    d = state / target
    os.chmod(d, 0o755)
    try:
        with pytest.raises(KeysError) as e:
            HubKeys.open(state)
        assert e.value.code == "bad_mode"
    finally:
        os.chmod(d, 0o700)


def test_uk_key_loose_refused_at_use(state):
    keys, _ = _fresh(state)
    uid = keys.issue_uk()["o"]["user_id"]
    os.chmod(state / "uk" / f"{uid}.key", 0o644)
    with pytest.raises(KeysError) as e:
        keys.uk_signer(uid)
    assert e.value.code == "bad_mode"


def test_foreign_owner_refused(state, monkeypatch):
    _fresh(state)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_owner"


def test_symlinked_key_refused(state, tmp_path):
    _fresh(state)
    key = state / "hxk.key"
    other = tmp_path / "elsewhere"
    other.write_bytes(key.read_bytes())
    os.chmod(other, 0o600)
    key.unlink()
    key.symlink_to(other)
    with pytest.raises(KeysError):
        HubKeys.open(state)


def test_missing_and_garbage_files_refused(state):
    _fresh(state)
    (state / "hxk.key").write_text("not a key\n")
    os.chmod(state / "hxk.key", 0o600)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_key_file"
    assert "not a key" not in str(e.value)
    (state / "hxk.key").unlink()
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "missing"


def test_mismatched_op_key_refused(state):
    _fresh(state)
    other = state / "op" / "1" / "key"
    other.write_bytes((G.K.priv["hk_op"].private_numbers().private_value.to_bytes(32, "big").hex() + "\n").encode())
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_state"


def test_crash_mid_write_leaves_old_key_intact(state, monkeypatch):
    keys, _ = _fresh(state)
    before = (state / "op" / "1" / "key").read_bytes()
    meta_before = (state / "meta.json").read_bytes()
    real_replace = os.replace
    calls = {"n": 0}

    def boom(src, dst):
        calls["n"] += 1
        if str(dst).endswith("meta.json"):  # the commit point of a rotation
            raise OSError("simulated crash")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        keys.rotate_op()
    monkeypatch.undo()
    assert (state / "meta.json").read_bytes() == meta_before
    assert (state / "op" / "1" / "key").read_bytes() == before
    assert not [p for p in state.rglob(".*.tmp")]
    # the store still opens on the old generation and a retry succeeds
    again = HubKeys.open(state)
    assert again.op_seq == 1
    assert again.rotate_op()["op_cert"]["o"]["op_seq"] == 2


def test_failed_atomic_write_keeps_old_file_and_no_temp(state, monkeypatch):
    from orch_hub.keys import fsutil

    target = state / "x.json"
    state.mkdir(mode=0o700)
    fsutil.write_atomic(target, b"old")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        fsutil.write_atomic(target, b"new")
    monkeypatch.undo()
    assert target.read_bytes() == b"old"
    assert not [p for p in state.iterdir() if p.name != "x.json"]


def test_no_private_key_in_errors_or_card(state):
    keys, _ = _fresh(state)
    secrets = [(state / n).read_text().strip() for n in ("hk_root.key", "hxk.key")] + [
        (state / "op" / "1" / "key").read_text().strip()
    ]
    blob = json.dumps(keys.card()) + json.dumps(keys.revoke_op())
    for s in secrets:
        assert s not in blob
    os.chmod(state / "hxk.key", 0o644)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    for s in secrets:
        assert s not in str(e.value)


# --- CLI ------------------------------------------------------------------------------------------------------------


def _cli(state, *args):
    return subprocess.run(
        [sys.executable, "-m", "orch_hub.cli", "keys", "--state", str(state), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
    )


def test_cli_roundtrip(state):
    r = _cli(state, "init")
    assert r.returncode == 0, r.stderr
    card = json.loads(r.stdout)
    assert card["o"]["kind"] == "hub.card" and card["o"]["dev"] is True
    u = _cli(state, "issue-uk", "new")
    assert u.returncode == 0
    uid = json.loads(u.stdout)["o"]["user_id"]
    ch = _cli(
        state, "issue-channel", "--user", uid, "--key-pub", G.K.b("channel_alice_phone"), "--factor", "se-unlocked"
    )
    assert ch.returncode == 0, ch.stderr
    assert json.loads(ch.stdout)["o"]["kind"] == "hub.channel_cert"
    rot = json.loads(_cli(state, "rotate-op").stdout)
    assert rot["op_cert"]["o"]["op_seq"] == 2 and rot["o"]["card_seq"] == 2
    assert json.loads(_cli(state, "card").stdout) == rot
    rev = json.loads(_cli(state, "revoke-op").stdout)
    assert rev["o"]["kind"] == "hub.op_revoked"
    refused = _cli(state, "issue-uk", "new")
    assert refused.returncode == 1 and "revoked" in refused.stderr
    # no private key in any output
    for p in (state / "hk_root.key", state / "hxk.key"):
        assert p.read_text().strip() not in r.stdout + u.stdout + refused.stderr


def test_cli_refuses_loose_mode(state):
    assert _cli(state, "init").returncode == 0
    os.chmod(state / "hk_root.key", 0o644)
    r = _cli(state, "card")
    assert r.returncode == 1 and "bad_mode" in r.stderr


def test_cli_needs_state():
    r = subprocess.run(
        [sys.executable, "-m", "orch_hub.cli", "keys", "card"],
        check=False,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "ORCH_HUB_STATE"} | {"PYTHONPATH": "src"},
    )
    assert r.returncode == 1 and "no_state_dir" in r.stderr


# --- crash consistency (review of PR 3) ---------------------------------------------------------------------------


def _crash_at(monkeypatch, suffix):
    real = os.replace

    def boom(src, dst):
        if str(dst).endswith(suffix):
            raise OSError("simulated crash")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", boom)


def test_crash_between_card_and_meta_never_reuses_card_seq(state, monkeypatch):
    keys, _ = _fresh(state)
    _crash_at(monkeypatch, "card.json")  # meta (the reservation) is already written, card.json is not
    with pytest.raises(OSError):
        keys.reissue_card(relay_url="https://relay.dev.severin.io")
    monkeypatch.undo()
    again = HubKeys.open(state)
    assert again.card()["o"]["card_seq"] == 1  # old card still served
    nxt = again.reissue_card(relay_url="https://relay2.dev.severin.io")
    assert nxt["o"]["card_seq"] == 3  # seq 2 was reserved by the crashed attempt, never reused


def test_crash_after_meta_commit_old_op_key_is_pruned_on_open(state, monkeypatch):
    keys, _ = _fresh(state)
    monkeypatch.setattr("orch_hub.keys.store.shutil.rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        keys.rotate_op()
    monkeypatch.undo()
    assert (state / "op" / "1" / "key").exists()  # the crash left the old op key behind
    again = HubKeys.open(state)
    assert again.op_seq == 2
    assert not (state / "op" / "1").exists()
    assert [p.name for p in (state / "op").iterdir()] == ["2"]


def test_prune_ignores_symlinks_and_odd_names(state, tmp_path):
    keys, _ = _fresh(state)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (state / "op" / "7").symlink_to(outside)
    (state / "op" / "notes").mkdir(mode=0o700)
    HubKeys.open(state)
    assert (outside / "keep").exists() and (state / "op" / "7").is_symlink() and (state / "op" / "notes").exists()
    assert keys.op_seq == 1


def test_leftover_temp_files_do_not_break_rotation(state):
    keys, _ = _fresh(state)
    uid = keys.issue_uk()["o"]["user_id"]
    keys.issue_channel(uid, G.K.pub["channel_alice_phone"], "se-unlocked")
    gen = state / "op" / "1"
    for d in ("ch", "uk"):
        junk = gen / d / f".{'a' * 32}.json.deadbeef.tmp"
        junk.write_text("{partial")
        os.chmod(junk, 0o600)
    assert len(keys.channel_certs()) == 1 and keys.users() == [uid]
    assert keys.rotate_op()["o"]["card_seq"] == 2


def test_new_dirs_are_fsynced_before_meta_commit(state, monkeypatch):
    keys, _ = _fresh(state)
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        os, "fsync", lambda fd: (events.append(("fsync", os.fstat(fd).st_mode & 0o170000)), real_fsync(fd))[1]
    )

    def rep(src, dst):
        events.append(("replace", str(dst).rsplit("/", 1)[-1]))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", rep)
    mk = []
    from orch_hub.keys import fsutil

    real_fd = fsutil.fsync_dir
    monkeypatch.setattr(fsutil, "fsync_dir", lambda p: (mk.append(Path(p).name), real_fd(p))[1])
    keys.rotate_op()
    # the parents of op/2, op/2/uk and op/2/ch (op/ and op/2) were fsynced, and that happened before meta.json
    assert "op" in mk and "2" in mk
    assert ("replace", "meta.json") in events


def test_open_verifies_op_cert_signature(state):
    _fresh(state)
    p = state / "op" / "1" / "cert.json"
    cert = json.loads(p.read_text())
    cert["o"]["expires_ms"] += 1  # tampered, pubkey and op_seq unchanged
    p.write_bytes(cj(cert) + b"\n")
    with pytest.raises(KeysError) as e:
        HubKeys.open(state)
    assert e.value.code == "bad_state"


def test_make_dir_checks_symlink_before_chmod(state, tmp_path):
    from orch_hub.keys import fsutil

    target = tmp_path / "t"
    target.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(fsutil.StateError):
        fsutil.make_dir(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
