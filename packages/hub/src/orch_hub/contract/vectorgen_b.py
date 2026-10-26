"""B-stream contract vectors (protocol.md §6.4 pairing link, channel sessions, push key; §10.8.4 a channel's mailbox
read at the relay). Called from :func:`orch_hub.contract.vectorgen.build`; same FAKE keys and fixed clock."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import channelapi as CA
from . import suite as S
from .canon import b64u, cj
from .vectorgen import (
    CH_ALICE,
    CH_BOB,
    DAY,
    DROP,
    HUB_ID,
    NOW,
    OP_CERT_ID,
    OP_CERT_NEXT,
    U_ALICE,
    U_BOB,
    K,
    card,
    channel_cert,
    fake16,
    hdr,
    op_cert,
    resign,
    signed,
)

API_HOST = "hub.dev.orch.example"
CODE = "ab12-cd34-ef56"
LINK = f"orch://hub-pair/{API_HOST}#h1.{HUB_ID}.{U_ALICE}.{CODE}"
SESSION = "FAKE-pairing-session-token-alice-0001"
OTHER_SESSION = "FAKE-pairing-session-token-alice-0002"
CH_NONCE = S.fake_seed("hub/channel_req_nonce")[:16]
MID = fake16("mailbox_msg")


def api_origin(key: str = "hk_op", label: str = "sig_hub_api_origin", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.api_origin"),
        "hub_id": HUB_ID,
        "op_cert_id": OP_CERT_ID,
        "origin": f"https://{API_HOST}",
        "issued_ms": NOW - DAY,
        "dev": True,
        **kw,
    }
    return signed(key, label, o)


def channel_auth(key: str = "channel_alice_phone", label: str = "sig_hub_channel_auth", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.channel_auth"),
        "hub_id": HUB_ID,
        "user_id": U_ALICE,
        "channel_id": CH_ALICE,
        "session_ref": CA.session_ref(SESSION),
        "ts_ms": NOW - 2_000,
        "dev": True,
        **kw,
    }
    return signed(key, label, {k: v for k, v in o.items() if v is not DROP})


def channel_push(key: str = "channel_alice_phone", label: str = "sig_hub_channel_push", **kw: Any) -> dict[str, Any]:
    o = {
        **hdr("hub.channel_push"),
        "hub_id": HUB_ID,
        "channel_id": CH_ALICE,
        "push_pub": K.b("push_alice_phone"),
        "issued_ms": NOW - 1_000,
        "dev": True,
        **kw,
    }
    return signed(key, label, o)


def mailbox_target(ch: str = CH_ALICE, mid: str | None = None, hub: str = HUB_ID) -> str:
    return f"/v2/hub-channels/{hub}/{ch}/mailbox" + (f"/{mid}" if mid else "")


def channel_req(
    method: str = "GET",
    target: str | None = None,
    body: bytes = b"",
    *,
    cert: dict[str, Any] | None = None,
    key: str = "channel_alice_phone",
    ts: int = NOW,
    nonce: bytes = CH_NONCE,
    signed_target: str | None = None,
    signed_body: bytes | None = None,
    label: str | None = None,
    **hdr_kw: Any,
) -> dict[str, Any]:
    target = target or mailbox_target()
    st = signed_target or target
    hub, ch = st.split("/")[3], st.split("/")[4]
    msg = CA.channel_request_bytes(hub, ch, ts, nonce, method, st, body if signed_body is None else signed_body)
    if label is not None:  # wrong-domain vector: the same tail under another label
        msg = label.encode() + msg[len(b"orch/v2/hub-channel-relay-req|") :]
    headers = {
        "Orch-Channel-Cert": b64u(cj(cert or channel_cert())),
        "Orch-Channel-Ts": str(ts),
        "Orch-Channel-Nonce": nonce.hex(),
        "Orch-Channel-Signature": b64u(S.sign(K.priv[key], msg, deterministic=True)),
    }
    headers.update(hdr_kw)
    return {
        "method": method,
        "target": target,
        "headers": {k: v for k, v in headers.items() if v is not None},
        "body_hex": body.hex(),
    }


def build_b(
    case: Callable[..., None],
    sig_entry: Callable[[str, str, str, dict[str, Any]], None],
) -> dict[str, Any]:
    base = {"now_ms": NOW, "dev_mode": True}

    # ---- §6.4 the pairing link (operator QR); the app parses it before anything else
    def pl(name: str, link: Any, expect: dict[str, Any], note: str = "", real: bool = False) -> None:
        case(name, "pair_link", {"link": link, "dev_mode": not real}, expect, 4, note)

    ok_link = {"api": f"https://{API_HOST}", "hub_id": HUB_ID, "user_id": U_ALICE, "code": CODE}
    pl("pair_link_valid", LINK, {"ok": ok_link})
    pl(
        "pair_link_with_port",
        LINK.replace(API_HOST, API_HOST + ":8443"),
        {"ok": {**ok_link, "api": f"https://{API_HOST}:8443"}},
    )
    pl(
        "pair_link_loopback_is_http",
        LINK.replace(API_HOST, "127.0.0.1:8443"),
        {"ok": {**ok_link, "api": "http://127.0.0.1:8443"}},
        "dev only: a loopback host is plain http, every other host https",
    )
    pl(
        "pair_link_loopback_at_real_verifier",
        LINK.replace(API_HOST, "127.0.0.1:8443"),
        {"error": "malformed"},
        "a real build is https only: no loopback, no plain http",
        real=True,
    )
    pl("pair_link_localhost_at_real_verifier", LINK.replace(API_HOST, "localhost:8443"), {"error": "malformed"},
       real=True)  # fmt: skip
    pl("pair_link_https_host_at_real_verifier", LINK, {"ok": ok_link}, real=True)
    frag = f"#h1.{HUB_ID}.{U_ALICE}.{CODE}"
    bad = {
        "upper_case_hub_id": LINK.replace(HUB_ID, HUB_ID.upper()),
        "upper_case_host": LINK.replace(API_HOST, API_HOST.upper()),
        "scheme_https": f"https://{API_HOST}/{frag}",
        "userinfo": f"orch://hub-pair/mallory@{API_HOST}{frag}",
        "path_after_host": f"orch://hub-pair/{API_HOST}/x{frag}",
        "query": f"orch://hub-pair/{API_HOST}?api=x{frag}",
        "other_version": LINK.replace("#h1.", "#h2."),
        "missing_code": f"orch://hub-pair/{API_HOST}#h1.{HUB_ID}.{U_ALICE}",
        "extra_part": LINK + ".x",
        "code_upper_case": LINK.replace(CODE, CODE.upper()),
        "port_zero": LINK.replace(API_HOST, API_HOST + ":0"),
        "port_too_big": LINK.replace(API_HOST, API_HOST + ":65536"),
        "port_leading_zero": LINK.replace(API_HOST, API_HOST + ":08443"),
        "second_fragment": LINK + "#x",
        "no_fragment": f"orch://hub-pair/{API_HOST}",
        "empty_host": f"orch://hub-pair/{frag}",
        "host_trailing_dot": LINK.replace(API_HOST, API_HOST + "."),
        "non_ascii_host": LINK.replace(API_HOST, "hüb.example"),
        "percent_encoded": LINK.replace(API_HOST, "hub%2eexample"),
    }
    for n, link in bad.items():
        pl(f"pair_link_{n}", link, {"error": "malformed"})

    # ---- §6.4 the hub's signed API origin (L2B-1): checked against the link before any password
    ob = {**base, "card": card(), "link_api": f"https://{API_HOST}"}

    def org(name: str, s: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "api_origin", {**ob, **kw, "signed": s}, expect, 4, note)

    org("api_origin_valid", api_origin(), {"ok": {"origin": f"https://{API_HOST}"}})
    org(
        "api_origin_phishing_link",
        api_origin(),
        {"error": "other_origin"},
        "a link to another host with the real hub_id: the relayed object names the real origin, so no password is sent",
        link_api="https://hub-login.attacker.example",
    )
    org("api_origin_other_port", api_origin(), {"error": "other_origin"}, link_api=f"https://{API_HOST}:8443")
    org("api_origin_signed_by_rogue_op", api_origin(key="hk_op_rogue"), {"error": "bad_signature"})
    org("api_origin_under_card_label", api_origin(label="sig_hub_card"), {"error": "bad_signature"})
    org("api_origin_other_hub", api_origin(hub_id=fake16("other_hub")), {"error": "other_hub"})
    org("api_origin_stale_op_cert", api_origin(op_cert_id=OP_CERT_NEXT), {"error": "stale_op_cert"})
    org("api_origin_revoked_op_cert", api_origin(), {"error": "revoked"}, revoked_later=[OP_CERT_ID])
    org("api_origin_future", api_origin(issued_ms=NOW + 301_000), {"error": "cert_invalid"})
    org("api_origin_not_an_origin", api_origin(origin=f"https://{API_HOST}/path"), {"error": "malformed"})
    org(
        "api_origin_http_loopback_dev",
        api_origin(origin="http://127.0.0.1:8443"),
        {"ok": {"origin": "http://127.0.0.1:8443"}},
        link_api="http://127.0.0.1:8443",
    )
    org(
        "api_origin_http_at_real_verifier",
        api_origin(origin="http://127.0.0.1:8443", dev=False),
        {"error": "malformed"},
        card=card(op=op_cert(dev=False), dev=False),
        dev_mode=False,
        link_api="http://127.0.0.1:8443",
    )
    org("api_origin_dev_false", api_origin(dev=False), {"error": "dev_mismatch"})

    # ---- §6.4 channel session: the channel key binds a pairing session of its user to itself
    channels = {
        CH_ALICE: {"user_id": U_ALICE, "kind": "phone", "key_pub": K.b("channel_alice_phone"),
                   "expires_ms": NOW + 30 * DAY, "revoked": False},
        CH_BOB: {"user_id": U_BOB, "kind": "browser", "key_pub": K.b("channel_bob_browser"),
                 "expires_ms": NOW + 30 * DAY, "revoked": False},
    }  # fmt: skip
    ab = {**base, "hub_id": HUB_ID, "session_user": U_ALICE, "session_token": SESSION, "channels": channels}

    def au(name: str, s: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "channel_auth", {**ab, **kw, "signed": s}, expect, 4, note)

    a_ok = {"ok": {"channel_id": CH_ALICE, "user_id": U_ALICE}}
    au("channel_auth_valid", channel_auth(), a_ok)
    au(
        "channel_auth_other_session",
        channel_auth(),
        {"error": "challenge_invalid"},
        "signed for another pairing session: the binding cannot be carried to a session it was not made for",
        session_token=OTHER_SESSION,
    )
    au("channel_auth_stale", channel_auth(ts_ms=NOW - 121_000), {"error": "stale_timestamp"})
    au("channel_auth_future", channel_auth(ts_ms=NOW + 121_000), {"error": "stale_timestamp"})
    au("channel_auth_under_proof_label", channel_auth(label="sig_hub_channel_proof"), {"error": "bad_signature"})
    au(
        "channel_auth_under_register_label",
        channel_auth(label="sig_hub_channel_register"),
        {"error": "bad_signature"},
    )
    au("channel_auth_signed_by_push_key", channel_auth(key="push_alice_phone"), {"error": "bad_signature"})
    au(
        "channel_auth_channel_of_other_user",
        channel_auth(),
        {"error": "unknown_channel"},
        "Bob's session names Alice's channel: the same answer as an unknown id",
        session_user=U_BOB,
    )
    au(
        "channel_auth_unknown_channel",
        channel_auth(channel_id=fake16("no_such_channel")),
        {"error": "unknown_channel"},
    )
    au(
        "channel_auth_browser_channel",
        channel_auth(key="channel_bob_browser", channel_id=CH_BOB, user_id=U_BOB),
        {"error": "not_allowed"},
        "V3: only a phone channel holds a channel session",
        session_user=U_BOB,
    )
    au("channel_auth_other_hub", channel_auth(hub_id=fake16("other_hub")), {"error": "other_hub"})
    au("channel_auth_other_user_field", channel_auth(user_id=U_BOB), {"error": "other_user"})
    au(
        "channel_auth_revoked",
        channel_auth(),
        {"error": "channel_revoked"},
        "told only to the key holder (after the signature): the phone wipes on it (B6)",
        channels={**channels, CH_ALICE: {**channels[CH_ALICE], "revoked": True}},
    )
    au(
        "channel_auth_revoked_bad_signature",
        channel_auth(key="channel_bob_browser"),
        {"error": "bad_signature"},
        "a stranger learns nothing about the revocation",
        channels={**channels, CH_ALICE: {**channels[CH_ALICE], "revoked": True}},
    )
    au(
        "channel_auth_cert_expired",
        channel_auth(),
        {"error": "cert_expired"},
        channels={**channels, CH_ALICE: {**channels[CH_ALICE], "expires_ms": NOW}},
    )
    au("channel_auth_extra_field", channel_auth(extra=1), {"error": "malformed"})
    au("channel_auth_missing_dev", channel_auth(dev=DROP), {"error": "malformed"})
    au("channel_auth_dev_false", channel_auth(dev=False), {"error": "dev_mismatch"})
    au("channel_auth_dev_at_real_verifier", channel_auth(), {"error": "dev_refused"}, dev_mode=False)
    au("channel_auth_wrong_suite", channel_auth(suite=1), {"error": "wrong_suite"})
    au("channel_auth_session_ref_not_b64u32", channel_auth(session_ref="AAAA"), {"error": "malformed"})

    # ---- §6.4 the push key: what every hint is sealed to (§10.8.2)
    pb = {**base, "hub_id": HUB_ID, "channel_id": CH_ALICE, "key_pub": K.b("channel_alice_phone")}

    def pk(name: str, s: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "channel_push", {**pb, **kw, "signed": s}, expect, 4, note)

    p_ok = {"ok": {"push_pub": K.b("push_alice_phone"), "issued_ms": NOW - 1_000}}
    pk("channel_push_valid", channel_push(), p_ok)
    pk("channel_push_newer_than_held", channel_push(), p_ok, held_issued_ms=NOW - 60_000)
    pk(
        "channel_push_replayed_older",
        channel_push(),
        {"error": "replay"},
        "an earlier push-key registration cannot be replayed back",
        held_issued_ms=NOW - 1_000,
    )
    pk("channel_push_other_channel", channel_push(channel_id=CH_BOB), {"error": "other_user"})
    pk("channel_push_other_hub", channel_push(hub_id=fake16("other_hub")), {"error": "other_hub"})
    pk(
        "channel_push_key_is_channel_key",
        channel_push(push_pub=K.b("channel_alice_phone")),
        {"error": "malformed"},
        "one key, one use: the NSE never holds the signing key",
    )
    pk("channel_push_signed_by_push_key", channel_push(key="push_alice_phone"), {"error": "bad_signature"})
    pk("channel_push_under_auth_label", channel_push(label="sig_hub_channel_auth"), {"error": "bad_signature"})
    pk("channel_push_stale", channel_push(issued_ms=NOW - 121_000), {"error": "stale_timestamp"})
    pk("channel_push_bad_key", channel_push(push_pub="AAAA"), {"error": "malformed"})
    pk("channel_push_dev_false", channel_push(dev=False), {"error": "dev_mismatch"})

    # ---- §10.8.4 the channel's own mailbox read at the relay
    rb = {**base, "hub_id": HUB_ID, "hub_card": card()}
    r_ok = {"ok": {"hub": HUB_ID, "channel_id": CH_ALICE, "nonce": CH_NONCE.hex()}}

    def cr(name: str, req: dict[str, Any], expect: dict[str, Any], note: str = "", **kw: Any) -> None:
        case(name, "channel_request", {**rb, **kw, "request": req}, expect, 4, note)

    cr("channel_request_list", channel_req(), r_ok)
    cr("channel_request_get", channel_req(target=mailbox_target(mid=MID)), r_ok)
    cr("channel_request_delete", channel_req("DELETE", mailbox_target(mid=MID)), r_ok)
    cr(
        "channel_request_replayed_nonce",
        channel_req("DELETE", mailbox_target(mid=MID)),
        {"error": "replay"},
        "single-use nonce per (hub, channel) for 240 s",
        nonces={f"{HUB_ID}/{CH_ALICE}": {CH_NONCE.hex(): NOW - 1000}},
    )
    cr("channel_request_stale", channel_req(ts=NOW - 121_000), {"error": "stale_timestamp"})
    cr(
        "channel_request_revoked_channel",
        channel_req(),
        {"error": "channel_revoked"},
        "the hub's signed mailbox purge on revoke (§10.8.1) marks the channel revoked: no read until the cert expires",
        revoked_channels={HUB_ID: [CH_ALICE]},
    )
    cr(
        "channel_request_revoked_channel_bad_signature",
        channel_req(key="channel_bob_browser"),
        {"error": "bad_channel_signature"},
        "a stranger learns nothing about the revocation",
        revoked_channels={HUB_ID: [CH_ALICE]},
    )
    cr(
        "channel_request_other_channel_revoked",
        channel_req(),
        r_ok,
        "a revocation is per (hub, channel)",
        revoked_channels={HUB_ID: [CH_BOB], fake16("other_hub"): [CH_ALICE]},
    )
    cr(
        "channel_request_target_swapped",
        channel_req(target=mailbox_target(mid=MID), signed_target=mailbox_target(mid=fake16("other_msg"))),
        {"error": "bad_channel_signature"},
    )
    cr(
        "channel_request_body_swapped",
        channel_req(body=b"", signed_body=b"{}"),
        {"error": "bad_channel_signature"},
    )
    cr(
        "channel_request_other_channels_mailbox",
        channel_req(target=mailbox_target(ch=CH_BOB), key="channel_alice_phone"),
        {"error": "other_user"},
        "Alice's certificate and key cannot read Bob's mailbox",
    )
    cr(
        "channel_request_other_hub",
        channel_req(target=mailbox_target(hub=fake16("other_hub"))),
        {"error": "unknown_hub"},
    )
    cr(
        "channel_request_signed_by_other_key",
        channel_req(key="channel_bob_browser"),
        {"error": "bad_channel_signature"},
    )
    cr(
        "channel_request_signed_by_hub_op",
        channel_req(key="hk_op"),
        {"error": "bad_channel_signature"},
        "the hub cannot pose as one of its channels at the relay",
    )
    cr(
        "channel_request_under_hub_relay_label",
        channel_req(label="orch/v2/hub-relay-req|"),
        {"error": "bad_channel_signature"},
    )
    cr(
        "channel_request_cert_expired",
        channel_req(cert=channel_cert(expires_ms=NOW)),
        {"error": "cert_expired"},
    )
    cr(
        "channel_request_cert_by_rogue_op",
        channel_req(cert=resign("hk_op_rogue", "sig_hub_channel_cert", channel_cert())),
        {"error": "bad_signature"},
    )
    cr(
        "channel_request_cert_under_stale_op_cert",
        channel_req(cert=channel_cert(op_cert_id=OP_CERT_NEXT)),
        {"error": "stale_op_cert"},
    )
    cr(
        "channel_request_op_cert_revoked",
        channel_req(),
        {"error": "unknown_hub"},
        "the held card's op cert is revoked: every channel certificate under it dies with it",
        revoked_op_certs=[OP_CERT_ID],
    )
    cr("channel_request_post", channel_req("POST"), {"error": "malformed"})
    dup = channel_req()
    dup["headers"]["ORCH-CHANNEL-NONCE"] = "00" * 16
    cr("channel_request_duplicate_header", dup, {"error": "malformed"}, "L2B-9: two spellings of one header")
    cr("channel_request_query", channel_req(target=mailbox_target() + "?after=1"), {"error": "malformed"})
    cr("channel_request_no_cert", channel_req(**{"Orch-Channel-Cert": None}), {"error": "malformed"})
    cr("channel_request_cert_not_b64u", channel_req(**{"Orch-Channel-Cert": "{}"}), {"error": "malformed"})
    cr(
        "channel_request_under_hubs_prefix",
        channel_req(target=f"/v2/hubs/{HUB_ID}/mailbox/{CH_ALICE}", signed_target=mailbox_target()),
        {"error": "malformed"},
        "a channel never speaks under the hub-signed prefix",
    )

    def ro(name: str, method: str, target: str, expect: dict[str, Any], body: bytes = b"", note: str = "") -> None:
        case(name, "channel_route", {"method": method, "target": target, "body_hex": body.hex()}, expect, 4, note)

    base_r = {"hub": HUB_ID, "channel_id": CH_ALICE}
    ro("channel_route_list", "GET", mailbox_target(), {"ok": {**base_r, "route": "mailbox_list"}})
    ro("channel_route_get", "GET", mailbox_target(mid=MID), {"ok": {**base_r, "route": "mailbox_get", "id": MID}})
    ro(
        "channel_route_delete",
        "DELETE",
        mailbox_target(mid=MID),
        {"ok": {**base_r, "route": "mailbox_delete", "id": MID}},
    )
    ro(
        "channel_route_purge_is_the_hubs",
        "DELETE",
        mailbox_target(),
        {"error": "not_found"},
        note="only the hub empties a whole mailbox (on revoke)",
    )
    ro("channel_route_put", "PUT", mailbox_target(mid=MID), {"error": "not_found"})
    ro("channel_route_push", "GET", f"/v2/hub-channels/{HUB_ID}/{CH_ALICE}/push", {"error": "not_found"})
    ro("channel_route_id_not_hex", "GET", mailbox_target() + "/xyz", {"error": "not_found"})
    ro("channel_route_body", "GET", mailbox_target(), {"error": "malformed"}, body=b"{}")

    # ---- signatures and the section
    sig_entry("api_origin", "hk_op", "sig_hub_api_origin", api_origin())
    sig_entry("channel_auth_alice_phone", "channel_alice_phone", "sig_hub_channel_auth", channel_auth())
    sig_entry("channel_push_alice_phone", "channel_alice_phone", "sig_hub_channel_push", channel_push())
    req = channel_req()
    return {
        "pair_link": {"link": LINK, "parsed": ok_link, "fingerprint": CA.fingerprint(HUB_ID)},
        "api_origin": api_origin(),
        "session_ref": {"token": SESSION, "session_ref": CA.session_ref(SESSION)},
        "channel_auth": channel_auth(),
        "channel_push": channel_push(),
        "channel_request": {
            "request": req,
            "signed_hex": CA.channel_request_bytes(HUB_ID, CH_ALICE, NOW, CH_NONCE, "GET", mailbox_target(), b"").hex(),
        },
        "mailbox_list_answer": {"messages": [{"id": MID, "expires_ms": NOW + DAY}]},
        "channel_session_ms": CA.CHANNEL_SESSION_MS,
    }


__all__ = ["build_b"]
