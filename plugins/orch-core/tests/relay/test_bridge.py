"""R2: proto §8.6 in its normative order, the replay and ``rid_conflict`` rules, the seq window, the closed op table,
the idem rules, the scopes, the start fence, epochs and rotation, chunking and the chunk POST outcomes (11A.4, 11A.5).
No network: :mod:`tests.relay.bridgekit` fakes R6, the host and the relay's POST route."""

from __future__ import annotations

import json
import secrets

import pytest

from orch import crypto
from orch.relay import framing
from orch.relay.bridge import CHUNK_ROOM, Item, classify_post
from orch.relay.retry import RelayRefused
from orch.wsproto import wire

from .bridgekit import T0, Phone, World, open_chunk, ulid
from .helpers import WS


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def refusal(w: World, chunks: list[bytes]) -> dict:
    assert len(chunks) == 1, chunks
    h, meta, data = open_chunk(chunks[0], w.wk, w.key.public_key)
    assert h.flags & wire.FLAG_LAST and h.flags & wire.FLAG_REFUSAL and data == b""
    return meta


# -- the happy paths --------------------------------------------------------------------------------------------------


def test_a_read_is_served_as_the_devices_person_with_principal_r_device(w):
    meta, data = w.call({"op": "ticket.get", "args": {"ref": "DEMO-1"}})
    assert meta == {"status": "ok"}
    assert data["data"]["viewer"] == "p_" + w.phone.person
    assert data["data"]["principal"] == "r:d_" + w.phone.device
    assert w.host.reads == ["ticket.get"]


def test_the_answer_is_sealed_under_the_request_epoch_and_signed_by_the_wsk(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    (chunk,) = w.send(env)
    h, _, _ = open_chunk(chunk, w.wk, w.key.public_key)
    req = wire.parse_header(env[: wire.HEADER_LEN])
    assert (h.direction, h.epoch, h.rid, h.device, h.seq, h.ts_ms) == (2, req.epoch, req.rid, req.device, 0, T0)


def test_a_write_runs_once_and_a_new_rid_with_the_same_idem_gets_duplicate_true(w):
    m = w.new_ticket()
    meta, first = w.call(m)
    assert meta == {"status": "ok"} and first["duplicate"] is False
    meta, again = w.call(m)  # a new rid and seq, the same idem (layer 3)
    assert again["duplicate"] is True and again["data"] == first["data"]
    assert len(w.host.runs) == 1


def test_a_big_answer_is_chunked_under_256_kib_and_reassembles(w):
    size = 3 * CHUNK_ROOM
    chunks = w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": size}}))
    assert len(chunks) >= 4 and all(len(c) <= wire.MAX_CHUNK for c in chunks)
    flags = [wire.parse_header(c[: wire.HEADER_LEN]).flags for c in chunks]
    assert [bool(f & wire.FLAG_LAST) for f in flags] == [False] * (len(chunks) - 1) + [True]
    assert [wire.parse_header(c[: wire.HEADER_LEN]).seq for c in chunks] == list(range(len(chunks)))
    metas = [open_chunk(c, w.wk, w.key.public_key)[1] for c in chunks]
    assert metas[0] == {"status": "ok"} and all(m == {} for m in metas[1:])
    _, data = w.answer(chunks)
    assert len(data["data"]["text"]) == size


def test_an_answer_above_4_mib_is_frame_too_large(w):
    chunks = w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": framing.MAX_ANSWER + 1}}))
    assert refusal(w, chunks) == {"refusal": "frame.too_large", "retryable": False}


def test_a_host_refusal_is_sealed_with_its_code_only(w):
    chunks = w.send(w.phone.request(w.wk, {"op": "ticket.get", "args": {"ref": "missing"}}))
    assert refusal(w, chunks) == {"refusal": "not_found", "retryable": False}


# -- step 1 to 3: silence ---------------------------------------------------------------------------------------------


def _mutate(env: bytes, offset: int, value: bytes) -> bytes:
    return env[:offset] + value + env[offset + len(value) :]


@pytest.mark.parametrize(
    "why",
    ["short", "magic", "version", "direction", "flags", "suite", "workspace", "mailbox", "item_device", "huge"],
)
def test_shape_failures_are_dropped_silently_and_never_recorded(w, why):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    item_rid = wire.parse_header(env[: wire.HEADER_LEN]).rid.hex()
    device = None
    if why == "short":
        env = env[:100]
    elif why == "magic":
        env = _mutate(env, 0, b"ORB1")
    elif why == "version":
        env = _mutate(env, 4, b"\x01")
    elif why == "direction":
        env = _mutate(env, 5, b"\x02")
    elif why == "flags":
        env = _mutate(env, 6, b"\x01")
    elif why == "suite":
        env = _mutate(env, 7, b"\x01")
    elif why == "workspace":
        env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, ws="ab" * 16)
        item_rid = wire.parse_header(env[: wire.HEADER_LEN]).rid.hex()
    elif why == "mailbox":
        item_rid = "00" * 16
    elif why == "item_device":
        device = "11" * 16
    elif why == "huge":
        env = env + bytes(wire.MAX_REQUEST)
    item = Item(1, "bridge", device if device is not None else env[24:40].hex(), item_rid, env)
    assert w.bridge.receive(item) is None
    assert w.post.posts == [] and w.rids.dump()["rid"] == []


