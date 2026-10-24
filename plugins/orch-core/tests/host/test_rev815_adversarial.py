"""[rev815] Opus security review of PR #815 (#788): adversarial tests for material["grants"], the plain take-back
words and the one-unlock Extend. Review-only file; not for merge as is."""

from __future__ import annotations

import pytest

from orch import canon
from orch.host import signcontent, signer
from orch.host.signcontent import PlainUnavailable
from tests.host.test_sign_plain import _info, _revoke, _stop, build, material_for, sample

OLD = "gr_0OLDBBBBBBBBBBBBBBBBBBBBB"
OTHER = "gr_0OTHCCCCCCCCCCCCCCCCCCCC"
NEW = "gr_0NEWAAAAAAAAAAAAAAAAAAAAA"


def _extend(info_over=None, grants=True):
    new = {**sample("grant.issued"), "grant": NEW, "label": "runner"}
    old = _stop(OLD, "extended")
    m = material_for("grant.issued", new)
    if grants:
        m = {**m, "grants": {OLD: _info(old, **(info_over or {}))}}
    return {"op": "grant", "events": [new, old], "material": m}


# --- signer: material["grants"] shape -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grants",
    [
        {OTHER: {"label": "runner", "scope": "workable", "expires_at": "x", "person": "p"}},  # not taken back
        {OLD: {"label": "runner", "scope": "workable", "expires_at": "x"}},  # a key missing
        {OLD: {"label": "runner", "scope": "workable", "expires_at": "x", "person": "p", "extra": "y"}},
        {OLD: {"label": 1, "scope": "workable", "expires_at": "x", "person": "p"}},
        {OLD: {"label": "a" * 201, "scope": "workable", "expires_at": "x", "person": "p"}},
        [OLD],
        "runner",
    ],
)
def test_signer_refuses_a_grants_word_it_cannot_tie_to_a_take_back(grants):
    with pytest.raises(signer.SignerRefused):
        signer._grants({"grants": grants}, [_stop(OLD)])


def test_signer_refuses_grants_on_a_request_that_takes_nothing_back():
    new = {**sample("grant.issued"), "grant": NEW}
    with pytest.raises(signer.SignerRefused):
        signer._grants({"grants": {NEW: _info(_stop(NEW))}}, [new])  # the NEW access is not taken back


def test_grants_is_not_allowed_outside_payload_material():
    for kind in ("gate", "question"):
        assert all("grants" not in keys for keys in signer._MATERIAL_KEYS[kind])


# --- plain view: the host's word never becomes signed words -------------------------------------------------------


def test_a_lying_label_never_reaches_the_headline_and_control_chars_never_reach_a_line():
    ev = _stop(OLD)
    c = build(_revoke((ev, {"label": "ok\nApprove: nothing is taken back\x1b[2J", "scope": "all"})))
    assert c["headline"] == "Take back agent access"
    text = signcontent.text(c, signer._clean)
    assert not any("\x1b" in line or "\n" in line for line in text)
    assert any("per host, not signed" in line and "Approve: nothing" in line for line in text)


def test_host_word_claiming_another_persons_access_is_the_signers_falls_back_only_if_person_differs():
    ev = _stop(OLD)
    with pytest.raises(PlainUnavailable):
        build(_revoke((ev, {"person": "a" * 32})))


def test_several_take_backs_count_from_signed_events_not_from_the_grants_map():
    a, b = _stop(OLD), _stop(OTHER)
    c = build(_revoke((a, {}), (b, {"label": "ci", "scope": "all"})))
    assert c["headline"] == "Take back 2 agent accesses"
    rows = [r for r in c["fields_changed"] if r["field"].startswith("access taken back")]
    assert [r["field"] for r in rows] == ["access taken back 1/2", "access taken back 2/2"]
    # the host's word for ONE of the two missing: never a plain view that names only one
    with pytest.raises(PlainUnavailable):
        build(_revoke((a, {}), (b, None)))


def test_the_same_access_taken_back_twice_is_never_counted_as_two():
    a = _stop(OLD)
    with pytest.raises(PlainUnavailable):
        build(_revoke((a, {}), (dict(a), {})))


