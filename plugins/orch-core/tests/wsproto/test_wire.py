"""``orch.wsproto.wire``: origin, ``auth_bytes`` (byte-equal to orch-relay ``proto.auth_bytes``), strict capped JSON
bodies, base64 size checks, ORB2 header/envelope codec and body sealing, against the vendored vectors."""

from __future__ import annotations

import json

import pytest

from orch import canon, crypto
from orch.wsproto import wire

from . import corpus

RA = corpus.S2["relay_auth"]
WS = corpus.IDS["workspace_a"]


# --- origin ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://relay.dev.severin.io",
        "https://relay.example:8443",
        "https://a",
        "http://127.0.0.1",
        "http://127.0.0.1:8101",
        "http://localhost:65535",
    ],
)
def test_origin_accepted_verbatim(origin):
    assert wire.check_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://relay.dev.severin.io/",  # trailing slash
        "https://relay.dev.severin.io/v2",  # path
        "HTTPS://relay.dev.severin.io",  # upper-case scheme
        "https://Relay.dev.severin.io",  # upper-case host
        "http://relay.dev.severin.io",  # plain http to a non-loopback host
        "http://[::1]",  # 11A.2: only 127.0.0.1 and localhost over http
        "http://127.0.0.2",
        "https://relay.example:0",
        "https://relay.example:65536",
        "https://relay.example:08443",  # leading zero
        "https://relay.example:",
        "https://user@relay.example",
        "https://relay.example?x=1",
        "https://relay.example#f",
        "https://-relay.example",
        "https://relay.example.",
        "https://relä.example",  # non-ASCII
        "ftp://relay.example",
        "https://",
        "",
        " https://relay.example",
        "https://relay.example\n",
        None,
        b"https://relay.example",
    ],
)
def test_origin_refused(origin):
    with pytest.raises(wire.WireError):
        wire.check_origin(origin)


def test_origin_parts():
    assert wire.origin_parts("https://relay.example") == ("https", "relay.example", 443)
    assert wire.origin_parts("https://relay.example:8443") == ("https", "relay.example", 8443)
    assert wire.origin_parts("http://127.0.0.1") == ("http", "127.0.0.1", 80)


# --- auth_bytes (proto §12.2, relay_auth vectors) -------------------------------------------------------------------


def test_auth_bytes_equals_the_vector_signed_bytes():
    got = wire.auth_bytes(RA["origin"], bytes.fromhex(RA["challenge"]), RA["kind"], RA["id"])
    assert got == bytes.fromhex(RA["signed_bytes"])
    assert crypto.verify(corpus.pub("phone.sig"), bytes.fromhex(RA["sig"]), got)


def test_auth_bytes_layout_for_a_workspace():
    ch = bytes(range(32))
    origin = "https://relay.example:8443"
    got = wire.auth_bytes(origin, ch, "workspace", WS)
    o = origin.encode()
    lp16 = len(o).to_bytes(2, "big") + o
    assert got == b"orch/v2/sig/relay-auth|" + b"\x02" + lp16 + ch + b"\x02" + bytes.fromhex(WS)


def _step_verifies(case, step, origin, kind=None):
    try:
        msg = wire.auth_bytes(origin, bytes.fromhex(case["challenge"]), kind or step["kind"], step["id"])
    except wire.WireError:
        return None
    return crypto.verify(bytes.fromhex(step["pub"]), bytes.fromhex(step["sig"]), msg)


@pytest.mark.parametrize("case", RA["cases"], ids=[c["name"] for c in RA["cases"]])
def test_relay_auth_cases_signature_layer(case):
    """The relay-side state of each case (consumed or expired challenge) is the relay's; here we pin what the bytes
    decide: the signature check with the relay's own origin."""
    step = case["steps"][0]
    ok = _step_verifies(case, step, case["origin"])
    name = case["name"]
    if name in ("valid_then_challenge_reused", "expired_challenge"):
        assert ok is True  # refused by challenge state, not by the bytes
    elif name == "unknown_kind":
        assert ok is None  # auth_bytes refuses the kind (relay: 400 malformed)
    else:
        assert ok is False, name
    if name == "signed_as_a_workspace_presented_as_a_device":
        assert _step_verifies(case, step, case["origin"], kind="workspace") is True  # kind byte 2 is bound


