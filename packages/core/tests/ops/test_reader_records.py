"""H12d-2: reader records, their levels and the one check at the append choke point (orch-v2-host §0.8.6 item 9.4,
9.9, 9.10). Unit level: a replayed workspace is stood in for by the model's own ``Device``/``Member`` types."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from orch import crypto
from orch.identity import reader
from orch.model.types import Device, Member
from orch.ops import readers
from orch.store import readerrecs
from tests.identity.helpers import Person
from tests.identity.reader_helpers import decl, malleate

WS = "705d40abbb8c1c90354a1acaa94c935c"
NOW = 1_790_000_000_000
UID = os.getuid()


def _ws(*people: Person):
    devices, members = {}, {}
    for p in people:
        devices[p.device] = Device(p.device, p.ref, p.cert())
        members[p.ref] = Member(p.ref, "x", "member", crypto.b64u(p.pk_pub))
    ws = SimpleNamespace(devices=devices, members=members, former={})
    return ws


def _state(ws):
    """A stand-in for the model's ``State``: the workspace view and ``device_evidence``."""
    return SimpleNamespace(
        workspace=ws,
        device_evidence=lambda d: {
            "device_cert": ws.devices[d].cert,
            "pk_pub": ws.members[ws.devices[d].person].pk_pub,
        },
    )


def _admit(state, p: Person, *, level=1, created_ms=NOW, via="declare", source="declare", surfaces=("log",)):
    d = decl(p, WS, level=level, via=via, created_ms=created_ms, surfaces=surfaces)
    got = readers.verify_admission(
        d, workspace_id=WS, cert=p.cert(), person=p.ref, via=via, surfaces=surfaces, now_ms=created_ms
    )
    rec = readers.make_record(got, d, p.ref, source, "2026-10-15T00:00:00Z")
    return readers.store_record(state, WS, UID, rec, incumbent_ok=_verified(p)), d


def _verified(p: Person):
    """``incumbent_ok`` for ``p``'s device: the stored record verifies against it now."""
    st = _state(_ws(p))
    dev = st.workspace.devices[p.device]
    return lambda old: readers.record_level(old, WS, dev, st.device_evidence(p.device)) is not None


def test_a_stored_declaration_counts_at_its_level_and_nothing_else_counts(tmp_path):
    a, b = Person(), Person()
    ws = _ws(a, b)
    rec, d = _admit(tmp_path, a)
    assert rec["digest"] == reader.decl_digest(d) and rec["reader"] == 1
    assert oct(os.stat(readerrecs.record_path(tmp_path, WS, a.device[2:])).st_mode & 0o777) == "0o600"
    assert readers.levels(tmp_path, WS, _state(ws)) == {a.device: 1, b.device: None}


def test_a_record_counts_only_for_its_exact_device_pair(tmp_path):
    a = Person()
    _admit(tmp_path, a)
    ws = _ws(a)
    # another person's certificate for the same keys (F1 lets anyone certify any key): inherits nothing
    other = Person()
    ws.devices[a.device] = Device(a.device, other.ref, a.cert())
    assert readers.levels(tmp_path, WS, _state(ws))[a.device] is None
    # removed and revoked devices are no readers at all
    ws = _ws(a)
    ws.devices[a.device].removed = True
    assert readers.levels(tmp_path, WS, _state(ws)) == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(reader=7),
        lambda r: r.update(digest="sha256:" + "0" * 64),
        lambda r: r.update(source="pair"),
        lambda r: r.update(extra=1),
        lambda r: r["decl"]["o"].update(reader=0),  # the signature no longer covers it
        lambda r: r.update(person_id="1" * 32),
    ],
)
def test_a_tampered_record_counts_as_no_declaration(tmp_path, change):
    a = Person()
    rec, _ = _admit(tmp_path, a)
    change(rec)
    readerrecs.write_record(tmp_path, WS, UID, rec)
    assert readers.levels(tmp_path, WS, _state(_ws(a)))[a.device] is None


