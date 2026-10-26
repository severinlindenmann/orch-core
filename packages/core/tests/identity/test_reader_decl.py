"""H12d-1: ``orch.identity.reader.verify_decl`` (orch-v2-host §0.8.6 item 9.3), written from the spec.

The relay owns the ``reader_decl`` signature vectors (9.12); ``test_relay_reader_decl_vectors`` consumes them once the
relay corpus that carries them is vendored (TODO, after orch-core #442). Everything here is core's own reading of 9.3,
with the relay reference's reason names (``shape``, ``signature``, ``workspace``, ``device``, ``keys``, ``who``,
``bind``, ``kat_missing``, ``kat_mismatch``) plus core's ``time`` (the per-``via`` windows the relay does not model).
"""

from __future__ import annotations

import hashlib

import pytest

from orch import canon, crypto
from orch.identity import reader
from orch.identity.errors import Refused

from ..crypto.vectors import S2

NOW = 1_790_000_000_000
WS = "705d40abbb8c1c90354a1acaa94c935c"
OFFER = "a078a2b2aaadd8b9936f0260104de707"
ATTEMPT = "b9d179923176ba29616b3a03e527091b"
DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
DAY = 86_400_000


def _key(name: str):
    d = int.from_bytes(hashlib.sha256(b"h12d1 test key|" + name.encode()).digest(), "big") % (crypto.P256_N - 1) + 1
    k = crypto.private_key_from_scalar(d)
    return k, crypto.public_bytes(k)


PHONE, PHONE_PUB = _key("phone sig")
_, PHONE_KX = _key("phone kx")
INTRUDER, INTRUDER_PUB = _key("intruder sig")
_, PERSON_PUB = _key("person")
PK_PIN = crypto.b64u(crypto.pk_pin(PERSON_PUB))
PERSON_ID = crypto.person_id(PERSON_PUB).hex()
EXPECTED = {"1": {"log": DIGEST, "view": DIGEST}}


