"""H12c builders 6 and 7 for R6 (design 11A.15.10): the member list, the WK grant, the sealed keys, against the vendored
relay corpus: every matching case, accept and refuse. Ground truth is orch-relay ``docs/protocol-v2.md`` §5.3, §7.1,
§7.2, §7.3 and the corpus; where the design text differs, the vectors win (the PR lists each difference)."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.crypto import sealing, suite
from orch.identity import Refused, memberlist

from ..wsproto import corpus

S2 = corpus.S2
KEYS = S2["keys"]
IDS = S2["ids"]
WS = IDS["workspace_a"]
OWNER = IDS["person_alice"]


def _kx_priv(name: str):
    seed = int.from_bytes(bytes.fromhex(KEYS[f"{name}.kx"]["seed"]), "big")
    return crypto.private_key_from_scalar(seed % (crypto.P256_N - 1) + 1)


def _eph(seed_hex: str):
    return crypto.private_key_from_scalar(int.from_bytes(bytes.fromhex(seed_hex), "big") % (crypto.P256_N - 1) + 1)


# --- 6. the member list (protocol §7.2, corpus ``member_lists`` and ``member_list_snapshots``) ---------------------

LISTS = S2["member_lists"]
# The codes check_member_list owns (§7.2 checks 1 to 3); every other refusal needs the previous list or the directory.
OWN_CODES = {"bad_signature", "malformed", "other_workspace"}


@pytest.mark.parametrize("case", LISTS, ids=[c["name"] for c in LISTS])
def test_check_member_list_vectors(case):
    wsk = bytes.fromhex(case["wsk_pub"])
    want = case["expect"].get("refuse")
    if want in OWN_CODES:
        with pytest.raises(Refused) as e:
            memberlist.check_member_list(case["signed"], wsk, case["card_workspace_id"])
        assert e.value.code == want
        return
    got = memberlist.check_member_list(case["signed"], wsk, case["card_workspace_id"])
    o = case["signed"]["o"]
    assert got.digest == crypto.b64u(crypto.sha256(canon.dumps(o)))
    assert (got.epoch, got.list_seq, got.wk_commit, list(got.persons)) == (
        o["epoch"],
        o["list_seq"],
        o["wk_commit"],
        o["persons"],
    )
    assert [(m.device_id, m.person_id, list(m.scopes)) for m in got.members] == [
        (m["device_id"], m["person_id"], m["scopes"]) for m in o["members"]
    ]
    if "ok" in case["expect"]:
        assert case["expect"]["ok"] == o


def _rebuild(o: dict, *, first: bool) -> dict:
    return memberlist.member_list_o(
        o["workspace_id"],
        o["epoch"],
        o["list_seq"],
        o["issued_ms"],
        o["wk_commit"],
        list(reversed(o["members"])),  # the builder sorts
        owner_person_id=OWNER,
        first=first,
    )


ACCEPTED = [c for c in LISTS if "ok" in c["expect"]]


@pytest.mark.parametrize("case", ACCEPTED, ids=[c["name"] for c in ACCEPTED])
def test_member_list_o_rebuilds_every_accepted_list_byte_for_byte(case):
    """The relay accepted these bytes: the builder gives the same ``cj(o)``, so the vector's signature verifies over it.
    ``persons`` is derived (the owner is never listed: every corpus list is ``persons: []``)."""
    o = case["signed"]["o"]
    built = _rebuild(o, first=case["prev"] is None)
    assert canon.dumps(built) == canon.dumps(o)
    sig = crypto.unb64u(case["signed"]["sig"], crypto.SIG_LEN)
    assert crypto.verify(bytes.fromhex(case["wsk_pub"]), sig, crypto.L["sig_member_list"] + canon.dumps(built))


def test_first_list_above_2_16_is_refused_by_the_builder():
    """The relay's ``bad_epoch`` for a first list above 2^16 (check 4) is a builder refusal: R6 never signs it."""
    case = next(c for c in LISTS if c["name"] == "first_list_epoch_above_2_16_refused")
    with pytest.raises(Refused) as e:
        _rebuild(case["signed"]["o"], first=True)
    assert e.value.code == "malformed"
    assert _rebuild(case["signed"]["o"], first=False)["epoch"] == 65537


