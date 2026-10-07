"""Every case of the bridge protocol's vector file (tests/fixtures/bridge, a pinned copy of orch-tix's
tests/bridge_vectors.json) against orch.remote.bridge_host. The vectors are the contract: a failing case is a bug
here, never a reason to change the file."""
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from orch.remote.bridge_host import envelope as E, files, keys, signatures  # noqa: E402
from orch.remote.bridge_host.assertion import (Issued, assertion_challenge, registration_challenge,  # noqa: E402
                                               verify_assertion)
from orch.remote.bridge_host.budgets import BUDGET, BUDGET_WINDOW_MS, OFFER_BUDGET, SlidingLimit  # noqa: E402
from orch.remote.bridge_host.host_check import SEQ_WINDOW, WINDOW_MS, Host, Requirement  # noqa: E402
from orch.remote.bridge_host.pairing import LABEL_MAX, Offer, Pairing, Pending  # noqa: E402
from orch.remote.bridge_host.registry import Credential, Device  # noqa: E402
from orch.remote.bridge_host.replay_store import MAX_REPLAY_BODY, RETENTION_MS, Record, _decode  # noqa: E402
from orch.remote.bridge_host.shown import clean_shown, subject_hash  # noqa: E402

VECTORS = Path(__file__).parent / "fixtures" / "bridge" / "bridge_vectors.json"
VECTORS_SHA256 = "11abcbd4b981ba85628f30778a351b288128e4ba2b494ecb3d8b56badfe6372d"
RAW = VECTORS.read_bytes()
VEC = json.loads(RAW)
K_WS = bytes.fromhex(VEC["hkdf"][0]["okm"])
HOST_PUB = bytes.fromhex(VEC["keys"]["host"]["pub"])
RP_ID, ORIGIN = "tix.example", "https://tix.example"
MISSING = object()


def test_the_vector_file_is_the_pinned_copy():
    assert hashlib.sha256(RAW).hexdigest() == VECTORS_SHA256


def test_constants_and_labels_match():
    c = VEC["constants"]
    assert (c["magic"], c["header_len"], c["tag_len"], c["sig_len"]) == (E.MAGIC.decode(), E.HEADER_LEN, E.TAG_LEN,
                                                                         E.SIG_LEN)
    assert (c["max_request"], c["max_chunk"], c["max_meta"]) == (E.MAX_REQUEST, E.MAX_CHUNK, E.MAX_META)
    assert (c["window_ms"], c["seq_window"], c["rid_retention_ms"]) == (WINDOW_MS, SEQ_WINDOW, RETENTION_MS)
    assert (c["budget"], c["budget_window_ms"], c["offer_budget"]) == (BUDGET, BUDGET_WINDOW_MS, OFFER_BUDGET)
    from orch.remote.bridge_host import assertion as A
    mine = {"ws": keys.L_WS, "msg": keys.L_MSG, "sig": signatures.L_SIG, "device": keys.L_DEVICE, "fp": keys.L_FP,
            "host": keys.L_HOST, "pair": keys.L_PAIR, "phone_link": keys.L_PHONE, "assert": A.L_ASSERT,
            "webauthn_reg": A.L_REG}
    assert {k: v.decode() for k, v in mine.items()} == c["labels"]
    assert VEC["version"] == E.VERSION and MAX_REPLAY_BODY == 64 * 1024


# -- hkdf, seal, sign, ids, pairing ------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", VEC["hkdf"], ids=lambda c: c["name"])
def test_hkdf(case):
    assert keys.hkdf(bytes.fromhex(case["ikm"]), bytes.fromhex(case["salt"]), bytes.fromhex(case["info"])).hex() \
        == case["okm"]


def test_workspace_and_message_keys():
    mk, ws = bytes.fromhex(VEC["keys"]["mk"]), VEC["keys"]["workspace"]
    assert keys.workspace_key(mk, ws).hex() == VEC["hkdf"][0]["okm"]
    assert keys.workspace_key(mk, "0" * 32).hex() == VEC["hkdf"][1]["okm"]
    assert keys.message_key(K_WS, bytes.fromhex(VEC["hkdf"][2]["salt"])).hex() == VEC["hkdf"][2]["okm"]


