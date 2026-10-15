"""H12c-e (11A.15.10 item 12, 11A.15.6 step 9): the person-path material kind ``relay.pair_device`` on the host.

``human.prepare {op:"relay.pair_device", args:{offer_id}}`` takes R6's verified facts from the workspace's
``pair_device`` source (a fake here: R6c registers the real one), the terminal signer checks and renders every bound
field, and ``human.submit`` appends ``device.added {device, cert}`` with R6's precommit and commit in the one critical
section of the append choke point, under invariant I with the phone's pair declaration."""

from __future__ import annotations

import copy
import json

import pytest

from orch import canon, crypto
from orch.host import convenience, pairdevice, review, signer
from orch.host.errors import HostError
from orch.identity import pairdevice as pd
from orch.store import readerrecs
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_click import Click, _click_sign, refused
from tests.identity.helpers import Person
from tests.identity.reader_helpers import decl_o, sign_decl
from tests.store.helpers import T0

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

OFFER = "0f" * 16
ATTEMPT = "a1" * 16
NAMES = lambda w: ("acme", "ACME")  # noqa: E731 - the signer's own registry


def phone_of(owner) -> Person:
    p = Person()
    p.pk, p.pk_pub = owner.pk, owner.pk_pub
    return p


def pair_decl(p: Person, wid: str, level: int = 1, *, offer: str = OFFER, attempt: str = ATTEMPT) -> dict:
    o = decl_o(p, wid, level=level, via="pair", created_ms=T0 * 1000, surfaces=("view",))
    o["bind"] = {"via": "pair", "offer_id": offer, "attempt": attempt}
    return sign_decl(p, o)


class Source:
    """A stand-in for R6c's ``PairDeviceSource``: one offer's facts, and a record of the step-9 calls."""

    def __init__(self, served, facts: pd.PairDeviceFacts) -> None:
        self.served, self.facts_ = served, facts
        self.calls: list[tuple] = []
        self.refuse_precommit: HostError | None = None

    def facts(self, offer_id: str) -> pd.PairDeviceFacts:
        if offer_id != self.facts_.offer_id:
            raise HostError("not_found", "no such offer")
        return self.facts_

    def precommit(self, alloc) -> None:
        in_log = self.served.service.store.logged_event("workspace", alloc.event_id) is not None
        self.calls.append(("precommit", alloc, in_log))
        if self.refuse_precommit is not None:
            raise self.refuse_precommit

    def committed(self, alloc, seq: int) -> None:
        in_log = self.served.service.store.logged_event("workspace", alloc.event_id) is not None
        self.calls.append(("committed", alloc, seq, in_log))

    def refused(self, alloc, error) -> None:
        self.calls.append(("refused", alloc, error.code))


def make_facts(served, *, level: int | None = None, path: str = "7b", **kw) -> tuple[pd.PairDeviceFacts, Person]:
    owner = served.lab.ws.owner
    phone = kw.pop("phone", None) or phone_of(owner)
    decl = None if level is None else pair_decl(phone, served.wid, level)
    args = {
        "workspace_id": served.wid,
        "offer_id": OFFER,
        "attempt": ATTEMPT,
        "cert": phone.cert(scopes=("look", "decide"), created=T0 * 1000),
        "label": "Sev’s iPhone",
        "scopes": ("look", "decide"),
        "path": path,
        "code": "QW3RT6",
        "decl": decl,
        "decl_who": None if decl is None else {"person_id": crypto.person_id(owner.pk_pub).hex()},
    }
    args.update(kw)
    return pd.PairDeviceFacts(**args), phone


@pytest.fixture
def pairing(served):
    facts, phone = make_facts(served)
    src = Source(served, facts)
    served.service.pair_device = src
    yield src, phone
    served.service.pair_device = None


def _install(served, facts) -> Source:
    src = Source(served, facts)
    served.service.pair_device = src
    return src


def _submit(human, prepared):
    signed = human.sign(prepared)
    return human.submit_res(prepared["request"]["id"], signed.events)


def _ws_events(served):
    served.service.store.refresh()
    return [e for _, e in served.service.store.merged_events() if e.get("type") == "device.added"]


# --- prepare: the material, the review, the signer --------------------------------------------------------------


