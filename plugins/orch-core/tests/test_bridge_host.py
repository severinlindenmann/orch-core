"""orch.remote.bridge_host beyond the vectors: what happens after a request is accepted (scope, fresh assertions,
leases, the stored outcome), restarts, damaged or full storage, registration, the registry's audit log, sealed
answers, and the module's import boundary."""
import hashlib
import json
import os
import struct
import subprocess
import sys

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import test_bridge_host_vectors as V  # noqa: E402
from orch.remote.bridge_host import envelope as E, files, keys, signatures  # noqa: E402
from orch.remote.bridge_host.assertion import AD_AT, AD_UP, AD_UV, assertion_challenge  # noqa: E402
from orch.remote.bridge_host.host_check import Host, Requirement, load_host_key  # noqa: E402
from orch.remote.bridge_host.registry import Credential, Device, Registry, revoke_everywhere  # noqa: E402

WS_HEX = V.VEC["keys"]["workspace"]
WS = bytes.fromhex(WS_HEX)
NOW = 1_790_000_000_000
KEY_A = signatures.private_key(bytes.fromhex(V.VEC["keys"]["device_a"]["d"]))
KEY_B = signatures.private_key(bytes.fromhex(V.VEC["keys"]["device_b"]["d"]))
NEW = signatures.private_key(bytes.fromhex(V.VEC["keys"]["intruder"]["d"]))  # a device that is not registered
AUTH = signatures.private_key(bytes.fromhex(V.VEC["keys"]["authenticator"]["d"]))
CRED_ID = b"credential-one"


def pub(k):
    return signatures.public_bytes(k)


def did(k):
    return keys.device_id(WS, pub(k)).hex()


def route(meta, data):
    path = meta.get("path")
    if path == "/factory/start":
        return Requirement("type", "fresh", {"kind": "charter", "shown": "Start \u202eepic E-1", "digest": ""})
    if path == "/terminal/input":
        return Requirement("type", "lease")
    if path == "/never":
        return None
    if path == "/move":
        return Requirement("decide")
    return Requirement("look")


def credential(count=0, be=False):
    return Credential(CRED_ID, pub(AUTH), count, be, be, V.RP_ID, V.ORIGIN)


def make_host(config_dir, clock, scope_a="type", cred=None, **kw):
    host = Host(workspace=WS, k_ws=V.K_WS, host_key=signatures.private_key(bytes.fromhex(V.VEC["keys"]["host"]["d"])),
                root=files.bridge_dir(config_dir, WS_HEX), clock=clock, route=route,
                phone_key=lambda pid: None, rp_id=V.RP_ID, origin=V.ORIGIN, **kw)
    if not host.registry.devices():
        host.registry.add(Device(did(KEY_A), pub(KEY_A), scope_a, "Laptop", 0, credential=cred), 0)
        host.registry.add(Device(did(KEY_B), pub(KEY_B), "look", "Phone", 0), 0)
    return host


def env(key, meta, data=b"", seq=1, ts=NOW, flags=0, stream=E.ZERO_ID, rid=None, device=None):
    h = E.Header(E.TO_HOST, flags, WS, bytes.fromhex(device or did(key)), rid or os.urandom(16), stream, seq, ts,
                 os.urandom(16))
    hb = h.encode()
    body = keys.seal(V.K_WS, hb, E.frame(meta, data))
    return hb + body + signatures.sign(key, signatures.signed_bytes(hb, body))


def rid_of(e):
    return E.Header.decode(e).rid.hex()


def send(host, e):
    return host.check(e, rid_of(e))


def http(path, **kw):
    return {"op": "http", "method": "POST", "path": path, **kw}


@pytest.fixture
def clock():
    return V.Clock(NOW)


@pytest.fixture
def host(tmp_path, clock):
    return make_host(tmp_path, clock)


# -- after the record: scope, run, stored outcome ----------------------------------------------------------------------

def test_a_request_runs_once_and_its_replay_is_the_stored_outcome(host, clock):
    e = env(KEY_A, http("/move"), b'{"to":"testing"}', seq=1)
    acc = send(host, e)
    assert acc.result == "accept"
    run = host.authorize(acc)
    assert run.result == "run" and run.answer_rids == (acc.rid,) and not run.fresh
    host.finish(run.answer_rids, {"status": 200, "headers": {"content-type": "application/json"}}, b'{"moved":true}')
    clock.now += 10_000
    again = send(host, e)
    assert again.result == "replay" and again.outcome["status"] == 200 and again.body == b'{"moved":true}'


def test_a_body_over_64_kib_keeps_only_its_head_and_a_replay_is_already_done_with_the_status(host):
    e = env(KEY_A, http("/"), seq=1)
    run = host.authorize(send(host, e))
    assert host.finish(run.answer_rids, {"status": 201}, bytes(64 * 1024 + 1)) is True
    again = send(host, e)
    assert (again.result, again.code, again.fields) == ("refuse", "already_done", {"status": 201})


def test_route_scope_is_checked_and_the_refusal_replaces_the_record(host, clock):
    e = env(KEY_B, http("/move"), seq=1)  # device b holds look only
    refused = host.authorize(send(host, e))
    assert (refused.result, refused.code) == ("refuse", "forbidden_scope")
    assert send(host, e).code == "forbidden_scope"  # the stored refusal, not a second decision
    never = host.authorize(send(host, env(KEY_A, http("/never"), seq=1)))
    assert never.code == "forbidden_scope"


def test_a_request_accepted_but_not_finished_is_never_run_again_after_a_restart(tmp_path, clock):
    host = make_host(tmp_path, clock)
    e = env(KEY_A, http("/move"), seq=1)
    assert send(host, e).result == "accept"  # recorded; the host "crashes" before it runs
    clock.now += 5_000
    restarted = make_host(tmp_path, clock)
    v = send(restarted, e)
    assert (v.result, v.code, v.fields) == ("refuse", "already_done", {"status": "unknown"})
    clock.now += 900_000  # the record expired: the seq is consumed, so the same bytes are still refused
    v = send(restarted, e)
    assert (v.code, v.fields) == ("stale_sequence", {"high": 1})


