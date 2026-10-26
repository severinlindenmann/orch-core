"""D15 log_sync receive (protocol.md §10.7): vector replay through the real service and store, then the service tests:
in-order pages, gap, fork, reset, bad host_sig, epochs, durability, private mode, sealed-at-rest, selective refusal
over a real TLS link."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from custodial_helpers import LOG, WK, WS, core_seal
from view_helpers import OWNER, TK2, WSK_PUB, Host, services

from orch_hub.contract import phase4 as P4
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.contract.hub import Refused
from orch_hub.link.wkseal import WkSeals
from orch_hub.view import ViewStore
from orch_hub.view.service import Keyring

V = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_hub.json").read_text())
HXK_PRIV, HXK_PUB = G.K.priv["hxk"], G.K.pub["hxk"]


def make_seals(tmp_path, ws: str, epochs: dict[int, bytes]) -> WkSeals:
    d = tmp_path / "seal"
    d.mkdir(mode=0o700, exist_ok=True)
    w = WkSeals(
        str(d / "wk-seal.db"),
        hub_id=G.HUB_ID,
        hxk_pub=HXK_PUB,
        exchange=lambda p: HXK_PRIV.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p)
        ),
    )
    for e, wk in sorted(epochs.items()):
        w.accept(ws, e, core_seal(wk, G.HUB_ID, ws, e, HXK_PUB))
    return w


@pytest.fixture
def seals(tmp_path):
    s = make_seals(tmp_path, WS, {1: WK})
    yield s
    s.close()


@pytest.fixture
def svc(tmp_path, seals):
    s = services(tmp_path, seals)
    yield s
    s.close()


def code(fn, *a, **kw):
    with pytest.raises(Refused) as r:
        fn(*a, **kw)
    return r.value.code


def accept(svc, body, ws=WS):
    return svc.service.accept(ws, WSK_PUB, body)


# --- the contract vectors, through the real service + store -------------------------------------------------------------

LOG_SYNC_CASES = [c for c in V["cases"] if c["check"] == "log_sync"]


@pytest.mark.parametrize("case", LOG_SYNC_CASES, ids=[c["name"] for c in LOG_SYNC_CASES])
def test_vector_replay(tmp_path, case):
    a = case["args"]
    ws = a["workspace_id"]
    seals = make_seals(tmp_path, ws, {int(e): bytes.fromhex(k) for e, k in a["wk_epochs"].items()})
    s = services(tmp_path, seals)

    def step(body):
        if set(body) == {"wk_seal"}:  # a rotation between pages: the host seals the new epoch to the hub
            for e, k in body["wk_seal"].items():
                seals.accept(ws, int(e), core_seal(bytes.fromhex(k), G.HUB_ID, ws, int(e), HXK_PUB))
        else:
            s.service.accept(ws, unb64u(a["wsk_pub"], 65), body)

    try:
        want = case["expect"]
        if "error" in want:
            got = None
            with pytest.raises(Refused) as r:
                for body in a["pages"]:
                    step(body)
            got = {"error": r.value.code}
        else:
            for body in a["pages"]:
                step(body)
            got = {
                "ok": {
                    lg: {"seq": len(s.store.heads(ws, lg)), "head": s.store.heads(ws, lg)[-1]}
                    for lg in sorted(s.store.logs(ws))
                }
            }
            if "proj" in want["ok"]:  # D15b: the projection the service keeps per ticket log
                keys = Keyring(seals, ws, {int(e): bytes.fromhex(k) for e, k in a["wk_epochs"].items()})
                got["ok"]["proj"] = {
                    lg: s.service.proj(ws, lg, keys) for lg in sorted(s.store.logs(ws)) if s.store.proj_key(ws, lg)
                }
        assert got == want
    finally:
        s.close()
        seals.close()


# --- service tests ------------------------------------------------------------------------------------------------------


def test_in_order_pages_reply_with_seq_and_head(svc):
    h = Host()
    h.genesis()
    h.append("workspace", "member.added", roster_v=1)
    r1 = accept(svc, h.page("workspace", 1, 1))
    assert r1 == {"seq": 1, "head": P4.event_head(h.logs["workspace"][0])}
    r2 = accept(svc, h.page("workspace", 2))
    assert r2 == {"seq": 2, "head": P4.event_head(h.logs["workspace"][1])}
    h.ticket()
    assert accept(svc, h.page(LOG))["seq"] == 1
    assert svc.store.logs(WS) == [LOG, "workspace"]


def test_resend_is_idempotent_and_stores_nothing_new(svc):
    h = Host()
    h.genesis()
    p = h.page("workspace")
    accept(svc, p)
    accept(svc, p)
    assert len(svc.store.pages(WS, "workspace")) == 1
    assert len(svc.store.heads(WS, "workspace")) == 1


def test_gap_is_refused_and_nothing_stored(svc):
    h = Host()
    h.genesis()
    h.append("workspace", "member.added", roster_v=1)
    assert code(accept, svc, h.page("workspace", 2)) == "view_gap"
    assert svc.store.heads(WS, "workspace") == []


def test_ticket_line_ahead_of_workspace_view_is_a_gap(svc):
    h = Host()
    h.genesis()
    h.ticket()
    assert code(accept, svc, h.page(LOG)) == "view_gap"


def test_fork_is_refused_and_the_held_view_is_unchanged(svc):
    h = Host()
    h.genesis()
    accept(svc, h.page("workspace"))
    held = svc.store.heads(WS, "workspace")
    f = Host()
    f.append("workspace", "workspace.created", prefix="EVIL", workspace_id=WS, owner={"person": OWNER})
    assert code(accept, svc, f.page("workspace")) == "view_fork"
    assert svc.store.heads(WS, "workspace") == held


def test_reset_replaces_the_view_and_drops_every_ticket_view(svc):
    h = Host()
    h.genesis()
    h.ticket()
    h.ticket(TK2, "DEMO-0002", "Second")
    h.push_all(svc)
    assert set(svc.store.logs(WS)) == {"workspace", LOG, TK2}
    f = Host()
    f.append("workspace", "workspace.created", prefix="NEW", workspace_id=WS, owner={"person": OWNER})
    r = accept(svc, f.page("workspace", reset=True))
    assert r["seq"] == 1
    assert svc.store.logs(WS) == ["workspace"]  # L2P4-9
    assert svc.store.pages(WS, LOG) == [] and len(svc.store.pages(WS, "workspace")) == 1
    # the host resends the ticket logs; a ticket line whose ws_seq is within the new log is accepted
    t = Host()
    t.logs["workspace"] = f.logs["workspace"]
    t.ticket()
    assert accept(svc, t.page(LOG))["seq"] == 1


def test_ticket_reset_leaves_other_logs(svc):
    h = Host()
    h.genesis()
    h.ticket()
    h.ticket(TK2, "DEMO-0002", "Second")
    h.push_all(svc)
    t = Host()
    t.logs["workspace"] = h.logs["workspace"]
    t.ticket(LOG, "DEMO-0001", "Rewritten")
    accept(svc, t.page(LOG, reset=True))
    assert set(svc.store.logs(WS)) == {"workspace", LOG, TK2}


def test_line_signed_by_another_key_is_bad_signature(svc):
    h = Host(key=G.K.priv["wsk_b"])
    h.genesis()
    assert code(accept, svc, h.page("workspace")) == "bad_signature"
    assert svc.store.logs(WS) == []


def test_no_wk_seal_yet_is_bad_epoch(tmp_path):
    seals = make_seals(tmp_path, WS, {})
    s = services(tmp_path, seals)
    h = Host()
    h.genesis()
    assert code(accept, s, h.page("workspace")) == "bad_epoch"
    s.close()
    seals.close()


def test_rotation_old_pages_still_open_for_a_build_read(tmp_path):
    seals = make_seals(tmp_path, WS, {1: WK})
    s = services(tmp_path, seals)
    h = Host()
    h.genesis()
    accept(s, h.page("workspace"))
    wk2 = bytes(range(32, 64))
    seals.accept(WS, 2, core_seal(wk2, G.HUB_ID, WS, 2, HXK_PUB))
    assert code(accept, s, h.page("workspace")) == "bad_epoch"  # still sealed under epoch 1
    h.append("workspace", "member.added", roster_v=1)
    h.epoch, h.wk = 2, wk2
    accept(s, h.page("workspace", 2))
    from orch_hub.view.service import Keyring

    k = Keyring(seals, WS)
    try:
        lines = s.service.lines(WS, "workspace", k)  # one page under epoch 1, one under epoch 2
    finally:
        k.close()
    assert [e["seq"] for e in lines] == [1, 2]
    s.close()
    seals.close()


def test_store_survives_reopen_and_is_private(tmp_path, seals):
    s = services(tmp_path, seals)
    h = Host()
    h.genesis()
    accept(s, h.page("workspace"))
    path = s.store._db.execute("PRAGMA database_list").fetchone()[2]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    s.close()
    s2 = services(tmp_path, seals)
    assert s2.store.heads(WS, "workspace") == [P4.event_head(h.logs["workspace"][0])]
    s2.close()


def test_pages_rest_sealed_no_plaintext_in_the_db(tmp_path, seals):
    s = services(tmp_path, seals)
    h = Host()
    h.genesis()
    h.ticket(title="Very secret ticket title")
    h.push_all(s)
    path = s.store._db.execute("PRAGMA database_list").fetchone()[2]
    s.close()
    raw = Path(path).read_bytes()
    assert b"Very secret" not in raw and b"DEMO" not in raw and WK not in raw


def test_drop_forgets_the_workspace(svc):
    h = Host()
    h.genesis()
    h.push_all(svc)
    svc.store.drop(WS)
    assert svc.store.logs(WS) == []


def test_private_file_modes_are_enforced(tmp_path):
    p = tmp_path / "view.db"
    p.write_bytes(b"")
    os.chmod(p, 0o644)
    with pytest.raises(Exception, match="bad_private_file"):
        ViewStore(str(p))


def test_oversize_workspace_is_refused(tmp_path, seals, monkeypatch):
    from orch_hub.view import store as ST

    monkeypatch.setattr(ST, "MAX_WS_BYTES", 10)
    s = services(tmp_path, seals)
    h = Host()
    h.genesis()
    assert code(accept, s, h.page("workspace")) == "too_large"
    s.close()


# --- over a real TLS link: mode gate and the whole path -------------------------------------------------------------------


def test_over_link_full_mode_accepts_selective_refuses(tmp_path, make_hub):
    seals = make_seals(tmp_path, G.WS_A, {})
    s = services(tmp_path, seals)
    hub = make_hub(wk_seals=seals, views=s.service)
    c = hub.open(first=True)
    c.request("join", hub.join_body(mode="full"))
    c.mode = "full"
    wk = bytes(range(64, 96))
    assert c.request("wk_seal", {"epoch": 1, "wrap": b64u(core_seal(wk, G.HUB_ID, G.WS_A, 1, HXK_PUB))}) == {}
    h = Host(ws=G.WS_A, wk=wk, key=G.K.priv["wsk_a"])
    h.genesis()
    body = h.page("workspace")
    r = c.request("log_sync", body)
    assert r == {"seq": 1, "head": P4.event_head(h.logs["workspace"][0])}
    assert s.store.heads(G.WS_A, "workspace") == [r["head"]]
    # selective workspace: wrong_mode, nothing stored (a second workspace B, joined selective)
    hb = hub.open(ws=G.WS_B, wsk="wsk_b", first=True)
    hb.request("join", hub.join_body(invite="invB", ws=G.WS_B, wsk="wsk_b", mode="selective"))
    hb.mode = "selective"
    hh = Host(ws=G.WS_B, wk=wk, key=G.K.priv["wsk_b"])
    hh.genesis()
    with pytest.raises(Exception) as ex:
        hb.request("log_sync", hh.page("workspace"))
    assert "wrong_mode" in str(ex.value)
    assert s.store.logs(G.WS_B) == []
    s.close()
    seals.close()


# --- D15b task 1: the projection is kept with the view, in ONE transaction -----------------------------------------


def _ticket_host():
    h = Host()
    h.genesis()
    h.ticket()
    return h


def _keys(svc, seals):
    return Keyring(seals, WS, {1: WK})


def test_proj_is_kept_and_read_back(svc, seals):
    h = _ticket_host()
    accept(svc, h.page("workspace"))
    p = h.proj(gates={"requirements": h.gate()})
    accept(svc, h.page(LOG, proj=p))
    assert svc.service.proj(WS, LOG, _keys(svc, seals)) == p
    assert svc.service.proj(WS, "workspace", _keys(svc, seals)) is None


def test_proj_is_sealed_at_rest(svc):
    h = _ticket_host()
    accept(svc, h.page("workspace"))
    accept(svc, h.page(LOG, proj=h.proj(status="testing")))
    raw = svc.store.proj_page(WS, LOG)
    assert "testing" not in json.dumps(raw)


def test_bad_proj_leaves_the_view_unchanged(svc, seals):
    h = _ticket_host()
    accept(svc, h.page("workspace"))
    h.ask()
    good = h.proj()
    accept(svc, h.page(LOG, 1, 1, proj={**good, "seq": 1, "head": P4.event_head(h.logs[LOG][0])}))
    before = (svc.store.heads(WS, LOG), svc.store.pages(WS, LOG), svc.store.proj_key(WS, LOG))
    bad = {**h.proj(), "status": "nonsense"}  # the lines (seq 2) are fine, the projection is not
    assert code(accept, svc, h.page(LOG, 2, proj=bad)) == "malformed"
    assert (svc.store.heads(WS, LOG), svc.store.pages(WS, LOG), svc.store.proj_key(WS, LOG)) == before
    assert len(svc.store.heads(WS, LOG)) == 1
    stale_ws = {**h.proj(), "ws_head": "sha256:" + "00" * 32}
    assert code(accept, svc, h.page(LOG, 2, proj=stale_ws)) == "view_fork"
    assert len(svc.store.heads(WS, LOG)) == 1
    # the good page still goes in afterwards
    accept(svc, h.page(LOG, 2, proj=h.proj()))
    assert svc.service.proj(WS, LOG, _keys(svc, seals))["seq"] == 2


def test_overlap_page_refreshes_proj_without_new_lines(svc, seals):
    h = _ticket_host()
    accept(svc, h.page("workspace"))
    accept(svc, h.page(LOG, proj=h.proj(status="backlog")))
    npages = len(svc.store.pages(WS, LOG))
    accept(svc, h.page(LOG, 1, 1, proj=h.proj(status="in_progress")))
    assert len(svc.store.pages(WS, LOG)) == npages
    assert svc.service.proj(WS, LOG, _keys(svc, seals))["status"] == "in_progress"


def test_ticket_reset_drops_its_proj_and_workspace_reset_drops_all(svc, seals):
    h = _ticket_host()
    accept(svc, h.page("workspace"))
    accept(svc, h.page(LOG, proj=h.proj()))
    accept(svc, h.page(LOG, reset=True))
    assert svc.service.proj(WS, LOG, _keys(svc, seals)) is None
    accept(svc, h.page(LOG, proj=h.proj()))
    accept(svc, h.page("workspace", reset=True))
    assert svc.store.proj_key(WS, LOG) is None


def test_projection_only_pages_count_toward_the_cap(svc, monkeypatch):
    from orch_hub.view import store as ST

    h = _ticket_host()
    accept(svc, h.page("workspace"))
    accept(svc, h.page(LOG, proj=h.proj()))
    one = len(json.dumps(svc.store.proj_page(WS, LOG), sort_keys=True, separators=(",", ":")))
    used = sum(
        len(json.dumps(b, sort_keys=True, separators=(",", ":")))
        for lg in svc.store.logs(WS)
        for b in svc.store.pages(WS, lg)
    )
    # room for the pages held plus one projection body (the one a refresh replaces) and a little
    monkeypatch.setattr(ST, "MAX_WS_BYTES", used + one + one // 2)
    accept(svc, h.page(LOG, 1, 1, proj=h.proj(status="testing")))  # replaces its own row: fits
    # a second ticket's projection-only page would add a row that no longer fits
    h.ticket(uid="01J9ZP0000000000000000TKT2", key="DEMO-0002")
    accept(svc, h.page("workspace"))  # no-op resend
    before = (svc.store.proj_key(WS, "01J9ZP0000000000000000TKT2"), svc.store.heads(WS, "01J9ZP0000000000000000TKT2"))
    pg = h.page("01J9ZP0000000000000000TKT2", proj=h.proj("01J9ZP0000000000000000TKT2"))
    assert code(accept, svc, pg) == "too_large"
    assert before == (None, [])
    assert svc.store.proj_key(WS, LOG) is not None
