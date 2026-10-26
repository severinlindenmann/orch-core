"""H13-0: the Drop builders and openers (orch-relay protocol-v2 §10, §19.9; orch-v2-host 11A.16.5, D7).

Reproduced byte-exact from ``suites.2.drop``: the ORD2 blob and the empty blob, the sealed metadata, ``dek_commit``,
every wrap (``wk`` with the vector's salt, ``wxk`` and ``device`` with the vector's ephemeral seed), the ``drop_claim``
objects, the ODU2 ``upload_frame`` and every accepted ``relay_put`` frame (parsed, then rebuilt), and the same-DEK
rewrap of ``wk_wrap_at_an_old_epoch_then_the_same_dek_rewrapped``. The host frame builder enforces D7: a device-only
frame carries no workspace wrap, and nothing but a §10.3 wrap record (never the recovery envelope) enters a frame."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import pickle
import types

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from orch import canon, crypto
from orch.crypto import drop, recovery, suite
from orch.crypto.encoding import b64u, unb64u

from .vectors import ALL, S2, priv, pub

D = S2["drop"]
IDS = S2["ids"]
WS = bytes.fromhex(IDS["workspace_a"])
WS_B = bytes.fromhex(IDS["workspace_b"])
PHONE = bytes.fromhex(IDS["device_phone"])
LAPTOP = bytes.fromhex(IDS["device_laptop"])
OBJ = bytes.fromhex(D["object_id"])
DEK = bytes.fromhex(D["dek"])


def fake(label: str) -> bytes:
    """The relay reference's deterministic FAKE material (``ref/orch_protocol_ref.py`` ``fake``)."""
    return hashlib.sha256(b"FAKE orch v2 test vector: " + label.encode()).digest()


def seed_priv(seed: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(seed, "big") % (crypto.P256_N - 1) + 1, ec.SECP256R1())


WK1 = bytes.fromhex(S2["push"]["cases"][0]["keys"][IDS["workspace_a"]]["wk"]["1"])


def fix_salt(monkeypatch, salt: bytes) -> None:
    """Fix SALTED-AEAD's salt for one test (the randomness source inside ``orch.crypto.suite``; review F4: no
    cross-module seam)."""
    monkeypatch.setattr(suite, "secrets", types.SimpleNamespace(token_bytes=lambda n: salt[:n]))


def fix_ephemeral(monkeypatch, key) -> None:
    monkeypatch.setattr(suite, "generate_private_key", lambda: key)


def vector_version():
    return drop._seal_version_with_prefix(
        DEK, bytes.fromhex(D["nonce_prefix"]), OBJ, 1, bytes.fromhex(D["content"]), D["meta"], D["chunk_size"]
    )


# --- constants, blob, metadata, commitments -------------------------------------------------------------------------


def test_constants_match_the_vectors():
    c = ALL["constants"]["drop"]
    assert c["magic"].encode() == drop.BLOB_MAGIC and c["header_len"] == drop.HEADER_LEN
    assert c["default_chunk"] == drop.DEFAULT_CHUNK and c["max_chunk"] == drop.MAX_CHUNK
    assert drop.FRAME_MAGIC == b"ODU2" and drop.MAX_HEAD == 512 * 1024


def test_content_key_blob_meta_and_empty_blob_byte_exact():
    prefix = bytes.fromhex(D["nonce_prefix"])
    content = bytes.fromhex(D["content"])
    assert drop._content_key(DEK, OBJ, 1).hex() == D["k_content"]
    sv = vector_version()
    assert sv.blob.hex() == D["blob"] and sv.meta.hex() == D["meta_sealed"] and sv.dek == DEK
    empty = drop._seal_version_with_prefix(DEK, prefix, OBJ, 1, b"", D["meta"], D["chunk_size"])
    assert empty.blob.hex() == D["empty_blob"]
    assert drop.decrypt_blob(DEK, OBJ, 1, sv.blob) == content
    assert drop.decrypt_blob(DEK, OBJ, 1, bytes.fromhex(D["empty_blob"])) == b""


