"""F1 payload bytes survive the existing authenticated relay mailbox path.

The relay is opaque transport, not an F1 event verifier. Endpoint verification is covered by core and hub.
"""

import json
from pathlib import Path

import pytest
from test_hub_phone import hub  # noqa: F401 - existing authenticated HTTP harness

from orch_relay.proto import cj, unb64u

V = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/core/f1/signatures.json").read_text()
)
CASES = [V[k] for k in ("ticket_event", "ws_event", "addon_event")]
CASES += [c for c in V["person_refused"] if c["name"].startswith("addon_")]


@pytest.mark.parametrize("case", CASES)
def test_f1_payload_is_transported_without_field_filtering(hub, case):  # noqa: F811
    raw = cj(case["event"])
    assert hub.mb_put(c=raw).status_code == 200
    got = hub.mb_get()
    assert got.status_code == 200
    assert unb64u(got.json()["c"]) == raw
    assert json.loads(unb64u(got.json()["c"])) == case["event"]