def test_a_future_epoch_a_deleted_key_a_wrong_key_and_epoch_0_are_dropped(w):
    keys = dict(w.wk)
    keys[3] = secrets.token_bytes(32)
    keys[1] = secrets.token_bytes(32)
    for env in (
        w.phone.request(keys, {"op": "whoami", "args": {}}, epoch=3),  # above current
        w.phone.request(keys, {"op": "whoami", "args": {}}, epoch=1),  # sealed under another key: tag
    ):
        assert w.send(env) == []
    del w.epochs.keys[1]  # a key the host deleted
    assert w.send(w.phone.request({1: keys[1]}, {"op": "whoami", "args": {}}, epoch=1)) == []
    pair = w.phone.request(
        {0: secrets.token_bytes(32)}, {"op": "pair"}, epoch=0, stream=secrets.token_bytes(16)
    )  # R6's; dropped until R6 exists
    assert w.send(pair) == []
    assert w.post.posts == [] and w.rids.dump()["rid"] == []


# -- step 4: the device -----------------------------------------------------------------------------------------------


def test_not_in_the_member_list_is_a_recorded_not_member_that_uses_no_seq(w):
    stranger = Phone()
    env = stranger.request(w.wk, {"op": "whoami", "args": {}})
    assert refusal(w, w.send(env)) == {"refusal": "not_member"}
    assert w.rids.window(stranger.device) == (0, 0)
    assert w.send(env) == w.post.for_rid(env[40:56])[:1]  # a resend replays the stored bytes


def test_a_signature_by_another_device_is_bad_signature(w):
    other = Phone()
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, signer=other.dk)
    assert refusal(w, w.send(env)) == {"refusal": "bad_signature"}


def test_held_revocation_evidence_for_the_pair_is_revoked(w):
    w.host.standings[("d_" + w.phone.device, "p_" + w.phone.person)] = "revoked"
    assert refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}))) == {"refusal": "revoked"}


def test_an_f1_state_without_this_exact_pair_is_not_member(w):
    w.host.standings[("d_" + w.phone.device, "p_" + w.phone.person)] = "not_member"
    assert refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}))) == {"refusal": "not_member"}


def test_an_expired_certificate_is_not_member(tmp_path):
    w = World(tmp_path)
    try:
        p = Phone(expires_ms=T0 - 1)
        w.r6.add(p)
        assert refusal(w, w.send(p.request(w.wk, {"op": "whoami", "args": {}}))) == {"refusal": "not_member"}
    finally:
        w.close()


