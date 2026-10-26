"""B9 step 3: the secrets are generated in core, shown once, confirmed in core, five tries."""

from __future__ import annotations

import logging

import pytest

from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError
from orch.setup.steps import Secrets

from .conftest import PROFILE, make_env, post


def err(fn, *a, **k) -> SetupError:
    with pytest.raises(SetupError) as e:
        fn(*a, **k)
    return e.value


def reveal(f):
    return post(f, "secrets/reveal")[1]


def words_for(r, wrong=False):
    return {str(p): ("zzzz" if wrong else r["recovery_words"][p - 1]) for p in r["check_positions"]}


def test_reveal_needs_the_approval_step_first(flow):
    assert err(post, flow, "secrets/reveal").code == "setup.phase"


def test_reveal_once_then_410(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    assert len(r["recovery_words"]) == 24 and len(r["check_positions"]) == 3
    assert r["check_positions"] == sorted(set(r["check_positions"]))
    assert isinstance(r["passphrase"], str) and len(r["passphrase"].split()) == 6
    e = err(post, flow, "secrets/reveal")
    assert e.status == 410 and e.code == "secrets.shown"


def test_one_click_path_shows_the_recovery_code_only(flow):
    post(flow, "profile", {**PROFILE, "method": "click"})
    assert reveal(flow)["passphrase"] is None


def test_confirm_in_core_moves_to_review(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    assert post(flow, "secrets/confirm", {"words": words_for(r)})[1] == {"ok": True}
    assert flow.phase == "review" and flow.secrets.confirmed


def test_confirm_before_reveal_refused(flow):
    post(flow, "profile", PROFILE)
    assert err(post, flow, "secrets/confirm", {"words": {}}).code == "secrets.not_shown"


def test_confirm_must_name_exactly_the_asked_positions(flow):
    post(flow, "profile", PROFILE)
    r = reveal(flow)
    w = words_for(r)
    w.pop(next(iter(w)))
    assert err(post, flow, "secrets/confirm", {"words": w}).code == "invalid.input"


def test_five_wrong_tries_end_the_session_and_new_secrets_come_next(tmp):
    f = SetupFlow(make_env(tmp))
    f.launch(str(tmp.work))
    post(f, "profile", PROFILE)
    r = reveal(f)
    left = []
    for _ in range(5):
        out = post(f, "secrets/confirm", {"words": words_for(r, wrong=True)})[1]
        left.append(out.get("attempts_left"))
    assert left == [4, 3, 2, 1, 0] and f.phase == "cancelled" and f.error["code"] == "secrets.exhausted"
    assert f.secrets.dead  # wiped
    # a new session (a new `orch setup`) draws different secrets
    f2 = SetupFlow(make_env(tmp))
    f2.launch(str(tmp.work))
    post(f2, "profile", PROFILE)
    assert reveal(f2)["recovery_words"] != r["recovery_words"]


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
    post(flow, "secrets/confirm", {"words": words_for(r, wrong=True)})
    blob = repr(flow.dispatch("GET", "state", {}, None)) + caplog.text + repr(flow.error)
    assert not any(w in blob for w in (r["passphrase"], " ".join(r["recovery_words"])))
    assert secrets_text


def test_cancel_wipes_the_secrets(flow):
    post(flow, "profile", PROFILE)
    reveal(flow)
    s = flow.secrets
    post(flow, "cancel")
    assert s.dead and flow.secrets is None