@pytest.mark.parametrize("case", VEC["seal"], ids=lambda c: c["name"])
def test_seal_and_open(case):
    f = case["header_fields"]
    h = E.Header(f["direction"], f["flags"], bytes.fromhex(f["workspace"]), bytes.fromhex(f["device"]),
                 bytes.fromhex(f["rid"]), bytes.fromhex(f["stream"]), f["seq"], f["ts_ms"], bytes.fromhex(f["salt"]),
                 f["key_version"], f["version"], f["magic"].encode())
    hb, pt = h.encode(), bytes.fromhex(case["plaintext"])
    assert hb.hex() == case["header"] and E.Header.decode(hb) == h
    assert keys.message_key(bytes.fromhex(case["k_ws"]), h.salt).hex() == case["message_key"]
    assert keys.ZERO_NONCE.hex() == case["nonce"]
    assert keys.seal(bytes.fromhex(case["k_ws"]), hb, pt).hex() == case["sealed"]
    assert keys.open_sealed(bytes.fromhex(case["k_ws"]), hb, bytes.fromhex(case["sealed"])) == pt
    meta, data = E.unframe(pt)
    assert E.frame(meta, data) == pt


@pytest.mark.parametrize("case", VEC["sign"], ids=lambda c: c["name"])
def test_sign_verify(case):
    assert signatures.verify(bytes.fromhex(case["pub"]), bytes.fromhex(case["sig"]), bytes.fromhex(case["msg"])) \
        is case["valid"]


@pytest.mark.parametrize("case", VEC["sig_scalars"], ids=lambda c: c["name"])
def test_signature_scalars(case):
    assert signatures.scalars_in_range(bytes.fromhex(case["sig"])) is case["in_range"]


def test_ids_fingerprints_and_host_pin():
    ws = bytes.fromhex(VEC["keys"]["workspace"])
    for name in ("device_a", "device_b"):
        c, pub = VEC["ids"][name], bytes.fromhex(VEC["ids"][name]["pub"])
        assert keys.device_id(ws, pub).hex() == c["device_id"]
        assert keys.device_id(bytes(16), pub).hex() == c["device_id_other_workspace"]
        assert keys.device_fingerprint(pub) == c["fingerprint"]
    assert keys.host_pin(HOST_PUB).hex() == VEC["ids"]["host_pin"]
    for name in ("device_a", "device_b", "host", "intruder", "authenticator"):  # the fake keys are what they say
        k = VEC["keys"][name]
        assert signatures.public_bytes(signatures.private_key(bytes.fromhex(k["d"]))).hex() == k["pub"]


def test_pairing_vector():
    p = VEC["pairing"]
    ws, pid, secret = bytes.fromhex(p["workspace"]), bytes.fromhex(p["pairing_id"]), bytes.fromhex(p["secret"])
    pub = bytes.fromhex(p["device_pub"])
    assert keys.host_pin(bytes.fromhex(p["host_pub"])).hex() == p["host_pin"]
    assert keys.pair_mac(secret, ws, pid, pub).hex() == p["mac"]
    assert keys.device_id(ws, pub).hex() == p["device_id"]
    assert keys.device_fingerprint(pub) == p["device_fingerprint"]
    assert keys.phone_link_proof(bytes.fromhex(p["phone_key"]), bytes.fromhex(p["device_id"])).hex() == p["phone_proof"]
    draws = iter([pid, secret])
    offer, fragment = Pairing(ws).offer("decide", 0, lambda n: next(draws), bytes.fromhex(p["host_pub"]))
    assert fragment == p["link_fragment"] and offer.expires_ms == 600_000


@pytest.mark.parametrize("case", VEC["shown"], ids=lambda c: c["name"])
def test_shown(case):
    text = "".join(map(chr, case["input"]))
    if case["expect"] is None:
        with pytest.raises(ValueError):
            clean_shown(text)
    else:
        assert clean_shown(text) == case["expect"]


# -- host cases ---------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


