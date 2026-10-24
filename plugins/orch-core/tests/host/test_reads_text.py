"""H6b ``ticket.text`` (§0.8.5, cap ``text/1``) through a real host: the pairs table, UTF-8 byte chunks that never split
a scalar value, the event identity, and the visibility of ``ticket.get``."""

from __future__ import annotations

import pytest

from orch.identity import new_ulid
from tests.host.human_helpers import Human, err, ok
from tests.host.live_helpers import world as make_world
from tests.host.test_dispatch_matrix import as_agent_class
from tests.host.test_reads_ticket import agent_ev, events_of, full, get


@pytest.fixture
def w(tmp_path):
    w = make_world(tmp_path)
    try:
        yield w
    finally:
        w.close()
        w.lab.close()


def text(c, ref, seq, field="text", **args):
    return ok(c.req("ticket.text", {"ref": ref, "seq": seq, "field": field, **args}))["data"]


def read_all(c, ref, seq, field="text", length=None):
    """Chunks until ``next`` is null, as a client must: the first response's ``event_id`` and ``uid`` continue it."""
    args = {} if length is None else {"length": length}
    first = text(c, ref, seq, field, **args)
    chunks, res = [first], first
    while res["next"] is not None:
        res = text(c, first["uid"], seq, field, event_id=first["event_id"], offset=res["next"], **args)
        chunks.append(res)
    return chunks


def comment(w, key, body):
    """The seq of a new ``log.added`` with this text."""
    w.write(key, body)
    return events_of(w, w.lab.ws.uid(key))[-1]["seq"]


# --- the pairs table ----------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_every_pair_of_the_table_serves_the_stored_text(w):
    h = Human(w)
    f = full(w, message="verdict text " + "ü" * 700)  # a pass verdict with a text longer than a Snip
    uid, key = f.uid, f.key
    served = {}  # (type, field) -> (seq, stored text)

    def note(typ, field):
        e = [e for e in events_of(w, uid) if e["type"] == typ][-1]
        served[(typ, field)] = (e["seq"], e[field])

    note("task.done", "text")
    note("verdict.given", "text")
    # reopen the settled ticket and exercise the rest on a fresh one
    uid2, key2 = w.ticket("pairs")
    h.agent("claim", {"ref": key2})
    h.agent("log", {"ref": key2, "text": "a comment é€😀"})
    h.agent("task.add", {"ref": key2, "text": "t"})
    agent_ev(w, uid2, "task.blocked", task="T1", reason="blocked é")
    agent_ev(w, uid2, "task.reopened", task="T1", reason="again €")
    agent_ev(w, uid2, "task.skipped", task="T1", reason="skipped 😀")
    h.agent("ask", {"ref": key2, "text": "free answer?", "text_question": True})
    h.run("answer", {"ref": key2, "question": "Q1", "message": "an answer é"})
    g = get(h.c, key2)["gates"]["requirements"]
    w.person_event(
        "gate.changes_requested", uid2, gate="requirements", gate_gen=g["gen"], hash=g["hash"],
        policy_hash=g["policy_hash"], text="please change é",
    )  # fmt: skip
    agent_ev(w, uid2, "handoff.written", text="handed over €")
    h.agent("release", {"ref": key2})  # back to open
    agent_ev(w, uid2, "status.changed", reason="parked 😀", **{"from": "open", "to": "backlog"})
    w.person_event("ticket.moved", uid2, reason="back on é", **{"from": "backlog", "to": "open"})
    h.run("close", {"ref": key2, "resolution": "other", "message": "closing €"})
    h.run("reopen", {"ref": key2, "message": "reopening 😀"})
    for typ, field in (
        ("log.added", "text"), ("task.blocked", "reason"), ("task.reopened", "reason"), ("task.skipped", "reason"),
        ("question.answered", "text"), ("gate.changes_requested", "text"), ("handoff.written", "text"),
        ("status.changed", "reason"), ("ticket.moved", "reason"), ("ticket.closed", "text"),
        ("ticket.reopened", "text"),
    ):  # fmt: skip
        e = next(e for e in events_of(w, uid2) if e["type"] == typ)
        served[(typ, field)] = (e["seq"], e[field])
    assert set(served) == {
        ("log.added", "text"), ("handoff.written", "text"), ("ticket.closed", "text"), ("ticket.reopened", "text"),
        ("question.answered", "text"), ("gate.changes_requested", "text"), ("verdict.given", "text"),
        ("task.done", "text"), ("task.skipped", "reason"), ("task.blocked", "reason"), ("task.reopened", "reason"),
        ("status.changed", "reason"), ("ticket.moved", "reason"),
    }  # fmt: skip
    for (typ, field), (seq, stored) in served.items():
        ref = uid if typ in ("task.done", "verdict.given") else uid2
        res = text(h.c, ref, seq, field)
        assert res["text"] == stored and res["bytes"] == len(stored.encode()) and res["next"] is None, (typ, field)
        assert (res["type"], res["field"], res["offset"], res["uid"]) == (typ, field, 0, ref)
        event = next(e for e in events_of(w, ref) if e["seq"] == seq)
        assert (
            res["event_id"] == event["id"]
            and res["at"] == event["at"]
            and res["actor"]["kind"] == event["actor"]["kind"]
        )
        assert "sig" not in res["actor"] and "host_sig" not in res["actor"]
    # a Snip is cut at 1 024 bytes in ticket.get; ticket.text has the rest
    seq, stored = served[("verdict.given", "text")]
    assert len(stored.encode()) > 1024
    verdict = get(h.c, key)["verdict"]
    assert verdict["text_cut"] is True and stored.startswith(verdict["text"]) and verdict["seq"] == seq
    assert text(h.c, key, seq)["text"] == stored


