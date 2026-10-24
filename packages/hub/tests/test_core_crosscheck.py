"""Independent check with orch-core's own primitives (canonical JSON, suite-2 verify, b64u, F1 signing bytes, the live
label table). Read-only import from an orch-core checkout; skipped when none is present (GitHub CI). Path:
$ORCH_CORE_SRC, else ~/orch-dev/orch-core/plugins/orch-core/src."""

from __future__ import annotations

import importlib
import itertools
import json
import os
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(os.environ.get("ORCH_CORE_SRC", Path.home() / "orch-dev/orch-core/plugins/orch-core/src"))
if not (SRC / "orch" / "crypto" / "suite.py").is_file():
    pytest.skip("orch-core source not present", allow_module_level=True)
sys.path.insert(0, str(SRC))
try:
    core_suite = importlib.import_module("orch.crypto.suite")
    core_enc = importlib.import_module("orch.crypto.encoding")
    core_labels = importlib.import_module("orch.crypto.labels")
    core_jcs = importlib.import_module("orch.canon.jcs")
    core_hash = importlib.import_module("orch.canon.hashing")
finally:
    sys.path.remove(str(SRC))

from orch_hub.contract.labels import HUB_LABELS

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())


def pub(name: str) -> bytes:
    return core_enc.unb64u(V["keys"][name]["pub"], 65)


def core_verify_object(signed: dict, label: str, key: str) -> bool:
    return core_suite.verify(pub(key), core_enc.unb64u(signed["sig"], 64), label.encode() + core_jcs.dumps(signed["o"]))


def test_binding_and_unbound_with_core_primitives():
    b, u = V["objects"]["binding_a_1"], V["objects"]["unbound_a_1"]
    assert core_verify_object(b, "orch/v2/sig/hub-binding|", "wsk_a")
    assert core_verify_object(u, "orch/v2/sig/hub-unbound|", "wsk_a")
    assert not core_verify_object(b, "orch/v2/sig/hub-unbound|", "wsk_a")
    assert not core_verify_object(b, "orch/v2/sig/hub-binding|", "wsk_b")
    # the derived hub id, with core's hash
    root = core_enc.unb64u(b["o"]["hk_root_pub"], 65)
    assert core_suite.sha256(b"orch/v2/id/hub|" + bytes([2]) + root)[:16].hex() == b["o"]["hub_id"]


def test_publish_request_with_core_primitives():
    p = V["publish"]
    r, h = p["request"], p["request"]["headers"]

    def lp16(s: str) -> bytes:
        return struct.pack(">H", len(s)) + s.encode()

    appr = core_jcs.loads_strict(core_enc.unb64u(h["Orch-Owner-Approval"]))
    signed = (
        b"orch/v2/publish-hub|"
        + bytes([2])
        + bytes.fromhex(h["Orch-Workspace"])
        + struct.pack(">Q", int(h["Orch-Ts"]))
        + bytes.fromhex(h["Orch-Nonce"])
        + lp16(r["method"])
        + lp16(r["host"])
        + lp16(r["target"])
        + core_suite.sha256(bytes.fromhex(r["body_hex"]))
        + bytes.fromhex(h["Orch-Hub"])
        + core_suite.sha256(core_jcs.dumps(appr))
    )
    assert signed.hex() == p["wsk_signed_hex"]
    assert core_suite.verify(pub("wsk_a"), core_enc.unb64u(h["Orch-Signature"], 64), signed)
    hub_bytes = b"orch/v2/hub-publish|" + bytes([2]) + bytes.fromhex(h["Orch-Hub"]) + core_suite.sha256(signed)
    assert core_suite.verify(pub("hk_op"), core_enc.unb64u(h["Orch-Hub-Signature"], 64), hub_bytes)
    assert core_verify_object(appr, "orch/v2/sig/hub-publish-approval|", "dk_alice_mac.sig")
    assert core_verify_object(appr["device_cert"], "orch/v2/sig/device-cert|", "pk_alice")
    assert core_suite.person_id(pub("pk_alice")).hex() == V["ids"]["person_alice"]
    assert core_suite.device_id(pub("dk_alice_mac.sig")).hex() == appr["o"]["device_id"]


def test_hub_joined_is_a_core_person_event():
    j = V["hub_joined"]
    msg = core_hash.person_signing_bytes(V["ids"]["workspace_a"], "workspace", j["event"])
    assert msg.hex() == j["signed_hex"]
    assert core_suite.verify(pub(j["signer"]), core_enc.unb64u(j["event"]["sig"], 64), msg)


def test_custodial_context_is_core_signed_context():
    c = V["custodial"]
    e = {k: v for k, v in c["event"].items() if k != "sig"}
    ticket = core_hash.signed_context("sig_ticket_event", c["workspace_id"], c["log"], e)
    body = ticket[len(b"orch/v2/sig/ticket-event|") :]
    assert bytes.fromhex(c["uk_signed_hex"]) == c["uk_label"].encode() + body


def test_every_canonical_byte_string_is_core_cj():
    for e in V["signatures"]:
        raw = bytes.fromhex(e["canonical_hex"])
        assert core_jcs.dumps(core_jcs.loads_strict(raw)) == raw