def test_member_list_cases_are_all_found():
    """Exact (==), from the corpus: 34 lists, 13 accepted, 8 refused by check_member_list itself, 13 by state."""
    codes = [c["expect"].get("refuse") for c in LISTS]
    assert len(LISTS) == 34
    assert len(ACCEPTED) == 13
    assert sorted(c for c in codes if c in OWN_CODES) == ["bad_signature", *["malformed"] * 6, "other_workspace"]
    assert sorted(c for c in codes if c is not None and c not in OWN_CODES) == [
        *["bad_epoch"] * 3,
        "missing_grant",
        "other_person",
        "revoked",
        *["rotation_required"] * 2,
        *["scope_exceeded"] * 2,
        "stale_list",
        *["wk_commit_changed"] * 2,
    ]


SNAPSHOT_LISTS = [
    (f"{c['name']}[{i}]", c, s)
    for c in S2["member_list_snapshots"]
    for i, s in enumerate(c["steps"])
    if s["op"] == "list"
]


@pytest.mark.parametrize(("name", "case", "step"), SNAPSHOT_LISTS, ids=[n for n, _, _ in SNAPSHOT_LISTS])
def test_check_member_list_snapshots(name, case, step):
    """Every snapshot list verifies (the D2 outcomes are the device's state machine). The digest is the device's D1
    ``list_hash``: equal to the head after a no-op or an advance."""
    got = memberlist.check_member_list(step["signed"], bytes.fromhex(case["wsk_pub"]), case["card_workspace_id"])
    if step["expect"] in ({"ok": "noop"}, {"ok": "advanced"}):
        assert got.digest == step["state"]["head"]["list_hash"]


def test_snapshot_lists_are_all_found():
    assert len(SNAPSHOT_LISTS) == sum(s["op"] == "list" for c in S2["member_list_snapshots"] for s in c["steps"]) == 10
    assert sum(s["expect"] in ({"ok": "noop"}, {"ok": "advanced"}) for _, _, s in SNAPSHOT_LISTS) == 3


_FIRST = next(c for c in LISTS if c["name"] == "first_list")["signed"]["o"]
_M = _FIRST["members"]


def _args(**kw):
    a = {
        "workspace_id": WS,
        "epoch": 1,
        "list_seq": 1,
        "issued_ms": 1,
        "wk_commit": _FIRST["wk_commit"],
        "members": _M,
        "owner_person_id": OWNER,
        "first": True,
    }
    a.update(kw)
    return a


def _member(**kw):
    m = dict(_M[0])
    m.update(kw)
    return m


@pytest.mark.parametrize(
    "kw",
    [
        {"workspace_id": WS.upper()},
        {"epoch": 0},
        {"epoch": 2**32},
        {"epoch": True},
        {"list_seq": 0},
        {"list_seq": 2**53},
        {"issued_ms": -1},
        {"issued_ms": 1.0},
        {"wk_commit": "A" * 42 + "B"},  # not canonical
        {"wk_commit": crypto.b64u(bytes(31))},
        {"members": []},  # an empty first list
        {"members": [_member(person_id=IDS["person_bob"])]},  # a first list without an owner device
        {"members": [_M[0], _M[0]]},  # a duplicate device
        {"members": [_member(scopes=["decide"])]},  # not a prefix
        {"members": [_member(scopes=[])]},
        {"members": [_member(scopes=["drop:" + "0" * 32])]},  # a scoped agent is never a member
        {"members": [{**_M[0], "label": "x"}]},
        {"members": [{k: v for k, v in _M[0].items() if k != "scopes"}]},
        {"members": _M[0]},
        {"owner_person_id": OWNER[:-2]},
        {"first": 1},
        {"epoch": 2**16 + 1},  # first list above 2^16
    ],
)
def test_member_list_o_refuses(kw):
    with pytest.raises(Refused) as e:
        memberlist.member_list_o(**_args(**kw))
    assert e.value.code == "malformed"


