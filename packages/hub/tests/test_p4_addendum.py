"""C3b addendum to the hub contract (protocol.md §8.5 event side, §8.6, §8.7, §9.1, §10.2b f1, §10.7 sender rules):
the properties the vector cases cannot show on their own."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import phase4 as P4
from orch_hub.contract.canon import b64u
from orch_hub.contract.hub import Refused

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())
NAMES = {c["name"]: c for c in V["cases"]}
WS = "00" * 16


def _body(raw_len: int) -> dict:
    return {"epoch": 1, "log": "workspace", "from_seq": 1, "count": 1, "head": "sha256:" + "11" * 32,
            "reset": False, "sealed": b64u(bytes(raw_len))}  # fmt: skip


def test_page_limit_counts_raw_bytes_not_text():
    # exactly 1 MiB raw passes the size step (and fails only at the AEAD); one byte more is too_large
    with pytest.raises(Refused) as r:
        P4.open_view_page(bytes(32), WS, _body(P4.VIEW_MAX_SEALED))
    assert r.value.code == "bad_wrap"
    with pytest.raises(Refused) as r:
        P4.open_view_page(bytes(32), WS, _body(P4.VIEW_MAX_SEALED + 1))
    assert r.value.code == "too_large"
    assert len(b64u(bytes(P4.VIEW_MAX_SEALED))) <= 1_398_102


def test_largest_valid_line_fits_one_page():
    line = {"v": 2, "seq": 1, "pad": "x" * (524_288 - 40)}
    sealed = P4.seal_view_page(bytes(32), WS, 1, "workspace", 1, [line], bytes(16))
    from orch_hub.contract.canon import unb64u

    assert len(unb64u(sealed["sealed"])) <= P4.VIEW_MAX_SEALED


def test_every_custodial_type_has_an_event_shaped_vector():
    for typ, _lk in H.CUSTODIAL_TYPES:
        c = NAMES[f"custodial_event_{typ.replace('.', '_')}"]
        assert "ok" in c["expect"] and c["args"]["event"]["type"] == typ


def test_base_rev_is_an_object_keyed_by_set():
    ev = NAMES["custodial_event_ticket_updated"]["args"]["event"]
    assert set(ev["base_rev"]) == set(ev["set"])
    assert NAMES["custodial_event_base_rev_single_hash"]["expect"] == {"error": "malformed"}


def test_share_items_without_f1_unchanged():
    # additive: the item the earlier share_item vectors carry has no f1 and keeps its pinned hash
    item = V["phase4"]["share_item"]["item"]
    assert "f1" not in item["o"] and P4.item_hash(item) == V["phase4"]["share_item"]["item_hash"]
    assert NAMES["share_item_needs_you"]["expect"]["ok"]["hash"] == V["phase4"]["share_item"]["item_hash"]
    assert "f1" in V["phase4"]["share_item_f1"]["item"]["o"]


def test_retryable_set_is_exactly_the_core_one():
    assert P4.DELIVER_RETRYABLE == {"unavailable", "unknown_snapshot"}
    assert P4.deliver_next({"error": "unavailable"}, attempt=2, first_sent_ms=0, now_ms=1) == {"resend_in_ms": 2000}
