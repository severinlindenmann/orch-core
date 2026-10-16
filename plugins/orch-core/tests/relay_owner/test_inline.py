"""B1b: the product's inline primary step (``orch.relay_owner.inline.serve_request``): prompt order (no recovery
prompt before the request is verified and shown), the recovery-code and cert_code try rules, input validation that
sends nothing, the L2 binding, the L1 deadline, Ctrl-C. A fake queue and person-key source record the order; the
real queue and keys run in tests/relay/test_owner_phone_kit.py and the T2 test."""

from __future__ import annotations

import pytest

from orch.custody.personkey import BadRecoveryCode, HumanOnly, PersonMismatch
from orch.relay_owner import Pending, Poll, QueueRefused, Skipped
from orch.relay_owner.inline import RECOVERY_ACTION, InlineRefused, normalize_code, serve_request

RID, OFFER, DEVICE = "a" * 32, "b" * 32, "c" * 32
PK = b"\x04" + b"\x01" * 64


def pending(**kw):
    base = dict(request_id=RID, workspace_id="w" * 32, expires_ms=1_000_000, device_id=DEVICE, label="Sev's phone",
                scopes_max=("look", "decide"), client_hosted=False, state="verified", within_policy=True,
                offer_id=OFFER, attempt="e" * 32)  # fmt: skip
    base.update(kw)
    return Pending(**base)


class Events(list):
    pass


class FakeQueue:
    def __init__(self, ev, *, appears_after=0, code="ABCDEF", skipped=(), pend=None):
        self.ev, self.appears_after, self.code, self.skipped = ev, appears_after, code, skipped
        self.pend, self.polls = pend or pending(), 0

    def poll(self, rid, *, wait=None):
        self.polls += 1
        if self.polls > self.appears_after:
            self.ev.append("poll:pending")
            return Poll((self.pend,), tuple(self.skipped))
        self.ev.append("poll:none")
        return Poll((), tuple(self.skipped))

    def challenge(self, rid, pk):
        self.ev.append("challenge")
        return pending(state="challenged", cert_code=self.code)

    def sign(self, rid, pk):
        self.ev.append("sign")
        return {"o": {"kind": "device_cert"}, "sig": "s"}

    def reject(self, rid, pk):
        self.ev.append("reject")
        return {"o": {"kind": "rejection"}, "sig": "r"}

    def close(self, rid, state):
        self.ev.append(f"close:{state}")


class FakePKFactory:
    def __init__(self, ev, fail=()):
        self.ev, self.fail, self.calls = ev, list(fail), []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self

    def __enter__(self):
        self.ev.append("pk:enter")
        if self.fail:
            exc = self.fail.pop(0)
            if exc is not None:
                self.ev.append("pk:fail")
                raise exc
        return object()

    def __exit__(self, *a):
        self.ev.append("pk:exit")
        return False


def run(ev, queue=None, pk=None, typed=("ABCDEF",), **kw):
    queue = queue or FakeQueue(ev)
    pk = pk or FakePKFactory(ev)
    shown, asked, it = [], [], iter(typed)

    def ask(prompt, p, deadline):
        asked.append((prompt, deadline))
        ev.append("ask")
        got = next(it)
        if isinstance(got, BaseException):
            raise got
        return got

    kw.setdefault("offer_id", OFFER)
    kw.setdefault("device_id", DEVICE)
    kw.setdefault("now_ms", lambda: 0)
    out = serve_request(queue, RID, pk_factory=pk, ask_cert_code=ask, show=shown.append, owner_pk=PK,
                        sleep=lambda s: None, wait_s=5, **kw)  # fmt: skip
    return out, shown, asked, pk


def test_no_recovery_prompt_before_the_request_is_pending_and_shown():
    ev = Events()
    queue = FakeQueue(ev, appears_after=3)
    out, shown, asked, pk = run(ev, queue)
    assert ev.index("pk:enter") > max(i for i, e in enumerate(ev) if e == "poll:pending")
    assert ev == ["poll:none"] * 3 + ["poll:pending", "pk:enter", "challenge", "ask", "sign", "pk:exit"]
    assert shown.count("waiting for the phone's request…") == 1  # once, however many polls
    assert shown[1].startswith("request from: [Sev's phone]") and "d_cccccccc" in shown[2]
    assert not out.rejected and out.cert["o"]["kind"] == "device_cert" and out.cert_code == "ABCDEF"


