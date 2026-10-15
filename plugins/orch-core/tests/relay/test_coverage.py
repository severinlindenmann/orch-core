"""Which parts of the vendored relay corpus R1 consumes, and why the rest is not R1's (11A.10: sections move to
"consumed" as each group lands). A new fixture file or vector section fails here until someone classifies it.
Also the ``genesis.json`` fixtures: the host's local pin check agrees with every relay refusal it can predict."""

from __future__ import annotations

import pytest

from orch import canon
from orch.relay import retry
from orch.relay.session import Fenced

from . import corpus
from .helpers import GENESIS, V_ORIGIN, WS, Alarms, directory
from .helpers import session as make_session
from .mock import MockRelay, fixture_expect
from .test_session import login_expects

R1_FIXTURES = {
    "session_challenge": "test_session",
    "session_login": "test_session (every case: success body-checked, each refusal mapped)",
    "session_logout": "test_session",
    "host_hold": "test_session (every case)",
    "card_put": "test_session (every case: first card without session, W(g), each refusal)",
    "card_get": "test_session",
    "members": "test_session (WSK signature and workspace; field set is R6)",
    "checkpoint_get": "test_session (success verified; 404, 410)",
    "genesis": "test_coverage (success; local pin check vs relay refusals)",
    "relay_info": "test_relay_info (H12d-3: link step 1 and every R1 login, orch-v2-host §0.8.6 item 9.8)",
}
NOT_R1 = {
    "accounts": "owner client (P3-D3/D34; orch.relay_owner, tests/relay_owner/test_client.py)",
    "device_put": "owner client: PK-signed certificate upload (P3-D5; tests/relay_owner/test_client.py)",
    "device_get": "R6; the owner client reads it too (tests/relay_owner/test_client.py)",
    "revocations_post": "owner client (PK-signed revocation)",
    "revocation_get": "R4/R6 (revocation evidence)",
    "revocation_feed": "R4/R6",
    "bridge_device_poll": "device side",
    "bridge_device_post": "device side",
    "bridge_host_post": "R2 (response chunks)",
    "host_poll": "R2 (poll thread) and R4 (notices.unpublished)",
    "pair_device_post": "device side",
    "pair_host_post": "R6 (pairing answers)",
    "pair_poll": "R6",
    "cert_challenge": "owner client (tests/relay_owner/test_client.py)",
    "cert_rejection": "owner client (tests/relay_owner/test_client.py)",
    "cert_request_get": "R6",
    "cert_requests_poll": "owner client (tests/relay_owner/test_client.py)",
    "cert_requests_post": "R6",
    "card_key": "device side (card-key wraps)",
    "card_keys": "R6 (card-key wraps)",
    "grants": "device side (WK grants)",
    "membership": "R6 (PUT membership)",
    "checkpoint_put": "R3 (final publication)",
    "staging_page": "R3",
    "staging_manifest": "R3",
    "baseline_post": "R3/devices (P3-D13)",
    "baseline_delete": "R3/devices",
    "proof": "devices and R3",
    "transitions": "devices and R3",
    "unpublished_get": "R4",
    "unpublished_ack": "R4",
    "drop_put": "H13b-2 (the uploader); the ODU2 frame itself is H13-0's (tests/crypto/test_drop.py, upload_frame)",
    "drop_list": "H13b-1 (the Drop thread's mirror)",
    "drop_descriptors": "H13b-1",
    "drop_descriptor_get": "H13b-1 and H13b-2 (read-back after a lost answer)",
    "drop_wraps_get": "H13b-1",
    "drop_blob_get": "H13b-1 (downloads)",
}
R1_SECTIONS = {
    "relay_auth": "test_wire (signed bytes, every case's signature layer), test_sign, test_adversarial",
    "card": "test_card (all 19 cases, sealed part, ck_commit), test_sign (signed bytes)",
    "hkdf": "test_wire (k_bridge epochs 1-2, k_pair, k_msg_bridge); other derivations are not R1's",
    "bridge": "test_wire (seal vector; ORB2 layer of all 47 host_cases); the rest of §8.6 is R2",
    "keys": "test fixtures (seeded keys)",
    "ids": "test fixtures",
    "suite": "metadata",
    "name": "metadata",
}
NOT_R1_SECTIONS = {
    "sign": "crypto tests (tests/crypto)",
    "salted_aead": "crypto tests",
    "seal": "crypto tests; H12c seal_wk (tests/identity/test_memberlist.py)",
    "seal_open": "crypto tests; tests/identity/test_memberlist.py",
    "label_sealed": "identity tests",
    "certs": "identity tests (R1 uses two signed certs as hostile inputs)",
    "revocations": "identity tests / R4",
    "wk_grants": "H12c builders (tests/identity/test_memberlist.py); R6 publishes them",
    "wk_commit": "H12c builders (tests/identity/test_pairing.py)",
    "member_lists": "H12c builders (tests/identity/test_memberlist.py; checks 1-3, the rest is the relay's)",
    "member_list_snapshots": "H12c builders (tests/identity/test_memberlist.py; the D2 state machine is the device's)",
    "cert_request": "H12c builders (tests/identity/test_certreq.py; signed_cert/sign_again are the owner client's)",
    "enroll": "device side",
    "webauthn": "device side",
    "question_hash": "canon tests",
    "decisions": "not served in v1.7 (11A.11)",
    "revocation_op": "owner client",
    "push": "H13-0 builders (tests/crypto/test_h13_push.py); the push worker is H13a",
    "drop": "H13-0 builders (tests/crypto/test_h13_drop.py); the Drop thread is H13b",
    "ws_envelopes": "ws-to-ws (later phase)",
    "publish": "orch-publish (not the relay client)",
    "p3": "relay-side state machine cases; R2/R3/R6 consume the host-relevant ones",
    "checkpoints": "R3 (R1 reads one checkpoint through checkpoint_get)",
    "cert_binding": "H12c builders (tests/identity/test_certreq.py, test_pair_answers.py, test_pairing.py)",
    "rejected_forms": "H12c builders (tests/identity/test_pair_answers.py, test_certreq.py)",
    "approved_anchor": "H12c builders (tests/identity/test_pair_answers.py; join is the device's)",
    "checkpoints_g4": "R3",
    "unpublished": "R4",
    "reader_decl": "identity tests (tests/identity/test_reader_decl.py: decls, pair_meta gates); the pair meta and "
    "rd in the request meta: tests/identity/test_pair_gate.py (pair_meta gates), tests/relay/test_relay_rd.py (H12d-3)",
}
TOP_LEVEL = {"encodings": "test_wire (strict_parse, b64u)", "labels": "tests/crypto", "constants": "test_wire (bridge)"}