def test_a_certificate_of_another_person_under_the_listed_person_is_not_member(w):
    imposter = Phone()
    m = w.phone.member()
    w.r6.members[imposter.device] = type(m)(imposter.device, w.phone.person, m.scopes, imposter.cert, w.phone.pk_pub)
    assert refusal(w, w.send(imposter.request(w.wk, {"op": "whoami", "args": {}}))) == {"refusal": "not_member"}


def test_the_store_unavailable_is_silence(w):
    w.host.standings[("d_" + w.phone.device, "p_" + w.phone.person)] = "unavailable"
    assert w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}})) == []
    assert w.rids.dump()["rid"] == []


def test_step_4_refusals_are_budgeted_at_10_per_minute_then_dropped(w):
    stranger = Phone()
    answered = [w.send(stranger.request(w.wk, {"op": "whoami", "args": {}})) for _ in range(12)]
    assert [len(a) for a in answered] == [1] * 10 + [0] * 2
    w.clock.ms += 60_000
    assert len(w.send(stranger.request(w.wk, {"op": "whoami", "args": {}}))) == 1


def test_a_device_revoked_after_its_answer_gets_silence_on_resend_not_the_answer(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    n = len(w.post.posts)
    w.host.standings[("d_" + w.phone.device, "p_" + w.phone.person)] = "revoked"
    assert w.send(env) == [] and len(w.post.posts) == n


# -- step 5: replay and rid_conflict ----------------------------------------------------------------------------------


def test_an_identical_resend_replays_the_exact_stored_bytes_and_never_runs_again(w):
    m = w.new_ticket()
    env = w.phone.request(w.wk, m)
    first = w.send(env)
    w.clock.ms += 30_000
    again = w.send(env)
    assert again == first
    assert len(w.host.runs) == 1


def test_a_resend_while_admitted_gets_no_answer(w):
    env = w.phone.request(w.wk, w.new_ticket())
    assert w.send(env, run=False) == []
    assert w.send(env) == []  # the resend: still admitted, no interim already_done
    assert w.host.runs == []


def test_rid_conflict_a_attempted_original_is_reposted_as_is(w):
    rid = secrets.token_bytes(16)
    first = w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, rid=rid))
    other = w.phone.request(w.wk, {"op": "needs", "args": {}}, rid=rid)
    assert w.send(other) == first


def test_rid_conflict_b_admitted_original_gets_silence(w):
    rid = secrets.token_bytes(16)
    w.send(w.phone.request(w.wk, w.new_ticket(), rid=rid), run=False)
    assert w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, rid=rid)) == []


def _unattempt(w):
    """Test setup: the state after a crash between T2 and the first ``attempted`` mark (nothing attempted yet)."""
    with w.rids._lock:  # noqa: SLF001
        w.rids._db.execute("UPDATE chunk SET state='stored'")  # noqa: SLF001


def test_rid_conflict_c_answered_unattempted_original_is_posted(w):
    rid = secrets.token_bytes(16)
    first = w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, rid=rid))
    _unattempt(w)
    got = w.send(w.phone.request(w.wk, {"op": "needs", "args": {}}, rid=rid))
    assert got == first and w.host.reads == ["whoami"]


def test_rid_conflict_d_without_the_original_key_is_a_sealed_rid_conflict_at_index_0(w):
    rid = secrets.token_bytes(16)
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, rid=rid, epoch=1, seq=1)
    w.bridge.receive(w.item(env))  # stale_epoch refusal, sealed under epoch 1, posted
    _unattempt(w)
    del w.epochs.keys[1]
    got = w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, rid=rid))
    assert refusal(w, got) == {"refusal": "rid_conflict"}
    assert wire.parse_header(got[0][: wire.HEADER_LEN]).epoch == 2


# -- 5b, 6 to 10 ------------------------------------------------------------------------------------------------------


