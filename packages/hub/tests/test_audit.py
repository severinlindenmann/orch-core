# ruff: noqa: F811, RUF059  (the `shub` fixture is imported then named as an argument)
"""The audit chain (protocol.md §10.5): records for grants, memberships and space changes, blinded scope tags,
`audit_get` / `audit_ack` over real links, verified with the contract's own checks."""

from __future__ import annotations

import json
import threading

import pytest
from spaces_helpers import ADMIN, HELD, WS_C, shub  # noqa: F401

from orch_hub.access import Access, AccessError
from orch_hub.audit import OPERATOR, AuditLog, AuditStore
from orch_hub.contract import audit as AU
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError

NOW = G.NOW


def verify_all(records, head=None, certs=()):
    """What a workspace does: every record under the held card, chained."""
    for r in records:
        o, ops = AU.verify_record_ex(r["record"], HELD, dev_mode=True, now_ms=NOW, op_certs=list(certs))
        head = AU.apply_record(head, r["record"], o, op_seq=ops, current_op_seq=1)
        if "event" in r:
            AU.verify_record(r["record"], HELD, dev_mode=True, now_ms=NOW, event=r["event"])
    return head


def two(shub):
    h = shub()
    return h, h.join(G.WS_A), h.join(G.WS_B)


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


def test_space_changes_are_audited_in_order(shub):
    h, a, b = two(shub)
    h.join(WS_C)
    sid = h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    h.svc.add(sid, WS_C, ADMIN)
    h.svc.remove(sid, G.WS_B, ADMIN)
    h.svc.remove(sid, G.WS_A, None)  # unbind
    h.svc.remove(sid, WS_C, ADMIN)  # the last member: the space ends
    rows = h.audit.store.after(0, 100)
    got = [(ev["change"], ev["epoch"], ev["workspace_id"], ev["actor_user_id"]) for _, _, ev in rows]
    assert got == [
        ("create", 1, None, ADMIN),
        ("add", 2, WS_C, ADMIN),
        ("remove", 3, G.WS_B, ADMIN),
        ("unbind", 4, G.WS_A, None),
        ("delete", 4, WS_C, ADMIN),
    ]
    assert verify_all([{"record": s} for _, s, _ in rows])["seq"] == 5


def test_grants_and_memberships_are_audited(shub):
    h, a, b = two(shub)
    h.access.add_user(G.U_BOB, "bob")
    h.access.add_group("eng")
    h.access.add_member("eng", G.U_BOB, actor=ADMIN)
    h.access.grant(G.WS_A, "eng", "edit")
    h.access.grant(G.WS_A, "eng", "admin", actor=ADMIN)
    h.access.revoke(G.WS_A, "eng")
    h.access.remove_member("eng", G.U_BOB)
    evs = [ev for _, _, ev in h.audit.store.after(0, 100)]
    assert [(e.get("group"), e.get("role", "-"), e.get("added", "-"), e["actor_user_id"]) for e in evs] == [
        ("eng", "-", True, ADMIN),
        ("eng", "edit", "-", OPERATOR),
        ("eng", "admin", "-", ADMIN),
        ("eng", None, "-", OPERATOR),
        ("eng", "-", False, OPERATOR),
    ]


def test_a_failed_audit_rolls_the_change_back(tmp_path):
    acc = Access()
    acc.add_group("eng")
    acc.observer = lambda ev: (_ for _ in ()).throw(RuntimeError("audit down"))
    with pytest.raises(RuntimeError):
        acc.grant(G.WS_A, "eng", "edit")
    assert acc.grants() == []  # nothing changed without a record
    acc.observer = None
    with pytest.raises(AccessError):
        acc.revoke(G.WS_A, "eng")


def test_audit_get_gives_a_workspace_only_its_own_events(shub):
    h, a, b = two(shub)
    h.access.add_group("eng")
    h.access.grant(G.WS_A, "eng", "edit")  # about A
    h.access.grant(G.WS_B, "eng", "read")  # about B
    ra, rb = a.request("audit_get", {"since": 0}), b.request("audit_get", {"since": 0})
    assert len(ra["records"]) == len(rb["records"]) == 2  # everybody sees every record ...
    assert [("event" in r) for r in ra["records"]] == [True, False]  # ... and only its own events
    assert [("event" in r) for r in rb["records"]] == [False, True]
    assert ra["records"][0]["event"]["workspace_id"] == G.WS_A
    assert ra["head"] == rb["head"] and ra["head"]["seq"] == 2
    verify_all(ra["records"])
    verify_all(rb["records"])


def test_other_workspaces_ids_are_not_in_the_records_they_receive(shub):
    h, a, b = two(shub)
    h.access.add_group("eng")
    h.access.grant(G.WS_B, "eng", "read")
    text = json.dumps(a.request("audit_get", {"since": 0}))
    assert G.WS_B not in text  # A sees an opaque tag, not B's id
    (r,) = a.request("audit_get", {"since": 0})["records"]
    assert r["record"]["o"]["scope_tag"] is not None and "event" not in r


def test_hub_wide_records_have_no_tag_and_no_event(shub):
    h, a, b = two(shub)
    h.access.add_user(G.U_BOB, "bob")
    h.access.add_group("eng")
    h.access.add_member("eng", G.U_BOB)
    (r,) = a.request("audit_get", {"since": 0})["records"]
    assert r["record"]["o"]["type"] == "membership" and r["record"]["o"]["scope_tag"] is None and "event" not in r