def test_every_fixture_file_is_classified():
    files = {p.stem for p in corpus.FIXTURES.glob("*.json") if p.name != "MANIFEST.json"}
    assert files == set(R1_FIXTURES) | set(NOT_R1), files ^ (set(R1_FIXTURES) | set(NOT_R1))
    assert not set(R1_FIXTURES) & set(NOT_R1)


def test_every_vector_section_is_classified():
    sections = set(corpus.S2)
    assert sections == set(R1_SECTIONS) | set(NOT_R1_SECTIONS), sections ^ (set(R1_SECTIONS) | set(NOT_R1_SECTIONS))
    assert {"comment", "version", "ed25519_small_order", "suites"} | set(TOP_LEVEL) == set(corpus.VECTORS)


# --- genesis.json ---------------------------------------------------------------------------------------------------


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


def holder_session(relay, **kw):
    relay.expect(*login_expects(), fixture_expect("host_hold", "success"))
    s = make_session(relay.origin(), V_ORIGIN, **kw)
    s.hold()
    return s


def test_genesis_success_fixture(relay):
    s = holder_session(relay)
    relay.expect(fixture_expect("genesis", "success"))
    body = corpus.case("genesis", "success")["request"]["json"]
    directory(s).put_genesis(body["pin"], body["cert"], genesis=GENESIS)


@pytest.mark.parametrize(
    "name", ["refusal_401_bad_signature", "refusal_409_restore_not_owner", "refusal_409_genesis_mismatch"]
)
def test_genesis_pins_the_relay_refuses_are_refused_locally(relay, name):
    """``genesis_non_owner``, ``genesis_decide_only`` and ``genesis_other_wsk``: the host never sends them."""
    s = holder_session(relay)
    body = corpus.case("genesis", name)["request"]["json"]
    n = len(relay.seen)
    with pytest.raises(ValueError):
        directory(s).put_genesis(body["pin"], body["cert"], genesis=GENESIS)
    assert len(relay.seen) == n


@pytest.mark.parametrize("name", ["refusal_403_not_eligible", "refusal_409_fenced", "refusal_400_malformed"])
def test_genesis_refusals_map(relay, name):
    alarms = Alarms()
    s = holder_session(relay, alarms=alarms)
    relay.expect(fixture_expect("genesis", name, body=None))
    body = corpus.case("genesis", "success")["request"]["json"]
    if name == "refusal_409_fenced":
        with pytest.raises(Fenced):
            directory(s).put_genesis(body["pin"], body["cert"], genesis=GENESIS)
        assert alarms.codes() == ["fenced"]
        return
    with pytest.raises(retry.RelayRefused) as e:
        directory(s).put_genesis(body["pin"], body["cert"], genesis=GENESIS)
    assert retry.classify(e.value, membership=False) == retry.PERMANENT


def test_genesis_fixture_body_is_cj():
    body = corpus.case("genesis", "success")["request"]["json"]
    assert canon.dumps(body) == corpus.request_body(corpus.case("genesis", "success")["request"])
    assert body["pin"]["o"]["workspace_id"] == WS
