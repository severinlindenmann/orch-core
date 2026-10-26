"""H13-0: the push object builder and opener (orch-relay protocol-v2 §9, §19.10; orch-v2-host 11A.16.2, 11A.16.4).

``outer = {"v":2, "ws", "epoch", "c": b64u(SALTED-AEAD(K_push(e), "orch/v2/push-msg", "orch/v2/push-aad|" || suite || ws
|| u32(e), cj({"p": payload, "sig"})))}``. The signature is made by the caller (``RelaySigner.push``, H13-1) over
:func:`orch.crypto.push.push_signed_bytes`; the builder never holds a signing key. Every ``push`` vector is reproduced
byte-exact (the vector's salt, the vector's signature), and every case is replayed through the opener."""

from __future__ import annotations

import json
import types

import pytest

from orch import canon, crypto
from orch.crypto import push
from orch.crypto.encoding import b64u, unb64u

from .vectors import ALL, S2, priv, pub

V = S2["push"]
IDS = S2["ids"]
WS = bytes.fromhex(IDS["workspace_a"])
WK1 = bytes.fromhex(V["cases"][0]["keys"][IDS["workspace_a"]]["wk"]["1"])
NOW = V["payload"]["ts_ms"]


def _keys(case_keys: dict) -> dict:
    return {
        ws: push.PushKeys(wsk_pub=unb64u(k["wsk_pub"]), wk={int(e): bytes.fromhex(w) for e, w in k["wk"].items()})
        for ws, k in case_keys.items()
    }


def _inner(raw: bytes, wk: bytes, epoch: int = 1) -> dict:
    outer = json.loads(raw)
    c = unb64u(outer["c"])
    pt = crypto.salted_open(push.k_push(wk, WS, epoch), crypto.L["kdf_push_msg"], push.push_aad(WS, epoch), c)
    return json.loads(pt)


def test_constants_match_the_vectors():
    c = ALL["constants"]["push"]
    assert c["max_bytes"] == push.MAX_PUSH
    assert c["max_age_ms"] == push.MAX_AGE_MS
    assert tuple(c["kinds"]) == push.PUSH_KINDS
    assert set(push.HOST_PUSH_KINDS) == {"question", "question.closed", "drop.new", "ticket.update"}  # D2 + B10 Part B
    assert set(push.HOST_PUSH_KINDS) < set(push.PUSH_KINDS)


def test_k_push_aad_and_signed_bytes_vectors():
    assert push.k_push(WK1, WS, 1).hex() == V["k_push"]
    assert push.push_aad(WS, 1).hex() == V["aad"]
    assert push.push_signed_bytes(WS, 1, V["payload"]).hex() == V["signed_bytes"]


def _reseal(raw: str, wk: bytes = WK1, epoch: int = 1) -> bytes:
    inner = _inner(raw.encode(), wk, epoch)
    salt = unb64u(json.loads(raw)["c"])[:16]
    with pytest.MonkeyPatch.context() as mp:  # the salt drawn inside orch.crypto.suite, fixed for this call only
        mp.setattr(crypto.suite, "secrets", types.SimpleNamespace(token_bytes=lambda n: salt[:n]))
        k = push.k_push(wk, WS, epoch)
        return push._seal_push(k, WS, epoch, inner["p"], unb64u(inner["sig"]), kinds=push.PUSH_KINDS, padded=False)


def test_the_raw_vector_is_reproduced_byte_exact():
    assert _reseal(V["raw"]) == V["raw"].encode()
    assert V["salt"] == unb64u(json.loads(V["raw"])["c"])[:16].hex()


def test_every_case_raw_that_opens_is_reproduced_byte_exact():
    n = 0
    for case in V["cases"]:
        for step in case["steps"]:
            raw = step["raw"].encode()
            for ws, k in case["keys"].items():
                if ws != IDS["workspace_a"]:
                    continue
                for e, wk in k["wk"].items():
                    try:
                        _inner(raw, bytes.fromhex(wk), int(e))
                    except Exception:
                        continue
                    if json.loads(raw)["epoch"] != int(e):
                        continue
                    try:
                        again = _reseal(step["raw"], bytes.fromhex(wk), int(e))
                    except push.PushError:  # unknown_kind and extra_field: the builder refuses those payloads
                        assert case["name"] in ("unknown_kind", "extra_field")
                        continue
                    assert again == raw, case["name"]
                    n += 1
    assert n >= 8