def test_hub_labels_prefix_free_with_live_core_table():
    live = {v for v in core_labels.LABELS.values() if v.endswith("|")}
    live |= {v for v in core_labels.PLANNED_LABELS.values() if v.endswith("|")}
    live |= set(core_hash.LABELS.values())
    hub = set(HUB_LABELS.values()) | {"orch/v2/hub-custodial|gate.approved|"}
    # core's C2a labels (what the WSK signs for a hub) are the same strings as the hub's: shared on purpose. Every shared
    # string must carry the same NAME on both sides; the rest stays disjoint, and the union stays prefix-free.
    core_names = {v: k for k, v in {**core_labels.LABELS, **core_labels.PLANNED_LABELS}.items() if v.endswith("|")}
    hub_names = {v: k for k, v in HUB_LABELS.items()}
    shared = (set(hub_names) | {"orch/v2/hub-custodial|gate.approved|"}) & set(core_names)
    assert all(core_names[x] == hub_names[x] for x in shared), {x for x in shared if core_names[x] != hub_names.get(x)}
    for a, b in itertools.permutations(sorted(live | hub), 2):
        assert not b.startswith(a), (a, b)


def test_v2_card_check_accepts_the_relay_corpus_card():
    """L2-3: the hub's v2 directory-card check accepts the card from orch-relay's own vectors (as copied into
    orch-core's publish_v2.json), an independent fixture, and refuses it under another owner key."""
    from orch_hub.contract import hub as H

    fx = SRC.parent / "tests" / "publish" / "vectors" / "publish_v2.json"
    v2 = json.loads(fx.read_text())
    card = v2["card"]["card"]
    pk = bytes.fromhex(v2["keys"]["person_alice.sig"]["pub"])
    assert H.verify_v2_card(card, pk, now_ms=card["o"]["issued_ms"])["workspace_id"] == v2["ids"]["workspace_a"]
    other = bytes.fromhex(v2["keys"]["wsk_a.sig"]["pub"])
    try:
        H.verify_v2_card(card, other, now_ms=card["o"]["issued_ms"])
    except H.Refused as r:
        assert r.code == "other_person"
    else:
        raise AssertionError("card accepted under another owner key")


def test_question_hash_and_refusals_agree_with_live_core():
    """#1123: the hub's raw-question hash equals core's ``question_hash`` on every frozen fixture, and everything core
    refuses is either unhashable there or not renderable here. Needs core code with the raw-question entry (core PR #1135
    or later); skipped on an older checkout."""
    import inspect

    from orch_hub.view import project as PR

    if list(inspect.signature(core_hash.question_hash).parameters)[:3] != ["qid", "ticket_uid", "question"]:
        pytest.skip("core checkout predates the raw-question hash")
    f = json.loads((Path(__file__).resolve().parent / "data" / "core_f1" / "hashes.json").read_text())
    for v in f["question_hash"]:
        assert PR.question_hash(v["qid"], v["ticket"], v["question"]) == core_hash.question_hash(
            v["qid"], v["ticket"], v["question"]
        ), v["name"]
    for v in f["question_hash_refused"]:
        with pytest.raises(core_hash.HashError):
            core_hash.question_hash("a" * 32, "T", v["question"])


def test_market_policy_constants_match_core():
    """D14: the official marketplace URL and the capability list the policy validator mirrors."""
    import re

    from orch_hub.contract import market_policy as MP

    # read as text: importing orch.market / orch.addons would pull in core's whole dependency set
    consts = (SRC / "orch" / "market" / "constants.py").read_text()
    manifest = (SRC / "orch" / "addons" / "manifest.py").read_text()
    official = re.search(r'^OFFICIAL_URL = "([^"]+)"', consts, re.MULTILINE)
    caps = re.search(r"^CAPABILITIES = \(([^)]*)\)", manifest, re.MULTILINE)
    assert official and caps
    assert MP.OFFICIAL_URL == official.group(1)
    assert sorted(re.findall(r'"([a-z_]+)"', caps.group(1))) == list(MP.CAPABILITIES)


def _norm(pattern: str) -> str:
    """core's JSON-schema spelling (``^…(?![\\s\\S])``) to the hub's Python spelling (``…\\Z``)."""
    return pattern.removeprefix("^").replace(r"(?![\s\S])", r"\Z")


def test_market_policy_patterns_match_core_schema():
    """D14 L1: the hand-copied URL, semver, key and id patterns and the case-insensitive hosts equal core's own, so a
    core-side change cannot make hub and core call each other's policies malformed."""
    import re

    from orch_hub.contract import market_policy as MP

    defs = json.loads((SRC / "orch" / "addons" / "schemas" / "market.v1.json").read_text())["$defs"]
    assert MP._GIT_URL.pattern == _norm(defs["gitUrl"]["pattern"])
    assert MP._SEMVER + r"\Z" == _norm(defs["semver"]["pattern"])
    assert MP._B64U_KEY.pattern == _norm(defs["b64uKey"]["pattern"])
    assert MP._ID.pattern == _norm(defs["name"]["pattern"])
    consts = (SRC / "orch" / "market" / "constants.py").read_text()
    hosts = re.search(r"^CASE_INSENSITIVE_HOSTS = \(([^)]*)\)", consts, re.MULTILINE)
    assert hosts and tuple(re.findall(r'"([^"]+)"', hosts.group(1))) == MP.CASE_INSENSITIVE_HOSTS
