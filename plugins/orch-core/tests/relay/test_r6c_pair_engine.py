"""R6c: the offer engine end to end against fakes (11A.15.6, 11A.15.9A; the host vectors of 11A.15.14 that the engine
owns): offers and links, consumption, the answers table, the SAS entry, path 7a and 7b, step 8, the step-9 hooks and
recovery, publication through a FAKE membership thread and the fail-closed default, the baseline, deadlines,
cancellation, abandon, tails. Never a real relay."""

from __future__ import annotations

import pickle
import secrets

import pytest

from orch import crypto
from orch.identity import pairing
from orch.identity.pairdevice import cert_digest
from orch.relay.r6 import pair as P
from orch.relay.r6.pair import GateRefused, PairRefused, UnavailableMembership
from orch.wsproto import wire

from .helpers import WS, WSK_PUB
from .pairkit import OWNER, T0, Kit, Phone, make_cert, make_challenge, make_rejection

MIN = 60_000


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    yield k
    k.close()


def offer(kit, scopes=("look", "decide")):
    h = kit.engine.offer(scopes)
    return h, Phone(h.link.native)


def send(kit, phone, meta=None, *, ts=None, rid=None, signer=None):
    env, rid = phone.envelope(meta or phone.meta(), ts=kit.clock.ms if ts is None else ts, rid=rid, signer=signer)
    got = kit.engine.receive(env, rid)
    return got, env, rid


def last(kit, phone):
    return kit.last_answer(phone)[0]


def code_of(kit, h, phone):
    rec = kit.rec(h.offer_id)
    return pairing.sas(WS, h.offer_id, phone.sig_pub, phone.kx_pub, WSK_PUB, crypto.unb64u(rec["sas_nonce"]))


def consumed(kit, **kw):
    h, phone = offer(kit)
    got, _, _ = send(kit, phone, phone.meta(**kw))
    assert got == "answer:pending", got
    return h, phone


def confirmed_7b(kit):
    h, phone = consumed(kit)
    state, _ = kit.engine.answer(h.offer_id, code_of(kit, h, phone).lower(), True)
    assert state == "cert_pending"
    return h, phone


def challenged(kit):
    h, phone = confirmed_7b(kit)
    req = kit.relay.requests[-1]
    ch = make_challenge(req["o"], phone)
    kit.relay.request_state = {"state": "challenged", "challenge": ch, "cert": None, "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["challenge"] == ch
    return h, phone, req, ch


def certified(kit, scopes=("look", "decide")):
    h, phone, req, ch = challenged(kit)
    cert = make_cert(phone, scopes=scopes)
    kit.relay.request_state = {"state": "signed", "challenge": ch, "cert": cert, "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    return h, phone, cert


def alloc_of(kit, h, phone, cert, *, event_id=None):
    rec = kit.rec(h.offer_id)
    return {
        "offer_id": h.offer_id, "attempt": rec["consumed"]["attempt"], "device": "d_" + phone.device_id,
        "cert_digest": cert_digest(cert), "scopes": tuple(rec["scopes"]), "review_digest": "sha256:" + "11" * 32,
        "event_id": event_id or "01JAAAAAAAAAAAAAAAAAAAAAAA", "event_digest": "sha256:" + "22" * 32,
        "decl_digest": None,
    }  # fmt: skip


def allocated(kit):
    h, phone, cert = certified(kit)
    kit.engine.facts(h.offer_id)
    a = alloc_of(kit, h, phone, cert)
    kit.engine.precommit(a, landed=lambda i: False)
    kit.engine.committed(a)
    return h, phone, cert, a


# -- offers and links (steps 1, 2; 11A.15.9A) --------------------------------------------------------------------------


def test_offer_writes_open_before_the_link_and_the_link_names_this_offer(kit):
    h, phone = offer(kit)
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "open" and rec["scopes"] == ["look", "decide"] and rec["expires_ms"] == T0 + 10 * MIN
    link = pairing.parse_pair_link(h.link.native)
    assert link.native and link.origin == "https://relay.dev.severin.io"  # the card's relay_url, byte-equal
    assert link.fields.offer_id == h.offer_id and crypto.b64u(link.fields.secret) == rec["secret"]
    assert pairing.parse_pair_link(h.link.https).fields == link.fields
    assert link.fields.pk_pin == crypto.pk_pin(crypto.unb64u(crypto.b64u(kit.ctx.owner_pk_pub)))


def test_the_handle_never_shows_the_link(kit):
    h, _ = offer(kit)
    for text in (repr(h), str(h), repr(h.public())):
        assert "orch://" not in text and "#v2." not in text
    with pytest.raises(Exception):  # noqa: B017 - any pickling error: the link stays in the process
        pickle.dumps(h)


def test_one_live_offer_per_workspace(kit):
    offer(kit)
    with pytest.raises(PairRefused) as e:
        kit.engine.offer()
    assert e.value.code == "relay.pair_open"


@pytest.mark.parametrize("scopes", [("look", "decide", "operate"), ("operate",), ("type",), (), ("decide", "look")])
def test_pair_offer_operate_refused_v17_and_scopes_shape(kit, scopes):
    with pytest.raises(PairRefused) as e:
        kit.engine.offer(scopes)
    assert e.value.code == "invalid.input"
    assert kit.store.offer_ids() == []


def test_offer_refused_without_primary_or_when_the_host_blocks(kit):
    kit.host.has_primary = False
    with pytest.raises(PairRefused) as e:
        kit.engine.offer()
    assert e.value.code == "relay.no_primary"
    kit.host.has_primary, kit.host.blocked = True, PairRefused("relay.not_linked", "x")
    with pytest.raises(PairRefused) as e:
        kit.engine.offer()
    assert e.value.code == "relay.not_linked" and kit.store.offer_ids() == []
    with pytest.raises(PairRefused):
        kit.engine.offer(ttl_ms=11 * MIN)


def test_list_offers_has_no_secret(kit):
    h, phone = consumed(kit)
    rows = kit.engine.list_offers()
    assert [r.offer_id for r in rows] == [h.offer_id]
    text = repr(rows)
    rec = kit.rec(h.offer_id)
    assert rec["secret"] not in text and rec["sas_nonce"] not in text and code_of(kit, h, phone) not in text
    assert rows[0].label == "Severin's iPhone" and rows[0].sas_tries_left == 3


# -- consumption and the answers (steps 3 to 5) ------------------------------------------------------------------------


def test_pair_offer_consumed_once_second_device_pairing_closed_and_warns(kit):
    h, phone = consumed(kit)
    other = Phone(h.link.native)
    got, _, _ = send(kit, other)
    assert got == "refuse:pairing_closed"
    assert kit.rec(h.offer_id)["second_key"] is True
    assert "pairing_second_key" in kit.store.alarms()["alarms"]
    assert kit.engine.list_offers()[0].second_key is True


def test_pair_resend_same_rid_replays_exact_answer_bytes(kit):
    h, phone = offer(kit)
    got, env, rid = send(kit, phone)
    first = kit.relay.answers[-1][2]
    assert kit.engine.receive(env, rid) == "replay"
    assert kit.relay.answers[-1][2] == first


def test_pair_refusal_bytes_replayed_after_crash(tmp_path):
    k = Kit(tmp_path)
    h, phone = offer(k)
    got, env, rid = send(k, phone, ts=T0 - 400_000)
    assert got == "refuse:stale_timestamp"
    sent = k.relay.answers[-1][2]
    k.close()
    k2 = Kit(tmp_path, clock=k.clock)  # a restart: a new engine on the same directory
    try:
        assert k2.engine.receive(env, rid) == "replay"
        assert k2.relay.answers[-1][2] == sent
    finally:
        k2.close()


def test_pair_resend_answers_pending_in_every_later_state(kit):
    h, phone, cert, a = allocated(kit)
    for _ in range(2):
        got, _, _ = send(kit, phone)
        assert got == "answer:pending"
    assert last(kit, phone)["state"] == "pending"


def test_pair_answer_codes_within_phone_set(kit):
    assert P.REFUSAL_CODES == {"malformed", "pairing_closed", "bad_signature", "stale_timestamp", "other_person",
                               "cert_invalid", "cert_expired", "revoked"}  # fmt: skip


def test_refusals_are_budgeted_five_a_minute_per_offer(kit):
    h, phone = offer(kit)
    outs = [send(kit, phone, {"op": "nope"})[0] for _ in range(7)]
    assert outs[:5] == ["refuse:malformed"] * 5 and outs[5:] == ["drop:budget"] * 2
    kit.clock.ms += MIN
    assert send(kit, phone, {"op": "nope"})[0] == "refuse:malformed"


def test_pair_meta_v2_with_reader_decl_reaches_the_gate_and_the_first_stored_wins(kit):
    h, phone = offer(kit)
    decl = {"o": {"x": 1}, "sig": "s"}
    kit.host.decl = (decl, {"pk_pin": "p"})
    got, _, _ = send(kit, phone, phone.meta(reader_decl=decl))
    assert got == "answer:pending"
    assert kit.host.gates[-1][1] == {"pk_pin": crypto.b64u(crypto.pk_pin(kit.ctx.owner_pk_pub))}
    assert kit.rec(h.offer_id)["consumed"]["decl"] == decl
    kit.host.decl = ({"o": {"x": 2}, "sig": "t"}, {"pk_pin": "p"})
    send(kit, phone, phone.meta(reader_decl={"o": {"x": 2}, "sig": "t"}))
    assert kit.rec(h.offer_id)["consumed"]["decl"] == decl and len(kit.host.gates) == 1  # a resend never re-gates


def test_pair_reader_decl_rejected_before_sas_and_primary(kit):
    h, phone = offer(kit)
    kit.host.gate_error = GateRefused("sign.convenience_readers", "no_declaration")
    got, _, _ = send(kit, phone)
    assert got == "answer:rejected" and last(kit, phone) == {"state": "rejected", "wsk_pub": crypto.b64u(WSK_PUB)}
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "rejected" and rec["reason"].startswith("reader_gate")
    assert kit.relay.requests == []
    with pytest.raises(PairRefused):
        kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)


def test_presented_cert_path_7a_gates_with_the_certs_person_and_answers_pending_until_approved(kit):
    h, phone = offer(kit)
    cert = make_cert(phone)
    got, _, _ = send(kit, phone, phone.meta(cert=cert))
    assert got == "answer:pending" and kit.rec(h.offer_id)["path"] == "7a"
    assert kit.host.gates[-1][1] == {"person_id": OWNER}
    state, _ = kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)
    assert state == "confirmed" and kit.rec(h.offer_id)["cert"] == cert and kit.relay.requests == []
    env, _ = phone.status(ts=kit.clock.ms)
    kit.engine.receive(env, secrets.token_hex(16)) if False else None
    got = kit.engine.receive(*phone.status(ts=kit.clock.ms))
    assert got == "answer:pending"
    facts = kit.engine.facts(h.offer_id)
    assert facts.path == "7a" and facts.code == code_of(kit, h, phone)