def _o(**changes):
    o = {
        "v": 1,
        "kind": "reader_decl",
        "suite": 2,
        "workspace_id": WS,
        "device_id": crypto.device_id(PHONE_PUB).hex(),
        "dk_hash": crypto.b64u(crypto.dk_hash(PHONE_PUB, PHONE_KX)),
        "who": {"pk_pin": PK_PIN},
        "reader": 1,
        "kat": {"1": {"view": DIGEST}},
        "bind": {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
        "created_ms": NOW,
    }
    o.update(changes)
    return o


def _signed(o=None, key=PHONE, label=reader.LABEL):
    o = _o() if o is None else o
    try:
        msg = label + canon.cj_checked(o)
    except canon.HashError:  # not canonical JSON (a float): nothing can sign it, any signature will do
        return {"o": o, "sig": "A" * 86}
    return {"o": o, "sig": crypto.b64u(crypto.sign(key, msg))}


CTX = {
    "workspace_id": WS,
    "dk_sig_pub": PHONE_PUB,
    "dk_kx_pub": PHONE_KX,
    "who": {"pk_pin": PK_PIN},
    "bind": {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT},
    "surfaces": ("view",),
    "now_ms": NOW,
    "expected": EXPECTED,
}


def verify(decl, **changes):
    return reader.verify_decl(decl, **{**CTX, **changes})


def refused(decl, reason, **changes):
    with pytest.raises(reader.DeclInvalid) as exc:
        verify(decl, **changes)
    assert exc.value.code == "reader.decl_invalid"
    assert exc.value.reason == reason, exc.value
    assert isinstance(exc.value, Refused)
    return exc.value


def test_dk_hash_is_the_protocol_formula():
    from ..crypto.test_pairing_binding import dk_hash

    assert crypto.b64u(crypto.dk_hash(PHONE_PUB, PHONE_KX)) == dk_hash(2, PHONE_PUB, PHONE_KX)
    with pytest.raises(crypto.CryptoError):
        crypto.dk_hash(PHONE_PUB, b"\x04" * 65)


# --- accepted -----------------------------------------------------------------------------------------------------


def test_a_valid_pairing_declaration():
    d = _signed()
    got = verify(d)
    assert got == reader.Declared(
        device_id=crypto.device_id(PHONE_PUB).hex(),
        reader=1,
        level=1,
        digest="sha256:" + hashlib.sha256(canon.cj_checked(d)).hexdigest(),
    )
    assert reader.decl_digest(d) == got.digest


def test_reader_0_is_valid_without_kat():
    got = verify(_signed(_o(reader=0, kat={})))
    assert (got.reader, got.level) == (0, 0)


def test_a_higher_unknown_level_counts_as_the_highest_known_with_kat_1_only():
    got = verify(_signed(_o(reader=7, kat={"1": {"view": DIGEST}, "7": {"surface_of_level_7": "anything"}})))
    assert (got.reader, got.level) == (7, 1)


def test_every_required_surface_is_checked():
    d = _signed(_o(kat={"1": {"view": DIGEST, "log": DIGEST}}))
    assert verify(d, surfaces=("log", "view")).level == 1
    refused(_signed(), "kat_missing", surfaces=("log",))
    refused(_signed(_o(kat={"1": {"log": OTHER, "view": DIGEST}})), "kat_mismatch", surfaces=("log", "view"))


def test_a_person_id_binding_and_an_enrol_declaration():
    who = {"person_id": PERSON_ID}
    d = _signed(_o(who=who, bind={"via": "enrol"}, created_ms=NOW - DAY))
    assert verify(d, who=who, bind={"via": "enrol"}).level == 1


# --- refused ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "changes", "reason"),
    [
        ("wrong_workspace", {"workspace_id": "282193f85af5b8d07610fbb3a8448857"}, "workspace"),
        ("wrong_device", {"device_id": crypto.device_id(INTRUDER_PUB).hex()}, "device"),
        ("dk_hash_of_another_key", {"dk_hash": crypto.b64u(crypto.dk_hash(PHONE_PUB, INTRUDER_PUB))}, "keys"),
        ("other_offer", {"bind": {"via": "pair", "offer_id": "77" * 16, "attempt": ATTEMPT}}, "bind"),
        ("other_attempt", {"bind": {"via": "pair", "offer_id": OFFER, "attempt": "4a" * 16}}, "bind"),
        ("other_via", {"bind": {"via": "declare"}}, "bind"),
        ("wrong_pk_pin", {"who": {"pk_pin": crypto.b64u(crypto.pk_pin(INTRUDER_PUB))}}, "who"),
        ("person_id_where_the_pairing_names_pk_pin", {"who": {"person_id": PERSON_ID}}, "who"),
        ("reader_1_without_kat_1", {"kat": {}}, "kat_missing"),
        ("kat_1_without_the_required_surface", {"kat": {"1": {"log": DIGEST}}}, "kat_missing"),
        ("wrong_kat_1_digest", {"kat": {"1": {"view": OTHER}}}, "kat_mismatch"),
        ("kat_1_not_a_digest", {"kat": {"1": {"view": "sha256:" + "G" * 64}}}, "shape"),
        ("kat_1_upper_case_digest", {"kat": {"1": {"view": "sha256:" + "AB" * 32}}}, "shape"),
        ("kat_1_unknown_surface", {"kat": {"1": {"view": DIGEST, "screen": DIGEST}}}, "shape"),
        ("kat_1_empty", {"kat": {"1": {}}}, "shape"),
        ("kat_level_key_with_a_leading_zero", {"kat": {"01": {"view": DIGEST}}}, "shape"),
        ("kat_level_key_zero", {"kat": {"0": {"view": DIGEST}, "1": {"view": DIGEST}}}, "shape"),
        ("kat_level_above_reader", {"kat": {"1": {"view": DIGEST}, "2": {"view": DIGEST}}}, "shape"),
        ("kat_not_an_object", {"kat": []}, "shape"),
        ("unknown_field", {"extra": True}, "shape"),
        ("version_2", {"v": 2}, "shape"),
        ("version_true", {"v": True}, "shape"),
        ("other_kind", {"kind": "reader"}, "shape"),
        ("other_suite", {"suite": 1}, "shape"),
        ("reader_negative", {"reader": -1}, "shape"),
        ("reader_above_2_31_minus_1", {"reader": 2**31}, "shape"),
        ("reader_boolean", {"reader": True}, "shape"),
        ("reader_float", {"reader": 1.0}, "shape"),
        ("created_ms_negative", {"created_ms": -1}, "shape"),
        ("workspace_upper_hex", {"workspace_id": WS.upper()}, "shape"),
        ("device_id_prefixed", {"device_id": "d_" + crypto.device_id(PHONE_PUB).hex()}, "shape"),
        ("dk_hash_short", {"dk_hash": crypto.b64u(bytes(31))}, "shape"),
        ("who_two_keys", {"who": {"pk_pin": PK_PIN, "person_id": PERSON_ID}}, "shape"),
        ("who_empty", {"who": {}}, "shape"),
        ("who_prefixed_person", {"who": {"person_id": "p_" + PERSON_ID}}, "shape"),
        ("bind_unknown_via", {"bind": {"via": "relay"}}, "shape"),
        (
            "bind_pair_with_an_extra_field",
            {"bind": {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT, "rid": "0" * 32}},
            "shape",
        ),
        ("bind_pair_without_attempt", {"bind": {"via": "pair", "offer_id": OFFER}}, "shape"),
        ("bind_enrol_with_a_field", {"bind": {"via": "enrol", "offer_id": OFFER}}, "shape"),
        ("over_2_kib", {"kat": {"1": {"view": DIGEST}, "9": {"pad": "x" * 2048}}, "reader": 9}, "shape"),
    ],
)
def test_refused(name, changes, reason):
    refused(_signed(_o(**changes)), reason)