def test_member_list_o_empty_rotation_and_other_persons():
    """After the first list an empty list is valid (11A.15.4's empty rotation); another person's member puts that
    person in ``persons``, sorted and once."""
    empty = memberlist.member_list_o(**_args(members=[], first=False, epoch=2, list_seq=2))
    assert (empty["persons"], empty["members"]) == ([], [])
    bob = IDS["person_bob"]
    two = [_member(device_id=IDS["device_bob_phone"], person_id=bob), *_M]
    o = memberlist.member_list_o(**_args(members=two))
    assert o["persons"] == [bob]
    assert [m["device_id"] for m in o["members"]] == sorted(m["device_id"] for m in two)


# --- 7. WK grants (protocol §7.3, corpus ``wk_grants``, the snapshot grants) and sealing (``seal``, ``seal_open``) --

GRANTS = S2["wk_grants"]["cases"]


@pytest.mark.parametrize("case", GRANTS, ids=[c["name"] for c in GRANTS])
def test_check_wk_grant_vectors(case):
    wsk = bytes.fromhex(case["wsk_pub"])
    dev = IDS[f"device_{case['device']}"]
    if "refuse" in case["expect"]:
        with pytest.raises(Refused) as e:
            memberlist.check_wk_grant(case["signed"], wsk, WS, dev)
        assert e.value.code == case["expect"]["refuse"]
        return
    g = memberlist.check_wk_grant(case["signed"], wsk, WS, dev)
    wk = crypto.open_sealed(_kx_priv(case["device"]), bytes.fromhex(dev), "wk", bytes.fromhex(WS), g.epoch, g.sealed)
    assert {"epoch": g.epoch, "wk": wk.hex()} == case["expect"]["ok"]
    # the builder rebuilds the signed object byte for byte from the grant's parts
    o = case["signed"]["o"]
    assert canon.dumps(memberlist.wk_grant_o(WS, dev, g.epoch, g.sealed)) == canon.dumps(o)


SNAPSHOT_GRANTS = [
    (f"{c['name']}[{i}][{j}]", c, g)
    for c in S2["member_list_snapshots"]
    for i, s in enumerate(c["steps"])
    for j, g in enumerate(s.get("grants", []))
]


@pytest.mark.parametrize(("name", "case", "grant"), SNAPSHOT_GRANTS, ids=[n for n, _, _ in SNAPSHOT_GRANTS])
def test_check_wk_grant_snapshot_grants(name, case, grant):
    """Every grant the snapshot cases hand a device verifies and opens (matching it to ``wk_commit`` is D3/D4)."""
    dev = IDS[f"device_{case['device']}"]
    g = memberlist.check_wk_grant(grant, bytes.fromhex(case["wsk_pub"]), case["card_workspace_id"], dev)
    wk = crypto.open_sealed(_kx_priv(case["device"]), bytes.fromhex(dev), "wk", bytes.fromhex(WS), g.epoch, g.sealed)
    assert len(wk) == 32
    assert canon.dumps(memberlist.wk_grant_o(WS, dev, g.epoch, g.sealed)) == canon.dumps(grant["o"])


def test_grant_cases_are_all_found():
    assert [c["name"] for c in GRANTS] == ["valid", "signed_by_another_workspace", "grant_for_another_device"]
    assert len(SNAPSHOT_GRANTS) == 14


def _fixed_ephemeral(monkeypatch, seed_hex: str) -> None:
    """The test-only CSPRNG seam: ``orch.crypto.seal`` draws its ephemeral key from ``suite.generate_private_key``."""
    monkeypatch.setattr(suite, "generate_private_key", lambda: _eph(seed_hex))


SEAL = S2["seal"]


@pytest.mark.parametrize("case", SEAL, ids=[c["name"] for c in SEAL])
def test_seal_wk_vector(case, monkeypatch):
    assert case["purpose"] == "wk" and case["extra_aad"] == ""
    _fixed_ephemeral(monkeypatch, case["eph_seed"])
    got = memberlist.seal_wk(
        bytes.fromhex(case["recipient_kx_pub"]),
        case["recipient_id"],
        case["object_id"],
        case["epoch"],
        bytes.fromhex(case["plaintext"]),
    )
    assert got.hex() == case["sealed"]


