"""The sealed field (#1198 C5, docs/architecture/orch-v2-sealed-field.md): construction, vectors, adversarial cases.

``tests/click_sealed_vectors.json`` is the cross-side contract (the browser side, C6, runs the same file). It is
deterministic; regenerate it with ``uv run python tests/host/test_click_sealed.py`` only on a contract change.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orch.click import sealed
from orch.click.sealed import Context, Recipient, SealedError, b64url, seal

VECTORS = Path(__file__).resolve().parents[1] / "click_sealed_vectors.json"

WS = "0123456789abcdef0123456789abcdef"
WS2 = "fedcba9876543210fedcba9876543210"
RID = "hr_01JB2C3D4E5F6G7H8J9K0MNPQR"
RID2 = "hr_01JB2C3D4E5F6G7H8J9K0MNPQS"
VD = "sha256:" + "ab" * 32
VD2 = "sha256:" + "cd" * 32


def _key(name: str) -> ec.EllipticCurvePrivateKey:
    n = int.from_bytes(hashlib.sha256(b"orch-sealed-vector/" + name.encode()).digest(), "big")
    order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    return ec.derive_private_key(n % (order - 1) + 1, ec.SECP256R1())


def _jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    nums = key.private_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "d": b64url(nums.private_value.to_bytes(32, "big")),
        "x": b64url(nums.public_numbers.x.to_bytes(32, "big")),
        "y": b64url(nums.public_numbers.y.to_bytes(32, "big")),
    }


def _ctx(d: dict[str, Any]) -> Context:
    return Context(d["purpose"], d["workspace_id"], d["request_id"], d["view_digest"])


def _ctxd(purpose: str, ws: str = WS, rid: str = RID, vd: str = VD) -> dict[str, str]:
    return {"purpose": purpose, "workspace_id": ws, "request_id": rid, "view_digest": vd}


SEAL_CASES: list[tuple[str, str, str]] = [
    ("in_hub_backup_key", "hub.backup_key", "orch-backup-7Q4M-2KX9-ZP3W-8HDN-5RTV-JC6B"),
    ("in_recovery_code", "device.recovery_code", "TIGER-OCEAN-MAPLE-RIVER-SEVEN-CLOUD"),
    ("in_passphrase_utf8", "custody.passphrase", "Grüne Wiese am Fluss 🌿 7"),
    ("in_github_token", "github.token", "example-token-not-a-real-pat-0123456789abcdef"),
    ("in_passphrase_254_one_block", "custody.passphrase", "p" * 254),
    ("in_passphrase_255_two_blocks", "custody.passphrase", "q" * 255),
    ("out_grant_secret", "grant.secret", "orch_gs_3vJ9xQ2mT7wL5nR8kB4cF6hD1pZ0yA"),
]


def _seal_vector(name: str, purpose: str, text: str) -> dict[str, Any]:
    rcpt, eph = _key(name + "/recipient"), _key(name + "/ephemeral")
    ctxd = _ctxd(purpose)
    ctx = _ctx(ctxd)
    rpk = sealed.encode_public(rcpt.public_key())
    env = sealed._seal_with(eph, rpk, text, ctx)
    epk_raw, rpk_raw = sealed._unb64url(env["epk"], "epk"), sealed._unb64url(rpk, "rpk")
    shared = eph.exchange(ec.ECDH(), rcpt.public_key())
    ad = sealed.aad(ctx, epk_raw, rpk_raw)
    key, nonce = sealed.derive(shared, ad)
    return {
        "name": name,
        "direction": ctx.direction,
        "context": ctxd,
        "plaintext": text,
        "plaintext_hex": text.encode().hex(),
        "recipient_jwk": _jwk(rcpt),
        "recipient_public": rpk,
        "ephemeral_jwk": _jwk(eph),
        "shared_secret_hex": shared.hex(),
        "aad_hex": ad.hex(),
        "key_hex": key.hex(),
        "nonce_hex": nonce.hex(),
        "padded_hex": sealed.pad(text.encode()).hex(),
        "envelope": env,
    }


def _flip(b64: str, index: int) -> str:
    raw = bytearray(sealed._unb64url(b64, "x"))
    raw[index] ^= 0x01
    return b64url(bytes(raw))


def _negate(epk: str) -> str:
    """-E: the same x coordinate (so the same ECDH secret), the other y; only E in the AAD tells them apart."""
    raw = sealed._unb64url(epk, "epk")
    p = 2**256 - 2**224 + 2**192 + 2**96 - 1
    return b64url(raw[:33] + (p - int.from_bytes(raw[33:], "big")).to_bytes(32, "big"))


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _noncanonical(text: str) -> str:
    """The same bytes with an unused low bit of the last character set (a second spelling a lax decoder accepts)."""
    assert len(text) % 4 in (2, 3)
    return text[:-1] + _ALPHABET[_ALPHABET.index(text[-1]) | 1]


def _open_cases(seals: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def add(name: str, base: str, *, expect: str, envelope: Any = None, context: dict[str, str] | None = None,
            recipient: dict[str, str] | None = None, direction: str | None = None,
            bound: dict[str, str] | None = None) -> None:  # fmt: skip
        b = seals[base]
        ctxd = context or b["context"]
        cases.append({
            "name": name,
            "base": base,
            "direction": direction or b["direction"],
            "recipient_jwk": recipient or b["recipient_jwk"],
            "bound": bound or {"workspace_id": ctxd["workspace_id"], "request_id": ctxd["request_id"]},
            "context": ctxd,
            "envelope": envelope if envelope is not None else b["envelope"],
            "expect": expect,
        })  # fmt: skip

    for base in ("in_github_token", "out_grant_secret"):
        b = seals[base]
        env, p = b["envelope"], b["context"]["purpose"]
        add(f"{base}/ok", base, expect="ok")
        add(f"{base}/other_request", base, expect="open_failed", context=_ctxd(p, rid=RID2))
        add(f"{base}/other_view_digest", base, expect="open_failed", context=_ctxd(p, vd=VD2))
        add(f"{base}/other_workspace", base, expect="open_failed", context=_ctxd(p, ws=WS2))
        add(f"{base}/bound_to_other_request", base, expect="context",
            bound={"workspace_id": WS, "request_id": RID2})  # fmt: skip
        add(f"{base}/bound_to_other_workspace", base, expect="context",
            bound={"workspace_id": WS2, "request_id": RID})  # fmt: skip
        add(f"{base}/other_recipient", base, expect="open_failed", recipient=_jwk(_key(base + "/someone-else")))
        add(f"{base}/ct_bit_flipped", base, expect="open_failed", envelope={**env, "ct": _flip(env["ct"], 0)})
        add(f"{base}/tag_bit_flipped", base, expect="open_failed", envelope={**env, "ct": _flip(env["ct"], -1)})
        raw_ct = sealed._unb64url(env["ct"], "ct")
        add(f"{base}/ct_truncated", base, expect="malformed", envelope={**env, "ct": b64url(raw_ct[:-1])})
        add(f"{base}/ct_tag_only", base, expect="malformed", envelope={**env, "ct": b64url(raw_ct[-16:])})
        raw_epk = sealed._unb64url(env["epk"], "epk")
        add(f"{base}/epk_truncated", base, expect="malformed", envelope={**env, "epk": b64url(raw_epk[:-1])})
        add(f"{base}/epk_compressed", base, expect="malformed",
            envelope={**env, "epk": b64url(bytes([2 + (raw_epk[-1] & 1)]) + raw_epk[1:33])})  # fmt: skip
        add(f"{base}/epk_off_curve", base, expect="malformed", envelope={**env, "epk": _flip(env["epk"], 64)})
        add(f"{base}/epk_other_key", base, expect="open_failed",
            envelope={**env, "epk": sealed.encode_public(_key(base + "/mallory").public_key())})  # fmt: skip
        add(f"{base}/epk_negated", base, expect="open_failed", envelope={**env, "epk": _negate(env["epk"])})
        add(f"{base}/epk_noncanonical_last_char", base, expect="malformed",
            envelope={**env, "epk": _noncanonical(env["epk"])})  # fmt: skip
        add(f"{base}/ct_noncanonical_last_char", base, expect="malformed",
            envelope={**env, "ct": _noncanonical(env["ct"])})  # fmt: skip
        add(f"{base}/epk_is_recipient", base, expect="malformed", envelope={**env, "epk": b["recipient_public"]})
        add(f"{base}/extra_field", base, expect="malformed", envelope={**env, "purpose": p})
        add(f"{base}/missing_alg", base, expect="malformed", envelope={k: v for k, v in env.items() if k != "alg"})
        add(f"{base}/version_2", base, expect="malformed", envelope={**env, "v": 2})
        add(f"{base}/version_true", base, expect="malformed", envelope={**env, "v": True})
        add(f"{base}/other_alg", base, expect="malformed", envelope={**env, "alg": "X25519-HKDF-SHA256-A256GCM"})
        add(f"{base}/ct_padded_base64", base, expect="malformed", envelope={**env, "ct": env["ct"] + "="})
        add(f"{base}/ct_std_base64", base, expect="malformed", envelope={**env, "ct": env["ct"][:-1] + "+"})
        add(f"{base}/not_an_object", base, expect="malformed", envelope=[env["epk"], env["ct"]])
    # swapped purpose within a direction: the same envelope, the purpose the signer expected for another field
    add("in_github_token/swapped_purpose", "in_github_token", expect="open_failed",
        context=_ctxd("custody.passphrase"))  # fmt: skip
    add("in_recovery_code/swapped_purpose", "in_recovery_code", expect="open_failed",
        context=_ctxd("hub.backup_key"))  # fmt: skip
    # across directions: an input purpose at the browser, the grant secret at the signer
    add("out_grant_secret/opened_as_input", "out_grant_secret", expect="context",
        context=_ctxd("github.token"), direction="out")  # fmt: skip
    add("in_github_token/opened_as_output", "in_github_token", expect="context",
        context=_ctxd("grant.secret"), direction="in")  # fmt: skip
    # authenticates but the padding is wrong (nonzero tail, wrong length, an extra block)
    for name, padded in (
        ("pad_nonzero_tail", (5).to_bytes(2, "big") + b"hello" + b"\x00" * 248 + b"\x01"),
        ("pad_length_zero", b"\x00" * BLOCK_),
        ("pad_length_too_big", (255).to_bytes(2, "big") + b"x" * 254),
        ("pad_extra_block", (5).to_bytes(2, "big") + b"hello" + b"\x00" * (2 * BLOCK_ - 7)),
    ):
        for base in ("in_github_token", "out_grant_secret"):
            b = seals[base]
            ctx = _ctx(b["context"])
            eph = _key(f"{base}/{name}")
            rpk = sealed.decode_public(b["recipient_public"])
            rcpt_raw = rpk[1]
            epk = sealed.encode_public(eph.public_key())
            ad = sealed.aad(ctx, sealed._unb64url(epk, "epk"), rcpt_raw)
            k, n = sealed.derive(eph.exchange(ec.ECDH(), rpk[0]), ad)
            env = {"v": 1, "alg": sealed.ALG, "epk": epk, "ct": b64url(AESGCM(k).encrypt(n, padded, ad))}
            add(f"{base}/{name}", base, expect="malformed", envelope=env)
    return cases


BLOCK_ = sealed.BLOCK


def build_vectors() -> dict[str, Any]:
    seals = {name: _seal_vector(name, purpose, text) for name, purpose, text in SEAL_CASES}
    return {
        "format": "orch.sealed-vectors/1",
        "alg": sealed.ALG,
        "spec": "docs/architecture/orch-v2-sealed-field.md",
        "notes": [
            "Keys are JWK (P-256); public keys and envelope fields are base64url without padding.",
            "seal: with ephemeral_jwk fixed, sealing plaintext to recipient_public for context gives exactly "
            "envelope; every intermediate (shared_secret, aad, key, nonce, padded) is given.",
            "open: the recipient (direction in = the signer, out = the browser), bound to bound.workspace_id and "
            "bound.request_id when it was made, opens envelope for context; expect is 'ok' (the plaintext of the base "
            "seal vector) or a refusal reason.",
            "replay and the view_digest pin are stateful (a second open of the same epk is 'replay'; after a first "
            "open, another view_digest is 'context') and are not vectors.",
        ],
        "seal": list(seals.values()),
        "open": _open_cases(seals),
    }


# ---------------------------------------------------------------------------------------------------- the vectors


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    return json.loads(VECTORS.read_text())


def test_vectors_file_is_current(vectors):
    assert vectors == build_vectors(), "regenerate: uv run python tests/host/test_click_sealed.py"


def test_vectors_cover_every_purpose_and_both_directions(vectors):
    assert {v["context"]["purpose"] for v in vectors["seal"]} == set(sealed.PURPOSES)
    assert {v["direction"] for v in vectors["open"]} == {"in", "out"}
    assert {v["expect"] for v in vectors["open"]} >= {"ok", "open_failed", "malformed", "context"}


def _private(jwk: dict[str, str]) -> ec.EllipticCurvePrivateKey:
    d = int.from_bytes(sealed._unb64url(jwk["d"], "d"), "big")
    key = ec.derive_private_key(d, ec.SECP256R1())
    nums = key.public_key().public_numbers()
    assert (b64url(nums.x.to_bytes(32, "big")), b64url(nums.y.to_bytes(32, "big"))) == (jwk["x"], jwk["y"])
    return key


def test_seal_vectors_reproduce(vectors):
    for v in vectors["seal"]:
        ctx = _ctx(v["context"])
        eph, rcpt = _private(v["ephemeral_jwk"]), _private(v["recipient_jwk"])
        assert sealed.encode_public(rcpt.public_key()) == v["recipient_public"]
        assert sealed._seal_with(eph, v["recipient_public"], v["plaintext"], ctx) == v["envelope"], v["name"]
        shared = rcpt.exchange(ec.ECDH(), eph.public_key())  # the recipient side derives the same x coordinate
        assert shared.hex() == v["shared_secret_hex"]
        epk_raw = sealed._unb64url(v["envelope"]["epk"], "epk")
        ad = sealed.aad(ctx, epk_raw, sealed._unb64url(v["recipient_public"], "rpk"))
        assert ad.hex() == v["aad_hex"]
        assert [x.hex() for x in sealed.derive(shared, ad)] == [v["key_hex"], v["nonce_hex"]]
        assert bytes.fromhex(v["plaintext_hex"]).decode() == v["plaintext"]


def test_open_vectors(vectors):
    plain = {v["name"]: v["plaintext"] for v in vectors["seal"]}
    for case in vectors["open"]:
        b = case["bound"]
        key = _private(case["recipient_jwk"])
        rcpt = Recipient._for_vectors(case["direction"], b["workspace_id"], b["request_id"], key)
        try:
            got = rcpt.open(case["envelope"], _ctx(case["context"]))
        except SealedError as exc:
            assert exc.reason == case["expect"], case["name"]
        else:
            assert case["expect"] == "ok", case["name"]
            assert got.decode() == plain[case["base"]], case["name"]


# ---------------------------------------------------------------------------------------------- the module itself


def _pair(purpose: str = "github.token", secret: str = "s3cret") -> tuple[Recipient, dict[str, Any], Context]:
    ctx = Context(purpose, WS, RID, VD)
    rcpt = Recipient(ctx.direction, workspace_id=WS, request_id=RID)
    return rcpt, seal(rcpt.public, secret, ctx), ctx


def _rcpt(direction: str) -> Recipient:
    return Recipient(direction, workspace_id=WS, request_id=RID)


def test_no_way_to_pass_a_key_in():
    """Finding 3: the public API makes every key itself (a reused ephemeral key reuses key and nonce)."""
    import inspect

    assert list(inspect.signature(seal).parameters) == ["recipient_public", "secret", "ctx"]
    assert set(inspect.signature(Recipient).parameters) == {"direction", "workspace_id", "request_id"}


def test_unpad_returns_a_wipeable_slice():
    out = sealed.unpad(sealed.pad(b"hello"))
    assert type(out) is bytearray and out == bytearray(b"hello")


def test_round_trip_both_directions():
    for purpose, direction in sealed.PURPOSES.items():
        rcpt, env, ctx = _pair(purpose, "geheim ✓")
        assert rcpt.direction == direction
        assert rcpt.open(env, ctx) == bytearray("geheim ✓".encode())


def test_every_seal_uses_a_fresh_ephemeral_key():
    rcpt, ctx = _rcpt("in"), Context("hub.backup_key", WS, RID, VD)
    a, b = seal(rcpt.public, "same", ctx), seal(rcpt.public, "same", ctx)
    assert a["epk"] != b["epk"] and a["ct"] != b["ct"]
    assert rcpt.open(a, ctx) == rcpt.open(b, ctx) == bytearray(b"same")


def test_replay_is_refused():
    rcpt, env, ctx = _pair()
    rcpt.open(env, ctx)
    with pytest.raises(SealedError) as exc:
        rcpt.open(copy.deepcopy(env), ctx)
    assert exc.value.reason == "replay"


def test_a_failed_open_does_not_burn_the_envelope():
    rcpt, env, ctx = _pair()
    with pytest.raises(SealedError):
        rcpt.open({**env, "ct": _flip(env["ct"], 0)}, ctx)
    with pytest.raises(SealedError):
        rcpt.open(env, Context("github.token", WS, RID, VD2))  # no pin yet: the AAD refuses it
    assert rcpt.open(env, ctx) == bytearray(b"s3cret")


def test_envelope_of_another_request_does_not_open():
    """Two approvals, two keys: an envelope for request 1 does not open at request 2's key, and a recipient refuses
    any context but its own request's, whatever its caller passes (the C10 wiring-bug case)."""
    r1, env1, ctx1 = _pair()
    r2 = Recipient("in", workspace_id=WS, request_id=RID2)
    ctx2 = Context("github.token", WS, RID2, VD2)
    for rcpt, ctx, reason in ((r2, ctx2, "open_failed"), (r2, ctx1, "context"), (r1, ctx2, "context")):
        with pytest.raises(SealedError) as exc:
            rcpt.open(env1, ctx)
        assert exc.value.reason == reason
    r3 = Recipient("in", workspace_id=WS2, request_id=RID)
    with pytest.raises(SealedError) as exc:
        r3.open(seal(r3.public, "x", ctx1), ctx1)  # sealed to r3's key, but for another workspace's request
    assert exc.value.reason == "context"