def test_malformed_meta_is_recorded_and_uses_no_seq(w):
    for bad in (b"\x00\x00\x00\x02{}", b"\x00\x00\x00\x03[1]", b'\x00\x00\x00\x0b{"op":1.5}', b"\x00"):
        env = w.phone.request(w.wk, bad, seq=7)
        assert refusal(w, w.send(env)) == {"refusal": "malformed"}
    assert w.rids.window(w.phone.device) == (0, 0)
    assert w.call({"op": "whoami", "args": {}}, seq=7)[0] == {"status": "ok"}


def test_the_seq_window_refuses_a_used_seq_and_one_below_the_window(w):
    w.call({"op": "whoami", "args": {}}, seq=100)
    meta = refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=100)))
    assert meta == {"refusal": "stale_sequence", "high": 100}
    assert (
        refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=36)))["refusal"] == "stale_sequence"
    )
    assert w.call({"op": "whoami", "args": {}}, seq=37)[0] == {"status": "ok"}  # inside the window, unused
    zero = w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=0)
    assert refusal(w, w.send(zero))["refusal"] == "stale_sequence"


def test_a_stale_timestamp_consumes_the_seq(w):
    meta = refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=5, ts_ms=T0 - 300_001)))
    assert meta == {"refusal": "stale_timestamp", "host_ms": T0}
    assert refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, seq=5)))["refusal"] == "stale_sequence"
    assert w.call({"op": "whoami", "args": {}}, seq=6, ts_ms=T0 + 300_000)[0] == {"status": "ok"}  # the edge


def test_an_old_epoch_with_a_held_key_is_stale_epoch_sealed_under_that_old_key(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, epoch=1)
    (chunk,) = w.send(env)
    h, meta, _ = open_chunk(chunk, w.wk, w.key.public_key)
    assert h.epoch == 1 and meta == {"refusal": "stale_epoch", "epoch": 2}
    assert w.host.reads == []


def test_a_stream_is_forbidden_scope(w):
    for kw in ({"flags": wire.FLAG_STREAM}, {"stream": secrets.token_bytes(16)}):
        assert refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, **kw))) == {
            "refusal": "forbidden_scope"
        }


def test_busy_above_the_per_device_quota(w, monkeypatch):
    from orch.relay import bridge

    monkeypatch.setattr(bridge, "PER_DEVICE", 2)
    w.call({"op": "whoami", "args": {}})
    w.call({"op": "whoami", "args": {}})
    assert refusal(w, w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}))) == {"refusal": "busy"}
    monkeypatch.setattr(bridge, "MAX_RECORDS", 3)
    assert w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}})) == []  # above the host-wide cap: dropped


def test_the_start_fence_refuses_a_request_stamped_before_the_worker_started(w):
    w.clock.ms += 10_000
    w.reopen()
    meta = refusal(w, w.send(w.phone.request(w.wk, w.new_ticket(), ts_ms=T0)))
    assert meta == {"refusal": "already_done", "status": "unknown"}
    assert w.host.runs == []


# -- step 11 ----------------------------------------------------------------------------------------------------------


GRANT = "gr_" + "0" * 26 + "." + "A" * 43


@pytest.mark.parametrize(
    "meta,code",
    [
        ({"op": "whoami", "args": {}, "auth": {}}, "req.invalid"),
        ({"op": "whoami", "args": {}, "auth": {"grant": GRANT}}, "req.invalid"),
        ({"op": "ticket.get", "args": {"ref": GRANT}}, "grant.secret_in_args"),
        ({"op": "ticket.get", "args": {GRANT: 1}}, "grant.secret_in_args"),
        ({"op": "whoami", "args": {}, "extra": 1}, "req.invalid"),
        ({"op": "whoami"}, "req.invalid"),
        ({"op": "whoami", "args": []}, "req.invalid"),
        ({"op": "whoami", "args": {}, "idem": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}, "req.invalid"),
        ({"op": "whoami", "args": {}, "rd": "nope"}, "req.invalid"),
    ],
)
def test_the_meta_rules_before_any_column(w, meta, code):
    assert refusal(w, w.send(w.phone.request(w.wk, meta)))["refusal"] == code
    assert w.host.reads == [] and w.host.runs == []


