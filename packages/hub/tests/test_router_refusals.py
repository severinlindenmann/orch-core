"""Router refusals: every one a protocol.md §14 code, nothing queued, nothing logged about the envelope."""

# ruff: noqa: F811  (the `rhub` fixture is imported, then named as a test argument)
from __future__ import annotations

import logging

import pytest
from router_helpers import envelope, rhub  # noqa: F401

from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.link import LinkError
from orch_hub.router import Limits
from orch_hub.router.envelope import MAX_ENVELOPE


def refused(c, env: bytes, code: str):
    with pytest.raises(LinkError) as e:
        c.route_out(env)
    assert e.value.code == code
    assert "state" not in e.value.args


def test_unjoined_sender_is_refused(rhub):
    h = rhub()
    c = h.open(first=True)  # authenticated by first-contact key, but never joined
    refused(c, envelope(), "unknown_workspace")


def test_unbound_sender_is_refused(rhub):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    a.request("binding_put", {"object": G.unbound(seq=1)})
    refused(a, envelope(), "not_bound")
    assert h.queue.depth(G.WS_B) == 0


def test_recipient_not_joined_or_unbound_is_not_allowed(rhub):
    h = rhub()
    a = h.join(G.WS_A)
    refused(a, envelope(), "not_allowed")  # B never joined
    b = h.join(G.WS_B)
    b.request("binding_put", {"object": G.unbound(ws=G.WS_B, wsk="wsk_b", seq=1)})
    refused(a, envelope(), "not_allowed")
    assert h.queue.depth(G.WS_B) == 0


def test_authz_denial_is_not_allowed(rhub):
    seen = []

    class Deny:
        def may_route(self, s, r):
            seen.append((s, r))
            return False

    h = rhub(authz=Deny())
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    refused(a, envelope(), "not_allowed")
    assert seen == [(G.WS_A, G.WS_B)]


def test_authz_is_asked_after_the_signature(rhub):
    class Boom:
        def may_route(self, s, r):
            raise AssertionError("must not be reached for a forged envelope")

    h = rhub(authz=Boom())
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    refused(a, envelope(key="wsk_b"), "bad_signature")


def test_sender_must_be_the_link_workspace(rhub):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    refused(a, envelope(frm=G.WS_B, to=G.WS_A, key="wsk_b"), "other_workspace")  # A relays an envelope of B
    refused(a, envelope(frm=G.WS_B, to=G.WS_A, key="wsk_a"), "other_workspace")
    assert h.queue.depth(G.WS_A) == 0 and h.queue.depth(G.WS_B) == 0


def test_bad_signature_and_tampered_bytes(rhub):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    env = bytearray(envelope())
    env[100] ^= 1  # a body byte
    refused(a, bytes(env), "bad_signature")
    env = bytearray(envelope())
    env[56] ^= 1  # wxk_version in the header
    refused(a, bytes(env), "bad_signature")
    assert h.queue.depth(G.WS_B) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: b"XXXX" + e[4:],  # magic
        lambda e: e[:4] + b"\x01" + e[5:],  # version
        lambda e: e[:5] + b"\x09" + e[6:],  # suite
        lambda e: e[:6] + b"\x04" + e[7:],  # unknown flag bit
        lambda e: e[:7] + b"\x01" + e[8:],  # reserved byte
        lambda e: e[:-1],  # short signature
        lambda e: e + b"\x00",  # trailing bytes without COSIGNED
        lambda e: e[:60],  # header only
        lambda e: b"",
    ],
)
def test_malformed_envelopes(rhub, mutate):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    with pytest.raises(LinkError) as e:
        a.request("route_out", {"envelope": b64u(mutate(envelope())) if mutate(envelope()) else "AA"})
    assert e.value.code == "malformed"


def test_cosigned_flag_needs_its_block_and_length(rhub):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    e = envelope(flags=1, cosig=b"x")
    refused(a, e + b"y", "malformed")  # length field disagrees
    refused(a, e[: -len(b"x") - 4], "malformed")  # flag set, block stripped
    refused(a, envelope(flags=1, cosig=b""), "malformed")  # empty block


def test_to_self_is_malformed(rhub):
    h = rhub()
    a = h.join(G.WS_A)
    refused(a, envelope(to=G.WS_A), "malformed")


def test_oversize_envelope_is_too_large(rhub):
    h = rhub()
    a, _ = h.join(G.WS_A), h.join(G.WS_B)
    refused(a, envelope(body=bytes(MAX_ENVELOPE)), "too_large")  # a few bytes over 1 MiB
    # exactly at the cap is fine
    big = envelope(body=bytes(MAX_ENVELOPE - 60 - 4 - 64))
    assert len(big) == MAX_ENVELOPE
    assert a.route_out(big) in ("forwarded", "queued")


def test_queue_bounds(rhub):
    h = rhub(limits=Limits(max_entries=3, max_bytes=10_000, max_per_sender=2))
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    a.route_out(envelope())
    a.route_out(envelope())
    refused(a, envelope(), "queue_full")  # per-sender cap
    assert h.queue.depth(G.WS_B) == 2


def test_queue_total_and_byte_bounds(rhub):
    h = rhub(limits=Limits(max_entries=2, max_bytes=700, max_per_sender=10))
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    a.route_out(envelope())  # ~330 bytes each
    a.route_out(envelope())
    refused(a, envelope(), "queue_full")
    h.queue.limits = Limits(max_entries=10, max_bytes=700, max_per_sender=10)
    refused(a, envelope(body=bytes(400)), "queue_full")  # bytes bound
    assert h.queue.depth(G.WS_B) == 2


def test_same_id_other_bytes_is_replay(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    eid = b"\x07" * 16
    a.route_out(envelope(env_id=eid, body=b"one"))
    refused(a, envelope(env_id=eid, body=b"two"), "replay")


def test_no_envelope_bytes_in_logs(rhub, caplog):
    caplog.set_level(logging.DEBUG)
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    marker = b"SECRET-MARKER-BYTES"
    a.route_out(envelope(body=marker * 4))
    b.next_route_in()
    with pytest.raises(LinkError):
        a.route_out(envelope(key="wsk_b", body=marker * 4))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET" not in text and b64u(marker)[:12] not in text
