"""R3 T2 (#1591, design I6; hub contract 10.2c "Presence for actions"): the click signer's presence for a remote-device
stream: nonces it issues itself, one batch per verified assertion, each digest good for one action, on one stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.click import presence as P
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host.stream_helpers import DEVICE, HH, SID, digest_text, make_world

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC = json.loads((CONTRACT / "vectors_remote.json").read_text())
VECTORS = [c for c in DOC["cases"] if c["check"] == "presence"]
OTHER = "7" * 32
D1, D2, D3 = (digest_text(bytes([i]) * 32) for i in (1, 2, 3))


@pytest.fixture
def world(served):
    w = make_world(served)
    yield w
    w.click.close()


@pytest.fixture
def fido(served):
    w = make_world(served, "fido2")
    yield w
    w.click.close()


def advance(world, seconds: float) -> None:
    world.served.lab.ws.clock[0] += seconds


# --- the contract vectors against the real check ---


@pytest.mark.parametrize("c", VECTORS, ids=[c["name"] for c in VECTORS])
def test_presence_vectors_replayed(c):
    a = c["args"]
    try:
        got = {
            "count": P.check(
                a["entry"],
                a["workspace_id"],
                bytes.fromhex(a["handshake_hash_hex"]),
                bytes.fromhex(a["nonce_hex"]),
                [bytes.fromhex(d) for d in a["digests_hex"]],
                a["assertion"],
                a["held"],
                issued={bytes.fromhex(k): v for k, v in a["issued"].items()},
                now_ms=a["now_ms"],
            )
        }
    except HubRefused as exc:
        got = {"error": exc.code}
    assert got == c["expect"], c["name"]


def test_the_vectors_cover_the_presence_cases_the_contract_requires():
    names = {c["name"] for c in VECTORS}
    assert {r for r in DOC["required"] if r.startswith(("presence_", "open_with_presence"))} - {
        "open_with_presence_signature"
    } <= names
    assert len(VECTORS) >= 25 and unb64u(VECTORS[0]["args"]["entry"]["presence_pub"], 65)


# --- nonces ---


def test_nonce_single_use_5min(world):
    nonce = world.call("presence_nonce")["nonce"]
    assert len(unb64u(nonce, 32)) == 32 and world.call("presence_nonce")["nonce"] != nonce
    raw = unb64u(nonce, 32)
    args = {"nonce": nonce, "digests": [D1], "assertion": world.streams.assertion(raw, [bytes.fromhex(D1[7:])])}
    assert world.call("presence", **args) == {"ok": True, "n": 1}
    assert world.refusal("presence", **args).code == "remote.presence_invalid"  # used: gone
    # a nonce older than 5 minutes is gone too
    old = world.call("presence_nonce")["nonce"]
    advance(world, 301)
    stale = {
        "nonce": old,
        "digests": [D1],
        "assertion": world.streams.assertion(unb64u(old, 32), [bytes.fromhex(D1[7:])]),
    }
    assert world.refusal("presence", **stale).code == "remote.presence_invalid"
    # a nonce the signer never issued
    made_up = b"\x07" * 32
    forged = {
        "nonce": b64u(made_up),
        "digests": [D1],
        "assertion": world.streams.assertion(made_up, [bytes.fromhex(D1[7:])]),
    }
    assert world.refusal("presence", **forged).code == "remote.presence_invalid"
    assert len(world.streams.used) == 1


def test_a_failed_assertion_still_burns_the_nonce(world):
    nonce = world.call("presence_nonce")["nonce"]
    raw = unb64u(nonce, 32)
    good = world.streams.assertion(raw, [bytes.fromhex(D1[7:])])
    bad = {"form": "p256", "sig": good["sig"][:-2] + ("AA" if good["sig"][-2:] != "AA" else "BB")}
    assert world.refusal("presence", nonce=nonce, digests=[D1], assertion=bad).code == "remote.presence_invalid"
    assert world.refusal("presence", nonce=nonce, digests=[D1], assertion=good).code == "remote.presence_invalid"


def test_at_most_eight_nonces_are_outstanding_per_stream(world):
    for _ in range(8):
        world.call("presence_nonce")
    assert world.refusal("presence_nonce").code == "rate.limited"
    world.streams.add(OTHER)
    world.call("presence_nonce", sid=OTHER)  # another stream has its own
    advance(world, 301)
    world.call("presence_nonce")  # the old ones expired


def test_presence_from_other_stream_refused(world):
    """A nonce is issued for one stream; a batch is bound to its stream and to that stream's ``hh``."""
    world.streams.add(OTHER, hh=b64u(bytes([9]) * 32))
    nonce = world.call("presence_nonce")["nonce"]
    raw = unb64u(nonce, 32)
    one = [bytes.fromhex(D1[7:])]
    # the nonce of stream SID posted on stream OTHER
    assert (
        world.refusal(
            "presence", sid=OTHER, nonce=nonce, digests=[D1], assertion=world.streams.assertion(raw, one)
        ).code
        == "remote.presence_invalid"
    )
    # the nonce is burnt by that attempt only on OTHER; on SID a signature made over another stream's hh fails
    wrong_hh = world.streams.assertion(raw, one, hh=bytes([9]) * 32)
    assert world.refusal("presence", nonce=nonce, digests=[D1], assertion=wrong_hh).code == "remote.presence_invalid"
    # a verified batch on SID is no batch on OTHER
    world.present([D1])
    assert world.refusal("presence_take", sid=OTHER, digest=D1).code == "remote.presence_required"
    assert world.call("presence_take", digest=D1) == {"ok": True}


