"""Refusals on the link: transport pin, link auth, receiver steps 1-6 of §10.2, framing limits, join checks."""

from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest

from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.link import LinkError, framing, tls

NOW = G.NOW
ALICE_PK = b64u(G.K.pub["pk_alice"])


def raw(hub, pin=None):
    return tls.connect_pinned("127.0.0.1", hub.port, pin or hub.identity.spki_sha256, timeout=5)


def challenge(conn):
    f = framing.read_frame(conn, first_timeout=5, rest_timeout=5)
    return unb64u(f["challenge"], 32)


def auth_frame(wsk, ws, ch, exporter, **extra):
    s = KS.WskSigner(G.K.priv[wsk])
    return {"t": "auth", "workspace_id": ws, "sig": b64u(s.link_auth(G.HUB_ID, ws, ch, exporter)), **extra}


def reply_of(conn):
    return framing.read_frame(conn, first_timeout=5, rest_timeout=5)


def joined(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    return c


# --- transport ---------------------------------------------------------------------------------------------------


def test_wrong_spki_pin_is_refused_by_the_client(hub):
    with pytest.raises(tls.PinMismatch):
        raw(hub, pin=b64u(b"\x00" * 32))


def test_tls12_is_refused(hub):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", hub.port), timeout=5) as s, pytest.raises(ssl.SSLError):
        ctx.wrap_socket(s)


# --- link auth ---------------------------------------------------------------------------------------------------


def test_auth_signature_by_another_key(hub):
    joined(hub)  # pins wsk_a
    c = raw(hub)
    ch = challenge(c)
    framing.write_frame(c, auth_frame("wsk_b", G.WS_A, ch, c.exporter()), timeout=5)
    assert reply_of(c) == {"t": "error", "error": "auth_failed"}


def test_auth_replayed_from_another_tls_session(hub):
    joined(hub)
    c1, c2 = raw(hub), raw(hub)
    ch = challenge(c2)
    # a signature made over the exporter of ANOTHER session (a replay / relay of someone's auth)
    framing.write_frame(c2, auth_frame("wsk_a", G.WS_A, ch, c1.exporter()), timeout=5)
    assert reply_of(c2) == {"t": "error", "error": "auth_failed"}


def test_auth_over_another_challenge(hub):
    joined(hub)
    c = raw(hub)
    challenge(c)
    framing.write_frame(c, auth_frame("wsk_a", G.WS_A, b"\x01" * 32, c.exporter()), timeout=5)
    assert reply_of(c) == {"t": "error", "error": "auth_failed"}


def test_unpinned_workspace_needs_wsk_pub(hub):
    c = raw(hub)
    framing.write_frame(c, auth_frame("wsk_a", G.WS_A, challenge(c), c.exporter()), timeout=5)
    assert reply_of(c) == {"t": "error", "error": "auth_failed"}


def test_pinned_workspace_other_claimed_key(hub):
    joined(hub)
    c = raw(hub)
    ch = challenge(c)
    framing.write_frame(c, auth_frame("wsk_b", G.WS_A, ch, c.exporter(), wsk_pub=G.K.b("wsk_b")), timeout=5)
    assert reply_of(c) == {"t": "error", "error": "auth_failed"}


@pytest.mark.parametrize("bad", [{"t": "nope"}, {"t": "auth"}, {"t": "auth", "workspace_id": "zz", "sig": "AA"}])
def test_malformed_auth_frame(hub, bad):
    c = raw(hub)
    challenge(c)
    framing.write_frame(c, bad, timeout=5)
    assert reply_of(c)["error"] == "malformed"


def test_unjoined_link_may_only_join(hub):
    c = hub.open(first=True)
    with pytest.raises(LinkError) as e:
        c.request("snapshot_get", {})
    assert e.value.code == "unknown_workspace"
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):  # D12b G8: the first non-join op also ends the link
        c.request("binding_put", {"object": G.binding()})


# --- receiver verification, §10.2 steps 1-6 (each closes the link) --------------------------------------------------