def test_audit_get_pages_and_returns_the_op_certs_the_page_names(shub):
    h, a, b = two(shub)
    h.access.add_group("eng")
    for i in range(130):
        h.access.grant(G.WS_A, "eng", "read" if i % 2 else "edit")
    p1 = a.request("audit_get", {"since": 0})
    assert len(p1["records"]) == 100 and p1["head"]["seq"] == 130
    p2 = a.request("audit_get", {"since": 100})
    assert [r["record"]["o"]["seq"] for r in p2["records"]] == list(range(101, 131))
    assert [c["o"]["cert_id"] for c in p1["op_certs"]] == [G.OP_CERT_ID]
    head = verify_all(p1["records"], certs=p1["op_certs"])
    assert verify_all(p2["records"], head)["seq"] == 130


def test_a_removed_record_is_a_gap_for_the_workspace(shub):
    h, a, b = two(shub)
    h.access.add_group("eng")
    for _ in range(3):
        h.access.grant(G.WS_A, "eng", "edit")
    recs = a.request("audit_get", {"since": 0})["records"]
    with pytest.raises(Exception) as e:
        verify_all([recs[0], recs[2]])
    assert e.value.code == "audit_gap"


def test_ack_is_recorded_not_enforced(shub):
    h, a, b = two(shub)
    h.access.add_group("eng")
    for _ in range(3):
        h.access.grant(G.WS_A, "eng", "edit")
    recs = a.request("audit_get", {"since": 0})["records"]
    hashes = [AU.chain_hash(r["record"]) for r in recs]
    assert a.request("audit_ack", {"seq": 2, "head": hashes[1]}) == {"acked": 2}
    assert a.request("audit_ack", {"seq": 3, "head": hashes[2]}) == {"acked": 3}
    assert a.request("audit_ack", {"seq": 1, "head": hashes[0]}) == {"acked": 3}  # acks only rise
    assert h.audit.store.acks() == {G.WS_A: 3}
    refused(a, "audit_ack", {"seq": 2, "head": hashes[0]}, "audit_fork")  # a head the hub did not sign there
    refused(a, "audit_ack", {"seq": 4, "head": hashes[2]}, "audit_fork")  # beyond the chain
    assert h.audit.store.acked(G.WS_B) == 0
    # not enforced: a workspace that never acks changes nothing for the others or for the log
    h.access.grant(G.WS_A, "eng", "read")
    assert h.audit.store.length() == 4


def test_audit_ops_need_a_bound_workspace(shub):
    h, a, b = two(shub)
    b.request("binding_put", {"object": G.unbound(ws=G.WS_B, wsk="wsk_b", seq=1)})
    refused(b, "audit_get", {"since": 0}, "not_bound")


def test_the_chain_continues_after_a_restart(tmp_path):
    path = str(tmp_path / "audit.db")

    def log(store):
        return AuditLog(store, KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, lambda: G.OP_CERT_ID, now_ms=lambda: NOW)

    s1 = AuditStore(path)
    l1 = log(s1)
    r1 = l1.append("membership", {"group": "g", "user_id": G.U_BOB, "added": True, "actor_user_id": OPERATOR})
    s1.close()
    s2 = AuditStore(path)
    r2 = log(s2).append("membership", {"group": "g", "user_id": G.U_BOB, "added": False, "actor_user_id": OPERATOR})
    assert r2["o"]["seq"] == 2 and r2["o"]["prev"] == AU.chain_hash(r1)
    s2.close()


def test_concurrent_appends_stay_contiguous_and_linked():
    store = AuditStore()
    log = AuditLog(store, KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, lambda: G.OP_CERT_ID, now_ms=lambda: NOW)
    ev = {"group": "g", "user_id": G.U_BOB, "added": True, "actor_user_id": OPERATOR}

    def work():
        for _ in range(20):
            log.append("membership", ev)

    ts = [threading.Thread(target=work) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rows = store.after(0, 1000)
    assert [s for s, _, _ in rows] == list(range(1, 101))
    verify_all([{"record": sg} for _, sg, _ in rows])


def test_timestamps_never_go_back_even_if_the_clock_does():
    clock = iter([NOW, NOW - 5000, NOW + 10])
    store = AuditStore()
    log = AuditLog(store, KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, lambda: G.OP_CERT_ID, now_ms=lambda: next(clock))
    ev = {"group": "g", "user_id": G.U_BOB, "added": True, "actor_user_id": OPERATOR}
    ts = [log.append("membership", ev)["o"]["ts_ms"] for _ in range(3)]
    assert ts == [NOW, NOW, NOW + 10]


def test_events_are_checked_against_the_contract_before_signing():
    log = AuditLog(AuditStore(), KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, lambda: G.OP_CERT_ID, now_ms=lambda: NOW)
    from orch_hub.contract.hub import Refused

    for typ, ev in (("grant", {"group": "g"}), ("login", {}), ("membership", {"group": "g", "x": 1})):
        with pytest.raises(Refused):
            log.append(typ, ev)
    assert log.store.length() == 0
    r = log.custodial(
        workspace_id=G.WS_A,
        log=G.TICKET,
        verb="gate.approved",
        user_id=G.U_ALICE,
        channel_id=G.CH_ALICE,
        digest="A" * 43,
    )
    assert r["o"]["type"] == "custodial" and r["o"]["scope_tag"]
