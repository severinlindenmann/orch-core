"""CryptoVerifier: person/host/embedded verification, and conformance with C4's Protocol once it is merged."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from orch import crypto
from orch.identity import CryptoVerifier, make_revocation

from .helpers import NOW, WS, Person, build_genesis, host_sign, resign, sign_person

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
    p, v = Person(), CryptoVerifier()
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
    assert not v.verify_person({k: x for k, x in ev.items() if k != "sig"}, ctx(p))
    assert not v.verify_person(ev, SimpleNamespace())
    assert not v.verify_person({}, ctx(p))


def test_verify_person_checks_the_actor_device_matches_the_certificate():
    p, other = Person(), Person()
    assert not CryptoVerifier().verify_person(
        ticket_event(p, actor={"kind": "person", "id": p.ref, "device": other.device}), ctx(p)
    )
    assert not CryptoVerifier().verify_person(ticket_event(p, actor={"kind": "agent"}), ctx(p))


def test_verify_host_takes_log_key_and_workspace_per_call():
    g = build_genesis()
    wsk_pub = crypto.public_bytes(g.wsk)
    v = CryptoVerifier()
    assert v.verify_host(g.event, log="workspace", wsk_pub=wsk_pub, workspace_id=WS)
    assert v.verify_host(g.event, log="workspace", wsk_pub=None, workspace_id=WS), "genesis carries its own key"
    assert not v.verify_host(g.event, log="workspace", wsk_pub=None, workspace_id="11" * 16), "V3: genesis id differs"
    assert not v.verify_host(g.event, log="workspace", wsk_pub=wsk_pub, workspace_id="11" * 16)
    assert not v.verify_host(
        g.event, log="workspace", wsk_pub=crypto.public_bytes(crypto.generate_private_key()), workspace_id=WS
    )
    assert not v.verify_host(g.event, log=TICKET, wsk_pub=wsk_pub, workspace_id=WS)
    ev = copy.deepcopy(g.event)
    ev["prefix"] = "X"
    assert not v.verify_host(ev, log="workspace", wsk_pub=wsk_pub, workspace_id=WS)
    assert not v.verify_host({}, log="workspace", wsk_pub=wsk_pub, workspace_id=WS)
    assert not v.verify_host(g.event, log="workspace", wsk_pub=b"junk", workspace_id=WS)
    assert not v.verify_host(g.event, log="workspace", wsk_pub=wsk_pub, workspace_id=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        v.verify_host(g.event, log="workspace", wsk_pub=wsk_pub)  # type: ignore[call-arg]


def test_workspace_id_is_never_read_from_the_event():
    """V2: an event that names a workspace is verified against the one the caller passes, not its own."""
    g = build_genesis()
    other_ws = "22" * 16
    ev = {
        "v": 2,
        "id": TICKET,
        "seq": 2,
        "at": "2026-10-10T10:00:00Z",
        "prev": None,
        "hash_v": 1,
        "type": "member.removed",
        "workspace_id": WS,
        "person": "p_" + "1" * 32,
    }
    host_sign(g.wsk, ev, "workspace", ws=other_ws)  # signed for another workspace but names WS inside
    wsk_pub = crypto.public_bytes(g.wsk)
    v = CryptoVerifier()
    assert v.verify_host(ev, log="workspace", wsk_pub=wsk_pub, workspace_id=other_ws)
    assert not v.verify_host(ev, log="workspace", wsk_pub=wsk_pub, workspace_id=WS)


def test_verify_host_on_ticket_logs():
    g = build_genesis()
    wsk_pub = crypto.public_bytes(g.wsk)
    ev = ticket_event(Person())
    host_sign(g.wsk, ev, TICKET)
    v = CryptoVerifier()
    assert v.verify_host(ev, log=TICKET, wsk_pub=wsk_pub, workspace_id=WS)
    assert not v.verify_host(ev, log=TICKET, wsk_pub=wsk_pub, workspace_id="11" * 16)
    assert not v.verify_host(ev, log="workspace", wsk_pub=wsk_pub, workspace_id=WS)
    assert not v.verify_host(ev, log="01J9ZP0000000000000000000Z", wsk_pub=wsk_pub, workspace_id=WS)


def test_no_key_means_genesis_only():
    g = build_genesis()
    ev = copy.deepcopy(g.event)
    ev["type"] = "member.added"
    host_sign(g.wsk, ev)
    assert not CryptoVerifier().verify_host(ev, log="workspace", wsk_pub=None, workspace_id=WS)
    ticket = ticket_event(Person())
    host_sign(g.wsk, ticket, TICKET)
    assert not CryptoVerifier().verify_host(ticket, log=TICKET, wsk_pub=None, workspace_id=WS)


def test_verifier_is_stateless_and_immutable():
    v = CryptoVerifier()
    with pytest.raises(AttributeError):
        v.x = 1  # type: ignore[attr-defined]
    assert not hasattr(v, "for_log")


def test_verify_embedded_genesis_uses_the_one_genesis_implementation():
    g = build_genesis()
    v = CryptoVerifier()
    assert v.verify_embedded(g.event, pk_pub=None)
    assert v.verify_embedded(g.event, pk_pub=crypto.b64u(g.owner.pk_pub))
    assert not v.verify_embedded(g.event, pk_pub=crypto.b64u(Person().pk_pub)), "another person's key"
    ev = copy.deepcopy(g.event)
    ev["delegation"]["o"]["client_hosted"] = True
    assert not v.verify_embedded(ev, pk_pub=None)
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = Person().cert()
    assert not v.verify_embedded(ev, pk_pub=None)
    ev = copy.deepcopy(g.event)
    ev["device_cert"] = g.owner.cert(scopes=("look",))
    assert not v.verify_embedded(resign(g, ev), pk_pub=None), "scopes are checked too (check_genesis)"


def test_verify_embedded_member_added_device_added_device_revoked():
    p, m = Person(), Person()
    v = CryptoVerifier()
    added = {"type": "member.added", "person": m.ref, "pk_pub": crypto.b64u(m.pk_pub), "device_cert": m.cert()}
    assert v.verify_embedded(added, pk_pub=None) and v.verify_embedded(added, pk_pub=crypto.b64u(m.pk_pub))
    assert not v.verify_embedded(added, pk_pub=crypto.b64u(p.pk_pub))
    assert not v.verify_embedded({**added, "device_cert": p.cert()}, pk_pub=None)

    dev = {"type": "device.added", "device": m.device, "cert": m.cert()}
    assert v.verify_embedded(dev, pk_pub=crypto.b64u(m.pk_pub))
    assert not v.verify_embedded(dev, pk_pub=None), "device.added needs the person's key"
    assert not v.verify_embedded(dev, pk_pub=crypto.b64u(p.pk_pub))

    did = crypto.device_id(m.sig_pub).hex()
    cert_o = m.cert()["o"]
    rev = make_revocation(m.pk_pub, m.pk_sign, device_id_hex=did, revoked_ms=NOW, reason="lost")
    r_ev = {"type": "device.revoked", "device": "d_" + did, "reason": "lost", "revocation": rev}
    assert v.verify_embedded(r_ev, pk_pub=m.pk_pub, device_cert=cert_o)
    assert not v.verify_embedded(r_ev, pk_pub=m.pk_pub), "V1: device_cert is required"
    assert not v.verify_embedded(r_ev, pk_pub=m.pk_pub, device_cert=None)
    assert not v.verify_embedded(r_ev, pk_pub=None), "no person key: fail closed"
    assert not v.verify_embedded(r_ev, pk_pub=m.pk_pub, device_cert=p.cert()["o"]), "another person's device"
    assert not v.verify_embedded(r_ev, pk_pub=p.pk_pub, device_cert=cert_o)
    assert not v.verify_embedded({**r_ev, "reason": "retired"}, pk_pub=m.pk_pub, device_cert=cert_o)


def test_unknown_missing_or_malformed_types_are_false_and_never_raise():
    v = CryptoVerifier()
    for junk in (
        {},
        {"x": 1.5},
        {"type": None},
        {"type": 5},
        {"type": ["member.added"]},
        {"type": "ticket.updated"},
        {"type": "member.added"},
        {"type": "workspace.created"},
        {"type": "device.revoked"},
        {"type": "device.added", "cert": 5},
    ):
        assert v.verify_embedded(junk, pk_pub=None) is False
    assert v.verify_embedded({"type": "device.added", "cert": 5}, pk_pub="nope") is False


def test_signatures_match_the_c4_interface():
    """The interface as decided in the C4 rulings; tightened to the real Protocol once C4 is merged (below)."""
    import inspect

    def params(fn):
        return [(n, p.kind, p.default is p.empty) for n, p in inspect.signature(fn).parameters.items() if n != "self"]

    class Interface:
        def verify_person(self, event, context): ...
        def verify_host(self, event, *, log, wsk_pub, workspace_id): ...
        def verify_embedded(self, event, *, pk_pub): ...

    assert params(CryptoVerifier.verify_person) == params(Interface.verify_person)
    assert params(CryptoVerifier.verify_host) == params(Interface.verify_host)
    ours, theirs = params(CryptoVerifier.verify_embedded), params(Interface.verify_embedded)
    assert ours[: len(theirs)] == theirs
    assert [n for n, _, req in ours[len(theirs) :] if req] == [], "extra parameters are optional keywords"
    assert [n for n, _, _ in ours] == ["event", "pk_pub", "device_cert"]


def test_satisfies_c4_protocol():
    """Runs once C4 (orch.model.verifier) is merged and carries the amended Protocol; skipped until then."""
    import inspect

    mod = pytest.importorskip("orch.model.verifier")
    for name in ("verify_person", "verify_host", "verify_embedded"):
        theirs = list(inspect.signature(getattr(mod.Verifier, name)).parameters)
        ours = list(inspect.signature(getattr(CryptoVerifier, name)).parameters)
        assert ours[: len(theirs)] == theirs, name
    p = Person()
    ev = ticket_event(p)
    context = mod.SigContext(workspace_id=WS, log=TICKET, cert=p.cert())
    v: mod.Verifier = CryptoVerifier()
    assert v.verify_person(ev, context) is True
    g = build_genesis()
    assert v.verify_host(g.event, log="workspace", wsk_pub=None, workspace_id=WS) is True
    assert v.verify_embedded(g.event, pk_pub=None) is True
