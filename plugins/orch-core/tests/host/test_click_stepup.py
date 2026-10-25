"""#1198 R1-6: the click signer checks a remote dashboard session's passkey step-up itself (host spec §0.8.6 10.5).

A bare signer (no host behind it) with one passkey in a private store; the server only says `remote: true`."""

from __future__ import annotations

import pytest

from orch.click.protocol import ClickError
from orch.click.signer import ClickSigner
from tests.host.test_click import Mono

# -- #1198 R1-6: the passkey step-up of a remote dashboard session, checked by the signer itself -----------------------

STEPUP_WID = "ab" * 16
STEPUP_RID = "hr_" + "0" * 26


@pytest.fixture
def stepup_world(tmp_path):
    """A bare signer (no host) with one passkey in a private store; ``ask`` runs ``begin`` and answers it."""
    import base64
    import os
    from types import SimpleNamespace

    from orch.click import passkeys as wa
    from orch.click.stepup import Stepups
    from tests.host import passkey_helpers as pk

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    authn = pk.Authenticator()
    store = pk.store_with(state / "dashboard", authn)
    mono = Mono()
    steps = Stepups(state, mono, rp_id=lambda: pk.RP, store=store)
    entry = {"workspace_id": STEPUP_WID, "prefix": "w", "root": str(tmp_path), "host_uid": os.getuid(), "peers": {}}
    signer = ClickSigner(state, workspaces=lambda: [entry], mono=mono, stepups=steps)

    def ask(digest, *, authn=authn, op="custody_off", rid=STEPUP_RID, **kw):
        args = {"workspace_id": STEPUP_WID, "view_digest": digest, "op": op}
        if op in ("custody_off", "rotate", "sign"):
            args["request_id"] = rid
        nonce = signer.handle("stepup", args)["nonce"]
        raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
        return authn.proof(nonce, wa.stepup_challenge(raw, digest), **kw)

    yield SimpleNamespace(signer=signer, ask=ask, mono=mono, authn=authn, pk=pk, wa=wa, steps=steps, store=store)
    signer.close()


DIGEST = "sha256:" + "1" * 64
SEALED_OFF = {"workspace_id": STEPUP_WID, "request_id": STEPUP_RID, "view_digest": DIGEST, "sealed": {}}


def stepup_refused(signer, op, args):
    with pytest.raises(ClickError) as caught:
        signer.handle(op, args)
    return caught.value


def passed_the_stepup_gate(signer, op, args):
    """The call got past the step-up check: whatever it answers, it is not `passkey.stepup_required`."""
    try:
        signer.handle(op, args)
    except ClickError as exc:
        assert exc.code != "passkey.stepup_required", exc
    except Exception:  # noqa: BLE001 - the bare signer has no host behind it; reaching the op is what is tested
        pass


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("custody_on", {"workspace_id": STEPUP_WID, "passphrase": "x"}),
        ("custody_on", {"workspace_id": STEPUP_WID, "resume": True}),
        ("custody_off", SEALED_OFF),
        ("rotate", SEALED_OFF),
        ("runner", {"workspace_id": STEPUP_WID, "on": True, "view_digest": DIGEST}),
        ("runner", {"workspace_id": STEPUP_WID, "on": False, "terminals": True, "view_digest": DIGEST}),
        ("sign", {"workspace_id": STEPUP_WID, "request_id": "hr_" + "0" * 26, "view_digest": DIGEST, "sealed": {}}),
        (
            "sign",
            {"workspace_id": STEPUP_WID, "request_id": "hr_" + "0" * 26, "view_digest": DIGEST, "reveal_key": "A"},
        ),
    ],
)
def test_stepup_a_forged_server_says_ok_is_refused_for_every_listed_op(stepup_world, op, args):
    """The server only says `remote: true`; with no assertion, or one that is not the signer's, nothing runs."""
    w = stepup_world
    assert stepup_refused(w.signer, op, {**args, "remote": True}).code == "passkey.stepup_required"
    assert stepup_refused(w.signer, op, {**args, "remote": True, "stepup": None}).code == "passkey.stepup_required"
    forged = {
        "nonce": "A" * 43,
        "id": "A" * 43,
        "response": {"clientDataJSON": "A", "authenticatorData": "A", "signature": "A"},
    }
    assert stepup_refused(w.signer, op, {**args, "remote": True, "stepup": forged}).code == "passkey.stepup_required"