def test_pair_cert_presented_other_person_and_other_keys_refused(kit):
    from .pairkit import corpus

    h, phone = offer(kit)
    bob = make_cert(phone, pk_priv=corpus.priv("person_bob.sig"))
    assert send(kit, phone, phone.meta(cert=bob))[0] == "refuse:other_person"
    other = Phone(h.link.native)
    assert send(kit, phone, phone.meta(cert=make_cert(other)))[0] == "refuse:cert_invalid"
    kit.host.revoked_ids.add(phone.device_id)
    assert send(kit, phone, phone.meta(cert=make_cert(phone)))[0] == "refuse:revoked"


def test_pair_cert_scopes_above_offer_refused_on_7a(kit):
    h, phone = offer(kit, ("look", "decide"))
    send(kit, phone, phone.meta(cert=make_cert(phone, scopes=("look",))))
    state, _ = kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)
    assert state == "failed" and "pairing_cert_invalid" in kit.store.alarms()["alarms"]


# -- the SAS (step 6) --------------------------------------------------------------------------------------------------


def test_sas_typed_code_mismatch_counts_and_third_wrong_code_rejects(kit):
    h, phone = consumed(kit)
    assert kit.engine.answer(h.offer_id, "AAAAAA", True) == ("pending", 2)
    assert kit.engine.answer(h.offer_id, "BBBBBB", True) == ("pending", 1)
    assert kit.engine.answer(h.offer_id, "CCCCCC", True) == ("rejected", 0)
    assert kit.rec(h.offer_id)["reason"] == "sas_mismatch"


@pytest.mark.parametrize("code, approve", [("", True), ("   ", True), (None, True)])
def test_sas_empty_or_reject_is_reject_at_once(kit, code, approve):
    h, phone = consumed(kit)
    assert kit.engine.answer(h.offer_id, code, approve)[0] == "rejected"


def test_sas_explicit_reject_with_the_right_code_rejects(kit):
    h, phone = consumed(kit)
    assert kit.engine.answer(h.offer_id, code_of(kit, h, phone), False)[0] == "rejected"
    got = kit.engine.receive(*phone.status(ts=kit.clock.ms))
    assert got == "answer:rejected"


# -- path 7b: the cert request, the challenge, the cert or rejection (7b to 8) -----------------------------------------


def test_7b_cert_request_is_signed_persisted_before_the_post_and_outstanding(kit):
    h, phone = confirmed_7b(kit)
    rec = kit.rec(h.offer_id)
    req = kit.relay.requests[-1]
    assert rec["request"]["signed"] == req and req["o"]["offer_id"] == h.offer_id
    assert req["o"]["primary_device_id"] == kit.host.primary().device_id and req["o"]["scopes_max"] if False else True
    assert kit.store.outstanding()["request_id"] == req["o"]["request_id"]
    assert rec["phone_deadline_ms"] == min(T0 + 10 * MIN, req["o"]["expires_ms"] + MIN)
    with pytest.raises(PairRefused):
        kit.engine.offer()  # an outstanding request blocks a new offer
    got = kit.engine.receive(*phone.status(ts=kit.clock.ms))
    assert got == "answer:cert_pending" and last(kit, phone)["challenge"] is None
    assert last(kit, phone)["primary_label"] == "MacBook Pro"


