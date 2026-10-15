"""H12c builders 1-4 for R6 pairing (design 11A.15.10), against the vendored relay corpus: every matching case, accept
and refuse. Ground truth is orch-relay ``docs/protocol-v2.md`` §4, §7.2, §8.3, §8.4 and the corpus; where the design
text differs, the vectors win (the PR lists each difference)."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.identity import Refused, pairing

from ..relay import corpus

S2 = corpus.S2

# --- 1. wk_commit (protocol §7.2, corpus ``wk_commit``) -------------------------------------------------------------

WK_COMMIT = S2["wk_commit"]


@pytest.mark.parametrize("case", WK_COMMIT, ids=[c["name"] for c in WK_COMMIT])
def test_wk_commit_vectors(case):
    got = pairing.wk_commit(case["workspace_id"], case["epoch"], bytes.fromhex(case["wk"]))
    assert got == case["wk_commit"]


def test_wk_commit_covers_every_corpus_case():
    assert {c["name"] for c in WK_COMMIT} == {"epoch_1", "epoch_2", "other_workspace_same_key", "epoch_u32_max"}


_WS = WK_COMMIT[0]["workspace_id"]
_WK = bytes.fromhex(WK_COMMIT[0]["wk"])


@pytest.mark.parametrize(
    ("ws", "epoch", "wk"),
    [
        (_WS, 0, _WK),  # epoch 0 is the pairing epoch, never a key epoch
        (_WS, 2**32, _WK),  # above u32
        (_WS, -1, _WK),
        (_WS, True, _WK),  # a bool is not an epoch
        (_WS, 1.0, _WK),
        (_WS, "1", _WK),
        (_WS, 1, _WK[:31]),
        (_WS, 1, _WK + b"\0"),
        (_WS, 1, bytearray(_WK)),  # exactly bytes
        (_WS, 1, _WK.hex()),
        (_WS.upper(), 1, _WK),  # ids are lower-case hex
        (_WS[:-2], 1, _WK),
        (bytes.fromhex(_WS), 1, _WK),  # the text form only
    ],
)
def test_wk_commit_refuses_out_of_range_inputs(ws, epoch, wk):
    with pytest.raises(Refused) as e:
        pairing.wk_commit(ws, epoch, wk)
    assert e.value.code == "malformed"
