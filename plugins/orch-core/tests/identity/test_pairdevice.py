"""H12c-e (11A.15.10 item 12): the pure half of the person-path material kind ``relay.pair_device``.

``PairDeviceFacts`` is what R6 hands the host at step 9; :func:`material` is what the review binds;
:func:`check_material` is the signer's recomputation, refusing every unbound or extra field (one negative case per
binding)."""

from __future__ import annotations

import copy
import hashlib

import pytest

from orch import canon, crypto
from orch.identity import pairdevice as pd
from orch.identity.pairing import clean_label
from tests.identity.helpers import WS, Person

OFFER = "a" * 32
ATTEMPT = "b" * 32


def phone_of(owner: Person) -> Person:
    """A new device (fresh keys) of ``owner``'s person: what a phone is after the primary signed its cert."""
    p = Person()
    p.pk, p.pk_pub = owner.pk, owner.pk_pub
    return p


def facts(owner: Person | None = None, phone: Person | None = None, **kw) -> pd.PairDeviceFacts:
    owner = owner or Person()
    phone = phone or phone_of(owner)
    args = {
        "workspace_id": WS,
        "offer_id": OFFER,
        "attempt": ATTEMPT,
        "cert": phone.cert(scopes=("look", "decide")),
        "label": "Sev's iPhone",
        "scopes": ("look", "decide"),
        "path": "7b",
        "code": "ABC234",
        "decl": None,
        "decl_who": None,
    }
    args.update(kw)
    return pd.PairDeviceFacts(**args)


def bound(f: pd.PairDeviceFacts) -> tuple[dict, dict]:
    """(material without review, the unsigned event as human.prepare builds it)."""
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000A",
        "actor": {"kind": "person", "id": f.person, "device": None},
        "auth": None,
        "hash_v": 1,
        "roster_v": 3,
        "based_on": None,
        **pd.device_added(f),
    }
    return {**pd.material(f), "review": []}, ev


# --- the facts ----------------------------------------------------------------------------------------------------


def test_facts_name_the_device_and_person_of_the_certificate_and_keep_an_immutable_copy():
    owner = Person()
    phone = phone_of(owner)
    cert = phone.cert(scopes=("look", "decide"))
    f = facts(owner, phone, cert=cert)
    assert f.device == phone.device and f.person == owner.ref
    cert["o"]["device_id"] = "0" * 32  # the caller's dict changing later does not change the facts
    assert f.device == phone.device
    f.cert["sig"] = "x"  # nor does a returned copy
    assert f.cert["sig"] != "x"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"workspace_id": "A" * 32}, "workspace_id"),
        ({"offer_id": "a" * 31}, "offer_id"),
        ({"attempt": None}, "attempt"),
        ({"label": "tab\there"}, "label"),  # not what clean_label returns
        ({"label": "x" * 81}, "label"),
        ({"label": 7}, "label"),
        ({"scopes": ()}, "scopes"),
        ({"scopes": ("look", "decide", "operate")}, "scopes"),  # N1: no operate for a phone in v1.7
        ({"scopes": ("type",)}, "scopes"),
        ({"scopes": ("decide", "look")}, "scopes"),  # canonical order only
        ({"scopes": ("look", "look")}, "scopes"),
        ({"path": "7c"}, "path"),
        ({"code": "abc234"}, "code"),
        ({"code": "ABC23"}, "code"),
        ({"code": "ABC231"}, "code"),  # base32 has no 1
        ({"decl": {"o": {}, "sig": ""}, "decl_who": None}, "decl"),  # a declaration needs its who
        ({"decl": None, "decl_who": {"person_id": "c" * 32}}, "decl"),
        ({"cert": {"o": {}}}, "cert"),
    ],
)
def test_facts_refuse_anything_outside_the_contract(change, why):
    with pytest.raises(pd.PairDeviceInvalid) as e:
        facts(**change)
    assert e.value.reason == why


@pytest.mark.parametrize(
    "scopes",
    [("look", "decide", "operate"), ("look", "decide", "type"), ("look",), ("look", "decide", "drop:" + "ab" * 16)],
)
def test_a_phone_certificate_outside_look_decide_or_without_decide_is_refused(scopes):
    owner = Person()
    phone = phone_of(owner)
    cert = phone.cert(scopes=("look", "decide"))
    cert["o"]["scopes_max"] = list(scopes)  # this module checks the shape; the signature is R6's and the model's
    with pytest.raises(pd.PairDeviceInvalid) as e:
        facts(owner, phone, cert=cert, scopes=("look",))
    assert e.value.reason == "cert"


def test_the_list_scopes_must_be_within_the_certificates_ceiling():
    owner = Person()
    phone = phone_of(owner)
    assert facts(owner, phone, scopes=("look",)).scopes == ("look",)  # the list may be narrower
    cert = phone.cert(scopes=("look", "decide"))
    cert["o"]["scopes_max"] = ["decide"]
    with pytest.raises(pd.PairDeviceInvalid):  # never wider: the 7a cert of an earlier offer caps it (step 8)
        facts(owner, phone, cert=cert, scopes=("look", "decide"))


def test_the_label_is_the_clean_label_output_byte_for_byte_and_no_nfc():
    decomposed = "Café"  # NFD: clean_label keeps it as is (no normalisation, 11A.15.13)
    assert clean_label(decomposed) == decomposed
    assert facts(label=decomposed).label == decomposed
    assert facts(label="").label == ""  # "empty becomes phone" is display-only, never the recorded label


