"""#832: a valid reader-declaration signature is never refused or redacted as a GitHub token by chance; nothing else is
exempted (same rule as #825: exact shape, exact path)."""

from __future__ import annotations

import base64
import json
import random

import pytest

from orch import crypto
from orch.host.auth import GITHUB_TOKEN_SHAPE, Redactor, secret_in_request
from orch.host.workspace import ok_result

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0"  # ghp_ + 20 chars
SIG = TOKEN + "A" * (86 - len(TOKEN) - 1) + "A"  # 86 chars, canonical (last char 'A'), contains the token shape
OTHER = "ghp_" + "Zy9X8w7V6u5T4s3R2q1P"


def declare(sig, **extra):
    return {"op": "reader.declare", "args": {"reader_decl": {"o": {"v": 1}, "sig": sig, **extra}}}


def file_frame(decl, name="decl.json", op="member.add", args_name=None, raw=None):
    raw = json.dumps(decl).encode() if raw is None else raw
    return {
        "op": op,
        "args": {"reader_decl": args_name or name},
        "files": {name: {"b64": base64.b64encode(raw).decode()}},
    }


def test_the_fixture_is_a_valid_signature_with_a_token_shape():
    assert GITHUB_TOKEN_SHAPE.search(SIG) and len(crypto.unb64u(SIG, crypto.SIG_LEN)) == 64


# --- reader.declare (args) ---------------------------------------------------------------------------------------


def test_a_reader_declare_whose_signature_contains_a_token_shape_is_accepted():
    assert not secret_in_request(declare(SIG))


def test_the_same_string_in_the_declaration_body_is_still_refused():
    frame = declare(SIG)
    frame["args"]["reader_decl"]["o"]["who"] = TOKEN
    assert secret_in_request(frame)
    assert secret_in_request(declare(SIG, note=TOKEN))
    assert secret_in_request(declare(SIG, other=SIG))  # a second copy of the signature at another key


@pytest.mark.parametrize(
    "bad", [SIG[:-1], SIG + "A", TOKEN, TOKEN + "x" * 40, SIG[:-1] + "B", SIG.replace("A", "+", 1)]
)
def test_a_sig_of_the_wrong_shape_is_refused(bad):
    assert secret_in_request(declare(bad))


def test_the_exemption_is_for_reader_declare_at_reader_decl_sig_only():
    assert secret_in_request({"op": "ticket.note", "args": {"reader_decl": {"sig": SIG}}})
    assert secret_in_request({"op": "reader.declare", "args": {"sig": SIG, "reader_decl": {}}})
    assert secret_in_request({"op": "reader.declare", "args": {"reader_decl": {"x": {"sig": SIG}}}})
    assert secret_in_request({"op": "reader.declare", "args": {"reader_decl": [{"sig": SIG}]}})
    assert secret_in_request({**declare(SIG), "opts": {"idem": SIG}})
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"sig": SIG}], "reader_decl": {"sig": SIG}}})


def test_the_grant_checks_still_apply_to_a_declaration_signature():
    secret = "S3cretS3cretS3cret"
    frame = declare(SIG)
    frame["auth"] = {"grant": "gr_x." + secret}
    assert not secret_in_request(frame)
    grant = "gr_0" + "0" * 25 + "." + "A" * 43
    assert secret_in_request(declare(grant))


def test_forced_token_shaped_declaration_signatures_are_accepted():
    rng = random.Random(832)
    for _ in range(2000):
        while True:
            raw = bytearray(crypto.b64u(rng.randbytes(64)).encode())
            pos = rng.randrange(0, 86 - 24)
            raw[pos : pos + 4] = b"gh" + bytes([rng.choice(b"pousr")]) + b"_"
            text = raw.decode()
            try:
                crypto.unb64u(text, crypto.SIG_LEN)
            except crypto.EncodingError:
                continue
            if GITHUB_TOKEN_SHAPE.search(text):
                break
        assert not secret_in_request(declare(text))


# --- member.add --reader-decl PATH (the declaration file, sent inline) -------------------------------------------


def test_a_declaration_file_whose_signature_contains_a_token_shape_is_accepted():
    assert not secret_in_request(file_frame({"o": {"v": 1}, "sig": SIG}))


def test_the_same_string_elsewhere_in_the_declaration_file_is_refused():
    assert secret_in_request(file_frame({"o": {"v": 1, "who": TOKEN}, "sig": SIG}))
    assert secret_in_request(file_frame({"o": {"v": 1}, "sig": SIG, "extra": 1}))  # not exactly {o, sig}
    assert secret_in_request(file_frame({"o": {"v": 1}, "sig": TOKEN + "x" * 40}))  # wrong shape