def test_prepare_builds_device_added_and_the_relay_pair_device_material_from_the_sources_facts(served, human, pairing):
    src, phone = pairing
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    (ev,) = prepared["events"]
    assert prepared["log"] == "workspace" and ev["type"] == "device.added"
    assert set(review.payload(ev)) == {"device", "cert"} and ev["device"] == phone.device  # no label in the event
    m = prepared["material"]
    assert {k: v for k, v in m.items() if k != "review"} == pd.material(src.facts_)
    assert m["comparison"] == {"kind": "cert_code", "code": "QW3RT6"} and m["label"] == "Sev’s iPhone"
    assert m["review"][0]["derived"]["reader"] == 0  # no declaration: level 0
    human.check(prepared)  # the terminal signer reproduces and binds everything


def test_the_review_shows_every_bound_field_and_the_word_is_the_devices_last_eight(served, human, pairing):
    src, phone = pairing
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    text = signer.render(prepared, NAMES)
    m = prepared["material"]
    for needle in (
        served.wid,
        phone.device,
        "Sev’s iPhone (named by the device; not signed, not authenticated)",
        "scopes: look, decide",
        m["cert_digest"],
        "cert_code: QW3RT6",
        OFFER,
        ATTEMPT,
        "Reader: old (level 0)",
    ):
        assert needle in text, needle
    assert signer.confirm_word(prepared, NAMES) == phone.device[-8:]


def test_path_7a_binds_the_sas(served, human):
    facts, _ = make_facts(served, path="7a", code="ZZ2345")
    _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    assert prepared["material"]["comparison"] == {"kind": "sas", "code": "ZZ2345"}
    assert "sas: ZZ2345 (the SAS you compared (path 7a))" in signer.render(prepared, NAMES)


def test_the_request_names_only_the_offer(served, human, pairing):
    for extra in ({"device": "d_" + "0" * 32}, {"label": "x"}, {"code": "AAAAAA"}):
        assert err(human.prepare_res("relay.pair_device", {"offer_id": OFFER, **extra})) == "invalid.input"
    assert err(human.prepare_res("relay.pair_device", {"offer_id": "f" * 32})) == "not_found"  # the source's refusal


def test_without_a_relay_pairing_the_op_is_unsupported(served, human):
    assert err(human.prepare_res("relay.pair_device", {"offer_id": OFFER})) == "op.unsupported"


def test_facts_of_another_workspace_or_offer_are_refused(served, human):
    facts, _ = make_facts(served, workspace_id="e" * 32)
    _install(served, facts)
    assert err(human.prepare_res("relay.pair_device", {"offer_id": OFFER})) == "internal"


def test_a_phone_certificate_of_another_person_is_refused(served, human):
    stranger = Person()
    facts, _ = make_facts(served, phone=stranger)
    _install(served, facts)
    assert err(human.prepare_res("relay.pair_device", {"offer_id": OFFER})) == "role.denied"


def test_a_plain_request_of_relay_pair_device_is_human_only(served, pairing):
    c = served.client()
    assert err(c.req("relay.pair_device", {"offer_id": OFFER}, idem=True)) == "human_only"


# --- the signer's binding: negative cases --------------------------------------------------------------------------


def _resealed(prepared, *, material=None, events=None, op=None):
    """``prepared`` with a changed material, events or op, its digest and fingerprint recomputed (a lying host)."""
    p = copy.deepcopy(prepared)
    if material is not None:
        p["material"] = material
    if events is not None:
        p["events"] = events
    if op is not None:
        p["op"] = op
    p["material"]["review"] = [review.of(e, p["material"]) for e in p["events"]]
    for block, ev in zip(p["material"]["review"], p["events"], strict=True):
        if ev["type"] in review.READER_TYPES:
            block["derived"]["reader"] = 0
    p["review_digest"] = review.review_digest(p["material"])
    p["fingerprint"] = review.fingerprint(p["review_digest"], p["events"], p["op"])
    return p


