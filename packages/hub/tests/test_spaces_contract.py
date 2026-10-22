"""Shared-space contract (protocol.md §10.4) beyond the replayed vector cases: the byte-level `spaces` section,
size caps, paging and randomised sealing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import spaces as SP
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())
SV = V["spaces"]


def test_item_vector_bytes_replay_from_json():
    it = SV["item"]
    sk = bytes.fromhex(SV["sk"]["1"])
    salt = bytes.fromhex(it["salt_hex"])
    assert SP._item_key(sk, SV["space_id"], it["epoch"], salt).hex() == it["item_key_hex"]
    assert SP._item_aad(SV["space_id"], it["epoch"], it["item_id"]).hex() == it["aad_hex"]
    assert SP.seal_item(sk, SV["space_id"], 1, it["item_id"], it["signed_item"], salt=salt) == it["sealed"]
    assert SP.open_item(sk, SV["space_id"], 1, it["item_id"], it["sealed"]) == it["signed_item"]
    assert SP.sk_commit(SV["space_id"], 1, sk) == SV["sk_commit_e1"]


def test_wrap_vector_opens_with_the_members_wxk_only():
    w = SV["wrap_b_e1"]
    priv_b = S.key_from_seed(bytes.fromhex(V["keys"]["wxk_b"]["seed"]))
    priv_a = S.key_from_seed(bytes.fromhex(V["keys"]["wxk_a"]["seed"]))
    kw = {"hub_id": V["ids"]["hub"], "space_id": SV["space_id"], "epoch": 1, "workspace_id": w["workspace_id"]}
    assert SP.open_sk(w["sealed"], wxk_priv=priv_b, wxk_version=1, **kw).hex() == SV["sk"]["1"]
    for bad in ({"wxk_priv": priv_a, "wxk_version": 1}, {"wxk_priv": priv_b, "wxk_version": 2}):
        with pytest.raises(H.Refused) as e:
            SP.open_sk(w["sealed"], **kw, **bad)
        assert e.value.code == "bad_wrap"
    assert (
        SP._seal_aad("space-sk", bytes.fromhex(SV["space_id"]), 1, SP._wrap_extra(kw["hub_id"], 1)).hex()
        == w["aad_hex"]
    )


def test_real_sealing_is_randomised_and_opens():
    sk = bytes.fromhex(SV["sk"]["1"])
    obj = SV["item"]["signed_item"]
    a = SP.seal_item(sk, SV["space_id"], 1, SV["item"]["item_id"], obj, salt=SP.new_salt())
    b = SP.seal_item(sk, SV["space_id"], 1, SV["item"]["item_id"], obj, salt=SP.new_salt())
    assert a != b
    assert SP.open_item(sk, SV["space_id"], 1, SV["item"]["item_id"], b) == obj


def test_item_over_the_cap_is_too_large():
    sk = bytes.fromhex(SV["sk"]["1"])
    with pytest.raises(H.Refused) as e:
        SP.open_item(sk, SV["space_id"], 1, SV["item"]["item_id"], b64u(bytes(SP.MAX_SEALED + 1)))
    assert e.value.code == "too_large"
    st = SP.new_state(G.space_rec(1)["o"])
    body = {"space_id": G.SPACE, "epoch": 1, "item_id": G.fake16("x"), "sealed": "A" * (SP.MAX_SEALED * 2)}
    with pytest.raises(H.Refused) as e:
        SP.space_put(st, G.WS_A, body, sks={}, wsk_pubs={}, dev_mode=True)
    assert e.value.code == "too_large"


def test_space_get_pages_at_100_entries():
    st = SP.new_state(G.space_rec(1)["o"])
    st["items"] = {
        G.fake16(f"i{n}"): {"seq": n, "epoch": 1, "ws": G.WS_A, "digest": "", "revoked": False} for n in range(1, 151)
    }
    st["next_seq"] = 151
    r = SP.space_get(st, G.WS_B, {"space_id": G.SPACE, "since": 0})
    assert len(r["entries"]) == 100 and r["next"] == 100
    r2 = SP.space_get(st, G.WS_B, {"space_id": G.SPACE, "since": r["next"]})
    assert len(r2["entries"]) == 50 and r2["next"] == 150


def test_link_ops_have_exact_bodies():
    for op, ok, bad in (
        ("space_put", {"space_id": G.SPACE, "epoch": 1, "item_id": G.fake16("a"), "sealed": "AA"}, {"epoch": 0}),
        ("space_get", {"space_id": G.SPACE, "since": 0}, {"since": -1}),
        ("space_revoke", {"space_id": G.SPACE, "item_id": G.fake16("a")}, {"item_id": "zz"}),
        ("space_list", {}, {"x": 1}),
    ):
        H.verify_link(
            G.link(op, ok),
            direction="w2h",
            hub_id_=G.HUB_ID,
            workspace_id=G.WS_A,
            mode="selective",
            pub=G.K.pub["wsk_a"],
            now_ms=G.NOW,
        )
        with pytest.raises(H.Refused) as e:
            H.verify_link(
                G.link(op, {**ok, **bad}),
                direction="w2h",
                hub_id_=G.HUB_ID,
                workspace_id=G.WS_A,
                mode="full",
                pub=G.K.pub["wsk_a"],
                now_ms=G.NOW,
            )
        assert e.value.code == "malformed"
    assert unb64u(b64u(b"x")) == b"x"
