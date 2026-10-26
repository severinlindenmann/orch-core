"""#825: a valid signature must never be refused as a GitHub token by chance; nothing else is exempted."""

from __future__ import annotations

import random

import pytest

from orch import crypto
from orch.host.auth import GITHUB_TOKEN_SHAPE, secret_in_request

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0"  # ghp_ + 20 chars
SIG = TOKEN + "A" * (86 - len(TOKEN) - 1) + "A"  # 86 chars, canonical (last char 'A'), contains the token shape


def submit(sig, **event):
    return {"op": "human.submit", "args": {"request_id": "r", "events": [{"id": "e", "sig": sig, **event}]}}


def test_the_fixture_is_a_valid_signature_with_a_token_shape():
    assert len(SIG) == 86 and len(crypto.unb64u(SIG, crypto.SIG_LEN)) == 64


def test_a_signed_submit_whose_signature_contains_a_token_shape_is_accepted():
    assert not secret_in_request(submit(SIG))


@pytest.mark.parametrize("field", ["note", "title", "path", "label"])
def test_the_same_token_in_free_text_is_refused(field):
    assert secret_in_request(submit(SIG, **{field: TOKEN}))
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"sig": SIG, "x": {field: TOKEN}}]}})


def test_a_sig_of_the_wrong_shape_is_refused():
    for bad in (SIG[:-1], SIG + "A", TOKEN, TOKEN + "x" * 40, SIG[:-1] + "B", SIG.replace("A", "+", 1)):
        assert secret_in_request(submit(bad)), bad


def test_a_sig_elsewhere_or_on_another_op_is_not_exempt():
    assert secret_in_request({"op": "ticket.note", "args": {"events": [{"sig": SIG}]}})
    assert secret_in_request({"op": "human.submit", "args": {"sig": SIG}})
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"other": SIG}]}})
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"sig": SIG}]}, "opts": {"x": SIG}})
    assert secret_in_request({"op": "human.submit", "args": {"events": [{"sig": SIG, SIG: 1}]}})


def test_a_signature_still_gets_the_grant_checks():
    grant = "gr_0" + "0" * 25 + "." + "A" * 43
    secret = "S3cretS3cretS3cret"
    frame = submit(SIG)
    frame["auth"] = {"grant": "gr_x." + secret}
    assert not secret_in_request(frame)
    frame["args"]["events"][0]["sig"] = grant
    assert secret_in_request(frame)


def _forced(rng):
    """A canonical 64-byte signature value whose base64url text contains ``gh?_`` plus 20 or more token chars."""
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
            return text


def test_ten_thousand_forced_token_shaped_signatures_are_accepted():
    """Fails without the exemption: every value here matches the token shape (so the old guard refused it)."""
    rng = random.Random(825)
    for _ in range(10_000):
        sig = _forced(rng)
        assert GITHUB_TOKEN_SHAPE.search(sig) and len(crypto.unb64u(sig, crypto.SIG_LEN)) == 64
        assert not secret_in_request(submit(sig))


def test_ten_thousand_random_signatures_are_accepted():
    rng = random.Random(826)
    for _ in range(10_000):
        assert not secret_in_request(submit(crypto.b64u(rng.randbytes(64))))


def test_the_presented_grant_secret_inside_a_signature_shaped_value_is_still_refused():
    secret = crypto.b64u(bytes(range(32)))  # 43 chars, like a grant secret
    sig = (("A" * 5) + secret + "A" * 86)[:85] + "A"
    assert len(crypto.unb64u(sig, crypto.SIG_LEN)) == 64
    frame = submit(sig)
    frame["auth"] = {"grant": "gr_x." + secret}
    assert secret_in_request(frame)
    frame["auth"] = {"grant": "gr_x." + crypto.b64u(bytes(32))}
    assert not secret_in_request(frame)


def test_the_outcome_record_keeps_a_digest_of_each_signature_not_the_raw_value():
    from orch.host.human import _sigs_digested

    args = {"request_id": "r", "events": [{"id": "e", "sig": SIG, "note": "n"}, {"id": "f"}]}
    out = _sigs_digested(args)
    assert SIG not in repr(out) and out["events"][0]["sig"].startswith("sha256:") and out["request_id"] == "r"
    assert args["events"][0]["sig"] == SIG and out["events"][1] == {"id": "f"}