@pytest.mark.parametrize(
    "change",
    [
        {"label": "Sev’s iPhone‮"},  # not clean_label's output
        {"scopes": ["look", "decide", "operate"]},
        {"scopes": ["decide", "look"]},
        {"device": "d_" + "c" * 32},
        {"cert_digest": "sha256:" + "0" * 64},
        {"comparison": {"kind": "sas", "code": "qw3rt6"}},
        {"comparison": {"kind": "cert_code", "code": "QW3RT6", "shown": True}},
        {"workspace_id": "e" * 32},
        {"offer_id": "zz"},
        {"extra": "anything"},  # any extra field
    ],
)
def test_the_signer_refuses_each_rebound_field_even_with_a_recomputed_digest(served, human, pairing, change):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    m = {**prepared["material"], **change}
    with pytest.raises(signer.SignerRefused):
        human.check(_resealed(prepared, material=m))


def test_a_cross_kind_substitution_is_refused_both_ways(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    as_payload = {"kind": "payload", "ticket": None, "review": []}
    with pytest.raises(signer.SignerRefused, match="material of kind 'payload'"):
        human.check(_resealed(prepared, material=as_payload))
    _, key = human.ticket()
    logged = human.prepare("log", {"ref": key, "text": "hello"})
    pair_m = copy.deepcopy(prepared["material"])
    with pytest.raises(signer.SignerRefused):
        human.check(_resealed(logged, material=pair_m))


def test_an_extra_event_field_or_a_swapped_certificate_is_refused(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    ev = copy.deepcopy(prepared["events"])
    ev[0]["label"] = "Sev’s iPhone"
    with pytest.raises(signer.SignerRefused):
        human.check(_resealed(prepared, events=ev))
    other = phone_of(served.lab.ws.owner)
    ev = copy.deepcopy(prepared["events"])
    ev[0]["cert"], ev[0]["device"] = other.cert(scopes=("look", "decide"), created=T0 * 1000), other.device
    m = {**prepared["material"], "device": other.device}
    with pytest.raises(signer.SignerRefused, match="cert_digest"):
        human.check(_resealed(prepared, material=m, events=ev))


# --- convenience is refused (11A.15.6) ------------------------------------------------------------------------------


def test_pair_device_refuses_convenience_auth_at_prepare_with_the_pair_device_reason(served, human, pairing):
    res = human.prepare_res(
        "relay.pair_device", {"offer_id": OFFER}, signer={"device": human.person.device, "auth": "convenience"}
    )
    assert err(res) == "sign.convenience_unavailable"
    assert res["error"]["message"].startswith("pair_device:")
    assert res["error"]["detail"] == {"reason": "pair_device", "devices": []}


def test_pair_device_refusal_fix_text_names_custody_passphrase(served, human, pairing):
    res = human.prepare_res(
        "relay.pair_device", {"offer_id": OFFER}, signer={"device": human.person.device, "auth": "convenience"}
    )
    assert "orch keys custody passphrase" in res["error"]["hint"] and "--in-place" in res["error"]["hint"]


def test_pair_device_refuses_convenience_under_dev_opt_in(served, human, pairing, monkeypatch):
    # whatever relaxes the convenience condition (the flag on, every reader qualified, a dev opt-in): still refused
    monkeypatch.setattr(convenience, "check", lambda *a, **k: None)
    monkeypatch.setattr(convenience, "why", lambda *a, **k: None)
    monkeypatch.setenv("ORCH_DEV_CONVENIENCE", "1")
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    res = human.prepare_res(
        "relay.pair_device", {"offer_id": OFFER}, signer={"device": human.person.device, "auth": "convenience"}
    )
    assert err(res) == "sign.convenience_unavailable"


def test_a_handoff_request_is_never_bound_to_a_convenience_signer(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER}, handoff=True)
    res = human.get(prepared["request"]["id"], signer={"device": human.person.device, "auth": "convenience"})
    assert err(res) == "sign.convenience_unavailable" and res["error"]["detail"]["reason"] == "pair_device"


def test_the_signer_refuses_a_convenience_backend_and_convenience_events(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    with pytest.raises(signer.SignerRefused, match="convenience"):
        signer.check(prepared, workspace_id=served.wid, person=human.person.ref, auth="convenience")
    ev = copy.deepcopy(prepared["events"])
    ev[0]["auth"] = "convenience"
    with pytest.raises(signer.SignerRefused):
        signer.check(_resealed(prepared, events=ev), workspace_id=served.wid, person=human.person.ref)


def test_submit_refuses_convenience_events_on_a_pair_request(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    signed = human.sign(prepared)
    events = copy.deepcopy(signed.events)
    events[0]["auth"] = "convenience"
    res = human.submit_res(prepared["request"]["id"], events)
    assert err(res) in ("sign.convenience_unavailable", "human.request_mismatch")
    assert not [e for e in _ws_events(served) if e["device"] == pairing[1].device]


def test_the_click_signer_refuses_relay_pair_device_as_terminal_only(served, pairing):
    c = Click(served)
    try:
        refused(lambda: c.prepare("relay.pair_device", {"offer_id": OFFER}), "human.click_refused", "terminal_only")
    finally:
        c.close()


# --- submit: the critical section ---------------------------------------------------------------------------------


def test_submit_appends_device_added_with_precommit_before_and_commit_after(served, human, pairing):
    src, phone = pairing
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    signed = human.sign(prepared)
    res = ok(human.submit_res(prepared["request"]["id"], signed.events))
    (logged,) = [e for e in _ws_events(served) if e["device"] == phone.device]
    assert res["data"] == {"device": phone.device, "offer_id": OFFER, "attempt": ATTEMPT}
    (pre, post) = src.calls
    assert pre[0] == "precommit" and pre[2] is False  # the intent before the append
    assert post[0] == "committed" and post[3] is True and post[2] == logged["seq"]
    alloc = pre[1]
    assert alloc == post[1]
    assert (alloc.offer_id, alloc.attempt, alloc.device) == (OFFER, ATTEMPT, phone.device)
    assert alloc.review_digest == prepared["review_digest"] and alloc.event_id == logged["id"]
    assert alloc.cert_digest == prepared["material"]["cert_digest"]
    assert alloc.event_digest == pairdevice.event_digest(logged)  # recovery recomputes it from the log line
    assert alloc.decl_digest is None
    assert served.service.store.state.workspace.devices[phone.device].person == human.person.ref


def test_a_precommit_refusal_appends_nothing(served, human, pairing):
    src, phone = pairing
    src.refuse_precommit = HostError("relay.pair_closed", "the offer was cancelled")
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    res = _submit(human, prepared)
    assert err(res) == "relay.pair_closed"
    assert [c[0] for c in src.calls] == ["precommit"]  # refused() is for refusals after R6 accepted
    assert not [e for e in _ws_events(served) if e["device"] == phone.device]


def test_without_a_source_at_submit_nothing_is_appended(served, human, pairing):
    src, phone = pairing
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    signed = human.sign(prepared)
    served.service.pair_device = None
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "op.unsupported"
    assert not [e for e in _ws_events(served) if e["device"] == phone.device]


# --- readers: the H12d-2 rules through this path -------------------------------------------------------------------


def test_a_level_1_pair_declaration_is_admitted_with_its_reader_record_source_pair(served, human):
    facts, phone = make_facts(served, level=1)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    assert prepared["material"]["review"][0]["derived"]["reader"] == 1
    assert "Reader: understands convenience signing (level 1)" in signer.render(prepared, NAMES)
    ok(_submit(human, prepared))
    rec = readerrecs.read_record(served.lab.state, served.wid, phone.device[2:])
    assert rec["source"] == "pair" and rec["reader"] == 1 and rec["decl"] == facts.decl
    assert src.calls[0][1].decl_digest == rec["digest"]
    from orch.ops import readers as rd

    assert rd.levels(served.lab.state, served.wid, served.service.store.state)[phone.device] == 1


def test_a_level_1_phone_is_admitted_with_convenience_history(served, human):
    c = Click(served)
    try:
        _, key = human.ticket()
        _click_sign(c, key)
    finally:
        c.close()
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    facts, phone = make_facts(served, level=1)
    _install(served, facts)
    ok(_submit(human, human.prepare("relay.pair_device", {"offer_id": OFFER})))


@pytest.mark.parametrize(("level", "reason"), [(None, "no_declaration"), (0, "reader_too_old")])
def test_an_old_phone_is_refused_with_convenience_history(served, human, level, reason):
    c = Click(served)
    try:
        _, key = human.ticket()
        _click_sign(c, key)
    finally:
        c.close()
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    facts, _ = make_facts(served, level=level)
    _install(served, facts)
    res = human.prepare_res("relay.pair_device", {"offer_id": OFFER})
    assert err(res) == "sign.convenience_readers" and res["error"]["detail"]["reason"] == reason


def test_the_append_gate_refuses_an_old_phone_even_when_the_early_checks_were_skipped(served, human, monkeypatch):
    facts, phone = make_facts(served, level=None)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})  # no convenience yet: level 0 is fine
    signed = human.sign(prepared)
    c = Click(served)
    try:
        _, key = human.ticket()
        _click_sign(c, key)  # a convenience event lands in the history
    finally:
        c.close()
    monkeypatch.setattr(convenience, "refuse_reader_op", lambda *a, **k: None)  # the early checks are skipped
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert err(res) == "sign.convenience_readers" and res["error"]["detail"]["reason"] == "no_declaration"
    assert [c_[0] for c_ in src.calls] == ["precommit", "refused"]  # R6 ends the offer failed
    assert src.calls[1][2] == "sign.convenience_readers"
    assert not [e for e in _ws_events(served) if e["device"] == phone.device]


def test_a_declaration_bound_to_another_attempt_is_reader_decl_invalid(served, human):
    owner = served.lab.ws.owner
    phone = phone_of(owner)
    other = pair_decl(phone, served.wid, 1, attempt="b2" * 16)
    facts, _ = make_facts(served, phone=phone, decl=other, decl_who={"person_id": crypto.person_id(owner.pk_pub).hex()})
    _install(served, facts)
    res = human.prepare_res("relay.pair_device", {"offer_id": OFFER})
    assert err(res) == "reader.decl_invalid" and res["error"]["message"].startswith("bind")


def test_a_pk_pin_declaration_counts_under_the_owners_pin(served, human):
    owner = served.lab.ws.owner
    phone = phone_of(owner)
    pin = crypto.b64u(crypto.pk_pin(owner.pk_pub))
    o = decl_o(phone, served.wid, level=1, via="pair", created_ms=T0 * 1000, surfaces=("view",))
    o["bind"] = {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT}
    o["who"] = {"pk_pin": pin}
    facts, _ = make_facts(served, phone=phone, decl=sign_decl(phone, o), decl_who={"pk_pin": pin})
    _install(served, facts)
    ok(_submit(human, human.prepare("relay.pair_device", {"offer_id": OFFER})))
    from orch.ops import readers as rd

    assert rd.levels(served.lab.state, served.wid, served.service.store.state)[phone.device] == 1


def test_a_pk_pin_declaration_under_another_persons_pin_is_refused(served, human):
    """H12d-3 (9.3): a pairing declaration signed for a pk_pin counts only when the certificate's person is the member
    whose key that pin names; a pin of another key is reader.decl_invalid (who) at prepare, before anything lands."""
    from tests.identity.helpers import Person

    owner = served.lab.ws.owner
    phone = phone_of(owner)
    pin = crypto.b64u(crypto.pk_pin(Person().pk_pub))
    o = decl_o(phone, served.wid, level=1, via="pair", created_ms=T0 * 1000, surfaces=("view",))
    o["bind"] = {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT}
    o["who"] = {"pk_pin": pin}
    facts, _ = make_facts(served, phone=phone, decl=sign_decl(phone, o), decl_who={"pk_pin": pin})
    _install(served, facts)
    res = human.prepare_res("relay.pair_device", {"offer_id": OFFER})
    assert err(res) == "reader.decl_invalid" and res["error"]["message"].startswith("who")
    assert not [e for e in _ws_events(served) if e["device"] == phone.device]


# --- the F1 device.added vectors ------------------------------------------------------------------------------------


def test_the_f1_device_added_vector_is_what_relay_pair_device_builds_and_signs():
    """devices.json ``device_added``: "a device without operate is allowed" (sev3, scopes look and decide): the event
    this kind builds from that certificate is the vector's, byte for byte in its signing bytes, and the vector's own
    signature (by sev1, RFC 6979) verifies over them."""
    from tests.model.f1_runner import load

    sc = load("devices.json")["scenarios"][0]
    keys = load("signatures.json")["keys"]
    step = next(s for s in sc["steps"] if s.get("note") == "a device without operate is allowed")
    vec = step["event"]
    facts = pd.PairDeviceFacts(
        workspace_id=sc["workspace_id"],
        offer_id=OFFER,
        attempt=ATTEMPT,
        cert=vec["cert"],
        label="sev3",
        scopes=("look", "decide"),
        path="7a",
        code="ABCDEF",
    )
    built = pd.device_added(facts)
    assert built == {k: vec[k] for k in ("type", "device", "cert")}
    envelope = {k: vec[k] for k in ("v", "id", "actor", "auth", "hash_v", "roster_v", "based_on")}
    event = {**envelope, **built, **{k: vec[k] for k in ("seq", "at", "prev") if k in vec}}
    if "ws_seq" in vec:
        event["ws_seq"] = vec["ws_seq"]
    payload = canon.person_signing_bytes(sc["workspace_id"], "workspace", event)
    assert payload == canon.person_signing_bytes(sc["workspace_id"], "workspace", vec)
    pub = crypto.unb64u(keys["dk_sev1"]["pub_b64u"], crypto.PUB_LEN)
    assert crypto.verify(pub, crypto.unb64u(vec["sig"], crypto.SIG_LEN), payload)
    m = {**pd.material(facts), "review": []}
    pd.check_material(m, {**envelope, **built}, workspace_id=sc["workspace_id"], person=vec["actor"]["id"])


def test_the_f1_vectors_with_operate_or_drop_certificates_never_pass_as_a_phone():
    from tests.model.f1_runner import load

    sc = load("devices.json")["scenarios"][0]
    for step in sc["steps"]:
        ev = step["event"]
        if ev["type"] != "device.added":
            continue
        scopes = ev["cert"]["o"]["scopes_max"]
        if set(scopes) <= {"look", "decide"} and "decide" in scopes:
            continue
        with pytest.raises(pd.PairDeviceInvalid):
            pd.PairDeviceFacts(
                workspace_id=sc["workspace_id"], offer_id=OFFER, attempt=ATTEMPT, cert=ev["cert"], label="",
                scopes=("look",), path="7b", code="ABCDEF",
            )  # fmt: skip


def test_the_pair_material_json_shape_for_the_dashboard_is_stable(served, human, pairing):
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    assert sorted(prepared["material"]) == sorted([*pd.MATERIAL_FIELDS, "review"])
    json.dumps(prepared)  # plain JSON end to end


def test_the_terminal_hand_off_binds_signs_and_submits_a_pair_request(served, human):
    facts, phone = make_facts(served, level=1)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER}, handoff=True)
    assert prepared["request"]["code"] and prepared["events"][0]["auth"] is None
    bound = ok(human.get(prepared["request"]["id"], signer=human.signer))["data"]  # the declaration is rechecked
    assert bound["events"][0]["auth"] == "passphrase" and bound["material"] == prepared["material"]
    ok(_submit(human, bound))
    assert [c[0] for c in src.calls] == ["precommit", "committed"]
    assert served.service.store.state.workspace.devices[phone.device].person == human.person.ref


