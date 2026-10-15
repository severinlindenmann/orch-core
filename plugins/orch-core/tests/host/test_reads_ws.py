"""H6b, the workspace reads (§0.8, §0.8.5): ``workspace.show``, ``member.list``, ``session.list``, ``grant.list``
with the ``read_ext/2`` fields, and the host session records they read (:mod:`orch.host.sessions`)."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from orch import canon, crypto
from orch.host import sessions
from orch.host.reads import iso
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, world
from tests.host.live_helpers import ok as ok_
from tests.host.test_dispatch_matrix import as_agent_class, wrong_secret
from tests.store.helpers import GRANT_SECRET


def ok(res):
    return ok_(res)["data"]


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def wq(tmp_path):
    """The same workspace with the test UID mapped to quinn, a plain member (a person-class peer, not an owner)."""
    s = world(tmp_path, map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def touch(c, grant: str, session: str, n: int = 1) -> None:
    """``n`` column-G requests of ``session`` under ``grant`` (any op records the session)."""
    for _ in range(n):
        ok(c.req("whoami", {}, grant=grant, session=session))


def sid(name: str | int) -> str:
    """A session id (``s_<ULID>``): an integer keeps its order, a name is a stable digest."""
    n = name if isinstance(name, int) else int(hashlib.sha1(name.encode()).hexdigest(), 16) % 10**20
    return f"s_01J9ZP{n:020d}"


def grant_id(grant: str) -> str:
    return grant.partition(".")[0]


# --- workspace.show --------------------------------------------------------------------------------------------------


def test_workspace_show_gives_the_identity_of_the_workspace(w):
    c = w.client()
    data = ok(c.req("workspace.show"))
    ws = w.service.store.state.workspace
    assert set(data) == {
        "id", "prefix", "name", "genesis", "epoch", "wsk_fingerprint", "members_v", "state", "next_key",
        "required_reader",  # H12d-2
    }  # fmt: skip
    assert data["id"] == w.wid == ws.workspace_id and data["prefix"] == ws.prefix == "DEMO"
    assert data["name"] == "Acme"  # config.json `workspace.name`
    assert data["genesis"] == ws.genesis and data["genesis"].startswith("sha256:")
    assert data["members_v"] == ws.roster_v == 2  # the owner and quinn
    assert data["state"] == "ok"
    assert data["epoch"] == 0  # P2 has no committed membership epoch yet (§11 is P3): documented in reads_ws
    assert data["wsk_fingerprint"] == "sha256:" + hashlib.sha256(w.lab.ws.wsk_pub).hexdigest()[:32]


def test_workspace_show_follows_the_roster_version_and_is_open_to_g_and_n(w):
    c = w.client()
    before = ok(c.req("workspace.show"))["members_v"]
    w.person_event("role.changed", person=w.quinn.ref, role="maintainer")
    assert ok(c.req("workspace.show"))["members_v"] == before + 1
    as_p = ok(c.req("workspace.show"))
    as_g = ok(c.req("workspace.show", grant=w.grant, session=SESSION))
    assert as_g == {**as_p, "next_key": None}  # next_key is the persons' (H12e, judgement call: G is null)
    as_agent_class(w)
    assert ok(c.req("workspace.show"))["id"] == w.wid  # no-person may read it (§0.5)
    assert ok(c.req("workspace.show"))["next_key"] is None
    assert ok(c.req("workspace.show", grant=wrong_secret(w.grant)))["prefix"] == "DEMO"


def test_workspace_show_next_key_is_the_stores_exact_next_key_for_a_person_who_may_create(w):
    c = w.client()
    store = w.service.store
    assert ok(c.req("workspace.show"))["next_key"] == w.h.run(w.service.call(store.peek_key)) == "DEMO-0001"
    w.ticket("one")
    assert ok(c.req("workspace.show"))["next_key"] == "DEMO-0002"


def test_workspace_show_next_key_is_null_for_a_viewer_and_set_for_a_member(wq):
    c = wq.client()
    assert ok(c.req("workspace.show"))["next_key"] == "DEMO-0001"  # quinn, a member, may create
    wq.person_event("role.changed", person=wq.quinn.ref, role="viewer")
    assert ok(c.req("workspace.show"))["next_key"] is None  # a viewer may not create (§11A.14 14.6)


def test_next_key_for_column_r_needs_a_non_viewer_and_a_decide_device():
    from types import SimpleNamespace

    from orch.host.reads_ws import may_create

    dev = SimpleNamespace(person="p_1", removed=False, revoked=False, expires_ms=None, scopes=("look", "decide"))
    look = SimpleNamespace(**{**dev.__dict__, "scopes": ("look",)})
    relay = SimpleNamespace(device_id="d_1")
    ws = SimpleNamespace(devices={"d_1": dev}, members={"p_1": SimpleNamespace(role="member")})
    assert may_create("R", "member", relay, ws, 0) is True
    assert may_create("R", "viewer", relay, ws, 0) is False
    assert may_create("R", "member", relay, SimpleNamespace(**{**ws.__dict__, "devices": {"d_1": look}}), 0) is False
    assert may_create("R", "member", SimpleNamespace(device_id="d_2"), ws, 0) is False
    assert may_create("R", "member", SimpleNamespace(device_id="d_1", person_id="p_2"), ws, 0) is False
    assert may_create("P", "owner", None, ws, 0) is True and may_create("P", "viewer", None, ws, 0) is False
    assert may_create("P", None, None, ws, 0) is False
    assert may_create("G", "owner", None, ws, 0) is False and may_create("N", None, None, ws, 0) is False


# --- member.list -----------------------------------------------------------------------------------------------------


def test_member_list_has_the_devices_with_their_validity(w):
    c = w.client()
    data = ok(c.req("member.list"))
    by_person = {m["person"]: m for m in data["members"]}
    assert set(by_person) == {w.lab.owner, w.quinn.ref}
    owner, quinn = by_person[w.lab.owner], by_person[w.quinn.ref]
    assert (owner["role"], owner["name"], quinn["role"]) == ("owner", "Owner", "member")
    (device,) = owner["devices"]
    assert set(device) == {"id", "auth", "scopes", "valid", *DEVICE_EXT} and device["valid"] is True
    assert device["reader"] is None  # H12d-2: no reader record (#505: null, never 0)
    assert device["auth"] == "passphrase"  # the genesis was signed with it: the custody label F1 records
    assert sorted(device["scopes"]) == ["decide", "look", "operate", "type"]
    assert quinn["devices"][0]["auth"] is None  # signed nothing in the workspace log: unknown, not guessed
    assert set(owner) == {"person", "name", "role", "devices"}


def test_member_list_marks_a_revoked_device_invalid_and_is_open_to_g_and_n(w):
    w.lab.revoke_device(w.quinn)
    c = w.client()
    quinn = next(m for m in ok(c.req("member.list"))["members"] if m["person"] == w.quinn.ref)
    assert [d["valid"] for d in quinn["devices"]] == [False]
    assert ok(c.req("member.list", grant=w.grant, session=SESSION)) == ok(c.req("member.list"))
    as_agent_class(w)
    assert len(ok(c.req("member.list"))["members"]) == 2  # N reads it too (§0.5)


#: the v1.9 ``read_ext/3`` device fields (§0.8.5)
DEVICE_EXT = {"label", "paired_at", "relay", "cert_expires_at", "reader"}
PAIRED_MS = 1_760_000_123_456  # a relay record's paired_ms: served as Time, whole seconds


def test_member_list_device_fields_without_r6_or_reader_records(w):
    """read_ext/3 (v1.9): ``cert_expires_at`` is the F1 certificate's expiry as ``Time``; the R6 relay record and the
    H12d reader record are not built, so ``label``, ``paired_at`` and ``reader`` are null and ``relay`` is false."""
    c = w.client()
    assert "read_ext/3" in c.welcome["caps"] and "read_ext/2" in c.welcome["caps"]
    ws = w.service.store.state.workspace
    for member in ok(c.req("member.list"))["members"]:
        for device in member["devices"]:
            assert ws.devices[device["id"]].expires_ms is None and device["cert_expires_at"] is None  # never expires
            assert (device["label"], device["paired_at"], device["reader"]) == (None, None, None)
            assert device["relay"] is False
    expires_ms = (w.lab.ws.clock[0] + 3600) * 1000 + 999
    store = w.lab.ws.other()
    w.lab.ws.store = store
    try:
        ref, _ = w.lab.ws.add_device(expires_ms=expires_ms)
    finally:
        store.close()
        w.lab.ws.store = None
    owner = next(m for m in ok(c.req("member.list"))["members"] if m["person"] == w.lab.owner)
    added = next(d for d in owner["devices"] if d["id"] == ref)
    assert added["cert_expires_at"] == iso(expires_ms // 1000) == iso(w.lab.ws.clock[0] + 3600)
    assert (added["valid"], added["relay"]) == (True, False)


@pytest.mark.parametrize(
    ("ms", "want"),
    [
        (1_760_000_123_456, "2025-10-09T08:55:23Z"),  # a normal value
        (0, "1970-01-01T00:00:00Z"),
        (253402300799999, "9999-12-31T23:59:59Z"),  # the last RFC 3339 second
        (253402300800000, None),  # year 10000: no RFC 3339 Time
        (10**30, None),
        (-1, None),  # before 1970
        (-62135596800000, None),  # year 1
        (True, None),
        (1.5, None),
        ("7", None),
        (None, None),
    ],
)
def test_ms_time_answers_null_outside_the_rfc3339_range(ms, want):
    from orch.host.reads_ws import _ms_time

    assert _ms_time(ms) == want


def test_member_list_survives_a_cert_that_expires_in_year_10000(w, monkeypatch):
    """A verified certificate may carry ``expires_ms`` in year 10000: ``cert_expires_at`` is null, the device is still
    listed and valid, and the read does not fail. A relay record's out-of-range ``paired_ms`` is null the same way."""
    from orch.host import reads_ws

    store = w.lab.ws.other()
    w.lab.ws.store = store
    try:
        ref, _ = w.lab.ws.add_device(expires_ms=253402300800000)
    finally:
        store.close()
        w.lab.ws.store = None
    record = {"device_id": ref, "person_id": w.lab.owner, "label": "Phone", "paired_ms": -86_400_000}
    monkeypatch.setattr(reads_ws, "relay_devices", lambda ctx: {ref: record})
    c = w.client()
    owner = next(m for m in ok(c.req("member.list"))["members"] if m["person"] == w.lab.owner)
    added = next(d for d in owner["devices"] if d["id"] == ref)
    assert (added["cert_expires_at"], added["paired_at"], added["valid"], added["relay"]) == (None, None, True, True)


