"""D3 shared event vectors: stage, digest and caps survive transport and are signature-bound."""

import json
from pathlib import Path

from orch_relay.proto import SUITE, cj, unb64u

V = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/core/f1/signatures.json").read_text()
)
EXCLUDED = {"seq", "at", "prev", "ws_seq", "sig", "host_sig"}


def signing_bytes(event):
    return b"orch/v2/sig/ws-event|" + cj(
        {
            "contract": 1,
            "suite": 2,
            "workspace_id": V["workspace_id"],
            "log": "workspace",
            "event": {k: v for k, v in event.items() if k not in EXCLUDED},
        }
    )


def test_staged_grant_and_tampered_variants():
    case = V["addon_event"]
    event = case["event"]
    pub = unb64u(case["cert"]["o"]["dk_sig_pub"])
    msg = signing_bytes(event)
    assert msg.hex() == case["person_signing_hex"]
    assert SUITE.verify(pub, unb64u(event["sig"]), msg)
    cases = [c for c in V["person_refused"] if c["name"].startswith("addon_")]
    assert len(cases) == 3
    for bad in cases:
        assert not SUITE.verify(pub, unb64u(bad["event"]["sig"]), signing_bytes(bad["event"]))
