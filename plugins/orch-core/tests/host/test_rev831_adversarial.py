"""[rev831] adversarial tests for the #825 signature exemption (review only, not pushed)."""

from __future__ import annotations

import random

from orch import crypto
from orch.host.auth import GITHUB_TOKEN_SHAPE, secret_in_request


def canon_sig_with(text: str, pos: int = 0) -> str:
    """A canonical 86-char base64url value of 64 bytes with ``text`` at ``pos``."""
    raw = ("A" * pos + text + "A" * 86)[:85] + "A"
    assert len(crypto.unb64u(raw, crypto.SIG_LEN)) == 64
    return raw


def submit(events, op="human.submit", **extra):
    return {"op": op, "args": {"request_id": "r", "events": events, **extra}}


REAL_CLASSIC = "ghp_" + "Z" * 36  # the real classic PAT length (40)
REAL_FINE = "github_pat_" + "1" * 22 + "_" + "Q" * 59  # 93 chars: cannot fit in 86


def test_a_full_length_classic_token_fits_in_a_canonical_sig_and_passes_the_guard():
    # documents the accepted residual: the guard alone no longer stops it; signature verification must
    sig = canon_sig_with(REAL_CLASSIC, 10)
    assert REAL_CLASSIC in sig and not secret_in_request(submit([{"sig": sig}]))


def test_a_fine_grained_token_cannot_fit():
    assert len(REAL_FINE) > 86


def test_the_presented_grant_secret_inside_a_canonical_sig_is_refused():
    secret = crypto.b64u(bytes(range(32)))  # 43 base64url chars, like a grant secret
    sig = canon_sig_with(secret, 5)
    frame = submit([{"sig": sig}])
    frame["auth"] = {"grant": "gr_0" + "0" * 25 + "." + secret}
    assert secret_in_request(frame)
    frame["auth"] = {"grant": "gr_0" + "0" * 25 + "." + crypto.b64u(bytes(32))}
    assert not secret_in_request(frame)


def test_look_alike_paths_are_scanned():
    sig = canon_sig_with("ghp_" + "a" * 20)
    looks = [
        {"op": "human.submit", "args": {"x": {"events": [{"sig": sig}]}}},
        {"op": "human.submit", "args": {"events[0].sig": sig}},
        {"op": "human.submit", "args": {"events": {"0": {"sig": sig}}}},
        {"op": "human.submit", "args": {"events": [[{"sig": sig}]]}},
        {"op": "human.submit", "args": {"events": [{"sig": [sig]}]}},
        {"op": "human.submit", "args": {"events": [{"sig": {"sig": sig}}]}},
        {"op": "human.submit", "args": {"events": [{"payload": {"sig": sig}}]}},
        {"op": "human.submit", "args": {"events": [{"sig": sig}]}, "files": {"f": {"sig": sig}}},
        {"op": "human.submit ", "args": {"events": [{"sig": sig}]}},
        {"op": "HUMAN.SUBMIT", "args": {"events": [{"sig": sig}]}},
        {"op": "relay.submit", "args": {"events": [{"sig": sig}]}},
        {"op": "human.prepare", "args": {"op": "x", "args": {"events": [{"sig": sig}]}}},
    ]
    for frame in looks:
        assert secret_in_request(frame), frame


def test_one_exempt_sig_does_not_exempt_a_sibling_event():
    sig = canon_sig_with("ghp_" + "a" * 20)
    assert secret_in_request(submit([{"sig": sig}, {"sig": sig, "note": "ghp_" + "b" * 20}]))
    assert secret_in_request(submit([{"sig": sig}, {"sig": sig[:-1]}]))
    assert secret_in_request(submit([{"sig": sig}], extra="ghp_" + "c" * 20))


def test_the_frame_is_not_mutated():
    sig = canon_sig_with("ghp_" + "a" * 20)
    frame = submit([{"sig": sig}])
    secret_in_request(frame)
    assert frame["args"]["events"][0]["sig"] == sig


def test_forced_token_shapes_at_every_position_in_random_sigs():
    rng = random.Random(8251)
    hits = 0
    for _ in range(2_000):
        raw = bytearray(crypto.b64u(rng.randbytes(64)).encode())
        pos = rng.randrange(0, 86 - 24)
        raw[pos : pos + 4] = b"gh" + bytes([rng.choice(b"pousr")]) + b"_"
        s = raw.decode()
        try:
            crypto.unb64u(s, crypto.SIG_LEN)
        except crypto.EncodingError:
            continue
        if not GITHUB_TOKEN_SHAPE.search(s):
            continue  # a '-' in the next 20 chars
        assert not secret_in_request(submit([{"sig": s}]))
        hits += 1
    assert hits > 500