def test_seal_version_draws_a_fresh_dek_and_prefix():
    a = drop.seal_version(OBJ, 1, b"x", {"name": "a"}, chunk=512)
    b = drop.seal_version(OBJ, 1, b"x", {"name": "a"}, chunk=512)
    assert a.dek != b.dek and a.blob[32:40] != b.blob[32:40] and a.meta != b.meta
    assert drop.decrypt_blob(a.dek, OBJ, 1, a.blob) == b"x"
    assert drop.open_meta(a.dek, OBJ, 1, a.meta) == {"name": "a"}


def test_meta_is_sealed_only_with_a_fresh_dek():
    """Review F2: the zero-nonce meta seal is reachable only through ``seal_version``, which draws the DEK itself; no
    public function seals content or metadata under a DEK the caller supplies."""
    assert not hasattr(drop, "seal_meta") and not hasattr(drop, "encrypt_blob") and not hasattr(drop, "content_key")
    assert "dek" not in inspect.signature(drop.seal_version).parameters
    takes_dek = {
        n
        for n in drop.__all__
        if isinstance(getattr(drop, n), types.FunctionType) and "dek" in inspect.signature(getattr(drop, n)).parameters
    }
    # openers, the commitment and the wrap builders (which seal the DEK itself, never content or metadata)
    assert takes_dek == {
        "decrypt_blob", "decrypt_chunk", "open_meta", "dek_commit", "wrap_wk", "wrap_sealed", "rewrap_same_dek",
    }  # fmt: skip


def test_a_sealed_version_never_prints_pickles_or_copies():
    sv = drop.seal_version(OBJ, 1, b"x", {"name": "a"}, chunk=512)
    for text in (repr(sv), str(sv), f"{sv}", json.dumps(sv, default=str)):
        assert "redacted" in text and sv.dek.hex() not in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(sv)
    with pytest.raises(AttributeError):
        sv.dek = bytes(32)


def test_blob_truncation_and_substitution_refused():
    blob = bytes.fromhex(D["blob"])
    step = D["chunk_size"] + 16
    body = blob[drop.HEADER_LEN :]
    hdr = blob[: drop.HEADER_LEN]
    cases = [
        blob[: drop.HEADER_LEN + 2 * step],  # the last chunk dropped: chunk 2 is not marked last
        blob[:-1],
        blob[: drop.HEADER_LEN + 15],
        blob[: drop.HEADER_LEN],
        b"",
        hdr[:24] + (2).to_bytes(4, "big") + hdr[28:] + body,  # another version in the header
        hdr[:8] + bytes(16) + hdr[24:] + body,  # another object
        b"ORD1" + blob[4:],
        hdr[:5] + b"\x01" + hdr[6:] + body,  # another suite
        hdr[:28] + (0).to_bytes(4, "big") + hdr[32:] + body,  # chunk size 0
        hdr[:28] + (drop.MAX_CHUNK + 1).to_bytes(4, "big") + hdr[32:] + body,
        hdr + body[step:] + body[:step],  # chunks reordered
    ]
    for bad in cases:
        with pytest.raises(drop.DropError):
            drop.decrypt_blob(DEK, OBJ, 1, bad)
    with pytest.raises(drop.DropError):
        drop.decrypt_blob(bytes(32), OBJ, 1, blob)  # another DEK


def test_chunk_level_decryption_matches():
    blob = bytes.fromhex(D["blob"])
    chunk, prefix = drop.parse_blob_header(blob[: drop.HEADER_LEN], OBJ, 1)
    assert (chunk, prefix.hex()) == (D["chunk_size"], D["nonce_prefix"])
    hdr = blob[: drop.HEADER_LEN]
    step = chunk + 16
    body = blob[drop.HEADER_LEN :]
    parts = [body[i : i + step] for i in range(0, len(body), step)]
    out = b"".join(drop.decrypt_chunk(DEK, OBJ, 1, hdr, i, ct, i == len(parts) - 1) for i, ct in enumerate(parts))
    assert out == bytes.fromhex(D["content"])
    with pytest.raises(drop.DropTagError):
        drop.decrypt_chunk(DEK, OBJ, 1, hdr, 0, parts[0], True)  # is_last flipped
    with pytest.raises(drop.DropError):
        drop.decrypt_chunk(DEK, OBJ, 2, hdr, 0, parts[0], False)  # a header of another version