def test_a_roll_forward_of_a_landed_pair_event_calls_committed_again_without_a_new_precommit(served, human):
    from orch.host import human as host_human

    facts, phone = make_facts(served)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    ok(_submit(human, prepared))
    record = served.service.requests.read(prepared["request"]["id"])
    record = {**record, "outcome": None, "state": "bound"}  # as if the host died after the append
    host_human.finish(served.service, served.service.requests, record)
    assert [c[0] for c in src.calls] == ["precommit", "committed", "committed"]  # committed() is idempotent for R6


# --- review round 1 of #528 (Opus, Codex) ---------------------------------------------------------------------------


def test_a_roll_forward_over_a_same_id_event_of_other_content_refuses_and_never_commits(served, human):
    from orch.host import human as host_human

    facts, phone = make_facts(served)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    ok(_submit(human, prepared))
    record = served.service.requests.read(prepared["request"]["id"])
    events = copy.deepcopy(record["submit"]["events"])
    events[0]["sig"] = events[0]["sig"][:-2] + ("AA" if not events[0]["sig"].endswith("AA") else "BB")
    record = {**record, "outcome": None, "state": "bound", "submit": {**record["submit"], "events": events}}
    src.calls.clear()
    fut = served.service.executor.submit(host_human.finish, served.service, served.service.requests, record)
    with pytest.raises(Exception):  # noqa: B017 - the stored failure (a landed id with other content)
        fut.result()
    assert [c[0] for c in src.calls] == ["refused"]  # never committed, never precommitted
    assert src.calls[0][2] == "human.request_mismatch"