def test_the_sequence_state_survives_a_restart(tmp_path, clock):
    host = make_host(tmp_path, clock)
    for seq in (1, 2, 3):
        assert send(host, env(KEY_A, http("/"), seq=seq)).result == "accept"
    v = send(make_host(tmp_path, clock), env(KEY_A, http("/"), seq=2))
    assert (v.code, v.fields) == ("stale_sequence", {"high": 3})


def test_retention_is_bounded_by_the_host_clock_and_expired_records_are_pruned(host, clock):
    e = env(KEY_A, http("/"), seq=1, ts=NOW + 300_000)
    send(host, e)
    rec = host.store.get(rid_of(e), clock.now)
    assert rec.until == NOW + 900_000 and rec.received_at == NOW
    assert host.store.prune(NOW + 899_999) == 0 and host.store.prune(NOW + 900_000) == 1


def test_a_full_store_drops_new_requests_and_runs_nothing(tmp_path, clock):
    host = make_host(tmp_path, clock, max_records=2)
    assert send(host, env(KEY_A, http("/"), seq=1)).result == "accept"
    assert send(host, env(KEY_A, http("/"), seq=2)).result == "accept"
    v = send(host, env(KEY_A, http("/"), seq=3))
    assert v.result == "drop" and "StoreFull" in v.why
    clock.now += 900_000  # once they expired, there is room again
    assert send(host, env(KEY_A, http("/"), seq=4, ts=clock.now)).result == "accept"


def test_a_damaged_record_never_runs(host, clock):
    e = env(KEY_A, http("/"), seq=1)
    send(host, e)
    path = host.store.dir / f"{rid_of(e)}.json"
    path.write_text("{not json", encoding="utf-8")
    v = send(host, e)
    assert (v.code, v.fields) == ("already_done", {"status": "unknown"})
    rec = json.loads(json.dumps({"v": 1}))
    path.write_text(json.dumps({**rec, "device": did(KEY_A), "digest": "0" * 64, "received_at": NOW,
                                "until": NOW + 10 ** 12, "outcome": None, "body": ""}), encoding="utf-8")
    assert send(host, e).code == "already_done"  # retention other than 900 s is damage, not a longer life
    path.unlink()
    path.symlink_to(host.store.dir / "elsewhere.json")
    assert send(host, e).code == "already_done"  # never through a link


def test_damaged_sequence_state_or_registry_answers_nothing(host, clock):
    (host.store.seq_dir / f"{did(KEY_A)}.json").write_text("[]", encoding="utf-8")
    assert send(host, env(KEY_A, http("/"), seq=1)).result == "drop"
    host.registry.path.write_text("{}", encoding="utf-8")
    assert send(host, env(KEY_B, http("/"), seq=1)).result == "drop"
    with pytest.raises(files.Damaged):  # and nothing is written over it
        host.registry.revoke(did(KEY_B), NOW)


def test_a_registry_entry_whose_id_is_not_its_key_is_damage(host):
    data = json.loads(host.registry.path.read_text(encoding="utf-8"))
    data["devices"][did(KEY_A)]["pub"] = pub(KEY_B).hex()
    host.registry.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(files.Damaged):
        host.registry.devices()


def test_a_revocation_takes_effect_on_the_next_check_and_ends_streams_and_lease(host, clock):
    opening = env(KEY_A, http("/terminal/stream"), seq=1, flags=E.F_STREAM)
    host.authorize(send(host, opening))
    host.leases[did(KEY_A)] = NOW + 60_000
    assert host.revoke(did(KEY_A)) == [rid_of(opening)]
    assert did(KEY_A) not in host.leases and host.streams == {}
    assert send(host, env(KEY_A, http("/"), seq=2)).code == "revoked"
    events = [json.loads(x)["event"] for x in host.registry.audit_path.read_text(encoding="utf-8").splitlines()]
    assert events == ["added", "added", "revoked"]


def test_a_revoked_device_asking_to_pair_gets_a_refusal_the_device_can_verify(host):
    """§6.2: every refusal to an op = "pair" request carries host_pub. A revoked device is refused (it does not pair
    again under its old key, and nothing becomes pending); the refusal must carry host_pub so the device does not
    drop it as no answer (found by the end-to-end run, #94)."""
    offer, _ = host.offer("operate")
    host.revoke(did(KEY_A))
    meta = {"op": "pair", "pairing_id": offer.pairing_id.hex(), "pub": pub(KEY_A).hex(), "label": "again",
            "mac": keys.pair_mac(offer.secret, WS, offer.pairing_id, pub(KEY_A)).hex()}
    v = send(host, env(KEY_A, meta, seq=5))
    assert v.result == "refuse" and v.code == "revoked" and v.fields == {"host_pub": host.host_pub.hex()}
    assert did(KEY_A) not in host.pairing.pending and host.registry.get(did(KEY_A)).revoked
    assert not offer.used, "the owner's link was not spent by a revoked device"
    plain = send(host, env(KEY_A, http("/"), seq=6))
    assert plain.code == "revoked" and plain.fields == {}, "only a pair request's refusal carries host_pub"


def test_a_request_accepted_before_a_revocation_is_refused_at_authorize(host):
    acc = send(host, env(KEY_A, http("/"), seq=1))
    host.revoke(did(KEY_A))
    assert host.authorize(acc).code == "revoked"


def test_the_kill_switch_refuses_everything(host):
    acc = send(host, env(KEY_A, http("/"), seq=1))
    host.stop()
    assert host.authorize(acc).code == "stopped"


def test_cancel_closes_only_the_devices_own_stream(host):
    opening = env(KEY_A, http("/terminal/stream"), seq=1, flags=E.F_STREAM)
    host.authorize(send(host, opening))
    stream = bytes.fromhex(rid_of(opening))
    assert send(host, env(KEY_B, {"op": "cancel"}, seq=1, stream=stream)).code == "forbidden_scope"
    v = host.authorize(send(host, env(KEY_A, {"op": "cancel"}, seq=2, stream=stream)))
    assert v.result == "cancel" and host.streams == {}


