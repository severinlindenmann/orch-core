"""#1044: random base64url key material holds a ``ghp_``-shaped run in about 1 invite in 13,000. The GitHub-token
shape is not applied to the fields that are key material **by schema** (exact path and exact decoded length);
everywhere else, free text above all, it is applied as before. Grant shapes and the presented secret are never
exempt."""

from __future__ import annotations

import base64
import json
import random

import pytest

from orch import crypto
from orch.host.auth import secret_in_request
from orch.ops.base import Context
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.tokenshape import GITHUB_TOKEN_SHAPE

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GRANT = "gr_0" + "0" * 25 + "." + "A" * 43


def b64(n: int, rng: random.Random) -> str:
    return crypto.b64u(rng.randbytes(n))


def forced(n: int, rng: random.Random) -> str:
    """Canonical base64url of ``n`` random bytes with a token-shaped run planted inside it."""
    while True:
        raw = bytearray(crypto.b64u(rng.randbytes(n)).encode())
        pos = rng.randrange(0, len(raw) - 24)
        raw[pos : pos + 4] = b"gh" + bytes([rng.choice(b"pousr")]) + b"_"
        text = raw.decode()
        try:
            crypto.unb64u(text, n)
        except crypto.EncodingError:
            continue
        if GITHUB_TOKEN_SHAPE.search(text):
            return text


def cert_doc(rng: random.Random, **keys: str) -> dict:
    """A device certificate shaped like ``certs.make_device_cert`` (random bytes, unsigned: the shape matters)."""
    o = {
        "v": 2, "suite": 2, "kind": "device_cert", "device_id": rng.randbytes(16).hex(),
        "person_id": rng.randbytes(16).hex(), "dk_sig_pub": b64(65, rng), "dk_kx_pub": b64(65, rng),
        "label_sealed": b64(48, rng), "created_ms": 1_700_000_000_000, "expires_ms": None,
        "scopes_max": ["look", "decide", "operate", "type"],
    }  # fmt: skip
    o.update({k: v for k, v in keys.items() if k != "sig"})
    return {"o": o, "sig": keys.get("sig", b64(64, rng))}


def decl_doc(rng: random.Random, **keys: str) -> dict:
    o = {
        "v": 1, "kind": "reader_decl", "suite": 2, "workspace_id": "ws_" + rng.randbytes(8).hex(),
        "device_id": rng.randbytes(16).hex(), "dk_hash": b64(32, rng), "who": {"person_id": rng.randbytes(16).hex()},
        "reader": 1, "kat": {"1": {"log": "sha256:" + "0" * 64}}, "bind": {"via": "enrol"},
        "created_ms": 1_700_000_000_000,
    }  # fmt: skip
    o.update({k: v for k, v in keys.items() if k != "sig"})
    return {"o": o, "sig": keys.get("sig", b64(64, rng))}


def invite_frame(pk: str, cert: dict, decl: dict | None = None, **extra) -> dict:
    def inline(doc):
        return {"b64": base64.b64encode(json.dumps(doc).encode()).decode()}

    args = {"name": "Nina", "pk": pk, "cert": "cert.json", **extra}
    files = {"cert.json": inline(cert)}
    if decl is not None:
        args["reader_decl"] = "decl.json"
        files["decl.json"] = inline(decl)
    return {"op": "member.add", "args": args, "files": files}


def runtime_refuses(doc: dict) -> bool:
    call = Call(Context(env={}), "member.add")
    try:
        call.check_secret_structured(json.dumps(doc), "device_cert")
    except OrchError as e:
        assert e.code == "grant.secret_in_args"
        return True
    return False


# --- the 40,000-invite sample: random key material, zero false refusals -------------------------------------------


@pytest.mark.slow
def test_forty_thousand_random_invites_are_never_refused_and_the_old_check_did_refuse_some():
    rng = random.Random(1044)
    old_refusals = 0
    for _ in range(40_000):
        pk, cert, decl = b64(65, rng), cert_doc(rng), decl_doc(rng)
        text = pk + json.dumps(cert) + json.dumps(decl)
        old_refusals += bool(GITHUB_TOKEN_SHAPE.search(text))  # what the whole-text scan did
        assert not secret_in_request(invite_frame(pk, cert, decl)), (pk, cert, decl)
        assert not runtime_refuses(cert)
    assert old_refusals >= 1  # the sample really contains token-shaped key material (about 3 in 40,000)


# --- every exempt field, with a token-shaped run planted in it -----------------------------------------------------


@pytest.mark.parametrize("field,n", [("dk_sig_pub", 65), ("dk_kx_pub", 65), ("label_sealed", 48), ("sig", 64)])
def test_a_token_shaped_run_in_a_certificate_key_field_is_accepted(field, n):
    rng = random.Random(field)
    for _ in range(50):
        cert = cert_doc(rng, **{field: forced(n, rng)})
        assert GITHUB_TOKEN_SHAPE.search(json.dumps(cert))
        assert not secret_in_request(invite_frame(b64(65, rng), cert))
        assert not runtime_refuses(cert)