def expect_link_refusal(c, msg, code):
    c.send_raw(msg)
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == code
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):  # the link is closed
        framing.read_frame(c._c, first_timeout=2, rest_timeout=2)


def test_step1_bad_signature(hub):
    c = joined(hub)
    m = c.build("snapshot_get", {})
    m["sig"] = b64u(bytes(64))
    expect_link_refusal(c, m, "bad_signature")


def test_step1_signed_by_another_workspace_key(hub):
    c = joined(hub)
    c._wsk = KS.WskSigner(G.K.priv["wsk_b"])
    expect_link_refusal(c, c.build("snapshot_get", {}), "bad_signature")


def test_step1_malformed_extra_field(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("snapshot_get", {}, extra=1), "malformed")


def test_step1_wrong_direction_label(hub):
    c = joined(hub)
    m = G.link("snapshot_get", {}, direction="h2w", key="wsk_a", label="sig_hub_route")
    expect_link_refusal(c, m, "bad_signature")


def test_step2_unknown_op(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("sign_anything", {}), "unknown_op")


def test_step2_request_with_re(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("snapshot_get", {}, re=1), "unknown_op")


def test_step2_hub_direction_op_sent_by_host(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("event_deliver", {"log": "x", "event": {}}), "unknown_op")


def test_step3_wk_seal_in_selective_mode(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("wk_seal", {"epoch": 1, "wrap": "AA"}), "wrong_mode")


def test_step4_other_hub(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("snapshot_get", {}, hub_id=G.ROGUE_HUB_ID), "other_hub")


def test_step4_other_workspace(hub):
    c = joined(hub)
    expect_link_refusal(c, c.build("snapshot_get", {}, workspace_id=G.WS_B), "other_workspace")


@pytest.mark.parametrize("delta", [-121_000, 121_000])
def test_step5_stale_timestamp(hub, delta):
    c = joined(hub)
    expect_link_refusal(c, c.build("snapshot_get", {}, ts_ms=NOW + delta), "stale_timestamp")


@pytest.mark.parametrize(
    ("op", "body"),
    [("snapshot_get", {"x": 1}), ("card_put", {"card": {}}), ("binding_put", {"object": "no"}), ("join", {})],
)
def test_step6_malformed_body(hub, op, body):
    c = joined(hub)
    expect_link_refusal(c, c.build(op, body), "malformed")


def test_later_phase_and_unserved_ops_are_unknown_op_replies(hub):
    c = joined(hub)
    for op, body in (
        ("route_out", {"envelope": "AA"}),
        ("audit_ack", {"seq": 1, "head": b64u(bytes(32))}),
        ("share_put", {"item_id": G.WS_A, "sealed": "AA"}),
    ):
        with pytest.raises(LinkError) as e:
            c.request(op, body)
        assert e.value.code == "unknown_op"
    assert "snapshot" in c.request("snapshot_get", {})  # the link stays usable


# --- framing and timeouts ----------------------------------------------------------------------------------------


def test_oversize_frame_closes_the_link_before_reading_it(make_hub):
    h = make_hub(max_frame=4096)
    c = joined(h)
    c._c.send_all((4097).to_bytes(4, "big"), time.monotonic() + 5)  # header only: nothing is allocated for the body
    assert framing.read_frame(c._c, first_timeout=3, rest_timeout=3) == {"t": "error", "error": "malformed"}
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):
        framing.read_frame(c._c, first_timeout=3, rest_timeout=3)


@pytest.mark.parametrize("payload", [b"[1]", b'{"a":1.5}', b'{"a":1,"a":2}', b"\xff\xfe", b"{"])
def test_non_strict_frame_closes_the_link(hub, payload):
    c = joined(hub)
    c._c.send_all(len(payload).to_bytes(4, "big") + payload, time.monotonic() + 5)
    assert framing.read_frame(c._c, first_timeout=3, rest_timeout=3) == {"t": "error", "error": "malformed"}
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):
        framing.read_frame(c._c, first_timeout=3, rest_timeout=3)