# --- chunks ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("length", [4, 5, 6, 7, 8, 9, 13, 64])
def test_chunks_never_split_a_scalar_and_concatenate_to_the_stored_text(w, length):
    uid, key = w.ticket("chunks")
    body = ("a" + "é" + "€" + "😀") * 40 + "end"  # 1, 2, 3 and 4-byte scalars
    seq = comment(w, key, body)
    chunks = read_all(w.client(), key, seq, length=length)
    assert "".join(c["text"] for c in chunks) == body
    raw = len(body.encode())
    offset = 0
    for c in chunks:
        assert c["offset"] == offset and c["bytes"] == raw and c["text"] != "" and len(c["text"].encode()) <= length
        assert c["next"] is None or c["next"] == offset + len(c["text"].encode()) > offset
        offset = c["next"] if c["next"] is not None else raw
    assert chunks[-1]["next"] is None and offset == raw
    assert len({c["event_id"] for c in chunks}) == 1


def test_length_4_makes_nonempty_progress_on_four_byte_scalars(w):
    uid, key = w.ticket("emoji")
    seq = comment(w, key, "😀😁😂")
    c = w.client()
    assert [(r["text"], r["offset"], r["next"]) for r in read_all(c, key, seq, length=4)] == [
        ("😀", 0, 4), ("😁", 4, 8), ("😂", 8, None),
    ]  # fmt: skip
    one = text(c, key, seq, length=5)  # 5 bytes: one scalar and one byte of the next, which is not split
    assert (one["text"], one["next"], one["bytes"]) == ("😀", 4, 12)
    assert text(c, key, seq, length=7)["text"] == "😀" and text(c, key, seq, length=8)["text"] == "😀😁"


def test_the_default_length_takes_a_comment_in_one_chunk(w):
    """F1 caps every stored string at 4 096 bytes, so a text takes one chunk at the default length (262 144)."""
    uid, key = w.ticket("long")
    seq = comment(w, key, "x" * 4096)
    c = w.client()
    r = text(c, key, seq)
    assert r["bytes"] == 4096 and r["next"] is None and len(r["text"]) == 4096 and r["offset"] == 0
    assert text(c, key, seq, length=262144)["text"] == r["text"]
    two = read_all(c, key, seq, length=3000)
    assert [x["offset"] for x in two] == [0, 3000] and "".join(x["text"] for x in two) == "x" * 4096


def test_offset_at_the_end_is_empty_and_past_it_or_inside_a_scalar_is_invalid(w):
    uid, key = w.ticket("offsets")
    seq = comment(w, key, "a😀b")  # bytes: a(0) 😀(1..4) b(5), total 6
    c = w.client()
    first = text(c, key, seq, length=4)
    eid = first["event_id"]
    assert first["bytes"] == 6 and (first["text"], first["next"]) == ("a", 1)
    end = text(c, uid, seq, event_id=eid, offset=6)
    assert (end["text"], end["next"], end["offset"], end["bytes"]) == ("", None, 6, 6)
    assert text(c, uid, seq, event_id=eid, offset=5)["text"] == "b"
    for bad in (7, 100):  # past the text
        assert err(c.req("ticket.text", {"ref": uid, "seq": seq, "field": "text", "event_id": eid, "offset": bad})) == (
            "invalid.input"
        )
    for inside in (2, 3, 4):  # inside the emoji
        assert err(
            c.req("ticket.text", {"ref": uid, "seq": seq, "field": "text", "event_id": eid, "offset": inside})
        ) == ("invalid.input")
    # offset > 0 needs the event_id
    assert err(c.req("ticket.text", {"ref": uid, "seq": seq, "field": "text", "offset": 1})) == "invalid.input"
    assert err(c.req("ticket.text", {"ref": uid, "seq": seq, "field": "text", "length": 3})) == "invalid.input"
    assert err(c.req("ticket.text", {"ref": uid, "seq": 0, "field": "text"})) == "invalid.input"
    assert err(c.req("ticket.text", {"ref": uid, "seq": seq, "field": "body"})) == "invalid.input"