def test_the_first_open_pins_the_view_digest():
    rcpt, ctx = _rcpt("in"), Context("hub.backup_key", WS, RID, VD)
    assert rcpt.view_digest is None
    rcpt.open(seal(rcpt.public, "a", ctx), ctx)
    assert rcpt.view_digest == VD
    other = Context("custody.passphrase", WS, RID, VD2)
    with pytest.raises(SealedError) as exc:
        rcpt.open(seal(rcpt.public, "b", other), other)  # sealed correctly, but for another review of the request
    assert exc.value.reason == "context"
    retry = Context("hub.backup_key", WS, RID, VD)
    assert rcpt.open(seal(rcpt.public, "c", retry), retry) == bytearray(b"c")  # a retyped value, same review


def test_recipient_binding_is_validated():
    for ws, rid in ((WS.upper(), RID), (WS, "hr_short"), (None, RID), (WS, None)):
        with pytest.raises(SealedError) as exc:
            Recipient("in", workspace_id=ws, request_id=rid)
        assert exc.value.reason == "context"
    with pytest.raises(TypeError):
        Recipient("in")  # a recipient always belongs to one request


def test_closed_recipient_refuses():
    rcpt, env, ctx = _pair()
    rcpt.close()
    assert rcpt.closed
    with pytest.raises(SealedError) as exc:
        rcpt.open(env, ctx)
    assert exc.value.reason == "closed"