@pytest.mark.parametrize("case", V["cases"], ids=lambda c: c["name"])
def test_push_cases_through_the_opener(case):
    keys = _keys(case["keys"])
    last: dict[str, int] = {}
    for step in case["steps"]:
        want = step["expect"]
        try:
            p = push.open_push(step["raw"].encode(), keys, step["now_ms"])
        except push.PushError as e:
            assert want == {"result": "drop", "why": e.why}, case["name"]
            continue
        key = f"{json.loads(step['raw'])['ws']}/{p['id']}"
        if key in last and p["ts_ms"] <= last[key]:  # step 7, the caller's replay memory
            assert want == {"result": "drop", "why": "replay"}
            continue
        last[key] = p["ts_ms"]
        assert want == {"result": "show", "tag": p["id"], **p}


def _sealed(payload: dict, *, wk: bytes = WK1, epoch: int = 1, signer: str = "wsk_a.sig") -> bytes:
    sig = crypto.sign(priv(signer), push.push_signed_bytes(WS, epoch, payload))
    return push.seal_push(push.k_push(wk, WS, epoch), WS, epoch, payload, sig)


def _keys_a() -> dict:
    return {IDS["workspace_a"]: push.PushKeys(wsk_pub=pub("wsk_a.sig"), wk={1: WK1})}


def test_host_builder_round_trip_with_a_padded_label():
    p = push.push_payload("question", "q-0167f4bf37be9ccc", "Ship L-0042?", NOW)
    assert len(p["label"]) == 80 and p["label"].rstrip(" ") == "Ship L-0042?"
    raw = _sealed(p)
    assert len(raw) <= push.MAX_PUSH
    assert push.open_push(raw, _keys_a(), NOW) == p
    a, b = _sealed(p), _sealed(p)
    assert json.loads(a)["c"][:22] != json.loads(b)["c"][:22]  # a fresh salt per seal


def test_padding_rule():
    assert push.pad_label("Answered") == "Answered" + " " * 72
    assert push.pad_label("x" * 80) == "x" * 80
    assert push.pad_label("é" * 80) == "é" * 80  # code points, not bytes
    for bad in ("x" * 81, "", "trailing ", " ", "a\u0000b", "a​b", "\ud800", 5, None):
        with pytest.raises(push.PushError):
            push.pad_label(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kw",
    [
        dict(kind="join"),
        dict(kind="peer.ticket"),
        dict(kind="exec"),
        dict(id=""),
        dict(id="x" * 129),
        dict(id="q-é"),
        dict(id=5),
        dict(ts_ms=-1),
        dict(ts_ms=True),
        dict(ts_ms=2**53),
        dict(ts_ms=1.5),
    ],
)
def test_host_builder_refuses_bad_payloads(kw):
    args = dict(kind="question", id="q-1", label="Ship it?", ts_ms=NOW)
    args.update(kw)
    with pytest.raises(push.PushError):
        push.push_payload(**args)


def test_seal_push_refuses_an_unpadded_or_extra_field_payload_and_a_bad_signature():
    p = push.push_payload("question", "q-1", "Ship it?", NOW)
    sig = bytes(64)
    k = push.k_push(WK1, WS, 1)
    for bad in (
        {**p, "label": "Ship it?"},  # not padded
        {**p, "url": "https://x"},
        {k_: v for k_, v in p.items() if k_ != "ts_ms"},
        {**p, "kind": "join"},
    ):
        with pytest.raises(push.PushError):
            push.seal_push(k, WS, 1, bad, sig)
    for bad_sig in (b"", bytes(63), bytes(65), "x" * 64):
        with pytest.raises(push.PushError):
            push.seal_push(k, WS, 1, p, bad_sig)  # type: ignore[arg-type]
    for bad_ws, bad_e in ((bytes(15), 1), (WS, -1), (WS, 2**32), (WS, True)):
        with pytest.raises(push.PushError):
            push.seal_push(k, bad_ws, bad_e, p, sig)


def test_outer_is_at_most_3072_bytes():
    # 80 four-byte code points: the longest label; a 128-byte id. Still within the cap.
    p = push.push_payload("question.closed", "q" * 128, "\U0001f600" * 80, NOW)
    raw = _sealed(p)
    assert len(raw) <= push.MAX_PUSH
    assert push.open_push(raw, _keys_a(), NOW) == p