def test_only_the_file_the_op_names_is_exempt():
    assert secret_in_request(file_frame({"o": {}, "sig": SIG}, op="ticket.note"))
    frame = file_frame({"o": {}, "sig": SIG}, name="a.json", args_name="b.json")
    assert secret_in_request(frame)  # the file is not the one named by reader_decl
    frame = file_frame({"o": {}, "sig": SIG})
    frame["files"]["other.json"] = frame["files"]["decl.json"]
    assert secret_in_request(frame)
    assert secret_in_request({"op": "blob.put", "args": {"b64": frame["files"]["decl.json"]["b64"]}})


def test_the_grant_checks_still_apply_inside_a_declaration_file():
    grant = "gr_0" + "0" * 25 + "." + "A" * 43
    assert secret_in_request(file_frame({"o": {}, "sig": grant}))
    frame = file_frame({"o": {}, "sig": SIG})
    frame["auth"] = {"grant": "gr_x." + SIG[10:60]}  # the presented secret appears in the signature value
    assert secret_in_request(frame)


# --- the read side -----------------------------------------------------------------------------------------------


def listing(sig, **row):
    return ok_result({"readers": [{"device": "d_1", "decl": {"o": {"v": 1}, "sig": sig}, **row}]})


def test_reader_list_returns_a_stored_signature_that_contains_a_token_shape():
    out = Redactor(None, "reader.list").obj(listing(SIG))
    assert out["data"]["readers"][0]["decl"]["sig"] == SIG


def test_the_same_string_at_any_other_field_is_still_redacted():
    r = Redactor(None, "reader.list")
    out = r.obj(listing(SIG, note=OTHER))
    assert out["data"]["readers"][0]["note"] == "[redacted]"
    assert r.obj(listing(OTHER))["data"]["readers"][0]["decl"]["sig"] == "[redacted]"  # not signature-shaped
    out = r.obj(ok_result({"readers": [{"decl": {"o": {"who": OTHER}, "sig": SIG}}]}))
    assert out["data"]["readers"][0]["decl"]["o"]["who"] == "[redacted]"
    assert r.obj(ok_result({"other": {"decl": {"sig": SIG}}}))["data"]["other"]["decl"]["sig"] == "[redacted]"


def test_other_ops_and_the_plain_redactor_redact_the_signature_as_before():
    assert Redactor().obj(listing(SIG))["data"]["readers"][0]["decl"]["sig"] == "[redacted]"
    assert Redactor(None, "member.list").obj(listing(SIG))["data"]["readers"][0]["decl"]["sig"] == "[redacted]"
    assert Redactor(None, "reader.declare").text(SIG) == "[redacted]"


def test_the_read_side_exemption_still_redacts_the_grant_and_its_secret():
    grant = "gr_x." + "S3cretS3cretS3cret"
    r = Redactor(grant, "reader.list")
    sig = "A" * 5 + "S3cretS3cretS3cret" + "A" * 80
    sig = sig[:85] + "A"
    assert len(crypto.unb64u(sig, crypto.SIG_LEN)) == 64
    assert "S3cretS3cretS3cret" not in r.obj(listing(sig))["data"]["readers"][0]["decl"]["sig"]


def test_a_duplicate_key_in_the_declaration_file_is_never_exempt():
    """Parsers keep the last duplicate key: text under an earlier one must still be scanned (no exemption)."""
    clean_o = '{"v": 1}'
    for raw in (
        f'{{"o": {{"who": "{TOKEN}"}}, "o": {clean_o}, "sig": "{SIG}"}}',  # token under an earlier duplicate `o`
        f'{{"o": {clean_o}, "sig": "{TOKEN}", "sig": "{SIG}"}}',  # token under an earlier duplicate `sig`
        f'{{"o": {clean_o}, "sig": "{SIG}", "sig": "{SIG}"}}',  # even a repeated valid signature
    ):
        assert secret_in_request(file_frame(None, raw=raw.encode())), raw
    assert not secret_in_request(file_frame(None, raw=f'{{"o": {clean_o}, "sig": "{SIG}"}}'.encode()))


def test_the_star_in_a_read_path_matches_list_indexes_only():
    r = Redactor(None, "reader.list")
    body = {"data": {"readers": {"*": {"decl": {"sig": SIG}}}}}  # a mapping with the key "*", not a list
    assert r.obj(body)["data"]["readers"]["*"]["decl"]["sig"] == "[redacted]"
    assert r.obj(listing(SIG))["data"]["readers"][0]["decl"]["sig"] == SIG