def test_meta_and_dek_commit_byte_exact():
    assert drop.open_meta(DEK, OBJ, 1, bytes.fromhex(D["meta_sealed"])) == D["meta"]
    assert b64u(drop.dek_commit(OBJ, 1, DEK)) == D["dek_commit"]
    for bad in ((DEK, OBJ, 2), (DEK, bytes(16), 1), (bytes(32), OBJ, 1)):
        with pytest.raises(drop.DropTagError):
            drop.open_meta(*bad, bytes.fromhex(D["meta_sealed"]))
    with pytest.raises(drop.DropError):
        drop.open_meta(DEK, OBJ, 1, b"x" * (drop.MAX_META + 1))


# --- wraps ----------------------------------------------------------------------------------------------------------


SPACE_O = {**D["descriptor"]["o"], "recipients": [IDS["workspace_a"]]}


def test_wk_wrap_byte_exact(monkeypatch):
    w = D["wraps"]["wk"]
    salt = unb64u(w["wrap"])[:16]
    assert salt == fake("wrap salt")[:16]
    fresh = drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK)
    assert fresh["wrap"] != w["wrap"] and drop.open_wrap(fresh, SPACE_O, WS, wk=WK1) == DEK
    fix_salt(monkeypatch, salt)
    assert drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK) == w
    assert drop.open_wrap(w, SPACE_O, WS, wk=WK1) == DEK


@pytest.mark.parametrize(
    "name,seed,kx",
    [
        ("wxk_a", "eph wrap a", "wxk_a_1.kx"),
        ("wxk_b", "eph wrap b", "wxk_b_1.kx"),
        ("device", "eph wrap d", "phone.kx"),
    ],
)
def test_sealed_wraps_byte_exact(monkeypatch, name, seed, kx):
    w = D["wraps"][name]
    target = bytes.fromhex(w["target_id"])
    fresh = drop.wrap_sealed(w["target_kind"], pub(kx), target, w["key_version"], OBJ, 1, DEK)
    assert fresh["wrap"] != w["wrap"] and drop._open_wrap_sealed(fresh, priv(kx)) == DEK
    fix_ephemeral(monkeypatch, seed_priv(fake(seed)))
    assert drop.wrap_sealed(w["target_kind"], pub(kx), target, w["key_version"], OBJ, 1, DEK) == w
    assert drop._open_wrap_sealed(w, priv(kx)) == DEK


@pytest.mark.parametrize("case", D["wrap_negative"], ids=lambda c: c["name"])
def test_wrap_negative(case):
    rec = case["record"]
    assert case["opens"] is False
    with pytest.raises(drop.DropError):
        if rec["target_kind"] == "wk":
            drop._open_wrap_wk(rec, WK1, WS)
        else:
            for kx in ("wxk_a_1.kx", "wxk_b_1.kx", "phone.kx"):
                try:
                    drop._open_wrap_sealed(rec, priv(kx))
                except drop.DropError:
                    continue
                raise AssertionError("opened")
            raise drop.DropTagError()


def test_open_wrap_checks_the_descriptor_and_dek_commit():
    o = D["descriptor"]["o"]
    assert drop.open_wrap(D["wraps"]["wxk_a"], o, WS, kx_priv=priv("wxk_a_1.kx")) == DEK
    assert drop.open_wrap(D["wraps"]["device"], o, PHONE, kx_priv=priv("phone.kx")) == DEK
    assert drop.open_wrap(D["wraps"]["wk"], SPACE_O, WS, wk=WK1) == DEK
    with pytest.raises(drop.DropError):  # a wrap of another version
        drop.open_wrap({**D["wraps"]["device"], "version": 2}, o, PHONE, kx_priv=priv("phone.kx"))
    with pytest.raises(drop.DropError):  # a DEK that is not the committed one
        drop.open_wrap(D["wraps"]["device"], {**o, "dek_commit": b64u(bytes(32))}, PHONE, kx_priv=priv("phone.kx"))
    other_dek = drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, bytes(32))
    with pytest.raises(drop.DropError):  # a wrap of another DEK (the key-commitment check, §10.4)
        drop.open_wrap(other_dek, o, PHONE, kx_priv=priv("phone.kx"))