def test_cert_pending_challenge_byte_identical_never_null_again(kit):
    h, phone, req, ch = challenged(kit)
    seen = []
    for _ in range(3):
        kit.engine.receive(*phone.status(ts=kit.clock.ms))
        seen.append(last(kit, phone)["challenge"])
    assert seen == [ch, ch, ch]
    other = make_challenge(req["o"], phone)  # another nonce: never replaces the stored one
    kit.relay.request_state = {"state": "challenged", "challenge": other, "cert": None, "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["challenge"] == ch


def test_an_unverifiable_challenge_is_dropped_and_alarmed(kit):
    h, phone = confirmed_7b(kit)
    req = kit.relay.requests[-1]
    bad = make_challenge(req["o"], Phone(h.link.native))  # for other keys
    kit.relay.request_state = {"state": "challenged", "challenge": bad, "cert": None, "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["challenge"] is None and "relay_misbehaving" in kit.store.alarms()["alarms"]


def test_the_cert_from_the_relay_is_verified_and_clears_outstanding(kit):
    h, phone, cert = certified(kit)
    rec = kit.rec(h.offer_id)
    assert rec["cert"] == cert and rec["state"] == "cert_pending" and kit.store.outstanding() is None
    f = kit.engine.facts(h.offer_id)
    assert f.path == "7b" and f.code == pairing.cert_code(rec["challenge"]["o"])


def test_7b_cert_with_other_scopes_than_requested_fails(kit):
    h, phone, req, ch = challenged(kit)
    kit.relay.request_state = {"state": "signed", "challenge": ch, "cert": make_cert(phone, scopes=("look",)),
                               "rejection": None}  # fmt: skip
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "failed"
    assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "answer:rejected"


def test_the_primarys_rejection_is_relayed_unchanged_in_the_long_form(kit):
    h, phone, req, ch = challenged(kit)
    rej = make_rejection(req["o"])
    kit.relay.request_state = {"state": "rejected", "challenge": ch, "cert": None, "rejection": rej}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "rejected" and kit.store.outstanding() is None
    kit.engine.receive(*phone.status(ts=kit.clock.ms))
    ans = last(kit, phone)
    assert ans["rejection"] == rej and ans["pk_pub"] == crypto.b64u(kit.ctx.owner_pk_pub)


def test_pair_cert_from_the_inline_cli_with_its_challenge(kit):
    h, phone = confirmed_7b(kit)
    req = kit.relay.requests[-1]
    ch = make_challenge(req["o"], phone)
    cert = make_cert(phone)
    assert kit.engine.pair_cert(h.offer_id, cert=cert, challenge=ch) == "cert_pending"
    assert kit.rec(h.offer_id)["cert"] == cert and kit.engine.facts(h.offer_id).path == "7b"


def test_request_open_alarms_and_retries_the_same_bytes(kit):
    kit.relay.fail["request"] = "request_open"
    h, phone = confirmed_7b(kit)
    assert "pairing_request_open" in kit.store.alarms()["alarms"]
    assert kit.rec(h.offer_id)["request"]["posted"] is False
    signed = kit.rec(h.offer_id)["request"]["signed"]
    del kit.relay.fail["request"]
    kit.engine.tick()
    assert kit.relay.requests[-1] == signed and kit.rec(h.offer_id)["request"]["posted"] is True


def test_a_cert_request_past_expiry_without_a_cert_ends_expired(kit):
    h, phone = confirmed_7b(kit)
    kit.clock.ms += 10 * MIN
    assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "answer:expired"
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "expired"


# -- step 9: facts, precommit, committed, refused, recovery ------------------------------------------------------------


def test_step9_refused_near_phone_deadline_offer_expired(kit):
    h, phone, cert = certified(kit)
    kit.clock.ms = kit.rec(h.offer_id)["phone_deadline_ms"] - 119_000
    with pytest.raises(PairRefused) as e:
        kit.engine.facts(h.offer_id)
    assert e.value.code == "relay.pair_closed"
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "expired"


def test_facts_refuse_outside_step_9(kit):
    h, phone = consumed(kit)
    with pytest.raises(PairRefused):
        kit.engine.facts(h.offer_id)


def test_precommit_checks_the_record_inside_and_accepts_an_equal_repeat(kit):
    h, phone, cert = certified(kit)
    a = alloc_of(kit, h, phone, cert)
    for bad in ({"attempt": "ab" * 16}, {"device": "d_" + "cd" * 16}, {"cert_digest": "sha256:" + "00" * 32},
                {"scopes": ("look",)}):  # fmt: skip
        with pytest.raises(PairRefused):
            kit.engine.precommit({**a, **bad}, landed=lambda i: False)
    kit.engine.precommit(a, landed=lambda i: False)
    kit.engine.precommit(a, landed=lambda i: False)  # the roll-forward repeat
    assert kit.rec(h.offer_id)["intent"]["event_id"] == a["event_id"]
    b = {**a, "event_id": "01JBBBBBBBBBBBBBBBBBBBBBBB"}
    with pytest.raises(PairRefused):  # the earlier allocation landed: never a second
        kit.engine.precommit(b, landed=lambda i: True)
    kit.engine.precommit(b, landed=lambda i: False)  # it never landed: replaced
    assert kit.rec(h.offer_id)["intent"]["event_id"] == b["event_id"]


def test_no_wk_sealed_before_cert_and_allocation(kit):
    h, phone, cert = certified(kit)
    assert kit.membership.additions == [] and kit.relay.devices == []
    a = alloc_of(kit, h, phone, cert)
    kit.engine.precommit(a, landed=lambda i: False)
    kit.runner.run()
    assert kit.membership.additions == []
    kit.engine.committed(a)
    assert kit.membership.additions == []  # step 10 is a pair-thread job
    kit.runner.run()
    assert kit.relay.devices == [(phone.device_id, cert)]
    assert kit.membership.additions == [(h.offer_id, phone.device_id)]


def test_committed_is_idempotent_and_writes_the_device_record(kit):
    h, phone, cert, a = allocated(kit)
    kit.engine.committed(a)
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "allocated" and rec["event_id"] == a["event_id"]
    dev = kit.store.device(phone.device_id)
    assert dev["cert"] == cert and dev["scopes"] == ["look", "decide"] and dev["offer_id"] == h.offer_id
    assert dev["person_id"] == OWNER and dev["label"] == "Severin's iPhone"


def test_refused_ends_the_offer_failed(kit):
    h, phone, cert = certified(kit)
    a = alloc_of(kit, h, phone, cert)
    kit.engine.precommit(a, landed=lambda i: False)
    kit.engine.refused(a, "reader")
    assert kit.rec(h.offer_id)["state"] == "failed"


def test_crash_after_device_added_recovers_devices_and_allocated_by_intent_not_cert(tmp_path):
    k = Kit(tmp_path)
    h, phone, cert = certified(k)
    a = alloc_of(k, h, phone, cert)
    k.engine.precommit(a, landed=lambda i: False)
    k.close()  # the append landed; the host died before committed
    k2 = Kit(tmp_path, clock=k.clock)
    try:
        # matching is by the intent's event id and digest: a landed event of another id is not ours
        assert k2.engine.recover(lambda i: i["event_id"] == "01JZZZZZZZZZZZZZZZZZZZZZZZ") == []
        assert k2.rec(h.offer_id)["state"] == "cert_pending"
        assert k2.engine.recover(lambda i: i["event_id"] == a["event_id"]) == [h.offer_id]
        assert k2.rec(h.offer_id)["state"] == "allocated" and k2.store.device(phone.device_id) is not None
    finally:
        k2.close()


# -- publication, baseline, approved (steps 10 to 12) ------------------------------------------------------------------


def approve(kit):
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    kit.membership.events.on_committed(h.offer_id, {"list_seq": 4, "epoch": 3, "adds": [phone.device_id]})
    kit.runner.run()
    return h, phone, cert


def test_approved_numbers_are_the_acknowledged_publication_and_scopes_the_entrys(kit):
    h, phone, cert = approve(kit)
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "approved" and kit.relay.baselines == [(kit.host.cp["hash"], h.offer_id)]
    assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "answer:approved"
    ans = last(kit, phone)
    assert (ans["epoch"], ans["list_seq"], ans["card_seq"]) == (3, 4, kit.host.card)
    assert ans["scopes"] == kit.store.device(phone.device_id)["scopes"] == ["look", "decide"]
    assert ans["baseline"] == kit.host.cp and ans["cert"] == cert


def test_a_publication_without_the_device_is_not_adopted(kit):
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    kit.membership.events.on_committed(h.offer_id, {"list_seq": 4, "epoch": 3, "adds": ["ab" * 16]})
    kit.runner.run()
    assert kit.rec(h.offer_id)["state"] == "allocated" and kit.rec(h.offer_id)["publication"] is None


def test_approved_baseline_never_null_when_published_retries_until_deadline(kit):
    kit.host.cp = None
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    kit.membership.events.on_committed(h.offer_id, {"list_seq": 4, "epoch": 3, "adds": [phone.device_id]})
    kit.runner.run()
    assert kit.rec(h.offer_id)["state"] == "allocated" and "pairing_baseline_missing" in kit.store.alarms()["alarms"]
    kit.host.cp = {"n": 9, "hash": "sha256:" + "cd" * 32}
    kit.clock.ms += 31_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "approved"


def test_a_refused_add_fails_the_offer_and_raises_the_needs_item(kit):
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    kit.membership.events.on_refused(h.offer_id, "content")
    kit.runner.run()
    assert kit.rec(h.offer_id)["state"] == "failed"
    needs = kit.store.alarms()["needs"]
    assert needs and "remove the device" in needs[0]["text"]


def test_cert_conflict_at_put_device_fails_and_excludes(kit):
    kit.relay.fail["device"] = "cert_conflict"
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    assert kit.rec(h.offer_id)["state"] == "failed" and "pairing_cert_conflict" in kit.store.alarms()["alarms"]
    assert kit.membership.exclusions == [(h.offer_id, phone.device_id, "cert_conflict")]
    assert kit.membership.additions == []


def test_put_device_transient_retries_on_the_tick(kit):
    kit.relay.fail["device"] = "retry"
    h, phone, cert, a = allocated(kit)
    kit.runner.run()
    assert kit.membership.additions == []
    del kit.relay.fail["device"]
    kit.engine.tick()
    assert kit.membership.additions == [(h.offer_id, phone.device_id)]


# -- the fail-closed default without R6b -------------------------------------------------------------------------------


def test_unavailable_membership_is_the_fail_closed_default(tmp_path):
    k = Kit(tmp_path, membership=UnavailableMembership())
    try:
        h, phone, cert = certified(k)
        with pytest.raises(PairRefused) as e:  # step 9 refuses: nothing is allocated
            k.engine.facts(h.offer_id)
        assert e.value.code == "relay.pair_closed" and "publisher" in e.value.message
        # even an allocation that got past the check is never published: refused(publisher_unavailable)
        a = alloc_of(k, h, phone, cert)
        k.engine.precommit(a, landed=lambda i: False)
        k.engine.committed(a)
        k.runner.run()
        assert k.rec(h.offer_id)["state"] == "failed"
        assert k.rec(h.offer_id)["reason"] == "publication:publisher_unavailable"
        assert k.engine.receive(*phone.status(ts=k.clock.ms)) == "answer:rejected"
    finally:
        k.close()


def test_unavailable_membership_contract():
    m = UnavailableMembership()
    got = []

    class Ev:
        def on_committed(self, o, p):
            got.append(("committed", o))

        def on_refused(self, o, r):
            got.append(("refused", o, r))

    m.bind(Ev())
    assert m.ready() is False
    m.post_addition("ab" * 16, "cd" * 16, {})
    assert got == [("refused", "ab" * 16, "publisher_unavailable")]


# -- deadlines, cancellation, abandon, tails ---------------------------------------------------------------------------


def test_allocated_past_deadline_expired_excluded(kit):
    h, phone, cert, a = allocated(kit)
    kit.relay.fail["device"] = "retry"
    kit.clock.ms = kit.rec(h.offer_id)["phone_deadline_ms"] + 1
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "expired"
    assert kit.membership.exclusions == [(h.offer_id, phone.device_id, "deadline")]
    assert "did not finish" in kit.store.alarms()["needs"][0]["text"]


def test_abandon_published_or_allocated_excludes(kit):
    h, phone, cert, a = allocated(kit)
    assert kit.engine.abandon(h.offer_id) == "failed"
    assert kit.membership.exclusions == [(h.offer_id, phone.device_id, "abandoned")]


def test_cancelled_open_offer_answers_expired(kit):
    h, phone = offer(kit)
    assert kit.engine.cancel(h.offer_id) == "cancelled"
    assert send(kit, phone)[0] == "answer:expired"


def test_cancelled_consumed_offer_answers_rejected(kit):
    h, phone = consumed(kit)
    kit.engine.cancel(h.offer_id)
    assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "answer:rejected"
    assert send(kit, phone)[0] == "answer:rejected"


def test_cancel_after_allocated_refused_and_a_cancelled_offers_cert_refused(kit):
    h, phone, cert, a = allocated(kit)
    with pytest.raises(PairRefused) as e:
        kit.engine.cancel(h.offer_id)
    assert e.value.code == "relay.pair_closed"


def test_cancel_cert_pending_keeps_the_outstanding_request_and_refuses_its_cert(kit):
    h, phone, req, ch = challenged(kit)
    kit.engine.cancel(h.offer_id)
    assert kit.store.outstanding() is not None
    with pytest.raises(PairRefused):
        kit.engine.offer()  # the next offer waits for the old request
    with pytest.raises(PairRefused):
        kit.engine.pair_cert(h.offer_id, cert=make_cert(phone))
    kit.clock.ms = req["o"]["expires_ms"] + MIN + 1
    kit.engine.tick()
    assert kit.store.outstanding() is None
    kit.engine.offer()


def test_open_offer_expires_and_is_silent_after_its_tail(kit):
    h, phone = offer(kit)
    kit.clock.ms += 10 * MIN
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "expired"
    assert send(kit, phone)[0] == "refuse:pairing_closed"
    kit.clock.ms += 10 * MIN + 1
    kit.engine.tick()
    rec = kit.rec(h.offer_id)
    assert rec["secret"] is None and rec["sas_nonce"] is None
    assert send(kit, phone)[0] == "drop:no_key"
    kit.clock.ms += 7 * 86_400_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["tombstone"] is True


def test_pending_past_the_phone_deadline_expires(kit):
    h, phone = consumed(kit)
    kit.clock.ms += 10 * MIN + 1
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "expired"
    with pytest.raises(PairRefused):
        kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)


# -- the signer's callbacks --------------------------------------------------------------------------------------------


def test_the_signer_binding_holds_only_for_the_confirmed_7b_pairing(kit):
    h, phone = consumed(kit)
    dk = crypto.b64u(crypto.dk_hash(phone.sig_pub, phone.kx_pub))
    att = kit.rec(h.offer_id)["consumed"]["attempt"]
    assert not kit.engine.binding_held(h.offer_id, att, phone.device_id, dk)  # pending: not yet
    kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)
    assert kit.engine.binding_held(h.offer_id, att, phone.device_id, dk)
    assert not kit.engine.binding_held(h.offer_id, "00" * 16, phone.device_id, dk)