def test_an_unsafe_record_file_counts_as_no_declaration(tmp_path):
    a = Person()
    _admit(tmp_path, a)
    path = readerrecs.record_path(tmp_path, WS, a.device[2:])
    os.chmod(path, 0o644)
    assert readers.levels(tmp_path, WS, _state(_ws(a)))[a.device] is None
    os.chmod(path, 0o600)
    target = path.with_suffix(".real")
    os.rename(path, target)
    os.symlink(target, path)
    assert readers.levels(tmp_path, WS, _state(_ws(a)))[a.device] is None


def test_the_same_declaration_with_another_signature_keeps_the_stored_record(tmp_path):
    """I1: records compare declarations on sha256(cj(o)), never on rd (ECDSA is malleable)."""
    a = Person()
    rec, d = _admit(tmp_path, a)
    twin = malleate(d)
    assert twin["sig"] != d["sig"] and reader.decl_digest(twin) != rec["digest"]
    got = readers.verify_admission(
        twin, workspace_id=WS, cert=a.cert(), person=a.ref, via="declare", surfaces=("log",), now_ms=NOW
    )
    kept = readers.store_record(
        tmp_path, WS, UID, readers.make_record(got, twin, a.ref, "declare", "later"), incumbent_ok=_verified(a)
    )
    assert kept == rec and readerrecs.read_record(tmp_path, WS, a.device[2:]) == rec


def test_latest_wins_by_created_ms_and_a_lower_declaration_lowers_the_level(tmp_path):
    a = Person()
    ws = _ws(a)
    _admit(tmp_path, a, created_ms=NOW)
    older, _ = _admit(tmp_path, a, level=0, created_ms=NOW - 1000)  # loses: the stored one is later
    assert older["reader"] == 1 and readers.levels(tmp_path, WS, _state(ws))[a.device] == 1
    newer, _ = _admit(tmp_path, a, level=0, created_ms=NOW + 1000)  # a withdrawal
    assert newer["reader"] == 0 and readers.levels(tmp_path, WS, _state(ws))[a.device] == 0


def test_a_higher_unknown_level_counts_as_the_highest_known_one(tmp_path):
    a = Person()
    rec, _ = _admit(tmp_path, a, level=7)
    assert rec["reader"] == 7 and readers.levels(tmp_path, WS, _state(_ws(a)))[a.device] == reader.HOST_MAX


def test_prune_drops_what_does_not_verify_against_the_log(tmp_path):
    a, b = Person(), Person()
    _admit(tmp_path, a)
    _admit(tmp_path, b)
    ws = _ws(a)  # b is not a current device here
    out = readers.prune(tmp_path, WS, _state(ws))
    assert out == {"kept": [a.device], "dropped": [b.device]}
    assert readerrecs.device_ids(tmp_path, WS) == [a.device[2:]]


# --- check_readers -------------------------------------------------------------------------------------------------


def _member_added(p: Person) -> dict:
    return {"type": "member.added", "person": p.ref, "device_cert": p.cert(), "auth": "passphrase"}


def _conv(p: Person) -> dict:
    return {
        "type": "ticket.updated",
        "actor": {"kind": "person", "id": p.ref, "device": p.device},
        "auth": "convenience",
    }


def test_a_reader_below_the_required_level_is_refused_with_its_reason():
    owner, new = Person(), Person()
    ws = _ws(owner)
    for lv, reason in (({}, "no_declaration"), ({new.device: 0}, "reader_too_old")):
        with pytest.raises(readers.ReadersRefused) as info:
            readers.check_readers(ws, lv, [_member_added(new)], required=1, relay_level=None)
        e = info.value
        assert (e.code, e.reason, e.devices) == ("sign.convenience_readers", reason, [new.device])
        assert e.required == 1 and e.declared == 0
    readers.check_readers(ws, {new.device: 1}, [_member_added(new)], required=1, relay_level=None)
    readers.check_readers(ws, {}, [_member_added(new)], required=0, relay_level=None)  # no convenience: optional