def test_a_token_shaped_run_in_the_invitees_pk_or_the_declaration_hash_is_accepted():
    rng = random.Random(7)
    for _ in range(50):
        assert not secret_in_request(invite_frame(forced(65, rng), cert_doc(rng)))
        assert not secret_in_request(invite_frame(b64(65, rng), cert_doc(rng), decl_doc(rng, dk_hash=forced(32, rng))))


def test_the_signed_member_added_event_may_carry_the_same_key_material():
    rng = random.Random(11)
    cert = cert_doc(rng, dk_sig_pub=forced(65, rng), dk_kx_pub=forced(65, rng), label_sealed=forced(48, rng))
    cert["sig"] = forced(64, rng)
    event = {
        "type": "member.added",
        "name": "Nina",
        "pk_pub": forced(65, rng),
        "device_cert": cert,
        "sig": b64(64, rng),
    }
    assert not secret_in_request({"op": "human.submit", "args": {"events": [event]}})


# --- free text and everything not listed is still refused ---------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        {"op": "new", "args": {"title": "deploy " + TOKEN}},
        {"op": "ticket.note", "args": {"text": "see " + TOKEN + " for the login"}},
        {"op": "section.set", "args": {"body": "x\n" + TOKEN}},
        {"op": "member.add", "args": {"name": TOKEN, "pk": "A" * 87}},
        {"op": "human.submit", "args": {"events": [{"type": "member.added", "name": TOKEN}]}},
    ],
)
def test_a_real_token_in_free_text_is_still_refused(frame):
    assert secret_in_request(frame)


def test_a_real_token_in_a_ticket_title_or_body_is_refused_by_the_runtime():
    call = Call(Context(env={}), "new")
    for text in ("deploy " + TOKEN, TOKEN, "a\nb " + TOKEN + "\nc"):
        with pytest.raises(OrchError) as exc:
            call.text(text, one_line="\n" not in text, what="title")
        assert exc.value.code == "grant.secret_in_args"
        with pytest.raises(OrchError) as exc:
            call.body_text(text)
        assert exc.value.code == "grant.secret_in_args"


def test_a_token_anywhere_else_in_the_certificate_file_is_refused():
    rng = random.Random(3)
    for where in ("device_id", "person_id", "kind", "label", "extra"):
        cert = cert_doc(rng, **{where: TOKEN})
        assert secret_in_request(invite_frame(b64(65, rng), cert)), where
        assert runtime_refuses(cert), where
    cert = cert_doc(rng)
    cert["o"]["scopes_max"] = ["look", TOKEN]
    assert secret_in_request(invite_frame(b64(65, rng), cert)) and runtime_refuses(cert)
    cert = cert_doc(rng)
    cert["o"][TOKEN] = 1  # in a key name
    assert secret_in_request(invite_frame(b64(65, rng), cert)) and runtime_refuses(cert)
    cert = cert_doc(rng)
    cert["note"] = TOKEN  # a field outside {o, sig}: the whole file is scanned
    assert secret_in_request(invite_frame(b64(65, rng), cert)) and runtime_refuses(cert)


def test_a_field_of_the_wrong_shape_is_not_exempt():
    rng = random.Random(5)
    plain = TOKEN + "x" * 47  # token shaped, 87 chars, but not a 65 byte key (wrong length / not canonical)
    for field, bad in (
        ("dk_sig_pub", plain), ("dk_sig_pub", forced(64, rng)), ("dk_kx_pub", forced(33, rng)),
        ("label_sealed", TOKEN), ("label_sealed", forced(2000, rng)), ("sig", forced(65, rng)), ("sig", TOKEN),
    ):  # fmt: skip
        cert = cert_doc(rng, **{field: bad})
        assert secret_in_request(invite_frame(b64(65, rng), cert)), (field, bad[:12])
        assert runtime_refuses(cert), (field, bad[:12])
    assert secret_in_request(invite_frame(plain, cert_doc(rng)))  # --pk of the wrong length
    assert secret_in_request(invite_frame(forced(64, rng), cert_doc(rng)))


def test_the_exemption_is_for_these_ops_and_paths_only():
    rng = random.Random(9)
    key = forced(65, rng)
    assert secret_in_request({"op": "ticket.note", "args": {"pk": key}})
    assert secret_in_request({"op": "member.add", "args": {"pk2": key}})
    assert secret_in_request({"op": "member.add", "args": {"name": key}})  # a free text field, key shaped
    assert secret_in_request({"op": "member.add", "args": {"pk": key}, "opts": {"idem": key}})
    cert = cert_doc(rng, dk_sig_pub=key)
    assert secret_in_request({**invite_frame(b64(65, rng), cert), "op": "ticket.note"})
    # the cert file is exempt only as the file named by --cert, never another inline file or a blob chunk
    frame = invite_frame(b64(65, rng), cert)
    frame["files"]["other.json"] = frame["files"]["cert.json"]
    assert secret_in_request(frame)
    assert secret_in_request({"op": "blob.put", "args": {"b64": frame["files"]["cert.json"]["b64"]}})
    # an event path that is not listed
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"type": "x", "text": key}]}})