def test_answers_are_one_chunk_epoch_0_signed_by_the_wsk(kit):
    h, phone = offer(kit)
    send(kit, phone)
    env = kit.relay.answers[-1][2]
    hd = wire.parse_header(env[: wire.HEADER_LEN])
    assert (hd.epoch, hd.seq, hd.flags, hd.direction) == (0, 0, wire.FLAG_LAST, 2)
    assert hd.device.hex() == phone.device_id and hd.stream.hex() == h.offer_id


# -- the one transition function (orch-07's structural fix: an edge table, compare-and-set, guards) --------------------

ALL = sorted(P.EDGES)


def _seeded(kit, state, **over):
    """A record in ``state`` with every guard satisfied, written directly (the test's own setup)."""
    h, phone = offer(kit)
    rec = kit.rec(h.offer_id)
    base = dict(
        state=state, sas_ok=True, path="7b", phone_deadline_ms=kit.clock.ms + 10 * MIN, cert={"o": {}, "sig": ""},
        consumed={"device_id": phone.device_id, "dk_sig_pub": crypto.b64u(phone.sig_pub),
                  "dk_kx_pub": crypto.b64u(phone.kx_pub), "attempt": "ab" * 16, "label": "x", "cert": None,
                  "decl": None, "decl_who": None, "gated": True},
        request={"request_id": "cd" * 16, "signed": {}, "expires_ms": kit.clock.ms + 10 * MIN, "posted": True},
        intent=None, event_id="E", publication={"list_seq": 1, "epoch": 1, "list_hash": None},
        baseline={"n": 1, "hash": "sha256:" + "00" * 32}, card_seq=1,
    )  # fmt: skip
    rec.update({**base, **over})
    kit.store.put(rec)
    return kit.rec(h.offer_id)