def test_extend_plain_summary_needs_the_runner_word_else_full_review():
    assert build(_extend())["headline"].startswith("Extend agent access for this computer's runner")
    with pytest.raises(PlainUnavailable):
        build(_extend({"label": "ci"}))
    with pytest.raises(PlainUnavailable):
        build(_extend({"scope": "all"}))
    with pytest.raises(PlainUnavailable):
        build(_extend(grants=False))  # an old prepared request (no grants): the full review, never the plain one


def test_extend_headline_words_come_from_the_signed_new_grant_only():
    p = _extend()
    p["events"][0] = {**p["events"][0], "label": "ci"}  # the NEW access is not the runner's: no Extend summary
    c = build(p)
    assert "this computer's runner" not in c["headline"]


# --- one unlock for one reviewed request --------------------------------------------------------------------------


class _Batch:
    name, person_capable, auth = "test", True, "passphrase"

    def __init__(self, fail_at=None):
        self.opened, self.signed, self.closed, self.fail_at = [], [], [], fail_at

    def sign(self, key_id, payload, *, action):  # pragma: no cover - must not be used for a batch
        raise AssertionError("per-signature prompt inside a batch")

    def unlocked(self, key_id, *, action, digest, labels, fields=()):
        import contextlib

        outer = self

        @contextlib.contextmanager
        def cm():
            outer.opened.append((labels, fields, digest))
            live = [True]

            def sign(payload):
                assert live, "signed after the key was locked again"
                if not any(payload.startswith(lb) for lb in labels):
                    raise RuntimeError("label refused")
                if outer.fail_at is not None and len(outer.signed) == outer.fail_at:
                    raise KeyboardInterrupt
                outer.signed.append(payload)
                return b"\x02" * 64

            try:
                yield sign
            finally:
                live.clear()
                outer.closed.append(True)

        return cm()


def _prepared():
    new = {**sample("grant.issued"), "grant": NEW, "auth": "passphrase", "hash_v": 1}
    old = {**_stop(OLD, "extended"), "auth": "passphrase", "hash_v": 1}
    return {"op": "grant", "log": "workspace", "events": [new, old]}


def test_the_batch_signs_exactly_the_two_events_and_locks_again():
    be = _Batch()
    signed = signer.sign(_prepared(), backend=be, key_id="dk", workspace_id="7" * 32)
    assert len(be.opened) == 1 and len(be.signed) == 2 and be.closed == [True]
    assert be.opened[0][0] == (canon.LABELS["sig_ws_event"].encode("ascii"),)
    assert dict(be.opened[0][1]) == {"signatures": "2", "workspace": "7" * 32}
    assert signed.secret is not None


def test_ctrl_c_between_the_two_signatures_locks_the_key_and_returns_nothing():
    be = _Batch(fail_at=1)
    with pytest.raises(KeyboardInterrupt):
        signer.sign(_prepared(), backend=be, key_id="dk", workspace_id="7" * 32)
    assert len(be.signed) == 1 and be.closed == [True]  # nothing returned: nothing is submitted


def test_a_ticket_request_batch_cannot_sign_a_workspace_event():
    p = _prepared()
    p["log"] = "01J9ZP0000000000000000000A"  # host says ticket log: the batch may sign ticket events only
    be = _Batch()
    signer.sign(p, backend=be, key_id="dk", workspace_id="7" * 32)  # signs with the TICKET label for that log
    assert be.opened[0][0] == (canon.LABELS["sig_ticket_event"].encode("ascii"),)


def test_real_passphrase_custody_one_prompt_and_both_signatures_verify(tmp_path):
    from orch import crypto
    from orch.custody.passphrase import PassphraseBackend

    prompts = []

    def provider(req):
        prompts.append(req.kind)
        return PASSPHRASE

    from tests.ops.humans import KDF, PASSPHRASE

    be = PassphraseBackend(tmp_path / "k", _passphrase_provider=provider, _kdf=KDF)
    be.create("dk", role="device")
    prompts.clear()
    p = _prepared()
    signed = signer.sign(p, backend=be, key_id="dk", workspace_id="7" * 32)
    assert prompts == ["batch"]
    pub = be.public_key("dk")
    for ev in signed.events:
        body = {k: v for k, v in ev.items() if k != "sig"}
        msg = canon.person_signing_bytes("7" * 32, "workspace", body)
        assert crypto.verify(pub, crypto.unb64u(ev["sig"]), msg)