def test_data_beside_the_meta_is_req_invalid(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, data=b"x")
    assert refusal(w, w.send(env))["refusal"] == "req.invalid"


FORBIDDEN = [
    "http", "cancel", "pair", "pair_status", "credential_begin", "credential_finish", "assert", "member_remove",
    "revocation", "drop_rewrap", "drop_claim", "decision", "relay.submit", "human.prepare", "human.submit", "new",
    "ask", "log", "artifact.add", "grant.issue", "session.list", "grant.list", "sub.open", "blob.put", "doctor",
    "host.status", "wait", "run.list", "term.open", "worktree.create", "records.list", "diff", "artifact.get",
    "quick.list", "commit.list", "list", "show", "TICKET.GET", "ticket.get ", "",
]  # fmt: skip


@pytest.mark.parametrize("op", FORBIDDEN)
def test_every_op_outside_the_closed_table_is_forbidden_scope(w, op):
    assert refusal(w, w.send(w.phone.request(w.wk, {"op": op, "args": {}}))) == {"refusal": "forbidden_scope"}
    assert w.host.reads == [] and w.host.runs == []


def test_the_table_is_closed_and_every_read_is_a_relay_op():
    from orch.host.columns import RELAY_OPS

    assert {s.backend for s in framing.OPS.values()} <= RELAY_OPS
    assert {k for k, s in framing.OPS.items() if s.kind == "write"} == {"new_ticket", "answer", "approve", "changes"}
    assert all(s.backend == "relay.submit" for s in framing.OPS.values() if s.kind == "write")


def test_the_grant_shape_is_the_hosts():
    from orch.host.auth import GRANT_SHAPE

    assert framing.GRANT_SHAPE.pattern == GRANT_SHAPE.pattern


def test_a_write_without_idem_is_idem_required_and_a_bad_one_idem_invalid(w):
    m = w.new_ticket()
    del m["idem"]
    assert refusal(w, w.send(w.phone.request(w.wk, m)))["refusal"] == "idem.required"
    m = w.new_ticket(idem="a_" + ulid())
    assert refusal(w, w.send(w.phone.request(w.wk, m)))["refusal"] == "idem.invalid"
    m = w.new_ticket(idem=ulid())  # well-formed, but not the event id: 14.6 P1 (review M2)
    assert refusal(w, w.send(w.phone.request(w.wk, m)))["refusal"] == "invalid.input"
    assert w.host.runs == []


@pytest.mark.parametrize("where", ["cert", "list"])
def test_the_scope_must_be_in_both_the_certificate_and_the_member_list(tmp_path, where):
    w = World(tmp_path)
    try:
        if where == "cert":
            p = Phone(("look",))
            w.r6.add(p, ("look", "decide"))
        else:
            p = Phone(("look", "decide"))
            w.r6.add(p, ("look",))
        w.phone = p
        assert refusal(w, w.send(p.request(w.wk, w.new_ticket())))["refusal"] == "forbidden_scope"
        assert w.call({"op": "whoami", "args": {}})[0] == {"status": "ok"}
    finally:
        w.close()


def test_a_look_decide_phone_creates_a_ticket(tmp_path):
    """I1: the 14.8 phone profile ``scopes_max: ["look","decide"]`` (no ``operate``) creates a ticket and answers."""
    w = World(tmp_path)
    try:
        p = Phone(("look", "decide"))
        w.r6.add(p)
        w.phone = p
        meta, data = w.answer(w.send(p.request(w.wk, w.new_ticket())))
        assert meta == {"status": "ok"} and data["data"]["seqs"] == [1]
        assert [op for op, _ in w.host.runs] == ["new_ticket"]
    finally:
        w.close()