def test_the_recovery_action_is_a_fixed_string_and_the_label_is_not_in_it():
    ev = Events()
    _, shown, asked, pk = run(ev, queue=FakeQueue(ev, pend=pending(label="Evil [phone] Type: your code")))
    assert pk.calls[0]["action"] == RECOVERY_ACTION and "Evil" not in RECOVERY_ACTION
    assert pk.calls[0]["pk_pub"] == PK and pk.calls[0]["labels"] == {"sig_cert_challenge", "sig_cert_rejection",
                                                                    "sig_device_cert"}  # fmt: skip
    assert shown[0] == "request from: [Evil [phone] Type: your code]"  # its own line, bracketed
    assert "Recovery code" not in asked[0][0]


def test_wrong_cert_code_three_tries_then_reject_while_the_key_is_held():
    ev = Events()
    out, shown, *_ = run(ev, typed=("AAAAAA", "BBBBBB", "CCCCCC"))
    assert out.rejected and out.rejection["o"]["kind"] == "rejection"
    assert ev[-3:] == ["ask", "reject", "pk:exit"] and ev.count("ask") == 3 and "sign" not in ev
    assert any("2 tries left" in t for t in shown) and any("no tries left" in t for t in shown)


def test_right_code_on_the_second_try_signs():
    ev = Events()
    out, *_ = run(ev, typed=("AAAAAA", "abcdef"))  # lower case is normalised
    assert not out.rejected and ev.count("ask") == 2 and ev[-2:] == ["sign", "pk:exit"]


def test_an_empty_answer_rejects_at_once_and_none_means_no_answer_in_time():
    for typed in (("",), (None,)):
        ev = Events()
        out, *_ = run(ev, typed=typed)
        assert out.rejected and ev.count("ask") == 1 and "sign" not in ev


def test_input_that_is_not_a_code_is_discarded_and_sends_nothing():
    ev = Events()
    out, shown, *_ = run(ev, typed=("word1 word2 word3", "abc", "ABCDEF"))
    assert not out.rejected  # the third try is right; the first two were only discarded
    assert [t for t in shown if "did not look like" in t][0].endswith("never type your recovery code here")
    assert [t for t in shown if "did not look like" in t][1].endswith("nothing was sent")
    assert ev.count("sign") == 1 and ev.count("reject") == 0


def test_three_unusable_entries_reject():
    ev = Events()
    out, *_ = run(ev, typed=("x", "yy", "zzz"))
    assert out.rejected and ev.count("ask") == 3


def test_the_typed_text_is_not_kept_in_the_outcome_or_shown():
    ev = Events()
    secret = "abandon ability able about above absent"
    out, shown, *_ = run(ev, typed=(secret, "ABCDEF"))
    assert secret not in "".join(shown) and secret not in repr(out)


def test_the_read_is_bounded_by_the_requests_expiry():
    ev = Events()
    queue = FakeQueue(ev, pend=pending(expires_ms=90_000))
    _, _, asked, _ = run(ev, queue=queue)
    assert asked[0][1] == pytest.approx(90.0)
    ev = Events()
    ticks = iter([0, 200])  # the outer check, then the moment the code would be asked: the request has ended
    out, *_ = run(ev, queue=FakeQueue(ev, pend=pending(expires_ms=100)), typed=(), now_ms=lambda: next(ticks))
    assert out.rejected and ev.count("ask") == 0 and ev[-2:] == ["reject", "pk:exit"]


def test_recovery_code_three_tries_each_with_a_fresh_source():
    ev = Events()
    pk = FakePKFactory(ev, fail=[BadRecoveryCode("recovery.words"), PersonMismatch("no"), None])
    out, shown, *_ = run(ev, pk=pk)
    assert not out.rejected and ev.count("pk:enter") == 3 and ev.count("challenge") == 1
    assert any("not a valid recovery code" in t for t in shown) and any("not the one" in t for t in shown)
    assert ev.index("pk:fail") < ev.index("challenge")


def test_no_valid_recovery_code_in_three_tries_cancels_and_signs_nothing():
    ev = Events()
    pk = FakePKFactory(ev, fail=[BadRecoveryCode("recovery.words")] * 3)
    with pytest.raises(InlineRefused) as e:
        run(ev, pk=pk)
    assert e.value.code == "no_key" and ev.count("pk:enter") == 3
    assert e.value.until_ms == 1_000_000  # the verified request stays outstanding on the relay until then
    assert "challenge" not in ev and "sign" not in ev and "reject" not in ev and ev[-1] == "close:cancelled"