def test_the_allocation_carries_the_reviewed_scopes(served, human):
    facts, _ = make_facts(served, scopes=("look",))
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    ok(_submit(human, prepared))
    assert src.calls[0][1].scopes == ("look",) == tuple(prepared["material"]["scopes"])


def test_submit_refuses_when_r6s_facts_no_longer_rebuild_the_reviewed_material(served, human):
    facts, phone = make_facts(served, scopes=("look",))
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    signed = human.sign(prepared)
    wider, _ = make_facts(served, phone=phone, scopes=("look", "decide"))
    src.facts_ = wider  # R6's record changed since the human reviewed it
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert err(res) == "human.request_mismatch"
    assert src.calls == []  # refused before R6's precommit: nothing allocated
    assert not [e for e in _ws_events(served) if e["device"] == phone.device]


def test_precommit_is_repeated_with_the_same_allocation_after_a_crash_before_the_append(served, human, monkeypatch):
    from orch.host import human as host_human

    facts, phone = make_facts(served)
    src = _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER})
    signed = human.sign(prepared)
    store = served.service.store
    real = type(store).append
    crashed = []

    def crash_once(self, *a, **k):
        if not crashed:
            crashed.append(1)
            raise RuntimeError("the host died between the intent and the append")
        return real(self, *a, **k)

    monkeypatch.setattr(type(store), "append", crash_once)
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert not res["ok"]
    record = served.service.requests.read(prepared["request"]["id"])
    assert record["submit"] and record.get("outcome") is None
    served.service.executor.submit(host_human.finish, served.service, served.service.requests, record).result()
    names = [c[0] for c in src.calls]
    assert names == ["precommit", "precommit", "committed"]
    assert src.calls[0][1] == src.calls[1][1]  # the same allocation: R6 accepts the repeat
    assert served.service.store.state.workspace.devices[phone.device].person == human.person.ref