def host_from_state(config_dir: Path, state: dict, clock: Clock, route=None) -> Host:
    """A real host (file-backed registry and request store under a temporary config dir) holding the vector state."""
    ws = bytes.fromhex(state["workspace"])
    phones = {k: bytes.fromhex(v) for k, v in state["phones"].items()}
    host = Host(workspace=ws, k_ws=K_WS, host_key=signatures.private_key(bytes.fromhex(VEC["keys"]["host"]["d"])),
                root=files.bridge_dir(config_dir, state["workspace"]), clock=clock, rand=__import__("os").urandom,
                route=route or (lambda meta, data: Requirement("look")), phone_key=phones.get, rp_id=RP_ID,
                origin=ORIGIN, key_version=state["key_version"], **state.get("limits", {}))
    for did, d in state["devices"].items():
        host.registry.add(Device(did, bytes.fromhex(d["pub"]), d["scope"], "", 0, d["revoked"]), 0)
        host.store.save_seq(did, d["high"], d["bitmap"])
    for rid, r in state["rids"].items():
        if r.get("damaged"):  # an unreadable file: holds a slot of the total cap, belongs to no device
            (host.store.dir / f"{rid}.json").write_text("not a record", encoding="utf-8")
            host.store.damaged.add(rid)
            continue
        host.store.put(rid, Record(r["device"], r["digest"], r["until"] - RETENTION_MS, r["until"], r["outcome"]),
                       clock.now)
    for pid, o in state["offers"].items():
        host.pairing.offers[pid] = Offer(bytes.fromhex(pid), bytes.fromhex(o["secret"]), o["scope"], o["expires_ms"],
                                         o.get("used", False), SlidingLimit(OFFER_BUDGET, BUDGET_WINDOW_MS,
                                                                            list(o.get("unverified", []))))
    for did, p in state["pending_pairs"].items():
        host.pairing.pending[did] = Pending(bytes.fromhex(p["pub"]), p["pairing_id"], p["scope"], p["phone_link"],
                                            p["label"], p["state"])
    host.streams.update(state["streams"])
    host.budget.entries = list(state["unverified"])
    return host


def flat(v) -> dict:
    """A verdict in the vector file's vocabulary."""
    d = {"result": v.result}
    if v.code is not None:
        d["code"] = v.code
    if v.result == "refuse" and v.header is not None:  # a refusal answering a STREAM request carries the flag
        d["stream"] = bool(v.header.flags & E.F_STREAM)
    if v.result in ("pair_pending", "pair_status"):  # the vectors name the answer's meta separately
        d["answer"] = v.fields
    else:
        d.update(v.fields)
    if v.result == "accept":
        d.update(scope=v.scope, meta=v.meta, data=v.data.hex())
    if v.result == "replay":
        d["outcome"] = v.outcome
    return d


def record_until(host: Host, rid: str):
    if not (len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)):
        return None
    raw = files.read(host.store.dir / f"{rid}.json", 1 << 20)
    return None if raw is None else _decode(raw).until


def flat_pairing(host: Host, v) -> dict:
    """flat() plus what the host shows the owner for a pair_pending (scope, phone link, label, fingerprint)."""
    d = flat(v)
    if v.result == "pair_pending":
        p = host.pairing.pending[v.device]
        d.update(fingerprint=p.fingerprint, device=v.device, scope=p.scope, phone_link=p.phone_link, label=p.label)
    return d


def run_host_case(tmp_path, case) -> list[str]:
    """Each step against ONE host; returns the mismatches."""
    clock = Clock(case.get("now_ms", (case.get("steps") or [{}])[0].get("now_ms", 0)))
    host = host_from_state(tmp_path, case["state"], clock)
    bad = []
    for i, st in enumerate(case.get("steps", [case])):
        env = bytes(st["envelope_zeros"]) if "envelope_zeros" in st else bytes.fromhex(st["envelope"])
        clock.now = st["now_ms"]
        got = flat_pairing(host, host.check(env, st["mailbox_id"]))
        want = st["expect"]
        if {k: got.get(k, MISSING) for k in want} != want:
            bad.append(f"step {i}: got {got}, want {want}")
        until = record_until(host, st["mailbox_id"])
        if until != st["record_until"]:
            bad.append(f"step {i}: record until {until}, want {st['record_until']}")
    return bad


@pytest.mark.parametrize("case", VEC["host_cases"], ids=lambda c: c["name"])
def test_host_case(tmp_path, case):
    assert run_host_case(tmp_path, case) == []


def test_every_host_step_runs():
    steps = sum(len(c.get("steps", [c])) for c in VEC["host_cases"])
    assert len(VEC["host_cases"]) == 76 and steps == 104