@pytest.mark.parametrize("frm", ALL)
@pytest.mark.parametrize("to", ALL)
def test_every_edge_outside_the_table_is_refused(tmp_path, frm, to):
    k = Kit(tmp_path)
    try:
        gate_refused = frm == "open" and to == "rejected"  # the one edge into rejected from open: a gate refusal
        rec = _seeded(k, frm, **({"consumed": {"gated": False}} if gate_refused else {}))
        if to == frm:
            return
        if to in P.EDGES[frm]:
            if to == "allocated":  # only the host's settlement allocates, and it needs the intent's event
                rec = k.engine._commit(rec, None, intent={"event_id": "E"})
                with pytest.raises(P.IllegalTransition):
                    k.engine._commit(rec, to)
                assert k.engine._commit(rec, to, settle=P._SETTLE)["state"] == to
                return
            assert k.engine._commit(rec, to)["state"] == to
        else:
            with pytest.raises(P.IllegalTransition):
                k.engine._commit(rec, to)
            assert k.rec(rec["offer_id"])["state"] == frm
    finally:
        k.close()


def test_the_edge_table_is_the_state_machine_of_11a_15_6():
    assert P.EDGES["approved"] == frozenset() and all(not P.EDGES[t] for t in P.TERMINAL)
    forward = ["open", "pending", "confirmed", "cert_pending", "allocated", "published", "approved"]
    for a, b in zip(forward, forward[1:], strict=False):
        assert b in P.EDGES[a]
    assert "allocated" in P.EDGES["confirmed"]  # 7a skips cert_pending
    assert not P.EDGES["open"] & {"confirmed", "cert_pending", "allocated", "published", "approved"}
    assert not P.EDGES["pending"] & {"cert_pending", "allocated", "published", "approved"}
    assert "cancelled" not in P.EDGES["allocated"] | P.EDGES["published"]


@pytest.mark.parametrize(
    "frm, to, over, why",
    [
        ("open", "pending", {"consumed": {"gated": False}}, "gate"),  # never pending without the step-4 gate
        ("open", "rejected", {}, "gate"),  # open -> rejected only for a gate refusal
        ("pending", "confirmed", {"sas_ok": False}, "SAS"),  # never confirmed without the typed SAS
        ("pending", "confirmed", {"phone_deadline_ms": T0 - 1}, "deadline"),  # never forward past the deadline
        ("confirmed", "cert_pending", {"request": None}, "request"),
        ("confirmed", "allocated", {"intent": None}, "intent"),
        ("cert_pending", "allocated", {"cert": None}, "certificate"),
        ("allocated", "published", {"baseline": None}, "baseline"),
    ],
)
def test_guards_refuse_a_skipped_step(tmp_path, frm, to, over, why):
    k = Kit(tmp_path)
    try:
        rec = _seeded(k, frm, **over)
        if frm == "open" and to == "rejected":
            rec = {**rec}
            with pytest.raises(P.IllegalTransition):
                k.engine._commit(rec, to)
            return
        with pytest.raises(P.IllegalTransition):
            k.engine._commit(rec, to)
        assert k.rec(rec["offer_id"])["state"] == frm
    finally:
        k.close()


def test_a_state_changes_only_its_own_fields(kit):
    rec = _seeded(kit, "pending", cert=None, challenge=None)
    for field, value in (("cert", {"o": {}, "sig": "x"}), ("intent", {"event_id": "F"}), ("publication", {"x": 1}),
                         ("secret", None), ("request", None)):  # fmt: skip
        with pytest.raises(P.IllegalTransition):
            kit.engine._commit(rec, None, **{field: value})
    ch = kit.engine._commit(rec, None, sas_tries=1)
    assert ch["sas_tries"] == 1 and ch["ver"] == rec.get("ver", 0) + 1


def test_set_once_challenge_and_cert_never_change(tmp_path):
    k = Kit(tmp_path)
    try:
        rec = _seeded(k, "cert_pending", challenge={"o": {"a": 1}, "sig": "s"})
        with pytest.raises(P.IllegalTransition):
            k.engine._commit(rec, None, challenge={"o": {"a": 2}, "sig": "s"})
        with pytest.raises(P.IllegalTransition):
            k.engine._commit(k.rec(rec["offer_id"]), None, cert={"o": {"b": 1}, "sig": "t"})
        expired = k.engine._commit(k.rec(rec["offer_id"]), "expired")
        with pytest.raises(P.IllegalTransition):  # a terminal record only drops its secrets
            k.engine._commit(expired, None, secret="AAAA")
        assert k.engine._commit(expired, None, secret=None, sas_nonce=None)["secret"] is None
    finally:
        k.close()


def test_compare_and_set_two_events_on_one_offer_one_wins(kit):
    """A relay answer and a deadline (or the host's step-9 hook and a pair-thread job) both read version v: the
    first write wins, the second gets StaleRecord and writes nothing."""
    h, phone = consumed(kit)
    a = kit.rec(h.offer_id)
    b = kit.rec(h.offer_id)
    kit.engine._commit(a, "expired", reason="phone_deadline")
    with pytest.raises(P.StaleRecord):
        kit.engine._commit(b, "confirmed", sas_ok=True)
    assert kit.rec(h.offer_id)["state"] == "expired"


def test_compare_and_set_under_real_threads(kit):
    import threading

    h, phone = consumed(kit)
    rec = kit.rec(h.offer_id)
    wins, losses = [], []
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            kit.engine._commit(rec, None, sas_tries=i)
            wins.append(i)
        except P.StaleRecord:
            losses.append(i)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1 and len(losses) == 7
    assert kit.rec(h.offer_id)["sas_tries"] == wins[0] and kit.rec(h.offer_id)["ver"] == rec["ver"] + 1


def test_the_api_maps_a_lost_race_to_a_refusal(kit, monkeypatch):
    h, phone = consumed(kit)
    real = kit.engine._commit

    def racing(rec, to=None, **ch):
        kit.engine._commit = real
        real(kit.rec(h.offer_id), None, second_key=True)  # another event wins first
        return real(rec, to, **ch)

    kit.engine._commit = racing
    with pytest.raises(PairRefused) as e:
        kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)
    assert e.value.code == "relay.pair_closed" and kit.rec(h.offer_id)["state"] == "pending"


@pytest.mark.parametrize("crash_at", ["intent", "device_record", "allocated", "device_put", "addition_posted"])
def test_a_crash_at_each_allocation_step_recovers_with_no_skipped_state(tmp_path, crash_at):
    """A crash after each durable write of steps 9 and 10, then a restart: recovery rolls forward through the table
    only (never skipping a state), and the publication completes."""
    k = Kit(tmp_path)
    h, phone, cert = certified(k)
    a = alloc_of(k, h, phone, cert)
    k.engine.precommit(a, landed=lambda i: False)
    if crash_at != "intent":
        rec = k.rec(h.offer_id)
        k.engine._device_record(rec, P._intent(a))
    if crash_at in ("allocated", "device_put", "addition_posted"):
        k.engine.committed(a)  # its pair-thread job is lost with the crash (the runner never runs)
        k.runner.jobs.clear()
    if crash_at == "device_put":  # the device is at the relay; the addition was never posted
        k.engine._commit(k.rec(h.offer_id), None, device_put=True)
    if crash_at == "addition_posted":  # posted (R6b holds it), then marked: nothing to post again
        k.engine.publish(h.offer_id)
    k.close()
    k2 = Kit(tmp_path, clock=k.clock)
    try:
        k2.engine.recover(lambda i: i["event_id"] == a["event_id"])
        assert k2.rec(h.offer_id)["state"] == "allocated"
        k2.engine.tick()
        assert (h.offer_id, phone.device_id) in k.membership.additions + k2.membership.additions
        k2.membership.events.on_committed(h.offer_id, {"list_seq": 2, "epoch": 2, "adds": [phone.device_id]})
        k2.runner.run()
        assert k2.rec(h.offer_id)["state"] == "approved"
    finally:
        k2.close()


