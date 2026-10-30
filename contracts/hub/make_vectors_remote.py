"""Builds ``contracts/hub/vectors_remote.json``: remote access through the hub, contract R0 (protocol.md §10.2c, §8.5
"No custodial type can"; spec ``orch-v2-hub-remote.md`` §5, §7, D84-D93; orch-core #1588).

Test code, deterministic: every key is FAKE (``SHA-256("FAKE orch v2 test vector: remote/" || label)``; X25519 takes
the 32 bytes as is, P-256 the relay map ``int(seed) mod (n-1) + 1``), every ECDSA signature is RFC 6979. The hub
replays EVERY case against ``orch_hub.contract.remote`` (``packages/hub/tests/test_remote_vectors.py``); orch-core
replays every case against its own test-side port (``packages/core/tests/host/test_remote_vectors.py``). Both fail
on a check kind they do not handle and on a missing required case.

``uv run python ../../contracts/hub/make_vectors_remote.py`` (from packages/hub) rewrites the file.

Expected values of the refusal cases are written here by hand; the positive outputs (bytes, hashes) come from the
reference, whose Noise was checked once against an independent implementation (the ``noiseprotocol`` package; see
the PR) and is checked again by core's second hand-written port on every run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import remote as R
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.labels import L

NOW = 1_800_000_000_000
DAY = 24 * 3600 * 1000


def seed(label: str) -> bytes:
    return hashlib.sha256(b"FAKE orch v2 test vector: remote/" + label.encode()).digest()


def hexid(label: str) -> str:
    return seed("id/" + label)[:16].hex()


# --- keys -----------------------------------------------------------------------------------------------------------

X_LABELS = ("dev_a_s", "dev_a_e", "dev_b_s", "dev_b_e", "dev_new_s", "dev_new_e", "wrk", "wrk_other", "host_e")
P_LABELS = ("presence_a", "presence_b", "presence_c", "presence_new", "wsk", "wsk_other", "hk_op", "attacker")
X = {n: seed("x25519/" + n) for n in X_LABELS}
XP = {n: R.x25519_pub(v) for n, v in X.items()}
P = {n: S.key_from_seed(seed("p256/" + n)) for n in P_LABELS}
PP = {n: S.public_bytes(k) for n, k in P.items()}


def sign(name: str, msg: bytes) -> bytes:
    return S.sign(P[name], msg, deterministic=True)


WS, WS2 = hexid("workspace"), hexid("workspace-2")
HUB, HUB2 = hexid("hub"), hexid("hub-2")
STREAM, STREAM2, STREAM_E = hexid("stream-1"), hexid("stream-2"), hexid("stream-enroll")
DEV_A, DEV_B, DEV_C = "rd_" + hexid("device-a"), "rd_" + hexid("device-b"), "rd_" + hexid("device-c")
CODE = "rc_" + hexid("code")
CODE_SECRET = seed("code-secret")[:16]
PERSON, OWNER = "p_" + hexid("person"), "p_" + hexid("owner")
CHALLENGE, CHALLENGE2 = seed("challenge-1"), seed("challenge-2")
NONCE, NONCE2 = seed("nonce-1"), seed("nonce-2")
DIGESTS = sorted(seed(f"view-{i}") for i in range(3))
ULID_T, ULID_E = "01JBQ8Z6R3M4N5P6Q7R8S9T0VW", "01JBQ8Z6R3M4N5P6Q7R8S9T0VX"
HASH = "sha256:" + hashlib.sha256(b"FAKE remote/base").hexdigest()


def entry(device_id: str, name: str, s: str, presence: str, kind: str, level: str, **over: Any) -> dict[str, Any]:
    e = {
        "device_id": device_id,
        "name": name,
        "person": PERSON,
        "transport_pub": b64u(XP[s]),
        "presence_pub": b64u(PP[presence]),
        "presence_kind": kind,
        "level_cap": level,
        "expires_ms": NOW + 7 * DAY,
        "added_ms": NOW - DAY,
        "confirmed_by": OWNER,
    }
    e.update(over)
    return e


ENTRY_A = entry(DEV_A, "Severin's MacBook", "dev_a_s", "presence_a", "platform", "work")
ENTRY_B = entry(DEV_B, "YubiKey on the Linux box", "dev_b_s", "presence_b", "fido2", "terminals")
ENTRY_C = entry(DEV_C, "Old iPad", "dev_new_s", "presence_c", "platform", "view", expires_ms=NOW - 1)


def list_o(devices: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": R.DEVICE_LIST_KIND,
        "hv": 0,
        "workspace_id": WS,
        "version": 3,
        "issued_ms": NOW - 3600_000,
        "devices": sorted(devices, key=lambda d: d["device_id"]),
    }
    o.update(over)
    return o


def signed_list(o: dict[str, Any], key: str = "wsk", label: bytes | None = None) -> dict[str, Any]:
    msg = (label if label is not None else R.RL["sig_remote_devices"]) + cj(o)
    return {"o": o, "sig": b64u(sign(key, msg))}


LIST_O = list_o([ENTRY_A, ENTRY_B, ENTRY_C])
LIST = signed_list(LIST_O)

# --- handshakes -----------------------------------------------------------------------------------------------------


def hs_run(kind: str, ident: str, stream: str, s: str, e: str, *, ws: str = WS, hub: str = HUB) -> dict[str, Any]:
    pro = R.prologue(kind, ws, ident, stream, hub)
    ini, m1 = R.initiator_msg1(pro, X[s], X[e], XP["wrk"])
    resp = R.responder_read_msg1(pro, X["wrk"], m1)
    m2 = R.responder_msg2(resp, X["host_e"], CHALLENGE)
    assert R.initiator_read_msg2(ini, m2) == CHALLENGE and ini.hh == resp.hh
    return {"pro": pro, "m1": m1, "m2": m2, "hh": resp.hh, "ini": ini, "resp": resp}


SESSION = hs_run("session", DEV_A, STREAM, "dev_a_s", "dev_a_e")
SESSION_B = hs_run("session", DEV_B, STREAM2, "dev_b_s", "dev_b_e")
ENROLL = hs_run("enroll", CODE, STREAM_E, "dev_new_s", "dev_new_e")
HH, HH_B, HH_E = SESSION["hh"], SESSION_B["hh"], ENROLL["hh"]


def p256_assert(name: str, msg: bytes) -> dict[str, Any]:
    return {"form": "p256", "sig": b64u(sign(name, msg))}


def ctap2_assert(name: str, msg: bytes, *, flags: int = 0x05, count: int = 7, rp: bytes | None = None) -> dict:
    auth = (rp if rp is not None else R.ctap2_rp_id_hash()) + bytes([flags]) + count.to_bytes(4, "big")
    return {"form": "ctap2", "auth_data": b64u(auth), "sig": b64u(sign(name, auth + hashlib.sha256(msg).digest()))}


OPEN_A = p256_assert("presence_a", R.open_bytes(WS, DEV_A, HH, CHALLENGE))
OPEN_MSG = {"t": "open", "api": [1, 3], "presence": OPEN_A}

ENROLL_KIND = "platform"
ENROLL_MSG = {
    "t": "enroll",
    "name": "Severin's new MacBook",
    "presence_kind": ENROLL_KIND,
    "presence_pub": b64u(PP["presence_new"]),
    "mac": b64u(R.enroll_mac(CODE_SECRET, HH_E, XP["dev_new_s"], PP["presence_new"], ENROLL_KIND)),
    "presence": p256_assert("presence_new", R.open_bytes(WS, CODE, HH_E, CHALLENGE)),
}


def transcript(name: str, h: dict[str, Any], kind: str, ident: str, stream: str, first: dict, reply: dict) -> dict:
    ini, resp = copy.deepcopy(h["ini"]), copy.deepcopy(h["resp"])
    c_first = ini.send.encrypt(b"", cj(first))
    assert resp.recv.decrypt(b"", c_first) == cj(first)
    c_reply = resp.send.encrypt(b"", cj(reply))
    k1, k2 = ini.send.k, ini.recv.k
    return {
        "name": name,
        "kind": kind,
        "workspace_id": WS,
        "hub_id": HUB,
        "id": ident,
        "stream_id": stream,
        "prologue_hex": h["pro"].hex(),
        "msg1_hex": h["m1"].hex(),
        "msg2_hex": h["m2"].hex(),
        "challenge_hex": CHALLENGE.hex(),
        "handshake_hash_hex": h["hh"].hex(),
        "k_i2r_hex": k1.hex() if k1 else None,
        "k_r2i_hex": k2.hex() if k2 else None,
        "first_plain": first,
        "first_hex": c_first.hex(),
        "reply_plain": reply,
        "reply_hex": c_reply.hex(),
    }


# --- cases ----------------------------------------------------------------------------------------------------------

CASES: list[dict[str, Any]] = []


def case(check: str, name: str, args: dict[str, Any], expect: dict[str, Any], note: str | None = None) -> None:
    c: dict[str, Any] = {"check": check, "name": name, "args": args, "expect": expect}
    if note:
        c["note"] = note
    CASES.append(c)


def err(code: str) -> dict[str, str]:
    return {"error": code}


def flip(b: bytes, i: int = -1) -> bytes:
    a = bytearray(b)
    a[i] ^= 1
    return bytes(a)


def prologue_cases() -> None:
    base = {"kind": "session", "workspace_id": WS, "id": DEV_A, "stream_id": STREAM, "hub_id": HUB}
    case("prologue", "prologue_session", base, {"hex": SESSION["pro"].hex()})
    case(
        "prologue",
        "prologue_enroll",
        {**base, "kind": "enroll", "id": CODE, "stream_id": STREAM_E},
        {"hex": ENROLL["pro"].hex()},
    )
    case("prologue", "prologue_session_with_code_id", {**base, "id": CODE}, err("malformed"))
    case("prologue", "prologue_enroll_with_device_id", {**base, "kind": "enroll"}, err("malformed"))
    case("prologue", "prologue_unknown_kind", {**base, "kind": "terminal"}, err("malformed"))
    case("prologue", "prologue_upper_hex_stream", {**base, "stream_id": STREAM.upper()}, err("malformed"))


def msg1_cases() -> None:
    def a(**over: Any) -> dict[str, Any]:
        p = {"kind": "session", "workspace_id": WS, "id": DEV_A, "stream_id": STREAM, "hub_id": HUB}
        p.update(over)
        return {"prologue": p, "wrk": "wrk", "msg1_hex": SESSION["m1"].hex()}

    case("msg1", "msg1_ok", a(), {"ok": {"rs_hex": XP["dev_a_s"].hex()}})
    note = "a recorded message 1 replayed with another prologue does not open (the prologue binds it)"
    case("msg1", "msg1_replayed_into_other_stream", a(stream_id=STREAM2), err("handshake"), note)
    case("msg1", "msg1_replayed_into_other_workspace", a(workspace_id=WS2), err("handshake"), note)
    case("msg1", "msg1_replayed_for_other_device", a(id=DEV_B), err("handshake"), note)
    case("msg1", "msg1_replayed_via_other_hub", a(hub_id=HUB2), err("handshake"), note)
    case("msg1", "msg1_session_replayed_as_enroll", a(kind="enroll", id="rc_" + DEV_A[3:]), err("handshake"), note)
    case("msg1", "msg1_other_wrk", {**a(), "wrk": "wrk_other"}, err("handshake"))
    case("msg1", "msg1_flipped_byte", {**a(), "msg1_hex": flip(SESSION["m1"], 40).hex()}, err("handshake"))
    early_m1 = _msg1_with_payload(b"x")
    case(
        "msg1",
        "msg1_with_early_data",
        {**a(), "msg1_hex": early_m1.hex()},
        err("malformed"),
        "message 1 carries no payload (no early data)",
    )
    case("msg1", "msg1_truncated", {**a(), "msg1_hex": SESSION["m1"][:95].hex()}, err("malformed"))


def _msg1_with_payload(payload: bytes) -> bytes:
    """Message 1 exactly as :func:`R.initiator_msg1` builds it, but with a payload (only for the refusal vector)."""
    hs = R.Handshake(
        sym=R.Symmetric.start(SESSION["pro"], XP["wrk"]), s=R.x25519_priv(X["dev_a_s"]), e=R.x25519_priv(X["dev_a_e"])
    )
    e_pub = XP["dev_a_e"]
    hs.sym.mix_hash(e_pub)
    hs.sym.mix_key(R._dh(hs.e, XP["wrk"]))
    enc_s = hs.sym.encrypt_and_hash(XP["dev_a_s"])
    hs.sym.mix_key(R._dh(hs.s, XP["wrk"]))
    return e_pub + enc_s + hs.sym.encrypt_and_hash(payload)


def msg2_cases() -> None:
    base = {"prologue_hex": SESSION["pro"].hex(), "wrk": "wrk", "msg1_hex": SESSION["m1"].hex(), "host_e": "host_e"}
    case(
        "msg2_build",
        "msg2_build_session",
        {**base, "challenge_hex": CHALLENGE.hex()},
        {"msg2_hex": SESSION["m2"].hex(), "handshake_hash_hex": HH.hex()},
    )
    case(
        "msg2_build",
        "msg2_build_enroll",
        {**base, "prologue_hex": ENROLL["pro"].hex(), "msg1_hex": ENROLL["m1"].hex(), "challenge_hex": CHALLENGE.hex()},
        {"msg2_hex": ENROLL["m2"].hex(), "handshake_hash_hex": HH_E.hex()},
    )
    case("msg2_build", "msg2_build_short_challenge", {**base, "challenge_hex": CHALLENGE[:31].hex()}, err("malformed"))
    dev = {"prologue_hex": SESSION["pro"].hex(), "s": "dev_a_s", "e": "dev_a_e", "wrk_pub_hex": XP["wrk"].hex()}
    case(
        "msg2_read",
        "msg2_read_ok",
        {**dev, "msg2_hex": SESSION["m2"].hex()},
        {"challenge_hex": CHALLENGE.hex(), "handshake_hash_hex": HH.hex()},
    )
    case("msg2_read", "msg2_read_flipped_byte", {**dev, "msg2_hex": flip(SESSION["m2"]).hex()}, err("handshake"))
    case(
        "msg2_read",
        "msg2_read_from_other_stream",
        {**dev, "msg2_hex": SESSION_B["m2"].hex()},
        err("handshake"),
        "a message 2 of another handshake does not open",
    )
    case("msg2_read", "msg2_read_short", {**dev, "msg2_hex": SESSION["m2"][:79].hex()}, err("malformed"))


def transport_cases(ts: list[dict[str, Any]]) -> None:
    for t in ts:
        args = {"transcript": t["name"], "dir": "i2r", "n": 0, "ct_hex": t["first_hex"]}
        case("transport", f"transport_{t['name']}_first", args, {"pt_hex": cj(t["first_plain"]).hex()})
        args_r = {"transcript": t["name"], "dir": "r2i", "n": 0, "ct_hex": t["reply_hex"]}
        case("transport", f"transport_{t['name']}_reply", args_r, {"pt_hex": cj(t["reply_plain"]).hex()})
    t = ts[0]
    flipped = flip(bytes.fromhex(t["first_hex"]), 3).hex()
    case(
        "transport",
        "transport_flipped_byte",
        {"transcript": t["name"], "dir": "i2r", "n": 0, "ct_hex": flipped},
        err("handshake"),
        "a changed byte fails the AEAD: the stream closes",
    )
    case(
        "transport",
        "transport_wrong_direction",
        {"transcript": t["name"], "dir": "r2i", "n": 0, "ct_hex": t["first_hex"]},
        err("handshake"),
        "a frame reflected back to its sender does not open",
    )
    case(
        "transport",
        "transport_replayed_frame",
        {"transcript": t["name"], "dir": "i2r", "n": 1, "ct_hex": t["first_hex"]},
        err("handshake"),
        "a frame replayed later in the stream (next nonce)",
    )


def open_cases() -> None:
    def a(**over: Any) -> dict[str, Any]:
        p = {
            "entry": ENTRY_A,
            "workspace_id": WS,
            "id": DEV_A,
            "handshake_hash_hex": HH.hex(),
            "challenge_hex": CHALLENGE.hex(),
            "assertion": OPEN_A,
            "held": 0,
        }
        p.update(over)
        return p

    case("open", "open_ok_platform", a(), {"count": 0})
    case(
        "open",
        "open_replayed_into_other_stream",
        a(handshake_hash_hex=HH_B.hex()),
        err("bad_signature"),
        "the open signature binds this handshake (stream)",
    )
    case("open", "open_replayed_into_other_workspace", a(workspace_id=WS2), err("bad_signature"))
    case(
        "open",
        "open_replayed_for_other_device",
        a(
            entry=ENTRY_B
            | {"presence_kind": "platform", "presence_pub": ENTRY_A["presence_pub"]}
            | {"device_id": DEV_B},
            id=DEV_B,
        ),
        err("bad_signature"),
        "the same key under another device id: the device id is in the signed bytes",
    )
    case("open", "open_other_challenge", a(challenge_hex=CHALLENGE2.hex()), err("bad_signature"))
    case(
        "open",
        "open_signed_by_other_key",
        a(assertion=p256_assert("attacker", R.open_bytes(WS, DEV_A, HH, CHALLENGE))),
        err("bad_signature"),
    )
    pres = p256_assert("presence_a", R.presence_bytes(WS, DEV_A, HH, CHALLENGE, [CHALLENGE]))
    case(
        "open",
        "open_with_presence_signature",
        a(assertion=pres),
        err("bad_signature"),
        "a remote-presence signature is never an open signature (domains)",
    )
    raw = {"form": "p256", "sig": b64u(sign("presence_a", R.open_bytes(WS, DEV_A, HH, CHALLENGE)[20:]))}
    case("open", "open_signed_without_label", a(assertion=raw), err("bad_signature"))
    case("open", "open_extra_key", a(assertion=OPEN_A | {"uv": True}), err("malformed"))
    case(
        "open",
        "open_unknown_form",
        a(assertion={"form": "keyfile", "sig": OPEN_A["sig"]}),
        err("malformed"),
        "a key file is not a presence form",
    )
    case("open", "open_short_challenge", a(challenge_hex=CHALLENGE[:16].hex()), err("malformed"))
    # FIDO2 (device B)
    msg_b = R.open_bytes(WS, DEV_B, HH_B, CHALLENGE)

    def b(**over: Any) -> dict[str, Any]:
        return a(entry=ENTRY_B, id=DEV_B, handshake_hash_hex=HH_B.hex(), **over)

    case("open", "open_ok_ctap2", b(assertion=ctap2_assert("presence_b", msg_b), held=6), {"count": 7})
    case(
        "open",
        "open_ok_ctap2_counterless",
        b(assertion=ctap2_assert("presence_b", msg_b, count=0)),
        {"count": 0},
        "an authenticator without a counter (0, held 0) is accepted",
    )
    case(
        "open",
        "open_ctap2_up_only",
        b(assertion=ctap2_assert("presence_b", msg_b, flags=0x01)),
        err("factor_too_weak"),
        "a touch without user verification is not enough",
    )
    case(
        "open",
        "open_ctap2_uv_without_up",
        b(assertion=ctap2_assert("presence_b", msg_b, flags=0x04)),
        err("factor_too_weak"),
    )
    case("open", "open_ctap2_counter_equal", b(assertion=ctap2_assert("presence_b", msg_b), held=7), err("replay"))
    case(
        "open",
        "open_ctap2_counter_decreased",
        b(assertion=ctap2_assert("presence_b", msg_b), held=9),
        err("replay"),
        "the sign counter is recorded and a decrease refused",
    )
    case(
        "open",
        "open_ctap2_counter_reset_to_zero",
        b(assertion=ctap2_assert("presence_b", msg_b, count=0), held=7),
        err("replay"),
    )
    case(
        "open",
        "open_ctap2_other_rp",
        b(assertion=ctap2_assert("presence_b", msg_b, rp=hashlib.sha256(b"example.com").digest())),
        err("bad_signature"),
    )
    case(
        "open",
        "open_ctap2_attested_data_flag",
        b(assertion=ctap2_assert("presence_b", msg_b, flags=0x45)),
        err("malformed"),
    )
    for nm, fl in (
        ("extension_data_flag", 0x85),
        ("backup_eligible", 0x0D),
        ("backup_state", 0x15),
    ):
        case(
            "open",
            f"open_ctap2_{nm}",
            b(assertion=ctap2_assert("presence_b", msg_b, flags=fl)),
            err("malformed" if nm.startswith("ext") else "factor_too_weak"),
            "ED refused; BE/BS: a presence key stays on one device (synced passkeys refused)",
        )
    case(
        "open",
        "open_ctap2_over_raw_bytes",
        b(
            assertion={
                "form": "ctap2",
                "auth_data": ctap2_assert("presence_b", msg_b)["auth_data"],
                "sig": b64u(sign("presence_b", msg_b)),
            }
        ),
        err("bad_signature"),
    )
    case(
        "open",
        "open_fido2_device_with_p256_form",
        b(assertion=p256_assert("presence_b", msg_b)),
        err("factor_mismatch"),
        "a FIDO2 device cannot fall back to a bare signature",
    )
    case(
        "open",
        "open_platform_device_with_ctap2_form",
        a(assertion=ctap2_assert("presence_a", R.open_bytes(WS, DEV_A, HH, CHALLENGE))),
        err("factor_mismatch"),
    )


def presence_cases() -> None:
    msg = R.presence_bytes(WS, DEV_A, HH, NONCE, DIGESTS)
    good = p256_assert("presence_a", msg)

    def a(**over: Any) -> dict[str, Any]:
        p = {
            "entry": ENTRY_A,
            "workspace_id": WS,
            "handshake_hash_hex": HH.hex(),
            "nonce_hex": NONCE.hex(),
            "digests_hex": [d.hex() for d in DIGESTS],
            "assertion": good,
            "held": 0,
            "issued": {NONCE.hex(): NOW - 60_000},
            "now_ms": NOW,
        }
        p.update(over)
        return p

    case(
        "presence_bytes",
        "presence_bytes_batch_of_3",
        {
            "workspace_id": WS,
            "device_id": DEV_A,
            "handshake_hash_hex": HH.hex(),
            "nonce_hex": NONCE.hex(),
            "digests_hex": [d.hex() for d in DIGESTS],
        },
        {"hex": msg.hex()},
    )
    case("presence", "presence_ok", a(), {"count": 0})
    case(
        "presence",
        "presence_replayed_into_other_stream",
        a(handshake_hash_hex=HH_B.hex()),
        err("bad_signature"),
        "presence binds the session's handshake hash",
    )
    case("presence", "presence_replayed_into_other_workspace", a(workspace_id=WS2), err("bad_signature"))
    case(
        "presence",
        "presence_replayed_for_other_device",
        a(entry=ENTRY_A | {"device_id": DEV_B}),
        err("bad_signature"),
        "same key, other device id",
    )
    case("presence", "presence_other_device_key", a(entry=ENTRY_C | {"expires_ms": NOW + DAY}), err("bad_signature"))
    case(
        "presence",
        "presence_nonce_not_issued",
        a(issued={}),
        err("replay"),
        "a used nonce is deleted; an unknown one is the same refusal",
    )
    case("presence", "presence_nonce_issued_for_other", a(issued={NONCE2.hex(): NOW - 1000}), err("replay"))
    case(
        "presence",
        "presence_batch_expired",
        a(issued={NONCE.hex(): NOW - R.BATCH_TTL_MS}),
        err("replay"),
        "a batch lives 5 min",
    )
    case("presence", "presence_batch_last_ms", a(issued={NONCE.hex(): NOW - R.BATCH_TTL_MS + 1}), {"count": 0})
    case("presence", "presence_nonce_from_future", a(issued={NONCE.hex(): NOW + 1}), err("replay"))
    case(
        "presence",
        "presence_digest_dropped",
        a(digests_hex=[d.hex() for d in DIGESTS[:2]]),
        err("bad_signature"),
        "a batch authorizes exactly its list",
    )
    case(
        "presence",
        "presence_digest_added",
        a(digests_hex=sorted([d.hex() for d in DIGESTS] + [seed("view-x").hex()])),
        err("bad_signature"),
    )
    case("presence", "presence_digests_unsorted", a(digests_hex=[d.hex() for d in reversed(DIGESTS)]), err("malformed"))
    case("presence", "presence_digests_duplicate", a(digests_hex=[DIGESTS[0].hex()] * 2), err("malformed"))
    case("presence", "presence_no_digest", a(digests_hex=[]), err("malformed"))
    many = sorted(seed(f"many-{i}").hex() for i in range(17))
    case("presence", "presence_17_digests", a(digests_hex=many, issued={NONCE.hex(): NOW}), err("malformed"))
    sixteen = sorted(seed(f"many-{i}") for i in range(16))
    ok16 = p256_assert("presence_a", R.presence_bytes(WS, DEV_A, HH, NONCE, sixteen))
    case("presence", "presence_16_digests", a(digests_hex=[d.hex() for d in sixteen], assertion=ok16), {"count": 0})
    as_open = p256_assert("presence_a", R.open_bytes(WS, DEV_A, HH, NONCE))
    case(
        "presence",
        "presence_with_open_signature",
        a(assertion=as_open, digests_hex=[DIGESTS[0].hex()]),
        err("bad_signature"),
        "an open signature is never a presence signature (domains)",
    )
    msg_b = R.presence_bytes(WS, DEV_B, HH_B, NONCE, DIGESTS)
    case(
        "presence",
        "presence_ok_ctap2",
        a(entry=ENTRY_B, handshake_hash_hex=HH_B.hex(), held=7, assertion=ctap2_assert("presence_b", msg_b, count=8)),
        {"count": 8},
    )
    case(
        "presence",
        "presence_ctap2_up_only",
        a(
            entry=ENTRY_B,
            handshake_hash_hex=HH_B.hex(),
            held=7,
            assertion=ctap2_assert("presence_b", msg_b, flags=0x01, count=8),
        ),
        err("factor_too_weak"),
    )
    for nm, fl in (("backup_eligible", 0x0D), ("backup_state", 0x15)):
        case(
            "presence",
            f"presence_ctap2_{nm}",
            a(
                entry=ENTRY_B,
                handshake_hash_hex=HH_B.hex(),
                held=7,
                assertion=ctap2_assert("presence_b", msg_b, flags=fl, count=8),
            ),
            err("factor_too_weak"),
        )


def enroll_cases() -> None:
    def a(**over: Any) -> dict[str, Any]:
        p = {
            "msg": ENROLL_MSG,
            "secret_hex": CODE_SECRET.hex(),
            "workspace_id": WS,
            "code_id": CODE,
            "handshake_hash_hex": HH_E.hex(),
            "challenge_hex": CHALLENGE.hex(),
            "transport_pub_hex": XP["dev_new_s"].hex(),
        }
        p.update(over)
        return p

    pending = {
        "name": ENROLL_MSG["name"],
        "presence_kind": ENROLL_KIND,
        "presence_pub": ENROLL_MSG["presence_pub"],
        "transport_pub": XP["dev_new_s"].hex(),
        "fingerprint": R.device_fp(XP["dev_new_s"], PP["presence_new"], ENROLL_KIND).hex(),
        "sign_count": 0,
    }
    case("enroll", "enroll_ok", a(), {"ok": pending})
    case(
        "enroll",
        "enroll_wrong_code",
        a(secret_hex=seed("other-code")[:16].hex()),
        err("bad_signature"),
        "a wrong try; five burn the code",
    )
    case(
        "enroll",
        "enroll_other_transport_key",
        a(transport_pub_hex=XP["dev_a_s"].hex()),
        err("bad_signature"),
        "the MAC binds the Noise static key of this handshake",
    )
    case("enroll", "enroll_replayed_into_other_stream", a(handshake_hash_hex=HH.hex()), err("bad_signature"))
    swapped = ENROLL_MSG | {"presence_pub": b64u(PP["attacker"])}
    case(
        "enroll",
        "enroll_presence_key_swapped",
        a(msg=swapped),
        err("bad_signature"),
        "the MAC binds the presence key: a relay cannot swap it",
    )
    att_mac = b64u(R.enroll_mac(CODE_SECRET, HH_E, XP["dev_new_s"], PP["attacker"], ENROLL_KIND))
    att = swapped | {"mac": att_mac}
    case(
        "enroll",
        "enroll_presence_by_other_key",
        a(msg=att),
        err("bad_signature"),
        "the presence signature must be by the named key",
    )
    kind_swapped = ENROLL_MSG | {"presence_kind": "fido2"}
    case("enroll", "enroll_kind_swapped", a(msg=kind_swapped), err("bad_signature"), "the MAC binds the kind")
    case(
        "enroll",
        "enroll_extra_field",
        a(msg=ENROLL_MSG | {"level_cap": "terminals"}),
        err("malformed"),
        "the device never sets its level or expiry; the owner does at the workspace",
    )
    case("enroll", "enroll_name_bidi", a(msg=ENROLL_MSG | {"name": "Mac\u202ekoob"}), err("malformed"))
    case("enroll", "enroll_name_too_long", a(msg=ENROLL_MSG | {"name": "x" * 65}), err("malformed"))
    case("enroll", "enroll_session_type", a(msg=ENROLL_MSG | {"t": "open"}), err("malformed"))
    case("enroll", "enroll_other_challenge", a(challenge_hex=CHALLENGE2.hex()), err("bad_signature"))
    case("enroll", "enroll_other_workspace", a(workspace_id=WS2), err("bad_signature"))
    case("enroll", "enroll_other_code_id", a(code_id="rc_" + hexid("code-2")), err("bad_signature"))


def derived_cases() -> None:
    case("wrk_fp", "wrk_fp", {"wrk_pub_hex": XP["wrk"].hex()}, {"hex": R.wrk_fp(XP["wrk"]).hex()})
    case(
        "code_bytes",
        "code_bytes",
        {"code_id": CODE, "secret_hex": CODE_SECRET.hex(), "wrk_pub_hex": XP["wrk"].hex()},
        {"hex": R.code_bytes(CODE, CODE_SECRET, XP["wrk"]).hex()},
    )
    case(
        "code_bytes",
        "code_bytes_short_secret",
        {"code_id": CODE, "secret_hex": CODE_SECRET[:15].hex(), "wrk_pub_hex": XP["wrk"].hex()},
        err("malformed"),
        "at least 128 random bits",
    )
    for kind in ("platform", "fido2"):
        case(
            "device_fp",
            f"device_fp_{kind}",
            {
                "transport_pub_hex": XP["dev_new_s"].hex(),
                "presence_pub": b64u(PP["presence_new"]),
                "presence_kind": kind,
            },
            {"hex": R.device_fp(XP["dev_new_s"], PP["presence_new"], kind).hex()},
        )
    case("session_static", "session_static_ok", {"entry": ENTRY_A, "rs_hex": XP["dev_a_s"].hex()}, {"ok": True})
    case(
        "session_static",
        "session_static_other_device_key",
        {"entry": ENTRY_A, "rs_hex": XP["dev_b_s"].hex()},
        err("handshake"),
        "the Noise static key must be the listed transport key of the named device",
    )
    case("open_msg", "open_msg_overlap", {"msg": OPEN_MSG, "host_api": [2, 5]}, {"api": 3})
    case("open_msg", "open_msg_exact", {"msg": OPEN_MSG | {"api": [2, 2]}, "host_api": [2, 5]}, {"api": 2})
    case(
        "open_msg",
        "open_msg_client_too_old",
        {"msg": OPEN_MSG | {"api": [1, 1]}, "host_api": [2, 5]},
        err("remote.version"),
    )
    case(
        "open_msg",
        "open_msg_client_too_new",
        {"msg": OPEN_MSG | {"api": [6, 9]}, "host_api": [2, 5]},
        err("remote.version"),
    )
    case(
        "open_msg", "open_msg_range_reversed", {"msg": OPEN_MSG | {"api": [3, 1]}, "host_api": [1, 5]}, err("malformed")
    )
    case(
        "open_msg",
        "open_msg_extra_key",
        {"msg": OPEN_MSG | {"level": "terminals"}, "host_api": [1, 5]},
        err("malformed"),
        "the level comes from the device list, never from the device",
    )
    case("open_msg", "open_msg_zero", {"msg": OPEN_MSG | {"api": [0, 1]}, "host_api": [1, 5]}, err("malformed"))


def device_list_cases() -> None:
    def a(signed: Any, **over: Any) -> dict[str, Any]:
        p = {"signed": signed, "wsk": "wsk", "workspace_id": WS, "held_version": 3}
        p.update(over)
        return p

    case("device_list", "device_list_ok", a(LIST), {"ok": True})
    case("device_list", "device_list_newer_than_held", a(LIST, held_version=2), {"ok": True})
    case(
        "device_list", "device_list_empty", a(signed_list(list_o([], version=4))), {"ok": True}, "every device removed"
    )

    def tamper(fn: Any) -> dict[str, Any]:
        o = copy.deepcopy(LIST_O)
        fn(o)
        return {"o": o, "sig": LIST["sig"]}

    def lvl(o: dict) -> None:
        o["devices"][0]["level_cap"] = "terminals"

    def exp(o: dict) -> None:
        o["devices"][0]["expires_ms"] += DAY

    def add(o: dict) -> None:
        o["devices"].append(entry("rd_" + "f" * 32, "Evil", "wrk_other", "attacker", "platform", "terminals"))

    def rm(o: dict) -> None:
        o["devices"].pop()

    def key(o: dict) -> None:
        o["devices"][0]["presence_pub"] = b64u(PP["attacker"])

    def ver(o: dict) -> None:
        o["version"] = 99

    for nm, fn in (
        ("level_cap", lvl),
        ("expiry", exp),
        ("added_device", add),
        ("removed_device", rm),
        ("presence_key", key),
        ("version", ver),
    ):
        case("device_list", f"device_list_tampered_{nm}", a(tamper(fn)), err("bad_signature"))
    case("device_list", "device_list_other_key", a(signed_list(LIST_O, "wsk_other")), err("bad_signature"))
    case("device_list", "device_list_signed_by_hub_op_key", a(signed_list(LIST_O, "hk_op")), err("bad_signature"))
    case(
        "device_list",
        "device_list_wrong_label",
        a(signed_list(LIST_O, label=L["sig_hub_link"])),
        err("bad_signature"),
        "a WSK signature under another WSK domain",
    )
    case(
        "device_list",
        "device_list_other_workspace",
        a(signed_list(list_o([ENTRY_A], workspace_id=WS2))),
        err("other_workspace"),
    )
    case(
        "device_list",
        "device_list_rollback",
        a(LIST, held_version=4),
        err("stale_version"),
        "an older list (a removed device back) is refused",
    )
    resigned = [
        ("extra_field", list_o([ENTRY_A], note="x")),
        ("entry_extra_field", list_o([ENTRY_A | {"sign_count": 3}])),
        ("entry_missing_field", list_o([{k: v for k, v in ENTRY_A.items() if k != "confirmed_by"}])),
        ("unsorted", {**list_o([]), "devices": [ENTRY_B, ENTRY_A]}),
        ("duplicate_id", {**list_o([]), "devices": [ENTRY_A, ENTRY_A]}),
        ("shared_presence_key", list_o([ENTRY_A, ENTRY_B | {"presence_pub": ENTRY_A["presence_pub"]}])),
        ("shared_transport_key", list_o([ENTRY_A, ENTRY_B | {"transport_pub": ENTRY_A["transport_pub"]}])),
        ("expires_before_added", list_o([ENTRY_A | {"expires_ms": ENTRY_A["added_ms"]}])),
        ("life_over_366_days", list_o([ENTRY_A | {"expires_ms": ENTRY_A["added_ms"] + 366 * DAY + 1}])),
        ("bad_level", list_o([ENTRY_A | {"level_cap": "admin"}])),
        ("bad_kind", list_o([ENTRY_A | {"presence_kind": "keyfile"}])),
        ("device_id_format", list_o([ENTRY_A | {"device_id": "d_" + DEV_A[3:]}])),
        ("person_format", list_o([ENTRY_A | {"person": "u_" + PERSON[2:]}])),
        ("name_control", list_o([ENTRY_A | {"name": "Mac\u0007"}])),
        ("presence_not_on_curve", list_o([ENTRY_A | {"presence_pub": b64u(b"\x04" + bytes(64))}])),
        ("transport_short", list_o([ENTRY_A | {"transport_pub": b64u(XP["wrk"][:31])}])),
        ("version_zero", list_o([ENTRY_A], version=0)),
        ("wrong_kind", list_o([ENTRY_A], kind="hub.link")),
    ]
    for nm, o in resigned:
        case("device_list", f"device_list_{nm}", a(signed_list(o)), err("malformed"))
    case("device_list", "device_list_wrong_suite", a(signed_list(list_o([ENTRY_A], suite=3))), err("wrong_suite"))
    case("device_list", "device_list_hv_1", a(signed_list(list_o([ENTRY_A], hv=1))), err("malformed"))
    many = [
        entry(
            "rd_" + f"{i:032x}",
            f"d{i}",
            "dev_a_s",
            "presence_a",
            "platform",
            "view",
            transport_pub=b64u(R.x25519_pub(seed(f"many/x/{i}"))),
            presence_pub=b64u(S.public_bytes(S.key_from_seed(seed(f"many/p/{i}")))),
        )
        for i in range(R.DEVICES_MAX + 1)
    ]
    case(
        "device_list",
        "device_list_65_devices",
        a(signed_list(list_o(many))),
        err("malformed"),
        "correctly signed, sorted, distinct keys: only the 64-entry cap refuses it",
    )
    # lookups
    for nm, dev, now, want in (
        ("ok", DEV_A, NOW, {"ok": DEV_A}),
        ("ok_last_ms", DEV_A, ENTRY_A["expires_ms"] - 1, {"ok": DEV_A}),
        ("expired_at_expiry", DEV_A, ENTRY_A["expires_ms"], err("remote.device")),
        ("expired_entry", DEV_C, NOW, err("remote.device")),
        ("unknown", "rd_" + hexid("nobody"), NOW, err("remote.device")),
    ):
        case("device_lookup", f"device_lookup_{nm}", {"list": LIST_O, "device_id": dev, "now_ms": now}, want)


def link(
    op: str, body: dict, direction: str, *, key: str | None = None, label: bytes | None = None, **over: Any
) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": "hub.link",
        "hv": 0,
        "hub_id": HUB,
        "workspace_id": WS,
        "dir": direction,
        "seq": 41,
        "ts_ms": NOW,
        "op": op,
        "re": None,
        "body": body,
    }
    o.update(over)
    k = key or ("hk_op" if direction == "h2w" else "wsk")
    lab = label if label is not None else (L["sig_hub_route"] if direction == "h2w" else L["sig_hub_link"])
    return {"o": o, "sig": b64u(sign(k, lab + cj(o)))}


def stream_link_cases() -> list[dict[str, Any]]:
    out = []

    def c(name: str, signed: dict, direction: str, expect: dict, note: str | None = None, **over: Any) -> None:
        args = {"signed": signed, "direction": direction, "hub_id": HUB, "workspace_id": WS, "now_ms": NOW}
        args["key"] = "hk_op" if direction == "h2w" else "wsk"
        args.update(over)
        case("stream_link", name, args, expect, note)
        out.append(signed)

    so = {"stream_id": STREAM, "device_id": DEV_A, "kind": "session"}
    data = {"stream_id": STREAM, "seq": 1, "bytes": b64u(SESSION["m1"])}
    close = {"stream_id": STREAM, "reason": "remote.off"}
    c("stream_open_session_ok", link("stream_open", so, "h2w"), "h2w", {"ok": "stream_open"})
    c(
        "stream_open_enroll_ok",
        link("stream_open", {"stream_id": STREAM_E, "device_id": CODE, "kind": "enroll"}, "h2w"),
        "h2w",
        {"ok": "stream_open"},
    )
    c("stream_data_h2w_ok", link("stream_data", data, "h2w"), "h2w", {"ok": "stream_data"})
    c(
        "stream_data_w2h_ok",
        link("stream_data", data | {"bytes": b64u(SESSION["m2"])}, "w2h"),
        "w2h",
        {"ok": "stream_data"},
    )
    c("stream_close_h2w_ok", link("stream_close", close | {"reason": "closed"}, "h2w"), "h2w", {"ok": "stream_close"})
    c("stream_close_w2h_ok", link("stream_close", close, "w2h"), "w2h", {"ok": "stream_close"})
    c(
        "stream_open_w2h",
        link("stream_open", so, "w2h"),
        "w2h",
        err("unknown_op"),
        "only the hub opens a stream (it forwards the device's request)",
    )
    c(
        "stream_data_as_reply",
        link("stream_data", data, "h2w", re=40),
        "h2w",
        err("unknown_op"),
        "stream frames are one-way: never a reply",
    )
    c("stream_open_session_with_code_id", link("stream_open", so | {"device_id": CODE}, "h2w"), "h2w", err("malformed"))
    c("stream_open_enroll_with_device_id", link("stream_open", so | {"kind": "enroll"}, "h2w"), "h2w", err("malformed"))
    c("stream_open_kind_terminal", link("stream_open", so | {"kind": "terminal"}, "h2w"), "h2w", err("malformed"))
    c(
        "stream_open_extra_field",
        link("stream_open", so | {"level": "terminals"}, "h2w"),
        "h2w",
        err("malformed"),
        "the hub names nothing but the stream, the device id and the kind",
    )
    c(
        "stream_open_hub_user_id",
        link("stream_open", so | {"device_id": "u_" + DEV_A[3:]}, "h2w"),
        "h2w",
        err("malformed"),
        "a hub user is never a device",
    )
    c("stream_data_empty", link("stream_data", data | {"bytes": ""}, "h2w"), "h2w", err("malformed"))
    c("stream_data_seq_zero", link("stream_data", data | {"seq": 0}, "h2w"), "h2w", err("malformed"))
    c(
        "stream_data_padded_b64u",
        link("stream_data", data | {"bytes": data["bytes"] + "="}, "h2w"),
        "h2w",
        err("malformed"),
    )
    c(
        "stream_data_max_frame",
        link("stream_data", data | {"bytes": b64u(bytes(R.FRAME_MAX))}, "h2w"),
        "h2w",
        {"ok": "stream_data"},
    )
    c(
        "stream_data_over_64k",
        link("stream_data", data | {"bytes": b64u(bytes(R.FRAME_MAX + 1))}, "h2w"),
        "h2w",
        err("malformed"),
        "frames are at most 65535 bytes (one Noise message)",
    )
    c(
        "stream_close_unknown_reason",
        link("stream_close", close | {"reason": "because"}, "w2h"),
        "w2h",
        err("malformed"),
    )
    c("stream_open_stale", link("stream_open", so, "h2w", ts_ms=NOW - 120_001), "h2w", err("stale_timestamp"))
    c("stream_open_ts_edge", link("stream_open", so, "h2w", ts_ms=NOW - 120_000), "h2w", {"ok": "stream_open"})
    c("stream_open_other_workspace", link("stream_open", so, "h2w", workspace_id=WS2), "h2w", err("other_workspace"))
    c("stream_open_other_hub", link("stream_open", so, "h2w", hub_id=HUB2), "h2w", err("other_hub"))
    c("stream_open_signed_by_wsk", link("stream_open", so, "h2w", key="wsk"), "h2w", err("bad_signature"))
    c("stream_open_w2h_label", link("stream_open", so, "h2w", label=L["sig_hub_link"]), "h2w", err("bad_signature"))
    c(
        "stream_data_w2h_signed_by_hub",
        link("stream_data", data, "w2h", key="hk_op"),
        "w2h",
        err("bad_signature"),
        "the hub cannot speak for the workspace inside a stream",
    )
    c(
        "stream_open_wrong_dir_field",
        link("stream_open", so, "h2w", dir="w2h", key="hk_op", label=L["sig_hub_route"]),
        "h2w",
        err("malformed"),
    )
    return out


def cost_cases(frames: list[dict[str, Any]]) -> None:
    small = frames[0]  # stream_open_session_ok
    big = link("stream_data", {"stream_id": STREAM, "seq": 2, "bytes": b64u(bytes(3000))}, "h2w")
    case(
        "frame_cost",
        "frame_cost_small_frame",
        {"signed": small},
        {"cost": R.FRAME_MIN_COST},
        "every frame costs at least 1 KiB (signature verification)",
    )
    case(
        "frame_cost",
        "frame_cost_3000_byte_frame",
        {"signed": big},
        {"cost": len(cj(big))},
        "the whole signed frame, signature and base64 included",
    )


def gate_cases() -> None:
    on = {"on": True, "expires_ms": NOW + DAY}

    def st(**over: Any) -> dict[str, Any]:
        s = {
            "remote": on,
            "devices": LIST_O,
            "codes": {},
            "streams_device": 0,
            "streams_workspace": 0,
            "handshakes_device": 0,
            "enrolls": 0,
            "seen_streams": [],
        }
        s.update(over)
        return s

    sess = {"stream_id": STREAM, "device_id": DEV_A, "kind": "session"}
    enr = {"stream_id": STREAM_E, "device_id": CODE, "kind": "enroll"}
    off = {"on": False, "expires_ms": None}
    for nm, body, s, want, note in (
        ("session_ok", sess, st(), {"ok": True}, None),
        ("session_until_off", sess, st(remote={"on": True, "expires_ms": None}), {"ok": True}, None),
        ("session_while_off", sess, st(remote=off), err("remote.off"), "D85: nothing reaches the dashboard"),
        (
            "session_off_unknown_device",
            sess | {"device_id": "rd_" + hexid("nobody")},
            st(remote=off),
            err("remote.off"),
            "off is answered first: an opted-out workspace says nothing about its devices",
        ),
        ("session_policy_expired", sess, st(remote={"on": True, "expires_ms": NOW}), err("remote.off"), None),
        ("session_unknown_device", sess | {"device_id": "rd_" + hexid("nobody")}, st(), err("remote.device"), None),
        ("session_expired_device", sess | {"device_id": DEV_C}, st(), err("remote.device"), None),
        ("session_device_streams_full", sess, st(streams_device=4), err("remote.busy"), "4 streams per device"),
        ("session_device_third_stream", sess, st(streams_device=3), {"ok": True}, None),
        ("session_workspace_streams_full", sess, st(streams_workspace=16), err("remote.busy"), "16 per workspace"),
        ("session_handshake_rate", sess, st(handshakes_device=10), err("remote.busy"), "10 per minute per device"),
        ("session_reused_stream_id", sess, st(seen_streams=[STREAM]), err("replay"), "a stream id is used once"),
        (
            "enroll_while_off",
            enr,
            st(remote=off, codes={CODE: NOW + 60_000}),
            {"ok": True},
            "D85: enrollment is the one stream kind allowed while off",
        ),
        ("enroll_no_code", enr, st(remote=off), err("remote.no_code"), "no state, no prompt"),
        ("enroll_code_expired", enr, st(codes={CODE: NOW}), err("remote.no_code"), "15 min"),
        ("enroll_other_code", enr, st(codes={"rc_" + hexid("code-2"): NOW + 60_000}), err("remote.no_code"), None),
        (
            "enroll_rate",
            enr,
            st(codes={CODE: NOW + 60_000}, enrolls=3),
            err("remote.busy"),
            "3 enroll streams per minute per workspace",
        ),
    ):
        case("gate", f"gate_{nm}", {"body": body, "state": s, "now_ms": NOW}, want, note)


def custodial_cases() -> None:
    """§8.5 "No custodial type can": every item has refusal vectors, checked as a channel action (hub ``check_action``)
    and as a delivered event (the host's role table, then its exact-payload check)."""
    base_grant = {
        "type": "grant.requested",
        "log": "workspace",
        "scope": "workable",
        "verbs": "agent",
        "hours": 8,
        "handoff": ULID_E,
    }
    gates = {"code": {"approvers": ["owner"], "count": 1, "not": ["assignees"], "applies": "all", "independent": True}}
    items = [
        (
            "grant",
            "grant_issued",
            {"type": "grant.issued", "log": "workspace", "scope": "all", "verbs": "agent", "hours": 8},
            "not_custodial",
        ),
        ("grant", "grant_requested_with_secret_hash", base_grant | {"secret_hash": HASH}, "malformed"),
        ("grant", "grant_requested_with_grant_id", base_grant | {"grant": "gr_" + ULID_T}, "malformed"),
        ("grant", "grant_requested_marked_remote", base_grant | {"remote": True}, "malformed"),
        ("grant", "grant_requested_naming_stream", base_grant | {"stream_id": STREAM}, "malformed"),
        ("grant", "grant_requested_naming_device", base_grant | {"device_id": DEV_A}, "malformed"),
        ("run", "claim_taken", {"type": "claim.taken", "log": ULID_T, "session": "s1"}, "not_custodial"),
        ("run", "task_started", {"type": "task.started", "log": ULID_T, "task": "T1"}, "not_custodial"),
        ("run", "run_started", {"type": "run.started", "log": "workspace"}, "not_custodial"),
        ("run", "agent_started", {"type": "agent.started", "log": ULID_T}, "not_custodial"),
        ("terminal", "terminal_opened", {"type": "terminal.opened", "log": "workspace"}, "not_custodial"),
        ("terminal", "terminal_input", {"type": "terminal.input", "log": "workspace", "text": "ls"}, "not_custodial"),
        ("remote", "remote_enabled", {"type": "remote.enabled", "log": "workspace", "level": "work"}, "not_custodial"),
        (
            "remote",
            "remote_changed",
            {"type": "remote.changed", "log": "workspace", "level": "terminals"},
            "not_custodial",
        ),
        ("remote", "remote_disabled", {"type": "remote.disabled", "log": "workspace"}, "not_custodial"),
        ("remote", "remote_device_added", {"type": "remote.device_added", "log": "workspace"}, "not_custodial"),
        ("remote", "remote_device_removed", {"type": "remote.device_removed", "log": "workspace"}, "not_custodial"),
        ("remote", "remote_wrk_rotated", {"type": "remote.wrk_rotated", "log": "workspace"}, "not_custodial"),
        ("remote", "device_added", {"type": "device.added", "log": "workspace"}, "not_custodial"),
        (
            "remote",
            "device_excluded_remote_device",
            {"type": "device.excluded", "log": "workspace", "device": DEV_A, "reason": "lost"},
            "malformed",
        ),
        (
            "remote",
            "settings_changed_remote_level",
            {"type": "settings.changed", "log": "workspace", "set": {"remote_level": "terminals"}},
            "malformed",
        ),
        (
            "remote",
            "settings_changed_remote_on",
            {"type": "settings.changed", "log": "workspace", "set": {"remote": True}},
            "malformed",
        ),
        (
            "remote",
            "settings_changed_remote_days",
            {"type": "settings.changed", "log": "workspace", "set": {"grant_hours": 8, "remote_days": 7}},
            "malformed",
        ),
        (
            "remote",
            "policy_changed_remote_gate",
            {"type": "policy.changed", "log": "workspace", "gates": gates | {"remote": gates["code"]}},
            "malformed",
        ),
    ]
    for item, nm, action, code in items:
        log = action["log"]
        event = {k: v for k, v in action.items() if k != "log"}
        event.update({"v": 2, "id": ULID_E, "based_on": HASH, "hash_v": 1, "roster_v": 0})
        args = {"item": item, "action": action, "event": event, "log": log}
        case("custodial_cannot", f"custodial_cannot_{nm}", args, err(code))


# --- inside a session (R3, #1591): app frames, exchanges, route_digest ------------------------------------------------

W_TPL = "/api/w/{ws}"


def fr(direction: str, kind: str | int, ident: int, payload: bytes = b"") -> dict[str, Any]:
    """A frame spec: the plaintext as hex."""
    k = R.APP_KINDS[kind] if isinstance(kind, str) else kind
    return {"dir": direction, "hex": (bytes([k]) + ident.to_bytes(4, "big") + payload).hex()}


def fill(direction: str, kind: str | int, ident: int, n: int, byte: int = 0x61) -> dict[str, Any]:
    """A frame spec too large to write out: ``kind || id || bytes([byte]) * n`` (both sides build it)."""
    k = R.APP_KINDS[kind] if isinstance(kind, str) else kind
    return {"dir": direction, "fill": {"kind": k, "id": ident, "len": n, "byte": byte}}


def frame_bytes(spec: dict[str, Any]) -> bytes:
    if "hex" in spec:
        return bytes.fromhex(spec["hex"])
    f = spec["fill"]
    return bytes([f["kind"]]) + f["id"].to_bytes(4, "big") + bytes([f["byte"]]) * f["len"]


def req_head(method: str = "GET", path: str = "/api/w/" + "0" * 32 + "/runs", query: str = "", headers=None) -> dict:
    return {"method": method, "path": path, "query": query, "headers": headers or [["accept", "application/json"]]}


def ws_head(path: str = "/term", query: str = "ticket=t1", headers=None) -> dict:
    return {"path": path, "query": query, "headers": headers or [["sec-websocket-protocol", "orch.term"]]}


def resp_head(status: int = 200, headers=None) -> dict:
    return {"status": status, "headers": [["content-type", "application/json"]] if headers is None else headers}


def describe(f: R.AppFrame) -> dict[str, Any]:
    """How a parsed frame is written in the vectors: byte payloads by their length."""
    v = len(f.value) if f.kind in ("BODY", "WS_BINARY") else f.value
    return {"kind": f.kind, "id": f.id, "value": v}


def app_frame_cases() -> None:
    def ok(name: str, spec: dict[str, Any], note: str | None = None) -> None:
        case("app_frame", name, spec, {"ok": describe(R.app_frame_parse(spec["dir"], frame_bytes(spec)))}, note)

    def bad(name: str, spec: dict[str, Any], note: str | None = None) -> None:
        case("app_frame", name, spec, err("malformed"), note)

    ws_path = "/api/w/" + WS
    ok("app_frame_request_head_get", fr("d2h", "HEAD", 1, cj(req_head(path=ws_path + "/runs"))))
    ok(
        "app_frame_request_head_post",
        fr(
            "d2h",
            "HEAD",
            7,
            cj(req_head("POST", ws_path + "/agents/start", "", [["content-type", "application/json"]])),
        ),
    )
    ok("app_frame_ws_open", fr("d2h", "WS_OPEN", 2, cj(ws_head())))
    ok("app_frame_response_head_200", fr("h2d", "HEAD", 1, cj(resp_head())))
    ok("app_frame_response_head_101", fr("h2d", "HEAD", 2, cj(resp_head(101, []))))
    ok("app_frame_body", fr("d2h", "BODY", 7, b'{"ticket":"DEMO-1"}'))
    ok("app_frame_body_max", fill("h2d", "BODY", 1, R.APP_PAYLOAD_MAX), "65514 bytes: the plaintext is 65519")
    ok("app_frame_end", fr("d2h", "END", 1))
    ok("app_frame_reset", fr("h2d", "RESET", 3, b"remote.busy"))
    ok("app_frame_ws_text", fr("d2h", "WS_TEXT", 2, "ls -la é\n".encode()))
    ok("app_frame_ws_text_empty", fr("h2d", "WS_TEXT", 2))
    ok("app_frame_ws_binary", fr("h2d", "WS_BINARY", 2, b"\x00\xff\x1b[0m"))
    ok("app_frame_ws_close", fr("d2h", "WS_CLOSE", 2, (1000).to_bytes(2, "big")))
    ok("app_frame_ping", fr("d2h", "PING", 0))
    ok("app_frame_pong", fr("h2d", "PONG", 0))

    bad("app_frame_too_short", {"dir": "d2h", "hex": "0300000001"[:8]})
    bad(
        "app_frame_over_max",
        fill("h2d", "BODY", 1, R.APP_PAYLOAD_MAX + 1),
        "65520 bytes cannot be one transport plaintext",
    )
    bad("app_frame_kind_0", fr("d2h", 0, 1))
    bad("app_frame_kind_11", fr("d2h", 11, 1))
    bad("app_frame_kind_255", fr("h2d", 255, 1))
    bad("app_frame_head_id_0", fr("d2h", "HEAD", 0, cj(req_head())))
    bad("app_frame_ping_id_1", fr("d2h", "PING", 1))
    bad("app_frame_ping_payload", fr("d2h", "PING", 0, b"x"))
    bad("app_frame_end_payload", fr("d2h", "END", 1, b"x"))
    bad("app_frame_body_empty", fr("d2h", "BODY", 1))
    bad("app_frame_reset_unknown_code", fr("h2d", "RESET", 1, b"revoked"))
    bad("app_frame_reset_empty", fr("h2d", "RESET", 1))
    bad("app_frame_ws_open_from_host", fr("h2d", "WS_OPEN", 1, cj(ws_head())))
    bad("app_frame_ws_text_bad_utf8", fr("d2h", "WS_TEXT", 2, b"\xc3\x28"))
    bad("app_frame_ws_close_1005", fr("d2h", "WS_CLOSE", 2, (1005).to_bytes(2, "big")), "never on the wire")
    bad("app_frame_ws_close_3_bytes", fr("d2h", "WS_CLOSE", 2, b"\x03\xe8x"))
    h = req_head()
    bad("app_frame_head_not_canonical", fr("d2h", "HEAD", 1, json.dumps(h).encode()), "a head is its own cj form")
    bad(
        "app_frame_head_extra_key",
        fr("d2h", "HEAD", 1, cj(h | {"person": PERSON})),
        "identity never comes from a frame",
    )
    bad("app_frame_head_missing_query", fr("d2h", "HEAD", 1, cj({k: v for k, v in h.items() if k != "query"})))
    bad("app_frame_head_response_shape_from_device", fr("d2h", "HEAD", 1, cj(resp_head())))
    bad("app_frame_head_request_shape_from_host", fr("h2d", "HEAD", 1, cj(h)))
    bad("app_frame_head_method_lower", fr("d2h", "HEAD", 1, cj(h | {"method": "get"})))
    bad("app_frame_head_method_connect", fr("d2h", "HEAD", 1, cj(h | {"method": "CONNECT"})))
    for nm, path in (
        ("dotdot", "/api/w/../setup"),
        ("dot", "/api/./w"),
        ("double_slash", "/api//w"),
        ("trailing_slash", "/api/w/"),
        ("percent", "/api/w/%2e%2e/setup"),
        ("backslash", "/api\\w"),
        ("semicolon", "/api/w;x"),
        ("relative", "api/w"),
        ("space", "/api/w x"),
        ("non_ascii", "/api/w/é"),
    ):
        bad(f"app_frame_head_path_{nm}", fr("d2h", "HEAD", 1, cj(h | {"path": path})), "the dashboard's clean_path")
    bad("app_frame_head_path_too_long", fr("d2h", "HEAD", 1, cj(h | {"path": "/" + "a" * 2048})))
    bad("app_frame_head_query_hash", fr("d2h", "HEAD", 1, cj(h | {"query": "a=1#frag"})))
    bad("app_frame_head_query_space", fr("d2h", "HEAD", 1, cj(h | {"query": "a=1 2"})))
    bad("app_frame_head_header_upper_name", fr("d2h", "HEAD", 1, cj(h | {"headers": [["Accept", "x"]]})))
    bad(
        "app_frame_head_header_newline", fr("d2h", "HEAD", 1, cj(h | {"headers": [["accept", "a\r\nx-orch-token: t"]]}))
    )
    bad("app_frame_head_header_leading_space", fr("d2h", "HEAD", 1, cj(h | {"headers": [["accept", " a"]]})))
    bad("app_frame_head_header_triple", fr("d2h", "HEAD", 1, cj(h | {"headers": [["accept", "a", "b"]]})))
    bad("app_frame_head_65_headers", fr("d2h", "HEAD", 1, cj(h | {"headers": [["x-a", str(i)] for i in range(65)]})))
    big = [["x-a", "v" * 4000] for _ in range(5)]
    bad("app_frame_head_over_16k", fr("d2h", "HEAD", 1, cj(h | {"headers": big})))
    for name in ("content-length", "transfer-encoding", "connection", "upgrade", "host", "origin"):
        bad(
            f"app_frame_head_{name.replace('-', '_')}",
            fr("d2h", "HEAD", 1, cj(h | {"headers": [[name, "1"]]})),
            "framing is the frames'; Host and Origin are the terminator's",
        )
    bad(
        "app_frame_ws_open_sec_websocket_key",
        fr("d2h", "WS_OPEN", 2, cj(ws_head(headers=[["sec-websocket-key", "x"]]))),
    )
    bad("app_frame_ws_open_with_method", fr("d2h", "WS_OPEN", 2, cj(ws_head() | {"method": "GET"})))
    bad(
        "app_frame_response_set_cookie",
        fr("h2d", "HEAD", 1, cj(resp_head(200, [["set-cookie", "orch_session=x"]]))),
        "a stream credential never leaves the dashboard process",
    )
    bad("app_frame_response_status_100", fr("h2d", "HEAD", 1, cj(resp_head(100))))
    bad("app_frame_response_status_600", fr("h2d", "HEAD", 1, cj(resp_head(600))))
    bad("app_frame_response_status_text", fr("h2d", "HEAD", 1, cj({"status": "200", "headers": []})))
    bad("app_frame_response_content_length", fr("h2d", "HEAD", 1, cj(resp_head(200, [["content-length", "2"]]))))


def request_head_cases() -> None:
    def a(spec: dict[str, Any]) -> dict[str, Any]:
        return spec

    h = req_head()
    case("request_head", "request_head_plain", a(fr("d2h", "HEAD", 1, cj(h))), {"ok": True})
    for nm, name in (
        ("authorization", "authorization"),
        ("cookie", "cookie"),
        ("setup_token", "x-setup-token"),
        ("orch_token", "x-orch-token"),
        ("orch_anything", "x-orch-person"),
    ):
        case(
            "request_head",
            f"request_head_bearer_{nm}",
            a(fr("d2h", "HEAD", 1, cj(h | {"headers": [["accept", "x"], [name, "Bearer t"]]}))),
            err("remote.bearer"),
            "an outside bearer is refused (400), never stripped",
        )
    case(
        "request_head",
        "request_head_ws_open_cookie",
        a(fr("d2h", "WS_OPEN", 2, cj(ws_head(headers=[["cookie", "orch_session=x"]])))),
        err("remote.bearer"),
    )
    case(
        "request_head",
        "request_head_upper_authorization",
        a(fr("d2h", "HEAD", 1, cj(h | {"headers": [["Authorization", "Bearer t"]]}))),
        err("malformed"),
        "no case folding: an upper-case name is a frame error before any bearer rule",
    )


SEQ_HEAD = cj(req_head())
SEQ_POST = cj(req_head("POST", "/api/w/" + "0" * 32 + "/wiki/create"))
SEQ_WS = cj(ws_head())
SEQ_200 = cj(resp_head())
SEQ_101 = cj(resp_head(101, []))
SEQ_403 = cj(resp_head(403))


def seq_case(name: str, frames: list[dict[str, Any]], note: str | None = None) -> None:
    st = R.AppStream()
    results: list[str] = []
    expect: dict[str, Any]
    for i, f in enumerate(frames):
        try:
            results.append(st.feed(f["dir"], frame_bytes(f)))
        except R.Refused as e:
            expect = {"error": e.code, "index": i}
            break
    else:
        expect = {"results": results}
    case("app_seq", name, {"frames": frames}, expect, note)


def get_exchange(i: int) -> list[dict[str, Any]]:
    return [
        fr("d2h", "HEAD", i, SEQ_HEAD),
        fr("d2h", "END", i),
        fr("h2d", "HEAD", i, SEQ_200),
        fr("h2d", "BODY", i, b"[]"),
        fr("h2d", "END", i),
    ]


def app_seq_cases() -> None:
    seq_case("app_seq_get", get_exchange(1))
    seq_case(
        "app_seq_post_body",
        [
            fr("d2h", "HEAD", 1, SEQ_POST),
            fr("d2h", "BODY", 1, b'{"title":'),
            fr("d2h", "BODY", 1, b'"x"}'),
            fr("d2h", "END", 1),
            fr("h2d", "HEAD", 1, SEQ_200),
            fr("h2d", "END", 1),
        ],
    )
    seq_case(
        "app_seq_response_before_request_end",
        [
            fr("d2h", "HEAD", 1, SEQ_POST),
            fr("h2d", "HEAD", 1, cj(resp_head(413))),
            fr("h2d", "END", 1),
            fr("d2h", "END", 1),
            fr("d2h", "HEAD", 2, SEQ_HEAD),
        ],
    )
    seq_case(
        "app_seq_interleaved",
        [
            fr("d2h", "HEAD", 1, SEQ_HEAD),
            fr("d2h", "HEAD", 2, SEQ_HEAD),
            fr("d2h", "END", 2),
            fr("h2d", "HEAD", 2, SEQ_200),
            fr("d2h", "END", 1),
            fr("h2d", "HEAD", 1, SEQ_200),
            fr("h2d", "END", 2),
            fr("h2d", "END", 1),
        ],
    )
    seq_case(
        "app_seq_websocket",
        [
            fr("d2h", "WS_OPEN", 1, SEQ_WS),
            fr("h2d", "HEAD", 1, SEQ_101),
            fr("d2h", "WS_TEXT", 1, b"ls\n"),
            fr("h2d", "WS_BINARY", 1, b"a b\r\n"),
            fr("d2h", "WS_CLOSE", 1, (1000).to_bytes(2, "big")),
            fr("h2d", "WS_BINARY", 1, b"bye"),
            fr("h2d", "WS_CLOSE", 1, (1000).to_bytes(2, "big")),
            fr("d2h", "WS_TEXT", 1, b"late"),
        ],
        "after both WS_CLOSE the exchange ended: a late frame is dropped",
    )
    seq_case(
        "app_seq_websocket_refused",
        [
            fr("d2h", "WS_OPEN", 1, SEQ_WS),
            fr("h2d", "HEAD", 1, SEQ_403),
            fr("h2d", "BODY", 1, b'{"error":"remote.refused"}'),
            fr("h2d", "END", 1),
        ],
    )
    seq_case("app_seq_id_reused_after_end", [*get_exchange(1), fr("d2h", "HEAD", 1, SEQ_HEAD)], "ids rise strictly")
    seq_case("app_seq_id_lower", [fr("d2h", "HEAD", 2, SEQ_HEAD), fr("d2h", "HEAD", 1, SEQ_HEAD)])
    seq_case("app_seq_id_reused_while_open", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "HEAD", 1, SEQ_HEAD)])
    seq_case("app_seq_ws_open_reuses_http_id", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "WS_OPEN", 1, SEQ_WS)])
    seq_case("app_seq_body_before_head", [fr("d2h", "BODY", 1, b"x")])
    seq_case("app_seq_response_unopened", [fr("h2d", "HEAD", 1, SEQ_200)], "the host never opens an exchange")
    seq_case(
        "app_seq_response_other_id",
        [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("h2d", "HEAD", 2, SEQ_200)],
        "a response for an id the device has not opened",
    )
    seq_case(
        "app_seq_body_after_end", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "END", 1), fr("d2h", "BODY", 1, b"x")]
    )
    seq_case("app_seq_end_twice", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "END", 1), fr("d2h", "END", 1)])
    seq_case("app_seq_response_body_before_head", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("h2d", "BODY", 1, b"x")])
    seq_case(
        "app_seq_second_response_head",
        [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("h2d", "HEAD", 1, SEQ_200), fr("h2d", "HEAD", 1, SEQ_200)],
    )
    seq_case("app_seq_101_to_http", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("h2d", "HEAD", 1, SEQ_101)])
    seq_case("app_seq_ws_text_on_http", [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "WS_TEXT", 1, b"x")])
    seq_case("app_seq_ws_text_before_101", [fr("d2h", "WS_OPEN", 1, SEQ_WS), fr("d2h", "WS_TEXT", 1, b"x")])
    seq_case(
        "app_seq_ws_frames_after_refusal",
        [fr("d2h", "WS_OPEN", 1, SEQ_WS), fr("h2d", "HEAD", 1, SEQ_403), fr("d2h", "WS_TEXT", 1, b"x")],
    )
    seq_case(
        "app_seq_end_on_websocket",
        [fr("d2h", "WS_OPEN", 1, SEQ_WS), fr("h2d", "HEAD", 1, SEQ_101), fr("d2h", "END", 1)],
    )
    seq_case("app_seq_body_on_websocket", [fr("d2h", "WS_OPEN", 1, SEQ_WS), fr("d2h", "BODY", 1, b"x")])
    seq_case(
        "app_seq_ws_text_after_own_close",
        [
            fr("d2h", "WS_OPEN", 1, SEQ_WS),
            fr("h2d", "HEAD", 1, SEQ_101),
            fr("d2h", "WS_CLOSE", 1, (1000).to_bytes(2, "big")),
            fr("d2h", "WS_TEXT", 1, b"x"),
        ],
    )
    seq_case(
        "app_seq_reset_by_host_late_frames_dropped",
        [
            fr("d2h", "HEAD", 1, SEQ_POST),
            fr("d2h", "BODY", 1, b"x"),
            fr("h2d", "RESET", 1, b"timeout"),
            fr("d2h", "BODY", 1, b"y"),
            fr("d2h", "END", 1),
            fr("d2h", "HEAD", 2, SEQ_HEAD),
        ],
        "frames the device sent before it saw the RESET are dropped; the stream stays",
    )
    seq_case(
        "app_seq_reset_by_device",
        [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", "RESET", 1, b"cancelled"), fr("h2d", "HEAD", 1, SEQ_200)],
    )
    seq_case("app_seq_reset_unopened", [fr("d2h", "RESET", 1, b"cancelled")])
    eight = [fr("d2h", "HEAD", i, SEQ_HEAD) for i in range(1, 9)]
    seq_case(
        "app_seq_ninth_exchange_busy",
        [
            *eight,
            fr("d2h", "HEAD", 9, SEQ_HEAD),
            fr("d2h", "END", 9),
            fr("h2d", "HEAD", 1, SEQ_200),
            fr("h2d", "END", 1),
            fr("d2h", "END", 1),
            fr("d2h", "HEAD", 10, SEQ_HEAD),
        ],
        "past 8 open HTTP exchanges: RESET remote.busy, the stream stays; a slot frees when one ends",
    )
    seq_case(
        "app_seq_third_websocket_busy",
        [
            fr("d2h", "WS_OPEN", 1, SEQ_WS),
            fr("d2h", "WS_OPEN", 2, SEQ_WS),
            fr("d2h", "WS_OPEN", 3, SEQ_WS),
            fr("d2h", "HEAD", 4, SEQ_HEAD),
        ],
        "2 WebSockets per stream, counted apart from HTTP",
    )
    n16 = [fill("d2h", "BODY", 1, R.APP_PAYLOAD_MAX) for _ in range(16)]
    rest = R.REQUEST_BODY_MAX - 16 * R.APP_PAYLOAD_MAX
    seq_case(
        "app_seq_request_body_exactly_1mib",
        [fr("d2h", "HEAD", 1, SEQ_POST), *n16, fill("d2h", "BODY", 1, rest), fr("d2h", "END", 1)],
    )
    seq_case(
        "app_seq_request_body_over_1mib",
        [fr("d2h", "HEAD", 1, SEQ_POST), *n16, fill("d2h", "BODY", 1, rest + 1), fr("d2h", "BODY", 1, b"x")],
        "RESET too_large at the byte past 1 MiB; later frames of the exchange are dropped",
    )
    seq_case(
        "app_seq_ping_pong",
        [fr("d2h", "PING", 0), fr("h2d", "PONG", 0), fr("h2d", "PING", 0), fr("d2h", "PING", 0), fr("d2h", "PONG", 0)],
    )
    seq_case("app_seq_pong_unsolicited", [fr("h2d", "PONG", 0)])
    seq_case("app_seq_second_ping_before_pong", [fr("d2h", "PING", 0), fr("d2h", "PING", 0)])
    seq_case(
        "app_seq_bad_frame_closes",
        [fr("d2h", "HEAD", 1, SEQ_HEAD), fr("d2h", 11, 1)],
        "a frame error closes the stream (malformed)",
    )


#: the app frames of the session transcript after ``opened``: device i2r nonce 1, 2; host r2i nonce 1, 2, 3
APP_SESSION = {
    "request_head": ("i2r", 1, R.app_frame("HEAD", 1, cj(req_head(path="/api/w/" + WS + "/runs")))),
    "request_end": ("i2r", 2, R.app_frame("END", 1)),
    "response_head": ("r2i", 1, R.app_frame("HEAD", 1, cj(resp_head()))),
    "response_body": ("r2i", 2, R.app_frame("BODY", 1, b"[]")),
    "response_end": ("r2i", 3, R.app_frame("END", 1)),
}


def app_transport_cases(t: dict[str, Any]) -> None:
    ct: dict[str, bytes] = {}
    for nm, (d, n, pt) in APP_SESSION.items():
        k = bytes.fromhex(t["k_i2r_hex" if d == "i2r" else "k_r2i_hex"])
        ct[nm] = R.CipherState(k=k, n=n).encrypt(b"", pt)
        case(
            "transport",
            f"transport_session_app_{nm}",
            {"transcript": t["name"], "dir": d, "n": n, "ct_hex": ct[nm].hex()},
            {"pt_hex": pt.hex()},
        )
    case(
        "transport",
        "transport_session_app_reordered",
        {"transcript": t["name"], "dir": "r2i", "n": 2, "ct_hex": ct["response_end"].hex()},
        err("handshake"),
        "app frames carry no counter: the Noise nonce orders them and a swap fails the AEAD",
    )
    order = ["request_head", "response_head", "request_end", "response_body", "response_end"]
    seq_case(
        "app_seq_session_transcript",
        [{"dir": "d2h" if APP_SESSION[n][0] == "i2r" else "h2d", "hex": APP_SESSION[n][2].hex()} for n in order],
        "the decrypted app frames of transport_session_app_*",
    )


TPL_ATTACH = W_TPL + "/runs/{run}/attach"
TPL_START = W_TPL + "/agents/start"
RUN1, RUN2 = "r_" + hexid("run-1"), "r_" + hexid("run-2")
START_BODY = cj({"ticket": "DEMO-7", "model": "sonnet"})


def route_args(method: str, template: str, path: str, query: str = "", body: bytes = b"", ws: str = WS) -> dict:
    return {
        "workspace_id": ws,
        "method": method,
        "template": template,
        "path": path,
        "query": query,
        "body_hex": body.hex(),
    }


def route_cases() -> list[bytes]:
    p = "/api/w/" + WS
    good = {
        "route_digest_attach_run_1": route_args("POST", TPL_ATTACH, f"{p}/runs/{RUN1}/attach", body=b"{}"),
        "route_digest_attach_run_2": route_args("POST", TPL_ATTACH, f"{p}/runs/{RUN2}/attach", body=b"{}"),
        "route_digest_agent_start": route_args("POST", TPL_START, f"{p}/agents/start", body=START_BODY),
        "route_digest_agent_start_other_body": route_args(
            "POST", TPL_START, f"{p}/agents/start", body=cj({"ticket": "DEMO-8", "model": "sonnet"})
        ),
        "route_digest_agent_start_other_workspace": route_args(
            "POST", TPL_START, f"/api/w/{WS2}/agents/start", body=START_BODY, ws=WS2
        ),
        "route_digest_terminal_socket": route_args("WEBSOCKET", "/term", "/term", "ticket=t1"),
        "route_digest_terminal_socket_query": route_args("WEBSOCKET", "/term", "/term", "ticket=t2"),
        "route_digest_empty_body": route_args("POST", W_TPL + "/terminals/open", f"{p}/terminals/open"),
    }
    out = []
    for name, a in good.items():
        b = R.route_bytes(
            a["workspace_id"], a["method"], a["template"], a["path"], a["query"], bytes.fromhex(a["body_hex"])
        )
        d = hashlib.sha256(b).digest()
        out.append(d)
        case("route_digest", name, a, {"bytes_hex": b.hex(), "digest_hex": d.hex()})
    assert len(set(out)) == len(out), "every route vector must give its own digest"
    base = good["route_digest_agent_start"]
    for name, over in (
        ("method_lower", {"method": "post"}),
        ("method_connect", {"method": "CONNECT"}),
        ("template_empty", {"template": ""}),
        ("template_space", {"template": "/api/w/{ws} /agents/start"}),
        ("template_too_long", {"template": "/" + "t" * 1024}),
        ("path_dotdot", {"path": "/api/w/../agents/start"}),
        ("path_percent", {"path": "/api/w/%41/agents/start"}),
        ("query_hash", {"query": "a#b"}),
        ("workspace_upper", {"workspace_id": WS.upper()}),
        ("websocket_with_body", {"method": "WEBSOCKET", "template": "/term", "path": "/term"}),
    ):
        case("route_digest", f"route_digest_{name}", base | over, err("malformed"))
    return out


def presence_route_cases(route_d: list[bytes]) -> None:
    """A batch over route digests (and one op digest) on stream A: valid on A only, for exactly its digests."""
    attach1, attach2, start = route_d[0], route_d[1], route_d[2]
    digests = sorted([attach1, start, DIGESTS[0]])
    msg = R.presence_bytes(WS, DEV_A, HH, NONCE2, digests)
    good = p256_assert("presence_a", msg)

    def a(**over: Any) -> dict[str, Any]:
        p = {
            "entry": ENTRY_A,
            "workspace_id": WS,
            "handshake_hash_hex": HH.hex(),
            "nonce_hex": NONCE2.hex(),
            "digests_hex": [d.hex() for d in digests],
            "assertion": good,
            "held": 0,
            "issued": {NONCE2.hex(): NOW - 1_000},
            "now_ms": NOW,
        }
        p.update(over)
        return p

    case("presence", "presence_route_batch_ok", a(), {"count": 0}, "route digests and an op digest in one batch")
    case(
        "presence",
        "presence_route_batch_replayed_into_other_stream",
        a(handshake_hash_hex=HH_B.hex()),
        err("bad_signature"),
        "a batch over route digests is bound to its stream's handshake hash",
    )
    other = sorted([attach2, start, DIGESTS[0]])
    case(
        "presence",
        "presence_route_batch_other_run",
        a(digests_hex=[d.hex() for d in other]),
        err("bad_signature"),
        "the attach to run 1 does not stand for the attach to run 2",
    )
    case(
        "presence",
        "presence_route_batch_other_workspace",
        a(workspace_id=WS2),
        err("bad_signature"),
    )


REQUIRED = [
    "msg1_replayed_into_other_stream",
    "msg1_replayed_into_other_workspace",
    "msg1_replayed_for_other_device",
    "msg1_with_early_data",
    "transport_flipped_byte",
    "open_replayed_into_other_stream",
    "open_replayed_into_other_workspace",
    "open_replayed_for_other_device",
    "open_ctap2_up_only",
    "open_ctap2_extension_data_flag",
    "open_ctap2_backup_eligible",
    "open_ctap2_backup_state",
    "open_ctap2_counter_decreased",
    "open_fido2_device_with_p256_form",
    "presence_replayed_into_other_stream",
    "presence_replayed_into_other_workspace",
    "presence_replayed_for_other_device",
    "presence_nonce_not_issued",
    "presence_batch_expired",
    "presence_digest_dropped",
    "presence_with_open_signature",
    "open_with_presence_signature",
    "enroll_wrong_code",
    "enroll_presence_key_swapped",
    "enroll_extra_field",
    "device_list_tampered_level_cap",
    "device_list_tampered_added_device",
    "device_list_rollback",
    "device_list_wrong_label",
    "stream_open_w2h",
    "stream_data_w2h_signed_by_hub",
    "stream_data_over_64k",
    "gate_session_while_off",
    "gate_enroll_while_off",
    "gate_enroll_no_code",
    "gate_session_unknown_device",
    "gate_session_device_streams_full",
    "open_msg_client_too_old",
    "session_static_other_device_key",
    # R3, inside a session
    "app_frame_over_max",
    "app_frame_kind_11",
    "app_frame_head_extra_key",
    "app_frame_head_not_canonical",
    "app_frame_head_path_dotdot",
    "app_frame_head_host",
    "app_frame_head_header_newline",
    "app_frame_response_set_cookie",
    "app_frame_ws_open_from_host",
    "request_head_bearer_authorization",
    "request_head_bearer_orch_token",
    "app_seq_id_reused_after_end",
    "app_seq_id_reused_while_open",
    "app_seq_body_before_head",
    "app_seq_response_unopened",
    "app_seq_101_to_http",
    "app_seq_ws_text_before_101",
    "app_seq_reset_by_host_late_frames_dropped",
    "app_seq_ninth_exchange_busy",
    "app_seq_third_websocket_busy",
    "app_seq_request_body_over_1mib",
    "app_seq_pong_unsolicited",
    "transport_session_app_reordered",
    "route_digest_attach_run_1",
    "route_digest_attach_run_2",
    "route_digest_websocket_with_body",
    "presence_route_batch_replayed_into_other_stream",
    "presence_route_batch_other_run",
]


def build() -> dict[str, Any]:
    CASES.clear()
    ts = [
        transcript("session", SESSION, "session", DEV_A, STREAM, OPEN_MSG, {"t": "opened", "api": 3}),
        transcript(
            "enroll",
            ENROLL,
            "enroll",
            CODE,
            STREAM_E,
            ENROLL_MSG,
            {"t": "pending", "fingerprint": R.device_fp(XP["dev_new_s"], PP["presence_new"], "platform").hex()},
        ),
    ]
    prologue_cases()
    msg1_cases()
    msg2_cases()
    transport_cases(ts)
    app_transport_cases(ts[0])
    open_cases()
    presence_cases()
    enroll_cases()
    derived_cases()
    device_list_cases()
    frames = stream_link_cases()
    cost_cases(frames)
    gate_cases()
    custodial_cases()
    app_frame_cases()
    request_head_cases()
    app_seq_cases()
    presence_route_cases(route_cases())
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "duplicate case name"
    assert set(REQUIRED) <= set(names)
    cases = copy.deepcopy(CASES)
    return {
        "about": "FAKE keys, test-only. Remote access through the hub, contracts R0 and R3 (protocol.md 10.2c, 8.5; #1588, #1591).",
        "labels": R.REMOTE_LABELS,
        "constants": {
            "noise_protocol": R.NOISE_PROTOCOL.decode(),
            "remote_version": R.REMOTE_VERSION,
            "kinds": R.KINDS,
            "frame_max": R.FRAME_MAX,
            "frame_min_cost": R.FRAME_MIN_COST,
            "challenge_len": R.CHALLENGE_LEN,
            "challenge_ttl_ms": R.CHALLENGE_TTL_MS,
            "batch_ttl_ms": R.BATCH_TTL_MS,
            "digests_max": R.DIGESTS_MAX,
            "devices_max": R.DEVICES_MAX,
            "device_life_max_ms": R.DEVICE_LIFE_MAX_MS,
            "limits": R.LIMITS,
            "byte_budget": R.BYTE_BUDGET,
            "levels": list(R.LEVELS),
            "presence_kinds": R.PRESENCE_KINDS,
            "ctap2_rp_id": R.CTAP2_RP_ID.decode(),
            "codes": list(R.CODES),
            "close_reasons": list(R.CLOSE_REASONS),
            "link_window_ms": 120_000,
            "app": {
                "kinds": R.APP_KINDS,
                "plain_max": R.APP_PLAIN_MAX,
                "payload_max": R.APP_PAYLOAD_MAX,
                "head_max": R.HEAD_MAX,
                "headers_max": R.HEADERS_MAX,
                "header_name_max": R.HEADER_NAME_MAX,
                "header_value_max": R.HEADER_VALUE_MAX,
                "path_max": R.PATH_MAX,
                "query_max": R.QUERY_MAX,
                "template_max": R.TEMPLATE_MAX,
                "methods": list(R.METHODS),
                "reset_codes": list(R.RESET_CODES),
                "exchanges_max": R.EXCHANGES_MAX,
                "websockets_max": R.WEBSOCKETS_MAX,
                "request_body_max": R.REQUEST_BODY_MAX,
                "request_end_ms": R.REQUEST_END_MS,
                "framing_headers": sorted(R.FRAMING_HEADERS),
                "request_refused_headers": sorted(R.REQUEST_ONLY_REFUSED),
                "response_refused_headers": sorted(R.RESPONSE_ONLY_REFUSED),
                "bearer_headers": sorted(R.BEARER_HEADERS),
                "bearer_prefix": R.BEARER_PREFIX,
            },
        },
        "keys": {
            "x25519": {n: {"priv_hex": X[n].hex(), "pub_hex": XP[n].hex()} for n in X_LABELS},
            "p256": {
                n: {"scalar_hex": P[n].private_numbers().private_value.to_bytes(32, "big").hex(), "pub": b64u(PP[n])}
                for n in P_LABELS
            },
        },
        "ids": {
            "workspace_id": WS,
            "workspace_id_2": WS2,
            "hub_id": HUB,
            "hub_id_2": HUB2,
            "device_a": DEV_A,
            "device_b": DEV_B,
            "device_c": DEV_C,
            "code_id": CODE,
            "code_secret_hex": CODE_SECRET.hex(),
            "person": PERSON,
            "owner": OWNER,
            "now_ms": NOW,
        },
        "device_list": LIST,
        "transcripts": ts,
        "required": REQUIRED,
        "cases": cases,
    }


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "vectors_remote.json"
    doc = build()
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=True) + "\n")
    print(f"wrote {out} ({len(doc['cases'])} cases)", file=sys.stderr)
