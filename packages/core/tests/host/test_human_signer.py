"""H5a, the signer's own checks (§0.8.1, D-P2-21, N4): shape first, every hash and every Review recomputed from the
material, the fingerprint (N2), ``before``, the header; all of it before any prompt."""

from __future__ import annotations

import copy

import pytest

from orch import canon
from orch.host import review, signer
from orch.ops.actors import allows
from tests.host import human_helpers
from tests.host.test_human import TwoNotes, ready
from tests.identity.helpers import Person

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

WS = "705d40abbb8c1c90354a1acaa94c935c"
UID = "01J9ZP0000000000000000000A"
H1, H2 = "sha256:" + "ab" * 32, "sha256:" + "cd" * 32
alice = Person()


def refused(prepared, human, match: str = "") -> None:
    signed = len(human.backend.payloads)
    with pytest.raises(signer.SignerRefused, match=match):
        human.sign(prepared)  # check, then sign
    assert len(human.backend.payloads) == signed  # nothing was signed: refused before any prompt


# --- N4: shape first ------------------------------------------------------------------------------------------------


def test_an_event_type_outside_emits_v1_of_the_op_is_refused(human):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    for typ in ("question.answered", "task.done", "claim.taken", "grant.issued", "nonsense"):
        bad = copy.deepcopy(p)
        bad["events"][0]["type"] = typ
        refused(bad, human, "refusing to sign")


def test_a_second_log_another_based_on_or_a_foreign_actor_is_refused(human, served):
    other_uid, _ = human.ticket("other")
    _, key = human.ticket()
    with TwoNotes():
        p = human.prepare("log", {"ref": key, "text": "pair"})
    human.check(p)  # as prepared, it passes
    bad = copy.deepcopy(p)
    bad["log"] = other_uid  # the events would be signed into a second log
    refused(bad, human, "not the ticket of the log")
    bad = copy.deepcopy(p)
    bad["events"][1]["based_on"] = H1
    refused(bad, human, "one based_on")
    bad = copy.deepcopy(p)
    bad["events"][0]["actor"]["id"] = Person().ref
    refused(bad, human, "not for this signer's person")
    bad = copy.deepcopy(p)
    bad["events"][1]["roster_v"] += 1
    refused(bad, human, "roster_v")
    bad = copy.deepcopy(p)
    bad["op"] = "approve"  # two events and a type approve does not emit
    refused(bad, human, "do not fit")
    bad = copy.deepcopy(p)
    bad["events"][0]["seq"] = 3
    refused(bad, human, "carries seq")
    bad = copy.deepcopy(p)
    bad["workspace_id"] = "f" * 32
    refused(bad, human, "another workspace")


def test_the_composite_never_prompts_for_a_refused_request(human, served):
    _, key = human.ticket()
    confirmed = []

    def call(op, args, files=None, idem=None):
        res = human.c.req(op, args, files=files, idem=idem)
        if op == "human.prepare":
            res["result"]["data"]["events"][0]["actor"]["id"] = Person().ref  # a host bug
        return res["result"]

    with pytest.raises(signer.SignerRefused):
        signer.run(
            call,
            "log",
            {"ref": key, "text": "x"},
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=human.person.ref,
            device=human.person.device,
            workspace=lambda w: ("Acme", "DEMO"),
            confirm=lambda text: confirmed.append(text) or True,
            show_secret=lambda t: None,
            idem="a_" + "0" * 26,
        )
    assert confirmed == [] and human.backend.payloads == []


# --- hashes recomputed from the material ----------------------------------------------------------------------------


def test_the_gate_and_question_hashes_are_recomputed_from_the_material(human, served):
    _, key = human.ticket()
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    human.check(p)
    bad = copy.deepcopy(p)
    bad["material"]["input"]["fields"]["acceptance"][0]["text"] = "something else"
    refused(bad, human, "gate hash")
    bad = copy.deepcopy(p)
    bad["events"][0]["policy_hash"] = H1
    refused(bad, human, "policy hash")
    human.agent("ask", {"ref": key, "text": "which?", "options": ["a", "b"]})
    q = human.prepare("answer", {"ref": key, "question": "Q1", "option": "a"})
    human.check(q)
    bad = copy.deepcopy(q)
    bad["material"]["question"]["text"] = "a different question"
    refused(bad, human, "question hash")


def test_a_section_text_whose_hash_differs_from_the_event_is_refused(human, served):
    _, key = human.ticket()
    p = human.prepare("section.set", {"ref": key, "section": "summary", "message": "the real text"})
    human.check(p)
    assert p["material"]["texts"] == {"summary": "the real text"}
    assert p["material"]["review"][0]["derived"]["sections"]["summary"]["text"] == "the real text"
    bad = copy.deepcopy(p)
    bad["material"]["texts"]["summary"] = "a text the person never signs"
    refused(bad, human, "does not hash to the event's section_hash")