def test_seal_cases_are_all_found():
    assert [c["name"] for c in SEAL] == ["wk_to_phone"]
    assert len(S2["seal_open"]) == 7


SEAL_OPEN = S2["seal_open"]


@pytest.mark.parametrize("case", SEAL_OPEN, ids=[c["name"] for c in SEAL_OPEN])
def test_seal_open_vectors_against_seal_wk(case):
    """The corpus's sealed WK opens only as purpose ``wk`` for exactly this recipient, workspace and epoch, which is
    what :func:`seal_wk` binds; its output is the grant's ``sealed`` shape."""
    blob = bytes.fromhex(case["sealed"])
    args = (_kx_priv(case["recipient"]), bytes.fromhex(case["recipient_id"]), case["purpose"])
    if case["plaintext"] is not None:
        pt = crypto.open_sealed(*args, bytes.fromhex(case["object_id"]), case["epoch"], blob)
        assert pt.hex() == case["plaintext"]
        assert memberlist.wk_grant_o(WS, case["recipient_id"], case["epoch"], blob)["sealed"] == crypto.b64u(blob)
    else:
        with pytest.raises(crypto.InvalidTag):
            crypto.open_sealed(*args, bytes.fromhex(case["object_id"]), case["epoch"], blob)


def test_seal_wk_is_fresh_and_opens():
    phone = bytes.fromhex(KEYS["phone.kx"]["pub"])
    dev, wk = IDS["device_phone"], bytes(range(32))
    a, b = memberlist.seal_wk(phone, dev, WS, 7, wk), memberlist.seal_wk(phone, dev, WS, 7, wk)
    assert a != b and len(a) == len(b) == memberlist.SEALED_KEY_LEN
    assert crypto.open_sealed(_kx_priv("phone"), bytes.fromhex(dev), "wk", bytes.fromhex(WS), 7, a) == wk


CARD = S2["card"]


def test_card_key_wrap_vector(monkeypatch):
    """``card.card_key_wrap_to_phone``: CK sealed to the phone (purpose ``card``, the workspace, epoch card_seq)."""
    v = CARD["card_key_wrap_to_phone"]
    _fixed_ephemeral(monkeypatch, v["eph_seed"])
    card_seq = CARD["card"]["o"]["card_seq"]
    got = memberlist.card_key_wrap(
        bytes.fromhex(KEYS["phone.kx"]["pub"]), IDS["device_phone"], WS, card_seq, bytes.fromhex(CARD["card_key"])
    )
    assert got.hex() == v["sealed"]
    # the committed CK of the card is the one wrapped
    ck = bytes.fromhex(CARD["card_key"])
    pre = crypto.L["h_card_key_commit"] + bytes.fromhex(WS) + card_seq.to_bytes(4, "big") + ck
    assert crypto.b64u(crypto.sha256(pre)) == CARD["card"]["o"]["ck_commit"]


_PHONE_KX = bytes.fromhex(KEYS["phone.kx"]["pub"])
_DEV = IDS["device_phone"]
_OFF = b"\x04" + bytes(64)


@pytest.mark.parametrize(
    "args",
    [
        (_OFF, _DEV, WS, 1, bytes(32)),
        (_PHONE_KX[:-1], _DEV, WS, 1, bytes(32)),
        (_PHONE_KX, _DEV.upper(), WS, 1, bytes(32)),
        (_PHONE_KX, _DEV, WS[:-2], 1, bytes(32)),
        (_PHONE_KX, _DEV, WS, 0, bytes(32)),
        (_PHONE_KX, _DEV, WS, 2**32, bytes(32)),
        (_PHONE_KX, _DEV, WS, True, bytes(32)),
        (_PHONE_KX, _DEV, WS, 1, bytes(31)),
        (_PHONE_KX, _DEV, WS, 1, bytearray(32)),
        (_PHONE_KX, _DEV, WS, 1, "00" * 32),
    ],
)
@pytest.mark.parametrize("fn", [memberlist.seal_wk, memberlist.card_key_wrap])
def test_sealing_refuses_bad_inputs(fn, args):
    with pytest.raises(Refused) as e:
        fn(*args)
    assert e.value.code == "malformed"
    assert "00" * 16 not in str(e.value)  # no key bytes in the message