@pytest.mark.parametrize(
    "who", [{"person_id": "c" * 32, "pk_pin": "x"}, {"device_id": "c" * 32}, {"person_id": "C" * 32}, {"pk_pin": "!"}]
)
def test_decl_who_is_exactly_one_well_formed_person_id_or_pk_pin(served, who):
    owner = served.lab.ws.owner
    phone = phone_of(owner)
    with pytest.raises(pd.PairDeviceInvalid) as e:
        make_facts(served, phone=phone, decl=pair_decl(phone, served.wid), decl_who=who)
    assert e.value.reason == "decl"


def test_the_recheck_refuses_a_pair_entry_bound_to_another_offer_or_attempt(served, human):
    facts, phone = make_facts(served, level=1)
    _install(served, facts)
    prepared = human.prepare("relay.pair_device", {"offer_id": OFFER}, handoff=True)
    rid = prepared["request"]["id"]
    record = served.service.requests.read(rid)
    record["readers"][0]["bind"] = {"via": "pair", "offer_id": OFFER, "attempt": "b2" * 16}
    served.service.requests.write(record)
    assert err(human.get(rid, signer=human.signer)) == "human.request_mismatch"  # at binding
    record["readers"][0]["bind"] = {"via": "pair", "offer_id": OFFER, "attempt": ATTEMPT}
    served.service.requests.write(record)
    bound = ok(human.get(rid, signer=human.signer))["data"]
    signed = human.sign(bound)
    record = served.service.requests.read(rid)
    record["readers"][0]["bind"] = {"via": "pair", "offer_id": "c" * 32, "attempt": ATTEMPT}
    served.service.requests.write(record)
    assert err(human.submit_res(rid, signed.events)) == "human.request_mismatch"  # at submit