def test_silent_peer_times_out(make_hub):
    h = make_hub(read_timeout=0.4)
    c = raw(h)
    challenge(c)  # and then nothing
    t0 = time.monotonic()
    with pytest.raises(tls.LinkClosed):
        framing.read_frame(c, first_timeout=5, rest_timeout=5)
    assert time.monotonic() - t0 < 3


def test_idle_link_times_out(make_hub):
    h = make_hub(idle_timeout=0.4)
    c = joined(h)
    with pytest.raises(tls.LinkClosed):
        framing.read_frame(c._c, first_timeout=5, rest_timeout=5)


def test_half_frame_times_out(make_hub):
    h = make_hub(read_timeout=0.4)
    c = joined(h)
    c._c.send_all((100).to_bytes(4, "big") + b"{", time.monotonic() + 5)
    with pytest.raises(tls.LinkClosed):
        framing.read_frame(c._c, first_timeout=5, rest_timeout=5)


# --- join (§9.5) -------------------------------------------------------------------------------------------------


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


def test_join_other_owner_and_other_wsk(hub):
    joined(hub)
    c = hub.open()  # pinned link
    refused(c, "join", hub.join_body("inv2", pk="pk_mallory"), "other_person")
    refused(c, "join", hub.join_body("inv2", wsk="wsk_b"), "wsk_mismatch")
    assert hub.registry.pin(G.WS_A)["wsk_pub"] == G.K.b("wsk_a")
    assert "inv2" in hub.invites.codes  # not consumed by refusals


def test_squatter_cannot_open_a_link_for_a_pinned_workspace(hub):
    joined(hub)
    with pytest.raises(LinkError) as e:
        hub.open(wsk="wsk_b", first=True)
    assert e.value.code == "auth_failed"


def test_join_card_key_differs_from_link_key(hub):
    c = hub.open(wsk="wsk_b", first=True)  # unpinned: link keyed by wsk_b, card says wsk_a
    refused(c, "join", hub.join_body(), "wsk_mismatch")


def test_join_for_another_workspace_than_the_link(hub):
    c = hub.open(first=True)
    refused(c, "join", hub.join_body("inv1", ws=G.WS_B), "other_workspace")


def test_join_bad_invite_and_bound_invite(hub):
    c = hub.open(first=True)
    refused(c, "join", hub.join_body("nope"), "bad_invite")
    refused(c, "join", hub.join_body("invB"), "other_workspace")  # invite bound to workspace B


def test_join_binding_for_another_hub(hub):
    c = hub.open(first=True)
    refused(c, "join", hub.join_body(root="hk_root_rogue"), "other_hub")


def test_join_binding_signed_by_other_key(hub):
    c = hub.open(first=True)
    body = hub.join_body()
    body["binding"] = G.binding(wsk="wsk_b")
    refused(c, "join", body, "bad_signature")


def test_join_card_forged_delegation(hub):
    c = hub.open(first=True)
    body = hub.join_body()
    body["pk_pub"] = b64u(G.K.pub["pk_mallory"])
    refused(c, "join", body, "other_person")


def test_real_hub_refuses_a_dev_workspace(make_hub):
    h = make_hub(dev_mode=False)
    refused(h.open(first=True), "join", h.join_body(), "dev_refused")


def test_card_put_checks_against_the_pin(hub):
    c = joined(hub)
    refused(c, "card_put", {"card": G.v2_card(pk="pk_mallory"), "pk_pub": b64u(G.K.pub["pk_mallory"])}, "other_person")
    refused(c, "card_put", {"card": G.v2_card(ws=G.WS_B), "pk_pub": ALICE_PK}, "other_workspace")
    refused(c, "card_put", {"card": G.v2_card(wsk="wsk_b"), "pk_pub": ALICE_PK}, "wsk_mismatch")