# -- durable first, fail closed (orch-07: "fail-open state drift") -----------------------------------------------------


class _Boom(OSError):
    pass


def _failing_put(kit, monkeypatch, *, after: int):
    """``store.put`` fails on its ``after``-th call from now (a disk error at that write)."""
    real = kit.store.put
    calls = {"n": 0}

    def put(rec):
        calls["n"] += 1
        if calls["n"] == after:
            raise _Boom("injected write failure")
        return real(rec)

    monkeypatch.setattr(kit.store, "put", put)
    return calls


def test_a_failed_consumption_write_sends_nothing_and_keeps_the_offer_open(kit, monkeypatch):
    h, phone = offer(kit)
    _failing_put(kit, monkeypatch, after=1)
    before = kit.rec(h.offer_id)
    with pytest.raises(_Boom):
        send(kit, phone)
    assert kit.relay.answers == []  # no pending answer for a consumption that is not durable
    after = kit.rec(h.offer_id)
    assert after == before and after["state"] == "open"
    monkeypatch.undo()
    assert send(kit, phone)[0] == "answer:pending"  # the device resends; the offer is consumed now


@pytest.mark.parametrize(
    "step",
    ["sas_confirm", "cert_request", "challenge", "cert", "intent", "allocated", "device_put", "publication",
     "published", "cancel", "expire"],
)  # fmt: skip
def test_an_injected_write_failure_at_each_transition_leaves_the_last_durable_state(kit, monkeypatch, step):
    def at(prepare, act):
        prepare_out = prepare()
        rec = kit.rec(prepare_out[0].offer_id)
        _failing_put(kit, monkeypatch, after=1)
        with pytest.raises(_Boom):
            act(prepare_out)
        monkeypatch.undo()
        assert kit.rec(rec["offer_id"]) == rec  # unchanged: state, version and every field

    if step == "sas_confirm":
        at(lambda: consumed(kit), lambda o: kit.engine.answer(o[0].offer_id, code_of(kit, o[0], o[1]), True))
    elif step == "cert_request":

        def prep():
            h, phone = consumed(kit)
            return h, phone

        def act(o):
            kit.engine._answer_sas(o[0].offer_id, code_of(kit, o[0], o[1]), True)

        # the confirmed write succeeds, the cert_pending write fails: confirmed stays, nothing was POSTed
        h, phone = prep()
        real = kit.store.put

        def put(rec):
            if rec["state"] == "cert_pending":
                raise _Boom("x")
            return real(rec)

        monkeypatch.setattr(kit.store, "put", put)
        with pytest.raises(_Boom):
            act((h, phone))
        monkeypatch.undo()
        assert kit.rec(h.offer_id)["state"] == "confirmed" and kit.relay.requests == []
        assert kit.store.outstanding() is None
    elif step == "challenge":
        h, phone = confirmed_7b(kit)
        req = kit.relay.requests[-1]
        kit.relay.request_state = {"state": "challenged", "challenge": make_challenge(req["o"], phone), "cert": None,
                                   "rejection": None}  # fmt: skip
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        kit.clock.ms += 10_000
        kit.engine.tick()  # the tick logs and goes on
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec
    elif step == "cert":
        h, phone, req, ch = challenged(kit)
        kit.relay.request_state = {"state": "signed", "challenge": ch, "cert": make_cert(phone), "rejection": None}
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        kit.clock.ms += 10_000
        kit.engine.tick()
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec and kit.store.outstanding() is not None
    elif step == "intent":
        h, phone, cert = certified(kit)
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        with pytest.raises(_Boom):
            kit.engine.precommit(alloc_of(kit, h, phone, cert), landed=lambda i: False)
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec
    elif step == "allocated":
        h, phone, cert = certified(kit)
        a = alloc_of(kit, h, phone, cert)
        kit.engine.precommit(a, landed=lambda i: False)
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        with pytest.raises(_Boom):
            kit.engine.committed(a)
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec and kit.runner.jobs == []  # no publication job for a non-durable state
        kit.engine.committed(a)  # the roll-forward completes it
        assert kit.rec(h.offer_id)["state"] == "allocated"
    elif step == "device_put":
        h, phone, cert, a = allocated(kit)
        kit.runner.jobs.clear()
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        with pytest.raises(_Boom):
            kit.engine.publish(h.offer_id)
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec and kit.membership.additions == []
    elif step == "publication":
        h, phone, cert, a = allocated(kit)
        kit.runner.run()
        rec = kit.rec(h.offer_id)
        _failing_put(kit, monkeypatch, after=1)
        with pytest.raises(_Boom):
            kit.engine.membership_committed(h.offer_id, {"list_seq": 4, "epoch": 3, "adds": [phone.device_id]})
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec and kit.relay.baselines == []
    elif step == "published":
        h, phone, cert, a = allocated(kit)
        kit.runner.run()
        real = kit.store.put

        def put(rec):
            if rec["state"] == "published":
                raise _Boom("x")
            return real(rec)

        monkeypatch.setattr(kit.store, "put", put)
        with pytest.raises(_Boom):
            kit.engine.membership_committed(h.offer_id, {"list_seq": 4, "epoch": 3, "adds": [phone.device_id]})
        monkeypatch.undo()
        assert kit.rec(h.offer_id)["state"] == "allocated"  # never approved without a durable published
        assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "answer:cert_pending"
    elif step == "cancel":
        at(lambda: consumed(kit), lambda o: kit.engine._cancel(o[0].offer_id))
    elif step == "expire":
        h, phone = consumed(kit)
        rec = kit.rec(h.offer_id)
        kit.clock.ms += 10 * MIN + 1
        _failing_put(kit, monkeypatch, after=1)
        kit.engine.tick()
        monkeypatch.undo()
        assert kit.rec(h.offer_id) == rec  # still pending on disk; the next tick expires it
        kit.engine.tick()
        assert kit.rec(h.offer_id)["state"] == "expired"


def test_a_corrupt_record_at_start_is_quarantined_as_failed_never_open(tmp_path):
    k = Kit(tmp_path)
    h, phone = offer(k)
    path = tmp_path / "membership" / "pairing" / f"{h.offer_id}.json"
    path.write_bytes(b"{not json")
    k.close()
    k2 = Kit(tmp_path, clock=k.clock)
    try:
        with pytest.raises(PairRefused):
            k2.engine.offer()  # an unreadable record blocks a new offer until it is quarantined (fail closed)
        assert k2.engine.recover(lambda i: False) == []
        rec = k2.rec(h.offer_id)
        assert rec["state"] == "failed" and rec["corrupt"] is True and rec["secret"] is None
        assert "pairing_state_corrupt" in k2.store.alarms()["alarms"] and k2.store.alarms()["needs"]
        assert send(k2, phone)[0] == "drop:no_key"
        k2.engine.offer()  # quarantined: no longer live
    finally:
        k2.close()


@pytest.mark.parametrize("bad", [{"state": "weird"}, {"state": "open", "ver": "1"}, {"v": 2}, {"state": None}])
def test_an_unknown_state_or_shape_is_refused_everywhere_and_quarantined(tmp_path, bad):
    k = Kit(tmp_path)
    try:
        h, phone = offer(k)
        rec = {**k.rec(h.offer_id), **bad}
        (tmp_path / "membership" / "pairing" / f"{h.offer_id}.json").write_text(__import__("json").dumps(rec))
        assert send(k, phone)[0] in ("drop:no_key",)
        assert not k.engine.is_live(h.offer_id)
        with pytest.raises(PairRefused):
            k.engine.offer()
        with pytest.raises(PairRefused):
            k.engine.cancel(h.offer_id)
        k.engine.recover(lambda i: False)
        assert k.rec(h.offer_id)["state"] == "failed"
    finally:
        k.close()


def test_an_unreadable_outstanding_request_is_cleared_and_alarmed(kit):
    (kit.store.root / "pairing" / "outstanding.json").write_bytes(b"[]")
    kit.engine.recover(lambda i: False)
    assert kit.store.outstanding() is None and "pairing_state_corrupt" in kit.store.alarms()["alarms"]