def test_open_wrap_checks_the_target_against_the_caller():
    """Review F3: the wrap must be addressed to the caller (``me``: its device id, or the space id for ``wk`` and
    ``wxk``), and a ``wxk`` or ``wk`` wrap must name the descriptor's own space."""
    o = D["descriptor"]["o"]
    with pytest.raises(drop.DropError):  # a device wrap for another device
        drop.open_wrap(D["wraps"]["device"], o, LAPTOP, kx_priv=priv("phone.kx"))
    with pytest.raises(drop.DropError):  # the space's wxk wrap asked for as a device
        drop.open_wrap(D["wraps"]["wxk_a"], o, PHONE, kx_priv=priv("wxk_a_1.kx"))
    with pytest.raises(drop.DropError):  # a wxk wrap to another workspace than the descriptor's space
        drop.open_wrap(D["wraps"]["wxk_b"], o, WS_B, kx_priv=priv("wxk_b_1.kx"))
    with pytest.raises(drop.DropError):  # a wk wrap asked for by another workspace
        drop.open_wrap(D["wraps"]["wk"], SPACE_O, WS_B, wk=WK1)
    for name in ("open_wrap_sealed", "open_wrap_wk"):
        assert not hasattr(drop, name) and name not in drop.__all__


@pytest.mark.parametrize(
    "mut",
    [
        lambda r: {**r, "x": 1},
        lambda r: {k: v for k, v in r.items() if k != "wrap"},
        lambda r: {**r, "target_kind": "vault"},
        lambda r: {**r, "object_id": r["object_id"].upper()},
        lambda r: {**r, "version": 0},
        lambda r: {**r, "version": True},
        lambda r: {**r, "key_version": -1},
        lambda r: {**r, "key_version": 2**32},
        lambda r: {**r, "wrap": r["wrap"] + "="},
        lambda r: {**r, "wrap": b64u(bytes(600))},
        lambda r: {**r, "target_kind": "device", "key_version": 1},
        lambda r: [r],
        lambda r: canon.dumps(r),
    ],
)
def test_wrap_record_check_is_closed(mut):
    with pytest.raises(drop.DropError):
        drop.check_wrap_record(mut(D["wraps"]["device"]))
    drop.check_wrap_record(D["wraps"]["device"])


# --- descriptors and claims -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", D["descriptor_cases"], ids=lambda c: c["name"])
def test_descriptor_cases(case):
    want = case["expect"]
    if "ok" in want:
        assert drop.verify_drop_object(case["signed"], bytes.fromhex(case["author_pub"])) == want["ok"]
    else:
        err = drop.DropTagError if want["refuse"] == "bad_signature" else drop.DropError
        with pytest.raises(err):
            drop.verify_drop_object(case["signed"], bytes.fromhex(case["author_pub"]))


def test_descriptor_builder_reproduces_the_vector_object():
    o = D["descriptor"]["o"]
    built = drop.drop_object(
        vector_version(),
        space_id=WS,
        object_kind="file",
        parent_hash=None,
        author_kind="device",
        author_id=PHONE,
        recipients="inbox",
        origin="human",
        created_ms=o["created_ms"],
        expires_ms=o["expires_ms"],
    )
    assert built == o
    assert drop.drop_object_signed_bytes(o) == crypto.L["sig_drop_object"] + canon.dumps(o)
    assert crypto.verify(pub("phone.sig"), unb64u(D["descriptor"]["sig"]), drop.drop_object_signed_bytes(o))


@pytest.mark.parametrize(
    "over",
    [
        {"object_kind": "folder"},
        {"space_kind": "personal"},
        {"version": 2, "parent": 1},
        {"parent": 1},
        {"recipients": []},
        {"recipients": [IDS["device_phone"], IDS["device_phone"]]},
        {"recipients": "everyone"},
        {"origin": "robot"},
        {"author_kind": "person"},
        {"content_len": -1},
        {"content_hash": "short"},
        {"dek_commit": b64u(bytes(31))},
        {"meta": b64u(bytes(drop.MAX_META + 1))},
        {"expires_ms": None},
        {"object_kind": "document", "recipients": "inbox", "expires_ms": None},
        {"object_kind": "document"},  # a document has no expiry
        {"v": 1},
        {"x": 1},
    ],
)
def test_descriptor_check_refuses(over):
    with pytest.raises(drop.DropError):
        drop.check_drop_object({**D["descriptor"]["o"], **over})


def test_parent_hash():
    o = D["descriptor"]["o"]
    assert drop.parent_hash(o) == crypto.sha256(crypto.L["h_drop_parent"] + canon.dumps(o))