def _owner_dev(w) -> str:
    return next(d.id for d in w.service.store.state.workspace.devices.values() if d.person == w.lab.owner)


def test_member_list_maps_a_relay_record_and_a_reader_record(w, monkeypatch):
    """The seams R6 and H12d fill: a relay record gives the cleaned label (protocol §2.5), ``paired_at`` and ``relay``;
    a reader record gives the level. A record naming another person gives nothing; a label that cleans to nothing is
    null, and so is a ``paired_ms`` that is not a non-negative integer."""
    from orch.host import reads_ws

    quinn_dev = next(d.id for d in w.service.store.state.workspace.devices.values() if d.person == w.quinn.ref)
    owner_dev = _owner_dev(w)
    records = {
        owner_dev: {
            "device_id": owner_dev,
            "person_id": w.lab.owner,
            "label": "Phone\u200b\x07 one",
            "paired_ms": PAIRED_MS,
        },
        quinn_dev: {"device_id": quinn_dev, "person_id": w.lab.owner, "label": "not quinn's", "paired_ms": 9},
    }
    monkeypatch.setattr(reads_ws, "relay_devices", lambda ctx: records)
    monkeypatch.setattr(reads_ws, "reader_levels", lambda ctx: {owner_dev: 1})
    c = w.client()
    by_person = {m["person"]: m for m in ok(c.req("member.list"))["members"]}
    (mine,) = by_person[w.lab.owner]["devices"]
    assert (mine["label"], mine["relay"], mine["reader"]) == ("Phone one", True, 1)
    assert mine["paired_at"] == "2025-10-09T08:55:23Z" == iso(PAIRED_MS // 1000)
    (theirs,) = by_person[w.quinn.ref]["devices"]
    assert (theirs["label"], theirs["paired_at"], theirs["relay"], theirs["reader"]) == (None, None, False, None)
    for bad in (-1, 1.5, "7", True):
        records[owner_dev] = {**records[owner_dev], "label": "\u200b\u2060", "paired_ms": bad}
        (mine,) = {m["person"]: m for m in ok(c.req("member.list"))["members"]}[w.lab.owner]["devices"]
        assert (mine["label"], mine["paired_at"], mine["relay"]) == (None, None, True), bad


def test_member_list_label_is_for_persons_only_g_and_n_get_null(w, monkeypatch):
    """orch-52 (#505): only a person (P) sees device labels; G (an agent grant) and N (no-person) get ``label: null``
    and everything else exactly as P, with the same members and devices (the viewer filter is otherwise unchanged)."""
    from orch.host import reads_ws

    owner_dev = _owner_dev(w)
    record = {"device_id": owner_dev, "person_id": w.lab.owner, "label": "Anna's phone", "paired_ms": PAIRED_MS}
    monkeypatch.setattr(reads_ws, "relay_devices", lambda ctx: {owner_dev: record})
    c = w.client()

    def labels(data):
        return {d["id"]: d["label"] for m in data["members"] for d in m["devices"]}

    def without_labels(data):
        return [{**m, "devices": [{**d, "label": None} for d in m["devices"]]} for m in data["members"]]

    as_p = ok(c.req("member.list"))
    assert labels(as_p)[owner_dev] == "Anna's phone"
    assert all(set(d) == {"id", "auth", "scopes", "valid", *DEVICE_EXT} for m in as_p["members"] for d in m["devices"])
    as_g = ok(c.req("member.list", grant=w.grant, session=SESSION))
    assert set(labels(as_g).values()) == {None}
    assert without_labels(as_g) == without_labels(as_p)  # paired_at, relay, cert_expires_at, reader: as P
    as_agent_class(w)
    as_n = ok(c.req("member.list"))
    assert set(labels(as_n).values()) == {None}
    assert without_labels(as_n) == without_labels(as_p)
    assert labels(as_n).keys() == labels(as_p).keys()  # the same devices


# --- grant.list ------------------------------------------------------------------------------------------------------


def test_grant_list_never_returns_a_secret_hash(w):
    w.owner_grant()
    qgrant = w.grant_for(w.quinn)
    c = w.client()
    touch(c, w.grant, sid("s1"))
    text = json.dumps(ok(c.req("grant.list", {"active": False})))
    secret_hash = canon.grant_secret_hash(GRANT_SECRET)
    for needle in ("secret_hash", secret_hash, secret_hash.partition(":")[2], crypto.b64u(GRANT_SECRET), "sig"):
        assert needle not in text, needle
    assert grant_id(qgrant) in text  # the grants themselves are listed, by their public id


def test_grant_list_fields(w):
    c = w.client()
    (grant,) = ok(c.req("grant.list"))["grants"]
    assert set(grant) == {
        "id", "person", "scope", "verbs", "label", "issued_at", "expires_at", "revoked_at", "sessions", "session_ids",
    }  # fmt: skip
    assert isinstance(grant["label"], str)  # the grant.issued label (null when the grant has none)
    assert grant["id"] == grant_id(w.grant) and grant["person"] == w.lab.owner
    assert grant["scope"] == "all" and grant["verbs"] == "agent" and grant["revoked_at"] is None
    assert grant["issued_at"] < grant["expires_at"] and grant["sessions"] == 0 and grant["session_ids"] == []
    assert "revoked" not in grant and "sessions_more" not in grant


def test_grant_list_revoked_names_the_signer_of_the_grant_revoked(w):
    own = w.owner_grant()
    qgrant = w.grant_for(w.quinn)
    qown = w.grant_for(w.quinn)
    owner_revokes = w.person_event("grant.revoked", grant=grant_id(qgrant), reason="private reason text")
    quinn_revokes = w.person_event("grant.revoked", who=w.quinn, grant=grant_id(qown), reason="again")
    c = w.client()
    assert {g["id"] for g in ok(c.req("grant.list"))["grants"]} == {grant_id(w.grant), own}  # active: not revoked
    by_id = {g["id"]: g for g in ok(c.req("grant.list", {"active": False}))["grants"]}
    assert set(by_id) == {grant_id(w.grant), own, grant_id(qgrant), grant_id(qown)}
    assert by_id[grant_id(qgrant)]["revoked"] == {
        "by": w.lab.owner,
        "at": owner_revokes["at"],
        "seq": owner_revokes["seq"],
    }
    assert by_id[grant_id(qown)]["revoked"] == {
        "by": w.quinn.ref,
        "at": quinn_revokes["at"],
        "seq": quinn_revokes["seq"],
    }
    assert by_id[grant_id(qgrant)]["revoked_at"] == owner_revokes["at"]
    assert "revoked" not in by_id[own] and by_id[own]["revoked_at"] is None
    assert "private reason text" not in json.dumps(by_id) and "reason" not in json.dumps(by_id)


def test_grant_list_a_grant_that_ended_otherwise_has_no_revoked(w):
    qgrant = w.grant_for(w.quinn)
    w.lab.remove_member(w.quinn)  # ends quinn's grants without a grant.revoked
    c = w.client()
    by_id = {g["id"]: g for g in ok(c.req("grant.list", {"active": False}))["grants"]}
    ended = by_id[grant_id(qgrant)]
    assert "revoked" not in ended and ended["revoked_at"] is not None  # revoked_at: the removal's time
    assert grant_id(qgrant) not in {g["id"] for g in ok(c.req("grant.list"))["grants"]}


def test_grant_list_revoked_at_is_never_before_issued_at_after_a_remove_and_re_add(w):
    """A person removed, re-added and then issued a grant: the earlier removal does not end the new grant."""
    q = w.quinn
    w.lab.ws.tick()
    w.person_event("member.removed", person=q.ref)
    w.lab.ws.tick(5)
    q.dk_sig, q.dk_kx = crypto.generate_private_key(), crypto.generate_private_key()  # a removed device id is final
    q.sig_pub, q.kx_pub = crypto.public_bytes(q.dk_sig), crypto.public_bytes(q.dk_kx)
    cert = q.cert(created=w.lab.ws.clock[0] * 1000)
    w.person_event(
        "member.added", person=q.ref, name="quinn", role="member", pk_pub=crypto.b64u(q.pk_pub), device_cert=cert
    )
    w.lab.ws.tick(5)
    gid = grant_id(w.grant_for(q))
    c = w.client()

    def entry():
        return {g["id"]: g for g in ok(c.req("grant.list", {"active": False}))["grants"]}[gid]

    assert entry()["revoked_at"] is None and gid in {g["id"] for g in ok(c.req("grant.list"))["grants"]}
    w.lab.ws.tick(5)
    second = w.person_event("member.removed", person=q.ref)
    got = entry()
    assert got["revoked_at"] == second["at"] and got["revoked_at"] >= got["issued_at"]
    assert "revoked" not in got


def test_grant_list_active_means_not_revoked_and_not_expired(w):
    short = w.grant_for(w.quinn, hours=1)
    c = w.client()
    assert grant_id(short) in {g["id"] for g in ok(c.req("grant.list"))["grants"]}
    w.lab.ws.clock[0] += 2 * 3600  # past its hour, inside the owner's eight
    assert {g["id"] for g in ok(c.req("grant.list"))["grants"]} == {grant_id(w.grant)}
    assert grant_id(short) in {g["id"] for g in ok(c.req("grant.list", {"active": False}))["grants"]}


# --- who sees which grants and sessions ------------------------------------------------------------------------------


def two_people(w):
    qgrant = w.grant_for(w.quinn)
    c = w.client()
    touch(c, w.grant, sid("own-1"), 2)
    touch(c, w.grant, sid("own-2"))
    touch(c, qgrant, sid("quinn-1"))
    return qgrant, c


def test_an_owner_sees_every_grants_sessions(w):
    qgrant, c = two_people(w)
    grants = {g["id"]: g for g in ok(c.req("grant.list"))["grants"]}
    assert set(grants) == {grant_id(w.grant), grant_id(qgrant)}
    assert grants[grant_id(w.grant)]["sessions"] == 2 and sorted(grants[grant_id(w.grant)]["session_ids"]) == sorted(
        [sid("own-1"), sid("own-2")]
    )
    assert grants[grant_id(qgrant)]["session_ids"] == [sid("quinn-1")]
    assert {s["session"] for s in ok(c.req("session.list"))["sessions"]} == {sid("own-1"), sid("own-2"), sid("quinn-1")}


def test_another_person_sees_their_own_grants_and_sessions_only(wq):
    qgrant, c = two_people(wq)
    wq.grant_for(wq.quinn)  # a second grant of quinn's
    seen = ok(c.req("grant.list"))["grants"]
    assert {g["person"] for g in seen} == {wq.quinn.ref} and len(seen) == 2
    assert sum(g["sessions"] for g in seen) == 1
    sessions_ = ok(c.req("session.list"))["sessions"]
    assert [s["session"] for s in sessions_] == [sid("quinn-1")] and sessions_[0]["person"] == wq.quinn.ref
    assert sid("own-1") not in json.dumps(ok(c.req("grant.list", {"active": False})))


def test_g_sees_its_own_persons_grants_and_sessions(w):
    qgrant, c = two_people(w)
    own = ok(c.req("grant.list", grant=w.grant, session=SESSION))["grants"]
    assert [g["id"] for g in own] == [grant_id(w.grant)]
    assert {s["session"] for s in ok(c.req("session.list", grant=w.grant, session=SESSION))["sessions"]} == {
        sid("own-1"),
        sid("own-2"),
        SESSION,
    }
    quinn_view = ok(c.req("grant.list", grant=qgrant, session=sid("quinn-1")))["grants"]
    assert [g["id"] for g in quinn_view] == [grant_id(qgrant)] and quinn_view[0]["session_ids"] == [sid("quinn-1")]
    assert [s["session"] for s in ok(c.req("session.list", grant=qgrant, session=sid("quinn-1")))["sessions"]] == [
        sid("quinn-1")
    ]


def test_no_person_is_refused_both_reads(w):
    two_people(w)
    c = w.client()
    as_agent_class(w)
    for op in ("grant.list", "session.list"):
        assert err(c.req(op)) == "op.not_allowed"
        assert err(c.req(op, grant=wrong_secret(w.grant), session=SESSION)) == "op.not_allowed"


# --- session ids and sessions_more -----------------------------------------------------------------------------------


@pytest.mark.slow
def test_session_ids_are_the_first_hundred_and_sessions_more_says_when_cut(w):
    c = w.client()
    for i in range(100):
        touch(c, w.grant, sid(i))
    (grant,) = ok(c.req("grant.list"))["grants"]
    assert grant["sessions"] == 100 and len(grant["session_ids"]) == 100 and "sessions_more" not in grant
    touch(c, w.grant, sid(100))
    (grant,) = ok(c.req("grant.list"))["grants"]
    assert grant["sessions"] == 101 and len(grant["session_ids"]) == 100 and grant["sessions_more"] is True
    assert grant["session_ids"] == [sid(i) for i in range(100)]  # one last_at: session id ascending
    assert len(ok(c.req("session.list", {"active": False}))["sessions"]) == 101  # session.list is not capped


def test_session_ids_are_ordered_by_last_at_descending_then_id(w):
    c = w.client()
    for name in ("b", "a", "c"):
        touch(c, w.grant, sid(name))
    w.lab.ws.clock[0] += 5
    touch(c, w.grant, sid("z"))
    w.lab.ws.clock[0] += 5
    touch(c, w.grant, sid("b"))  # b moves to the front
    (grant,) = ok(c.req("grant.list"))["grants"]
    assert grant["session_ids"] == [sid(x) for x in ("b", "z", "a", "c")]
    assert [s["session"] for s in ok(c.req("session.list"))["sessions"]] == [sid(x) for x in ("b", "z", "a", "c")]


# --- the session records ---------------------------------------------------------------------------------------------


def test_a_session_record_has_the_agent_the_person_the_grant_and_the_last_request(w):
    c = w.client()
    touch(c, w.grant, sid("rec-1"))
    t0 = w.lab.ws.clock[0]
    w.lab.ws.clock[0] += 10
    touch(c, w.grant, sid("rec-1"))
    (row,) = [s for s in ok(c.req("session.list"))["sessions"] if s["session"] == sid("rec-1")]
    assert row == {
        "session": sid("rec-1"),
        "agent": "orch-cli",  # Call.agent_id() over the host's empty environment: what the events carry
        "person": w.lab.owner,
        "grant_id": grant_id(w.grant),
        "claims": [],
        "leases": [],
        "last_at": row["last_at"],
    }
    assert row["last_at"] == iso(t0 + 10)  # exact, the time of the latest request
    _, key = w.ticket()
    w.write(key)  # an agent event of the owner's session carries the same agent id as its record
    store = w.lab.ws.other()
    try:
        logged = [e for e in store.events(key) if e["type"] == "log.added"]
    finally:
        store.close()
    assert (
        logged
        and logged[-1]["actor"]["id"]
        == "orch-cli"
        == next(s for s in ok(c.req("session.list"))["sessions"] if s["session"] == SESSION)["agent"]
    )


def test_only_a_verified_grant_with_a_session_is_recorded(w):
    c = w.client()
    ok(c.req("whoami", {}, grant=w.grant))  # no session
    ok(c.req("whoami", {}, session=sid("p-only")))  # column P: the session is nobody's
    ok(c.req("whoami", {}, grant=wrong_secret(w.grant), session=sid("wrong")))  # an unverified grant is column N
    assert ok(c.req("session.list", {"active": False}))["sessions"] == []
    assert ok(c.req("grant.list"))["grants"][0]["sessions"] == 0


def test_the_records_are_written_throttled_0600_and_survive_a_restart(w):
    path = w.lab.state / "hosts" / w.wid / "sessions.json"
    c = w.client()
    touch(c, w.grant, sid("keep"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    first = json.loads(path.read_text())
    assert [r["session"] for r in first["sessions"]] == [sid("keep")]
    at = first["sessions"][0]["last_at"]
    w.lab.ws.clock[0] += 30
    touch(c, w.grant, sid("keep"))  # inside the throttle: not written ...
    assert json.loads(path.read_text())["sessions"][0]["last_at"] == at
    assert (
        ok(c.req("session.list"))["sessions"][0]["last_at"] > ok(c.req("grant.list"))["grants"][0]["issued_at"]
    )  # ... but exact in memory
    w.lab.ws.clock[0] += 31  # 61 s after the write
    touch(c, w.grant, sid("keep"))
    assert json.loads(path.read_text())["sessions"][0]["last_at"] == at + 61
    touch(c, w.grant, sid("other"))  # a new session waits for the throttle too (one rewrite per interval)
    assert [r["session"] for r in json.loads(path.read_text())["sessions"]] == [sid("keep")]
    w.lab.ws.clock[0] += 61
    touch(c, w.grant, sid("keep"))
    assert sorted(r["session"] for r in json.loads(path.read_text())["sessions"]) == sorted([sid("keep"), sid("other")])
    reread = sessions.Records(path, os.getuid())
    assert sorted(r.session for r in reread.snapshot(w.lab.ws.clock[0])) == sorted([sid("keep"), sid("other")])


def test_the_records_are_bounded_and_kept_seven_days(w, monkeypatch):
    path = w.lab.state / "hosts" / w.wid / "unit-sessions.json"
    now = 1_800_000_000.0
    recs = sessions.Records(path, os.getuid())
    monkeypatch.setattr(sessions, "MAX_RECORDS", 5)
    for i in range(8):
        recs.note(sid(i), "orch-cli", "p_" + "0" * 32, "gr_X", now + i)
    assert sorted(r.session for r in recs.snapshot(now + 8)) == [sid(i) for i in range(3, 8)]  # the oldest dropped
    assert sessions.RETENTION == 7 * 24 * 3600
    assert recs.snapshot(now + 7 + sessions.RETENTION - 1) != []
    assert [r.session for r in recs.snapshot(now + 7 + sessions.RETENTION)] == []  # nothing older than seven days
    again = sessions.Records(path, os.getuid())
    assert again.snapshot(now + 10 + 7 * 24 * 3600) == []  # and none is loaded from the file either


def test_a_failing_record_never_reaches_the_request(w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(sessions.Records, "note", boom)
    c = w.client()
    assert ok(c.req("whoami", {}, grant=w.grant, session=sid("x")))["grant"]["id"] == grant_id(w.grant)
    monkeypatch.undo()
    assert ok(c.req("session.list", {"active": False}))["sessions"] == []


# --- session.list: claims, leases, active ----------------------------------------------------------------------------


def agent(w, c, op, args, grant=None, session=SESSION):
    return ok(c.req(op, args, grant=grant or w.grant, session=session, idem=True))


def test_session_list_has_the_claims_and_leases_of_the_session(w):
    uid, key = w.ticket()
    c = w.client()
    agent(w, c, "claim", {"ref": key})
    agent(w, c, "task.add", {"ref": key, "text": "do it"})
    agent(w, c, "task.start", {"task": f"{key}/T1"})
    (row,) = [s for s in ok(c.req("session.list"))["sessions"] if s["session"] == SESSION]
    assert row["claims"] == [key] and row["leases"] == [f"{key}/T1"]


def test_session_list_leaves_out_a_claim_on_a_ticket_the_viewer_cannot_see(w):
    uid, key = w.ticket()
    _, other = w.ticket("another")
    qgrant = w.grant_for(w.quinn)
    c = w.client()
    agent(w, c, "claim", {"ref": key}, grant=qgrant, session=sid("quinn-s"))
    agent(w, c, "claim", {"ref": other, "also": True}, grant=qgrant, session=sid("quinn-s"))
    seen = lambda **kw: next(s for s in ok(c.req("session.list", **kw))["sessions"] if s["session"] == sid("quinn-s"))  # noqa: E731
    assert seen()["claims"] == sorted([key, other])
    w.restrict(uid, [w.quinn.ref])  # the owner is not on its list
    assert seen()["claims"] == [other]  # the owner's P view: the restricted ticket is left out, the session is not
    assert key not in json.dumps(ok(c.req("session.list", {"active": False})))
    assert seen(grant=qgrant, session=sid("quinn-s"))["claims"] == sorted([key, other])  # quinn's own G view has it


def test_session_list_active_follows_the_grant_the_claim_and_the_idle_time(w):
    uid, key = w.ticket()
    c = w.client()
    qgrant = w.grant_for(w.quinn, hours=8)
    touch(c, w.grant, sid("idle"))
    touch(c, qgrant, sid("holder"))
    agent(w, c, "claim", {"ref": key}, grant=qgrant, session=sid("holder"))
    # (SESSION made the ticket: left out of the comparison)
    names = lambda **kw: {s["session"] for s in ok(c.req("session.list", kw))["sessions"]} - {SESSION}  # noqa: E731
    assert names() == {sid("idle"), sid("holder")}
    w.lab.ws.clock[0] += 130 * 60  # past claim_ttl_min (120): both are idle ... and the claim lapsed with it
    assert names() == set()
    assert names(active=False) == {sid("idle"), sid("holder")}
    touch(c, w.grant, sid("idle"))  # a request is activity again
    assert names() == {sid("idle")}
    w.person_event("grant.revoked", grant=grant_id(w.grant))
    assert names() == set()  # its grant is no longer valid


def test_session_list_orders_by_last_at_descending(w):
    c = w.client()
    for name in ("old", "mid"):
        touch(c, w.grant, sid(name))
        w.lab.ws.clock[0] += 3
    touch(c, w.grant, sid("new"))
    assert [s["session"] for s in ok(c.req("session.list"))["sessions"]] == [sid(x) for x in ("new", "mid", "old")]