# -- device cases: the responses the vectors sign with the host key open and verify with these primitives --------------
# The device side itself is the TIX app's; this is a port of the reference in orch-tix (tests/support/
# bridge_protocol_ref.py), so the vectors' responses are proved to be exactly what a conforming device accepts.

import hmac  # noqa: E402
import re  # noqa: E402

MAX_OFFSET_MS, PIN_FAILURES, MAX_LABEL = 86_400_000, 3, 80
DROP = {"result": "drop"}


def _adopt(res: dict, pend: dict, meta: dict, now_ms: int) -> None:
    """A verified stale_timestamp refusal gives the clock offset: once per pending request, at most 24 h (§5.1)."""
    if not pend.get("offset_adopted"):
        pend["offset_adopted"] = True
        off = meta["host_ms"] - now_ms
        if abs(off) <= MAX_OFFSET_MS:
            res["offset_ms"] = off
        else:
            res["clock_wrong"] = True


def device_check(env: bytes, pending: dict, mailbox: dict, now_ms: int, offset_ms: int) -> dict:
    if not E.OVERHEAD <= len(env) <= E.MAX_CHUNK:
        return DROP
    hb, body, sig = E.split(env)
    h = E.Header.decode(hb)
    if h.magic != E.MAGIC or h.version != E.VERSION or h.direction != E.TO_DEVICE \
            or h.flags & ~(E.F_LAST | E.F_STREAM | E.F_REFUSAL) or (h.flags & E.F_REFUSAL and not h.flags & E.F_LAST):
        return DROP
    if h.key_version != 1 or h.workspace.hex() != VEC["keys"]["workspace"] \
            or h.device.hex() != VEC["ids"]["device_a"]["device_id"]:
        return DROP
    pend = pending.get(h.rid.hex())
    if pend is None:
        return DROP
    if (mailbox["id"], mailbox["idx"], mailbox["last"], mailbox["stream"]) != \
            (h.rid.hex(), h.seq, bool(h.flags & E.F_LAST), bool(h.flags & E.F_STREAM)) or pend["stream"] != mailbox["stream"]:
        return DROP
    if not signatures.verify(HOST_PUB, sig, signatures.signed_bytes(hb, body)):
        try:  # only a K_ws holder can make the tag verify: only such a chunk is evidence the host key changed (§7)
            keys.open_sealed(K_WS, hb, body)
            return {**DROP, "pin_failure": True}
        except keys.InvalidTag:
            return {**DROP, "pin_failure": False}
    try:
        meta, data = E.unframe(keys.open_sealed(K_WS, hb, body))
    except (keys.InvalidTag, E.Malformed):
        return DROP
    if h.seq != pend["next"]:
        return DROP
    res = {"result": "accept", "last": bool(h.flags & E.F_LAST), "refusal": bool(h.flags & E.F_REFUSAL),
           "meta": meta, "data": data.hex(), "offset_ms": None, "clock_wrong": None}
    if h.flags & E.F_REFUSAL and meta.get("refusal") == "stale_timestamp" and type(meta.get("host_ms")) is int:
        _adopt(res, pend, meta, now_ms)
    elif abs(now_ms + offset_ms - h.ts_ms) > WINDOW_MS:
        return DROP
    pend["next"] += 1
    return res


def pin_run(results: list) -> list:
    """After each result: has the device said "the host key changed: pair again"? A pin failure counts, a verified
    chunk resets the run, any other drop neither counts nor resets (§7)."""
    run, alarm, out = 0, False, []
    for r in results:
        if r.get("pin_failure"):
            run += 1
            alarm = alarm or run >= PIN_FAILURES
        elif r["result"] == "accept":
            run = 0
        out.append(alarm)
    return out