def test_binding_put_checks(hub):
    c = joined(hub)
    refused(c, "binding_put", {"object": G.binding(ws=G.WS_B)}, "other_workspace")
    refused(c, "binding_put", {"object": G.binding(wsk="wsk_b")}, "bad_signature")
    refused(c, "binding_put", {"object": G.binding(root="hk_root_rogue")}, "other_hub")
    refused(c, "binding_put", {"object": G.binding(seq=1, mode="full")}, "binding_conflict")
    refused(c, "binding_put", {"object": {"o": {"kind": "hub.card"}, "sig": "AA"}}, "malformed")
    # unbound is WSK-signed only: a hub-signed one is refused
    refused(
        c,
        "binding_put",
        {"object": G.unbound() | {"sig": G.signed("hk_op", "sig_hub_unbound", G.unbound()["o"])["sig"]}},
        "bad_signature",
    )


def test_concurrent_joins_for_one_workspace_pin_exactly_one(hub):
    results = {}

    def go(name, pk, wsk):
        try:  # the handshake too: a loser that connects after the winner pinned is refused there (auth_failed)
            c = hub.open(wsk=wsk, first=True)
            c.request("join", hub.join_body(f"inv{1 if name == 'a' else 2}", pk=pk, wsk=wsk))
            results[name] = "ok"
        except LinkError as e:
            results[name] = e.code

    ts = [threading.Thread(target=go, args=a) for a in (("a", "pk_alice", "wsk_a"), ("m", "pk_mallory", "wsk_b"))]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(results) == ["a", "m"]  # both finished with an answer
    winners = [n for n, r in results.items() if r == "ok"]
    assert len(winners) == 1  # exactly one pin, whichever came first
    (loser,) = set(results) - set(winners)
    assert results[loser] in ("other_person", "wsk_mismatch", "auth_failed")
    pin = hub.registry.pin(G.WS_A)
    assert pin is not None and pin["wsk_pub"] == G.K.b("wsk_a" if winners == ["a"] else "wsk_b")


def test_link_opened_before_the_pin_dies_when_the_owner_joins_elsewhere(hub):
    attacker = hub.open(wsk="wsk_b", first=True)  # unjoined, offers its own key for ws A
    owner = hub.open(first=True)
    owner.request("join", hub.join_body())
    attacker.send_raw(attacker.build("snapshot_get", {}))
    with pytest.raises(LinkError) as e:
        attacker.recv_reply()
    assert e.value.code == "wsk_mismatch"
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):
        framing.read_frame(attacker._c, first_timeout=2, rest_timeout=2)
    assert "snapshot" in owner.request("snapshot_get", {})


def test_pre_auth_failures_are_indistinguishable(hub, caplog):
    joined(hub)
    codes = set()
    for ws, key, claim in ((G.WS_B, "wsk_a", False), (G.WS_A, "wsk_b", True), (G.WS_A, "wsk_b", False)):
        c = raw(hub)
        extra = {"wsk_pub": G.K.b(key)} if claim else {}
        framing.write_frame(c, auth_frame(key, ws, challenge(c), c.exporter(), **extra), timeout=5)
        codes.add(reply_of(c)["error"])
    assert codes == {"auth_failed"}


def test_unjoined_link_has_a_short_idle_timeout(make_hub):
    h = make_hub(unjoined_idle_timeout=0.4)
    c = h.open(first=True)
    with pytest.raises(tls.LinkClosed):
        framing.read_frame(c._c, first_timeout=5, rest_timeout=5)


def test_any_op_after_leave_is_not_bound_except_join_and_binding_put(hub):
    c = joined(hub)
    c.request("binding_put", {"object": G.unbound()})
    refused(c, "card_put", {"card": G.v2_card(), "pk_pub": ALICE_PK}, "not_bound")
    refused(c, "snapshot_get", {}, "not_bound")
    refused(c, "binding_put", {"object": G.binding()}, "unbound")  # served, and the store refuses


def test_join_without_card_is_malformed_before_any_lookup(hub):
    c = hub.open(first=True)
    body = hub.join_body()
    del body["card"]
    expect_link_refusal(c, c.build("join", body), "malformed")