def test_scope_reduced_between_receipt_and_run_is_refused_inside_the_job(w):
    env = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env))
    w.r6.add(w.phone, ("look",))
    w.bridge.run(adm)
    assert refusal(w, w.post.for_rid(env[40:56]))["refusal"] == "op.not_allowed"
    assert w.host.runs == []


def test_device_removed_between_receipt_and_run_is_refused_inside_the_job(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    adm = w.bridge.receive(w.item(env))
    del w.r6.members[w.phone.device]
    w.bridge.run(adm)
    assert refusal(w, w.post.for_rid(env[40:56]))["refusal"] == "op.not_allowed"


def test_two_devices_with_the_same_idem_never_collide(tmp_path):
    w = World(tmp_path)
    try:
        other = Phone()
        w.r6.add(other)
        m = w.new_ticket()
        w.call(m)
        w.phone, mine = other, w.phone
        ev = other.event(m["args"]["log"], "ticket.created", eid=m["idem"], key="DEMO-0077", ticket_type="bug",
                         title="other", owner="p_" + other.person)  # fmt: skip
        meta, data = w.call({"op": "new_ticket", "args": {"log": m["args"]["log"], "events": [ev]}, "idem": m["idem"]})
        # the outcome keys never collide (no replay of the first device's outcome, no idem.conflict); the log dedup
        # by event identity then sees the same id with another actor: event.duplicate_id (D), nothing runs again
        assert meta == {"refusal": "event.duplicate_id", "retryable": False} and data is None
        assert len(w.host.runs) == 1 and mine is not other
    finally:
        w.close()


# -- outcome.get (O1) -------------------------------------------------------------------------------------------------


def test_outcome_get_is_pending_for_an_admitted_write_and_before_the_bound(w):
    m = w.new_ticket()
    w.send(w.phone.request(w.wk, m), run=False)  # admitted, not run
    _, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    assert data["data"] == {"state": "pending"}
    fresh = ulid(w.clock.ms)
    _, data = w.call({"op": "outcome.get", "args": {"idem": fresh}})  # no record yet, inside the bound
    assert data["data"] == {"state": "pending"}
    w.clock.ms += framing.PENDING_NO_RECORD_MS
    meta = refusal(w, w.send(w.phone.request(w.wk, {"op": "outcome.get", "args": {"idem": fresh}}, ts_ms=w.clock.ms)))
    assert meta["refusal"] == "outcome.unknown_idem"


def test_outcome_get_reports_a_committed_write(w):
    m = w.new_ticket()
    w.call(m)
    _, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    assert data["data"]["state"] == "committed"


# -- epochs and rotation ----------------------------------------------------------------------------------------------


def test_a_rotation_while_it_ran_answers_already_done_not_data_under_the_old_epoch(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    adm = w.bridge.receive(w.item(env))
    w.epochs.rotate()
    w.bridge.run(adm)
    (chunk,) = w.post.for_rid(env[40:56])
    h, meta, _ = open_chunk(chunk, w.wk, w.key.public_key)
    assert h.epoch == 2 and meta == {"refusal": "already_done", "status": "unknown"}


def test_rotation_closes_an_open_answer_at_its_next_unused_index_and_keeps_attempted_chunks(w):
    w.post.script = {1: "retry"}  # chunk 0 posted, chunk 1 attempted (lost), the rest stored
    env = w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 3 * CHUNK_ROOM}})
    w.send(env)
    rid = env[40:56].hex()
    before = w.rids.get(w.phone.device, rid)
    assert [c.state for c in before.chunks][:2] == ["posted", "attempted"]
    w.epochs.rotate()
    w.bridge.rotate()
    after = w.rids.get(w.phone.device, rid)
    assert after.chunks[0].data == before.chunks[0].data and after.chunks[1].data == before.chunks[1].data
    assert [c.idx for c in after.chunks] == [0, 1, 2]
    h, meta, _ = open_chunk(after.chunks[2].data, w.wk, w.key.public_key)
    assert h.epoch == 2 and h.flags & wire.FLAG_REFUSAL and meta == {"refusal": "stale_epoch", "epoch": 3}


