"""CryptoVerifier: person/host/embedded verification, and conformance with C4's Protocol once it is merged."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from orch import crypto
from orch.identity import CryptoVerifier, make_revocation

from .helpers import NOW, WS, Person, build_genesis, host_sign, sign_person

TICKET = "01J9ZP0000000000000000000A"


def ticket_event(p: Person, **over):
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000C",
        "seq": 3,
        "at": "2026-10-10T10:00:00Z",
        "prev": None,
        "type": "ticket.updated",
        "actor": {"kind": "person", "id": p.ref, "device": p.device},
        "based_on": None,
        "hash_v": 1,
        "ws_seq": 1,
        "roster_v": 1,
        "auth": "passphrase",
    }
    ev.update(over)
    sign_person(p, ev, TICKET)
    return ev


def ctx(p: Person, log=TICKET, cert=None):
    return SimpleNamespace(workspace_id=WS, log=log, cert=cert if cert is not None else p.cert())


def test_verify_person():
    p, v = Person(), CryptoVerifier(WS)
    ev = ticket_event(p)
    assert v.verify_person(ev, ctx(p))
    assert v.verify_person(ev, ctx(p, cert=p.cert()["o"]))
    assert not v.verify_person(ev, ctx(p, log="01J9ZP0000000000000000000Z")), "replay into another log"
    assert not v.verify_person(ev, SimpleNamespace(workspace_id="11" * 16, log=TICKET, cert=p.cert()))
    assert not v.verify_person(ev, ctx(Person())), "another device's certificate"
    bad = copy.deepcopy(ev)
    bad["auth"] = "webauthn"
    assert not v.verify_person(bad, ctx(p))
    bad = copy.deepcopy(ev)
    bad["sig"] = bad["sig"][:-3] + "AAA"
    assert not v.verify_person(bad, ctx(p))
    unsigned = {k: x for k, x in ev.items() if k != "sig"}
    assert not v.verify_person(unsigned, ctx(p))
    assert not v.verify_person(ev, SimpleNamespace())
    assert not v.verify_person({}, ctx(p))


def test_verify_person_checks_the_actor_device_matches_the_certificate():
    p, other = Person(), Person()
    ev = ticket_event(p, actor={"kind": "person", "id": p.ref, "device": other.device})
    assert not CryptoVerifier(WS).verify_person(ev, ctx(p))
    ev = ticket_event(p, actor={"kind": "agent"})
    assert not CryptoVerifier(WS).verify_person(ev, ctx(p))


def test_verify_host():
    g = build_genesis()
    wsk_pub = crypto.public_bytes(g.wsk)
    v = CryptoVerifier(WS, wsk_pub)
    assert v.verify_host(g.event)
    assert CryptoVerifier(WS, crypto.b64u(wsk_pub)).verify_host(g.event)
    assert not CryptoVerifier(WS, crypto.public_bytes(crypto.generate_private_key())).verify_host(g.event)
    assert not CryptoVerifier("22" * 16, wsk_pub).verify_host(g.event)
    ev = copy.deepcopy(g.event)
    ev["prefix"] = "X"
    assert not v.verify_host(ev)
    assert not v.verify_host({})


def test_verify_host_on_ticket_logs_needs_the_log():
    g = build_genesis()
    wsk_pub = crypto.public_bytes(g.wsk)
    p = Person()
    ev = ticket_event(p)
    host_sign(g.wsk, ev, TICKET)
    v = CryptoVerifier(WS, wsk_pub)
    assert not v.verify_host(ev), "ticket uid unknown: fail closed"
    assert v.for_log(TICKET).verify_host(ev)
    assert not v.for_log("workspace").verify_host(ev)
    assert not v.for_log("01J9ZP0000000000000000000Z").verify_host(ev)


def test_genesis_host_sig_without_a_pinned_key_only_for_genesis():
    g = build_genesis()
    assert CryptoVerifier(WS).verify_host(g.event)
    ev = copy.deepcopy(g.event)
    ev["type"] = "member.added"
    host_sign(g.wsk, ev)
    assert not CryptoVerifier(WS).verify_host(ev)


def test_verify_embedded_genesis():
    g = build_genesis()
    v = CryptoVerifier(WS)
    assert v.verify_embedded(g.event, None)
    assert v.verify_embedded(g.event, crypto.b64u(g.owner.pk_pub))
    assert not v.verify_embedded(g.event, crypto.b64u(Person().pk_pub)), "another person's key"
    ev = copy.deepcopy(g.event)
    ev["delegation"]["o"]["client_hosted"] = True
    assert not v.verify_embedded(ev, None)
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = Person().cert()
    assert not v.verify_embedded(ev, None)
    assert not CryptoVerifier("33" * 16).verify_embedded(g.event, None), "other workspace"


def test_verify_embedded_member_added_device_added_device_revoked():
    p, m = Person(), Person()
    v = CryptoVerifier(WS)
    added = {"type": "member.added", "person": m.ref, "pk_pub": crypto.b64u(m.pk_pub), "device_cert": m.cert()}
    assert v.verify_embedded(added, None) and v.verify_embedded(added, crypto.b64u(m.pk_pub))
    assert not v.verify_embedded(added, crypto.b64u(p.pk_pub))
    assert not v.verify_embedded({**added, "device_cert": p.cert()}, None)

    dev = {"type": "device.added", "device": m.device, "cert": m.cert()}
    assert v.verify_embedded(dev, crypto.b64u(m.pk_pub))
    assert not v.verify_embedded(dev, None), "device.added needs the person's key"
    assert not v.verify_embedded(dev, crypto.b64u(p.pk_pub))

    did = crypto.device_id(m.sig_pub).hex()
    rev = make_revocation(m.pk_pub, m.pk_sign, device_id_hex=did, revoked_ms=NOW, reason="lost")
    r_ev = {"type": "device.revoked", "device": "d_" + did, "reason": "lost", "revocation": rev}
    assert v.verify_embedded(r_ev, crypto.b64u(m.pk_pub))
    assert not v.verify_embedded(r_ev, crypto.b64u(p.pk_pub))
    assert not v.verify_embedded({**r_ev, "reason": "retired"}, crypto.b64u(m.pk_pub))


def test_other_types_embed_nothing_and_garbage_never_raises():
    v = CryptoVerifier(WS)
    assert v.verify_embedded({"type": "ticket.updated"}, None)
    for junk in ({}, {"type": "member.added"}, {"type": "workspace.created"}, {"type": "device.revoked"}):
        assert v.verify_embedded(junk, None) in (True, False)
    assert not v.verify_embedded({"type": "device.added", "cert": 5}, "nope")


def test_satisfies_c4_protocol():
    """Runs once C4 (orch.model.verifier) is merged; skipped until then."""
    mod = pytest.importorskip("orch.model.verifier")
    p = Person()
    ev = ticket_event(p)
    v = CryptoVerifier(WS)
    context = mod.SigContext(workspace_id=WS, log=TICKET, cert=p.cert())
    assert v.verify_person(ev, context) is True
    g = build_genesis()
    assert CryptoVerifier(WS).verify_host(g.event) is True
    assert CryptoVerifier(WS).verify_embedded(g.event, None) is True
    for name in ("verify_person", "verify_host", "verify_embedded"):
        assert callable(getattr(mod.Verifier, name))