# --- batches ---


def test_batch_fixed_list_only(world):
    world.present([D1, D2])
    assert world.refusal("presence_take", digest=D3).code == "remote.presence_required"
    assert world.call("presence_take", digest=D2) == {"ok": True}
    assert world.call("presence_take", digest=D1) == {"ok": True}


def test_batch_digest_used_once(world):
    world.present([D1, D2])
    assert world.call("presence_take", digest=D1) == {"ok": True}
    assert world.refusal("presence_take", digest=D1).code == "remote.presence_required"
    assert world.call("presence_take", digest=D2) == {"ok": True}


def test_a_batch_ends_five_minutes_after_its_nonce_was_issued(world):
    nonce = world.call("presence_nonce")["nonce"]
    advance(world, 200)
    one = [bytes.fromhex(D1[7:])]
    world.call("presence", nonce=nonce, digests=[D1], assertion=world.streams.assertion(unb64u(nonce, 32), one))
    advance(world, 99)
    assert world.call("presence_take", digest=D1) == {"ok": True}  # 299 s after issue
    world.present([D2])
    advance(world, 301)
    assert world.refusal("presence_take", digest=D2).code == "remote.presence_required"


def test_a_batch_is_one_to_sixteen_sorted_unique_digests(world):
    nonce = world.call("presence_nonce")["nonce"]
    raw = unb64u(nonce, 32)
    a = world.streams.assertion(raw, [bytes.fromhex(D1[7:])])
    for digests in ([], [D2, D1], [D1, D1], [digest_text(bytes([i]) * 32) for i in range(1, 18)], ["sha256:xyz"]):
        got = world.refusal("presence", nonce=nonce, digests=digests, assertion=a)
        assert got.code in ("invalid.input", "remote.presence_invalid"), digests
        nonce = world.call("presence_nonce")["nonce"]  # the failed one was burnt or never read
    assert world.streams.used == []


def test_the_host_hears_the_counter_and_the_count_never_the_signature(world):
    world.present([D1, D2])
    (call,) = world.streams.used
    assert call == {"stream_id": SID, "device_id": DEVICE, "count": 0, "n": 2}


# --- key file, UP-only and the FIDO2 flags ---


@pytest.mark.parametrize(
    ("flags", "ok"),
    [(0x05, True), (0x01, False), (0x04, False), (0x05 | 0x08, False), (0x05 | 0x10, False), (0x05 | 0x40, False)],
    ids=["up_uv", "up_only", "uv_only", "backup_eligible", "backup_state", "attested_data"],
)
def test_key_file_up_only_be_bs_refused(fido, flags, ok):
    nonce = fido.call("presence_nonce")["nonce"]
    a = fido.streams.assertion(unb64u(nonce, 32), [bytes.fromhex(D1[7:])], count=1, flags=flags)
    args = {"nonce": nonce, "digests": [D1], "assertion": a}
    if ok:
        assert fido.call("presence", **args) == {"ok": True, "n": 1}
    else:
        assert fido.refusal("presence", **args).code == "remote.presence_invalid"
        assert fido.streams.used == []


def test_a_fido2_counter_that_does_not_rise_is_refused_by_the_hosts_check(fido):
    fido.streams.counter = 5
    nonce = fido.call("presence_nonce")["nonce"]
    one = [bytes.fromhex(D1[7:])]
    low = fido.streams.assertion(unb64u(nonce, 32), one, count=5)  # not above the held counter: the signer's check
    assert fido.refusal("presence", nonce=nonce, digests=[D1], assertion=low).code == "remote.presence_invalid"
    nonce = fido.call("presence_nonce")["nonce"]
    assert fido.call(
        "presence", nonce=nonce, digests=[D1], assertion=fido.streams.assertion(unb64u(nonce, 32), one, count=6)
    )
    assert fido.streams.counter == 6