def test_auth_bytes_origin_binding_is_exact():
    ch = bytes(32)
    base = wire.auth_bytes("https://relay.example", ch, "workspace", WS)
    for other in ("https://relay.example:443", "https://relay.example2", "http://localhost"):
        assert wire.auth_bytes(other, ch, "workspace", WS) != base


@pytest.mark.parametrize(
    "args",
    [
        ("https://relay.example", bytes(31), "workspace", WS),
        ("https://relay.example", bytes(33), "workspace", WS),
        ("https://relay.example", "00" * 32, "workspace", WS),
        ("https://relay.example", bytes(32), "agent", WS),
        ("https://relay.example", bytes(32), "workspace", WS.upper()),
        ("https://relay.example", bytes(32), "workspace", WS[:-2]),
        ("https://relay.example/", bytes(32), "workspace", WS),
    ],
)
def test_auth_bytes_refuses(args):
    with pytest.raises(wire.WireError):
        wire.auth_bytes(*args)


# --- strict JSON bodies ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", corpus.VECTORS["encodings"]["strict_parse"], ids=lambda c: c["name"])
def test_strict_parse_vectors_under_the_canonical_subset(case):
    """Signed/sealed/meta JSON uses the protocol subset (depth 16): ``loads_canonical``."""
    if case["ok"]:
        got = wire.loads_canonical(case["text"].encode())
        if "canonical" in case:
            assert canon.dumps(got) == case["canonical"].encode()
    else:
        with pytest.raises(wire.WireError):
            wire.loads_canonical(case["text"].encode())


@pytest.mark.parametrize("case", corpus.VECTORS["encodings"]["strict_parse"], ids=lambda c: c["name"])
def test_strict_parse_vectors_under_the_body_codec(case):
    """HTTP bodies: the same rules, depth <= 32 (11A.7); depth 17 is allowed there."""
    text = case["text"].encode()
    if case["ok"] or case["name"].startswith("depth_17"):
        wire.loads_body(text, cap=1024)
    else:
        with pytest.raises(wire.WireError):
            wire.loads_body(text, cap=1024)


def test_body_depth_32_accepted_33_refused():
    wire.loads_body(b"[" * 32 + b"]" * 32, cap=1024)
    with pytest.raises(wire.WireError) as e:
        wire.loads_body(b"[" * 33 + b"]" * 33, cap=1024)
    assert e.value.code == "malformed"


def test_body_depth_bomb_refused_before_parsing():
    bomb = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(wire.WireError):
        wire.loads_body(bomb, cap=1 << 20)


def test_brackets_inside_strings_do_not_count_toward_depth():
    text = json.dumps({"a": "[" * 100 + "{" * 100 + '\\"' + "]"}).encode()
    assert wire.loads_body(text, cap=4096)["a"].startswith("[[[")


def test_body_cap_is_checked_before_parsing():
    wire.loads_body(b"{}" + b" " * 98, cap=100)
    with pytest.raises(wire.WireError) as e:
        wire.loads_body(b"{}" + b" " * 99, cap=100)
    assert e.value.code == "too_large"


@pytest.mark.parametrize(
    "text",
    [b"\xff{}", b"\xef\xbb\xbf{}", b'{"a":1}x', b"", b"{", b'{"a":"\\ud800"}', b'{"":1}', b'{"\xc3\xa4":1}'],
)
def test_body_refusals(text):
    with pytest.raises(wire.WireError):
        wire.loads_body(text, cap=1024)