def test_opener_hostile_inputs():
    p = push.push_payload("drop.new", "72ac37c7000839d72774be83101c013f", "New file", NOW)
    raw = _sealed(p)
    keys = _keys_a()

    def why(r: bytes, k: dict = keys, now: int = NOW) -> str:
        with pytest.raises(push.PushError) as e:
            push.open_push(r, k, now)
        return e.value.why

    outer = json.loads(raw)
    assert why(raw[:-1]) == "shape"  # truncated
    assert why(raw + b" " * (push.MAX_PUSH - len(raw) + 1)) == "size"  # oversized
    assert why(b"\xff") == "shape"
    assert why(canon.dumps({**outer, "v": 1})) == "shape"
    assert why(canon.dumps({**outer, "x": 1})) == "shape"
    assert why(canon.dumps({**outer, "ws": outer["ws"].upper()})) == "shape"
    assert why(canon.dumps({**outer, "epoch": 2})) == "no_key"  # wrong epoch: not held
    assert why(raw, {}) == "no_key"
    k2 = {IDS["workspace_a"]: push.PushKeys(wsk_pub=pub("wsk_a.sig"), wk={1: WK1, 2: WK1})}
    assert why(canon.dumps({**outer, "epoch": 2}), k2) == "tag"  # the epoch is bound by the key and the AAD
    c = unb64u(outer["c"])
    assert why(canon.dumps({**outer, "c": b64u(c[:-1])})) == "tag"
    assert why(canon.dumps({**outer, "c": b64u(c[:20])})) == "tag"
    assert why(canon.dumps({**outer, "c": b64u(c[:-1] + bytes([c[-1] ^ 1]))})) == "tag"
    wrong = {IDS["workspace_a"]: push.PushKeys(wsk_pub=pub("phone.sig"), wk={1: WK1})}
    assert why(raw, wrong) == "signature"
    assert why(_sealed(p, signer="phone.sig")) == "signature"  # a member device holds K_push but not WSK
    assert why(raw, keys, NOW + push.MAX_AGE_MS + 1) == "stale"
    assert why(raw, keys, NOW - push.SKEW_MS - 1) == "stale"
    assert push.open_push(raw, keys, NOW + push.MAX_AGE_MS) == p


def test_opener_refuses_a_wrong_label_domain():
    """A payload signed under another label (``sig_decision``) or sealed under another message label fails."""
    p = push.push_payload("question", "q-1", "Ship it?", NOW)
    bad_bytes = crypto.L["sig_decision"] + push.push_signed_bytes(WS, 1, p)[len(crypto.L["sig_push"]) :]
    sig = crypto.sign(priv("wsk_a.sig"), bad_bytes)
    raw = push.seal_push(push.k_push(WK1, WS, 1), WS, 1, p, sig)
    with pytest.raises(push.PushError) as e:
        push.open_push(raw, _keys_a(), NOW)
    assert e.value.why == "signature"
    good_sig = crypto.sign(priv("wsk_a.sig"), push.push_signed_bytes(WS, 1, p))
    pt = canon.dumps({"p": p, "sig": b64u(good_sig)})
    c = crypto.salted_seal(push.k_push(WK1, WS, 1), crypto.L["kdf_bridge_msg"], push.push_aad(WS, 1), pt)
    raw2 = canon.dumps({"v": 2, "ws": WS.hex(), "epoch": 1, "c": b64u(c)})
    with pytest.raises(push.PushError) as e:
        push.open_push(raw2, _keys_a(), NOW)
    assert e.value.why == "tag"


def test_a_badly_encoded_signature_is_a_signature_failure():
    """Review F6: an inner ``sig`` that is not 64 bytes of canonical b64u is reported as ``signature``."""
    p = push.push_payload("question", "q-1", "Ship it?", NOW)
    for bad in ("", "AAAA", b64u(bytes(63)), b64u(bytes(64)) + "=", 5):
        pt = canon.dumps({"p": p, "sig": bad})
        c = crypto.salted_seal(push.k_push(WK1, WS, 1), crypto.L["kdf_push_msg"], push.push_aad(WS, 1), pt)
        raw = canon.dumps({"v": 2, "ws": WS.hex(), "epoch": 1, "c": b64u(c)})
        with pytest.raises(push.PushError) as e:
            push.open_push(raw, _keys_a(), NOW)
        assert e.value.why == "signature", bad