def test_a_refused_stream_opening_does_not_leave_the_stream_open(host):
    opening = env(KEY_B, http("/move"), seq=1, flags=E.F_STREAM)
    assert host.authorize(send(host, opening)).code == "forbidden_scope"
    assert host.streams == {}


# -- fresh assertions and the typing lease ----------------------------------------------------------------------------

def assertion_for(refusal, rid1: str, count=1, flags=AD_UP | AD_UV, key=AUTH, cid=CRED_ID):
    f = refusal.fields
    ch = assertion_challenge(WS, bytes.fromhex(did(KEY_A)), bytes.fromhex(rid1), f["purpose"], f["scope"],
                             f["expires_ms"], bytes.fromhex(f["nonce"]), f["subject"])
    ad = hashlib.sha256(V.RP_ID.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
    cdj = E.canonical_json({"type": "webauthn.get", "challenge": E.b64u(ch), "origin": V.ORIGIN, "crossOrigin": False})
    der = key.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    return {"op": "assert", "for": rid1, "credential_id": E.b64u(cid), "authenticator_data": E.b64u(ad),
            "client_data_json": E.b64u(cdj), "signature": E.b64u(der)}


@pytest.fixture
def fhost(tmp_path, clock):
    return make_host(tmp_path, clock, cred=credential())


def test_a_fresh_action_runs_exactly_once_after_its_assertion(fhost, clock):
    r1 = env(KEY_A, http("/factory/start"), seq=1)
    refusal = fhost.authorize(send(fhost, r1))
    assert refusal.code == "assertion_required"
    f = refusal.fields
    assert (f["purpose"], f["scope"], f["expires_ms"]) == ("fresh", "type", NOW + 120_000)
    assert f["subject"] == {"kind": "charter", "shown": "Start epic E-1", "digest": ""}  # cleaned by the host
    assert send(fhost, r1).code == "assertion_required"  # a replay of R1 is the stored refusal, not a new prompt
    clock.now += 5_000
    r2 = env(KEY_A, assertion_for(refusal, rid_of(r1)), seq=2, ts=clock.now)
    run = fhost.authorize(send(fhost, r2))
    assert run.result == "run" and run.fresh and run.rid == rid_of(r1) and run.meta["path"] == "/factory/start"
    assert run.answer_rids == (rid_of(r1), rid_of(r2))
    assert send(fhost, r1).code == "already_done"  # while it runs, R1 is neither prompted for again nor re-run
    fhost.finish(run.answer_rids, {"status": 200}, b"started")
    assert send(fhost, r1).result == "replay" and send(fhost, r2).result == "replay"
    assert fhost.registry.get(did(KEY_A)).credential.sign_count == 1
    again = fhost.authorize(send(fhost, env(KEY_A, assertion_for(refusal, rid_of(r1), count=2), seq=3, ts=clock.now)))
    assert again.code == "assertion_failed"  # single use
    audit = [json.loads(x) for x in fhost.registry.audit_path.read_text(encoding="utf-8").splitlines()]
    done = [a for a in audit if a["event"] == "assertion"]
    assert done[0]["ok"] is True and done[0]["subject"]["kind"] == "charter" and done[1]["ok"] is False


@pytest.mark.parametrize("bad", ["count", "flags", "key", "cid"])
def test_a_bad_assertion_refuses_and_r1_never_runs(fhost, clock, bad):
    fhost.registry.set_sign_count(did(KEY_A), 5, NOW)
    r1 = env(KEY_A, http("/factory/start"), seq=1)
    refusal = fhost.authorize(send(fhost, r1))
    kw = {"count": dict(count=5), "flags": dict(count=6, flags=AD_UP), "key": dict(count=6, key=NEW),
          "cid": dict(count=6, cid=b"other")}[bad]
    v = fhost.authorize(send(fhost, env(KEY_A, assertion_for(refusal, rid_of(r1), **kw), seq=2)))
    assert v.code == "assertion_failed" and rid_of(r1) not in fhost.parked
    assert send(fhost, r1).code == "assertion_required"  # R1's record keeps its refusal: it never ran


def test_an_assertion_from_another_device_does_not_release_r1(tmp_path, clock):
    host = make_host(tmp_path, clock, cred=credential())
    host.registry.set_scope(did(KEY_B), "type", NOW)
    r1 = env(KEY_A, http("/factory/start"), seq=1)
    refusal = host.authorize(send(host, r1))
    v = host.authorize(send(host, env(KEY_B, assertion_for(refusal, rid_of(r1)), seq=1)))
    assert v.code == "assertion_failed"


def test_fresh_actions_are_rate_limited_before_any_challenge(fhost, clock):
    codes = [fhost.authorize(send(fhost, env(KEY_A, http("/factory/start"), seq=i))).code for i in range(1, 8)]
    assert codes == ["assertion_required"] * 6 + ["assertion_failed"]
    clock.now += 600_000
    assert fhost.authorize(send(fhost, env(KEY_A, http("/factory/start"), seq=8, ts=clock.now))).code \
        == "assertion_required"


def test_the_typing_lease_covers_input_to_the_devices_own_open_stream_only(fhost, clock):
    opening = env(KEY_A, http("/terminal/stream"), seq=1, flags=E.F_STREAM)
    assert fhost.authorize(send(fhost, opening)).result == "run"
    stream = bytes.fromhex(rid_of(opening))
    k1 = env(KEY_A, http("/terminal/input"), b"ls\n", seq=2, stream=stream)
    refusal = fhost.authorize(send(fhost, k1))
    assert refusal.code == "lease_required" and refusal.fields["subject"]["kind"] == "lease"
    run = fhost.authorize(send(fhost, env(KEY_A, assertion_for(refusal, rid_of(k1)), seq=3)))
    assert run.result == "run" and not run.fresh and run.rid == rid_of(k1)
    k2 = env(KEY_A, http("/terminal/input"), b"pwd\n", seq=4, stream=stream)
    assert fhost.authorize(send(fhost, k2)).result == "run"  # inside the lease
    k_late = send(fhost, env(KEY_A, http("/terminal/input"), b"q", seq=40, stream=stream))
    fhost.close_stream(rid_of(opening))  # the stream ended between the check and authorize
    assert fhost.authorize(k_late).code == "assertion_failed"
    fhost.streams[rid_of(opening)] = did(KEY_A)
    no_stream = fhost.authorize(send(fhost, env(KEY_A, http("/terminal/input"), seq=5)))
    assert no_stream.code == "assertion_failed"  # not stream input: no lease class, and no subject for a fresh one
    clock.now += 15 * 60_000
    k3 = env(KEY_A, http("/terminal/input"), b"x", seq=6, stream=stream, ts=clock.now)
    assert fhost.authorize(send(fhost, k3)).code == "lease_required"  # the lease ended
    fhost.set_scope(did(KEY_A), "operate")
    assert did(KEY_A) not in fhost.leases and fhost.streams == {}


def test_a_restart_forgets_leases_and_challenges(tmp_path, clock):
    host = make_host(tmp_path, clock, cred=credential())
    r1 = env(KEY_A, http("/factory/start"), seq=1)
    refusal = host.authorize(send(host, r1))
    restarted = make_host(tmp_path, clock)
    v = restarted.authorize(send(restarted, env(KEY_A, assertion_for(refusal, rid_of(r1)), seq=2)))
    assert v.code == "assertion_failed"


# -- pairing, registration, approval -------------------------------------------------------------------------------

def cbor(x) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | ai]) + n.to_bytes(size, "big")
    if isinstance(x, bool):
        return bytes([0xF5 if x else 0xF4])
    if isinstance(x, int):
        return head(0, x) if x >= 0 else head(1, -1 - x)
    if isinstance(x, bytes):
        return head(2, len(x)) + x
    if isinstance(x, str):
        return head(3, len(x.encode())) + x.encode()
    if isinstance(x, dict):
        return head(5, len(x)) + b"".join(cbor(k) + cbor(v) for k, v in x.items())
    raise TypeError(x)