def test_claim_objects_reproduced():
    n = 0
    for case in D["claims"]:
        for step in case["steps"]:
            claim = step.get("claim")
            if not isinstance(claim, dict) or not isinstance(claim.get("o"), dict):
                continue
            o = claim["o"]
            try:
                wrap = drop.check_wrap_record(o.get("wrap"))
                built = drop.drop_claim_object(bytes.fromhex(o["object_id"]), bytes.fromhex(o["workspace_id"]), wrap)
            except (drop.DropError, KeyError, ValueError, TypeError):
                continue
            if built == o:
                n += 1
                ws_card = case["cards"].get(o["workspace_id"])
                if ws_card and step.get("expect", {}).get("status") == 200:
                    assert crypto.verify(
                        unb64u(ws_card["wsk_pub"]), unb64u(claim["sig"]), drop.drop_claim_signed_bytes(o)
                    )
    assert n >= 1


def test_claim_object_refuses_a_non_wk_wrap():
    with pytest.raises(drop.DropError):
        drop.drop_claim_object(OBJ, WS, D["wraps"]["wxk_a"])
    with pytest.raises(drop.DropError):
        drop.drop_claim_object(OBJ, WS_B, D["wraps"]["wk"])  # a wrap for another workspace


# --- the ODU2 frame -------------------------------------------------------------------------------------------------


def _frames(cases):
    for c in cases:
        for s in c.get("steps", [c]):
            if s.get("op") == "put" and "body_hex" in s:
                yield c.get("name", s.get("name")), s


def test_upload_frame_valid_is_rebuilt_byte_exact():
    valid = next(s for s in D["upload_frame"]["cases"] if s["name"] == "valid_frame")
    body = bytes.fromhex(valid["body_hex"])
    head, blob = drop.parse_frame(body)
    assert drop.encode_frame(head["descriptor"], head["wraps"], blob) == body
    assert body[:4] == b"ODU2" and int.from_bytes(body[4:8], "big") == len(canon.dumps(head))


@pytest.mark.parametrize("name", ["head_len_little_endian", "bad_magic", "blob_one_byte_short", "blob_one_byte_long"])
def test_upload_frame_hostile_cases(name):
    s = next(s for s in D["upload_frame"]["cases"] if s["name"] == name)
    with pytest.raises(drop.DropError):
        drop.parse_frame(bytes.fromhex(s["body_hex"]))


def test_upload_frame_head_caps():
    for s in D["upload_frame"]["cases"]:
        if "body_recipe" not in s:
            continue
        r = s["body_recipe"]
        body = bytes.fromhex(r["prefix_hex"]) + bytes([r["byte"]]) * r["count"] + bytes.fromhex(r["suffix_hex"])
        with pytest.raises(drop.DropError):
            drop.parse_frame(body)


def test_every_accepted_relay_put_frame_is_rebuilt_byte_exact():
    n = 0
    for name, s in _frames(D["relay_put"]):
        body = bytes.fromhex(s["body_hex"])
        if s["expect"]["status"] in (200, 201):
            head, blob = drop.parse_frame(body)
            assert drop.encode_frame(head["descriptor"], head["wraps"], blob) == body, name
            n += 1
    assert n >= 10


@pytest.mark.parametrize("name", ["file_version_2_refused", "inbox_document_refused"])
def test_relay_put_malformed_frames_are_refused(name):
    case = next(c for c in D["relay_put"] if c["name"] == name)
    bad = [s for s in case["steps"] if s.get("op") == "put" and s["expect"]["status"] == 400]
    assert bad
    for s in bad:
        with pytest.raises(drop.DropError):
            drop.parse_frame(bytes.fromhex(s["body_hex"]))