def test_signature_refusals():
    o = _o()
    refused(_signed(o, key=INTRUDER), "signature")
    refused(_signed(o, label=b"orch/v2/sig/relay-auth|"), "signature")
    refused({"o": {**o, "reader": 2, "kat": {"1": {"view": DIGEST}, "2": {"view": DIGEST}}}, "sig": _signed(o)["sig"]},
            "signature")  # fmt: skip
    refused({"o": o, "sig": "A" * 85}, "shape")
    refused({"o": o, "sig": crypto.b64u(bytes(63))}, "shape")


@pytest.mark.parametrize(
    "decl",
    [None, [], "x", {"o": _o()}, {"o": _o(), "sig": "x", "rd": "x"}, {"o": [], "sig": "x"}, {"o": None, "sig": "x"}],
)
def test_not_a_signed_object(decl):
    refused(decl, "shape")


def test_the_cap_is_2_kib_of_the_whole_signed_object():
    assert reader.MAX_BYTES == 2048
    pad = 0
    while True:  # grow a padding field of an ignored higher level up to the cap exactly
        o = _o(reader=9, kat={"1": {"view": DIGEST}, "9": {"pad": "x" * pad}})
        size = len(canon.cj_checked(_signed(o)))
        if size >= 2048:
            break
        pad += 1
    assert size == 2048  # a signature is 86 b64u characters whatever its value
    assert verify(_signed(o)).level == 1
    o["kat"]["9"]["pad"] += "x"
    refused(_signed(o), "shape")


# --- freshness (9.3: checked once, at admission) ------------------------------------------------------------------


def test_pairing_has_no_clock_window_but_no_far_future():
    assert verify(_signed(_o(created_ms=NOW - 365 * DAY))).level == 1
    assert verify(_signed(_o(created_ms=NOW + 300_000))).level == 1
    e = refused(_signed(_o(created_ms=NOW + 300_001)), "time")
    assert str(NOW) in str(e)  # a refusal states the host's time


def test_enrol_window_is_300_s_ahead_and_30_days_old():
    ctx = {"bind": {"via": "enrol"}}
    ok = (NOW + 300_000, NOW - 30 * DAY)
    bad = (NOW + 300_001, NOW - 30 * DAY - 1)
    for t in ok:
        assert verify(_signed(_o(bind={"via": "enrol"}, created_ms=t)), **ctx).level == 1
    for t in bad:
        refused(_signed(_o(bind={"via": "enrol"}, created_ms=t)), "time", **ctx)


def test_declare_window_is_plus_minus_300_s():
    ctx = {"bind": {"via": "declare"}}
    for t in (NOW - 300_000, NOW + 300_000):
        assert verify(_signed(_o(bind={"via": "declare"}, created_ms=t)), **ctx).level == 1
    for t in (NOW - 300_001, NOW + 300_001):
        refused(_signed(_o(bind={"via": "declare"}, created_ms=t)), "time", **ctx)


def test_a_recheck_never_reads_the_clock():
    d = _signed(_o(bind={"via": "declare"}, created_ms=NOW - 7 * DAY))
    refused(d, "time", bind={"via": "declare"})
    assert verify(d, bind={"via": "declare"}, now_ms=None, recheck=True).level == 1
    refused(_signed(_o(kat={"1": {"view": OTHER}})), "kat_mismatch", now_ms=None, recheck=True)