def attestation(ch, flags=AD_UP | AD_UV | AD_AT, cid=CRED_ID, alg=-7, typ="webauthn.create", origin=V.ORIGIN):
    p = pub(AUTH)
    cose = cbor({1: 2, 3: alg, -1: 1, -2: p[1:33], -3: p[33:]})
    ad = (hashlib.sha256(V.RP_ID.encode()).digest() + bytes([flags]) + struct.pack(">I", 0) + bytes(16)
          + len(cid).to_bytes(2, "big") + cid + cose)
    cdj = E.canonical_json({"type": typ, "challenge": E.b64u(ch), "origin": origin, "crossOrigin": False})
    return {"op": "credential_finish", "credential_id": E.b64u(CRED_ID),
            "attestation_object": E.b64u(cbor({"fmt": "none", "attStmt": {}, "authData": ad})),
            "client_data_json": E.b64u(cdj)}


def open_pairing(host, clock, scope="operate"):
    offer, fragment = host.offer(scope)
    meta = {"op": "pair", "pairing_id": offer.pairing_id.hex(), "pub": pub(NEW).hex(), "label": "My \u200bphone",
            "mac": keys.pair_mac(offer.secret, WS, offer.pairing_id, pub(NEW)).hex()}
    v = send(host, env(NEW, meta, seq=1))
    assert v.result == "pair_pending" and v.fields["fingerprint"] == keys.device_fingerprint(pub(NEW))
    return offer


def registration_challenge_of(v):
    from orch.remote.bridge_host.assertion import registration_challenge
    return registration_challenge(WS, bytes.fromhex(did(NEW)), v.fields["expires_ms"], bytes.fromhex(v.fields["nonce"]))


def test_pairing_registration_and_approval(host, clock):
    open_pairing(host, clock)
    assert host.pairing.pending[did(NEW)].label == "My phone"
    begin = send(host, env(NEW, {"op": "credential_begin"}, seq=2))
    assert begin.result == "credential_begin" and begin.fields["expires_ms"] == NOW + 120_000
    done = send(host, env(NEW, attestation(registration_challenge_of(begin)), seq=3))
    assert done.result == "credential_finish" and done.fields == {"registered": True, "synced": False}
    with pytest.raises(ValueError):
        host.approve(did(NEW), scope="type")  # only ever lowered
    dev = host.approve(did(NEW), scope="look")
    assert dev.scope == "look" and dev.credential.pub == pub(AUTH) and dev.credential.sign_count == 0
    assert host.registry.get(did(NEW)).label == "My phone"
    v = host.authorize(send(host, env(NEW, {"op": "pair_status"}, seq=1)))
    assert v.result == "pair_status" and v.fields == {"state": "approved", "scope": "look"}


@pytest.mark.parametrize("bad", ["no_uv", "no_at", "other_alg", "other_cid", "get_type", "other_origin", "replayed"])
def test_a_bad_registration_is_refused(host, clock, bad):
    open_pairing(host, clock)
    begin = send(host, env(NEW, {"op": "credential_begin"}, seq=2))
    ch = registration_challenge_of(begin)
    kw = {"no_uv": dict(flags=AD_UP | AD_AT), "no_at": dict(flags=AD_UP | AD_UV), "other_alg": dict(alg=-8),
          "other_cid": dict(cid=b"another"), "get_type": dict(typ="webauthn.get"),
          "other_origin": dict(origin="https://evil.example"), "replayed": {}}[bad]
    if bad == "replayed":
        assert send(host, env(NEW, attestation(ch), seq=3)).result == "credential_finish"
    v = send(host, env(NEW, attestation(ch, **kw), seq=4))
    assert (v.result, v.code) == ("refuse", "assertion_failed")


def test_a_rejected_pairing_is_reported_and_writes_nothing(host, clock):
    open_pairing(host, clock)
    host.reject(did(NEW))
    assert send(host, env(NEW, {"op": "pair_status"}, seq=2)).fields == {"state": "rejected"}
    assert did(NEW) not in host.registry.devices()