def test_loads_object_exact_keys():
    assert wire.loads_object(b'{"a":1,"b":2}', cap=64, keys={"a", "b"}) == {"a": 1, "b": 2}
    for bad in (b'{"a":1}', b'{"a":1,"b":2,"c":3}', b"[1]"):
        with pytest.raises(wire.WireError):
            wire.loads_object(bad, cap=64, keys={"a", "b"})


def test_dumps_body_is_cj_and_capped():
    assert wire.dumps_body({"b": 1, "a": [True, None]}, cap=64) == b'{"a":[true,null],"b":1}'
    with pytest.raises(wire.WireError) as e:
        wire.dumps_body({"a": "x" * 100}, cap=64)
    assert e.value.code == "too_large"


def test_wire_errors_never_echo_input():
    secret = "SECRET-TOKEN-abcdef"
    for fn in (
        lambda: wire.loads_body(('{"t":"' + secret + '"').encode(), cap=1024),
        lambda: wire.check_origin("https://" + secret + "/x"),
        lambda: wire.b64_field(secret + "=", max_len=64),
    ):
        with pytest.raises(wire.WireError) as e:
            fn()
        assert secret not in str(e.value) and secret not in repr(e.value)


# --- base64 fields ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", corpus.VECTORS["encodings"]["b64u"], ids=lambda c: c["name"])
def test_b64u_vectors(case):
    if case["bytes"] is None:
        with pytest.raises(wire.WireError):
            wire.b64_field(case["text"], max_len=16)
    else:
        assert wire.b64_field(case["text"], max_len=16) == bytes.fromhex(case["bytes"])


def test_b64_field_size_checked_before_decoding():
    ok = crypto.b64u(bytes(48))
    assert wire.b64_field(ok, max_len=48) == bytes(48)
    with pytest.raises(wire.WireError) as e:
        wire.b64_field(crypto.b64u(bytes(49)), max_len=48)
    assert e.value.code == "too_large"
    with pytest.raises(wire.WireError):
        wire.b64_field("A" * 10_000_001, max_len=48)  # refused by length alone
    with pytest.raises(wire.WireError):
        wire.b64_field(crypto.b64u(bytes(32)), max_len=64, exact=31)
    with pytest.raises(wire.WireError):
        wire.b64_field(12, max_len=64)


def test_ct_equal():
    assert wire.ct_equal(b"abc", b"abc")
    assert not wire.ct_equal(b"abc", b"abd")
    assert not wire.ct_equal(b"abc", b"ab")


# --- ORB2 -----------------------------------------------------------------------------------------------------------

SEAL = corpus.S2["bridge"]["seal"][0]
HKDF = {h["name"]: h for h in corpus.S2["hkdf"]}


def test_header_round_trip_against_the_seal_vector():
    raw = bytes.fromhex(SEAL["header"])
    h = wire.parse_header(raw)
    f = SEAL["header_fields"]
    assert (h.version, h.direction, h.flags, h.suite) == (f["version"], f["direction"], f["flags"], f["suite"])
    assert h.workspace.hex() == f["workspace"] and h.device.hex() == f["device"] and h.rid.hex() == f["rid"]
    assert h.stream.hex() == f["stream"] and h.salt.hex() == f["salt"]
    assert (h.seq, h.ts_ms, h.epoch) == (f["seq"], f["ts_ms"], f["epoch"])
    assert h.pack() == raw and len(raw) == wire.HEADER_LEN == 108


@pytest.mark.parametrize("name", ["k_bridge_epoch_1", "k_bridge_epoch_2"])
def test_k_bridge_vectors(name):
    v = HKDF[name]
    epoch = int(v["info_text"].rsplit("|", 1)[1])
    assert wire.k_bridge(bytes.fromhex(v["ikm"]), bytes.fromhex(WS), epoch) == bytes.fromhex(v["okm"])