# --- material and event -------------------------------------------------------------------------------------------


def test_the_material_binds_exactly_the_reviewed_fields():
    f = facts(path="7a", code="XYZ567")
    m = pd.material(f)
    assert set(m) == pd.MATERIAL_FIELDS
    assert m == {
        "kind": "relay.pair_device",
        "workspace_id": WS,
        "offer_id": OFFER,
        "attempt": ATTEMPT,
        "device": f.device,
        "label": f.label,
        "scopes": ["look", "decide"],
        "cert_digest": "sha256:" + hashlib.sha256(canon.dumps(f.cert)).hexdigest(),
        "comparison": {"kind": "sas", "code": "XYZ567"},
    }
    assert pd.material(facts(path="7b"))["comparison"]["kind"] == "cert_code"


def test_the_event_is_exactly_device_added_device_cert_and_carries_no_label():
    f = facts()
    ev = pd.device_added(f)
    assert ev == {"type": "device.added", "device": f.device, "cert": f.cert}
    assert "label" not in ev


def test_check_material_accepts_what_material_and_device_added_built():
    f = facts()
    m, ev = bound(f)
    got = pd.check_material(m, ev, workspace_id=WS, person=f.person)
    assert got == {k: m[k] for k in pd.MATERIAL_FIELDS}


def _mut(path, value):
    def apply(m, ev):
        target = {"m": m, "ev": ev}[path[0]]
        for key in path[1:-1]:
            target = target[key]
        if value is _DEL:
            del target[path[-1]]
        else:
            target[path[-1]] = value

    return apply


_DEL = object()
OTHER = Person()


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (_mut(("m", "extra"), 1), "material"),  # any extra field
        (_mut(("m", "label"), _DEL), "material"),
        (_mut(("m", "kind"), "payload"), "kind"),  # a cross-kind substitution
        (_mut(("m", "workspace_id"), "c" * 32), "workspace_id"),
        (_mut(("m", "offer_id"), "c" * 31), "offer_id"),
        (_mut(("m", "attempt"), "C" * 32), "attempt"),
        (_mut(("m", "device"), "d_" + "c" * 32), "device"),
        (_mut(("ev", "device"), "d_" + "c" * 32), "device"),
        (_mut(("m", "label"), "a‮b"), "label"),  # a bidi override is not clean_label output
        (_mut(("m", "label"), "x" * 81), "label"),
        (_mut(("m", "scopes"), ["look", "decide", "operate"]), "scopes"),
        (_mut(("m", "scopes"), []), "scopes"),
        (_mut(("m", "cert_digest"), "sha256:" + "0" * 64), "cert_digest"),
        (_mut(("m", "comparison"), {"kind": "sas", "code": "ABC234", "x": 1}), "comparison"),
        (_mut(("m", "comparison"), {"kind": "pin", "code": "ABC234"}), "comparison"),
        (_mut(("m", "comparison", "code"), "ABC23!"), "comparison"),
        (_mut(("ev", "label"), "Sev's iPhone"), "event"),  # any extra event field
        (_mut(("ev", "type"), "member.added"), "event"),
        (_mut(("ev", "cert", "o", "scopes_max"), ["look", "decide", "operate"]), "cert"),
        (_mut(("ev", "cert", "o", "person_id"), OTHER.ref[2:]), "cert"),
    ],
)
def test_check_material_refuses_each_unbound_or_extra_field(mutate, why):
    f = facts()
    m, ev = bound(f)
    mutate(m, ev)
    with pytest.raises(pd.PairDeviceInvalid) as e:
        pd.check_material(m, ev, workspace_id=WS, person=f.person)
    assert e.value.reason == why


def test_check_material_refuses_scopes_out_of_order_and_a_signer_of_another_person():
    f = facts()
    m, ev = bound(f)
    m["scopes"] = ["decide", "look"]
    with pytest.raises(pd.PairDeviceInvalid, match="scopes"):
        pd.check_material(m, ev, workspace_id=WS, person=f.person)
    m, ev = bound(f)
    with pytest.raises(pd.PairDeviceInvalid, match="cert"):
        pd.check_material(m, ev, workspace_id=WS, person=OTHER.ref)


def test_the_cert_digest_is_over_the_event_cert_so_a_swapped_certificate_is_refused():
    owner = Person()
    f = facts(owner)
    m, ev = bound(f)
    other = phone_of(owner)
    ev["cert"] = other.cert(scopes=("look", "decide"))
    ev["device"] = other.device
    m["device"] = other.device
    with pytest.raises(pd.PairDeviceInvalid) as e:
        pd.check_material(m, ev, workspace_id=WS, person=f.person)
    assert e.value.reason == "cert_digest"


def test_the_pure_module_reads_no_clock_and_holds_no_key():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pd))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"time", "sign", "generate_private_key", "open", "environ"}


def test_facts_repr_carries_no_code():
    f = facts(code="QQQ234")
    assert "QQQ234" not in repr(f)  # the comparison code stays out of logs and tracebacks
    assert copy.deepcopy(f) == f


def test_crypto_device_id_matches_the_certificate_device():
    owner = Person()
    phone = phone_of(owner)
    f = facts(owner, phone)
    assert f.device == "d_" + crypto.device_id(phone.sig_pub).hex()