_SEALED = sealing._seal_with_ephemeral(
    _eph("01" * 32), _PHONE_KX, bytes.fromhex(_DEV), "wk", bytes.fromhex(WS), 1, bytes(32)
)


@pytest.mark.parametrize(
    "args",
    [
        (WS, _DEV, 1, _SEALED[:-1]),
        (WS, _DEV, 1, _SEALED + b"\0"),
        (WS, _DEV, 1, _OFF + _SEALED[65:]),
        (WS, _DEV, 1, crypto.b64u(_SEALED)),
        (WS, _DEV, 0, _SEALED),
        (WS.upper(), _DEV, 1, _SEALED),
        (WS, bytes.fromhex(_DEV), 1, _SEALED),
    ],
)
def test_wk_grant_o_refuses(args):
    with pytest.raises(Refused) as e:
        memberlist.wk_grant_o(*args)
    assert e.value.code == "malformed"


def _wsk_sign(o: dict) -> dict:
    """Test-only WSK signing with the corpus key (production signs through RelaySigner)."""
    seed = int.from_bytes(bytes.fromhex(KEYS["wsk_a.sig"]["seed"]), "big") % (crypto.P256_N - 1) + 1
    key = crypto.private_key_from_scalar(seed)
    sig = crypto.sign(key, crypto.L[f"sig_{o['kind']}"] + canon.dumps(o))
    return {"o": o, "sig": crypto.b64u(sig)}


_WSK = bytes.fromhex(KEYS["wsk_a.sig"]["pub"])


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda o: o.update(extra=1), "malformed"),
        (lambda o: o.update(epoch=0), "malformed"),
        (lambda o: o.update(sealed=crypto.b64u(_SEALED[:-1])), "malformed"),
        (lambda o: o.update(sealed="not b64u!"), "malformed"),
        (lambda o: o.update(workspace_id=IDS["workspace_b"]), "not_for_this_device"),
        (lambda o: o.update(kind="member_list"), "bad_signature"),
    ],
)
def test_check_wk_grant_refuses_signed_bad_shapes(mutate, code):
    o = memberlist.wk_grant_o(WS, _DEV, 1, _SEALED)
    mutate(o)
    with pytest.raises(Refused) as e:
        memberlist.check_wk_grant(_wsk_sign(o), _WSK, WS, _DEV)
    assert e.value.code == code


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda o: o.update(extra=1), "malformed"),
        (lambda o: o.update(persons=[OWNER, OWNER]), "malformed"),
        (lambda o: o.update(members=list(reversed(o["members"]))), "malformed"),
        (lambda o: o["members"][0].update(scopes="look"), "malformed"),
        (lambda o: o.update(issued_ms=-1), "malformed"),
        (lambda o: o.update(members=[o["members"][0]] * 1025), "malformed"),
    ],
)
def test_check_member_list_refuses_signed_bad_shapes(mutate, code):
    o = memberlist.member_list_o(**_args())
    mutate(o)
    with pytest.raises(Refused) as e:
        memberlist.check_member_list(_wsk_sign(o), _WSK, WS)
    assert e.value.code == code


def test_check_member_list_accepts_an_own_signed_list_and_reprs_no_secret():
    o = memberlist.member_list_o(**_args())
    got = memberlist.check_member_list(_wsk_sign(o), _WSK, WS)
    assert got.members[0].device_id == o["members"][0]["device_id"]
    with pytest.raises(Refused) as e:
        memberlist.check_member_list(_wsk_sign(o), _WSK, IDS["workspace_b"])
    assert e.value.code == "other_workspace"