def test_an_offer_expires_after_ten_minutes(host, clock):
    offer, _ = host.offer("look")
    clock.now += 600_000
    meta = {"op": "pair", "pairing_id": offer.pairing_id.hex(), "pub": pub(NEW).hex(),
            "mac": keys.pair_mac(offer.secret, WS, offer.pairing_id, pub(NEW)).hex()}
    assert send(host, env(NEW, meta, seq=1, ts=clock.now)).code == "pairing_closed"


def test_revoking_by_key_reaches_every_workspace_on_this_computer(tmp_path, clock):
    other_hex = "ab" * 16
    other = Registry(files.bridge_dir(tmp_path, other_hex), bytes.fromhex(other_hex))
    other_id = keys.device_id(bytes.fromhex(other_hex), pub(KEY_A)).hex()
    other.add(Device(other_id, pub(KEY_A), "look", "Laptop", 0), 0)
    host = make_host(tmp_path, clock)
    done, damaged = revoke_everywhere(tmp_path, pub(KEY_A), NOW)
    assert sorted(done) == sorted([(WS_HEX, did(KEY_A)), (other_hex, other_id)]) and damaged == []
    assert other.get(other_id).revoked and host.registry.get(did(KEY_A)).revoked and not host.registry.get(did(KEY_B)).revoked


# -- answers -------------------------------------------------------------------------------------------------------

def test_sealed_answers_pass_the_devices_checks(host, clock):
    acc = send(host, env(KEY_A, http("/"), seq=1))
    pending = {acc.rid: {"next": 0, "stream": False}}
    chunk = host.seal_chunk(acc.header, 0, {"status": 200, "headers": {}}, b"ok", last=True)
    mailbox = {"id": acc.rid, "idx": 0, "last": True, "stream": False}
    assert V.device_check(chunk, pending, mailbox, NOW, 0)["data"] == b"ok".hex()
    stale = send(host, env(KEY_A, http("/"), seq=2, ts=NOW - 400_000))
    refusal = host.seal_refusal(stale)
    got = V.device_check(refusal, {stale.header.rid.hex(): {"next": 0, "stream": False}},
                         {"id": stale.header.rid.hex(), "idx": 0, "last": True, "stream": False}, NOW, 0)
    assert got["refusal"] and got["meta"] == {"refusal": "stale_timestamp", "host_ms": NOW}
    h = E.Header.decode(refusal)
    assert h.direction == E.TO_DEVICE and h.flags == E.F_LAST | E.F_REFUSAL and h.ts_ms == NOW
    assert E.Header.decode(host.seal_refusal(stale)).salt != h.salt  # a fresh salt for every seal
    with pytest.raises(ValueError):
        host.seal_chunk(acc.header, 0, {}, bytes(E.MAX_CHUNK), last=True)


def test_the_host_key_is_created_once_and_never_replaced(tmp_path):
    root = files.bridge_dir(tmp_path, WS_HEX)
    k1 = load_host_key(root, os.urandom)
    assert pub(load_host_key(root, os.urandom)) == pub(k1)
    assert oct((root / "host-key.pem").stat().st_mode & 0o777) == "0o600"
    (root / "host-key.pem").write_bytes(b"garbage")
    with pytest.raises(files.Damaged):
        load_host_key(root, os.urandom)


def test_records_live_under_the_config_dirs_permits_folder(host, tmp_path):
    assert host.root == tmp_path / "permits" / "bridge" / WS_HEX
    assert {p.name for p in host.root.iterdir()} >= {"registry.json", "audit.jsonl", "requests", "seq"}
    assert oct(host.root.stat().st_mode & 0o777) == "0o700"


# -- strict parsing -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b'{"op":"http","op":"cancel"}', b'{"op":NaN}', b"[1]", b'"x"', b"\xff\xfe"])
def test_meta_must_be_one_strict_json_object(raw):
    with pytest.raises(E.Malformed):
        E.unframe(struct.pack(">I", len(raw)) + raw)


@pytest.mark.parametrize("text", ["AA==", "A", "a+b/", "AB", "a b", None])  # "AB": not canonical (low bits set)
def test_base64url_is_strict(text):
    with pytest.raises(ValueError):
        E.unb64u(text)


def test_a_malformed_envelope_from_a_registered_device_is_recorded_without_its_seq(host):
    h = E.Header(E.TO_HOST, 0, WS, bytes.fromhex(did(KEY_A)), os.urandom(16), E.ZERO_ID, 1, NOW, os.urandom(16))
    hb = h.encode()
    body = keys.seal(V.K_WS, hb, struct.pack(">I", 7) + b'{"a":1,')
    e = hb + body + signatures.sign(KEY_A, signatures.signed_bytes(hb, body))
    assert send(host, e).code == "malformed"
    assert host.store.seq_state(did(KEY_A)) == (0, 0)


# -- the import boundary ------------------------------------------------------------------------------------------