# --- event identity -------------------------------------------------------------------------------------------------


def test_an_event_id_that_is_not_the_event_at_seq_is_not_found(w):
    """A wrong-but-valid ULID, and the id of another event of the same ticket. (Simulating a ``restore`` that abandons
    the event and reuses its seq needs a signed restore on a diverged log; the check is the same one: the id at
    ``seq`` now is not the id the client holds.)"""
    uid, key = w.ticket("identity")
    first, second = comment(w, key, "first"), comment(w, key, "second")
    c = w.client()
    held = text(c, key, first, length=4)
    other = text(c, key, second)["event_id"]
    for eid in (new_ulid(), other):
        args = {"ref": uid, "seq": first, "field": "text", "event_id": eid}
        assert err(c.req("ticket.text", args)) == "not_found"
        assert err(c.req("ticket.text", {**args, "offset": 4})) == "not_found"
    assert text(c, uid, first, event_id=held["event_id"], offset=4)["text"] == "t"
    # the event addressed by another ticket's ref is not found either
    uid2, key2 = w.ticket("elsewhere")
    assert (
        err(c.req("ticket.text", {"ref": key2, "seq": first, "field": "text", "event_id": held["event_id"]}))
        == "not_found"
    )


def test_a_field_the_event_lacks_or_another_type_is_not_found(w):
    h = Human(w)
    uid, key = w.ticket("lacks")
    h.agent("claim", {"ref": key})
    note = comment(w, key, "a note")
    h.run("close", {"ref": key, "resolution": "other"})  # no text
    closed = events_of(w, uid)[-1]["seq"]
    h.run("reopen", {"ref": key})  # no text
    reopened = events_of(w, uid)[-1]["seq"]
    c = h.c

    def code(seq, field="text", ref=key):
        return err(c.req("ticket.text", {"ref": ref, "seq": seq, "field": field}))

    assert code(closed) == "not_found" and code(reopened) == "not_found"  # the event has no text
    assert code(note, "reason") == "not_found"  # a log.added has text, not reason
    assert code(1) == "not_found" and code(2) == "not_found"  # ticket.created, claim.taken: not in the table
    assert code(reopened + 50) == "not_found" and code(10_000) == "not_found"  # no such event
    assert code(note, ref="DEMO-9999") == "not_found"
    assert code(1, ref="workspace") == "not_found"  # the workspace log is not a ticket: its texts are never served
    assert text(c, key, note)["text"] == "a note"


# --- visibility -----------------------------------------------------------------------------------------------------


def test_a_restricted_ticket_is_not_found_and_visibility_is_checked_on_every_request(w):
    uid, key = w.ticket("visible then hidden")
    seq = comment(w, key, "é" * 20)
    first = text(w.client(), key, seq, length=4)
    as_agent_class(w)
    n = w.client()
    assert text(n, key, seq)["text"] == "é" * 20  # a workspace ticket: no-person reads it
    w.restrict(uid, [w.lab.owner])
    assert err(n.req("ticket.text", {"ref": key, "seq": seq, "field": "text"})) == "not_found"  # same as a missing ref
    assert err(n.req("ticket.text", {"ref": "DEMO-9999", "seq": seq, "field": "text"})) == "not_found"
    follow = {"ref": first["uid"], "seq": seq, "field": "text", "event_id": first["event_id"], "offset": first["next"]}
    assert err(n.req("ticket.text", follow)) == "not_found"  # a continuation is checked again


def test_a_member_off_the_list_gets_not_found(tmp_path):
    q = make_world(tmp_path, map_to="quinn")
    try:
        uid, key = q.ticket("secret")
        seq = comment(q, key, "the secret text")
        q.restrict(uid, [q.lab.owner])
        assert err(q.client().req("ticket.text", {"ref": key, "seq": seq, "field": "text"})) == "not_found"
        q.restrict(uid, [q.lab.owner, q.quinn.ref])
        assert text(q.client(), key, seq)["text"] == "the secret text"
    finally:
        q.close()
        q.lab.close()
