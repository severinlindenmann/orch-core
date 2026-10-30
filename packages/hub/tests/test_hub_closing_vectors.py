"""#1463 (protocol.md 9.7): the committed ``contracts/hub/vectors_hub_closing.json`` is what the generator builds, every
notice case gives its expected answer under ``verify_closing`` (orch-core replays the same file), and the removal proof
digest is the one the console recomputes."""

from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

import pytest

from orch_hub.contract import closing as C
from orch_hub.contract import hub as H
from orch_hub.contract.canon import cj
from orch_hub.contract.keystore import OpSigner
from orch_hub.contract.backup import LABEL as BACKUP_LABEL
from orch_hub.contract.labels import ACCESS_CHANGE_LABEL, CUSTODIAL_PREFIX, HUB_LABELS, HUB_REMOVE_LABEL, V2_REUSED

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
DOC = json.loads((VEC / "vectors_hub_closing.json").read_text())
HELD = H.verify_card(DOC["card"], now_ms=DOC["now_ms"], dev_mode=True)


def _gen():
    spec = importlib.util.spec_from_file_location("make_vectors_hub_closing", VEC / "make_vectors_hub_closing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(c: dict) -> dict:
    a = c["args"]
    try:
        H.verify_closing(
            a["notice"],
            HELD,
            workspace_id=a["workspace_id"],
            binding_seq=a["binding_seq"],
            now_ms=a["now_ms"],
            dev_mode=a["dev_mode"],
            revoked_op_certs=frozenset(a["revoked_op_certs"]),
        )
        return {"ok": True}
    except H.Refused as e:
        return {"error": e.code}


def test_file_is_generated() -> None:
    assert DOC == json.loads(json.dumps(_gen().build()))


@pytest.mark.parametrize("case", DOC["cases"], ids=[c["name"] for c in DOC["cases"]])
def test_notice_case(case: dict) -> None:
    assert run(case) == case["expect"]


def test_every_refusal_class_is_covered() -> None:
    got = {run(c).get("error") for c in DOC["cases"]}
    assert got >= {
        None,
        "wrong_suite",
        "bad_signature",
        "malformed",
        "other_hub",
        "other_workspace",
        "stale_binding",
        "stale_op_cert",
        "revoked",
        "dev_refused",
        "dev_mismatch",
        "cert_invalid",
        "cert_expired",
    }


@pytest.mark.parametrize("case", DOC["digest_cases"], ids=[c["name"] for c in DOC["digest_cases"]])
def test_digest_case(case: dict) -> None:
    assert (b"orch/v2/hub-remove|" + cj(case["ctx"])).hex() == case["signed_hex"]
    assert C.remove_digest(case["ctx"]).hex() == case["digest_hex"]
    assert set(case["ctx"]) == {"contract", "kind", "hub_id", "nonce", "exp_ms", "user_id"}
    assert case["ctx"]["kind"] == "orch.hub_remove.v1"


def test_digests_differ_per_context() -> None:
    assert len({c["digest_hex"] for c in DOC["digest_cases"]}) == len(DOC["digest_cases"])


def test_the_real_signer_signs_a_notice_that_verifies() -> None:
    """The op signer (keystore) signs kind ``hub.closing`` under its own label; the verifier accepts it."""
    g = _gen()
    key = g.G.K.priv["hk_op"]
    o = C.closing_object(
        hub_id=g.G.HUB_ID,
        op_cert_id=g.G.OP_CERT_ID,
        workspace_id=g.G.WS_A,
        binding_seq=g.BSEQ,
        removal_id=g.REMOVAL_ID,
        issued_ms=g.ISSUED,
        dev=True,
    )
    signer = OpSigner(key)
    signed = signer.sign_object(o)
    out = H.verify_closing(signed, HELD, workspace_id=g.G.WS_A, binding_seq=g.BSEQ, now_ms=DOC["now_ms"], dev_mode=True)
    assert out["ends_ms"] == g.ISSUED + 7 * 86_400_000


def test_closing_object_refuses_a_life_over_seven_days() -> None:
    with pytest.raises(H.Refused):
        C.closing_object(
            hub_id="a" * 32, op_cert_id="b" * 32, workspace_id="c" * 32, binding_seq=1, removal_id="d" * 32,
            issued_ms=1, dev=True, life_ms=7 * 86_400_000 + 1,
        )  # fmt: skip


def test_labels_are_prefix_free() -> None:
    assert HUB_LABELS["sig_hub_closing"] == "orch/v2/hub-closing|"
    every = {
        *HUB_LABELS.values(),
        *V2_REUSED.values(),
        HUB_REMOVE_LABEL,
        ACCESS_CHANGE_LABEL,
        CUSTODIAL_PREFIX,
        BACKUP_LABEL.decode(),
        BACKUP_LABEL.decode().replace("backup|", "backup-aad|"),
    }
    for a, b in itertools.permutations(every, 2):
        assert not b.startswith(a), (a, b)


def test_protocol_doc_names_the_notice() -> None:
    doc = (VEC / "protocol.md").read_text()
    assert "orch/v2/hub-closing\\|" in doc and "orch/v2/hub-remove" in doc
    for op in ("closing_notice", "closing_get"):
        assert f"| `{op}` |" in doc
    for code in {c["expect"]["error"] for c in DOC["cases"] if "error" in c["expect"]} | {"hub_closing"}:
        assert f"`{code}`" in doc, code


def test_a_bool_binding_seq_is_not_current() -> None:
    """``True == 1`` in Python: the verifier's own binding_seq must be a real int (same rule as core)."""
    good = next(c for c in DOC["cases"] if c["name"] == "valid notice")["args"]
    for bad in (True, "3", 3.0, None):
        with pytest.raises(H.Refused) as e:
            H.verify_closing(good["notice"], HELD, workspace_id=good["workspace_id"], binding_seq=bad, now_ms=good["now_ms"], dev_mode=True)  # fmt: skip
        assert e.value.code == "stale_binding"