def test_not_exactly_a_certificate_file_means_a_full_scan():
    rng = random.Random(13)
    key = forced(65, rng)
    raw = json.dumps(cert_doc(rng, dk_sig_pub=key))
    for text in (raw + " trailing", raw.replace('"sig"', '"sig", "sig"', 1), "[" + raw + "]", raw[:-1]):
        frame = invite_frame(b64(65, rng), cert_doc(rng))
        frame["files"]["cert.json"] = {"b64": base64.b64encode(text.encode()).decode()}
        assert secret_in_request(frame), text[:20]
        call = Call(Context(env={}), "member.add")
        with pytest.raises(OrchError):
            call.check_secret_structured(text, "device_cert")
    dup = '{"o": {"dk_sig_pub": "' + key + '", "dk_sig_pub": "x"}, "sig": "' + b64(64, rng) + '"}'
    with pytest.raises(OrchError):
        Call(Context(env={}), "member.add").check_secret_structured(dup, "device_cert")


def test_grant_shapes_and_the_presented_secret_are_never_exempt_in_a_key_field():
    rng = random.Random(17)
    for field in ("dk_sig_pub", "dk_kx_pub", "label_sealed", "sig"):
        cert = cert_doc(rng, **{field: GRANT})
        assert secret_in_request(invite_frame(b64(65, rng), cert))
    secret = "S3cretS3cretS3cret"
    key = b64(65, rng)
    cert = cert_doc(rng, dk_sig_pub=key)
    frame = invite_frame(b64(65, rng), cert)
    frame["auth"] = {"grant": "gr_x." + key[5:40]}  # the secret is a piece of a key field
    assert secret_in_request(frame)
    frame["auth"] = {"grant": "gr_x." + secret}
    assert not secret_in_request(frame)
    call = Call(Context(env={}, grant="gr_x." + key[5:40]), "member.add")
    with pytest.raises(OrchError):
        call.check_secret_structured(json.dumps(cert), "device_cert")
    with pytest.raises(OrchError):
        Call(Context(env={}), "member.add").check_secret_structured(json.dumps(cert_doc(rng, sig=GRANT)), "device_cert")


# -- #1475: the hub card, the hub-owner proof and the share plan digest are key material too ---------------------


def _put(tree: dict, path: tuple, value: object) -> None:
    for step in path[:-1]:
        tree = tree.setdefault(step, {})
    tree[path[-1]] = value


@pytest.mark.parametrize(
    ("op", "prefix", "path", "n"),
    [
        *(
            (op, pre, path, n)
            for op, pre in (("hub.connect", ()), ("human.prepare", ("args",)))
            for path, n in (
                (("hub_card", "o", "hk_root_pub"), 65),
                (("hub_card", "o", "hxk_pub"), 65),
                (("hub_card", "o", "tls_spki_sha256"), 32),
                (("hub_card", "op_cert", "o", "hk_op_pub"), 65),
                (("hub_card", "op_cert", "sig"), 64),
                (("hub_card", "sig"), 64),
            )
        ),
        ("share.rule_add", (), ("expect",), 32),
        ("share.rule_remove", (), ("expect",), 32),
        ("share.sync", (), ("expect",), 32),
    ],
)
def test_1475_listed_key_fields_may_hold_a_token_shaped_run_and_free_text_may_not(op, prefix, path, n):
    rng = random.Random(1475)
    value = forced(n, rng)
    args: dict = {}
    _put(args, (*prefix, *path), value)
    assert not secret_in_request({"op": op, "args": args})
    # the same run at another place, or in a value of another length, is still refused
    other: dict = {}
    _put(other, (*prefix, *path[:-1], "note"), value)
    assert secret_in_request({"op": op, "args": other})
    longer: dict = {}
    _put(longer, (*prefix, *path), value + "AAAA")
    assert secret_in_request({"op": op, "args": longer})


def test_1475_the_owner_proof_of_a_hub_joined_event_is_key_material():
    rng = random.Random(1475)
    ev = {
        "hk_root_pub": forced(65, rng),
        "owner_proof": {"o": {"payload": {"hk_root_pub": forced(65, rng)}}, "sig": forced(64, rng)},
    }
    assert not secret_in_request({"op": "human.submit", "args": {"events": [ev]}})
    ev["owner_proof"]["o"]["payload"]["title"] = TOKEN
    assert secret_in_request({"op": "human.submit", "args": {"events": [ev]}})