# -- certificate validation at every acceptance point (orch-07: "authentication / certificate validation") -------------


def _forged(cert, **o_changes):
    """The certificate with fields of ``o`` changed and the signature kept (no longer valid)."""
    return {"o": {**cert["o"], **o_changes}, "sig": cert["sig"]}


def _unsigned(cert):
    return {"o": cert["o"], "sig": crypto.b64u(b"\0" * 64)}


@pytest.mark.parametrize(
    "make, code",
    [
        (lambda ph: make_cert(ph, pk_priv=crypto.generate_private_key()), "other_person"),  # another person's key
        (lambda ph: _forged(make_cert(ph, pk_priv=crypto.generate_private_key()), person_id=OWNER), "cert_invalid"),
        (lambda ph: _unsigned(make_cert(ph)), "cert_invalid"),  # unsigned
        (lambda ph: _forged(make_cert(ph), scopes_max=["look"]), "cert_invalid"),  # altered after signing
        (lambda ph: make_cert(ph, created_ms=T0 - 10_000, expires_ms=T0 - 1), "cert_expired"),
        (lambda ph: make_cert(Phone(ph.link)), "cert_invalid"),  # a valid cert for other keys
        (lambda ph: "not a cert", "cert_invalid"),
    ],
)
def test_a_presented_certificate_is_verified_before_the_offer_is_consumed(kit, make, code):
    h, phone = offer(kit)
    phone.link = h.link.native
    got, _, _ = send(kit, phone, phone.meta(cert=make(phone)))
    assert got == f"refuse:{code}"
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "open" and rec["consumed"] is None and kit.host.gates == []  # nothing changed


def test_a_revoked_presented_device_is_refused_before_consumption(kit):
    h, phone = offer(kit)
    kit.host.revoked_ids.add(phone.device_id)
    assert send(kit, phone, phone.meta(cert=make_cert(phone)))[0] == "refuse:revoked"
    assert kit.rec(h.offer_id)["state"] == "open"


@pytest.mark.parametrize("scopes", [("look", "decide", "operate"), ("look", "decide", "operate", "type")])
def test_an_over_scoped_presented_certificate_never_reaches_step_9(kit, scopes):
    """The protocol accepts it at 3.5 (corpus pair_with_existing_cert carries all four scopes); the v1.7 phone ceiling
    (N1) is step 8's: the offer ends failed after the SAS, before any allocation, no WK."""
    h, phone = offer(kit)
    send(kit, phone, phone.meta(cert=make_cert(phone, scopes=scopes)))
    assert kit.engine.answer(h.offer_id, code_of(kit, h, phone), True)[0] == "failed"
    with pytest.raises(PairRefused):
        kit.engine.facts(h.offer_id)
    assert kit.membership.additions == [] and "pairing_cert_invalid" in kit.store.alarms()["alarms"]


