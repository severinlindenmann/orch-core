"""B9 step 3: the secrets are generated in core, shown once, confirmed in core, five tries."""

from __future__ import annotations

import logging

import pytest

from orch.setup.protocol import SetupError
from orch.setup.steps import Secrets

from .conftest import PROFILE, post


def err(fn, *a, **k) -> SetupError:
    with pytest.raises(SetupError) as e:
        fn(*a, **k)
    return e.value


def reveal(f):
    return post(f, "secrets/reveal")[1]


def test_reveal_needs_the_approval_step_first(flow):
    assert err(post, flow, "secrets/reveal").code == "setup.phase"


def test_reveal_once_then_410(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    assert len(r["recovery_words"]) == 24 and "check_positions" not in r
    assert isinstance(r["passphrase"], str) and len(r["passphrase"].split()) == 6
    e = err(post, flow, "secrets/reveal")
    assert e.status == 410 and e.code == "secrets.shown"


def test_one_click_path_shows_the_recovery_code_only(flow):
    post(flow, "profile", {**PROFILE, "method": "click"})
    assert reveal(flow)["passphrase"] is None


def test_confirm_in_core_moves_to_review(flow):
    post(flow, "profile", PROFILE)
    reveal(flow)
    assert post(flow, "secrets/confirm", {"acknowledged": True})[1] == {"ok": True}
    assert flow.phase == "review" and flow.secrets.confirmed


def test_confirm_before_reveal_refused(flow):
    post(flow, "profile", PROFILE)
    assert err(post, flow, "secrets/confirm", {"acknowledged": True}).code == "secrets.not_shown"


def test_confirm_needs_acknowledged_true_and_nothing_else_counts(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    for bad in ({}, {"acknowledged": False}, {"acknowledged": "yes"}, {"words": {"1": r["recovery_words"][0]}}):
        assert err(post, flow, "secrets/confirm", bad).code == "invalid.input"
    assert not flow.secrets.confirmed and flow.phase != "review"
    assert post(flow, "secrets/confirm", {"acknowledged": True})[1] == {"ok": True}
    assert flow.secrets.confirmed and flow.phase == "review"


def test_there_is_no_attempts_counter_or_check_position(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    assert "check_positions" not in r and not hasattr(flow.secrets, "attempts_left")
    for _ in range(8):  # wrong bodies never wipe the session
        err(post, flow, "secrets/confirm", {"acknowledged": False})
    assert not flow.secrets.dead and post(flow, "secrets/confirm", {"acknowledged": True})[1] == {"ok": True}


def test_create_and_review_still_need_the_acknowledgement(flow):
    post(flow, "profile", PROFILE)
    reveal(flow)
    assert err(post, flow, "review").code in ("setup.phase", "secrets.not_confirmed")
    assert err(post, flow, "create", {"review_id": "x", "digest": "y"}).code in (
        "review.changed",
        "secrets.not_confirmed",
        "setup.phase",
    )


def test_create_side_accessors_refuse_before_confirm():
    s = Secrets(show_passphrase=True)
    assert err(s.passphrase).code == "secrets.not_confirmed"
    assert err(s.code).code == "secrets.not_confirmed"


def test_wipe_zeroises_the_buffers():
    s = Secrets(show_passphrase=True)
    buffers = (s._pass, s._code)
    s.wipe()
    assert all(not any(b) for b in buffers) and s.dead
    assert err(s.reveal).code == "secrets.gone"


def test_repr_hides_the_secrets():
    s = Secrets(show_passphrase=True)
    assert "redacted" in repr(s) and s._pass.decode() not in repr(s)


def test_changing_the_method_after_the_reveal_draws_new_secrets(flow):
    post(flow, "profile", {**PROFILE, "method": "click"})
    first = reveal(flow)
    post(flow, "profile", PROFILE)  # now the passphrase would have to be shown: new secrets
    second = reveal(flow)
    assert second["passphrase"] and second["recovery_words"] != first["recovery_words"]


def test_the_state_and_errors_never_carry_secrets(flow, caplog):
    caplog.set_level(logging.DEBUG)
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    secrets_text = [r["passphrase"], *r["recovery_words"]]
    post(flow, "secrets/confirm", {"acknowledged": True})
    blob = repr(flow.dispatch("GET", "state", {}, None)) + caplog.text + repr(flow.error)
    assert not any(w in blob for w in (r["passphrase"], " ".join(r["recovery_words"])))
    assert secrets_text


def test_cancel_wipes_the_secrets(flow):
    post(flow, "profile", PROFILE)
    reveal(flow)
    s = flow.secrets
    post(flow, "cancel")
    assert s.dead and flow.secrets is None


def test_confirm_only_in_the_secrets_phase(flow):
    post(flow, "profile", PROFILE)
    reveal(flow)
    post(flow, "secrets/confirm", {"acknowledged": True})
    assert flow.phase == "review"
    e = err(post, flow, "secrets/confirm", {"acknowledged": True})
    assert e.code == "setup.phase" and e.status == 409 and flow.phase == "review"