def test_same_dek_rewrap_vector():
    """``wk_wrap_at_an_old_epoch_then_the_same_dek_rewrapped``: the second frame is the first with its ``wk`` wrap
    rewrapped under epoch 2 (same DEK, same descriptor, same blob)."""
    case = next(c for c in D["relay_put"] if c["name"] == "wk_wrap_at_an_old_epoch_then_the_same_dek_rewrapped")
    old, new = (bytes.fromhex(s["body_hex"]) for s in case["steps"] if s.get("op") == "put")
    (h_old, b_old), (h_new, b_new) = drop.parse_frame(old), drop.parse_frame(new)
    assert h_old["descriptor"] == h_new["descriptor"] and b_old == b_new
    o = h_old["descriptor"]["o"]
    wk1, wk2 = fake("drop test WK epoch 1"), fake("drop test WK epoch 2")
    space = bytes.fromhex(o["space_id"])
    dek = drop.open_wrap(h_old["wraps"][0], o, space, wk=wk1)
    fresh = drop.rewrap_same_dek(o, dek, wk2, 2)
    assert drop.open_wrap(fresh, o, space, wk=wk2) == dek
    with pytest.MonkeyPatch.context() as mp:
        fix_salt(mp, fake(f"salt {o['object_id']} 2")[:16])
        w2 = drop.rewrap_same_dek(o, dek, wk2, 2)
    assert drop.reframe(old, [w2]) == new
    with pytest.raises(drop.DropError):  # never another DEK
        drop.rewrap_same_dek(o, bytes(32), wk2, 2)
    with pytest.raises(drop.DropError):  # never a device-only object
        drop.rewrap_same_dek({**o, "recipients": [IDS["device_laptop"]]}, dek, wk2, 2)


def test_reframe_keeps_descriptor_and_blob_and_refuses_foreign_wraps():
    valid = next(s for s in D["upload_frame"]["cases"] if s["name"] == "valid_frame")
    body = bytes.fromhex(valid["body_hex"])
    head, blob = drop.parse_frame(body)
    assert drop.reframe(body, head["wraps"]) == body
    other = {**head["wraps"][0], "object_id": bytes(16).hex()}
    with pytest.raises(drop.DropError):
        drop.reframe(body, [other])


# --- the host frame (D7) --------------------------------------------------------------------------------------------


def _host_object(recipients, *, dek=DEK, obj=OBJ):
    meta = {"name": "a.txt", "mime": "text/plain", "size": 9, "transcript": None}
    sv = drop._seal_version_with_prefix(dek, bytes(8), obj, 1, b"host file", meta, 512)
    blob = sv.blob
    o = drop.drop_object(
        sv,
        space_id=WS,
        object_kind="file",
        parent_hash=None,
        author_kind="workspace",
        author_id=WS,
        recipients=recipients,
        origin="agent",
        created_ms=1790000000000,
        expires_ms=1790000000000 + 7 * 86_400_000 - 600_000,
    )
    signed = {"o": o, "sig": b64u(crypto.sign(priv("wsk_a.sig"), drop.drop_object_signed_bytes(o)))}
    return signed, blob


def test_host_frame_space_and_device_only():
    signed, blob = _host_object([WS.hex()])
    wk = drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK)
    body = drop.build_host_frame(signed, [wk], blob)
    head, got = drop.parse_frame(body)
    assert head["wraps"] == [wk] and got == blob
    devs = sorted([IDS["device_phone"], IDS["device_laptop"]])
    signed, blob = _host_object(devs)
    wraps = [
        drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, DEK),
        drop.wrap_sealed("device", pub("laptop.kx"), LAPTOP, 0, OBJ, 1, DEK),
    ]
    body = drop.build_host_frame(signed, wraps, blob)
    assert drop.parse_frame(body)[0]["wraps"] == wraps


def test_a_device_only_frame_carries_no_workspace_wrap():
    devs = sorted([IDS["device_phone"], IDS["device_laptop"]])
    signed, blob = _host_object(devs)
    dev = [drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, DEK)]
    wk = drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK)
    wxk = drop.wrap_sealed("wxk", pub("wxk_a_1.kx"), WS, 1, OBJ, 1, DEK)
    for extra in (wk, wxk):
        with pytest.raises(drop.DropError):
            drop.build_host_frame(signed, dev + [extra], blob)
    stranger = drop.wrap_sealed("device", pub("agent.kx"), bytes.fromhex(IDS["device_agent"]), 0, OBJ, 1, DEK)
    with pytest.raises(drop.DropError):  # a device outside recipients
        drop.build_host_frame(signed, dev + [stranger], blob)
    with pytest.raises(drop.DropError):  # no wrap at all
        drop.build_host_frame(signed, [], blob)


