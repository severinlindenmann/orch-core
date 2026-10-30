"""R3 review, invariant B: the contract's ``app_seq_*`` vectors replayed against the host's REAL ``Tracker``
(not the test port of ``remote_port``), so the two cannot drift apart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.host.remote import session as S

DOC = json.loads((Path(__file__).resolve().parents[4] / "contracts" / "hub" / "vectors_remote.json").read_text())
CASES = [c for c in DOC["cases"] if c["check"] == "app_seq"]


def _frame(spec: dict) -> bytes:
    if "hex" in spec:
        return bytes.fromhex(spec["hex"])
    f = spec["fill"]
    return bytes([f["kind"]]) + f["id"].to_bytes(4, "big") + bytes([f["byte"]]) * f["len"]


def _replay(frames: list[dict]) -> dict:
    tr, out = S.Tracker(), []
    for i, f in enumerate(frames):
        pt = _frame(f)
        try:
            kind, ident = S.app_header(pt)
            out.append(
                tr.step(
                    f["dir"],
                    kind,
                    ident,
                    len(pt) - 5,
                    S.head_status(pt) if f["dir"] == "h2d" and kind == S.HEAD else None,
                )
            )
        except S._Fail:  # noqa: SLF001
            return {"error": "malformed", "index": i}
    return {"results": out}


def test_there_are_app_seq_vectors():
    assert len(CASES) >= 30


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_host_tracker_replays_the_app_seq_vector(c):
    assert _replay(c["args"]["frames"]) == c["expect"], c.get("note")