def test_rotation_of_an_answer_with_nothing_attempted_is_already_done(w):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    rid = env[40:56].hex()
    _unattempt(w)
    w.epochs.rotate()
    w.bridge.rotate()
    (c,) = w.rids.get(w.phone.device, rid).chunks
    assert open_chunk(c.data, w.wk, w.key.public_key)[1] == {"refusal": "already_done", "status": "unknown"}


# -- chunk POST outcomes ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,code,want",
    [
        (410, "expired", "expired"),
        (409, "retry_exhausted", "exhausted"),
        (409, "chunk_conflict", "conflict"),
        (409, "stale_epoch", "stale_epoch"),
        (403, "not_eligible", "dropped"),
        (403, "revoked", "dropped"),
        (429, "rate_limited", "retry"),
        (503, "overloaded", "retry"),
    ],
)
def test_classify_post(status, code, want):
    assert classify_post(RelayRefused(status, code)) == want


def test_a_lost_post_keeps_the_chunk_attempted_and_the_same_bytes_go_again(w):
    w.post.script = {0: "retry"}
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    rid = env[40:56].hex()
    assert w.rids.get(w.phone.device, rid).chunks[0].state == "attempted"
    w.bridge.flush()
    assert len(w.post.for_rid(rid)) == 1  # backoff: not before 1 s
    w.clock.ms += 1_000
    w.bridge.flush()
    first, second = w.post.for_rid(rid)
    assert first == second and w.rids.get(w.phone.device, rid).chunks[0].state == "posted"


def test_chunk_conflict_alarms_and_never_posts_other_bytes(w):
    w.post.script = {0: "conflict"}
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    assert w.alarms and w.alarms[0][0] == "chunk_conflict"
    w.bridge.flush()
    assert len(w.post.posts) == 1


def test_expired_stops_and_an_identical_resend_restores_the_same_bytes(w):
    w.post.script = {0: "expired"}
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    first = w.send(env)
    w.bridge.flush()
    assert len(w.post.posts) == 1  # never re-posted on a timer
    assert w.send(env) == first  # the device's resend is the recovery


# -- retry classes (O4) -----------------------------------------------------------------------------------------------


def test_the_retry_table_covers_every_code_the_bridge_seals_and_unknown_is_d():
    sealed = {
        "not_member", "revoked", "bad_signature", "busy", "malformed", "stale_sequence", "stale_timestamp",
        "stale_epoch", "forbidden_scope", "already_done", "rid_conflict", "req.invalid", "grant.secret_in_args",
        "idem.required", "idem.invalid", "event.expired", "op.not_allowed", "invalid.input", "frame.too_large",
        "internal", "outcome.unknown_idem", "idem.conflict", "event.duplicate_id", "ticket.exists",
        "ticket.key_not_next", "not_found",
    }  # fmt: skip
    assert sealed <= set(framing.RETRY)
    assert set(framing.RETRY.values()) == {"A", "B", "C-auto", "C-review", "D"}
    assert framing.retry_class("no.such.code") == "D"
    assert framing.retry_class("stale_epoch") == "A" and framing.retry_class("rid_conflict") == "B"


def test_answers_carry_no_request_text(w):
    secret_title = "TOP-SECRET-" + secrets.token_hex(8)
    m = w.new_ticket(title=secret_title)
    m["idem"] = ulid()  # idem.invalid
    (chunk,) = w.send(w.phone.request(w.wk, m))
    _, meta, data = open_chunk(chunk, w.wk, w.key.public_key)
    assert secret_title not in json.dumps(meta) and data == b""


def test_identity_principal_and_ws(w):
    _, data = w.call({"op": "whoami", "args": {}})
    assert data["data"]["principal"] == "r:d_" + w.phone.device
    assert WS and crypto