def test_k_pair_and_k_msg_vectors():
    v = HKDF["k_pair"]
    offer = bytes.fromhex(v["info_text"].rsplit("|", 1)[1])
    assert wire.k_pair(bytes.fromhex(v["ikm"]), bytes.fromhex(WS), offer) == bytes.fromhex(v["okm"])
    m = HKDF["k_msg_bridge"]
    assert wire.k_msg(bytes.fromhex(m["ikm"]), bytes.fromhex(m["salt"])) == bytes.fromhex(m["okm"])


def test_k_bridge_refuses_epoch_0_and_out_of_range():
    for e in (0, -1, 2**32):
        with pytest.raises(wire.WireError):
            wire.k_bridge(bytes(32), bytes.fromhex(WS), e)


def test_seal_vector_body_and_envelope():
    header = bytes.fromhex(SEAL["header"])
    k = bytes.fromhex(SEAL["k_bridge"])
    assert wire.k_msg(k, wire.parse_header(header).salt) == bytes.fromhex(SEAL["message_key"])
    pt = bytes.fromhex(SEAL["plaintext"])
    body = wire.seal_body(k, header, pt)
    assert body == bytes.fromhex(SEAL["sealed"])
    assert wire.open_body(k, header, body) == pt
    env = bytes.fromhex(SEAL["envelope"])
    h, b, sig = wire.parse_envelope(env, direction=1)
    assert h.pack() == header and b == body
    assert wire.build_envelope(header, body, sig) == env
    assert crypto.verify(corpus.pub("phone.sig"), sig, wire.bridge_signed_bytes(header, body))
    meta, data = wire.parse_plaintext(pt)
    assert meta["op"] == "http" and data == b""


def test_open_body_refuses_tampering():
    header = bytes.fromhex(SEAL["header"])
    k = bytes.fromhex(SEAL["k_bridge"])
    body = bytes.fromhex(SEAL["sealed"])
    for bad_h, bad_b in (
        (header[:-1] + bytes([header[-1] ^ 1]), body),
        (header, body[:-1] + bytes([body[-1] ^ 1])),
    ):
        with pytest.raises(wire.WireError):
            wire.open_body(k, bad_h, bad_b)
    with pytest.raises(wire.WireError):
        wire.open_body(bytes(32), header, body)


def test_plaintext_round_trip_and_meta_cap():
    pt = wire.build_plaintext({"op": "x", "b": 1}, b"data")
    assert pt[:4] == len(b'{"b":1,"op":"x"}').to_bytes(4, "big")
    assert wire.parse_plaintext(pt) == ({"op": "x", "b": 1}, b"data")
    with pytest.raises(wire.WireError):
        wire.build_plaintext({"x": "a" * 65_536}, b"")
    with pytest.raises(wire.WireError):
        wire.parse_plaintext((70_000).to_bytes(4, "big") + b"{}")
    with pytest.raises(wire.WireError):
        wire.parse_plaintext(b"\x00\x00")
    with pytest.raises(wire.WireError):
        wire.parse_plaintext((10).to_bytes(4, "big") + b"{}")  # meta_len past the end
    with pytest.raises(wire.WireError):
        wire.parse_plaintext((7).to_bytes(4, "big") + b'{"a":1.0}')


def _hdr(**kw):
    base = dict(
        version=2,
        direction=2,
        flags=1,
        suite=2,
        workspace=bytes.fromhex(WS),
        device=bytes(16),
        rid=bytes(range(16)),
        stream=bytes(16),
        seq=0,
        ts_ms=1,
        epoch=1,
        salt=bytes(16),
    )
    base.update(kw)
    return wire.Header(**base)


@pytest.mark.parametrize(
    "kw",
    [
        dict(version=1),
        dict(direction=3),
        dict(suite=1),
        dict(direction=2, flags=4),  # REFUSAL without LAST
        dict(direction=2, flags=8),
        dict(direction=1, flags=1),  # LAST on a request
        dict(seq=2**53),
        dict(epoch=2**32),
        dict(workspace=bytes(15)),
        dict(salt=bytes(17)),
    ],
)
def test_header_refusals(kw):
    with pytest.raises(wire.WireError):
        wire.parse_header(_hdr(**kw).pack() if _packable(kw) else b"\x00" * 108)