def open_pending_answer(env: bytes, case: dict) -> dict:
    """Every answer to a pair request (§8.1 step 4): tag first, then the pin of the host_pub it carries, then the
    signature with that key, then the rest of §7."""
    mailbox, pending, now_ms = case["mailbox"], json.loads(json.dumps(case["pending"])), case["now_ms"]
    if not E.OVERHEAD <= len(env) <= E.MAX_CHUNK:
        return DROP
    hb, body, sig = E.split(env)
    h = E.Header.decode(hb)
    if h.magic != E.MAGIC or h.version != E.VERSION or h.direction != E.TO_DEVICE \
            or h.flags not in (E.F_LAST, E.F_LAST | E.F_REFUSAL):
        return DROP
    refusal = bool(h.flags & E.F_REFUSAL)
    if h.key_version != 1 or h.workspace.hex() != VEC["keys"]["workspace"] \
            or h.device.hex() != VEC["ids"]["device_a"]["device_id"]:
        return DROP
    pend = pending.get(h.rid.hex())
    if pend is None:
        return DROP
    if (mailbox["id"], mailbox["idx"], mailbox["last"], mailbox["stream"]) != (h.rid.hex(), h.seq, True, False):
        return DROP
    try:
        meta, _ = E.unframe(keys.open_sealed(K_WS, hb, body))
        host_pub = bytes.fromhex(meta["host_pub"])
    except (keys.InvalidTag, E.Malformed, ValueError, KeyError, TypeError):
        return DROP
    if len(host_pub) != 65 or (meta.get("refusal") is None if refusal else meta.get("state") != "pending"):
        return DROP
    if not hmac.compare_digest(keys.host_pin(host_pub), bytes.fromhex(case["host_pin"])):
        return DROP
    if not signatures.verify(host_pub, sig, signatures.signed_bytes(hb, body)):
        return DROP
    if h.seq != pend["next"]:
        return DROP
    res = {"result": "accept", "host_pub": host_pub.hex(), "fingerprint": None if refusal else meta.get("fingerprint"),
           "refusal": meta["refusal"] if refusal else None, "offset_ms": None, "clock_wrong": None}
    if refusal and meta["refusal"] == "stale_timestamp" and type(meta.get("host_ms")) is int:
        _adopt(res, pend, meta, now_ms)
    elif abs(now_ms - h.ts_ms) > WINDOW_MS:
        return DROP
    return res


@pytest.mark.parametrize("case", VEC["device_cases"], ids=lambda c: c["name"])
def test_device_case(case):
    got = device_check(bytes.fromhex(case["envelope"]), json.loads(json.dumps(case["pending"])), case["mailbox"],
                       case["now_ms"], case["offset_ms"])
    assert {k: got.get(k) for k in case["expect"]} == case["expect"]


@pytest.mark.parametrize("case", VEC["pending_answers"], ids=lambda c: c["name"])
def test_pending_answer(case):
    got = open_pending_answer(bytes.fromhex(case["envelope"]), case)
    assert {k: got.get(k) for k in case["expect"]} == case["expect"]


@pytest.mark.parametrize("case", VEC["pin_runs"], ids=lambda c: c["name"])
def test_pin_runs(case):
    by_name = {c["name"]: c for c in VEC["device_cases"]}
    results = [device_check(bytes.fromhex(by_name[n]["envelope"]), json.loads(json.dumps(by_name[n]["pending"])),
                            by_name[n]["mailbox"], by_name[n]["now_ms"], by_name[n]["offset_ms"])
               for n in case["steps"]]
    assert pin_run(results) == case["alarm"]


_FRAGMENT = re.compile(r"#?v1\.([0-9a-f]{32})\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})")


def parse_pair_fragment(fragment: str):
    """The pairing link, strictly (§8.1 step 1): the device's parser, ported."""
    m = _FRAGMENT.fullmatch(fragment)
    if not m:
        return None
    try:
        s, pin = E.unb64u(m.group(3)), E.unb64u(m.group(4))
    except ValueError:  # not canonical
        return None
    if len(s) != 32 or len(pin) != 32:
        return None
    return {"workspace": m.group(1), "pairing_id": m.group(2), "secret": s.hex(), "host_pin": pin.hex()}


@pytest.mark.parametrize("case", VEC["links"], ids=lambda c: c["name"])
def test_links(case):
    assert parse_pair_fragment(case["fragment"]) == case["parsed"]


def test_the_host_offers_a_link_the_strict_parser_accepts():
    draws = iter([bytes(range(16)), bytes(range(32))])
    _, fragment = Pairing(bytes.fromhex(VEC["keys"]["workspace"])).offer("decide", 0, lambda n: next(draws), HOST_PUB)
    parsed = parse_pair_fragment(fragment)
    assert parsed and parsed["host_pin"] == keys.host_pin(HOST_PUB).hex() and parsed["secret"] == bytes(range(32)).hex()