def test_a_platform_key_with_the_fido2_form_or_the_other_way_round_is_refused(world, fido):
    nonce = world.call("presence_nonce")["nonce"]
    one = [bytes.fromhex(D1[7:])]
    ctap = fido.streams.assertion(unb64u(nonce, 32), one)  # a ctap2 object to a platform entry
    assert world.refusal("presence", nonce=nonce, digests=[D1], assertion=ctap).code == "remote.presence_invalid"
    nonce = fido.call("presence_nonce")["nonce"]
    plain = world.streams.assertion(unb64u(nonce, 32), one)
    assert fido.refusal("presence", nonce=nonce, digests=[D1], assertion=plain).code == "remote.presence_invalid"


def test_an_open_signature_is_not_a_presence_signature(world):
    from orch.crypto import suite
    from orch.crypto.encoding import b64u
    from orch.host.remote import enrollment

    nonce = world.call("presence_nonce")["nonce"]
    raw = unb64u(nonce, 32)
    msg = enrollment.open_bytes(world.wid, "rc_" + "6" * 32, HH, raw)  # the open bytes, same fields
    assertion = {"form": "p256", "sig": b64u(suite.sign(world.streams.key, msg))}
    assert world.refusal("presence", nonce=nonce, digests=[D1], assertion=assertion).code == "remote.presence_invalid"


# --- presence inside a sign ---


def prepared_approval(world, human):
    _, key = human.ticket()
    p = world.prepare("close", {"ref": key, "resolution": "other"})
    return p["request_id"], world.review(p["request_id"])


def test_a_presence_kind_is_signed_only_with_a_digest_of_a_live_batch(world, human):
    rid, digest = prepared_approval(world, human)
    refused = world.refusal("sign", request_id=rid, view_digest=digest)
    assert refused.code == "remote.presence_required"
    assert world.click.signatures == 0
    world.present([digest, D3])
    assert world.sign(rid, digest)
    assert world.click.signatures >= 1
    # the digest is used: the same sign again is not allowed without a new presence
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.presence_required"


def test_a_batch_for_another_review_does_not_sign_this_one(world, human):
    rid, digest = prepared_approval(world, human)
    world.present([D1, D2])
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.presence_required"
    assert world.click.signatures == 0


def test_a_sign_with_a_digest_that_is_not_the_review_burns_nothing(world, human):
    rid, digest = prepared_approval(world, human)
    world.present([digest])
    wrong = "sha256:" + "a" * 64
    assert world.refusal("sign", request_id=rid, view_digest=wrong).code == "invalid.input"
    assert world.sign(rid, digest)  # the batch entry is still there


def test_an_allowed_kind_needs_no_presence(world, human):
    _, key = human.ticket()
    p = world.prepare("log", {"ref": key, "text": "no presence"})
    assert world.sign(p["request_id"], world.review(p["request_id"]))


def test_a_closed_stream_loses_its_batch(world, human):
    rid, digest = prepared_approval(world, human)
    world.present([digest])
    del world.streams.open[SID]
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.no_stream"
    world.streams.add(SID)  # even if the id came back, the batch is gone
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.presence_required"


def test_wrong_or_missing_digest_leaves_presence_unchanged(world, human):
    rid, digest = prepared_approval(world, human)
    world.present([digest])
    for bad in ("sha256:" + "a" * 64, None):
        kw = {} if bad is None else {"view_digest": bad}
        assert world.refusal("sign", request_id=rid, **kw).code == "invalid.input"
    unknown = "hr_" + "9" * 26
    assert world.refusal("sign", request_id=unknown, view_digest=digest).code == "invalid.input"
    assert world.sign(rid, digest)  # the batch entry was never touched


def test_failed_sign_restores_presence(world, human):
    _, key = human.ticket()
    p = world.prepare("close", {"ref": key, "resolution": "other"})
    rid = p["request_id"]
    digest = world.call("view", request_id=rid)["view_digest"]  # no dwell yet: the sign is refused (too_soon)
    world.present([digest])
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "human.click_refused"
    assert world.click.signatures == 0
    world.click.mono.t += 2
    assert world.sign(rid, digest)  # the same presence still covers it
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.presence_required"


def test_presence_sequence_sign_needs_presence(world):
    """A sequence whose first step is an allowed op (new) but that holds a presence step (people.set) is signed
    only with a batch over its digest: the class of a sign is the strictest step (``_approval_class``)."""
    from orch.host import sequence
    from tests.host.test_sequence import intent_of
    from tests.host.test_sequence_click import seq_args

    intent, op, args, meta = seq_args(world.click, intent_of(people={"assignees": [world.click.owner.ref]}))
    assert op == "new" and [s["op"] for s in sequence.steps(intent)][-1] == "people.set"
    p = world.prepare(op, args, sequence=meta)
    rid = p["request_id"]
    digest = world.review(rid)
    assert world.refusal("sign", request_id=rid, view_digest=digest).code == "remote.presence_required"
    assert world.click.signatures == 0
    world.present([digest])
    assert world.sign(rid, digest)
    assert world.click.signatures >= 1