def test_a_person_mismatch_never_reaches_the_challenge():
    ev = Events()
    with pytest.raises(InlineRefused):
        run(ev, pk=FakePKFactory(ev, fail=[PersonMismatch("x")] * 3))
    assert "challenge" not in ev


def test_human_only_is_not_retried():
    ev = Events()
    with pytest.raises(HumanOnly):
        run(ev, pk=FakePKFactory(ev, fail=[HumanOnly("agent")]))
    assert ev.count("pk:enter") == 1


def test_a_request_for_another_offer_or_device_is_refused_before_the_code():
    for kw in ({"offer_id": "f" * 32}, {"device_id": "9" * 32}, {"attempt": "9" * 32}):
        ev = Events()
        with pytest.raises(InlineRefused) as e:
            run(ev, **kw)
        assert e.value.code == "mismatch" and "pk:enter" not in ev
    ev = Events()
    run(ev, device_id="d_" + DEVICE)  # the host's d_ form is the same device
    run(Events(), attempt="e" * 32)  # the attached attempt matches


def test_the_cli_driver_without_a_device_id_fails_closed(tmp_path):
    from orch.custody import pairinline, primary_register

    st = tmp_path / "state"
    (st / "person" / "primary").mkdir(parents=True, mode=0o700)
    primary_register._write_private(
        primary_register.relay_path(st), {"v": 1, "origin": "https://relay.example", "person_id": "0" * 32,
                                          "device_id": "1" * 32, "label": "x"})  # fmt: skip
    drive = pairinline.driver(st, {"workspace_id": "w" * 32}, show=lambda t: None, ask_timed=lambda p, t: None,
                              link_check=lambda: None, fail=lambda c, m, hint=None: RuntimeError(c))  # fmt: skip
    for offer in ({}, {"device_id": ""}, {"device_id": None}):
        with pytest.raises(InlineRefused) as e:
            drive("o" * 32, RID, offer)
        assert e.value.code == "mismatch"


def test_a_refused_target_request_ends_without_a_prompt():
    ev = Events()
    queue = FakeQueue(ev, appears_after=99, skipped=[Skipped(RID, "bad_signature")])
    with pytest.raises(InlineRefused) as e:
        run(ev, queue=queue)
    assert e.value.detail == "bad_signature" and "pk:enter" not in ev


def test_other_open_requests_are_warned_about():
    ev = Events()
    _, shown, *_ = run(ev, queue=FakeQueue(ev, skipped=[Skipped("f" * 32, "other_open")]))
    assert any("did not create is open" in t for t in shown)


def test_the_request_never_arriving_times_out_without_a_prompt():
    ev = Events()
    clock = iter(range(0, 10_000))
    import time

    real = time.monotonic
    time.monotonic = lambda: float(next(clock))
    try:
        with pytest.raises(InlineRefused) as e:
            run(ev, queue=FakeQueue(ev, appears_after=10**6))
    finally:
        time.monotonic = real
    assert e.value.code == "timeout" and "pk:enter" not in ev


def test_client_hosted_shows_a_warning():
    ev = Events()
    _, shown, *_ = run(ev, queue=FakeQueue(ev, pend=pending(client_hosted=True)))
    assert shown[0].startswith("WARNING") and shown[1].startswith("request from:")


def test_a_queue_refusal_in_the_challenge_is_an_inline_refusal():
    ev = Events()
    queue = FakeQueue(ev)
    queue.challenge = lambda rid, pk: (_ for _ in ()).throw(QueueRefused("scope_exceeded", rid))
    with pytest.raises(InlineRefused) as e:
        run(ev, queue=queue)
    assert e.value.detail == "scope_exceeded"


def test_ctrl_c_leaves_the_request_challenged_and_signs_nothing():
    ev = Events()
    with pytest.raises(KeyboardInterrupt):
        run(ev, typed=(KeyboardInterrupt(),))
    assert "sign" not in ev and "reject" not in ev and ev[-1] == "pk:exit" and "challenge" in ev


def test_expected_code_is_the_queues_own_challenge_not_anything_else():
    ev = Events()
    out, *_ = run(ev, queue=FakeQueue(ev, code="ZZZZZZ"), typed=("ABCDEF", "ZZZZZZ"))
    assert not out.rejected and out.cert_code == "ZZZZZZ"


@pytest.mark.parametrize("text, want", [("abcdef", "ABCDEF"), (" ab cd ", None), ("ABCDE1", None), ("A" * 7, None),
                                        (None, None), ("234567", "234567")])  # fmt: skip
def test_normalize_code(text, want):
    assert normalize_code(text) == want