def test_a_convenience_signature_names_exactly_the_readers_below_level_1():
    me, phone, other = Person(), Person(), Person()
    ws = _ws(me, other)
    ws.devices[phone.device] = Device(phone.device, me.ref, phone.cert())  # another device of mine
    all_ok = {me.device: None, phone.device: 1, other.device: 1}
    readers.check_readers(ws, all_ok, [_conv(me)], required=0, relay_level=None)  # the signer itself never counts
    cases = [
        ({**all_ok, other.device: 0}, None, "other_member", [other.device]),
        ({**all_ok, phone.device: None}, None, "phone_device", [phone.device]),
        (all_ok, 0, "relay_paired", []),
    ]
    for lv, relay, reason, devices in cases:
        with pytest.raises(readers.ReadersRefused) as info:
            readers.check_readers(ws, lv, [_conv(me)], required=0, relay_level=relay)
        assert (info.value.code, info.value.reason, info.value.devices) == (
            "sign.convenience_unavailable",
            reason,
            devices,
        )
    readers.check_readers(ws, all_ok, [_conv(me)], required=1, relay_level=1)


def test_a_restore_counts_every_current_device_as_added():
    a, b = Person(), Person()
    ws = _ws(a, b)
    with pytest.raises(readers.ReadersRefused):
        readers.check_readers(ws, {a.device: 1, b.device: 0}, [{"type": "restore"}], required=1, relay_level=None)
    readers.check_readers(ws, {a.device: 1, b.device: 1}, [{"type": "restore"}], required=1, relay_level=None)


# --- #513 review: malformed records never crash a read ---------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(source=[]),
        lambda r: r.update(source={}),
        lambda r: r.update(device_id=["x"]),
        lambda r: r.update(person_id={"a": 1}),
        lambda r: r.update(reader="1"),
        lambda r: r.update(reader=True),
        lambda r: r.update(digest=5),
        lambda r: r.update(dk_hash=None),
        lambda r: r.update(decl=[]),
        lambda r: r.update(decl={"o": [], "sig": "x"}),
        lambda r: r["decl"].update(o={"who": [], "bind": {}}),
        lambda r: r.update(at=1),
        lambda r: r.update(v="1"),
    ],
)
def test_a_record_with_any_field_of_the_wrong_type_is_level_0_never_an_error(tmp_path, change):
    a, b = Person(), Person()
    rec, _ = _admit(tmp_path, a)
    _admit(tmp_path, b)
    change(rec)
    readerrecs.record_path(tmp_path, WS, a.device[2:]).write_text(json.dumps(rec))  # any bytes, as found on disk
    ws = _ws(a, b)
    assert readers.levels(tmp_path, WS, _state(ws)) == {a.device: None, b.device: 1}
    assert readers.prune(tmp_path, WS, _state(ws)) == {"kept": [b.device], "dropped": [a.device]}  # keeps going


def test_a_record_that_is_not_an_object_is_level_0(tmp_path):
    a = Person()
    _admit(tmp_path, a)
    path = readerrecs.record_path(tmp_path, WS, a.device[2:])
    for raw in (b"[]", b"7", b'"x"', b"null"):
        path.write_bytes(raw)
        assert readers.levels(tmp_path, WS, _state(_ws(a))) == {a.device: None}


def test_hub_left_by_convenience_waives_only_the_relay_reason():
    me, other = Person(), Person()
    ws = _ws(me, other)
    left = {**_conv(me), "type": "hub.left"}
    ok = {me.device: None, other.device: 1}
    readers.check_readers(ws, ok, [left], required=0, relay_level=0)  # relay_paired alone: leaving passes
    with pytest.raises(readers.ReadersRefused) as info:  # the same relay refuses any other convenience event
        readers.check_readers(ws, ok, [_conv(me)], required=0, relay_level=0)
    assert info.value.reason == "relay_paired"
    with pytest.raises(readers.ReadersRefused) as info:  # another member's old reader still refuses leaving
        readers.check_readers(ws, {**ok, other.device: 0}, [left], required=0, relay_level=0)
    assert info.value.reason == "other_member"