def test_before_is_checked_against_base_rev_and_settings_keys(human, served):
    _, key = human.ticket()
    p = human.prepare("set", {"ref": key, "pairs": ["title=Better"]})
    assert p["material"]["before"] == {"ticket.title": "A ticket"}
    human.check(p)
    bad = copy.deepcopy(p)
    bad["material"]["before"]["ticket.title"] = "Not what it was"
    refused(bad, human, "base_rev")
    bad = copy.deepcopy(p)
    bad["material"]["before"]["grant_hours"] = 8
    refused(bad, human, "cannot be checked")
    settings = [{"type": "settings.changed", "set": {"grant_hours": 4, "repos": {"x": None}}}]
    signer._before({"before": {"grant_hours": 8, "repos": {}}}, settings)  # keys of set: shown, labelled unsigned
    with pytest.raises(signer.SignerRefused, match="cannot be checked"):
        signer._before({"before": {"claim_ttl_min": 30}}, settings)


# --- the Review, for every person event type ------------------------------------------------------------------------


def _types() -> list[str]:
    out: set[str] = set()
    for op in sorted(__import__("orch.model.emits", fromlist=["EMITS_V1"]).EMITS_V1):
        out |= set(review.person_op_types(op) or ())
    return sorted(out)


def _sample(typ: str) -> tuple[dict, dict]:
    """An event of ``typ`` and a material that backs it."""
    material: dict = {"kind": "payload", "ticket": {"key": "DEMO-0001", "uid": UID}}
    payload: dict = {"note": "a payload field"}
    if typ in ("member.added", "workspace.created"):
        payload = {"person": alice.ref, "device_cert": alice.cert()}
    elif typ == "device.added":
        payload = {"device": alice.device, "cert": alice.cert()}
    elif typ == "ticket.updated":
        payload = {
            "base_rev": {"body.summary": H1},
            "sections": {"summary": {"hash": canon.section_hash("t"), "refs": []}},
        }
        material["texts"] = {"summary": "t"}
    elif typ in ("gate.approved", "gate.changes_requested", "verdict.given"):
        payload = {"gate": "plan", "hash": H1}
    event = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": typ,
        "hash_v": 1,
        "roster_v": 1,
        "based_on": None,
        "auth": "passphrase",
        "actor": {"kind": "person", "id": alice.ref, "device": alice.device},
        **payload,
    }
    material["review"] = [review.of(event, material)]
    return event, material


@pytest.mark.parametrize("typ", _types())
def test_the_review_is_recomputed_by_the_signer_for_every_person_event_type(typ):
    assert allows("human", typ)
    event, material = _sample(typ)
    reader = typ in review.READER_TYPES  # H12d-2: the host attests the new reader's level (§0.8.6 item 9.11)
    if reader:
        material["review"][0]["derived"]["reader"] = 1
    signer.check_reviews(material, [event])
    (block,) = material["review"]
    assert block["payload"] == {k: v for k, v in event.items() if k not in review.ENVELOPE}
    # the host sends another payload: refused
    bad = copy.deepcopy(material)
    bad["review"][0]["payload"]["injected"] = "not in the event"
    with pytest.raises(signer.SignerRefused, match="review the host sent differs"):
        signer.check_reviews(bad, [event])
    # the host sends other derived values: refused (derived is never taken from the host)
    bad = copy.deepcopy(material)
    bad["review"][0]["derived"]["ticket_key"] = "EVIL-0001"
    with pytest.raises(signer.SignerRefused, match="review the host sent differs"):
        signer.check_reviews(bad, [event])
    # an unknown payload key is part of the review and shown (L2)
    future = {**event, "future_field": {"x": 1}}
    material2 = {**material, "review": [review.of(future, material)]}
    if reader:
        material2["review"][0]["derived"]["reader"] = 1
    signer.check_reviews(material2, [future])
    assert material2["review"][0]["payload"]["future_field"] == {"x": 1}


def test_a_reader_level_is_required_on_a_reader_event_and_must_be_a_level():
    """H12d-2: a signer refuses a reader-adding event without the host's level (a host before H12d-2) or with a
    malformed one; the level is shown as a host attestation."""
    event, material = _sample("member.added")
    with pytest.raises(signer.SignerRefused, match="no reader level"):
        signer.check_reviews(material, [event])
    for bad in (-1, "1", 1.0, True, 2**31):
        m = copy.deepcopy(material)
        m["review"][0]["derived"]["reader"] = bad
        with pytest.raises(signer.SignerRefused):
            signer.check_reviews(m, [event])
    assert review.reader_line(1).startswith("Reader: understands convenience signing (level 1)")
    assert review.reader_line(0).startswith("Reader: old (level 0)")