def test_stepup_a_valid_assertion_for_the_digest_lets_the_op_through_once(stepup_world):
    w = stepup_world
    args = {**SEALED_OFF, "remote": True}
    proof = w.ask(DIGEST)
    # past the check, the op itself looks at the (empty) sealed value and says so: not a step-up refusal
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": proof}).code == "invalid.input"
    # used up: the same assertion again is a replay
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": proof}).code == "passkey.stepup_required"


def test_stepup_the_check_accepts_a_good_assertion_exactly_once(stepup_world):
    w = stepup_world
    proof = w.ask(DIGEST)
    w.steps.check(STEPUP_WID, DIGEST, "custody_off", STEPUP_RID, proof)  # does not raise
    with pytest.raises(ClickError):
        w.steps.check(STEPUP_WID, DIGEST, "custody_off", STEPUP_RID, proof)


def test_stepup_custody_on_binds_to_the_action_digest_not_to_a_view(stepup_world):
    w = stepup_world
    on = {"workspace_id": STEPUP_WID, "passphrase": "x", "remote": True}
    passed_the_stepup_gate(
        w.signer, "custody_on", {**on, "stepup": w.ask(w.wa.action_digest("custody.on", STEPUP_WID), op="custody_on")}
    )
    assert (
        stepup_refused(w.signer, "custody_on", {**on, "stepup": w.ask(DIGEST, op="custody_on")}).code
        == "passkey.stepup_required"
    )


def test_stepup_an_assertion_for_another_digest_or_workspace_is_refused(stepup_world):
    w = stepup_world
    other = "sha256:" + "2" * 64
    got = stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": w.ask(other)})
    assert got.code == "passkey.stepup_required"


def test_stepup_a_nonce_is_bound_to_the_op_and_the_request(stepup_world):
    w = stepup_world
    args = {**SEALED_OFF, "remote": True}
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": w.ask(DIGEST, op="rotate")}).code == (
        "passkey.stepup_required"
    )
    other = "hr_" + "0" * 25 + "1"
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": w.ask(DIGEST, rid=other)}).code == (
        "passkey.stepup_required"
    )
    for bad in (
        {"op": "prepare"},
        {"op": "custody_on", "request_id": STEPUP_RID},  # a request where the op has none
        {"op": "sign"},  # none where it needs one
    ):
        with pytest.raises(ClickError) as caught:
            w.signer.handle("stepup", {"workspace_id": STEPUP_WID, "view_digest": DIGEST, **bad})
        assert caught.value.code == "invalid.input"


def test_stepup_a_stale_nonce_is_refused(stepup_world):
    from orch.click.stepup import NONCE_LIFETIME

    w = stepup_world
    proof = w.ask(DIGEST)
    w.mono.t += NONCE_LIFETIME
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": proof}).code == (
        "passkey.stepup_required"
    )


def test_stepup_a_nonce_the_signer_did_not_issue_is_refused_even_with_a_good_signature(stepup_world):
    w = stepup_world
    nonce = bytes(32)
    proof = w.authn.proof(w.pk.b64u(nonce), w.wa.stepup_challenge(nonce, DIGEST))
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": proof}).code == (
        "passkey.stepup_required"
    )


