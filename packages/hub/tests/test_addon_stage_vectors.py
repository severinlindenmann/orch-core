"""F1 events through the hub's production authenticated log-sync ingest path."""

import json
from pathlib import Path

import pytest

from orch_hub.contract import phase4 as P4
from orch_hub.contract.canon import unb64u
from orch_hub.contract.hub import Refused

V = json.loads((Path(__file__).resolve().parents[3] / "contracts/core/f1/signatures.json").read_text())
WK = bytes(range(32))


def ingest(case, event):
    # The vector starts after a known prefix; seed its preceding head, as a previous sync would.
    held = ["sha256:" + "0" * 64] * (event["seq"] - 1)
    held[-1] = case["event"]["prev"]
    views = {case["log"]: held}
    if case["log"] != "workspace":
        views["workspace"] = held * event["ws_seq"]
    page = P4.seal_view_page(WK, V["workspace_id"], 1, case["log"], event["seq"], [event], bytes(16))
    decoded = P4.open_view_page(WK, V["workspace_id"], page)
    assert decoded == [event]
    return P4.apply_view_page(
        views,
        page,
        workspace_id=V["workspace_id"],
        wsk_pub=unb64u(V["keys"]["wsk"]["pub_b64u"]),
        wk_epochs={1: WK},
    )


@pytest.mark.parametrize("which", ["ticket_event", "ws_event", "addon_event"])
def test_f1_event_ingest(which):
    case = V[which]
    assert ingest(case, case["event"])[case["log"]][-1] == case["head"]


@pytest.mark.parametrize(
    "bad", [c for c in V["person_refused"] if c["name"].startswith("addon_")], ids=lambda c: c["name"]
)
def test_staged_grant_tampering_is_refused_by_ingest(bad):
    with pytest.raises(Refused, match="bad_signature"):
        ingest(V["addon_event"], bad["event"])