def test_host_frame_rules():
    signed, blob = _host_object([WS.hex()])
    wk = drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK)
    dev = drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, DEK)
    with pytest.raises(drop.DropError):  # a space frame has exactly one wk wrap and nothing else
        drop.build_host_frame(signed, [wk, dev], blob)
    with pytest.raises(drop.DropError):
        drop.build_host_frame(signed, [wk, wk], blob)
    inbox, iblob = _host_object("inbox")
    with pytest.raises(drop.DropError):  # the host never writes inbox objects
        drop.build_host_frame(inbox, [drop.wrap_sealed("wxk", pub("wxk_a_1.kx"), WS, 1, OBJ, 1, DEK)], iblob)
    with pytest.raises(drop.DropError):  # a blob that is not the descriptor's
        drop.build_host_frame(signed, [wk], blob[:-1] + bytes([blob[-1] ^ 1]))


def test_the_recovery_envelope_never_enters_a_frame():
    devs = sorted([IDS["device_phone"], IDS["device_laptop"]])
    signed, blob = _host_object(devs)
    env = recovery.seal_recovery(WK1, WS, 1, OBJ, 1, DEK)
    dev = drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, DEK)
    for wraps in ([dev, env], [env], [dev, canon.loads_strict(env.to_host_bytes())]):
        with pytest.raises(drop.DropError):
            drop.build_host_frame(signed, wraps, blob)  # type: ignore[list-item]
        with pytest.raises(drop.DropError):
            drop.encode_frame(signed, wraps, blob)  # type: ignore[list-item]
    body = drop.build_host_frame(signed, [dev], blob)
    assert env.ct not in body and env.ct.hex().encode() not in body and b64u(env.ct).encode() not in body
    assert b"drop_recovery" not in body
    with pytest.raises(drop.DropError):
        drop.reframe(body, [env])  # type: ignore[list-item]


def test_parse_frame_size_limits():
    valid = next(s for s in D["upload_frame"]["cases"] if s["name"] == "valid_frame")
    body = bytes.fromhex(valid["body_hex"])
    for bad in (b"", b"ODU2", b"ODU2\x00\x00\x00", body[:8], b"ODU2" + (10).to_bytes(4, "big") + b"{}"):
        with pytest.raises(drop.DropError):
            drop.parse_frame(bad)
    head_len = int.from_bytes(body[4:8], "big")
    head = canon.loads_strict(body[8 : 8 + head_len])
    for bad_head in ({**head, "x": 1}, {"descriptor": head["descriptor"]}, {**head, "wraps": head["wraps"] * 2}):
        h = canon.dumps(bad_head)
        with pytest.raises(drop.DropError):
            drop.parse_frame(b"ODU2" + len(h).to_bytes(4, "big") + h + body[8 + head_len :])
    spaced = body[8 : 8 + head_len].replace(b":", b": ", 1)  # not canonical: refused, the head is cj
    with pytest.raises(drop.DropError):
        drop.parse_frame(b"ODU2" + len(spaced).to_bytes(4, "big") + spaced + body[8 + head_len :])


def test_a_host_reframing_keeps_d7():
    devs = sorted([IDS["device_phone"], IDS["device_laptop"]])
    signed, blob = _host_object(devs)
    dev = drop.wrap_sealed("device", pub("phone.kx"), PHONE, 0, OBJ, 1, DEK)
    body = drop.build_host_frame(signed, [dev], blob)
    lap = drop.wrap_sealed("device", pub("laptop.kx"), LAPTOP, 0, OBJ, 1, DEK)
    again = drop.reframe_host(body, [dev, lap])
    assert drop.parse_frame(again)[0]["wraps"] == [dev, lap]
    for extra in (
        drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK),
        drop.wrap_sealed("wxk", pub("wxk_a_1.kx"), WS, 1, OBJ, 1, DEK),
    ):
        with pytest.raises(drop.DropError):
            drop.reframe_host(body, [dev, extra])
    signed, blob = _host_object([WS.hex()])
    body = drop.build_host_frame(signed, [drop.wrap_wk(WK1, WS, 1, OBJ, 1, DEK)], blob)
    w2 = drop.rewrap_same_dek(signed["o"], DEK, bytes(32), 2)
    assert drop.parse_frame(drop.reframe_host(body, [w2]))[0]["wraps"] == [w2]