def test_without_cryptography_the_package_imports_and_says_how_to_fix_it():
    code = ("import sys\n"
            "for m in [m for m in sys.modules if m.startswith('cryptography')]: del sys.modules[m]\n"
            "sys.modules['cryptography'] = None\n"
            "import orch.remote.bridge_host as b\n"
            "assert b.available() is False\n"
            "import orch.dashboard.app, orch.cli\n"
            "try:\n"
            "    import orch.remote.bridge_host.host_check\n"
            "except b.MissingCryptography as e:\n"
            "    assert 'dashboard extra' in str(e)\n"
            "    print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr


def test_the_module_has_no_network_dashboard_or_global_state():
    from pathlib import Path
    import orch.remote.bridge_host as pkg
    import ast
    allowed = {"__future__", "base64", "binascii", "dataclasses", "hashlib", "hmac", "json", "os", "re", "stat",
               "struct", "tempfile", "typing", "unicodedata", "pathlib", "filelock", "cryptography", "orch"}
    for path in Path(pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert name.split(".")[0] in allowed, (path.name, name)  # no network, clock or random source
                assert not name.startswith("orch.") or name.startswith("orch.remote.bridge_host"), (path.name, name)
            assert not isinstance(node, (ast.Global, ast.Nonlocal)), path.name
        defaults = set()  # os.urandom only as the default of a parameter or a dataclass field: injected, CSPRNG by default
        for node in ast.walk(tree):
            if isinstance(node, ast.arguments):
                defaults |= {id(d) for d in node.defaults + [d for d in node.kw_defaults if d is not None]}
            if isinstance(node, ast.AnnAssign) and node.value is not None:
                defaults.add(id(node.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                    and node.attr == "urandom":
                assert id(node) in defaults, (path.name, node.lineno)


# -- one decision, one reading of the registry, checked again right before anything runs ---------------------------

def test_a_decision_carries_what_it_was_taken_on(host, clock):
    acc = send(host, env(KEY_A, http("/move"), seq=1))
    gen, devs = host.registry.snapshot()
    assert (acc.entry, acc.gen, acc.scope, acc.at_ms) == (devs[did(KEY_A)], devs[did(KEY_A)].gen, "type", NOW)
    assert host.store.get(acc.rid, NOW).outcome is None  # recorded, not yet decided
    run = host.authorize(acc)
    assert run.gen == acc.gen and run.until_ms is None and host.still_authorized(run)
    assert "_running" in host.store.get(acc.rid, NOW).outcome  # running only once the whole decision passed


@pytest.mark.parametrize("change,code", [("revoke", "revoked"), ("rescope", "scope_changed")])
def test_a_change_between_check_and_authorize_refuses(host, change, code):
    acc = send(host, env(KEY_A, http("/move"), seq=1))
    if change == "revoke":
        host.registry.revoke(did(KEY_A), NOW)  # from outside this Host (the Remote tab)
    else:
        host.registry.set_scope(did(KEY_A), "type", NOW)  # same scope, new generation: still a change
    v = host.authorize(acc)
    assert v.code == code and host.store.get(acc.rid, NOW).outcome == {"refusal": code}


def test_a_replaced_entry_between_check_and_authorize_never_runs(host):
    acc = send(host, env(KEY_A, http("/move"), seq=1))
    data = json.loads(host.registry.path.read_text(encoding="utf-8"))
    data["devices"][did(KEY_A)]["pub"] = pub(KEY_B).hex()  # a different key under the same id
    host.registry.path.write_text(json.dumps(data), encoding="utf-8")
    assert host.authorize(acc).result == "drop"
    assert host.store.get(acc.rid, NOW).outcome is None  # never running: a retry is already_done/unknown


@pytest.mark.parametrize("how", ["host_revoke", "registry_revoke", "registry_rescope", "kill"])
def test_a_change_between_authorize_and_running_is_seen_and_its_result_never_stored(host, how):
    acc = send(host, env(KEY_A, http("/move"), seq=1))
    run = host.authorize(acc)
    {"host_revoke": lambda: host.revoke(did(KEY_A)), "registry_revoke": lambda: host.registry.revoke(did(KEY_A), NOW),
     "registry_rescope": lambda: host.registry.set_scope(did(KEY_A), "decide", NOW), "kill": host.stop}[how]()
    assert not host.still_authorized(run)
    if how == "kill":
        return
    assert host.finish(run.answer_rids, {"status": 200}, b"secret") is False
    out = host.store.get(acc.rid, NOW).outcome
    assert out == {"refusal": "scope_changed" if how == "registry_rescope" else "revoked"}


def test_revoking_marks_every_unfinished_record_refused(host):
    decided = host.authorize(send(host, env(KEY_A, http("/move"), seq=1)))
    undecided = send(host, env(KEY_A, http("/move"), seq=2))
    finished = host.authorize(send(host, env(KEY_A, http("/"), seq=3)))
    host.finish(finished.answer_rids, {"status": 200})
    host.revoke(did(KEY_A))
    assert host.store.get(decided.rid, NOW).outcome == {"refusal": "revoked"}
    assert host.store.get(undecided.rid, NOW).outcome == {"refusal": "revoked"}
    assert host.store.get(finished.rid, NOW).outcome == {"status": 200}
    assert host.authorize(undecided).code == "revoked"


def test_lease_and_fresh_grants_end(fhost, clock):
    opening = env(KEY_A, http("/terminal/stream"), seq=1, flags=E.F_STREAM)
    fhost.authorize(send(fhost, opening))
    stream = bytes.fromhex(rid_of(opening))
    k1 = env(KEY_A, http("/terminal/input"), b"ls\n", seq=2, stream=stream)
    refusal = fhost.authorize(send(fhost, k1))
    run = fhost.authorize(send(fhost, env(KEY_A, assertion_for(refusal, rid_of(k1)), seq=3)))
    assert run.until_ms == NOW + 15 * 60_000 and fhost.still_authorized(run)
    r1 = env(KEY_A, http("/factory/start"), seq=4)
    fresh_refusal = fhost.authorize(send(fhost, r1))
    fresh = fhost.authorize(send(fhost, env(KEY_A, assertion_for(fresh_refusal, rid_of(r1), count=2), seq=5)))
    assert fresh.fresh and fresh.until_ms == fresh_refusal.fields["expires_ms"] and fhost.still_authorized(fresh)
    clock.now = fresh.until_ms
    assert not fhost.still_authorized(fresh)  # the assertion's window ended before it ran
    clock.now = run.until_ms
    assert not fhost.still_authorized(run)  # the lease ended


def test_readers_never_see_a_half_updated_registry(tmp_path, clock):
    import threading
    host = make_host(tmp_path, clock)
    errors, stop = [], threading.Event()

    def writer():
        try:
            for i in range(60):
                host.registry.set_scope(did(KEY_B), ("look", "operate")[i % 2], NOW)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def reader():
        last = -1
        try:
            while not stop.is_set():
                gen, devs = host.registry.snapshot()
                b = devs[did(KEY_B)]
                assert gen >= last and b.gen <= gen and b.scope in ("look", "operate") and len(devs) == 2
                last = gen
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and host.registry.snapshot()[0] == 62  # two additions, sixty changes


# -- review follow-ups: the request store quota, locked pairing calls, defaults, lease order, surviving mutants ------

from orch.remote.bridge_host import replay_store as RS  # noqa: E402


def test_the_store_caps():
    assert (RS.PER_DEVICE, RS.MAX_RECORDS, RS.BUSY_ALLOWANCE) == (1024, 16384, 64)


def test_a_device_over_its_quota_is_refused_busy_and_another_device_is_not(tmp_path, clock):
    host = make_host(tmp_path, clock, per_device=3)
    for seq in (1, 2, 3):
        assert send(host, env(KEY_A, http("/"), seq=seq)).result == "accept"
    over = env(KEY_A, http("/"), seq=4)
    v = send(host, over)
    assert (v.result, v.code, v.fields) == ("refuse", "busy", {})
    assert host.store.get(rid_of(over), NOW).outcome == {"refusal": "busy"}  # recorded before it is sent
    assert host.store.seq_state(did(KEY_A)) == (3, 0b111)  # busy does not consume the seq
    again = send(host, over)
    assert (again.code, again.why) == ("busy", "replay")  # replayable: the stored refusal
    assert send(host, env(KEY_B, http("/"), seq=1)).result == "accept"  # another device is unaffected
    chunk = host.seal_refusal(v)  # signed and sealed like every refusal, no field but the code
    got = V.device_check(chunk, {rid_of(over): {"next": 0, "stream": False}},
                         {"id": rid_of(over), "idx": 0, "last": True, "stream": False}, NOW, 0)
    assert got["refusal"] and got["meta"] == {"refusal": "busy"}


def test_past_the_busy_allowance_a_device_is_dropped_and_others_still_run(tmp_path, clock):
    host = make_host(tmp_path, clock, per_device=2, busy_allowance=2)
    codes = [send(host, env(KEY_A, http("/"), seq=s)) for s in range(1, 6)]
    assert [c.code or c.result for c in codes] == ["accept", "accept", "busy", "busy", "drop"]
    assert codes[-1].why == "busy_unrecordable"
    assert send(host, env(KEY_B, http("/"), seq=1)).result == "accept"
    clock.now += 900_000  # the records expire: the device's quota is free again
    assert send(host, env(KEY_A, http("/"), seq=10, ts=clock.now)).result == "accept"


def test_a_store_that_cannot_record_drops(tmp_path, clock):
    host = make_host(tmp_path, clock, max_records=2)
    send(host, env(KEY_A, http("/"), seq=1))
    send(host, env(KEY_B, http("/"), seq=1))
    v = send(host, env(KEY_A, http("/"), seq=2))
    assert v.result == "drop" and "StoreFull" in v.why


def test_pruning_reads_no_file_and_damaged_files_are_counted(tmp_path, clock, monkeypatch):
    host = make_host(tmp_path, clock, per_device=5000)
    for seq in range(1, 51):
        send(host, env(KEY_A, http("/"), seq=seq))
    (host.store.dir / ("ab" * 16 + ".json")).write_text("not a record", encoding="utf-8")
    reopened = RS.ReplayStore(host.root)  # the one full read is when the store opens
    assert reopened.damaged == {"ab" * 16} and reopened.count_for(did(KEY_A), NOW) == 50
    reads = []
    real = files.read
    monkeypatch.setattr(files, "read", lambda *a, **k: reads.append(a) or real(*a, **k))
    assert reopened.prune(NOW + 900_000) == 50 and reads == []  # expiry from the index, no file read
    assert reopened.damaged == {"ab" * 16} and (reopened.dir / ("ab" * 16 + ".json")).exists()  # kept, counted
    monkeypatch.setattr(files, "read", real)
    assert make_host(tmp_path, clock).health()["damaged_records"] == ["ab" * 16]


# A vector-style table of the quota rule (the shared vector file cannot change in this PR). The spec text implemented:
# "The request store keeps at most 1024 unexpired records per device and 16384 in total. A device over its quota is
# refused `busy` (recorded); other devices are unaffected. A host that cannot record drops."
QUOTA_CASES = [
    {"name": "under_quota_runs", "per_device": 2, "held_a": 1, "held_b": 0, "sender": "a", "expect": "accept"},
    {"name": "at_quota_refused_busy", "per_device": 2, "held_a": 2, "held_b": 0, "sender": "a", "expect": "busy"},
    {"name": "other_device_unaffected", "per_device": 2, "held_a": 2, "held_b": 0, "sender": "b", "expect": "accept"},
    {"name": "total_full_drops", "per_device": 2, "max_records": 3, "held_a": 2, "held_b": 1, "sender": "b",
     "expect": "drop"},
]


@pytest.mark.parametrize("case", QUOTA_CASES, ids=lambda c: c["name"])
def test_quota_case(tmp_path, clock, case):
    host = make_host(tmp_path, clock, per_device=case["per_device"], max_records=case.get("max_records"))
    for key, n in ((KEY_A, case["held_a"]), (KEY_B, case["held_b"])):
        for seq in range(1, n + 1):
            assert send(host, env(key, http("/"), seq=seq)).result == "accept"
    key = KEY_A if case["sender"] == "a" else KEY_B
    v = send(host, env(key, http("/"), seq=100))
    assert (v.code if v.result == "refuse" else v.result) == case["expect"]


def test_offers_and_approvals_go_through_the_host_lock(host, monkeypatch):
    entered = []

    class Lock:
        def __enter__(self):
            entered.append(1)

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(host, "_lock", Lock())
    host.offer("look")
    with pytest.raises(LookupError):
        host.approve("00" * 16)
    with pytest.raises(LookupError):
        host.reject("00" * 16)
    assert len(entered) == 3


def test_the_random_source_defaults_to_the_os_csprng(tmp_path, clock):
    import inspect
    assert make_host(tmp_path, clock).rand is os.urandom
    assert inspect.signature(load_host_key).parameters["rand"].default is os.urandom
    assert inspect.signature(signatures.generate).parameters["rand"].default is os.urandom


def test_a_lease_whose_r1_fails_its_recheck_opens_no_lease(tmp_path, clock):
    host = make_host(tmp_path, clock, cred=credential())
    opening = env(KEY_A, http("/terminal/stream"), seq=1, flags=E.F_STREAM)
    host.authorize(send(host, opening))
    k1 = env(KEY_A, http("/terminal/input"), b"ls\n", seq=2, stream=bytes.fromhex(rid_of(opening)))
    refusal = host.authorize(send(host, k1))
    assert refusal.code == "lease_required"
    host.route = lambda meta, data: None  # R1's route is no longer remote when it is checked again
    v = host.authorize(send(host, env(KEY_A, assertion_for(refusal, rid_of(k1)), seq=3)))
    assert v.code == "forbidden_scope" and did(KEY_A) not in host.leases


def test_an_assertion_for_one_request_never_runs_another_parked_one(fhost):
    r1 = env(KEY_A, http("/factory/start"), seq=1)
    r1b = env(KEY_A, http("/factory/start"), seq=2)
    refusal1 = fhost.authorize(send(fhost, r1))
    fhost.authorize(send(fhost, r1b))
    a = assertion_for(refusal1, rid_of(r1))
    a["for"] = rid_of(r1b)  # the challenge commits to R1; the request names R1b
    v = fhost.authorize(send(fhost, env(KEY_A, a, seq=3)))
    assert v.code == "assertion_failed" and v.why == "other_request"
    assert send(fhost, r1b).code == "assertion_required"  # R1b never ran


@pytest.mark.parametrize("bad", ["r_zero", "r_n", "s_n", "r_huge"])
def test_out_of_range_scalars_are_refused_before_the_library_is_asked(monkeypatch, bad):
    n = signatures.P256_N.to_bytes(32, "big")
    good = signatures.sign(KEY_A, b"m")
    sig = {"r_zero": bytes(32) + good[32:], "r_n": n + good[32:], "s_n": good[:32] + n,
           "r_huge": b"\xff" * 32 + good[32:]}[bad]

    def reached(_pub):
        raise AssertionError("the library was asked")
    monkeypatch.setattr(signatures, "load_public", reached)
    assert signatures.verify(pub(KEY_A), sig, b"m") is False


def test_a_record_with_a_second_hard_link_is_damaged(tmp_path):
    f = tmp_path / "r.json"
    f.write_text("{}", encoding="utf-8")
    os.link(f, tmp_path / "other")
    with pytest.raises(files.Damaged):
        files.read(f, 1024)


def test_a_resend_with_another_key_under_the_pending_device_id_is_pairing_closed(host, clock):
    offer = open_pairing(host, clock)  # NEW holds the used offer's pending pairing
    other = pub(AUTH)
    meta = {"op": "pair", "pairing_id": offer.pairing_id.hex(), "pub": other.hex(),
            "mac": keys.pair_mac(offer.secret, WS, offer.pairing_id, other).hex()}
    v = send(host, env(AUTH, meta, seq=2, device=did(NEW)))  # the pending device's id, another key
    assert (v.result, v.code) == ("refuse", "pairing_closed")  # not a resend: the offer is used


# -- rules the mutation check found nothing else pinning ----------------------------------------------------------------

def test_damaged_files_count_against_the_total_cap(tmp_path, clock):
    first = make_host(tmp_path, clock, max_records=3)
    for n in range(2):
        (first.store.dir / (f"{n:02x}" * 16 + ".json")).write_text("not a record", encoding="utf-8")
    host = make_host(tmp_path, clock, max_records=3)  # reopened: the two damaged files are known
    assert host.store.damaged and send(host, env(KEY_A, http("/"), seq=1)).result == "accept"
    v = send(host, env(KEY_A, http("/"), seq=2))
    assert v.result == "drop" and "StoreFull" in v.why


def test_a_request_with_the_refusal_flag_is_dropped(host):
    assert send(host, env(KEY_A, http("/"), seq=1, flags=E.F_REFUSAL)).result == "drop"
    assert send(host, env(KEY_A, http("/"), seq=1, flags=E.F_LAST)).result == "drop"
    assert send(host, env(KEY_A, http("/"), seq=1)).result == "accept"  # neither consumed the seq


def test_a_revoked_devices_refusal_spends_the_host_wide_budget(host, clock):
    host.registry.revoke(did(KEY_B), NOW)
    assert send(host, env(KEY_B, http("/"), seq=1)).code == "revoked"
    host.budget.entries = [NOW] * 10
    assert send(host, env(KEY_B, http("/"), seq=2)).result == "drop"
    assert send(host, env(KEY_A, http("/"), seq=1)).result == "accept"  # a verified signature is never dropped


def test_a_bad_signature_pair_refusal_counts_against_the_offers_own_budget(tmp_path):
    case = next(c for c in V.VEC["host_cases"] if c["name"] == "pair_request_device_id_not_of_pub")
    st = case["steps"][0] if "steps" in case else case
    clock = V.Clock(st["now_ms"])
    host = V.host_from_state(tmp_path, case["state"], clock)
    e, mb = bytes.fromhex(st["envelope"]), st["mailbox_id"]
    host.budget.entries = [clock.now] * 10  # the host-wide budget is spent: an open offer is not starved by it
    assert host.check(e, mb).code == "bad_signature"
    offer = next(iter(host.pairing.offers.values()))
    offer.budget.entries = [clock.now] * 5  # its own budget is spent too
    assert host.check(e, mb).result == "drop"


def test_a_result_is_stored_once_and_only_for_a_running_record(host):
    run = host.authorize(send(host, env(KEY_A, http("/"), seq=1)))
    undecided = send(host, env(KEY_A, http("/"), seq=2))  # recorded, never authorised: not running
    assert host.finish(undecided.rid and (undecided.rid,), {"status": 200}) is False
    assert host.finish(run.answer_rids, {"status": 200}, b"one") is True
    assert host.finish(run.answer_rids, {"status": 500}, b"two") is False
    assert host.store.get(run.rid, NOW).outcome == {"status": 200}