def test_stepup_a_bad_assertion_still_uses_up_the_nonce(stepup_world):
    w = stepup_world
    proof = w.ask(DIGEST, origin="https://evil.example.com")
    args = {**SEALED_OFF, "remote": True}
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": proof}).code == "passkey.stepup_required"
    good = w.authn.proof(
        proof["nonce"], w.wa.stepup_challenge(w.wa.b64u_decode(proof["nonce"], 32), DIGEST)
    )  # right signature, but the nonce is gone
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": good}).code == "passkey.stepup_required"


def test_stepup_an_unknown_passkey_or_another_relying_party_is_refused(stepup_world):
    w = stepup_world
    stranger = w.pk.Authenticator()
    args = {**SEALED_OFF, "remote": True}
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": w.ask(DIGEST, authn=stranger)}).code == (
        "passkey.stepup_required"
    )
    elsewhere = w.pk.Authenticator("other.example.com")
    w.store.add(elsewhere.record("Other"))
    assert stepup_refused(w.signer, "custody_off", {**args, "stepup": w.ask(DIGEST, authn=elsewhere)}).code == (
        "passkey.stepup_required"
    )


def test_stepup_without_user_verification_is_refused(stepup_world):
    w = stepup_world
    proof = w.ask(DIGEST, flags=w.pk.UP)
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": proof}).code == (
        "passkey.stepup_required"
    )


def test_stepup_a_store_that_cannot_be_trusted_refuses(stepup_world):
    import os

    w = stepup_world
    proof = w.ask(DIGEST)
    os.chmod(w.store.path, 0o644)
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": proof}).code == (
        "passkey.stepup_required"
    )


def test_stepup_no_configured_name_refuses(stepup_world):
    w = stepup_world
    w.steps._rp_id = lambda: None
    proof = w.ask(DIGEST)
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": True, "stepup": proof}).code == (
        "passkey.stepup_required"
    )


def test_stepup_plain_variants_and_local_calls_need_none(stepup_world):
    """Off, the review, a runner off, a sign without a typed secret: a plain click for a remote session too. A call
    that does not say `remote` is the local one."""
    w = stepup_world
    plain = [
        ("custody_off", {"workspace_id": STEPUP_WID}),
        ("custody_off", {"workspace_id": STEPUP_WID, "begin": True}),
        ("rotate", {"workspace_id": STEPUP_WID, "begin": True}),
        ("runner", {"workspace_id": STEPUP_WID, "on": True, "review": True}),
        ("runner", {"workspace_id": STEPUP_WID, "on": False, "view_digest": DIGEST}),
        ("sign", {"workspace_id": STEPUP_WID, "request_id": "hr_" + "0" * 26, "view_digest": DIGEST}),
    ]
    for op, args in plain:
        passed_the_stepup_gate(w.signer, op, {**args, "remote": True})
        passed_the_stepup_gate(w.signer, op, args)
    for op, args in (("custody_off", SEALED_OFF), ("custody_on", {"workspace_id": STEPUP_WID, "passphrase": "x"})):
        passed_the_stepup_gate(w.signer, op, args)  # local: no step-up


def test_stepup_the_arguments_are_closed(stepup_world):
    w = stepup_world
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "stepup": {}}).code == "invalid.input"
    assert stepup_refused(w.signer, "custody_off", {**SEALED_OFF, "remote": "yes"}).code == "invalid.input"
    for bad in (
        {"workspace_id": STEPUP_WID},
        {"workspace_id": STEPUP_WID, "view_digest": "x", "op": "runner"},
    ):
        assert stepup_refused(w.signer, "stepup", bad).code == "invalid.input"
    assert "passkey_stepup" in w.signer.status({})["features"]


def test_stepup_nonces_are_bounded(stepup_world):
    from orch.click.stepup import MAX_NONCES

    w = stepup_world
    for _ in range(MAX_NONCES + 10):
        w.signer.handle("stepup", {"workspace_id": STEPUP_WID, "view_digest": DIGEST, "op": "runner"})
    assert len(w.steps.nonces) <= MAX_NONCES