def test_the_derived_table(served):
    event, material = _sample("member.added")
    assert review.derived(event, material) == {
        "cert_device": alice.device,
        "cert_person": alice.ref,
        "cert_scopes_max": ["look", "decide", "operate", "type"],
        "cert_expires": None,
    }
    assert review.derived(*_sample("gate.approved")) == {"ticket_key": "DEMO-0001"}
    assert review.derived(*_sample("ticket.updated")) == {
        "sections": {"summary": {"text": "t", "hash": canon.section_hash("t")}}
    }
    assert review.derived(*_sample("grant.issued")) == {}
    assert review.derived(*_sample("log.added")) == {}


def test_an_unknown_payload_key_is_shown_in_the_review_text(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "seen"})
    p["events"][0]["future_field"] = "must be shown"
    p["material"]["review"] = [review.of(p["events"][0], p["material"])]
    p["review_digest"] = review.review_digest(p["material"])
    p["fingerprint"] = review.fingerprint(p["review_digest"], p["events"], p["op"])
    human.check(p)  # consistent, so the signer shows it...
    text = signer.render(p, lambda w: ("Acme", "DEMO"))
    assert "future_field: must be shown" in text  # ...and every payload key is in the review


# --- fingerprint (N2) -----------------------------------------------------------------------------------------------


def test_verdict_pass_and_verdict_fail_on_one_gate_have_different_fingerprints():
    base = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "verdict.given",
        "hash_v": 1,
        "roster_v": 1,
        "based_on": H2,
        "auth": "passphrase",
        "actor": {"kind": "person", "id": alice.ref, "device": alice.device},
        "gate_gen": 1,
        "hash": H1,
        "policy_hash": H2,
        "source_sha": [],
    }
    prints = []
    for outcome in ("pass", "fail"):
        ev = {**base, "outcome": outcome}
        material = {"kind": "gate", "gate": "verify", "input": {}, "ticket": {"key": "DEMO-0001", "uid": UID}}
        material["review"] = [review.of(ev, material)]
        prints.append(review.fingerprint(review.review_digest(material), [ev], "verdict"))
        # the events alone already differ: the fingerprint covers every payload field, not only the digest
        prints.append(review.fingerprint(H1, [ev], "verdict"))
    assert prints[0] != prints[2] and prints[1] != prints[3]


def test_the_fingerprint_sets_aside_only_device_auth_sig_and_a_grants_secret_hash():
    ev = {
        "type": "log.added",
        "actor": {"kind": "person", "id": alice.ref, "device": alice.device},
        "auth": "x",
        "sig": "s",
        "text": "t",
        "secret_hash": H1,
    }
    view = review.fingerprint_view(ev)
    assert view == {"type": "log.added", "actor": {"kind": "person", "id": alice.ref}, "text": "t", "secret_hash": H1}
    grant = {**ev, "type": "grant.issued"}
    assert "secret_hash" not in review.fingerprint_view(grant)
    assert review.fingerprint(H1, [ev], "log") != review.fingerprint(H1, [{**ev, "secret_hash": H2}], "log")
    assert review.fingerprint(H1, [grant], "log") == review.fingerprint(H1, [{**grant, "secret_hash": None}], "log")
    bound = {**ev, "actor": {**ev["actor"], "device": None}, "auth": None}
    assert review.fingerprint(H1, [ev], "log") == review.fingerprint(
        H1, [bound], "log"
    )  # binding keeps the fingerprint


def test_a_tampered_digest_or_fingerprint_is_refused(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    bad = copy.deepcopy(p)
    bad["review_digest"] = H1
    refused(bad, human, "review_digest")
    bad = copy.deepcopy(p)
    bad["fingerprint"] = "0" * 12
    refused(bad, human, "fingerprint")


# --- the header and the confirmation word ---------------------------------------------------------------------------


def test_sign_changes_nothing_but_sig_and_a_grants_secret_hash(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    signed = human.sign(p)
    assert [{k: v for k, v in e.items() if k != "sig"} for e in signed.events] == p["events"]
    g = human.prepare("grant", {"hours": 1})
    s = human.sign(g)
    assert {k: v for k, v in s.events[0].items() if k not in ("sig", "secret_hash")} == {
        k: v for k, v in g["events"][0].items() if k != "secret_hash"
    }
    assert "<redacted>" in repr(s) and s.secret.split(".")[1] not in repr(s)