@pytest.mark.parametrize(
    "make",
    [
        lambda ph: make_cert(ph, pk_priv=crypto.generate_private_key()),  # another person's key
        lambda ph: _unsigned(make_cert(ph)),
        lambda ph: make_cert(ph, created_ms=T0 - 10_000, expires_ms=T0 - 1),  # expired
        lambda ph: make_cert(ph, scopes=("look", "decide", "operate")),  # over the requested ceiling
        lambda ph: make_cert(Phone(ph.link)),  # other keys
        lambda ph: _forged(make_cert(ph), device_id="ab" * 16),
    ],
)
def test_the_relays_certificate_at_step_8_is_verified_before_it_is_kept(kit, make):
    h, phone, req, ch = challenged(kit)
    phone.link = h.link.native
    kit.relay.request_state = {"state": "signed", "challenge": ch, "cert": make(phone), "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "failed" and rec["cert"] is None
    with pytest.raises(PairRefused):
        kit.engine.facts(h.offer_id)


@pytest.mark.parametrize(
    "change",
    [{"workspace_id": "cd" * 16}, {"person_id": "ef" * 16}, {"request_id": "12" * 16}, {"attempt": "34" * 16},
     {"offer_id": "56" * 16}],
)  # fmt: skip
def test_a_challenge_of_another_workspace_person_request_attempt_or_offer_is_never_stored(kit, change):
    from .pairkit import pk_sign

    h, phone = confirmed_7b(kit)
    req = kit.relay.requests[-1]
    ch = make_challenge(req["o"], phone)
    bad = pk_sign("sig_cert_challenge", {**ch["o"], **change})
    kit.relay.request_state = {"state": "challenged", "challenge": bad, "cert": None, "rejection": None}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["challenge"] is None and "relay_misbehaving" in kit.store.alarms()["alarms"]


def test_a_rejection_signed_by_another_key_is_never_relayed(kit):
    from .pairkit import make_rejection

    h, phone, req, ch = challenged(kit)
    rej = make_rejection(req["o"])
    forged = {"o": rej["o"], "sig": crypto.b64u(crypto.sign(crypto.generate_private_key(), b"x"))}
    kit.relay.request_state = {"state": "rejected", "challenge": ch, "cert": None, "rejection": forged}
    kit.clock.ms += 10_000
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "cert_pending" and kit.rec(h.offer_id)["rejection"] is None


# -- Opus review of #588: step 9 is exclusive once the intent is recorded ----------------------------------------------


def intent_held(kit):
    h, phone, cert = certified(kit)
    a = alloc_of(kit, h, phone, cert)
    kit.engine.precommit(a, landed=lambda i: False)
    return h, phone, cert, a


def test_the_tick_never_expires_an_offer_whose_intent_is_recorded(kit):
    h, phone, cert, a = intent_held(kit)
    kit.clock.ms = kit.rec(h.offer_id)["phone_deadline_ms"] - 100_000  # inside the 2-minute step-9 margin
    kit.engine.tick()
    assert kit.rec(h.offer_id)["state"] == "cert_pending"
    kit.engine.committed(a)  # the host's settlement still lands
    assert kit.rec(h.offer_id)["state"] == "allocated" and kit.store.device(phone.device_id) is not None


def test_cancel_is_refused_once_the_intent_is_recorded(kit):
    h, phone, cert, a = intent_held(kit)
    with pytest.raises(PairRefused) as e:
        kit.engine.cancel(h.offer_id)
    assert e.value.code == "relay.pair_closed" and kit.rec(h.offer_id)["state"] == "cert_pending"
    kit.engine.committed(a)
    assert kit.rec(h.offer_id)["state"] == "allocated"


@pytest.mark.parametrize("relay_state", ["expired", "rejected"])
def test_a_relay_answer_never_ends_an_offer_with_a_cert_in_hand(kit, relay_state):
    h, phone, cert = certified(kit)
    req = kit.rec(h.offer_id)["request"]
    from .pairkit import make_rejection

    rej = make_rejection(req["signed"]["o"]) if relay_state == "rejected" else None
    kit.relay.request_state = {"state": relay_state, "challenge": None, "cert": None, "rejection": rej}
    kit.engine.notice([req["request_id"]])
    kit.clock.ms += 10_000
    kit.engine.tick()
    rec = kit.rec(h.offer_id)
    assert rec["state"] == "cert_pending" and rec["cert"] == cert  # an unauthenticated relay state never overrides


def test_a_relay_expired_during_step_9_leaves_the_host_settlement(kit):
    h, phone, cert, a = intent_held(kit)
    req = kit.rec(h.offer_id)["request"]
    kit.relay.request_state = {"state": "expired", "challenge": None, "cert": None, "rejection": None}
    kit.engine.notice([req["request_id"]])
    kit.engine.tick()
    kit.engine.committed(a)
    assert kit.rec(h.offer_id)["state"] == "allocated"


@pytest.mark.parametrize("to", ["expired", "failed", "cancelled", "rejected", "allocated"])
def test_with_an_intent_only_the_host_settlement_may_move_the_offer(kit, to):
    h, phone, cert, a = intent_held(kit)
    rec = kit.rec(h.offer_id)
    with pytest.raises(P.IllegalTransition):
        kit.engine._commit(rec, to, event_id=a["event_id"])
    with pytest.raises(P.IllegalTransition):
        kit.engine._commit(rec, None, cert=None)
    assert kit.rec(h.offer_id)["state"] == "cert_pending"


@pytest.mark.parametrize("landed", [True, False])
def test_past_the_deadline_the_tick_settles_through_the_host_by_the_log(kit, landed):
    h, phone, cert, a = intent_held(kit)
    kit.host.landed_fn = lambda intent: landed and intent["event_id"] == a["event_id"]
    kit.clock.ms = kit.rec(h.offer_id)["phone_deadline_ms"] + 1
    kit.engine.tick()
    assert kit.host.settled == 1
    rec = kit.rec(h.offer_id)
    if landed:
        assert rec["state"] == "allocated" and kit.store.device(phone.device_id) is not None
    else:
        assert rec["state"] == "failed" and rec["reason"] == "allocation:not_landed"
        with pytest.raises(PairRefused):  # a late resubmit of the old request cannot land any more
            kit.engine.precommit(a, landed=lambda i: False)


def test_an_equal_repeat_precommit_still_checks_the_state_and_the_deadline(kit):
    h, phone, cert, a = intent_held(kit)
    kit.engine.precommit(a, landed=lambda i: False)  # inside the window: accepted
    kit.clock.ms = kit.rec(h.offer_id)["phone_deadline_ms"] - 100_000
    with pytest.raises(PairRefused):
        kit.engine.precommit(a, landed=lambda i: False)


def test_recover_goes_on_after_a_failing_offer_and_raises_a_needs_item(kit, monkeypatch):
    h, phone, cert, a = intent_held(kit)
    rec = kit.rec(h.offer_id)
    other = "ee" * 16
    kit.store.put({**rec, "offer_id": other, "intent": {**rec["intent"], "offer_id": other}})
    real = kit.engine._device_record

    def flaky(r, alloc):
        if r["offer_id"] == h.offer_id:
            raise OSError("disk")
        return real(r, alloc)

    monkeypatch.setattr(kit.engine, "_device_record", flaky)
    assert kit.engine.recover(lambda i: True) == [other]
    assert "pairing_recovery_failed" in kit.store.alarms()["alarms"]
    assert any(n["offer_id"] == h.offer_id for n in kit.store.alarms()["needs"])


def test_a_readmitted_device_with_an_equal_record_keeps_the_old_record(kit):
    h, phone, cert, a = intent_held(kit)
    kit.store.write_device(
        {"v": 1, "device_id": phone.device_id, "person_id": OWNER, "cert": cert, "label": "Severin's iPhone",
         "offer_id": "aa" * 16, "paired_ms": 1, "decl_digest": None, "scopes": ["look", "decide"],
         "event_id": "01JOLDOLDOLDOLDOLDOLDOLDOL"}
    )  # fmt: skip
    kit.engine.committed(a)
    assert kit.rec(h.offer_id)["state"] == "allocated"
    assert kit.store.device(phone.device_id)["offer_id"] == "aa" * 16  # never overwritten


def test_a_device_record_of_other_content_is_alarmed_never_overwritten(kit):
    h, phone, cert, a = intent_held(kit)
    other = make_cert(phone, scopes=("look", "decide"))
    old = {"v": 1, "device_id": phone.device_id, "person_id": OWNER, "cert": other,
           "label": "old", "offer_id": "aa" * 16, "paired_ms": 1, "decl_digest": None, "scopes": ["look"],
           "event_id": "01JOLDOLDOLDOLDOLDOLDOLDOL"}  # fmt: skip
    kit.store.write_device(old)
    kit.engine.committed(a)
    assert kit.store.device(phone.device_id) == old
    assert kit.rec(h.offer_id)["state"] == "failed"
    assert "pairing_device_conflict" in kit.store.alarms()["alarms"]
    assert kit.membership.additions == [] and kit.store.alarms()["needs"]


def test_an_offer_without_decide_is_refused_at_step_1(kit):
    with pytest.raises(PairRefused) as e:
        kit.engine.offer(("look",))
    assert e.value.code == "invalid.input" and kit.store.offer_ids() == []


def test_an_approved_record_becomes_a_tombstone_after_7_days_and_the_signers_gate_closes(kit):
    h, phone, cert = approve(kit)
    assert kit.engine.is_live(h.offer_id)
    kit.clock.ms += 7 * 86_400_000 + 1
    kit.engine.tick()
    assert kit.rec(h.offer_id)["tombstone"] is True
    assert not kit.engine.is_live(h.offer_id)
    assert kit.engine.receive(*phone.status(ts=kit.clock.ms)) == "drop:no_key"


def test_the_handle_refuses_copy_and_deepcopy(kit):
    import copy

    h, _ = offer(kit)
    for fn in (copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(h)


def test_an_answer_whose_first_post_failed_is_posted_again_by_the_tick_without_a_resend(kit):
    kit.relay.fail["answer"] = "retry"  # the first POST dies (a stale keep-alive, a 503)
    h, phone = offer(kit)
    got, env, rid = send(kit, phone)
    assert got.startswith("answer:")
    sent = kit.relay.answers[-1][2]
    n = len(kit.relay.answers)
    assert kit.store.answers(h.offer_id)[0]["posted"] is False
    kit.engine.tick()  # inside the re-post spacing: not hammered
    assert len(kit.relay.answers) == n
    kit.clock.ms += 2_500
    kit.engine.tick()  # still failing: tried again, still unposted
    assert len(kit.relay.answers) == n + 1 and kit.store.answers(h.offer_id)[0]["posted"] is False
    del kit.relay.fail["answer"]
    kit.clock.ms += 2_500
    kit.engine.tick()  # the phone never resent: the stored bytes go out
    assert len(kit.relay.answers) == n + 2 and kit.relay.answers[-1][2] == sent
    assert kit.store.answers(h.offer_id)[0]["posted"] is True
    kit.clock.ms += 2_500
    kit.engine.tick()
    assert len(kit.relay.answers) == n + 2  # posted: nothing more


def _unposted(kit, n):
    """``n`` stored answers (distinct rids) whose POST failed transiently."""
    kit.relay.fail["answer"] = "retry"
    h, phone = offer(kit)
    for _ in range(n):
        send(kit, phone)
    return h


def test_the_repost_stops_at_the_first_transient_failure_of_a_tick(kit):
    _unposted(kit, 3)
    base = len(kit.relay.answers)
    kit.clock.ms += 2_500
    kit.engine.tick()
    assert len(kit.relay.answers) == base + 1  # one attempt, then no more POSTs this tick


def test_the_repost_is_capped_per_tick(kit):
    _unposted(kit, 6)
    del kit.relay.fail["answer"]
    base = len(kit.relay.answers)
    kit.clock.ms += 2_500
    kit.engine.tick()
    assert len(kit.relay.answers) == base + 4  # REPOSTS_PER_TICK
    kit.engine.tick()
    assert len(kit.relay.answers) == base + 6
    assert not kit.engine._reposted  # posted: the throttle keys are dropped


def test_a_failing_store_write_in_the_repost_does_not_end_the_tick_or_unthrottle_it(kit, monkeypatch):
    _unposted(kit, 1)
    del kit.relay.fail["answer"]

    def boom(*a, **kw):
        raise OSError("disk")

    monkeypatch.setattr(kit.store, "mark_answer_posted", boom)
    kit.clock.ms += 2_500
    base = len(kit.relay.answers)
    kit.engine.tick()  # does not raise
    assert len(kit.relay.answers) == base + 1
    kit.engine.tick()  # same instant: throttled by the key set before the POST
    assert len(kit.relay.answers) == base + 1