def test_dropping_freshness_needs_an_explicit_recheck():
    """#463 review L2: now_ms=None alone is a caller bug, never a silent skip of the time window."""
    d = _signed(_o(bind={"via": "declare"}, created_ms=NOW - 7 * DAY))
    with pytest.raises(ValueError, match="recheck"):
        verify(d, bind={"via": "declare"}, now_ms=None)
    with pytest.raises(ValueError, match="recheck"):
        verify(d, bind={"via": "declare"}, recheck=True)  # a recheck reads no clock: now_ms must be None
    with pytest.raises(ValueError):
        verify(d, bind={"via": "declare"}, now_ms=None, recheck=1)
    refused(d, "time", bind={"via": "declare"}, recheck=False)


# --- the context ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"surfaces": ()}, {"surfaces": ("screen",)}, {"dk_sig_pub": b"\x04" * 65}, {"workspace_id": "x"}],
)
def test_a_bad_context_is_a_programming_error(changes):
    with pytest.raises(ValueError):
        verify(_signed(), **changes)


# --- the relay-owned vectors (9.12) --------------------------------------------------------------------------------


def _relay_cases():
    section = S2.get("reader_decl")
    if not section:
        return []
    gates = [{"name": "pair_meta/" + m["name"], "decl": m["steps"][0]["expect"]["reader_decl"], **m["gate"]}
             for m in section["pair_meta"] if "gate" in m]  # fmt: skip
    return section["decls"] + gates


def _relay_ctx(c, **changes):
    return {
        "workspace_id": c["workspace_id"],
        "dk_sig_pub": crypto.unb64u(c["dk_sig_pub"]),
        "dk_kx_pub": crypto.unb64u(c["dk_kx_pub"]),
        "who": c["who"],
        "bind": c["bind"],
        "surfaces": tuple(c["surfaces"]),
        "host_max": c["host_max"],
        # the relay models no time window (orch_protocol_ref.verify_reader_decl): its cases are judged as a recheck,
        # and core's admission windows are pinned by core's own tests above
        "now_ms": None,
        "recheck": True,
        "expected": c.get("kat") or reader.KAT_DIGESTS,
        **changes,
    }


def test_the_relay_corpus_carries_cores_kat_digests():
    if "reader_decl" not in S2:
        pytest.skip("no reader_decl section in the vendored corpus")
    section = S2["reader_decl"]
    assert section["label"] == reader.LABEL.decode() and section["max_bytes"] == reader.MAX_BYTES
    assert section["host_max"] == reader.HOST_MAX
    assert {s: section["kat_core"][s] for s in reader.SURFACES} == dict(reader.KAT_DIGESTS["1"])


@pytest.mark.skipif("reader_decl" not in S2, reason="no reader_decl section in the vendored corpus")
@pytest.mark.parametrize("case", _relay_cases(), ids=lambda c: c["name"])
def test_relay_reader_decl_vectors(case):
    """Every relay `reader_decl` case (and the gate of each pair-meta case) gives the relay's verdict in core."""
    ctx = _relay_ctx(case["ctx"])
    if "ok" in case["expect"]:
        got = reader.verify_decl(case["decl"], **ctx)
        assert got._asdict() == case["expect"]["ok"]
    else:
        with pytest.raises(reader.DeclInvalid) as exc:
            reader.verify_decl(case["decl"], **ctx)
        assert (exc.value.code, exc.value.reason) == (case["expect"]["refuse"], case["expect"]["reason"])


def test_a_far_future_relay_case_is_refused_at_admission():
    """`created_ms_2_53_minus_1_accepted` is accepted by form (the relay models no clock); at admission core refuses
    any created_ms more than 300 s ahead, for every via (9.3 "Latest wins"; H12d-1 judgement call 4)."""
    cases = {c["name"]: c for c in _relay_cases()}
    if "created_ms_2_53_minus_1_accepted" not in cases:
        pytest.skip("no such relay case")
    c = cases["created_ms_2_53_minus_1_accepted"]
    with pytest.raises(reader.DeclInvalid) as exc:
        reader.verify_decl(c["decl"], **_relay_ctx(c["ctx"], now_ms=NOW, recheck=False))
    assert exc.value.reason == "time"
