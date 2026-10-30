"""#1463 (hub protocol 9.7): the host verifies a ``hub.closing`` notice exactly as the hub signs it. The ``cases`` of
the shared ``contracts/hub/vectors_hub_closing.json`` are replayed with the core verifier and with the test-side
port (``tests/hub/contract.py``); the hub replays the same file against its own. ``digest_cases`` are the console's
and are not read here: core never sees the removal proof."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.hubclient import closing as CL
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_hub_closing.json").read_text())
HELD = wire.verify_card(VEC["card"], now_ms=VEC["now_ms"], dev_mode=True)


def run(case: dict) -> dict:
    a = case["args"]
    try:
        out = CL.verify_closing(
            a["notice"],
            HELD,
            workspace_id=a["workspace_id"],
            binding_seq=a["binding_seq"],
            now_ms=a["now_ms"],
            dev_mode=a["dev_mode"],
            revoked_op_certs=frozenset(a["revoked_op_certs"]),
        )
    except HubRefused as e:
        return {"error": e.code}
    assert set(out) == {"removal_id", "issued_ms", "ends_ms", "binding_seq"}  # the closed result
    return {"ok": True}


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["name"] for c in VEC["cases"]])
def test_notice_case(case: dict) -> None:
    assert run(case) == case["expect"]


def test_every_list_of_the_file_is_accounted_for() -> None:
    """A new list in the file must be replayed or named here as not core's."""
    assert {k for k, v in VEC.items() if isinstance(v, list)} == {"cases", "digest_cases"}


def test_the_refusal_classes_of_the_file_are_all_exercised() -> None:
    got = {run(c).get("error") for c in VEC["cases"]}
    assert got >= {
        None,
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


def test_the_result_is_the_four_recorded_members() -> None:
    good = next(c for c in VEC["cases"] if c["name"] == "valid notice")["args"]
    out = CL.verify_closing(
        good["notice"],
        HELD,
        workspace_id=good["workspace_id"],
        binding_seq=good["binding_seq"],
        now_ms=good["now_ms"],
        dev_mode=True,
    )
    o = good["notice"]["o"]
    assert out == {k: o[k] for k in ("removal_id", "issued_ms", "ends_ms", "binding_seq")}


def test_a_non_int_binding_seq_is_not_current() -> None:
    good = next(c for c in VEC["cases"] if c["name"] == "valid notice")["args"]
    for bad in ("3", 3.0, None, True):
        with pytest.raises(HubRefused) as e:
            CL.verify_closing(
                good["notice"], HELD, workspace_id=good["workspace_id"], binding_seq=bad,
                now_ms=good["now_ms"], dev_mode=True,
            )  # fmt: skip
        assert e.value.code == "stale_binding"


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["name"] for c in VEC["cases"]])
def test_notice_case_test_side_port(case: dict) -> None:
    from tests.hub import contract as C

    held = C.verify_card(VEC["card"], now_ms=VEC["now_ms"], dev_mode=True)
    a = case["args"]
    try:
        C.verify_closing(
            a["notice"], held, workspace_id=a["workspace_id"], binding_seq=a["binding_seq"], now_ms=a["now_ms"],
            dev_mode=a["dev_mode"], revoked_op_certs=frozenset(a["revoked_op_certs"]),
        )  # fmt: skip
        got = {"ok": True}
    except C.Refused as e:
        got = {"error": e.code}
    assert got == case["expect"]


def test_the_label_is_prefix_free_with_every_label_core_knows() -> None:
    from orch import canon
    from orch.canon import hashing as core_hash
    from orch.crypto import labels
    from tests.hub import contract as C

    label = "orch/v2/hub-closing|"
    assert CL.SIG_CLOSING == wire.SIG_CLOSING == C.HUB_LABELS["sig_hub_closing"].encode() == label.encode()
    assert VEC["card"]["o"]["hub_id"]  # the vectors file the cases come from is the one the hub signs
    every = {
        *C.HUB_LABELS.values(),
        *(v for v in {**labels.LABELS, **labels.PLANNED_LABELS, **canon.LABELS}.values() if v.endswith("|")),
        *core_hash.LABELS.values(),
        "orch/v2/hub-remove|",  # the console's removal digest label (hub only)
    }
    every.discard(label)
    for other in every:
        assert not other.startswith(label) and not label.startswith(other), other


def test_closing_get_is_a_declared_w2h_op_with_an_exact_reply() -> None:
    """Contract 10.2: ``closing_get`` is w2h in both modes, reply exactly ``{"closing": notice | null}``;
    ``snapshot_get`` and ``hub_where`` keep their exact keys (no ``closing`` member)."""
    from orch.hubclient import link
    from orch.hubclient.tlslink import client as tls
    from tests.hub import contract as C

    assert link.W2H_OPS["closing_get"] is None and tls.W2H_OPS["closing_get"] is None
    assert C.LINK_OPS["closing_get"][:2] == ("w2h", None) and C.LINK_OPS["closing_notice"][:2] == ("h2w", None)
    ok = tls._REPLY["closing_get"]  # noqa: SLF001
    notice = VEC["cases"][0]["args"]["notice"]
    assert ok({"closing": None}) and ok({"closing": notice})
    for bad in ({}, {"closing": "x"}, {"closing": []}, {"closing": None, "x": 1}, {"notice": None}):
        assert not ok(bad), bad
    assert not tls._REPLY["hub_where"]({"addresses": [], "name": "x", "closing": None})  # noqa: SLF001