@pytest.mark.parametrize("case", VEC["labels"], ids=lambda c: c["name"])
def test_labels(case):
    text = "".join(map(chr, case["codepoints"]))
    assert [ord(c) for c in clean_shown(text)[:LABEL_MAX]] == case["host_stores"]
    assert (not any(0xD800 <= ord(c) <= 0xDFFF for c in text) and len(text) <= MAX_LABEL) is case["device_accepts"]


@pytest.mark.parametrize("case", VEC["challenge_parts"], ids=lambda c: c["name"])
def test_challenge_parts(case):
    m = case["meta"]
    nonce, exp = m.get("nonce"), m.get("expires_ms")
    ok = isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{64}", nonce) and type(exp) is int and exp >= 0
    assert ({"nonce": nonce, "expires_ms": exp} if ok else None) == case["parsed"]


# -- assertions ---------------------------------------------------------------------------------------------------------

def test_assertion_challenges():
    a = VEC["assertion"]
    i = a["challenge_inputs"]
    ws, dev, rid, nonce = (bytes.fromhex(i[k]) for k in ("workspace", "device", "rid", "nonce"))
    assert E.canonical_json(i["subject"]).decode() == i["subject_json"]
    assert subject_hash(i["subject"]).hex() == i["subject_hash"]
    assert assertion_challenge(ws, dev, rid, i["purpose"], i["scope"], i["expires_ms"], nonce, i["subject"]).hex() \
        == a["challenge"]
    from orch.remote.bridge_host.assertion import LEASE_SUBJECT
    assert assertion_challenge(ws, dev, rid, "lease", "type", i["expires_ms"], nonce, LEASE_SUBJECT).hex() \
        == a["lease_challenge"]
    r = a["registration_challenge"]
    assert registration_challenge(bytes.fromhex(r["workspace"]), bytes.fromhex(r["device"]), r["expires_ms"],
                                  bytes.fromhex(r["nonce"])).hex() == r["challenge"]


def run_assertion_case(case):
    c = case["credential"]
    cred = Credential(bytes.fromhex(c["credential_id"]), bytes.fromhex(c["pub"]), c["sign_count"], c["be"], False,
                      c["rp_id"], c["origin"])
    pending = {k: Issued(v["device"], v["expires_ms"]) for k, v in case["pending"].items()}
    a = case["assertion"]
    v = verify_assertion(cred, c["device"], pending, case["sender"], bytes.fromhex(a["credential_id"]),
                         bytes.fromhex(a["authenticator_data"]), bytes.fromhex(a["client_data_json"]),
                         bytes.fromhex(a["signature"]), case["now_ms"])
    got = ({"result": "verified", **({"counter_warning": True} if v.counter_warning else {})} if v.ok
           else {"result": "refuse", "code": "assertion_failed", "why": v.why})
    after = v.sign_count if v.sign_count is not None else cred.sign_count
    return got, after, pending


@pytest.mark.parametrize("case", VEC["assertion"]["cases"], ids=lambda c: c["name"])
def test_assertion_case(case):
    got, after, pending = run_assertion_case(case)
    assert got == case["expect"]
    assert after == case["sign_count_after"]
    named = json.loads(bytes.fromhex(case["assertion"]["client_data_json"]))["challenge"]
    assert E.unb64u(named).hex() not in pending  # the challenge it names is used up, whatever happened


def test_vector_counts():
    """Nothing in the file goes unrun: the sections and their sizes this suite covers."""
    assert sorted(VEC) == sorted(["version", "constants", "comment", "keys", "hkdf", "ids", "seal", "sign", "sig_scalars",
                                  "host_cases", "pairing", "device_cases", "pin_runs", "pending_answers", "labels",
                                  "links", "challenge_parts", "shown", "assertion"])
    assert [len(VEC[k]) for k in ("hkdf", "seal", "sign", "sig_scalars", "device_cases", "shown")] == [3, 1, 8, 7, 37, 6]
    assert [len(VEC[k]) for k in ("pin_runs", "pending_answers", "labels", "links", "challenge_parts")] \
        == [4, 22, 6, 9, 7]
    assert len(VEC["assertion"]["cases"]) == 17
