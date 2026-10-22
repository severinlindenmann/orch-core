"""vectors/vectors_hub.json both ways: the reference rebuilds the file byte for byte, and every case, signature and
byte string replays from its JSON alone (keys from their seeds, signatures verified, deterministic re-signing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj, loads_strict, unb64u
from orch_hub.contract.cases import outcome
from orch_hub.contract.labels import HUB_LABELS, custodial_label
from orch_hub.contract.vectorgen import build

PATH = Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json"
V = json.loads(PATH.read_text())
KEYS = {n: S.key_from_seed(bytes.fromhex(k["seed"])) for n, k in V["keys"].items()}


def pub(name: str) -> bytes:
    return unb64u(V["keys"][name]["pub"], 65)


def test_file_is_what_the_reference_builds():
    assert PATH.read_text() == json.dumps(build(), indent=1, sort_keys=True) + "\n"


def test_header():
    assert V["contract"] == "orch.hub/0" and V["suite"] == 2 and V["hv"] == 0
    assert "FAKE" in V["comment"] and "TEST-ONLY" in V["comment"]


@pytest.mark.parametrize("name", sorted(V["keys"]))
def test_key_from_seed(name):
    k = V["keys"][name]
    assert bytes.fromhex(k["seed"]) == S.fake_seed("hub/" + name)
    assert b64u(S.public_bytes(KEYS[name])) == k["pub"]


def test_labels_match():
    assert {k: v for k, v in V["labels"].items() if k != "custodial_family"} == HUB_LABELS


def test_ids():
    ids = V["ids"]
    assert ids["hub"] == H.hub_id(pub("hk_root"))
    assert ids["hub_rogue"] == H.hub_id(pub("hk_root_rogue"))
    assert ids["person_alice"] == H.person_id(pub("pk_alice"))
    assert ids["channel_alice_phone"] == H.channel_id(pub("channel_alice_phone"))


@pytest.mark.parametrize("case", V["cases"], ids=[c["name"] for c in V["cases"]])
def test_case(case):
    assert outcome(case) == case["expect"]


def test_every_case_has_a_phase_and_negatives_cover_the_brief():
    names = {c["name"] for c in V["cases"]}
    assert all(c["phase"] in (2, 3, 4) for c in V["cases"])
    for must in ("card_under_snapshot_label", "uk_cert_signed_by_root", "uk_cert_expired", "publish_binding_of_other_workspace",
                 "binding_replayed_after_leave", "publish_after_leave", "card_missing_dev_marker", "card_extra_field",
                 "card_b64u_padding", "card_text_duplicate_key", "card_dev_at_real_verifier", "link_sign_request_refused"):
        assert must in names


@pytest.mark.parametrize("e", V["signatures"], ids=[e["name"] for e in V["signatures"]])
def test_signature_both_ways(e):
    canonical = bytes.fromhex(e["canonical_hex"])
    assert cj(loads_strict(canonical)) == canonical
    msg = bytes.fromhex(e["signed_hex"])
    assert msg == e["label"].encode() + canonical
    sig = unb64u(e["sig"], 64)
    assert S.verify(pub(e["signer"]), sig, msg)
    assert S.sign(KEYS[e["signer"]], msg, deterministic=True) == sig
    assert not S.verify(pub(e["signer"]), sig, b"orch/v2/hub-x|" + canonical)


def test_custodial_bytes():
    c = V["custodial"]
    ev = c["event"]
    e_signed = {k: v for k, v in ev.items() if k != "sig"}
    digest = H.event_digest(c["workspace_id"], c["log"], e_signed)
    assert digest.hex() == c["digest_hex"]
    assert H.channel_proof_bytes(digest).hex() == c["proof_signed_hex"]
    assert c["uk_label"] == custodial_label(ev["type"]).decode() == "orch/v2/hub-custodial|gate.approved|"
    ub = bytes.fromhex(c["uk_signed_hex"])
    assert ub == H.custodial_bytes(c["workspace_id"], c["log"], e_signed)
    assert S.verify(pub("uk_alice"), unb64u(ev["sig"], 64), ub)
    assert S.verify(pub("channel_alice_phone"), unb64u(ev["channel_proof"], 64), bytes.fromhex(c["proof_signed_hex"]))


def test_hub_joined_bytes():
    j = V["hub_joined"]
    e = {k: v for k, v in j["event"].items() if k != "sig"}
    msg = b"orch/v2/sig/ws-event|" + cj(H.signed_context(V["ids"]["workspace_a"], "workspace", e))
    assert msg.hex() == j["signed_hex"]
    assert S.verify(pub(j["signer"]), unb64u(j["event"]["sig"], 64), msg)
    H.check_joined_payload({k: e[k] for k in ("hub_id", "hk_root_pub", "mode", "binding_seq", "aliases")})


def test_publish_bytes():
    p = V["publish"]
    r = p["request"]
    h = r["headers"]
    ws = bytes.fromhex(h["Orch-Workspace"])
    body = bytes.fromhex(r["body_hex"])
    pb = H.publish_signed_bytes(ws, int(h["Orch-Ts"]), bytes.fromhex(h["Orch-Nonce"]), r["method"], r["host"],
                                r["target"], body)
    assert pb.hex() == p["wsk_signed_hex"]
    assert S.verify(pub("wsk_a"), unb64u(h["Orch-Signature"], 64), pb)
    hb = H.hub_publish_bytes(h["Orch-Hub"], pb)
    assert hb.hex() == p["hub_signed_hex"]
    assert S.verify(pub("hk_op"), unb64u(h["Orch-Hub-Signature"], 64), hb)
    rh = H.publish_request_hash(ws, r["method"], r["host"], r["target"], body)
    assert b64u(rh) == p["request_hash"]
    appr = loads_strict(unb64u(h["Orch-Owner-Approval"]))
    assert appr == V["objects"]["publish_approval"] and appr["o"]["request_hash"] == p["request_hash"]


def test_link_auth_bytes():
    a = V["link_auth"]
    msg = H.link_auth_bytes(a["hub_id"], a["workspace_id"], bytes.fromhex(a["challenge_hex"]),
                            bytes.fromhex(a["exporter_hex"]))
    assert msg.hex() == a["signed_hex"]
    assert S.verify(pub(a["signer"]), unb64u(a["sig"], 64), msg)


def test_real_signer_randomises_and_still_verifies():
    msg = b"orch/v2/hub-card|{}"
    s1, s2 = S.sign(KEYS["hk_op"], msg), S.sign(KEYS["hk_op"], msg)
    assert s1 != s2 and S.verify(pub("hk_op"), s1, msg) and S.verify(pub("hk_op"), s2, msg)