def test_direction_is_structural():
    """The signer's input key never opens a grant secret, the browser's key never an input secret."""
    sig, br = _rcpt("in"), _rcpt("out")
    out_ctx, in_ctx = Context("grant.secret", WS, RID, VD), Context("custody.passphrase", WS, RID, VD)
    for rcpt, ctx in ((sig, out_ctx), (br, in_ctx)):
        env = seal(rcpt.public, "x", ctx)
        with pytest.raises(SealedError) as exc:
            rcpt.open(env, ctx)
        assert exc.value.reason == "context"
    with pytest.raises(ValueError):
        _rcpt("both")


@pytest.mark.parametrize(
    "fields",
    [
        ("hub.secret", WS, RID, VD),
        ("github.token", WS.upper(), RID, VD),
        ("github.token", WS, "hr_short", VD),
        ("github.token", WS, RID, "sha256:" + "AB" * 32),
        ("github.token", WS, RID, VD[:-1]),
        ("github.token", WS, None, VD),
    ],
)
def test_context_is_validated(fields):
    with pytest.raises(SealedError) as exc:
        Context(*fields)
    assert exc.value.reason == "context"


def test_size_limits():
    rcpt, ctx = _rcpt("in"), Context("custody.passphrase", WS, RID, VD)
    with pytest.raises(SealedError) as exc:
        seal(rcpt.public, "", ctx)
    assert exc.value.reason == "malformed"
    with pytest.raises(SealedError) as exc:
        seal(rcpt.public, b"x" * (sealed.MAX_SECRET + 1), ctx)
    assert exc.value.reason == "too_large"
    env = seal(rcpt.public, b"x" * sealed.MAX_SECRET, ctx)
    assert len(sealed._unb64url(env["ct"], "ct")) == sealed.MAX_PADDED + sealed.TAG
    assert rcpt.open(env, ctx) == bytearray(b"x" * sealed.MAX_SECRET)