def _packable(kw):
    try:
        _hdr(**kw).pack()
        return True
    except (wire.WireError, ValueError, OverflowError):
        return False


def test_header_magic_and_length():
    raw = _hdr().pack()
    wire.parse_header(raw)
    with pytest.raises(wire.WireError):
        wire.parse_header(b"ORB1" + raw[4:])
    with pytest.raises(wire.WireError):
        wire.parse_header(raw[:-1])


def test_envelope_size_limits():
    header = _hdr().pack()
    body = bytes(wire.MAX_CHUNK - wire.HEADER_LEN - 64)
    env = wire.build_envelope(header, body, bytes(64))
    assert len(env) == wire.MAX_CHUNK
    wire.parse_envelope(env, direction=2)
    with pytest.raises(wire.WireError) as e:
        wire.build_envelope(header, body + b"x", bytes(64))
    assert e.value.code == "too_large"
    with pytest.raises(wire.WireError):
        wire.parse_envelope(env + b"x", direction=2)
    with pytest.raises(wire.WireError):
        wire.parse_envelope(header + bytes(16 + 63), direction=2)  # under the 188-byte overhead
    with pytest.raises(wire.WireError):
        wire.parse_envelope(env, direction=1)  # direction mismatch


HOST_CASES = corpus.S2["bridge"]["host_cases"]
STRUCTURAL_DROPS = {"other_suite_dropped", "old_magic_v1_dropped", "version_1_dropped"}


@pytest.mark.parametrize("case", HOST_CASES, ids=[c["name"] for c in HOST_CASES])
def test_host_case_envelopes_parse_or_drop_structurally(case):
    """The ORB2 layer of the §8.6 host cases: structural drops are refused by the codec; every other envelope parses,
    and an accepted request opens with the case's epoch key to the expected meta and data. The rest of §8.6 is R2."""
    steps = [case] if "envelope" in case else case["steps"]
    for step in steps:
        env = bytes.fromhex(step["envelope"])
        if case["name"] in STRUCTURAL_DROPS:
            with pytest.raises(wire.WireError):
                wire.parse_envelope(env, direction=1)
            continue
        h, body, _sig = wire.parse_envelope(env, direction=1)
        if step["expect"].get("result") == "accept" and h.epoch >= 1:
            wk = bytes.fromhex(case["state"]["wk"][str(h.epoch)])
            pt = wire.open_body(wire.k_bridge(wk, h.workspace, h.epoch), h.pack(), body)
            meta, data = wire.parse_plaintext(pt)
            assert meta == step["expect"]["meta"]
            assert data.hex() == step["expect"]["data"]


def test_bridge_signed_bytes_label():
    assert wire.bridge_signed_bytes(b"H", b"B") == b"orch/v2/sig/bridge|HB"


# --- signed objects -------------------------------------------------------------------------------------------------


def test_verify_signed_member_list_fixture_under_wsk():
    ml = corpus.case("members", "success")["response"]["json"]["member_list"]
    wsk = corpus.pub("wsk_a.sig")
    assert wire.verify_signed(wsk, ml, "member_list") == ml["o"]
    assert wire.verify_signed(corpus.pub("wsk_b.sig"), ml, "member_list") is None
    assert wire.verify_signed(wsk, ml, "card") is None
    assert wire.verify_signed(wsk, {**ml, "x": 1}, "member_list") is None
    assert wire.verify_signed(wsk, {"o": {**ml["o"], "epoch": 2}, "sig": ml["sig"]}, "member_list") is None


def test_b64_field_never_decodes_an_oversized_field(monkeypatch):
    calls = []
    real = crypto.unb64u
    monkeypatch.setattr(wire.crypto, "unb64u", lambda *a, **k: calls.append(1) or real(*a, **k))
    with pytest.raises(wire.WireError):
        wire.b64_field(crypto.b64u(bytes(4096)), max_len=48)
    assert calls == []