def test_length_is_hidden_within_a_block():
    rcpt, ctx = _rcpt("in"), Context("custody.passphrase", WS, RID, VD)
    sizes = {len(seal(rcpt.public, "a" * n, ctx)["ct"]) for n in (1, 8, 100, 254)}
    assert len(sizes) == 1


def test_seal_refuses_a_bad_recipient_key():
    ctx = Context("grant.secret", WS, RID, VD)
    good = _rcpt("out").public
    for bad in (good[:-2], good + "A", "", 7, _flip(good, 64), b64url(b"\x04" + b"\x00" * 64)):
        with pytest.raises(SealedError) as exc:
            seal(bad, "x", ctx)
        assert exc.value.reason == "malformed"


def test_refusal_messages_never_carry_the_secret():
    rcpt, env, ctx = _pair("hub.backup_key", "orch-backup-TOPSECRET")
    for other in (Context("hub.backup_key", WS, RID2, VD), Context("custody.passphrase", WS, RID, VD)):
        with pytest.raises(SealedError) as exc:
            rcpt.open(env, other)
        assert "TOPSECRET" not in str(exc.value) and "TOPSECRET" not in repr(exc.value)


if __name__ == "__main__":  # regenerate the vectors (a contract change: the browser side must follow)
    VECTORS.write_text(json.dumps(build_vectors(), indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {VECTORS}", file=sys.stderr)
